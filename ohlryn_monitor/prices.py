"""가격 데이터 어댑터 — Binance USDⓈ-M public klines (인증·봇 불필요). I/O."""

from __future__ import annotations

import json
import urllib.request

BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"


def to_binance_symbol(s: str) -> str:
    """다양한 표기를 Binance 심볼로 정규화.

    'BTC/USDT:USDT' -> 'BTCUSDT', 'BTC' -> 'BTCUSDT', 'BTCUSDT' -> 'BTCUSDT'.
    (순수 함수 — I/O 없음. 편의상 이 모듈에 둠.)
    """
    s = s.upper().split(":")[0].replace("/", "").strip()
    if not s.endswith("USDT"):
        s += "USDT"
    return s


def fetch_closes(
    symbol: str, interval: str = "1d", limit: int = 200, base: str = BINANCE_FAPI_KLINES
) -> list[float]:
    """종가 시계열(오래된→최신)을 반환. symbol은 자유 표기 허용(정규화됨)."""
    sym = to_binance_symbol(symbol)
    url = f"{base}?symbol={sym}&interval={interval}&limit={limit}"
    with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 — 고정 호스트
        data = json.load(r)
    # kline: [openTime, open, high, low, close, volume, closeTime, ...]
    return [float(k[4]) for k in data]
