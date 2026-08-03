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
    base = {"now": "2026-08-03T00:00:00+00:00", "jobs": [_JOB_OK], "bots": [], "crashloop_flags": []}
    html_ok = render_html(base, "t")
    assert "<details class=card>" in html_ok
    assert "<details class=card open>" not in html_ok

    bad = {**base, "jobs": [{**_JOB_OK, "status": "stale"}]}
    assert "<details class=card open>" in render_html(bad, "t")


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
