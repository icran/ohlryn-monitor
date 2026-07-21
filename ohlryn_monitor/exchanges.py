"""거래소 계좌 equity 조회 — I/O 어댑터 (stdlib hmac/urllib만).

시크릿은 env 파일(BINANCE_API_KEY/BYBIT_API_KEY ...)에서 읽는다 (notify.parse_env).

- binance: GET /fapi/v2/account → totalMarginBalance (미실현 포함 총 equity)
- bybit:   GET /v5/account/wallet-balance (UNIFIED) → totalEquity
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request


# binance 자산별 합산 대상 스테이블 (1:1 가정). 톱레벨 totalMarginBalance는 USDT만
# 집계해 수동 USDC 거래분이 누락됨.
_BINANCE_STABLE_ASSETS = ("USDT", "USDC", "FDUSD", "BUSD")


def fetch_equity_binance(api_key: str, secret: str, *, timeout: float = 15.0) -> float:
    q = f"timestamp={int(time.time() * 1000)}&recvWindow=5000"
    sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"https://fapi.binance.com/fapi/v2/account?{q}&signature={sig}",
        headers={"X-MBX-APIKEY": api_key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — 고정 호스트
        data = json.loads(r.read().decode())
    # 자산별 marginBalance(지갑+UPnL) 합산 — 스테이블만 1:1로. 자산 목록 없으면 톱레벨 폴백.
    assets = data.get("assets") or []
    total = sum(float(a.get("marginBalance", 0)) for a in assets if a.get("asset") in _BINANCE_STABLE_ASSETS)
    return total if total > 0 else float(data["totalMarginBalance"])


def fetch_equity_bybit(api_key: str, secret: str, *, timeout: float = 15.0) -> float:
    ts = str(int(time.time() * 1000))
    recv = "5000"
    q = "accountType=UNIFIED"
    sign = hmac.new(secret.encode(), (ts + api_key + recv + q).encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"https://api.bybit.com/v5/account/wallet-balance?{q}",
        headers={
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv,
            "X-BAPI-SIGN": sign,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        data = json.loads(r.read().decode())
    if data.get("retCode") != 0:
        raise RuntimeError(f"bybit retCode={data.get('retCode')} {data.get('retMsg')}")
    info = data["result"]["list"][0]
    total_equity = float(info.get("totalEquity") or 0)
    return total_equity if total_equity > 0 else float(info.get("totalWalletBalance") or 0)


def fetch_equity(exchange: str, env: dict, *, timeout: float = 15.0) -> float:
    """거래소별 equity 조회. env는 .env 파싱 결과 (키 이름은 vector-backtester 규약)."""
    if exchange == "binance":
        return fetch_equity_binance(env["BINANCE_API_KEY"], env["BINANCE_SECRET_KEY"], timeout=timeout)
    if exchange == "bybit":
        return fetch_equity_bybit(env["BYBIT_API_KEY"], env["BYBIT_SECRET_KEY"], timeout=timeout)
    raise ValueError(f"지원하지 않는 거래소: {exchange}")
