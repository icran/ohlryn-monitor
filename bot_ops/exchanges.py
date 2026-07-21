"""거래소 계좌 equity 조회 — I/O 어댑터 (stdlib hmac/urllib만).

Cayenne `account_telegram_report.py`의 조회부 이식. 시크릿은 vector-backtester
`.env_*` 규약(BINANCE_API_KEY/BYBIT_API_KEY ...)에서 읽는다 (notify.parse_env).

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


def fetch_equity_binance(api_key: str, secret: str, *, timeout: float = 15.0) -> float:
    q = f"timestamp={int(time.time() * 1000)}&recvWindow=5000"
    sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"https://fapi.binance.com/fapi/v2/account?{q}&signature={sig}",
        headers={"X-MBX-APIKEY": api_key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — 고정 호스트
        data = json.loads(r.read().decode())
    return float(data["totalMarginBalance"])


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
