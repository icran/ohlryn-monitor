"""watchdog_guard 순수 판정 로직 테스트.

규칙:
  1) 오래 돌다 사망 1회 → RESTART
  2) 부팅 후 boot_grace(120s) 내 사망 연속 2회 → BREAK (같은 조건 사망 대리 지표)
  3) 종류 무관 window(30분) 내 5회 사망 → BREAK (백스톱)
     — 수동 재시작도 사망으로 세므로, 설정 수정 한 회차(최대 4회)는 통과해야 한다
  4) 차단 후엔 플래그 파일 — 쉘이 담당 (여기선 판정만)
"""

from ohlryn_monitor.watchdog_guard import (
    classify_death,
    decide,
    extract_crash_signature,
    build_alert,
)

T0 = 1_000_000.0  # 기준 epoch


def rec(ts, kind):
    return {"ts": ts, "kind": kind}


# ── classify_death: 생존시간으로 부팅실패/일반사망 분류 ──────────────


def test_boot_failure_within_grace():
    # 기동 45초 만에 죽으면 부팅실패
    assert classify_death(start_ts=T0, death_ts=T0 + 45, boot_grace=120) == "boot_failure"


def test_runtime_death_after_grace():
    # 3시간 돌다 죽으면 일반 사망
    assert classify_death(start_ts=T0, death_ts=T0 + 3 * 3600, boot_grace=120) == "runtime_death"


def test_unknown_start_treated_as_runtime():
    # 기동 시각 기록이 없으면(구버전 상태파일 등) 보수적으로 일반 사망 취급
    assert classify_death(start_ts=None, death_ts=T0, boot_grace=120) == "runtime_death"


# ── decide: 차단 판정 ────────────────────────────────────────────────


def test_first_runtime_death_restarts():
    # 규칙 1: 오래 돌다 처음 죽음 → 재기동
    action, reason = decide(history=[rec(T0, "runtime_death")], now=T0)
    assert action == "RESTART"


def test_single_boot_failure_restarts():
    # 부팅실패도 1회면 한 번 더 기회
    action, _ = decide(history=[rec(T0, "boot_failure")], now=T0)
    assert action == "RESTART"


def test_two_consecutive_boot_failures_break():
    # 규칙 2: 부팅실패 연속 2회 → 즉시 차단
    history = [rec(T0, "boot_failure"), rec(T0 + 300, "boot_failure")]
    action, reason = decide(history=history, now=T0 + 300)
    assert action == "BREAK"
    assert "부팅실패" in reason


def test_boot_failure_streak_reset_by_runtime_death():
    # 부팅실패 1회 → 오래 돌다 사망 → 부팅실패 1회: 연속이 아니므로 재기동
    history = [
        rec(T0, "boot_failure"),
        rec(T0 + 7200, "runtime_death"),
        rec(T0 + 7500, "boot_failure"),
    ]
    action, _ = decide(history=history, now=T0 + 7500)
    # 단, 30분 백스톱에도 안 걸려야 함 (7200s 간격이라 윈도 밖)
    assert action == "RESTART"


def test_five_deaths_in_window_break():
    # 규칙 3: 30분 내 5회 (종류 무관) → 차단
    history = [rec(T0 + 300 * i, "runtime_death") for i in range(5)]
    action, reason = decide(history=history, now=T0 + 1200)
    assert action == "BREAK"
    assert "5회" in reason


def test_four_deaths_in_window_restart():
    # 설정 수정 한 회차에 4회까지 수동 재시작하는 일이 있다 — 차단되면 안 된다.
    history = [rec(T0 + 300 * i, "runtime_death") for i in range(4)]
    action, _ = decide(history=history, now=T0 + 900)
    assert action == "RESTART"


def test_five_deaths_spread_out_restart():
    # 30분 밖으로 흩어진 5회는 정상 재기동
    history = [
        rec(T0, "runtime_death"),
        rec(T0 + 4000, "runtime_death"),
        rec(T0 + 8000, "runtime_death"),
        rec(T0 + 12000, "runtime_death"),
        rec(T0 + 16000, "runtime_death"),
    ]
    action, _ = decide(history=history, now=T0 + 16000)
    assert action == "RESTART"


def test_old_history_ignored_for_boot_streak():
    # 어제 부팅실패 1회 + 오늘 부팅실패 1회 = 연속 2회로 치지 않음 (streak_window 기본 1h)
    history = [rec(T0, "boot_failure"), rec(T0 + 86400, "boot_failure")]
    action, _ = decide(history=history, now=T0 + 86400)
    assert action == "RESTART"


# ── extract_crash_signature: 로그 꼬리에서 예외 줄 추출 ──────────────


def test_extract_signature_from_traceback():
    tail = (
        "[11:34:39] [INFO] boot...\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 88, in setup\n'
        "TypeError: Cannot convert tz-naive timestamp\n"
        "[11:34:41] [ERROR] shutdown\n"
    )
    assert extract_crash_signature(tail) == "TypeError: Cannot convert tz-naive timestamp"


def test_extract_signature_none_when_no_exception():
    assert extract_crash_signature("[11:00] [INFO] all fine\n") is None


# ── build_alert: 알림 본문 (동일 에러 표시 + 로그 첨부 + 트림) ───────


def test_alert_contains_reason_recovery_and_log():
    body = build_alert(
        name="natas-sub",
        reason="부팅실패 연속 2회",
        signature="TypeError: x",
        prev_signature="TypeError: x",
        log_tail="line1\nline2",
        flag_path="/home/u/wd_state/natas-sub.crashloop",
    )
    assert "natas-sub" in body and "부팅실패 연속 2회" in body
    assert "동일 에러: ✅" in body  # 직전과 같은 시그니처
    assert "natas-sub.crashloop" in body  # 복구 방법
    assert "line1" in body


def test_alert_marks_different_signature():
    body = build_alert(
        name="b", reason="r", signature="A: x", prev_signature="B: y",
        log_tail="", flag_path="/f",
    )
    assert "동일 에러: ❌" in body


def test_alert_trimmed_to_telegram_limit():
    body = build_alert(
        name="b", reason="r", signature=None, prev_signature=None,
        log_tail="x" * 10000, flag_path="/f",
    )
    assert len(body) <= 3600
