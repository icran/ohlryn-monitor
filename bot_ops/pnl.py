"""계좌 수익률 최고/최저 기록 추적 — 순수 로직 (I/O 없음).

Cayenne `UTIL/account_telegram_report.py`의 로직 이식:
    수익률 = (현재 equity − initial) / initial × 100
    계좌별 {worst, best}를 상태에 영속, **최초/최저 갱신(🙏)/최고 갱신(🚀) 때만** 발송.
    갱신 없으면 침묵 (health_check와 동일한 '침묵=정상' 철학).
"""

from __future__ import annotations


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


_STATUS_ICON = {"first": "(최초)", "worst": "🙏", "best": "🚀", "worst/best": "🙏/🚀"}


def build_summary_message(prefix: str, kst_time: str, rows: list[dict]) -> str:
    """전 계좌 요약 메시지 (순수). rows: [{name, rate|None, status, error|None}].

    rate 앞 아이콘: ≥+10% 🟢 / ≤−10% 🔴 (Cayenne 규칙 유지).
    """
    lines = [f"{prefix} [{kst_time} 기준]"]
    for r in rows:
        if r.get("rate") is None:
            lines.append(f"{r['name']:<14}{r.get('error', '조회실패'):>10}")
            continue
        rate = r["rate"]
        icon = "🟢 " if rate >= 10 else ("🔴 " if rate <= -10 else "")
        status = _STATUS_ICON.get(r.get("status", ""), "")
        lines.append(f"{r['name']:<14}{icon}{rate:>7.2f}%  {status}")
    return "\n".join(lines)


def should_send(rows: list[dict]) -> bool:
    """한 계좌라도 기록 갱신(status 비어있지 않음)이면 발송."""
    return any(r.get("status") for r in rows if r.get("rate") is not None)
