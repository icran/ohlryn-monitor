"""status_ui 조립 테스트 — 수집·렌더 (서버 기동 없이)."""

from ohlryn_monitor.status_ui import collect_status, render_html

CRON = "*/5 * * * * /x/wd.sh >> {log} 2>&1 # my-watchdog\n12 * * * * python3 -m a.b >> {log2} 2>&1 # my-alerter\n"


def test_collect_and_render(tmp_path):
    # 로그가 방금 갱신된 알리미는 ok, watchdog은 event_driven으로 ⚪
    log = tmp_path / "wd.log"; log.write_text("")
    log2 = tmp_path / "al.log"; log2.write_text("[11:55Z] issues=0 (all OK)\n")
    cfg = {
        "wd_state_dir": str(tmp_path),
        "event_driven": ["my-watchdog"],
        "descriptions": {"my-alerter": "테스트 알리미 설명"},
        "categories": {"my-watchdog": "봇 감시", "my-alerter": "알림"},
    }
    data = collect_status(cfg, crontab_text=CRON.format(log=log, log2=log2))
    by = {j["name"]: j for j in data["jobs"]}
    assert by["my-watchdog"]["status"] == "event"
    assert by["my-alerter"]["status"] == "ok"
    assert by["my-alerter"]["description"] == "테스트 알리미 설명"

    assert by["my-alerter"]["schedule_human"] == "매시 :12"  # 사람이 읽는 주기

    html_out = render_html(data, "t")
    assert "my-alerter" in html_out and "테스트 알리미 설명" in html_out
    assert "봇 감시" in html_out and "알림" in html_out  # 카테고리 헤더
    assert "매시 :12" in html_out
    assert "crash-loop 차단 플래그: 없음" in html_out


def test_crashloop_flag_shown(tmp_path):
    (tmp_path / "bot1.crashloop").write_text("부팅실패 연속 2회")
    cfg = {"wd_state_dir": str(tmp_path)}
    data = collect_status(cfg, crontab_text="")
    assert len(data["crashloop_flags"]) == 1
    assert "부팅실패" in render_html(data, "t")


_JOB_OK = {
    "name": "a", "description": "", "category": "알림", "schedule": "0 * * * *",
    "schedule_human": "매시 :00", "gap_min": 60, "log": "", "last_run": None,
    "status": "ok", "detail": "",
}


def test_sections_collapsed_when_ok_open_on_problem():
    # 정상 섹션은 접힘(details, open 없음) / 문제(error·stale) 섹션은 펼쳐짐(open)
    base = {"now": "2026-08-03T00:00:00+00:00", "jobs": [_JOB_OK], "bots": [],
            "crashloop_flags": [], "pnl_curve": {}}
    html_ok = render_html(base, "t")
    assert "<details class=card>" in html_ok
    assert "<details class=card open>" not in html_ok

    bad = {**base, "jobs": [{**_JOB_OK, "status": "stale"}]}
    assert "<details class=card open>" in render_html(bad, "t")


def test_mdd_row_without_ref_mdd_renders_dash():
    # 참조에 역대 MDD가 없는 레그(신규 추가 등)가 있어도 페이지가 죽지 않고 "—"로 표기
    # (회귀: ref_mdd None → TypeError로 do_GET 전체가 500, 2026-08-10 접속 불가 사고)
    data = {
        "now": "2026-08-10T00:00:00+00:00", "jobs": [], "bots": [], "crashloop_flags": [],
        "mdd": {
            "base_date": "2026-07-17", "combos": [], "acct_pnl": {}, "accounts": [],
            "rows": [{
                "strategy": "ibs", "ticker": "AVGO", "ref_mdd": None, "ref_mdd_date": None,
                "bt_now": None, "bt_since": None, "recent": [], "accounts": {},
            }],
        },
    }
    out = render_html(data, "t")
    assert "AVGO" in out


