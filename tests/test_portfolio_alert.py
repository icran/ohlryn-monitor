"""portfolio_signal_alert 오케스트레이션 테스트 — fetch 주입으로 네트워크 없이 검증."""

import unittest

from bot_ops.alerters.portfolio_signal_alert import build_alert_message, evaluate


def _fake_fetch(series_by_symbol):
    """symbol→closes 매핑을 주입 가능한 fetch 함수로 변환."""

    def fetch(symbol, interval="1d", limit=200):
        return series_by_symbol[symbol]

    return fetch


class TestEvaluate(unittest.TestCase):
    def test_시그널_변경_감지(self):
        # BTC: 직전 below → 현재 above(상향돌파), ETH: 변화 없음
        fetch = _fake_fetch({"BTC": [1, 2, 3, 10], "ETH": [10, 9, 8, 1]})
        prev = {"BTC": "below", "ETH": "below"}
        new_state, changes, errors = evaluate(["BTC", "ETH"], "1d", 3, 10, prev, fetch=fetch)
        self.assertEqual(new_state, {"BTC": "above", "ETH": "below"})
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["symbol"], "BTC")
        self.assertEqual((changes[0]["prev"], changes[0]["new"]), ("below", "above"))
        self.assertEqual(errors, [])

    def test_첫_관측은_변경으로_치지_않음(self):
        # prev에 없던 심볼(None) → 상태만 기록, 변경 아님
        fetch = _fake_fetch({"BTC": [1, 2, 3, 10]})
        new_state, changes, errors = evaluate(["BTC"], "1d", 3, 10, {}, fetch=fetch)
        self.assertEqual(new_state, {"BTC": "above"})
        self.assertEqual(changes, [])

    def test_fetch_실패는_격리하고_직전상태_유지(self):
        def fetch(symbol, interval="1d", limit=200):
            raise ConnectionError("boom")

        prev = {"BTC": "above"}
        new_state, changes, errors = evaluate(["BTC"], "1d", 3, 10, prev, fetch=fetch)
        self.assertEqual(new_state, {"BTC": "above"})  # 직전 상태 보존
        self.assertEqual(changes, [])
        self.assertEqual(len(errors), 1)

    def test_데이터_부족은_에러로_수집(self):
        fetch = _fake_fetch({"BTC": [1, 2]})  # sma3 계산 불가
        new_state, changes, errors = evaluate(["BTC"], "1d", 3, 10, {}, fetch=fetch)
        self.assertEqual(changes, [])
        self.assertTrue(errors and "부족" in errors[0])


class TestBuildMessage(unittest.TestCase):
    def test_메시지에_심볼과_방향_포함(self):
        changes = [{"symbol": "BTC", "prev": "below", "new": "above", "close": 65000.0, "sma": 60000.0}]
        msg = build_alert_message("Portfolio", "1d", 50, changes)
        self.assertIn("BTC", msg)
        self.assertIn("상향 돌파", msg)
        self.assertIn("1건", msg)
        self.assertIn("SMA50", msg)


if __name__ == "__main__":
    unittest.main()
