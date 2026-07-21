"""bot_ops.signals 순수 로직 단위 테스트 (stdlib unittest — 의존성 0)."""

import unittest

from bot_ops.signals import detect_change, sma, sma_cross_signal, sma_signal


class TestSma(unittest.TestCase):
    def test_sma_평균_계산(self):
        # 마지막 3개 평균 = (2+3+4)/3 = 3.0
        self.assertEqual(sma([1, 2, 3, 4], 3), 3.0)

    def test_sma_데이터_부족이면_None(self):
        # period > 길이 → None
        self.assertIsNone(sma([1, 2], 3))

    def test_sma_잘못된_period면_None(self):
        self.assertIsNone(sma([1, 2, 3], 0))


class TestSmaSignal(unittest.TestCase):
    def test_가격이_SMA_위면_above(self):
        # closes=[1,2,3,10], sma3=(2+3+10)/3=5, last=10>5 → above
        self.assertEqual(sma_signal([1, 2, 3, 10], 3), "above")

    def test_가격이_SMA_아래면_below(self):
        # closes=[10,9,8,1], sma3=(9+8+1)/3=6, last=1<6 → below
        self.assertEqual(sma_signal([10, 9, 8, 1], 3), "below")

    def test_데이터_부족이면_None(self):
        self.assertIsNone(sma_signal([1, 2], 5))

    def test_경계값_동일하면_above(self):
        # 모든 값 5 → sma=5, last=5 >= 5 → above
        self.assertEqual(sma_signal([5, 5, 5], 3), "above")


class TestSmaCrossSignal(unittest.TestCase):
    def test_fast가_slow_위면_bull(self):
        # 상승 추세: 최근값이 커서 fast SMA > slow SMA
        closes = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(sma_cross_signal(closes, 2, 5), "bull")

    def test_fast가_slow_아래면_bear(self):
        closes = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
        self.assertEqual(sma_cross_signal(closes, 2, 5), "bear")

    def test_fast가_slow_이상이면_None(self):
        self.assertIsNone(sma_cross_signal([1, 2, 3], 5, 2))


class TestDetectChange(unittest.TestCase):
    def test_시그널_뒤집히면_True(self):
        self.assertTrue(detect_change("above", "below"))

    def test_동일하면_False(self):
        self.assertFalse(detect_change("above", "above"))

    def test_None_포함이면_False(self):
        # 첫 관측(None)·데이터 결손은 변경으로 치지 않는다
        self.assertFalse(detect_change(None, "above"))
        self.assertFalse(detect_change("above", None))


if __name__ == "__main__":
    unittest.main()
