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
    }
    data = collect_status(cfg, crontab_text=CRON.format(log=log, log2=log2))
    by = {j["name"]: j for j in data["jobs"]}
    assert by["my-watchdog"]["status"] == "event"
    assert by["my-alerter"]["status"] == "ok"
    assert by["my-alerter"]["description"] == "테스트 알리미 설명"

    html_out = render_html(data, "t")
    assert "my-alerter" in html_out and "테스트 알리미 설명" in html_out
    assert "crash-loop 차단 플래그: 없음" in html_out


def test_crashloop_flag_shown(tmp_path):
    (tmp_path / "bot1.crashloop").write_text("부팅실패 연속 2회")
    cfg = {"wd_state_dir": str(tmp_path)}
    data = collect_status(cfg, crontab_text="")
    assert len(data["crashloop_flags"]) == 1
    assert "부팅실패" in render_html(data, "t")
