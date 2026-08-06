#!/usr/bin/env python3
"""봇 반복 경고(루프 갇힘) 감시 알리미 — cron(5~15분)용 I/O 어댑터.

봇 로그에서 지정 패턴이 최근 N분에 임계 이상 반복되면 🚨, 멈추면 ✅ 해소를 보낸다.

배경: 2026-08-04 server1 :8014 — #29 양변기 SOXL 이 부분 청산 후 잔량에 부동소수점
먼지(2.22e-16)를 남겨 포지션이 닫히지 않았다. 엔진이 매 tick 청산을 재시도했고 최소
주문량 미달로 거부되어 **그 레그의 신규 진입이 영구 차단**됐다. 돈은 잃지 않았지만
전략이 죽었고, 흔적이 로그에만 남아 아무도 몰랐다(사용자가 우연히 체결 알림을 보고
물어봐서 발견). 근본 수정과 별개로 "조용히 멈춤"을 구조적으로 드러내기 위한 감시다.

Usage:
  python3 -m ohlryn_monitor.alerters.stuck_loop_watch --config config/stuck_loop_watch.myserver.json
  python3 -m ohlryn_monitor.alerters.stuck_loop_watch --config ... --dry-run

Config 예시: config/stuck_loop_watch.example.json
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

from ohlryn_monitor.notify import parse_env, telegram_send
from ohlryn_monitor.state import load_state, save_state
from ohlryn_monitor.stuck_loop import count_recent_matches, plan_stuck_alerts

# 로그 전체를 읽지 않는다 — 반복 루프는 최근 구간에만 있으면 되고 로그는 수백 MB 가 될 수 있다.
DEFAULT_TAIL_BYTES = 512 * 1024


def tail_lines(path: str, max_bytes: int) -> list[str]:
    """파일 끝에서 최대 max_bytes 만 읽어 줄 리스트로. 없으면 빈 리스트."""
    if not os.path.exists(path):
        return []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()  # 잘린 첫 줄 버림
        data = f.read()
    return data.decode("utf-8", errors="replace").splitlines()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="JSON config 경로")
    ap.add_argument("--dry-run", action="store_true", help="텔레그램 전송/상태 저장 안 함")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    repo = cfg["repo"]
    now = datetime.now(timezone.utc)
    tail_bytes = cfg.get("tail_bytes", DEFAULT_TAIL_BYTES)
    cooldown = cfg.get("cooldown_minutes", 360)

    state = load_state(cfg["state_file"])
    sent_map = state.setdefault("sent", {})

    # 같은 로그를 여러 감시가 공유하므로 파일당 한 번만 읽는다.
    cache: dict[str, list[str]] = {}
    watches, statuses = [], []
    for w in cfg["watches"]:
        log_path = w["log"] if os.path.isabs(w["log"]) else os.path.join(repo, w["log"])
        if log_path not in cache:
            cache[log_path] = tail_lines(log_path, tail_bytes)
        window = w.get("window_minutes", 15)
        count = count_recent_matches(cache[log_path], w["pattern"], now - timedelta(minutes=window))
        watches.append({
            "name": w["name"], "label": w.get("label", w["name"]), "pattern": w["pattern"],
            "count": count, "threshold": w.get("threshold", 20),
            "window_minutes": window, "action": w.get("action"),
        })
        statuses.append(f"{w['name']}={count}")

    to_send, new_sent = plan_stuck_alerts(watches, now, sent_map, cooldown_minutes=cooldown)
    state["sent"] = new_sent

    if to_send:
        prefix = cfg.get("alert_prefix", "[stuck]")
        if args.dry_run:
            for msg in to_send:
                print(f"DRY-RUN telegram: {prefix} {msg}")
        else:
            # env 파싱은 실발송 직전에만 (dry-run 은 env 없이 동작해야 한다).
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
