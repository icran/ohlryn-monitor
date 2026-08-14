"""계좌 일별 수익률 곡선 — 순수 로직 (I/O 없음).

봇 /api/v1/trades 의 **청산 거래**에서 일별 실현손익을 뽑아 누적 수익률 곡선을 만든다.

왜 실현 기준인가: 과거 시점의 MTM(미실현 포함) equity는 어디에도 기록돼 있지 않아
복원할 수 없다(pnl_watch 로그는 타임스탬프가 없고 결손이 있음). 청산 거래는 봇 DB에
날짜와 함께 남아 있어 **결정적으로 재구성**되고, 외부 이체(입출금)와도 무관하다.
따라서 곡선 끝값은 텔레그램의 equity 기준 수익률과 미실현 손익만큼 다를 수 있다.
"""

from __future__ import annotations

from datetime import date, timedelta


def daily_net_pnl(trades: list[dict]) -> dict[str, float]:
    """청산 거래 → {"YYYY-MM-DD": Σ net_pnl}. 오픈/청산시각 없는 거래 제외.

    net_pnl(수수료·펀딩 차감)이 없으면 pnl로 폴백(구버전 봇 호환).
    """
    out: dict[str, float] = {}
    for t in trades:
        if t.get("is_open") or not t.get("exit_time"):
            continue
        d = str(t["exit_time"])[:10]
        v = t.get("net_pnl")
        if v is None:
            v = t.get("pnl") or 0.0
        out[d] = out.get(d, 0.0) + float(v)
    return out


def fill_curve(
    pnl_by_date: dict[str, float],
    initial: float,
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[tuple[str, float]]:
    """일별 pnl → 달력 채움 누적 수익률 곡선 [(날짜, 수익률 fraction)].

    - 거래 없는 날은 직전 누적값 유지 (연속 곡선)
    - 맨 앞에 시작 전날 0% 기준점 (차트가 0에서 출발)
    - start 이전 pnl은 시작 시점 누적에 반영 (곡선을 중간부터 그려도 값은 전체 누적)
    """
    if not pnl_by_date or not initial:
        return []
    d0 = date.fromisoformat(start or min(pnl_by_date))
    d1 = date.fromisoformat(end or max(pnl_by_date))
    cum = sum(v for k, v in pnl_by_date.items() if k < d0.isoformat())
    curve = [((d0 - timedelta(days=1)).isoformat(), round(cum / initial, 6))]
    d = d0
    while d <= d1:
        cum += pnl_by_date.get(d.isoformat(), 0.0)
        curve.append((d.isoformat(), round(cum / initial, 6)))
        d += timedelta(days=1)
    return curve


def combined_curve(
    pnl_maps: list[dict[str, float]],
    initials: list[float],
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[tuple[str, float]]:
    """전 계좌 합산 곡선 — Σ일별pnl / Σinitial.

    계좌마다 시작일이 달라도 거래 없는 계좌는 0 기여라 자연스럽다.
    (엄밀히는 나중에 투입된 계좌의 initial이 처음부터 분모에 들어가 초기 구간이
    소폭 희석되지만, 규모 대비 미미해 단순함을 택했다.)
    """
    merged: dict[str, float] = {}
    for m in pnl_maps:
        for k, v in m.items():
            merged[k] = merged.get(k, 0.0) + v
    return fill_curve(merged, sum(initials), start=start, end=end)
