"""ohlryn_monitor.pnl 순수 로직 테스트."""

from ohlryn_monitor.pnl import (
    build_summary_message,
    net_transfers,
    profit_rate,
    should_send,
    update_record,
)


class TestProfitRate:
    def test_basic_and_rounding(self):
        # 수익률 계산 + 소수점 2자리 반올림
        assert profit_rate(110, 100) == 10.0
        assert profit_rate(100.333, 100) == 0.33
        assert profit_rate(80, 100) == -20.0


class TestNetTransfers:
    def test_empty_is_zero(self):
        # 이체 내역이 없으면(None/빈 리스트) 보정 0 — 기존 config 하위호환
        assert net_transfers(None) == 0.0
        assert net_transfers([]) == 0.0

    def test_withdrawal_restores_rate(self):
        # 출금 -1000 후 equity가 1000 줄어도, 보정하면 이체 전과 같은 수익률
        # (initial 10000, 실손익 +500인 계좌에서 1000 출금 → equity 9500)
        transfers = [{"date": "2026-08-04", "amount": -1000, "memo": "이체 출금"}]
        adj = net_transfers(transfers)
        assert adj == -1000.0
        assert profit_rate(9500 - adj, 10000) == 5.0

    def test_deposit_and_withdrawal_sum(self):
        # 입금 +2000, 출금 -500 → 순이체 +1500. equity에서 차감해 외부 자금 유입을 수익으로 오인하지 않음
        transfers = [{"amount": 2000}, {"amount": -500}]
        assert net_transfers(transfers) == 1500.0


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

    def test_sub_step_change_is_silent(self):
        # 0.01%p 악화(-8.26 → -8.27)는 알림 없음 — 잦은 알림이 몰입을 방해한다.
        # 단 기록 자체는 갱신해 실제 최저치를 잃지 않는다.
        rec, status = update_record({"worst": -8.26, "best": 3.0}, -8.27)
        assert status == "" and rec["worst"] == -8.27

    def test_crossing_whole_percent_alerts(self):
        # -8.26 → -9.01: 정수 경계(-9%)를 통과하면 알림
        _, status = update_record({"worst": -8.26, "best": 3.0}, -9.01)
        assert status == "worst"

    def test_record_on_boundary_needs_next_step(self):
        # 기록이 이미 정확히 -9.00이면 다음 임계는 -10 (같은 경계 재알림 방지)
        _, s1 = update_record({"worst": -9.0, "best": 3.0}, -9.5)
        assert s1 == ""
        _, s2 = update_record({"worst": -9.0, "best": 3.0}, -10.0)
        assert s2 == "worst"

    def test_best_side_uses_same_step(self):
        # 최고 기록도 동일하게 1%p 단위
        _, s1 = update_record({"worst": -3.0, "best": 10.4}, 10.9)
        assert s1 == ""
        _, s2 = update_record({"worst": -3.0, "best": 10.4}, 11.0)
        assert s2 == "best"

    def test_step_is_configurable(self):
        # step으로 민감도 조절 (0.5%p 단위)
        _, status = update_record({"worst": -8.26, "best": 3.0}, -8.6, step=0.5)
        assert status == "worst"

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
