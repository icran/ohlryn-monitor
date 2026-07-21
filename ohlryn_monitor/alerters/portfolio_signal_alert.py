#!/usr/bin/env python3
"""포트폴리오 SMA 시그널 변경 알리미.

여러 티커의 가격 vs SMA 레짐(above/below)을 계산해, **뒤집힐 때만** 텔레그램 알림.
직전 상태는 JSON에 영속. 첫 실행(상태 없음)은 무음 초기화 (ATH 알림과 동일 패턴).

순수 로직(ohlryn_monitor.signals) + I/O 어댑터(prices/notify/state)를 조립한다.

Usage:
  python3 -m ohlryn_monitor.alerters.portfolio_signal_alert \
    --env-file /home/ubuntu/vector-backtester/.env_binance_sub \
    --symbols BTC,ETH,SOL --interval 1d --sma 50 \
    --state-file /home/ubuntu/portfolio_signal_state.json --label Portfolio
  # 미리보기(전송 안 함):
  python3 -m ohlryn_monitor.alerters.portfolio_signal_alert --env-file ... --symbols BTC,ETH --dry-run
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from ohlryn_monitor.notify import parse_env, telegram_send
from ohlryn_monitor.prices import fetch_closes
from ohlryn_monitor.signals import detect_change, sma_signal
from ohlryn_monitor.state import load_state, save_state

_ARROW = {"above": "🟢 상향 돌파", "below": "🔴 하향 이탈"}


def build_alert_message(label: str, interval: str, sma_period: int, changes: list[dict]) -> str:
    """시그널 변경 목록 → 텔레그램 HTML 메시지 (순수)."""
    kst = datetime.now(timezone.utc) + timedelta(hours=9)
    lines = [
        f"⚡ <b>{label} 시그널 변경 {len(changes)}건</b>",
        f"🕘 {kst:%Y-%m-%d %H:%M} KST",
        f"📐 {interval} · SMA{sma_period} 기준",
        "",
    ]
    body = []
    for c in changes:
        arrow = _ARROW.get(c["new"], c["new"])
        body.append(
            f"{c['symbol']:<10}{c['prev']}→{c['new']}  {arrow}\n"
            f"  close {c['close']:.6g} / sma {c['sma']:.6g}"
        )
    lines.append("<pre>" + "\n".join(body) + "</pre>")
    return "\n".join(lines)


def evaluate(symbols, interval, sma_period, limit, prev_state, fetch=fetch_closes):
    """각 심볼의 현재 시그널을 계산하고 직전 대비 변경을 수집한다.

    I/O(fetch)는 주입 가능 — 테스트에서 순수하게 검증하기 위함.
    반환: (new_state, changes, errors).
    """
    new_state = dict(prev_state)
    changes: list[dict] = []
    errors: list[str] = []
    for sym in symbols:
        try:
            closes = fetch(sym, interval=interval, limit=limit)
        except Exception as e:  # noqa: BLE001 — 개별 심볼 실패는 격리(직전 상태 유지)
            errors.append(f"{sym}: {type(e).__name__}")
            continue
        sig = sma_signal(closes, sma_period)
        if sig is None:
            errors.append(f"{sym}: 데이터 부족")
            continue
        prev = prev_state.get(sym)
        new_state[sym] = sig
        if detect_change(prev, sig):
            m = sum(closes[-sma_period:]) / sma_period
            changes.append({"symbol": sym, "prev": prev, "new": sig, "close": closes[-1], "sma": m})
    return new_state, changes, errors


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", required=True, help="TELEGRAM_TOKEN/CHAT_ID 포함 .env")
    ap.add_argument("--symbols", required=True, help="쉼표 구분 (예: BTC,ETH,SOL)")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--sma", type=int, default=50)
    ap.add_argument("--limit", type=int, default=0, help="fetch 봉 수 (0=sma+50)")
    ap.add_argument("--state-file", required=True)
    ap.add_argument("--label", default="Portfolio")
    ap.add_argument("--dry-run", action="store_true", help="전송 대신 stdout")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    limit = args.limit or (args.sma + 50)
    prev_state = load_state(args.state_file)
    first_run = not prev_state  # 상태 파일 없음/빈 → 무음 초기화

    new_state, changes, errors = evaluate(symbols, args.interval, args.sma, limit, prev_state)
    save_state(args.state_file, new_state)

    if args.dry_run and errors:
        print("[errors]", ", ".join(errors))

    if first_run:
        if args.dry_run:
            print(f"[init] {len(new_state)}개 심볼 상태 초기화 (무음): {new_state}")
        return
    if not changes:
        if args.dry_run:
            print("[no change] 시그널 변경 없음 (메시지 없음)")
        return

    msg = build_alert_message(args.label, args.interval, args.sma, changes)
    if args.dry_run:
        print(msg)
    else:
        env = parse_env(args.env_file)
        telegram_send(env["TELEGRAM_TOKEN"], env["TELEGRAM_CHAT_ID"], msg)


if __name__ == "__main__":
    main()