def test_pnl_curve_section_rendered():
    # 계좌 수익률 추이: 합산·계좌별 곡선 SVG + 마지막 수익률, 조회실패 계좌는 라벨로 표기
    curve = [("2026-07-16", 0.0), ("2026-07-17", 0.05), ("2026-07-18", -0.02)]
    data = {
        "now": "2026-08-14T00:00:00+00:00", "jobs": [], "bots": [], "crashloop_flags": [],
        "pnl_curve": {
            "start": "2026-07-17", "end": "2026-08-14", "combined": curve,
            "accounts": [
                {"name": "hs-binance", "initial": 200000.0, "curve": curve},
                {"name": "hs-bybit", "error": "URLError"},
            ],
        },
    }
    out = render_html(data, "t")
    assert "계좌 수익률 추이" in out
    assert "전체 합산" in out and "hs-binance" in out
    assert "-2.00%" in out  # 곡선 마지막 값이 헤더에 표기
    assert "조회실패(URLError)" in out
    assert out.count("<svg") >= 2  # 합산 + 계좌 곡선


def test_curve_hover_tooltip_wired():
    # 마우스 오버 시 일자별 값 확인: 곡선 svg에 데이터 속성(data-pts/labels/vals)과
    # 호버 가이드 그룹(.hv)이 있고, 페이지에 호버 스크립트가 1회 포함된다
    curve = [("2026-07-16", 0.0), ("2026-07-17", 0.05), ("2026-07-18", -0.02)]
    data = {
        "now": "2026-08-14T00:00:00+00:00", "jobs": [], "bots": [], "crashloop_flags": [],
        "pnl_curve": {
            "start": "2026-07-17", "end": "2026-07-18", "combined": curve,
            "accounts": [{"name": "hs-binance", "initial": 200000.0, "curve": curve}],
        },
    }
    out = render_html(data, "t")
    assert "data-pts=" in out and "data-labels=" in out and "data-vals=" in out
    assert "2026-07-17|" in out  # 날짜 라벨 데이터
    assert "+5.00%|" in out      # 일자별 값 포맷 데이터
    assert "class=hv" in out     # 호버 가이드(세로선·점·라벨) 그룹
    assert out.count("svg[data-pts]") == 1  # 호버 스크립트는 페이지에 1회
    # ⚠ 인라인 SVG 빈 영역은 포인터 이벤트를 통과시킨다 — 투명 캡처 rect가 없으면
    #   실제 마우스로는 2px 선 위에서만 호버가 발화한다(합성 이벤트 테스트론 안 잡힘)
    assert "pointer-events='all'" in out


def test_curve_x_axis_date_ticks():
    # 가로축에 중간 일자 눈금이 표기된다 (처음은 연도 포함, 중간·끝은 MM-DD)
    from datetime import date, timedelta
    curve = [((date(2026, 7, 16) + timedelta(days=i)).isoformat(), i / 1000) for i in range(30)]
    data = {
        "now": "2026-08-14T00:00:00+00:00", "jobs": [], "bots": [], "crashloop_flags": [],
        "pnl_curve": {"start": "2026-07-17", "end": "2026-08-14", "combined": curve,
                      "accounts": [{"name": "hs-binance", "initial": 1000.0, "curve": curve}]},
    }
    out = render_html(data, "t")
    assert ">2026-07-16<" in out           # 첫 눈금은 연도 포함
    assert ">07-26<" in out or ">07-25<" in out  # 중간 눈금 MM-DD
    assert ">08-14<" in out                # 끝 눈금 MM-DD


def test_bots_section_rendered():
    # 봇 현황: 봇 이름·전략(config)·중지 페어가 표기되고, 문제 봇이 있으면 섹션이 펼쳐짐
    data = {
        "now": "2026-08-03T00:00:00+00:00", "jobs": [], "crashloop_flags": [],
        "bots": [{
            "name": "sub 계좌 (8010)", "status": "warn", "detail": "문제 전략 1개",
            "strategies": [{
                "config": "alpha", "pair_names": ["BTC", "ETH"],
                "stopped": ["BTC"], "stale": [], "status": "warn",
            }],
        }],
    }
    out = render_html(data, "t")
    assert "봇 현황" in out
    assert "sub 계좌 (8010)" in out and "alpha" in out
    assert "중지" in out and "BTC" in out
    assert "<details class=card open>" in out
