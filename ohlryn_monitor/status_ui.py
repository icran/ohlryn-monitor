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
import urllib.parse
import urllib.request
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


def _anchor_at(anchors: dict, date: str, back_days: int = 10) -> dict | None:
    """해당 날짜의 앵커. 휴장일이면 직전 거래일까지 최대 back_days 되짚는다."""
    from datetime import date as _date
    from datetime import timedelta as _td

    try:
        d = _date.fromisoformat(str(date)[:10])
    except ValueError:
        return None
    for i in range(back_days + 1):
        hit = anchors.get(str(d - _td(days=i)))
        if hit:
            return hit
    return None


def _bt_now(cyc: dict) -> dict | None:
    """백테스트 곡선이 지금 어느 낙폭에 있는가 — 사이클 앵커의 마지막 점.

    역대 MDD(정적 최댓값)와 다른 값이다. 라이브 낙폭과 나란히 놓아야 "전략 자체가
    낙폭 중"인지 "우리 집행만 나쁜지"가 갈린다. ⚠ 사이클 곡선은 **청산 시점** 인덱스라
    마지막 점의 날짜가 데이터 끝과 다를 수 있다(마지막 거래가 닫힌 날).
    """
    anc = (cyc or {}).get("anchors") or {}
    if not anc:
        return None
    d = list(anc)[-1]
    a = anc[d]
    mdd = cyc.get("mdd")
    return {"date": d, "drawdown": a["drawdown"], "uw_days": a["uw_days"],
            "ratio": abs(a["drawdown"] / mdd) if mdd else None}


def collect_mdd(cfg: dict) -> dict:
    """전략×티커별 낙폭 — 봇 DB(읽기 전용) + 백테스트 기준선.

    백테스트를 매일 다시 돌리지 않는다. 기준선은 데이터가 갱신될 때만 재생성하고
    (natas_reference_mdd.py), 여기서는 라이브 거래만 읽어 앵커에 이어 붙인다.
    """
    import sqlite3

    from ohlryn_monitor.mdd import (
        group_key, leg_status, live_curve, returns_since, status_level, trade_returns,
    )

    mc = cfg.get("mdd")
    if not mc:
        return {}
    try:
        ref_doc = json.loads(open(mc["reference"]).read())
    except OSError as exc:
        return {"error": f"기준선 파일 읽기 실패: {exc}"}
    prefix = ref_doc.get("strategy_prefix", {})
    legs_ref = ref_doc["legs"]

    # (전략, 티커) → {계좌: [(청산일, 수익률)]}, 첫 거래일
    live: dict[tuple[str, str], dict[str, list]] = {}
    first: dict[tuple[str, str, str], str] = {}
    for acct in mc["accounts"]:
        try:
            con = sqlite3.connect(f"file:{acct['db']}?mode=ro", uri=True)
            rows = [dict(zip([c[0] for c in cur.description], r, strict=True))
                    for cur in [con.execute(
                        # ⚠ leverage 필수 — 빠지면 trade_returns 가 1 로 폴백해 lev10 계좌 수익률이
                        #   10배가 된다(2026-08-06 대시보드에 +264% 로 표시된 사고).
                        "SELECT strategy_id,pair,exit_date,entry_date,pnl,stake_amount,"
                        "max_stake_amount,leverage,is_open FROM trades")]
                    for r in cur.fetchall()]
            con.close()
        except sqlite3.Error:
            continue
        for r in rows:
            key = group_key(r["strategy_id"] or "", r["pair"] or "", prefix)
            if not key:
                continue
            live.setdefault(key, {}).setdefault(acct["label"], []).append(r)
            fk = (*key, acct["label"])
            ed = str(r["entry_date"])[:10]
            if fk not in first or ed < first[fk]:
                first[fk] = ed

    base_date = mc.get("base_date")

    def _bt_curve(cyc: dict) -> list[tuple[str, float]]:
        """기준일 이후 백테스트 수익률 곡선 — 기준일 직전 지분을 0% 로 리베이스.

        마지막 점이 곧 `_bt_since` 값이라 차트 끝과 성적 열이 어긋날 수 없다.
        """
        anc = (cyc or {}).get("anchors") or {}
        if not anc or not base_date:
            return []
        keys = sorted(anc)
        prior = [k for k in keys if k < base_date]
        if not prior:
            return []
        b = anc[prior[-1]]["equity"]
        return [(prior[-1], 0.0)] + [(k, anc[k]["equity"] / b - 1)
                                     for k in keys if k >= base_date]

    def _bt_since(cyc: dict) -> float | None:
        """기준일 이후 백테스트 수익률 — 곡선의 마지막 점."""
        c = _bt_curve(cyc)
        return c[-1][1] if len(c) > 1 else None

    out_rows = []
    # ⚠ 양변기 SOXL/SOXS 를 하나로 합쳐 순차 복리하면 안 된다 — 두 레그는 자본을 **동시에**
    #   나눠 쓰므로, 같은 날 반대로 움직이면 계좌에서는 상쇄되는데 순차 복리는 손실로
    #   계산해 낙폭이 부풀려진다(합산 -70.7% vs 실제 계좌 -42.5%). 티커별로만 본다.
    for (strat, ticker), by_acct in sorted(live.items()):
        ref = legs_ref.get(f"{strat}|{ticker}", {})
        cyc = ref.get("cycle") or {}
        accounts = {}
        for a, rs in sorted(by_acct.items()):
            rets = trade_returns(rs)
            anc = _anchor_at(cyc.get("anchors", {}), first.get((strat, ticker, a), ""))
            st = leg_status(rets, anc, {**cyc, "worst_trade": ref.get("worst_trade")})
            # ⚠ 위 drawdown 은 **백테스트 낙폭을 이어받은** 값이라 "우리 계좌가 잃은 금액"이
            #   아니다. 계좌 체감 손익은 앵커 없이(1.0 시작) 따로 낸다 — 라벨을 섞으면
            #   "-49.3% 잃었다"로 오독한다(2026-08-06 지적).
            solo = live_curve(rets, None)
            accounts[a] = {**st, "level": status_level(st),
                           "start": first.get((strat, ticker, a)),
                           "anchor_dd": (anc or {}).get("drawdown"),
                           "live_pnl": solo["equity"] - 1,
                           "since": returns_since(rets, base_date)}
        out_rows.append({
            "strategy": strat, "ticker": ticker,
            "curve_valid": True,
            "account_mdd": ref.get("account_mdd"),   # YBG: 계좌 전체(상쇄 후) 참고값
            "ref_mdd": cyc.get("mdd"), "ref_mdd_date": cyc.get("mdd_date"),
            "ref_max_uw": cyc.get("max_uw_days"), "ref_worst": ref.get("worst_trade"),
            "bt_now": _bt_now(cyc), "bt_since": _bt_since(cyc),
            "curve": _bt_curve(cyc),
            "recent": ref.get("recent_trades") or [],
            "accounts": accounts,
        })

    # ── 계좌별 실제 손익 — 배팅 비중이 계좌마다 달라 레그 수익률로는 안 보인다 ──
    #    레그 수익률은 pnl/노셔널이라 비중이 약분된다(그게 조합 표가 계좌 무관한 이유).
    #    체감 손익은 계좌 자본 대비라, 그로스 50%인 계좌와 100%인 계좌가 크게 갈린다.
    acct_pnl: dict[str, float] = {}
    for acct in mc["accounts"]:
        tot = 0.0
        for rs in live.values():
            for r in rs.get(acct["label"], []):
                if str(r.get("exit_date") or "")[:10] >= (base_date or ""):
                    tot += float(r.get("pnl") or 0.0)
        eq = _fetch_equity(cfg.get("repo", ""), acct)
        acct_pnl[acct["label"]] = {"usdt": tot, "equity": eq,
                                   "pct": (tot / eq) if eq else None}

    # ── 조합 성적 — 전부 기준선의 **조합 곡선** 한 출처에서 온다 ──
    # 성적·낙폭·차트가 모두 같은 곡선의 파생이라 서로 어긋날 수 없다. (레그 수익률을
    # 가중합하는 옛 방식은 곡선 끝값과 0.5pp 어긋났다 — 차트를 붙이며 드러남.)
    combos = []
    for rc in ref_doc.get("combos") or []:
        cyc = rc.get("cycle") or {}
        combos.append({"name": rc["name"], "bt": _bt_since(cyc),
                       "ref_mdd": cyc.get("mdd"), "ref_mdd_date": cyc.get("mdd_date"),
                       "bt_now": _bt_now(cyc), "curve": _bt_curve(cyc),
                       "recent": rc.get("recent_trades") or []})

    return {"period": ref_doc.get("period"), "generated_at": ref_doc.get("generated_at"),
            "base_date": base_date, "recent_from": ref_doc.get("recent_from"),
            "combos": combos, "acct_pnl": acct_pnl,
            "rows": out_rows, "accounts": [a["label"] for a in mc["accounts"]]}


