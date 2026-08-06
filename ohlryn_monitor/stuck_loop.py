"""봇이 조용히 멈춘 상태 감지 — 같은 경고가 짧은 시간에 반복되는지 본다 (순수 로직).

배경(2026-08-04, server1 :8014 양변기 SOXL): 부분 청산 후 잔량에 부동소수점 먼지
(2.22e-16)가 남아 포지션이 닫히지 않았다. 엔진이 매 tick 청산을 재시도했고 최소주문량
미달로 거부되어 **그 레그의 신규 진입이 영구 차단**됐다. 돈은 안 잃었지만 전략이 죽었고
흔적이 로그에만 남아 아무도 몰랐다.

이 모듈은 특정 버그가 아니라 **"같은 경고가 N회/M분 반복 = 루프에 갇힘"** 이라는 계열을
잡는다. 잔차 soft-kill(주문 차단·포지션 방치)이나 주문 반복 실패도 같은 형태다.

I/O(파일 읽기·텔레그램·상태 저장)는 alerters/stuck_loop_watch.py 가 담당한다.
"""

from datetime import datetime, timedelta, timezone
from typing import Iterable

# `[2026-08-04 13:32:40,847] [WARNING] ...` 형태의 선두 타임스탬프
_TS_LEN = len("[2026-08-04 13:32:40")


def parse_log_time(line: str) -> datetime | None:
    """로그 줄 선두의 `[YYYY-MM-DD HH:MM:SS...]` 를 UTC datetime 으로. 실패 시 None."""
    if len(line) < _TS_LEN or not line.startswith("["):
        return None
    try:
        return datetime.strptime(line[1:_TS_LEN], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def count_recent_matches(lines: Iterable[str], pattern: str, since: datetime) -> int:
    """`since` 이후 시각을 가진 줄 중 `pattern` 을 포함하는 개수.

    ⚠ 타임스탬프가 없는 줄은 패턴이 맞아도 제외한다 — 시각을 모르면 윈도우 판정을 할 수
      없고, 오래된 줄을 최근으로 오인하면 거짓 경보가 된다.
    """
    n = 0
    for line in lines:
        if pattern not in line:
            continue
        ts = parse_log_time(line)
        if ts is not None and ts >= since:
            n += 1
    return n


def plan_stuck_alerts(
    watches: list[dict],
    now: datetime,
    already_sent: dict,
    cooldown_minutes: int = 360,
) -> tuple[list[str], dict]:
    """감시 결과 → (보낼 메시지, 갱신된 발송 상태).

    watches 항목: name/label/pattern/count/threshold/window_minutes/action
    - count >= threshold 이고 쿨다운 밖이면 🚨 발송
    - 이전에 알렸는데 count == 0 이면 ✅ 해소 발송 후 상태 제거
    - 쿨다운 안이면 침묵하되 **최초 발송 시각을 갱신하지 않는다** (계속 밀리면 영영 재알림 없음)
    """
    msgs: list[str] = []
    sent = dict(already_sent)

    for w in watches:
        name = w["name"]
        count = w["count"]
        prev_iso = sent.get(name)

        if count >= w["threshold"]:
            if prev_iso is not None:
                prev = datetime.fromisoformat(prev_iso)
                if now - prev < timedelta(minutes=cooldown_minutes):
                    continue  # 쿨다운 중 — 상태 유지
            action = f"\n   조치: {w['action']}" if w.get("action") else ""
            msgs.append(
                f"🚨 봇이 같은 경고를 반복 중 — {w['label']}\n"
                f"   '{w['pattern']}' {count}회 / 최근 {w['window_minutes']}분 (임계 {w['threshold']})\n"
                f"   해당 레그가 루프에 갇혀 신규 진입을 못 하고 있을 수 있다.{action}"
            )
            sent[name] = now.isoformat()
        elif count == 0 and prev_iso is not None:
            msgs.append(f"✅ 반복 경고 해소 — {w['label']} ('{w['pattern']}' 최근 {w['window_minutes']}분 0회)")
            sent.pop(name, None)

    return msgs, sent
