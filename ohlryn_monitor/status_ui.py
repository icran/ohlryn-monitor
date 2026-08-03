#!/usr/bin/env python3
"""올린 모니터 상태 대시보드 — stdlib http.server 단일 파일 웹 UI.

"어떤 기능(cron)이 있고, 각각 잘 돌고 있는가"를 한 화면에:
  - crontab 자동 파싱 (이름·주기·로그 경로) → 새 기능이 늘어도 등록 없이 표시
  - 로그 신선도/에러로 🟢/🔴 판정 (판정 로직: cronstatus.py — 순수, 테스트됨)
  - 봇 현황: config "bots"(health_check와 동일 형식) 지정 시 /api/v1/bots 조회 →
    봇별 전략(config)·페어·중지/정체 표시 (요약 로직: botstatus.py — 순수, 테스트됨)
  - crash-loop 차단 플래그 (wd_state/*.crashloop)
  - 이름·설명은 config의 descriptions 맵에서 (미등록 작업도 표시됨)
  - 모든 섹션은 <details>로 접힘 — 문제(error/stale/down/warn) 있는 섹션만 펼쳐진 채 시작

Usage:
  python3 -m ohlryn_monitor.status_ui --config config/status_ui.myserver.json
  python3 -m ohlryn_monitor.status_ui --config ... --once   # HTML 1회 출력(테스트/검증)

보안: 기본 127.0.0.1 바인딩(인터넷 비노출) — SSH 터널로 접근:
  ssh -L 8020:localhost:8020 <서버>  →  브라우저 http://localhost:8020
+ HTTP Basic Auth. 읽기 전용 — 어떤 조작 엔드포인트도 없다.
"""

import argparse
import base64
import glob
import html
import json
import os
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ohlryn_monitor.alerters.health_check import fetch_bot_engines
from ohlryn_monitor.botstatus import summarize_bot
from ohlryn_monitor.cronstatus import humanize_schedule, judge, max_gap_minutes, parse_crontab
from ohlryn_monitor.notify import parse_env

_ICON = {"ok": "🟢", "error": "🔴", "stale": "🔴", "event": "⚪", "unknown": "❔"}
_TAIL_BYTES = 4000


def _read_tail(path: str) -> tuple[datetime | None, str]:
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            tail = f.read().decode(errors="replace")
        return mtime, tail
    except OSError:
        return None, ""


def collect_status(cfg: dict, crontab_text: str | None = None) -> dict:
    """대시보드 데이터 수집 (I/O) — HTML/JSON 양쪽이 공유."""
    now = datetime.now(timezone.utc)
    if crontab_text is None and cfg.get("crontab_file"):
        try:
            crontab_text = open(cfg["crontab_file"]).read()
        except OSError:
            crontab_text = ""
    if crontab_text is None:
        try:
            crontab_text = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True, timeout=10
            ).stdout
        except Exception:  # noqa: BLE001
            crontab_text = ""

    descriptions = cfg.get("descriptions", {})
    categories = cfg.get("categories", {})
    event_driven = set(cfg.get("event_driven", []))

    jobs = []
    for job in parse_crontab(crontab_text):
        gap = max_gap_minutes(job["schedule"])
        mtime, tail = _read_tail(job["log"]) if job["log"] else (None, "")
        st = judge(now, mtime, gap, tail, event_driven=job["name"] in event_driven)
        jobs.append(
            {
                "name": job["name"],
                "description": descriptions.get(job["name"], ""),
                "category": categories.get(job["name"], "기타"),
                "schedule": job["schedule"],
                "schedule_human": humanize_schedule(job["schedule"]),
                "gap_min": gap,
                "log": job["log"],
                "last_run": mtime.isoformat() if mtime else None,
                "status": st["status"],
                "detail": st["detail"],
            }
        )

    # 봇 현황 — config "bots"(health_check와 동일 형식: name/port/env) 지정 시에만 조회
    bots = []
    for bot in cfg.get("bots", []):
        engines, err = fetch_bot_engines(cfg.get("repo", ""), bot)
        bots.append(
            summarize_bot(
                bot["name"], engines, now=now, stale_minutes=cfg.get("stale_minutes", 40), error=err
            )
        )

    flags = []
    for p in sorted(glob.glob(os.path.join(cfg.get("wd_state_dir", ""), "*.crashloop"))):
        try:
            reason = open(p).read().strip()
        except OSError:
            reason = "?"
        flags.append({"flag": p, "reason": reason})

    return {"now": now.isoformat(), "jobs": jobs, "bots": bots, "crashloop_flags": flags}


