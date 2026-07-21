"""알림 어댑터 — .env 파싱 + 텔레그램 전송. stdlib(urllib)만. I/O.

vector-backtester `ctrend_telegram_report.py` 패턴을 이관하며 전송을 견고화:
- **재시도 3회 + 지수 백오프** (일시적 네트워크 blip이 리포트를 죽이지 않게)
- **타임아웃 30s** (느린 핸드셰이크 허용)
- **IPv4 우선** (api.telegram.org IPv6 경로 불안정 회피 — server3 2026-07-04 사고)

순수 로직(재시도 오케스트레이션)과 I/O(_send_once)를 분리해, 재시도 동작은 네트워크 없이 테스트한다.
"""

from __future__ import annotations

import socket
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager

_orig_getaddrinfo = socket.getaddrinfo


def parse_env(path: str) -> dict[str, str]:
    """KEY=VALUE 형식 .env 파일 파싱 (따옴표·주석·빈 줄 처리)."""
    env: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


@contextmanager
def prefer_ipv4():
    """이 블록 동안 socket.getaddrinfo가 IPv4(AF_INET) 주소만 반환하게 강제.

    urllib은 getaddrinfo 순서대로 연결을 시도하는데, api.telegram.org가 IPv6로
    먼저 resolve되고 그 경로가 불안정하면 핸드셰이크가 타임아웃난다. 단일 스레드
    cron 알리미 전제 — getaddrinfo를 임시 패치하고 finally에서 복원한다.
    """

    def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002 — stdlib 시그니처
        return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = ipv4_only
    try:
        yield
    finally:
        socket.getaddrinfo = _orig_getaddrinfo


def _send_once(token: str, chat_id: str, text: str, timeout: float) -> None:
    """텔레그램 sendMessage 1회 (HTML). IPv4 우선. 실패 시 예외 전파."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    ).encode()
    with prefer_ipv4():
        with urllib.request.urlopen(  # noqa: S310 — 고정 호스트
            urllib.request.Request(url, data=data), timeout=timeout
        ) as r:
            r.read()


def telegram_send(
    token: str,
    chat_id: str,
    text: str,
    *,
    retries: int = 3,
    timeout: float = 30.0,
    backoff: float = 1.0,
    _sender: Callable[[], None] | None = None,
    _sleep: Callable[[float], None] = time.sleep,
) -> None:
    """텔레그램 전송 (재시도 + 백오프). 모든 재시도 실패 시 마지막 예외를 raise.

    재시도 사이 대기 = backoff * 2**attempt (backoff=1 → 1s, 2s). 네트워크 오류
    (OSError 계열 — URLError/timeout/SSLError 포함)만 재시도한다.

    `_sender`/`_sleep`는 테스트 주입용(네트워크 없이 재시도 로직 검증).
    """
    send = _sender or (lambda: _send_once(token, chat_id, text, timeout))
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            send()
            return
        except OSError as e:  # URLError·socket.timeout·ssl.SSLError 모두 OSError 하위
            last_exc = e
            if attempt < retries - 1:
                _sleep(backoff * (2**attempt))
    assert last_exc is not None
    raise last_exc
