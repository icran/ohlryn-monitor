"""서버 헬스체크 판정 — 순수 로직 (I/O 없음).

봇 좀비·시스템 리소스·로그 에러를 판정하고, 쿨다운/회복 알림을 계산한다.
I/O(HTTP·/proc·텔레그램·상태파일)는 alerters/health_check.py 어댑터가 담당.

판정 철학: 침묵 = 정상. 같은 문제는 쿨다운 내 재알림 억제, 해소 시 회복 알림 1회.
"""

from __future__ import annotations

from datetime import datetime, timezone

Issue = tuple[str, str]  # (dedup_key, message)


def bot_issues(
    name: str,
    engines: list[dict] | None,
    *,
    now: datetime,
    stale_minutes: int,
    error: str | None = None,
) -> list[Issue]:
    """봇 1개의 엔진 목록 판정.

    - engines=None(+error) → API 응답 없음
    - is_running=False 엔진 존재 → 중지
    - last_updated가 stale_minutes보다 오래됨 → 좀비 의심
      (주의: last_updated는 캔들 '오픈시각' 라벨 — 15m봉은 최대 ~30분 자연 지연.
       임계는 자연지연 + 버퍼로 잡아야 함. 기본 40분)
    """
    if engines is None:
        return [(f"{name}:api", f"CRITICAL {name}: API 응답 없음 ({error or 'unknown'})")]
    if not engines:
        return [(f"{name}:empty", f"CRITICAL {name}: 엔진 목록 비어있음")]

    stopped, stale = [], []
    for e in engines:
        pair = e.get("pair", "?")
        if not e.get("is_running"):
            stopped.append(pair)
        lu = e.get("last_updated")
        if lu:
            try:
                dt = datetime.fromisoformat(str(lu).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now - dt).total_seconds() > stale_minutes * 60:
                    stale.append(pair)
            except ValueError:
                pass

    issues: list[Issue] = []
    if stopped:
        issues.append((f"{name}:stopped", f"CRITICAL {name}: 엔진 중지됨 {stopped}"))
    if stale:
        issues.append(
            (f"{name}:stale", f"CRITICAL {name}: {stale_minutes}분+ 데이터 정체(좀비 의심) {stale}")
        )
    return issues


def system_issues(metrics: dict, limits: dict) -> list[Issue]:
    """시스템 리소스 판정. metrics: disk_pct/mem_avail_mb/swap_used_mb/load1."""
    issues: list[Issue] = []
    if metrics.get("disk_pct", 0) > limits.get("disk_pct_max", 85):
        issues.append(("sys:disk", f"WARNING 디스크 {metrics['disk_pct']:.0f}% 사용"))
    if metrics.get("mem_avail_mb", 1 << 30) < limits.get("mem_avail_mb_min", 400):
        issues.append(("sys:mem", f"CRITICAL 메모리 잔여 {metrics['mem_avail_mb']}MB"))
    if metrics.get("swap_used_mb", 0) > limits.get("swap_used_mb_max", 2048):
        issues.append(("sys:swap", f"WARNING 스왑 {metrics['swap_used_mb']}MB 사용 (메모리 압박)"))
    if metrics.get("load1", 0) > limits.get("load1_max", 3.0):
        issues.append(("sys:load", f"WARNING load {metrics['load1']:.2f}"))
    return issues


def count_log_errors(chunk: str) -> int:
    """신규 로그 청크에서 에러 라인 수 집계."""
    return sum(1 for ln in chunk.splitlines() if "ERROR" in ln or "Traceback" in ln)


def log_issue(name: str, n_errors: int, threshold: int) -> list[Issue]:
    if n_errors >= threshold:
        return [(f"{name}:logerr", f"WARNING {name}: 신규 에러 로그 {n_errors}건")]
    return []


def next_log_offset(prev_offset: int | None, size: int) -> tuple[int, int]:
    """로그 오프셋 전이. 반환: (읽기 시작 위치, 새 오프셋).

    - 최초 실행(prev None): 과거 미소급 — 현재 크기부터 시작
    - size < prev: 로테이션/재생성 → 처음부터
    """
    if prev_offset is None:
        return size, size
    if size < prev_offset:
        return 0, size
    return prev_offset, size


def plan_alerts(
    active: dict, issues: list[Issue], *, now_ts: float, cooldown_sec: float
) -> tuple[list[str], dict]:
    """알림 계획 (순수). 반환: (보낼 메시지들, 갱신된 active 상태).

    - 신규 문제 or 쿨다운 경과 → 🚨 발송
    - 사라진 문제 → ✅ 회복 발송
    """
    to_send: list[str] = []
    new_active = {k: dict(v) for k, v in active.items()}
    current = {k for k, _ in issues}

    for key, msg in issues:
        rec = new_active.get(key)
        if rec is None or now_ts - rec.get("last_sent", 0) > cooldown_sec:
            to_send.append(f"🚨 {msg}")
            new_active[key] = {"first": rec["first"] if rec else now_ts, "last_sent": now_ts}

    for key in list(new_active):
        if key not in current:
            dur_min = (now_ts - new_active[key]["first"]) / 60
            to_send.append(f"✅ 해소: {key} (지속 {dur_min:.0f}분)")
            del new_active[key]

    return to_send, new_active


def has_critical(issues: list[Issue]) -> bool:
    return any("CRITICAL" in msg for _, msg in issues)
