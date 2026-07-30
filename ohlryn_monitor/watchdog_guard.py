"""[순수+CLI] watchdog crash-loop 차단 판정.

쉘 watchdog은 봇 사망을 발견하면 재기동 전에 `check`를 호출한다:

    PYTHONPATH=~/ohlryn-monitor python3 -m ohlryn_monitor.watchdog_guard check \
        --name natas-sub --state ~/wd_state/natas-sub.json \
        --flag ~/wd_state/natas-sub.crashloop --log ~/bot.log

    stdout 1행: RESTART | BREAK   (BREAK면 플래그 파일도 생성됨)
    BREAK 시 알림 본문이 <flag>.alert 파일에 저장된다 — 쉘이 텔레그램으로 발송.

재기동에 성공하면 `on-start`로 기동 시각을 기록한다.

판정 규칙 (2026-07-30 합의):
  1) 오래 돌다 사망 1회            → RESTART
  2) 부팅 grace(120s) 내 사망 연속 2회 → BREAK  (같은 조건 사망의 대리 지표)
  3) 종류 무관 30분 내 3회 사망     → BREAK  (백스톱)
복구: 원인 수정 후 플래그 파일 삭제 → 다음 주기 정상 재기동.

에러 시그니처(마지막 Traceback 예외 줄)는 판정에 쓰지 않고 알림에만 첨부한다
— 로그 파싱 실패가 오판으로 이어지지 않게 판정은 생존시간으로만 한다.
"""

import argparse
import os
import re
import time

from ohlryn_monitor.state import load_state, save_state

BOOT_GRACE_SEC = 120  # 기동 후 이 시간 내 사망 = 부팅실패
BOOT_STREAK_N = 2  # 부팅실패 연속 N회 → 차단
BOOT_STREAK_WINDOW_SEC = 3600  # 연속으로 인정할 최대 간격 (어제 실패와 이어 붙이지 않음)
WINDOW_SEC = 1800  # 백스톱 윈도 (30분)
WINDOW_N = 3  # 윈도 내 사망 N회 → 차단
HISTORY_KEEP = 20  # 상태파일에 남길 최근 사망 기록 수
ALERT_MAX_CHARS = 3500  # 텔레그램 4096 제한 여유
LOG_TAIL_LINES = 30

_EXC_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit))\b.*")


# ── 순수 로직 ────────────────────────────────────────────────────────


def classify_death(start_ts: float | None, death_ts: float, boot_grace: float = BOOT_GRACE_SEC) -> str:
    """생존시간으로 사망 종류 분류. 기동 시각 미상이면 보수적으로 일반 사망."""
    if start_ts is None:
        return "runtime_death"
    return "boot_failure" if (death_ts - start_ts) < boot_grace else "runtime_death"


def decide(
    history: list[dict],
    now: float,
    *,
    boot_streak_n: int = BOOT_STREAK_N,
    boot_streak_window: float = BOOT_STREAK_WINDOW_SEC,
    window_sec: float = WINDOW_SEC,
    window_n: int = WINDOW_N,
) -> tuple[str, str]:
    """사망 이력(마지막 항목 = 이번 사망)으로 차단 여부 판정.

    Returns:
        ("RESTART" | "BREAK", 사유)
    """
    # 규칙 2: 부팅실패 연속 N회 (streak — 최근 항목부터 거슬러 세되, 간격이
    # boot_streak_window를 넘으면 연속으로 보지 않는다)
    streak = 0
    prev_ts = now
    for entry in reversed(history):
        if entry["kind"] != "boot_failure" or (prev_ts - entry["ts"]) > boot_streak_window:
            break
        streak += 1
        prev_ts = entry["ts"]
    if streak >= boot_streak_n:
        return "BREAK", f"부팅실패 연속 {streak}회 (기동 {BOOT_GRACE_SEC}s 내 사망 반복 — 구조적 문제 의심)"

    # 규칙 3: 윈도 내 총 사망 횟수 백스톱
    recent = [e for e in history if (now - e["ts"]) <= window_sec]
    if len(recent) >= window_n:
        return "BREAK", f"{window_sec // 60:.0f}분 내 {len(recent)}회 사망 (백스톱)"

    return "RESTART", "일시 장애로 판단 — 재기동"


def extract_crash_signature(log_tail: str) -> str | None:
    """로그 꼬리에서 마지막 예외 줄(예: 'TypeError: ...')을 추출한다. 없으면 None."""
    signature = None
    for line in log_tail.splitlines():
        if _EXC_RE.match(line.strip()):
            signature = line.strip()
    return signature


def build_alert(
    name: str,
    reason: str,
    signature: str | None,
    prev_signature: str | None,
    log_tail: str,
    flag_path: str,
) -> str:
    """차단 CRITICAL 알림 본문 조립 (텔레그램 길이 제한 내 트림)."""
    lines = [f"🚨 crash loop 차단: {name}", f"사유: {reason}"]
    if signature and prev_signature:
        same = "✅" if signature == prev_signature else "❌"
        lines.append(f"직전 사망과 동일 에러: {same} {signature}")
    elif signature:
        lines.append(f"마지막 에러: {signature}")
    lines.append(f"복구: 원인 수정 후 rm {flag_path}")
    lines.append(f"─── 마지막 로그 {LOG_TAIL_LINES}줄 ───")
    head = "\n".join(lines)
    return (head + "\n" + log_tail)[:ALERT_MAX_CHARS]


# ── CLI (I/O 조립) ───────────────────────────────────────────────────


def _read_log_tail(path: str, n_lines: int = LOG_TAIL_LINES) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n_lines:])
    except OSError:
        return "(로그 파일 읽기 실패)"


def cmd_check(args: argparse.Namespace) -> int:
    now = time.time()
    state = load_state(args.state)
    history = state.get("deaths", [])

    kind = classify_death(state.get("last_start_ts"), now, args.boot_grace)
    log_tail = _read_log_tail(args.log) if args.log else ""
    signature = extract_crash_signature(log_tail)
    history.append({"ts": now, "kind": kind})
    history = history[-HISTORY_KEEP:]

    action, reason = decide(history, now)

    prev_signature = state.get("last_signature")
    state.update(
        deaths=history,
        last_signature=signature or prev_signature,
        last_start_ts=None,  # 사망 처리됨 — 다음 on-start까지 기동시각 없음
    )
    save_state(args.state, state)

    if action == "BREAK":
        # 플래그 생성 → 이후 watchdog은 침묵. 알림 본문은 쉘이 발송.
        with open(args.flag, "w", encoding="utf-8") as f:
            f.write(f"{reason}\n")
        with open(args.flag + ".alert", "w", encoding="utf-8") as f:
            f.write(build_alert(args.name, reason, signature, prev_signature, log_tail, args.flag))
    print(action)
    return 0


def cmd_on_start(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    state["last_start_ts"] = time.time()
    save_state(args.state, state)
    print("OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="watchdog crash-loop guard")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="사망 기록 + 차단 판정 (RESTART|BREAK 출력)")
    p_check.add_argument("--name", required=True)
    p_check.add_argument("--state", required=True)
    p_check.add_argument("--flag", required=True)
    p_check.add_argument("--log", default="")
    p_check.add_argument("--boot-grace", type=float, default=BOOT_GRACE_SEC)
    p_check.set_defaults(func=cmd_check)

    p_start = sub.add_parser("on-start", help="재기동 성공 시각 기록")
    p_start.add_argument("--state", required=True)
    p_start.set_defaults(func=cmd_on_start)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
