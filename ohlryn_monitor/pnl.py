"""계좌 수익률 최고/최저 기록 추적 — 순수 로직 (I/O 없음).

규칙:
    수익률 = (현재 equity − initial) / initial × 100
    계좌별 {worst, best}를 상태에 영속, **최초/최저 갱신(🙏)/최고 갱신(🚀) 때만** 발송.
    갱신 없으면 침묵 (health_check와 동일한 '침묵=정상' 철학).
"""

from __future__ import annotations

import math
from datetime import date

# 기록 갱신 알림의 기본 민감도(%p). 0.01%p마다 울리면 알림이 잦아 몰입을 방해하므로,
# **정수 % 경계를 넘을 때만** 보낸다(-8.26% → -9% 통과 시). config로 조절 가능.
DEFAULT_ALERT_STEP = 1.0


def days_since(start_date: str, today: date) -> int:
    """기록 시작일부터 몇 일째인지 (시작일 = 1일째). 미래 시작일이면 1로 방어."""
    d = (today - date.fromisoformat(start_date)).days + 1
    return max(d, 1)


def profit_rate(current: float, initial: float) -> float:
    """수익률(%) — 소수점 2자리 반올림."""
    return round((current - initial) / initial * 100, 2)


def net_transfers(transfers: list[dict] | None) -> float:
    """외부 이체 순합 (입금 +, 출금 −).

    거래 손익이 아닌 자금 이동은 equity를 흔들어 수익률로 오인된다 — 계좌 config의
    transfers 내역 합을 equity에서 차감해 보정한다 (출금 −1000 → equity에 +1000 복원).
    """
    return float(sum(float(t.get("amount", 0)) for t in transfers or []))


def _next_down(value: float, step: float) -> float:
    """value보다 **낮은** 첫 step 경계. value가 이미 경계면 한 칸 더 내려간다."""
    t = math.floor(value / step) * step
    return t if t < value - 1e-9 else t - step


def _next_up(value: float, step: float) -> float:
    """value보다 **높은** 첫 step 경계. value가 이미 경계면 한 칸 더 올라간다."""
    t = math.ceil(value / step) * step
    return t if t > value + 1e-9 else t + step


def update_record(
    record: dict | None, rate: float, *, step: float = DEFAULT_ALERT_STEP
) -> tuple[dict, str]:
    """계좌 1개의 기록 갱신 (순수).

    반환: (새 record, status) — status ∈ {"first", "worst", "best", "worst/best", ""}
    "" = 갱신 없음(침묵 대상). worst/best 동시 갱신은 최초 이후엔 불가능하지만 방어.

    **기록과 알림을 분리한다**: 기록(worst/best)은 미세 변동도 항상 갱신해 실제 최고·최저를
    잃지 않지만, 알림은 `step`(%p) 경계를 넘을 때만 낸다. 0.01%p마다 울리면 알림이 잦아
    오히려 무시하게 되기 때문. 경계를 기록값 기준으로 재계산하므로 -8.9%↔-9.1% 진동에도
    반복 발송되지 않는다.
    """
    if not record or (record.get("worst") is None and record.get("best") is None):
        return {"worst": rate, "best": rate}, "first"

    new = dict(record)
    parts = []
    if record.get("worst") is not None:
        worst = float(record["worst"])
        if rate < worst:
            new["worst"] = rate
            if rate <= _next_down(worst, step):
                parts.append("worst")
    if record.get("best") is not None:
        best = float(record["best"])
        if rate > best:
            new["best"] = rate
            if rate >= _next_up(best, step):
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