def render_html(data: dict, title: str) -> str:
    e = html.escape
    badge = {
        "ok": ("badge-ok", "정상"),
        "error": ("badge-danger", "오류"),
        "stale": ("badge-danger", "미실행 의심"),
        "event": ("badge-info", "이벤트 대기"),
        "unknown": ("badge-warn", "첫 실행 전"),
    }
    def _details_card(head: str, n_items: int, n_bad: int, body: str) -> str:
        """접히는 섹션 카드 — 문제가 있으면 펼쳐진 채 시작, 정상이면 접힘."""
        chip = (
            f"<span class='badge badge-danger'>문제 {n_bad}</span>"
            if n_bad
            else "<span class='badge badge-ok'>정상</span>"
        )
        open_attr = " open" if n_bad else ""
        return (
            f"<details class=card{open_attr}><summary class=card-head><span class=chev></span>"
            f"{head} <span class=count>{n_items}</span><span class=spacer></span>{chip}</summary>"
            f"{body}</details>"
        )

    sections = []

    # ── 봇 현황 (config "bots" 지정 시) ─────────────────────────────
    bots = data.get("bots") or []
    if bots:
        bot_badge = {
            "ok": ("badge-ok", "정상"),
            "down": ("badge-danger", "응답 없음"),
            "warn": ("badge-danger", "문제"),
        }
        rows = []
        for b in bots:
            cls, label = bot_badge.get(b["status"], ("badge-warn", b["status"]))
            strat_html = ""
            for s in b["strategies"]:
                icon = "🟢" if s["status"] == "ok" else "🔴"
                pairs = ", ".join(s["pair_names"][:6]) + ("…" if len(s["pair_names"]) > 6 else "")
                probs = ""
                if s["stopped"]:
                    probs += f" <span class=prob>⏸ 중지: {e(', '.join(s['stopped']))}</span>"
                if s["stale"]:
                    probs += f" <span class=prob>🧟 정체: {e(', '.join(s['stale']))}</span>"
                strat_html += (
                    f"<div class=strat>{icon} <b>{e(s['config'])}</b> "
                    f"<span class=mono>{len(s['pair_names'])}페어 · {e(pairs)}</span>{probs}</div>"
                )
            rows.append(
                f"<tr><td><span class='badge {cls}'>{label}</span></td>"
                f"<td><div class=name>{e(b['name'])}</div></td>"
                f"<td>{strat_html or '-'}</td>"
                f"<td class='mono detail'>{e(b['detail'])}</td></tr>"
            )
        n_bot_bad = sum(1 for b in bots if b["status"] != "ok")
        sections.append(
            _details_card(
                "🤖 봇 현황",
                len(bots),
                n_bot_bad,
                "<table><tr><th>상태</th><th>봇</th><th>실행 중인 전략</th><th>상세</th></tr>"
                + "".join(rows)
                + "</table>",
            )
        )

    # ── cron 작업 (카테고리별) ──────────────────────────────────────
    order = ["봇 감시", "알림", "데이터 수집", "기타"]
    groups: dict = {}
    for j in data["jobs"]:
        groups.setdefault(j.get("category", "기타"), []).append(j)

    for cat in order + [c for c in groups if c not in order]:
        if cat not in groups:
            continue
        rows = []
        for j in sorted(groups[cat], key=lambda x: x["name"]):
            cls, label = badge.get(j["status"], ("badge-warn", j["status"]))
            rows.append(
                f"<tr><td><span class='badge {cls}'>{label}</span></td>"
                f"<td><div class=name>{e(j['name'])}</div><div class=desc>{e(j['description'])}</div></td>"
                f"<td class=mono title=\"{e(j['schedule'])}\">{e(j.get('schedule_human', j['schedule']))}</td>"
                f"<td class=mono>{e(str(j['last_run'] or '-')[:16])}</td>"
                f"<td class=mono detail>{e(j['detail'])}</td></tr>"
            )
        n_cat_bad = sum(1 for j in groups[cat] if j["status"] in ("error", "stale"))
        sections.append(
            _details_card(
                e(cat),
                len(groups[cat]),
                n_cat_bad,
                "<table><tr><th>상태</th><th>작업</th><th>주기</th><th>마지막 기록(UTC)</th><th>상세</th></tr>"
                + "".join(rows)
                + "</table>",
            )
        )

    if data["crashloop_flags"]:
        flags_html = "".join(
            f"<div class='card flag-card'>🚨 <b>crash-loop 차단</b> — {e(f['flag'])}<br><span class=mono>{e(f['reason'])}</span></div>"
            for f in data["crashloop_flags"]
        )
    else:
        flags_html = "<div class='card ok-card'>crash-loop 차단 플래그: 없음 ✅</div>"

    n_bad = sum(1 for j in data["jobs"] if j["status"] in ("error", "stale")) + sum(
        1 for b in bots if b["status"] != "ok"
    )
    summary = (
        "<span class='badge badge-ok'>모두 정상</span>"
        if n_bad == 0
        else f"<span class='badge badge-danger'>문제 {n_bad}개</span>"
    )
    return f"""<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{e(title)}</title><style>
:root{{--bg:#f6f7f9;--surface:#fff;--line:#e8eaed;--text:#1b1f27;--text-sub:#6b7280;--text-faint:#9aa1ab;
--accent:#0d9268;--accent-soft:#e6f5ef;--accent-deep:#0a7a57;--warn-soft:#fdf3e2;--warn-text:#9a6b1a;
--info-soft:#edf1f7;--info-text:#4a5a75;--danger-soft:#fdeeee;--danger-text:#b04343;--r:14px}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:"Pretendard",-apple-system,BlinkMacSystemFont,system-ui,sans-serif;background:var(--bg);
color:var(--text);line-height:1.6;-webkit-font-smoothing:antialiased}}
.nav{{position:sticky;top:0;z-index:50;background:rgba(255,255,255,.92);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}}
.nav-inner{{max-width:1120px;margin:0 auto;padding:0 20px;height:64px;display:flex;align-items:center;gap:14px}}
.logo-mark{{width:28px;height:28px;border-radius:9px;background:var(--accent);display:inline-flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:15px}}
.logo{{font-weight:800;font-size:19px;letter-spacing:-.02em}}
.stamp{{color:var(--text-faint);font-size:13px;flex:1}}
.btn-refresh{{background:var(--accent);color:#fff;font-weight:700;font-size:14px;padding:9px 18px;border-radius:10px;text-decoration:none;transition:background .15s}}
.btn-refresh:hover{{background:var(--accent-deep)}}
.page{{max-width:1120px;margin:0 auto;padding:28px 20px 80px;display:flex;flex-direction:column;gap:18px}}
.card{{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}}
.card-head{{padding:14px 20px;font-weight:800;font-size:15px;display:flex;align-items:center;gap:8px}}
details.card>.card-head{{cursor:pointer;list-style:none;user-select:none}}
details.card>.card-head::-webkit-details-marker{{display:none}}
details.card[open]>.card-head{{border-bottom:1px solid var(--line)}}
.chev::before{{content:'▸';color:var(--text-faint);font-size:13px}}
details[open]>summary .chev::before{{content:'▾'}}
.spacer{{flex:1}}
.strat{{font-size:13.5px;padding:2px 0}}
.prob{{color:var(--danger-text);font-size:12.5px;font-weight:700}}
.count{{background:var(--bg);color:var(--text-sub);font-size:12px;font-weight:700;padding:1px 9px;border-radius:999px}}
table{{border-collapse:collapse;width:100%}}
th{{font-size:12px;color:var(--text-faint);font-weight:700;text-align:left;padding:9px 14px;border-bottom:1px solid var(--line)}}
td{{padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}}
tr:last-child td{{border-bottom:none}}
.name{{font-weight:700}}.desc{{font-size:12.5px;color:var(--text-sub)}}
.mono{{font-family:"SF Mono","Menlo",monospace;font-size:12.5px;color:#40485a}}
.detail{{max-width:340px;word-break:break-all;color:var(--text-faint)}}
.badge{{display:inline-flex;align-items:center;font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px;white-space:nowrap}}
.badge-ok{{background:var(--accent-soft);color:var(--accent-deep)}}
.badge-danger{{background:var(--danger-soft);color:var(--danger-text)}}
.badge-info{{background:var(--info-soft);color:var(--info-text)}}
.badge-warn{{background:var(--warn-soft);color:var(--warn-text)}}
.ok-card{{padding:14px 20px;color:var(--accent-deep);font-weight:600}}
.flag-card{{padding:14px 20px;background:var(--danger-soft);border-color:#f5cccc;color:var(--danger-text)}}
@media (max-width:720px){{.detail{{display:none}}th:nth-child(5){{display:none}}}}
</style></head><body>
<div class=nav><div class=nav-inner>
<span class=logo-mark>O</span><span class=logo>{e(title)}</span>
<span class=stamp>{e(data['now'][:19])}Z · {summary}</span>
<a class=btn-refresh href="/">↻ 새로고침</a>
</div></div>
<div class=page>
{''.join(sections)}
{flags_html}
</div></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    cfg: dict = {}
    auth: str | None = None
    title: str = "올린 모니터"

    def _authorized(self) -> bool:
        if self.auth is None:
            return True
        return self.headers.get("Authorization") == f"Basic {self.auth}"

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="ohlryn-monitor"')
            self.end_headers()
            return
        data = collect_status(self.cfg)
        if self.path.startswith("/api"):
            body = json.dumps(data, ensure_ascii=False).encode()
            ctype = "application/json; charset=utf-8"
        else:
            body = render_html(data, self.title).encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 요청 로그 침묵 (cron 로그 오염 방지)
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--once", action="store_true", help="서버 대신 HTML 1회 출력")
    args = ap.parse_args()
    with open(args.config) as f:
        cfg = json.load(f)

    if args.once:
        print(render_html(collect_status(cfg), cfg.get("title", "올린 모니터")))
        return

    env = parse_env(cfg["env"])
    user, pw = env.get("WEB_USERNAME", ""), env.get("WEB_PASSWORD", "")
    if not user or not pw:
        raise SystemExit("env에 WEB_USERNAME/WEB_PASSWORD 필요 (무인증 공개 서빙은 거부)")
    _Handler.cfg = cfg
    _Handler.title = cfg.get("title", "올린 모니터")
    _Handler.auth = base64.b64encode(f"{user}:{pw}".encode()).decode()

    port = int(cfg.get("port", 8020))
    # 기본은 localhost 바인딩 — 인터넷 비노출, SSH 터널로 접근.
    # 외부 노출이 필요하면 config에 "bind": "0.0.0.0" (방화벽/SG는 별도).
    bind = cfg.get("bind", "127.0.0.1")
    print(f"status UI: {bind}:{port} (basic auth)")
    ThreadingHTTPServer((bind, port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
