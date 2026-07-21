#!/usr/bin/env python3
"""서버 통합 헬스체크 알리미 — cron(*/5분)용 I/O 어댑터.

3계층 모니터링의 2계층:
    [watchdog]         프로세스 죽음 → 재기동 (별도)
    [이 스크립트]       봇 좀비·리소스·로그 에러 → 직접 텔레그램
    [healthchecks.io]  정상 완료 시 heartbeat ping — 침묵 = 서버 사망 알림
                       (CRITICAL 발견 시 /fail ping으로 이중 발화)

순수 판정(ohlryn_monitor.health) + I/O(notify/state + 봇 /api/v1 + /proc)를 조립한다.
경계 규칙: vector_backtester import 금지 — 봇 HTTP API·시스템 파일만.

Usage:
  python3 -m ohlryn_monitor.alerters.health_check --config /path/health_check.json
  python3 -m ohlryn_monitor.alerters.health_check --config ... --dry-run   # 전송/ping 안 함

Config(JSON) 예시 — 시크릿은 env 파일에 두고 경로만 참조:
{
  "repo": "/home/ubuntu/vector-backtester",
  "state_file": "/home/ubuntu/.ohlryn_monitor_health_state.json",
  "ping_url": "https://hc-ping.com/<uuid>",          // 없으면 heartbeat 생략
  "alert_env": ".env_binance_main",                   // TELEGRAM_TOKEN/CHAT_ID 출처 (repo 기준 상대)
  "alert_prefix": "[server1-health]",
  "cooldown_sec": 3600,
  "stale_minutes": 40,
  "log_error_threshold": 5,
  "system_limits": {"disk_pct_max": 85, "mem_avail_mb_min": 400,
                     "swap_used_mb_max": 2048, "load1_max": 3.0},
  "bots": [
    {"name": "sm-binance", "port": 8010, "env": ".env_sm...", "log": "sm_....log"}
  ]
}
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import urllib.request
from datetime import datetime, timezone

from ohlryn_monitor.health import (
    Issue,
    bot_issues,
    count_log_errors,
    has_critical,
    log_issue,
    next_log_offset,
    plan_alerts,
    system_issues,
)
from ohlryn_monitor.notify import parse_env, telegram_send
from ohlryn_monitor.state import load_state, save_state


def fetch_bot_engines(repo: str, bot: dict) -> tuple[list[dict] | None, str | None]:
    """봇 /api/v1/bots 조회. 반환: (engines | None, 오류명). 응답 {"bots": [...]} 언랩."""
    env = parse_env(os.path.join(repo, bot["env"]))
    auth = base64.b64encode(
        f"{env.get('WEB_USERNAME', '')}:{env.get('WEB_PASSWORD', '')}".encode()
    ).decode()
    req = urllib.request.Request(
        f"http://localhost:{bot['port']}/api/v1/bots",
        headers={"Authorization": f"Basic {auth}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 — localhost 고정
            payload = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001 — 원인 무관하게 '응답 없음'으로 보고
        return None, type(e).__name__
    engines = payload.get("bots", []) if isinstance(payload, dict) else payload
    return engines if isinstance(engines, list) else [], None


def read_system_metrics() -> dict:
    du = shutil.disk_usage("/")
    mem: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            mem[k] = int(v.strip().split()[0])  # kB
    return {
        "disk_pct": du.used / du.total * 100,
        "mem_avail_mb": mem.get("MemAvailable", 0) // 1024,
        "swap_used_mb": (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) // 1024,
        "load1": os.getloadavg()[0],
    }


def scan_log(repo: str, bot: dict, offsets: dict) -> int:
    """이전 실행 이후 신규 로그의 에러 라인 수. offsets는 in-place 갱신."""
    path = os.path.join(repo, bot["log"])
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    start, offsets[bot["log"]] = next_log_offset(offsets.get(bot["log"]), size)
    if size <= start:
        return 0
    with open(path, "rb") as f:
        f.seek(start)
        chunk = f.read(size - start).decode(errors="replace")
    return count_log_errors(chunk)


def ping(url: str, *, fail: bool) -> None:
    try:
        urllib.request.urlopen(url + ("/fail" if fail else ""), timeout=10)  # noqa: S310
    except Exception as e:  # noqa: BLE001 — ping 실패가 체크를 죽이면 안 됨
        print(f"heartbeat ping 실패: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="JSON config 경로")
    ap.add_argument("--dry-run", action="store_true", help="텔레그램/ping 전송 안 함")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    repo = cfg["repo"]
    limits = cfg.get("system_limits", {})
    now = datetime.now(timezone.utc)

    state = load_state(cfg["state_file"])
    issues: list[Issue] = []

    for bot in cfg["bots"]:
        engines, err = fetch_bot_engines(repo, bot)
        issues.extend(
            bot_issues(bot["name"], engines, now=now, stale_minutes=cfg.get("stale_minutes", 40), error=err)
        )

    issues.extend(system_issues(read_system_metrics(), limits))

    offsets = state.setdefault("log_offsets", {})
    for bot in cfg["bots"]:
        n = scan_log(repo, bot, offsets)
        issues.extend(log_issue(bot["name"], n, cfg.get("log_error_threshold", 5)))

    to_send, state["active_alerts"] = plan_alerts(
        state.get("active_alerts", {}),
        issues,
        now_ts=now.timestamp(),
        cooldown_sec=cfg.get("cooldown_sec", 3600),
    )

    if to_send:
        env = parse_env(os.path.join(repo, cfg["alert_env"]))
        prefix = cfg.get("alert_prefix", "[health]")
        for msg in to_send:
            if args.dry_run:
                print(f"DRY-RUN telegram: {prefix} {msg}")
            else:
                try:
                    telegram_send(env.get("TELEGRAM_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", ""), f"{prefix} {msg}")
                except Exception as e:  # noqa: BLE001
                    print(f"telegram 발송 실패: {e}")

    if not args.dry_run:
        save_state(cfg["state_file"], state)
        if cfg.get("ping_url"):
            ping(cfg["ping_url"], fail=has_critical(issues))

    ts = now.strftime("%H:%M")
    print(f"[{ts}Z] issues={len(issues)}" + (f" {[k for k, _ in issues]}" if issues else " (all OK)"))


if __name__ == "__main__":
    main()
