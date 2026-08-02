#!/usr/bin/env python3
"""파라미터 스케줄 만료 감시 알리미 — cron(일 1회)용 I/O 어댑터.

봇의 기간별 파라미터 스케줄(CSV)의 마지막 `end`를 읽어:
  만료 D-{warn_days} 도달 시  ⚠️ WARNING (단계별 1회)
  만료 후                     🚨 CRITICAL (1회)
  CSV 읽기 실패               🚨 CRITICAL (감시 사슬 고장)

배경: 2026-07-31 — WFA 스케줄이 조용히 만료된 채 봇이 재시작되어 파라미터가
기본값으로 회귀(진입 필터 OFF), 차단됐어야 할 진입으로 손실. 상세: docs/runbook.md.

Usage:
  python3 -m ohlryn_monitor.alerters.schedule_watch --config config/schedule_watch.myserver.json
  python3 -m ohlryn_monitor.alerters.schedule_watch --config ... --dry-run

Config 예시: config/schedule_watch.example.json
"""

import argparse
import json
import os
from datetime import datetime, timezone

from ohlryn_monitor.notify import parse_env, telegram_send
from ohlryn_monitor.schedule import last_end_from_csv, plan_schedule_alerts
from ohlryn_monitor.state import load_state, save_state


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="JSON config 경로")
    ap.add_argument("--dry-run", action="store_true", help="텔레그램 전송/상태 저장 안 함")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    repo = cfg["repo"]
    now = datetime.now(timezone.utc)

    state = load_state(cfg["state_file"])
    sent_map = state.setdefault("sent", {})
    warn_days = tuple(cfg.get("warn_days", [7, 3]))

    to_send: list[str] = []
    statuses: list[str] = []
    for sched in cfg["schedules"]:
        name = sched["name"]
        csv_path = os.path.join(repo, sched["csv"])
        end = last_end_from_csv(csv_path)
        msgs, sent_map[name] = plan_schedule_alerts(
            name, end, now, warn_days=warn_days, already_sent=sent_map.get(name, []),
            action=sched.get("action"),
        )
        to_send.extend(msgs)
        if end is None:
            days = "CSV읽기실패"
        else:
            left = (end - now).days
            days = f"D-{left}" if left >= 0 else f"만료+{-left}d"
        statuses.append(f"{name}={days}")

    if to_send:
        prefix = cfg.get("alert_prefix", "[schedule]")
        if args.dry_run:
            for msg in to_send:
                print(f"DRY-RUN telegram: {prefix} {msg}")
        else:
            # env 파싱은 실발송 직전에만 (dry-run은 env 없이 동작해야 — 2026-07-27 사고 계급).
            # 발송 실패 시 crash → save_state 미실행 → 다음 실행에서 자동 재시도.
            env_path = cfg["alert_env"] if os.path.isabs(cfg["alert_env"]) else os.path.join(repo, cfg["alert_env"])
            env = parse_env(env_path)
            for msg in to_send:
                telegram_send(env.get("TELEGRAM_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", ""), f"{prefix} {msg}")

    if not args.dry_run:
        save_state(cfg["state_file"], state)

    print(f"[{now.strftime('%H:%M')}Z] alerts={len(to_send)} | " + " ".join(statuses))


if __name__ == "__main__":
    main()
