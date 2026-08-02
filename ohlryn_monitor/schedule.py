"""[순수] 파라미터 스케줄 만료 감시 판정.

배경(2026-07-31 사고): 봇의 기간별 파라미터 스케줄(CSV)이 조용히 만료된 채
재시작되면 파라미터가 코드 기본값으로 회귀해(방어 필터 OFF 등) 의도치 않은
거래가 발생한다. 만료 D-N 경고와 만료 CRITICAL로 "아무도 모르는 만료"를 막는다.

CSV 계약: `end` 컬럼(ISO8601)을 가진 스케줄 파일. 마지막(최대) end = 스케줄 수명.
"""

import csv
from datetime import datetime, timezone

SentEntry = list  # [end_iso, stage] — JSON 상태 파일에 그대로 저장되는 형태


def last_end_from_csv(path: str) -> datetime | None:
    """스케줄 CSV의 최대 `end` 시각. 파일이 없거나 파싱 불가면 None."""
    try:
        with open(path, newline="") as f:
            ends = []
            for row in csv.DictReader(f):
                raw = (row.get("end") or "").strip()
                if raw:
                    ends.append(datetime.fromisoformat(raw))
        if not ends:
            return None
        end = max(ends)
        return end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    except (OSError, ValueError):
        return None


def plan_schedule_alerts(
    name: str,
    end: datetime | None,
    now: datetime,
    *,
    warn_days: tuple = (7, 3),
    already_sent: list,
) -> tuple[list[str], list]:
    """만료 경고 계획.

    단계: 만료 전 warn_days 임계(내림차순 권장) 각 1회 + 만료 후 CRITICAL 1회.
    already_sent: [[end_iso, stage], ...] — 같은 (end, stage)는 재발송하지 않는다.
    스케줄이 갱신되어 end가 바뀌면 이력이 자연 무효화된다.

    Returns:
        (보낼 메시지들, 갱신된 already_sent)
    """
    sent = [list(x) for x in already_sent]
    msgs: list[str] = []

    if end is None:
        key = ["missing", "missing"]
        if key not in sent:
            msgs.append(f"🚨 CRITICAL 스케줄 감시 실패: {name} — CSV를 읽을 수 없음 (경로/포맷 확인)")
            sent.append(key)
        return msgs, sent

    end_iso = end.isoformat()
    days_left = (end - now).total_seconds() / 86400

    if days_left <= 0:
        key = [end_iso, "expired"]
        if key not in sent:
            msgs.append(
                f"🚨 CRITICAL 스케줄 만료: {name} — {end_iso} 이후 무스케줄. "
                f"만료 후 봇 재시작 시 파라미터가 기본값으로 회귀함(방어 필터 OFF). "
                f"wfa.update로 스케줄을 연장할 것"
            )
            sent.append(key)
        return msgs, sent

    for threshold in sorted(warn_days):
        if days_left <= threshold:
            key = [end_iso, f"d{threshold}"]
            if key not in sent:
                msgs.append(
                    f"⚠️ WARNING 스케줄 만료 임박: {name} — D-{int(days_left)} ({end_iso} 만료). "
                    f"만료 전에 wfa.update 실행 필요"
                )
                sent.append(key)
            break  # 가장 타이트한 단계 하나만
    return msgs, sent
