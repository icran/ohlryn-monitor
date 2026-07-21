"""ohlryn_monitor.health 순수 판정 로직 테스트."""

from datetime import datetime, timedelta, timezone

from ohlryn_monitor.health import (
    bot_issues,
    count_log_errors,
    has_critical,
    log_issue,
    next_log_offset,
    plan_alerts,
    system_issues,
)

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


def _engine(pair="BTC/USDT:USDT", running=True, minutes_ago=10):
    return {
        "pair": pair,
        "is_running": running,
        "last_updated": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
    }


class TestBotIssues:
    def test_healthy_bot_no_issues(self):
        # 정상 봇(가동중 + 신선한 last_updated)은 이슈 없음
        assert bot_issues("b", [_engine()], now=NOW, stale_minutes=40) == []

    def test_api_unreachable_is_critical(self):
        # API 응답 없음(engines=None) → CRITICAL 1건
        issues = bot_issues("b", None, now=NOW, stale_minutes=40, error="URLError")
        assert len(issues) == 1 and "CRITICAL" in issues[0][1] and issues[0][0] == "b:api"

    def test_empty_engine_list_is_critical(self):
        # 엔진 목록이 비면 CRITICAL (API는 살았지만 봇 구성이 사라진 상태)
        issues = bot_issues("b", [], now=NOW, stale_minutes=40)
        assert issues[0][0] == "b:empty"

    def test_stopped_engine_detected(self):
        # is_running=False 엔진 → 중지 CRITICAL
        issues = bot_issues("b", [_engine(running=False)], now=NOW, stale_minutes=40)
        assert issues[0][0] == "b:stopped"

    def test_stale_engine_detected_as_zombie(self):
        # last_updated가 임계(40분)를 넘으면 좀비 의심 CRITICAL
        issues = bot_issues("b", [_engine(minutes_ago=41)], now=NOW, stale_minutes=40)
        assert issues[0][0] == "b:stale"

    def test_candle_open_label_lag_is_not_stale(self):
        # 15m봉 오픈시각 라벨의 자연 지연(~30분)은 좀비가 아님 (임계 40분 설계 근거)
        assert bot_issues("b", [_engine(minutes_ago=29)], now=NOW, stale_minutes=40) == []

    def test_naive_datetime_treated_as_utc(self):
        # tz 없는 last_updated(실제 API가 naive 반환)는 UTC로 간주
        e = {"pair": "X", "is_running": True, "last_updated": "2026-07-21 11:50:00"}
        assert bot_issues("b", [e], now=NOW, stale_minutes=40) == []


class TestSystemIssues:
    LIMITS = {"disk_pct_max": 85, "mem_avail_mb_min": 400, "swap_used_mb_max": 2048, "load1_max": 3.0}

    def test_all_within_limits(self):
        # 전부 한도 내면 이슈 없음
        m = {"disk_pct": 50, "mem_avail_mb": 4000, "swap_used_mb": 100, "load1": 0.5}
        assert system_issues(m, self.LIMITS) == []

    def test_low_memory_is_critical_others_warning(self):
        # 메모리 부족만 CRITICAL, 나머지는 WARNING 등급
        m = {"disk_pct": 90, "mem_avail_mb": 100, "swap_used_mb": 3000, "load1": 5.0}
        issues = dict(system_issues(m, self.LIMITS))
        assert "CRITICAL" in issues["sys:mem"]
        assert all("WARNING" in issues[k] for k in ("sys:disk", "sys:swap", "sys:load"))


class TestLogScan:
    def test_count_errors_and_threshold(self):
        # ERROR/Traceback 라인 집계 + 임계 미만이면 이슈 없음
        chunk = "INFO ok\nERROR boom\nTraceback (most recent call last):\nINFO fine\n"
        assert count_log_errors(chunk) == 2
        assert log_issue("b", 2, threshold=5) == []
        assert log_issue("b", 5, threshold=5)[0][0] == "b:logerr"

    def test_offset_first_run_skips_history(self):
        # 최초 실행은 과거 로그 미소급 (현재 크기부터)
        start, new = next_log_offset(None, 1000)
        assert (start, new) == (1000, 1000)

    def test_offset_rotation_resets(self):
        # 파일이 줄었으면(로테이션) 처음부터 다시
        start, new = next_log_offset(500, 200)
        assert (start, new) == (0, 200)


class TestPlanAlerts:
    def test_new_issue_sends_and_registers(self):
        # 신규 문제는 즉시 발송 + active 등록
        msgs, active = plan_alerts({}, [("k1", "CRITICAL x")], now_ts=1000, cooldown_sec=3600)
        assert msgs == ["🚨 CRITICAL x"] and "k1" in active

    def test_cooldown_suppresses_repeat(self):
        # 쿨다운 내 같은 문제는 재발송 억제
        active = {"k1": {"first": 1000, "last_sent": 1000}}
        msgs, _ = plan_alerts(active, [("k1", "CRITICAL x")], now_ts=1500, cooldown_sec=3600)
        assert msgs == []

    def test_cooldown_expiry_resends(self):
        # 쿨다운 경과 후엔 재발송 (first는 최초 발생 시각 유지)
        active = {"k1": {"first": 1000, "last_sent": 1000}}
        msgs, new_active = plan_alerts(active, [("k1", "CRITICAL x")], now_ts=5000, cooldown_sec=3600)
        assert msgs == ["🚨 CRITICAL x"] and new_active["k1"]["first"] == 1000

    def test_recovery_notice_once(self):
        # 문제 소멸 시 회복 알림 1회 + active에서 제거
        active = {"k1": {"first": 1000, "last_sent": 1000}}
        msgs, new_active = plan_alerts(active, [], now_ts=1600, cooldown_sec=3600)
        assert msgs == ["✅ 해소: k1 (지속 10분)"] and new_active == {}


def test_has_critical():
    # CRITICAL 포함 여부 판정 (healthchecks /fail ping 분기용)
    assert has_critical([("a", "WARNING x"), ("b", "CRITICAL y")])
    assert not has_critical([("a", "WARNING x")])