def _fetch_equity(repo: str, acct: dict) -> float | None:
    """봇 /api/v1/bots 의 account_balance.total_equity. 실패하면 None.

    fetch_bot_engines 는 bots 배열만 언랩하고 잔고를 버려서 여기서 따로 읽는다.
    """
    if not acct.get("port") or not acct.get("env"):
        return None
    try:
        env = parse_env(os.path.join(repo, acct["env"]))
        auth = base64.b64encode(
            f"{env.get('WEB_USERNAME', '')}:{env.get('WEB_PASSWORD', '')}".encode()).decode()
        req = urllib.request.Request(
            f"http://localhost:{acct['port']}/api/v1/bots",
            headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 — localhost 고정
            bal = (json.loads(r.read().decode()) or {}).get("account_balance") or {}
        eq = bal.get("total_equity")
        return float(eq) if eq else None
    except Exception:  # noqa: BLE001 — 잔고 없으면 USDT 금액만 표시
        return None


def _curve_svg(curve: list) -> str:
    """기간 수익률 곡선 — 인라인 SVG. 외부 라이브러리·JS 없이 그린다.

    곡선 데이터는 [(라벨, 수익률 fraction)]. 0% 기준선을 함께 그어 손익 전환
    시점이 보이게 한다. 낙폭 패널(_spark)과 계좌 수익률 추이가 공유한다.
    """
    if len(curve) < 2:
        return ""
    e = html.escape
    # ⚠ preserveAspectRatio 를 끄면(none) 글자가 가로로 늘어난다 — 눈금 %를 SVG 안에
    #   넣으려면 종횡비를 유지해야 한다. width:100%/height:auto 로 비례 축소한다.
    W, H = 560.0, 172.0
    L, R, T, B = 50.0, 12.0, 14.0, 26.0     # 좌(눈금 %)·우·상·하(날짜) 여백
    vals = [v for _, v in curve]
    lo, hi = min(vals + [0.0]), max(vals + [0.0])
    span = (hi - lo) or 1.0

    def _y(v):
        return T + (hi - v) / span * (H - T - B)

    def _x(i):
        return L + i / (len(curve) - 1) * (W - L - R)

    # 눈금: 0%·최고·최저. 서로 12 단위 이내면 겹치므로 버린다.
    # ⚠ 0% 를 맨 앞에 둔다 — 손익 전환선이라 최고/최저에 밀려 사라지면 안 된다
    #   (뒤에 두었더니 낙폭이 큰 레그에서 0% 선이 통째로 빠졌다).
    ticks, used = [], []
    for v in (0.0, hi, lo):
        y = _y(v)
        if any(abs(y - u) < 12 for u in used):
            continue
        used.append(y)
        ticks.append((v, y))
    grid = "".join(
        f"<line x1='{L}' y1='{y:.1f}' x2='{W - R}' y2='{y:.1f}' stroke='currentColor' "
        f"stroke-opacity='{0.35 if v == 0 else 0.13}'"
        + (" stroke-dasharray='3 3'" if v == 0 else "") + "/>"
        f"<text x='{L - 6}' y='{y + 3.5:.1f}' text-anchor=end class=gtick>{v:+.1%}</text>"
        for v, y in ticks)

    pts = " ".join(f"{_x(i):.1f},{_y(v):.1f}" for i, (_, v) in enumerate(curve))
    last = vals[-1]
    col = "var(--accent)" if last >= 0 else "var(--danger-text)"
    lx, ly = _x(len(curve) - 1), _y(last)
    # 끝값 말풍선 — 위쪽이 기본, 상단에 닿으면 아래로 뒤집는다
    ty = ly - 10 if ly - 10 > T + 10 else ly + 16

    # 가로축 일자 눈금 — 처음(연도 포함)·중간(MM-DD)·끝(MM-DD). 겹침은 x 간격으로 걸러낸다.
    n = len(curve)
    step = max(1, (n - 1) // 5)
    tick_is = list(range(0, n, step))
    if tick_is[-1] != n - 1:
        tick_is.append(n - 1)
    xticks = ""
    for i in tick_is:
        x = _x(i)
        if i not in (0, n - 1) and (x - L < 116 or _x(n - 1) - x < 56):
            continue  # 첫(긴)·끝 라벨과 겹치는 중간 눈금은 생략
        lbl = str(curve[i][0])
        if i != 0 and len(lbl) == 10:
            lbl = lbl[5:]  # 중간·끝은 MM-DD
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        if i not in (0, n - 1):  # 중간 눈금엔 흐린 세로 보조선
            xticks += (f"<line x1='{x:.1f}' y1='{T}' x2='{x:.1f}' y2='{H - B:.0f}' "
                       "stroke='currentColor' stroke-opacity='0.08'/>")
        xticks += (f"<text x='{x:.1f}' y='{H - 8:.0f}' text-anchor={anchor} "
                   f"class=gtick>{e(lbl)}</text>")

    # 마우스 오버용 일자별 데이터 — 좌표는 위 pts 그대로, 라벨·값은 미리 포맷해 JS는 계산 없음
    labels = "|".join(e(str(d)) for d, _ in curve)
    fvals = "|".join(f"{v:+.2%}" for _, v in curve)
    hover = (
        "<g class=hv style='display:none' pointer-events=none>"
        f"<line y1='{T}' y2='{H - B:.0f}' stroke='currentColor' "
        "stroke-opacity='0.45' stroke-dasharray='2 3'/>"
        "<circle r=3.6 fill='currentColor'/>"
        f"<text y='{T + 10:.0f}' class=hv-text></text></g>")
    return (
        f"<div class=spark><svg viewBox='0 0 {W:.0f} {H:.0f}' role=img "
        "aria-label='기간 수익률 곡선' "
        f"data-pts='{pts}' data-labels='{labels}' data-vals='{fvals}' "
        f"data-geom='{L},{R},{W:.0f}'>"
        # ⚠ 인라인 SVG의 빈(unpainted) 영역은 포인터 이벤트를 뒤로 통과시킨다 — 이 투명
        #   rect 가 전체 영역의 이벤트를 받아줘야 실제 마우스 호버가 선 위가 아니어도 발화한다
        f"<rect x='0' y='0' width='{W:.0f}' height='{H:.0f}' fill='none' "
        "pointer-events='all'/>"
        + grid + xticks
        + f"<polyline points='{pts}' fill=none stroke='{col}' stroke-width=2.2 "
        "stroke-linejoin=round stroke-linecap=round/>"
        f"<circle cx='{lx:.1f}' cy='{ly:.1f}' r=3.4 fill='{col}'/>"
        f"<text x='{min(lx, W - R):.1f}' y='{ty:.1f}' text-anchor=end class=glast "
        f"fill='{col}'>{last:+.2%}</text>"
        + hover
        + "</svg></div>")


# 곡선 마우스 오버 — 가장 가까운 일자에 세로선·점·"날짜 값" 라벨을 띄운다.
# 데이터(픽셀 좌표·라벨·포맷값)는 서버가 data-* 속성에 미리 넣어 JS는 조회만 한다.
_HOVER_JS = """<script>
document.querySelectorAll("svg[data-pts]").forEach(function (s) {
  var pts = s.dataset.pts.split(" ").map(function (p) { return p.split(",").map(Number); });
  var labels = s.dataset.labels.split("|"), vals = s.dataset.vals.split("|");
  var g = s.querySelector(".hv");
  if (!g || pts.length < 2) return;
  var line = g.querySelector("line"), dot = g.querySelector("circle"), txt = g.querySelector("text");
  var geom = s.dataset.geom.split(",").map(Number), L = geom[0], R = geom[1], W = geom[2];
  s.addEventListener("mousemove", function (ev) {
    var r = s.getBoundingClientRect();
    var xv = (ev.clientX - r.left) / r.width * W;
    var i = Math.round((xv - L) / (W - L - R) * (pts.length - 1));
    i = Math.max(0, Math.min(pts.length - 1, i));
    line.setAttribute("x1", pts[i][0]); line.setAttribute("x2", pts[i][0]);
    dot.setAttribute("cx", pts[i][0]); dot.setAttribute("cy", pts[i][1]);
    var left = pts[i][0] < W / 2;
    txt.setAttribute("x", left ? pts[i][0] + 7 : pts[i][0] - 7);
    txt.setAttribute("text-anchor", left ? "start" : "end");
    txt.textContent = labels[i] + "  " + vals[i];
    g.style.display = "";
  });
  s.addEventListener("mouseleave", function () { g.style.display = "none"; });
});
</script>"""


def _fetch_trades(repo: str, acct: dict) -> tuple[list | None, str | None]:
    """봇 /api/v1/trades — 청산 거래 목록. 실패 시 (None, 오류명)."""
    try:
        env = parse_env(os.path.join(repo, acct["env"]))
        auth = base64.b64encode(
            f"{env.get('WEB_USERNAME', '')}:{env.get('WEB_PASSWORD', '')}".encode()).decode()
        req = urllib.request.Request(
            f"http://localhost:{acct['port']}/api/v1/trades",
            headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 — localhost 고정
            doc = json.loads(r.read().decode())
        return (doc if isinstance(doc, list) else doc.get("trades", [])), None
    except Exception as e:  # noqa: BLE001 — 한 계좌 실패가 나머지를 막으면 안 됨
        return None, type(e).__name__


def collect_pnl_curve(cfg: dict) -> dict:
    """계좌별·합산 일별 실현 수익률 곡선 — config "pnl_curve" 지정 시에만.

    config: {"pnl_curve": {"accounts": [{"name","port","env","initial"}]}}
    시작일은 전 계좌 최초 청산일(= pcopy 첫 거래), 끝은 오늘(UTC).
    """
    from ohlryn_monitor.pnlcurve import combined_curve, daily_net_pnl, fill_curve

    pc = cfg.get("pnl_curve")
    if not pc:
        return {}
    end = datetime.now(timezone.utc).date().isoformat()
    accounts, maps, initials = [], [], []
    for acct in pc.get("accounts", []):
        trades, err = _fetch_trades(cfg.get("repo", ""), acct)
        if trades is None:
            accounts.append({"name": acct["name"], "error": err})
            continue
        m = daily_net_pnl(trades)
        maps.append(m)
        initials.append(float(acct["initial"]))
        accounts.append({"name": acct["name"], "initial": float(acct["initial"]), "_pnl": m})
    start = min((min(m) for m in maps if m), default=None)
    for a in accounts:
        if "_pnl" in a:
            a["curve"] = fill_curve(a.pop("_pnl"), a["initial"], start=start, end=end)
    return {"accounts": accounts, "start": start, "end": end,
            "combined": combined_curve(maps, initials, start=start, end=end)}


def collect_refresh_state(cfg: dict) -> dict:
    """기준선 갱신 버튼의 마지막 실행 상태."""
    mc = cfg.get("mdd") or {}
    path = mc.get("refresh_state")
    if not path:
        return {}
    try:
        return json.loads(open(path).read())
    except (OSError, ValueError):
        return {"status": "none", "at": None, "message": "아직 실행된 적 없음"}


def _git_version() -> str:
    """이 코드의 git short SHA — 화면에 박아 '지금 보는 페이지가 어느 배포인지'를
    한눈에 판별하게 한다 (터널/캐시로 옛 페이지를 보는 상황과 배포 문제를 즉시 구분).
    """
    try:
        out = subprocess.run(
            ["git", "-C", os.path.dirname(os.path.abspath(__file__)), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "?"
    except Exception:  # noqa: BLE001 — git 없는 배포 환경 허용
        return "?"


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

    try:
        mdd_data = collect_mdd(cfg)
    except Exception as exc:  # noqa: BLE001 — 대시보드 전체가 죽지 않게 격리
        mdd_data = {"error": f"{type(exc).__name__}: {exc}"}

    try:
        pnl_curve = collect_pnl_curve(cfg)
    except Exception as exc:  # noqa: BLE001 — 대시보드 전체가 죽지 않게 격리
        pnl_curve = {"error": f"{type(exc).__name__}: {exc}"}

    return {"now": now.isoformat(), "jobs": jobs, "bots": bots,
            "crashloop_flags": flags, "mdd": mdd_data, "pnl_curve": pnl_curve,
            "refresh": collect_refresh_state(cfg), "version": _git_version()}


def _favicon(has_problem: bool) -> str:
    """브라우저 탭 아이콘 — 인라인 SVG 데이터 URI.

    별도 엔드포인트나 바이너리 에셋 없이 HTML 한 줄로 끝낸다(stdlib·단일 파일 유지).
    글자(`<text>`)는 OS·브라우저마다 폰트가 달라 깨지므로 **링 도형**으로 그린다.

    문제가 있으면 빨강으로 바뀐다 — 탭만 보고도 알 수 있다.
    """
    color = "#b04343" if has_problem else "#0d9268"   # --danger-text / --accent
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
        f"<rect width='64' height='64' rx='16' fill='{color}'/>"
        "<circle cx='32' cy='32' r='14' fill='none' stroke='#fff' stroke-width='8'/>"
        "</svg>"
    )
    return "data:image/svg+xml," + urllib.parse.quote(svg, safe="")


def render_html(data: dict, title: str) -> str:
    e = html.escape
    badge = {
        "ok": ("badge-ok", "정상"),
        "error": ("badge-danger", "오류"),
        "stale": ("badge-danger", "미실행 의심"),
        "event": ("badge-info", "이벤트 대기"),
        "unknown": ("badge-warn", "첫 실행 전"),
    }
    def _details_card(head: str, n_items: int, n_bad: int, body: str,
                      bad_label: str = "문제", bad_cls: str = "badge-danger") -> str:
        """접히는 섹션 카드 — 주목할 게 있으면 펼쳐진 채 시작, 없으면 접힘.

        ⚠ 카드마다 n_bad 의 의미가 다르다. cron·봇 카드는 **고장**(정체·다운)이지만
          낙폭 카드는 "전략이 낙폭 국면"이라는 **정상 상태**다. 같은 '문제' 빨간 배지를
          쓰면 고장으로 오독되므로 bad_label/bad_cls 로 구분한다(2026-08-06 지적).
        """
        chip = (
            f"<span class='badge {bad_cls}'>{bad_label} {n_bad}</span>"
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

    # ── 계좌 수익률 추이 (config "pnl_curve" 지정 시) ─────────────────
    pc = data.get("pnl_curve") or {}
    if pc.get("accounts") or pc.get("error"):
        blocks = []
        if pc.get("error"):
            blocks.append(f"<div class=guide>수집 실패: {e(str(pc['error']))}</div>")
        else:
            blocks.append(
                "<div class=guide>"
                f"<b>{e(str(pc.get('start') or ''))} (pcopy 첫 거래) 이후 일별 누적 수익률</b> — "
                "봇이 <b>청산한 거래의 실현손익</b>(수수료·펀딩 차감) 기준입니다. "
                "미실현 손익은 포함되지 않아, 포지션이 열려 있으면 텔레그램의 equity 기준 "
                "수익률과 그만큼 다를 수 있습니다. 입출금 이체는 이 곡선에 영향을 주지 않습니다."
                "</div>")
            comb = pc.get("combined") or []
            if comb:
                blocks.append(
                    f"<div class=curvehead><b>전체 합산</b><span class=spacer></span>"
                    f"<span class='mono {'up' if comb[-1][1] >= 0 else 'down'}'>"
                    f"{comb[-1][1]:+.2%}</span></div>" + _curve_svg(comb))
            for a in pc["accounts"]:
                if a.get("error"):
                    blocks.append(
                        f"<div class=curvehead><b>{e(a['name'])}</b><span class=spacer></span>"
                        f"<span class=detail>조회실패({e(str(a['error']))})</span></div>")
                    continue
                cv = a.get("curve") or []
                last = cv[-1][1] if cv else 0.0
                blocks.append(
                    f"<div class=curvehead><b>{e(a['name'])}</b>"
                    f"<span class=detail>&nbsp;투입 {a['initial']:,.0f}</span>"
                    f"<span class=spacer></span>"
                    f"<span class='mono {'up' if last >= 0 else 'down'}'>{last:+.2%}</span></div>"
                    + _curve_svg(cv))
        n_err = sum(1 for a in pc.get("accounts", []) if a.get("error")) + (1 if pc.get("error") else 0)
        sections.append(_details_card(
            "계좌 수익률 추이 (일별·실현)", len(pc.get("accounts", [])), n_err,
            "".join(blocks), bad_label="조회실패"))

    m = data.get("mdd") or {}
    if m.get("rows"):
        def _state(row):
            """뱃지 + 한글 상태 — **백테스트 곡선 기준**.

            표에 보이는 숫자가 전부 백테스트라 배지도 같은 출처여야 한다. 라이브 낙폭으로
            판정하면 옆 칸 수치와 배지가 어긋나 읽는 사람이 원인을 찾을 수 없다
            (라이브 곡선은 앵커로 백테스트를 이어받아 값이 비슷하지만 같지는 않다).
            """
            src = row.get("bt_now")
            if not src:
                return "badge-info", "⚪ 미거래"
            r = src.get("ratio")
            dd = src.get("drawdown", 0)
            if r is None:
                return "badge-info", "— 기준없음"
            if r >= 1.0:
                return "badge-danger", "🔴 역대 갱신"
            if r >= 0.7:
                return "badge-warn", "🟠 역대 근접"
            if r >= 0.3:
                return "badge-warn", "🟡 낙폭 중"
            if dd >= -0.001:
                return "badge-ok", "🟢 신고점"
            return "badge-ok", "🟢 양호"

        dialogs = []
        _spark = _curve_svg

        def _trade_modal(key: str, title: str, trades: list, total: float | None = None,
                        curve: list | None = None) -> str:
            """'최근 거래' 버튼 + 모달. 백테스트 거래 목록이라 기준선에서 그대로 온다.

            보여주는 값은 **그 전략만 100% 굴리는 백테스트 계좌가 몇 % 움직였나** 하나뿐이다.
            조합이면 레그 비중을 이미 곱한 값(contrib)이라 계좌 수익 그 자체다.

            ⚠ 레그 자기 자본 기준 수익률을 조합 모달에 그대로 실으면 안 된다. 비중 없이
              늘어놓고 순차 복리까지 하면 계좌 수익이 2.5배로 부푼다(+18.16% 표시 vs
              실제 +7.14%, 2026-08-06 지적). 합계도 모달에서 재계산하지 않고 표의 성적
              열 값(total)을 그대로 쓴다 — 따로 계산하면 열과 어긋난다.
            """
            if not trades:
                return "<span class=detail>—</span>"
            # 진입일 최신순. ⚠ 목록에 담는 기준은 여전히 **청산일**이다(기준일 이후 청산된
            #   거래) — 곡선이 청산 시점 인덱스라 합계가 성적 열과 맞아야 하기 때문. 그래서
            #   기준일 이전에 진입한 거래가 맨 아래 남을 수 있다(예: 06-22 진입 → 07-07 청산).
            trades = sorted(trades, key=lambda t: (str(t.get("entry") or t["exit"]), t["exit"]),
                            reverse=True)
            weighted = any("w" in t for t in trades)
            val = (lambda t: t["contrib"]) if weighted else (lambda t: t["ret"])

            trs = "".join(
                f"<tr><td class=mono>{e(str(t.get('entry') or '-'))}</td>"
                f"<td class=mono>{e(str(t['exit']))}</td>"
                f"<td class=mono>{e(str(t['ticker']))}</td>"
                f"<td class='mono {'up' if val(t) >= 0 else 'down'}'>{val(t):+.2%}</td></tr>"
                for t in trades)
            wins = sum(1 for t in trades if t["ret"] > 0)
            if total is None and weighted:         # 성적 열이 비었을 때만 — 기여도 단순합
                total = sum(t["contrib"] for t in trades)
            elif total is None:                    # 레그 모달 — 자기 자본 복리
                total = 1.0
                for t in trades:
                    total *= 1 + t["ret"]
                total -= 1
            unit = ("각 거래가 <b>계좌</b>를 몇 % 움직였는지입니다(레그 비중 반영 완료). "
                    "합계는 표의 성적 열과 같은 값이며, 레그 안에서의 복리 때문에 "
                    "행 단순합과는 조금 다릅니다."
                    if weighted else
                    "각 거래가 <b>계좌</b>를 몇 % 움직였는지입니다.")
            dialogs.append(
                f"<dialog id=dlg-{key} class=modal><div class=modal-head>"
                f"<b>{title}</b> · 최근 거래 <span class=count>{len(trades)}</span>"
                f"<span class=spacer></span>"
                f"<form method=dialog><button class=modal-x>✕</button></form></div>"
                + _spark(curve or [])
                + f"<div class=guide>{e(str(m.get('recent_from') or ''))} 이후 "
                f"<b>백테스트</b>가 청산한 거래입니다(최신순). 승 {wins}/{len(trades)} · 합계 "
                f"<b>{total:+.2%}</b><br>{unit} 우리 실계좌가 아니라 "
                "<b>이 전략만 굴리는 백테스트 계좌</b> 기준입니다.</div>"
                "<div class=modal-body><table><tr><th>진입</th><th>청산</th><th>티커</th>"
                f"<th>계좌 수익</th></tr>{trs}</table></div></dialog>")
            return (f"<button class=btn-mini onclick=\"document.getElementById('dlg-{key}')"
                    f".showModal()\">🔍 {len(trades)}건</button>")

        head = ("<tr><th>전략</th><th>티커</th>"
                f"<th>{e(str(m.get('base_date') or ''))} 이후 성적"
                "<br><span class=detail>백테스트</span></th>"
                "<th>지금 낙폭<br><span class=detail>백테스트 현재</span></th>"
                "<th>역대 최악<br><span class=detail>이 레그 기준</span></th>"
                "<th>상태</th><th>최근 거래</th></tr>")
        rows, n_bad = [], 0
        for r in m["rows"]:
            cls, label = _state(r)
            if "🔴" in label or "🟠" in label:
                n_bad += 1

            b = r.get("bt_now") or {}
            bt_txt = f"{b['drawdown']:+.1%}" if b else "—"
            since = r.get("bt_since")
            since_txt = f"{since:+.2%}" if since is not None else "<span class=detail>—</span>"

            # 보조 설명 한 줄 — 역대 발생 시점 / 계좌 전체 낙폭(양변기)
            bits = []
            if r.get("ref_mdd_date"):
                bits.append(f"역대 최악 {e(str(r['ref_mdd_date']))}")
            if b.get("date"):
                bits.append(f"기준 {e(str(b['date']))}")
            if r.get("account_mdd") is not None:
                bits.append(f"계좌 전체(두 레그 상쇄 후) 역대 {r['account_mdd']:+.1%}")
            note = f"<br><span class=detail>{' · '.join(bits)}</span>" if bits else ""

            btn = _trade_modal(f"{r['strategy']}-{r['ticker']}",
                               f"{e(r['strategy'])} {e(r['ticker'])}", r.get("recent") or [],
                               curve=r.get("curve"))
            # 참조에 역대 MDD가 없는 레그(신규 추가 직후 등)는 "—" — 한 행 때문에 페이지가 죽으면 안 된다
            ref_mdd_txt = f"{r['ref_mdd']:+.1%}" if r.get("ref_mdd") is not None else "—"
            rows.append(
                f"<tr><td>{e(r['strategy'])}</td>"
                f"<td class=mono>{e(r['ticker'])}{note}</td>"
                f"<td class=mono>{since_txt}</td>"
                f"<td class=mono>{bt_txt}</td>"
                f"<td class=mono>{ref_mdd_txt}</td>"
                f"<td><span class='badge {cls}'>{label}</span></td>"
                f"<td>{btn}</td></tr>")

        guide = (
            "<div class=guide>"
            "<b>이 표는 전부 백테스트 기준입니다</b> — 전략 자체가 지금 어떤 상태인지 봅니다. "
            "우리 계좌가 실제로 번 돈은 위의 <b>계좌별 실제 손익</b> 표를 보세요.<br>"
            "· <b>지금 낙폭</b> ↔ <b>역대 최악</b> — 나란히 붙여 뒀습니다. "
            "지금이 역대 최악에 얼마나 가까운지가 상태 배지입니다.<br>"
            f"· <b>{e(str(m.get('base_date') or ''))} 이후 성적</b> — 기준일부터 지금까지 "
            "백테스트가 낸 수익률. 같은 기간 우리 실적과 대조하면 추종 여부가 보입니다.<br>"
            "· <b>역대 최악</b>은 이 레그 자본만 떼어 본 16년치 최대 낙폭입니다. "
            "배지 = 지금 낙폭 ÷ 역대 최악 (30%↑ 🟡 · 70%↑ 🟠 · 100%↑ 🔴)<br>"
            "· <b>🔍 최근 거래</b> — 그 성적을 만든 개별 거래를 펼쳐 봅니다. "
            "성적이 0.00%면 대개 손실이 아니라 <u>그 기간에 청산된 거래가 없다</u>는 뜻입니다."
            "</div>")
        cb = m.get("combos") or []
        combo_html = ""
        if cb:
            crows = []
            for i, c in enumerate(cb):
                def _f(v):
                    return f"{v:+.2%}" if v is not None else "<span class=detail>—</span>"
                b = c.get("bt_now") or {}
                cls, label = _state(c)
                mdd_txt = f"{c['ref_mdd']:+.1%}" if c.get("ref_mdd") is not None else "—"
                now_txt = f"{b['drawdown']:+.1%}" if b else "—"
                btn = _trade_modal(f"combo{i}", e(c["name"]), c.get("recent") or [], c["bt"],
                                   curve=c.get("curve"))
                crows.append(f"<tr><td>{e(c['name'])}</td>"
                             f"<td class=mono><b>{_f(c['bt'])}</b></td>"
                             f"<td class=mono>{now_txt}</td>"
                             f"<td class=mono>{mdd_txt}</td>"
                             f"<td><span class='badge {cls}'>{label}</span></td>"
                             f"<td>{btn}</td></tr>")
            combo_html = (
                f"<div class=guide><b>조합 성적</b> — {e(str(m.get('base_date')))} 이후. "
                "이 표도 전부 <b>백테스트</b>입니다. 낙폭·역대 최악은 레그를 <u>같은 날 "
                "합산해</u> 만든 조합 곡선 기준입니다(순차 복리하면 상쇄가 사라져 부풀려짐).</div>"
                f"<table class=t-mdd><tr><th>구성</th>"
                f"<th>{e(str(m.get('base_date') or ''))} 이후 성적"
                "<br><span class=detail>백테스트</span></th>"
                "<th>지금 낙폭<br><span class=detail>백테스트 현재</span></th>"
                "<th>역대 최악<br><span class=detail>이 조합 기준</span></th>"
                "<th>상태</th><th>최근 거래</th></tr>" + "".join(crows) + "</table>")
        ap = m.get("acct_pnl") or {}
        if any(v["usdt"] for v in ap.values()):
            arows = []
            for a, v in sorted(ap.items()):
                pct = f"{v['pct']:+.2%}" if v.get("pct") is not None else "—"
                eqt = f"{v['equity']:,.0f}" if v.get("equity") else "<span class=detail>조회 실패</span>"
                arows.append(f"<tr><td>{e(a)}</td><td class=mono>{v['usdt']:+,.2f}</td>"
                             f"<td class=mono><b>{pct}</b></td><td class=mono>{eqt}</td></tr>")
            combo_html += (
                "<div class=guide><b>계좌별 실제 손익</b> — 위 조합 성적은 "
                "<u>배팅 비중이 약분된 100% 환산값</u>입니다. 계좌마다 그로스 노출과 시작일이 "
                "달라 체감 손익은 아래가 실제입니다.</div>"
                "<table class=t-mdd><tr><th>계좌</th><th>실현손익(USDT)</th><th>계좌 대비</th>"
                "<th>계좌 자본</th></tr>" + "".join(arows) + "</table>")

        # ── 기준선 갱신 바 ───────────────────────────────────────────
        # ⚠ 이 카드를 재작성하면서 버튼 렌더링이 누락됐던 적이 있다(백엔드 `/refresh`·
        #   상태 수집은 살아 있는데 화면에만 없어 발견이 늦었다). 데이터를 수집하면
        #   반드시 표시까지 확인한다.
        rf = data.get("refresh") or {}
        rf_badge = {
            "ok": ("badge-ok", "정상"),
            "running": ("badge-info", "실행 중"),
            "error": ("badge-danger", "실패"),
        }.get(rf.get("status"), ("badge-warn", "실행 전"))
        per = m.get("period") or {}
        refresh_html = (
            "<div class=refresh-bar>"
            "<form method=post action='/refresh' style='display:inline'>"
            "<button class=btn-run type=submit>↻ 기준선 갱신</button></form>"
            f"<span class=rf-meta>기준 데이터 <b>{e(str(per.get('start') or '?'))} ~ "
            f"{e(str(per.get('end') or '?'))}</b></span>"
            f"<span class=spacer></span>"
            f"<span class='badge {rf_badge[0]}'>{rf_badge[1]}</span>"
            f"<span class=rf-meta>{e(str(rf.get('at') or '')[:19])} "
            f"{e(str(rf.get('message') or ''))[:80]}</span>"
            "</div>"
            "<div class=guide>최신 주가를 받아 16년 백테스트를 다시 돌려 위 기준선을 "
            "갱신합니다(수 분 소요). 실행 중에도 화면은 기존 값으로 계속 동작합니다.</div>"
        )

        sections.append(_details_card(
            "전략·티커별 낙폭", len(m["rows"]), n_bad,
            refresh_html + combo_html + guide
            + "<table class=t-mdd>" + head + "".join(rows) + "</table>"
            + "".join(dialogs),
            bad_label="역대 근접", bad_cls="badge-warn"))
    elif m.get("error"):
        sections.append(_details_card("전략·티커별 낙폭", 0, 1,
                                      f"<div class=detail style='padding:8px 12px'>{e(m['error'])}</div>"))

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
<meta name=viewport content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<link rel=icon href="{_favicon(n_bad > 0)}"><style>
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
.guide{{padding:12px 20px;line-height:1.75;font-size:13px;color:var(--text-faint);border-bottom:1px solid var(--line)}}
.guide b{{color:var(--text)}}
.badge{{display:inline-flex;align-items:center;font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px;white-space:nowrap}}
.badge-ok{{background:var(--accent-soft);color:var(--accent-deep)}}
.badge-danger{{background:var(--danger-soft);color:var(--danger-text)}}
.badge-info{{background:var(--info-soft);color:var(--info-text)}}
.badge-warn{{background:var(--warn-soft);color:var(--warn-text)}}
.ok-card{{padding:14px 20px;color:var(--accent-deep);font-weight:600}}
.flag-card{{padding:14px 20px;background:var(--danger-soft);border-color:#f5cccc;color:var(--danger-text)}}
.btn-mini{{background:var(--bg);border:1px solid var(--line);color:var(--text-sub);font-size:12px;font-weight:700;padding:4px 10px;border-radius:8px;cursor:pointer;white-space:nowrap;font-family:inherit}}
.btn-mini:hover{{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-deep)}}
.modal{{border:none;border-radius:var(--r);padding:0;max-width:min(560px,94vw);width:100%;box-shadow:0 24px 64px rgba(0,0,0,.22);color:var(--text)}}
.modal::backdrop{{background:rgba(18,22,30,.42);backdrop-filter:blur(2px)}}
.modal-head{{padding:14px 20px;font-size:15px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--line)}}
.modal-x{{background:none;border:none;font-size:16px;color:var(--text-faint);cursor:pointer;padding:2px 6px}}
.modal-body{{max-height:60vh;overflow:auto}}
.spark{{padding:12px 18px 4px;color:var(--text-faint)}}
.curvehead{{display:flex;align-items:center;gap:6px;padding:14px 20px 0;font-size:14px}}
.curvehead+.curvehead{{border-top:1px solid var(--line)}}
.hv-text{{font-size:12.5px;font-weight:700;fill:var(--text)}}
.spark svg{{width:100%;height:auto;display:block;overflow:visible}}
.gtick{{font-family:'SF Mono','Menlo',monospace;font-size:11px;fill:currentColor}}
.glast{{font-family:'SF Mono','Menlo',monospace;font-size:13px;font-weight:700}}
.refresh-bar{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 20px;border-bottom:1px solid var(--line)}}
.btn-run{{background:var(--accent);color:#fff;border:none;font-weight:700;font-size:13px;padding:8px 16px;border-radius:9px;cursor:pointer;font-family:inherit}}
.btn-run:hover{{background:var(--accent-deep)}}
.rf-meta{{font-size:12.5px;color:var(--text-faint);font-family:'SF Mono','Menlo',monospace}}
.up{{color:var(--accent-deep);font-weight:700}}.down{{color:var(--danger-text);font-weight:700}}
@media (max-width:720px){{.detail{{display:none}}table:not(.t-mdd) th:nth-child(5){{display:none}}
.modal{{max-width:100%;width:100%}}}}
</style></head><body>
<div class=nav><div class=nav-inner>
<span class=logo-mark>O</span><span class=logo>{e(title)}</span>
<span class=stamp>{e(data['now'][:19])}Z · {summary} · v{e(str(data.get('version') or '?'))}</span>
<a class=btn-refresh href="/">↻ 새로고침</a>
</div></div>
<div class=page>
{''.join(sections)}
{flags_html}
</div>{_HOVER_JS}</body></html>"""


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
        # 상태 대시보드는 항상 실시간이어야 한다 — 브라우저/중간 캐시가 옛 페이지를
        # 보여주면 "배포했는데 안 바뀐다"류의 혼란이 생긴다(2026-08-14).
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        """기준선 갱신 트리거 — 유일한 조작 엔드포인트.

        ⚠ 이 대시보드는 원래 읽기 전용이었다. 이 엔드포인트만 예외이며 표면을 최소로 둔다:
          - 사용자 입력 인자 없음(고정 스크립트 경로) → 주입 표면 없음
          - POST 전용(GET 프리페치로 실수 발동 방지) + Basic Auth
          - 스크립트 자체가 flock 으로 중복 실행 차단
        """
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="ohlryn-monitor"')
            self.end_headers()
            return
        script = (self.cfg.get("mdd") or {}).get("refresh_script")
        if self.path != "/refresh" or not script:
            self.send_response(404)
            self.end_headers()
            return
        subprocess.Popen(["/bin/bash", script], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        self.send_response(303)          # PRG — 새로고침으로 재실행되지 않게
        self.send_header("Location", "/")
        self.end_headers()

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
