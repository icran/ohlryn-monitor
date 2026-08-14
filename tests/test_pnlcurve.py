"""pnlcurve 순수 로직 테스트 — 봇 청산 거래 → 일별 누적 수익률 곡선."""

from ohlryn_monitor.pnlcurve import combined_curve, daily_net_pnl, fill_curve


def _t(exit_time, net_pnl=None, pnl=0.0, is_open=False):
    return {"exit_time": exit_time, "net_pnl": net_pnl, "pnl": pnl, "is_open": is_open}


class TestDailyNetPnl:
    def test_같은_날_청산은_합산된다(self):
        # 하루에 2건 청산 → 그 날짜 키에 net_pnl 합
        trades = [_t("2026-07-17T07:26:00Z", net_pnl=100.0), _t("2026-07-17T21:00:00Z", net_pnl=-30.0)]
        assert daily_net_pnl(trades) == {"2026-07-17": 70.0}

    def test_net_pnl_없으면_pnl_폴백(self):
        # 구버전 봇 응답엔 net_pnl이 없을 수 있다 → pnl로 폴백
        trades = [_t("2026-07-18T00:00:00Z", net_pnl=None, pnl=50.0)]
        assert daily_net_pnl(trades) == {"2026-07-18": 50.0}

    def test_오픈_거래와_청산시각_없는_거래는_제외(self):
        # 실현 곡선이므로 미청산 거래는 포함하지 않는다
        trades = [
            _t("2026-07-18T00:00:00Z", net_pnl=10.0, is_open=True),
            {"exit_time": None, "net_pnl": 99.0, "pnl": 99.0, "is_open": False},
            _t("2026-07-19T00:00:00Z", net_pnl=5.0),
        ]
        assert daily_net_pnl(trades) == {"2026-07-19": 5.0}


class TestFillCurve:
    def test_달력_채움과_누적(self):
        # 거래 없는 날도 직전 누적값으로 채워 연속 곡선을 만든다.
        # 맨 앞에 시작 전날 0% 기준점을 붙인다(차트가 0에서 출발하게).
        pnl = {"2026-07-17": 100.0, "2026-07-19": -50.0}
        curve = fill_curve(pnl, initial=1000.0, start="2026-07-17", end="2026-07-20")
        assert curve == [
            ("2026-07-16", 0.0),
            ("2026-07-17", 0.1),
            ("2026-07-18", 0.1),
            ("2026-07-19", 0.05),
            ("2026-07-20", 0.05),
        ]

    def test_시작일_이전_거래는_시작일에_귀속되지_않는다(self):
        # start 이후 곡선만 요청해도 누적은 start 이전 pnl을 포함해야 한다
        # (중간부터 그리면 0에서 시작하는 게 아니라 그날까지의 누적이어야 함)
        pnl = {"2026-07-15": 100.0, "2026-07-17": 100.0}
        curve = fill_curve(pnl, initial=1000.0, start="2026-07-17", end="2026-07-17")
        assert curve[-1] == ("2026-07-17", 0.2)

    def test_거래가_없으면_빈_곡선(self):
        assert fill_curve({}, initial=1000.0, start=None, end="2026-07-20") == []


class TestCombinedCurve:
    def test_계좌_합산은_pnl_합과_initial_합으로(self):
        # 합산 수익률 = Σ일별pnl / Σinitial. 계좌마다 시작일이 달라도
        # 아직 거래 없는 계좌는 0 기여로 자연스럽게 처리된다.
        maps = [{"2026-07-17": 100.0}, {"2026-07-18": -50.0}]
        curve = combined_curve(maps, initials=[1000.0, 1000.0], end="2026-07-18")
        assert curve == [
            ("2026-07-16", 0.0),
            ("2026-07-17", 0.05),
            ("2026-07-18", 0.025),
        ]

    def test_전부_비면_빈_곡선(self):
        assert combined_curve([{}, {}], initials=[1000.0, 500.0], end="2026-07-18") == []
