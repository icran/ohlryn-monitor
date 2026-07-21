"""시그널 계산 — 순수 로직 (네트워크·파일 I/O 없음, 단위 테스트 대상)."""

from __future__ import annotations


def sma(values: list[float], period: int) -> float | None:
    """마지막 period개 값의 단순이동평균. 데이터 부족/잘못된 period면 None."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def sma_signal(closes: list[float], period: int) -> str | None:
    """가격 vs SMA 레짐. 마지막 종가가 SMA 이상이면 'above', 미만이면 'below'.

    데이터가 부족하면 None (불충분 → 시그널 없음). 경계(==)는 'above'로 처리.
    """
    m = sma(closes, period)
    if m is None or not closes:
        return None
    return "above" if closes[-1] >= m else "below"


def sma_cross_signal(closes: list[float], fast: int, slow: int) -> str | None:
    """단·장기 SMA 교차 레짐. fast SMA가 slow SMA 이상이면 'bull', 미만이면 'bear'.

    확장용(price-vs-sma의 변형). 데이터 부족 시 None.
    """
    if fast <= 0 or slow <= 0 or fast >= slow:
        return None
    f, s = sma(closes, fast), sma(closes, slow)
    if f is None or s is None:
        return None
    return "bull" if f >= s else "bear"


def detect_change(prev: str | None, new: str | None) -> bool:
    """이전·현재 시그널이 모두 정의됐고 서로 다르면 True.

    None(불충분/초기)은 변경으로 치지 않는다 — 첫 관측·데이터 결손이 알림을 유발하지 않도록.
    """
    if prev is None or new is None:
        return False
    return prev != new
