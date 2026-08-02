"""cronstatus 순수 로직 테스트 — crontab 파싱·기대주기 계산·상태 판정.

설계: crontab 한 줄에 이미 이름(# 태그)·주기·로그 경로(>>)가 있다.
이를 파싱해 "각 작업이 잘 돌고 있는가"를 로그 신선도로 판정한다 (알리미 무수정).
"""

from datetime import datetime, timezone

from ohlryn_monitor.cronstatus import (
    parse_crontab,
    max_gap_minutes,
    judge,
)

CRONTAB = """\
*/5 * * * * /home/u/vb/scripts/sm_binance_watchdog.sh >> /home/u/watchdog_sm.log 2>&1 # vb-sm-watchdog
30 0 * * * cd /home/u/om && /usr/bin/python3 -m ohlryn_monitor.alerters.schedule_watch --config c.json >> /home/u/schedule_watch.log 2>&1 # vb-schedule-watch
59 20 * * 1-5 cd /home/u/vb && .venv/bin/python scripts/ibs_fill_probe.py >> /home/u/ibs_probe.log 2>&1 # vb-ibs-probe-est
# 주석 줄은 무시
MAILTO=""
12 * * * * cd /home/u/om && python3 -m x >> /home/u/pnl_watch.log 2>&1 # vb-pnl-watch
"""


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# ── parse_crontab ────────────────────────────────────────────────────


def test_parse_extracts_name_schedule_log():
    jobs = parse_crontab(CRONTAB)
    assert len(jobs) == 4  # 주석·환경변수 줄 제외
    j = jobs[0]
    assert j["name"] == "vb-sm-watchdog"
    assert j["schedule"] == "*/5 * * * *"
    assert j["log"] == "/home/u/watchdog_sm.log"


def test_parse_line_without_tag_gets_command_name():
    jobs = parse_crontab("5 0 * * * /some/cmd --x >> /tmp/a.log 2>&1\n")
    assert len(jobs) == 1
    assert jobs[0]["name"]  # 태그가 없어도 뭔가 식별자 존재
    assert jobs[0]["log"] == "/tmp/a.log"


def test_parse_line_without_log_redirect():
    jobs = parse_crontab("5 0 * * * /some/cmd # tagged\n")
    assert jobs[0]["log"] is None


# ── max_gap_minutes: 스케줄에서 실행 간 최대 간격 (분) ────────────────


def test_gap_every_5min():
    assert max_gap_minutes("*/5 * * * *") == 5


def test_gap_offset_5min():
    assert max_gap_minutes("1-59/5 * * * *") == 5  # 1,6,...,56 → 다음 시 :01도 정확히 5분


def test_gap_hourly():
    assert max_gap_minutes("12 * * * *") == 60


def test_gap_daily():
    assert max_gap_minutes("30 0 * * *") == 1440


def test_gap_weekdays_only():
    # 평일 20:59 — 금요일 → 월요일 간격이 최대 (3일 = 4320분)
    assert max_gap_minutes("59 20 * * 1-5") == 4320


# ── judge: 로그 신선도/에러로 상태 판정 ──────────────────────────────

NOW = dt("2026-08-02T12:00:00")


def test_judge_fresh_ok():
    st = judge(now=NOW, log_mtime=dt("2026-08-02T11:58:00"), gap_min=5, log_tail="[11:55Z] issues=0 (all OK)")
    assert st["status"] == "ok"


def test_judge_stale_when_log_silent_too_long():
    # */5분 작업의 로그가 20분 조용 → cron 미실행 의심
    st = judge(now=NOW, log_mtime=dt("2026-08-02T11:40:00"), gap_min=5, log_tail="ok")
    assert st["status"] == "stale"


def test_judge_daily_job_not_stale_within_day():
    st = judge(now=NOW, log_mtime=dt("2026-08-02T00:30:10"), gap_min=1440, log_tail="alerts=0")
    assert st["status"] == "ok"


def test_judge_error_in_recent_log():
    tail = "x\nTraceback (most recent call last):\nValueError: boom\n"
    st = judge(now=NOW, log_mtime=dt("2026-08-02T11:59:00"), gap_min=5, log_tail=tail)
    assert st["status"] == "error"
    assert "ValueError" in st["detail"]


def test_judge_event_driven_never_stale():
    # watchdog류: 이벤트 있을 때만 로그 — 조용=정상
    st = judge(now=NOW, log_mtime=dt("2026-07-01T00:00:00"), gap_min=5, log_tail="", event_driven=True)
    assert st["status"] == "event"


def test_judge_no_log_yet():
    st = judge(now=NOW, log_mtime=None, gap_min=5, log_tail="")
    assert st["status"] == "unknown"


# ── humanize_schedule: 사람이 읽는 주기 ──────────────────────────────

from ohlryn_monitor.cronstatus import humanize_schedule


def test_humanize_every_n_min():
    assert humanize_schedule("*/5 * * * *") == "5분마다"
    assert humanize_schedule("1-59/5 * * * *") == "5분마다"


def test_humanize_hourly():
    assert humanize_schedule("12 * * * *") == "매시 :12"


def test_humanize_daily_with_kst():
    assert humanize_schedule("30 0 * * *") == "매일 00:30 UTC (09:30 KST)"
    assert humanize_schedule("5 0 * * *") == "매일 00:05 UTC (09:05 KST)"


def test_humanize_weekdays_kst_next_day():
    # UTC 20:59 + 9h = 익일 05:59 KST
    assert humanize_schedule("59 20 * * 1-5") == "평일 20:59 UTC (익일 05:59 KST)"


def test_humanize_fallback_raw():
    assert humanize_schedule("0 3 1 * *") == "0 3 1 * *"  # 월간 등 미지원 패턴은 원문


# ── cd 접두 상대경로 로그 해석 ───────────────────────────────────────


def test_parse_resolves_relative_log_with_cd_prefix():
    line = "0 21 * * 1-5 cd /home/u/vb && .venv/bin/python x.py >> logs/fill.log 2>&1 # fill-job\n"
    jobs = parse_crontab(line)
    assert jobs[0]["log"] == "/home/u/vb/logs/fill.log"
