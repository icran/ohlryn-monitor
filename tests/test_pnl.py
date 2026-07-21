"""bot_ops.pnl 순수 로직 테스트 (Cayenne 이식 규칙 검증)."""

from bot_ops.pnl import build_summary_message, profit_rate, should_send, update_record


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

    def test_message_icons(self):
        # +10% 이상 🟢, −10% 이하 🔴, 상태 아이콘(🚀/🙏/최초) 표기
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
        assert "🟢" in msg and "🚀" in msg and "🔴" in msg and "🙏" in msg
        assert "(최초)" in msg and "Timeout" in msg
