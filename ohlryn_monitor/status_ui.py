#!/usr/bin/env python3
"""올린 모니터 상태 대시보드 — stdlib http.server 단일 파일 웹 UI.

"어떤 기능(cron)이 있고, 각각 잘 돌고 있는가"를 한 화면에:
  - crontab 자동 파싱 (이름·주기·로그 경로) → 새 기능이 늘어도 등록 없이 표시
  - 로그 신선도/에러로 🟢/🔴 판정 (판정 로직: cronstatus.py — 순수, 테스트됨)
  - crash-loop 차단 플래그 (wd_state/*.crashloop)
  - 이름·설명은 config의 descriptions 맵에서 (미등록 작업도 표시됨)

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

from ohlryn_monitor.cronstatus import judge, max_gap_minutes, parse_crontab
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
                "schedule": job["schedule"],
                "gap_min": gap,
                "log": job["log"],
                "last_run": mtime.isoformat() if mtime else None,
                "status": st["status"],
                "detail": st["detail"],
            }
        )

    flags = []
    for p in sorted(glob.glob(os.path.join(cfg.get("wd_state_dir", ""), "*.crashloop"))):
        try:
            reason = open(p).read().strip()
        except OSError:
            reason = "?"
        flags.append({"flag": p, "reason": reason})

    return {"now": now.isoformat(), "jobs": jobs, "crashloop_flags": flags}


def render_html(data: dict, title: str) -> str:
    e = html.escape
    rows = []
    for j in data["jobs"]:
        icon = _ICON.get(j["status"], "❔")
        rows.append(
            f"<tr><td>{icon}</td><td><b>{e(j['name'])}</b><br><span class=desc>{e(j['description'])}</span></td>"
            f"<td class=mono>{e(j['schedule'])}</td>"
            f"<td class=mono>{e(str(j['last_run'] or '-')[:16])}</td>"
            f"<td class=mono>{e(j['detail'])}</td></tr>"
        )
    flags_html = (
        "<p class=ok>crash-loop 차단 플래그: 없음 ✅</p>"
        if not data["crashloop_flags"]
        else "".join(
            f"<p class=bad>🚨 {e(f['flag'])} — {e(f['reason'])}</p>" for f in data["crashloop_flags"]
        )
    )
    n_bad = sum(1 for j in data["jobs"] if j["status"] in ("error", "stale"))
    summary = "모든 작업 정상 🟢" if n_bad == 0 else f"문제 작업 {n_bad}개 🔴"
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>{e(title)}</title><style>
body{{background:#0a0b0f;color:#d8dce4;font-family:'JetBrains Mono',ui-monospace,monospace;margin:2rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #23262e;padding:.45rem .6rem;text-align:left;vertical-align:top}}
th{{color:#e2c044}}.mono{{font-size:.78rem;color:#9aa3b2}}.desc{{font-size:.75rem;color:#6b7484}}
.ok{{color:#34d399}}.bad{{color:#f87171}}h1{{font-size:1.05rem}}h1 span{{color:#6b7484;font-size:.8rem}}
</style></head><body>
<h1>{e(title)} <span>{e(data['now'][:19])}Z · {summary}</span> <a href=\"/\" style=\"color:#e2c044;text-decoration:none;border:1px solid #e2c044;padding:.15rem .6rem;font-size:.8rem\">↻ 새로고침</a></h1>
<table><tr><th></th><th>작업 / 설명</th><th>주기</th><th>마지막 기록(UTC)</th><th>상태 상세</th></tr>
{''.join(rows)}</table>
{flags_html}
</body></html>"""


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
