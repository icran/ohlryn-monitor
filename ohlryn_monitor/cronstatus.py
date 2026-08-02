"""[순수] crontab 기반 작업 상태 판정 — status UI의 두뇌.

철학: 아무것도 새로 기록하게 하지 않는다. crontab 한 줄에 이미
이름(`# 태그` 주석)·주기(5필드)·로그 경로(`>> path`)가 있으므로,
이를 파싱해 로그 신선도로 "잘 돌고 있는가"를 판정한다 (알리미 무수정).

한계: watchdog류는 이벤트 발생 시에만 로그를 남긴다(침묵=정상) —
config에서 event_driven으로 표시해 신선도 판정에서 제외한다.
"""

import re
from datetime import datetime, timedelta

_LOG_RE = re.compile(r">>\s*(\S+)")
_TAG_RE = re.compile(r"#\s*(\S+)\s*$")
_ERROR_RE = re.compile(r"Traceback|ERROR|CRITICAL|FAILED", re.IGNORECASE)

# 신선도 임계 = 기대 최대 간격 × 배수 + 여유 (실행 지연·시계 오차 흡수)
STALE_FACTOR = 1.6
STALE_MARGIN_MIN = 5


# ── crontab 파싱 ─────────────────────────────────────────────────────


def parse_crontab(text: str) -> list[dict]:
    """crontab 텍스트 → [{name, schedule, command, log}].

    주석 줄·환경변수 줄(KEY=...)·@reboot 류는 건너뛴다.
    name: 줄 끝 `# 태그` 주석. 없으면 커맨드 첫 토큰의 basename.
    """
    jobs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("@"):
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line):
            continue  # MAILTO= 등 환경변수
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        schedule = " ".join(parts[:5])
        command = parts[5]
        m = _TAG_RE.search(command)
        if m:
            name = m.group(1)
            command = command[: m.start()].rstrip()
        else:
            first = command.split()[0]
            name = first.rsplit("/", 1)[-1]
        log_m = _LOG_RE.search(command)
        jobs.append(
            {
                "name": name,
                "schedule": schedule,
                "command": command,
                "log": log_m.group(1) if log_m else None,
            }
        )
    return jobs


# ── 스케줄 → 기대 최대 간격 ──────────────────────────────────────────


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    vals: set[int] = set()
    for token in field.split(","):
        step = 1
        if "/" in token:
            token, step_s = token.split("/", 1)
            step = int(step_s)
        if token == "*":
            start, end = lo, hi
        elif "-" in token:
            a, b = token.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(token)
        vals.update(range(start, end + 1, step))
    return vals


def max_gap_minutes(schedule: str, sim_days: int = 60) -> int:
    """cron 5필드 스케줄의 실행 간 최대 간격(분). 실제 달력으로 시뮬레이션.

    dom/dow가 모두 제한되면 vixie cron 규약대로 OR로 매칭한다.
    """
    f = schedule.split()
    minutes = _parse_field(f[0], 0, 59)
    hours = _parse_field(f[1], 0, 23)
    doms = _parse_field(f[2], 1, 31)
    months = _parse_field(f[3], 1, 12)
    dows = _parse_field(f[4], 0, 7)
    if 7 in dows:  # 7 == 0 == 일요일
        dows.add(0)
    dom_any = f[2] == "*"
    dow_any = f[4] == "*"

    t = datetime(2026, 1, 1)
    end = t + timedelta(days=sim_days)
    prev = None
    max_gap = 0
    while t < end:
        if t.minute in minutes and t.hour in hours and t.month in months:
            dom_ok = t.day in doms
            dow_ok = (t.weekday() + 1) % 7 in dows  # weekday(): 월=0 → cron 일=0
            matched = (dom_ok or dow_ok) if (not dom_any and not dow_any) else (
                (dom_ok if not dom_any else True) and (dow_ok if not dow_any else True)
            )
            if matched:
                if prev is not None:
                    max_gap = max(max_gap, int((t - prev).total_seconds() // 60))
                prev = t
        t += timedelta(minutes=1)
    return max_gap or sim_days * 1440


# ── 상태 판정 ────────────────────────────────────────────────────────


def judge(
    now: datetime,
    log_mtime: datetime | None,
    gap_min: int,
    log_tail: str,
    event_driven: bool = False,
) -> dict:
    """작업 하나의 상태. Returns {status, detail}.

    status: ok(🟢) | error(🔴 로그에 에러) | stale(🔴 cron 미실행 의심)
            | event(⚪ 이벤트형 — 침묵=정상) | unknown(로그 없음)
    """
    tail_lines = [ln for ln in log_tail.strip().splitlines() if ln.strip()]
    last_line = tail_lines[-1] if tail_lines else ""

    if event_driven:
        age = _age_str(now, log_mtime)
        return {"status": "event", "detail": f"마지막 이벤트 {age}" + (f" — {last_line}" if last_line else "")}

    if log_mtime is None:
        return {"status": "unknown", "detail": "로그 없음 (아직 첫 실행 전?)"}

    err = next((ln for ln in reversed(tail_lines) if _ERROR_RE.search(ln)), None)
    # 에러 판정은 "마지막 실행 블록"에 한정: 마지막 줄이 정상 요약이면 이전 에러는 지나간 것
    if err and (not last_line or _ERROR_RE.search(last_line) or err == last_line):
        return {"status": "error", "detail": err.strip()[:160]}

    age_min = (now - log_mtime).total_seconds() / 60
    if age_min > gap_min * STALE_FACTOR + STALE_MARGIN_MIN:
        return {"status": "stale", "detail": f"로그 갱신 {_age_str(now, log_mtime)} 전 — cron 미실행 의심 (기대 주기 ~{gap_min}분)"}

    return {"status": "ok", "detail": last_line[:160]}


def _age_str(now: datetime, ts: datetime | None) -> str:
    if ts is None:
        return "기록 없음"
    s = int((now - ts).total_seconds())
    if s < 3600:
        return f"{s // 60}분"
    if s < 86400:
        return f"{s // 3600}시간"
    return f"{s // 86400}일"
