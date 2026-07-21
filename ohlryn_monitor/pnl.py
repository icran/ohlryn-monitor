"""계좌 수익률 최고/최저 기록 추적 — 순수 로직 (I/O 없음).

규칙:
    수익률 = (현재 equity − initial) / initial × 100
    계좌별 {worst, best}를 상태에 영속, **최초/최저 갱신(🙏)/최고 갱신(🚀) 때만** 발송.
    갱신 없으면 침묵 (health_check와 동일한 '침묵=정상' 철학).
"""

from __future__ import annotations

from datetime import date


def days_since(start_date: str, today: date) -> int:
    """기록 시작일부터 몇 일째인지 (시작일 = 1일째). 미래 시작일이면 1로 방어."""
    d = (today - date.fromisoformat(start_date)).days + 1
    return max(d, 1)


def profit_rate(current: float, initial: float) -> float:
    """수익률(%) — 소수점 2자리 반올림."""
    return round((current - initial) / initial * 100, 2)


def update_record(record: dict | None, rate: float) -> tuple[dict, str]:
    """계좌 1개의 기록 갱신 (순수).

    반환: (새 record, status) — status ∈ {"first", "worst", "best", "worst/best", ""}
    "" = 갱신 없음(침묵 대상). worst/best 동시 갱신은 최초 이후엔 불가능하지만 방어.
    """
    if not record or (record.get("worst") is None and record.get("best") is None):
        return {"worst": rate, "best": rate}, "first"

    new = dict(record)
    parts = []
    if record.get("worst") is not None and rate < round(float(record["worst"]), 2):
        new["worst"] = rate
        parts.append("worst")
    if record.get("best") is not None and rate > round(float(record["best"]), 2):
        new["best"] = rate
        parts.append("best")
    return new, "/".join(parts)


_STATUS_ICON = {"first": "\u2728", "worst": "\U0001F64F", "best": "\U0001F680", "worst/best": "\U0001F64F\U0001F680"}


def build_summary_message(prefix: str, kst_time: str, rows: list[dict], *, day_n: int | None = None) -> str:
    """전 계좌 요약 메시지 (순수, 텔레그램 HTML).

    모바일 가독성 우선: <pre> 미사용(작은 글씨 방지), 제목은 중립(아이콘 없음),
    상태 아이콘(🚀 최고 / 🙏 최저 / ✨ 최초)은 해당 계좌 행 끝에만 붙는다.
    """
    lines = [
        f"<b>{prefix} 수익률 기록 갱신</b>",
        f"{kst_time} KST" + (f" · {day_n}일째" if day_n else ""),
        "",
    ]
    for r in rows:
        if r.get("rate") is None:
            lines.append(f"{r['name']}  조회실패({r.get('error', '?')})")
            continue
        icon = _STATUS_ICON.get(r.get("status", ""), "")
        lines.append(f"{r['name']}  {r['rate']:+.2f}%" + (f"  {icon}" if icon else ""))
    return "\n".join(lines)


def should_send(rows: list[dict]) -> bool:
    """한 계좌라도 기록 갱신(status 비어있지 않음)이면 발송."""
    return any(r.get("status") for r in rows if r.get("rate") is not None)
