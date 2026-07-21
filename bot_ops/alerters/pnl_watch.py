#!/usr/bin/env python3
"""계좌 수익률 최고/최저 기록 알리미 — cron(매시)용 I/O 어댑터.

계좌별 초기 투자금 대비 현재 equity 수익률을 조회해, **최초/최저 갱신(🙏)/
최고 갱신(🚀) 때만** 전 계좌 요약을 텔레그램 발송. 갱신 없으면 침묵.
Cayenne `[COIN]risk_check_main.py`(BlockingScheduler 매시 12분)의 bot-ops 이식판.

순수 로직(bot_ops.pnl) + I/O(exchanges/notify/state)를 조립한다.

Usage:
  python3 -m bot_ops.alerters.pnl_watch --config /path/pnl_watch.json
  python3 -m bot_ops.alerters.pnl_watch --config ... --dry-run   # 조회만, 전송/저장 안 함

Config(JSON) — 시크릿은 env 파일에 두고 경로만 참조:
{
  "repo": "/home/ubuntu/vector-backtester",
  "state_file": "/home/ubuntu/.bot_ops_pnl_state.json",
  "alert_env": ".env_binance_main",
  "alert_prefix": "[server1-pnl]",
  "accounts": [
    {"name": "main-binance", "exchange": "binance", "env": ".env_binance_main", "initial": 100000}
  ]
}
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

from bot_ops.exchanges import fetch_equity
from bot_ops.notify import parse_env, telegram_send
from bot_ops.pnl import build_summary_message, days_since, profit_rate, should_send, update_record
from bot_ops.state import load_state, save_state


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="JSON config 경로")
    ap.add_argument("--dry-run", action="store_true", help="전송/상태저장 안 함 (조회 결과만 출력)")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    repo = cfg["repo"]

    state = load_state(cfg["state_file"])
    records = state.setdefault("records", {})

    rows: list[dict] = []
    for acc in cfg["accounts"]:
        name = acc["name"]
        try:
            env = parse_env(os.path.join(repo, acc["env"]))
            equity = fetch_equity(acc["exchange"], env)
            rate = profit_rate(equity, acc["initial"])
            new_rec, status = update_record(records.get(name), rate)
            records[name] = new_rec
            rows.append({"name": name, "rate": rate, "status": status, "record": new_rec, "equity": equity})
        except Exception as e:  # noqa: BLE001 — 한 계좌 실패가 나머지를 막으면 안 됨
            rows.append({"name": name, "rate": None, "error": type(e).__name__})

    now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
    day_n = days_since(cfg["start_date"], now_kst.date()) if cfg.get("start_date") else None
    message = build_summary_message(
        cfg.get("alert_prefix", "[pnl]"), now_kst.strftime("%Y-%m-%d %H:%M"), rows, day_n=day_n
    )

    for r in rows:
        eq = f" equity={r['equity']:,.2f}" if r.get("equity") is not None else ""
        print(f"  {r['name']}: rate={r.get('rate')} status={r.get('status', r.get('error'))}{eq}")

    if args.dry_run:
        print(f"DRY-RUN send={should_send(rows)}\n{message}")
        return

    if should_send(rows):
        env = parse_env(os.path.join(repo, cfg["alert_env"]))
        try:
            telegram_send(env.get("TELEGRAM_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", ""), message)
        except Exception as e:  # noqa: BLE001
            print(f"telegram 발송 실패: {e}")
    save_state(cfg["state_file"], state)


if __name__ == "__main__":
    main()
