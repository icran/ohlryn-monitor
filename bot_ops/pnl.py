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


_STATUS_ICON = {"first": "✨ 최초", "worst": "🙏 최저", "best": "🚀 최고", "worst/best": "🙏🚀"}


def build_summary_message(prefix: str, kst_time: str, rows: list[dict]) -> str:
    """전 계좌 요약 메시지 (순수, 텔레그램 HTML — <pre> 고정폭 표).

    rows: [{name, rate|None, status, record{worst,best}|None, error|None}]
    제목 아이콘: 갱신 종류에 따라 🚀(최고)/🙏(최저)/✨(최초). rate ≥+10% 🟢 / ≤−10% 🔴.
    """
    statuses = {r.get("status") for r in rows if r.get("rate") is not None}
    head_icon = "🚀" if any(s and "best" in s for s in statuses) else (
        "🙏" if any(s and "worst" in s for s in statuses) else "✨"
    )
    lines = [
        f"{head_icon} <b>{prefix} 수익률 기록 갱신</b>",
        f"🕘 {kst_time} KST",
        "",
    ]
    body = []
    for r in rows:
        if r.get("rate") is None:
            body.append(f"{r['name']:<13} 조회실패({r.get('error', '?')})")
            continue
        rate = r["rate"]
        mood = "🟢" if rate >= 10 else ("🔴" if rate <= -10 else "  ")
        status = _STATUS_ICON.get(r.get("status", ""), "")
        body.append(f"{r['name']:<13}{mood}{rate:>+8.2f}%  {status}")
    lines.append("<pre>" + "\n".join(body) + "</pre>")
    return "\n".join(lines)


def should_send(rows: list[dict]) -> bool:
    """한 계좌라도 기록 갱신(status 비어있지 않음)이면 발송."""
    return any(r.get("status") for r in rows if r.get("rate") is not None)
