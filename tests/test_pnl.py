"""ohlryn_monitor.pnl 순수 로직 테스트."""

from ohlryn_monitor.pnl import build_summary_message, profit_rate, should_send, update_record


class TestProfitRate:
    def test_basic_and_rounding(self):
        # 수익률 계산 + 소수점 2자리 반올림
        assert profit_rate(110, 100) == 10.0
        assert profit_rate(100.333, 100) == 0.33
        assert profit_rate(80, 100) == -20.0


class TestUpdateRecord:
    def test_first_run_initializes_both(self):
        # 최초 실행: worst=best=현재값, status="first" (발송 대상)
        rec, status = update_record(None, 5.0)
        assert rec == {"worst": 5.0, "best": 5.0} and status == "first"

    def test_no_record_change_is_silent(self):
        # worst~best 사이 값은 갱신 없음 → status "" (침묵)
        rec, status = update_record({"worst": -3.0, "best": 10.0}, 5.0)
        assert status == "" and rec == {"worst": -3.0, "best": 10.0}

    def test_new_worst(self):
        # 최저 갱신 → worst만 교체
        rec, status = update_record({"worst": -3.0, "best": 10.0}, -7.5)
        assert status == "worst" and rec["worst"] == -7.5 and rec["best"] == 10.0

    def test_new_best(self):
        # 최고 갱신 → best만 교체
        rec, status = update_record({"worst": -3.0, "best": 10.0}, 12.34)
        assert status == "best" and rec["best"] == 12.34

    def test_legacy_float_precision_compat(self):
        # 과거 상태의 긴 소수(호환성): 반올림 후 비교 — 동일값 재계산이 갱신으로 오탐되면 안 됨
        rec, status = update_record({"worst": -3.001234, "best": 10.006789}, 10.01)
        assert status == ""  # 10.01 == round(10.006789, 2) → 갱신 아님


class TestMessageAndSend:
    def test_send_only_on_record(self):
        # 기록 갱신이 하나라도 있어야 발송
        assert not should_send([{"name": "a", "rate": 5.0, "status": ""}])
        assert should_send([{"name": "a", "rate": 5.0, "status": "best"}])

    def test_failed_account_does_not_trigger_send(self):
        # 조회 실패 계좌만으로는 발송하지 않음 (health_check가 봇 이상을 별도 담당)
        assert not should_send([{"name": "a", "rate": None, "error": "URLError"}])

    def test_message_minimal_mobile_format(self):
        # 모바일 가독: <pre> 미사용, 제목 중립(아이콘 없음), 상태 아이콘은 해당 행에만, 텍스트 라벨 없음
        msg = build_summary_message(
            "[t]", "2026-07-21 12:00",
            [
                {"name": "up", "rate": 15.0, "status": "best"},
                {"name": "down", "rate": -12.0, "status": "worst"},
                {"name": "flat", "rate": 1.0, "status": ""},
                {"name": "new", "rate": 0.0, "status": "first"},
                {"name": "bad", "rate": None, "error": "Timeout"},
            ],
        )
        assert msg.startswith("<b>[t] 수익률 기록 갱신</b>")  # 제목에 아이콘 없음
        assert "<pre>" not in msg and "🕘" not in msg  # 모바일 작은 글씨·시계 아이콘 제거
        assert "최저" not in msg and "최고" not in msg and "최초" not in msg  # 텍스트 라벨 없음
        assert "down  -12.00%  🙏" in msg and "up  +15.00%  🚀" in msg  # 아이콘은 행 끝에만
        assert "flat  +1.00%" in msg and "Timeout" in msg
        assert "~" not in msg  # 기록범위 미표시 (사용자 결정 2026-07-21)


class TestDaysSince:
    def test_start_day_is_day_one(self):
        # 시작일 당일 = 1일째, 이후 하루마다 +1 (2026-07-17 시작 → 07-21은 5일째)
        from datetime import date
        from ohlryn_monitor.pnl import days_since
        assert days_since("2026-07-17", date(2026, 7, 17)) == 1
        assert days_since("2026-07-17", date(2026, 7, 21)) == 5

    def test_header_shows_day_count(self):
        # day_n 전달 시 헤더에 "N일째" 표기, 없으면 미표기
        rows = [{"name": "a", "rate": 1.0, "status": "best"}]
        assert "5일째" in build_summary_message("[t]", "07-21 12:00", rows, day_n=5)
        assert "일째" not in build_summary_message("[t]", "07-21 12:00", rows)
