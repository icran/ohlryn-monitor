"""전략×티커별 낙폭 — 라이브를 백테스트 기준선에 이어 붙인다 (순수 로직).

목적: "라이브가 백테스트가 본 적 없는 영역에 들어갔는가"를 보는 것. 자동 청산이 아니라
**사람이 교체 여부를 판단하기 위한 트리거**다.

⚠ 설계의 핵심 — **앵커**. 라이브 곡선을 1.0(낙폭 0%)에서 시작하면 안 된다. 전략은 연속된
   하나의 과정이라, 라이브 시작 시점에 이미 어느 낙폭에 있었는지를 이어받아야 한다.
   실측: #29 양변기는 2026-07-28 시작 시점에 이미 -29.86% 낙폭(기준 MDD -42.49%의 70%)
   이었다. 0%로 놓으면 -23%를 더 잃어도 "-23% / -42% = 54%, 여유"로 보이지만 실제로는
   역대 최대 낙폭이다. 정반대 판단을 하게 된다.

⚠ 단위 — **원가 대비 사이클 수익률 복리 곡선**을 쓴다. 라이브 DB 에는 레그 자본(유휴 현금
   포함)이 없고 거래별 pnl·원가만 있으므로, 기준선도 같은 단위(reference_mdd.json 의
   `cycle` 블록)로 생성해 비교한다. 유휴 현금이 빠져 낙폭이 레그 equity 기준보다 깊다.

계좌 변형 접미사(_main/_pcopy_half/_sm_q/_w25_10s ...)는 무시하고 (전략, 티커)로 묶는다.
"""

from __future__ import annotations


def group_key(strategy_id: str, pair: str, prefix_map: dict[str, str]) -> tuple[str, str] | None:
    """strategy_id + pair → (전략, 티커). 알 수 없는 전략이면 None.

    계좌 변형은 접미사로만 나타나므로 접두사 매칭이면 충분하다 — config 에 계좌를
    나열할 필요가 없다.
    """
    for prefix, name in prefix_map.items():
        if strategy_id.startswith(prefix):
            return name, pair.split("/")[0]
    return None


def trade_returns(rows) -> list[tuple[str, float]]:
    """완료 거래 → [(청산일, 원가대비 수익률)] 청산일 순.

    원가는 **노셔널** = `max_stake_amount`(분할 진입 총합) × `leverage` 다.

    ⚠ 두 가지를 모두 틀리기 쉽다:
      - `stake_amount`(최초 1회분)를 쓰면 IBS 4분할에서 최대 4배 부풀려진다
      - `leverage` 를 빼면 lev 10 계좌의 수익률이 기준선(1x 가격 수익률)의 10배가 된다.
        stake 는 증거금이고 실제 원가는 stake × lev 이다.
    노셔널로 나눠야 계좌별 레버리지·비중과 무관하게 기준선과 같은 단위가 된다.
    """
    out = []
    for r in rows:
        if r.get("is_open"):
            continue
        exit_date = r.get("exit_date")
        if not exit_date:
            continue
        cost = (r.get("max_stake_amount") or r.get("stake_amount") or 0.0) * float(
            r.get("leverage") or 1)
        if not cost:
            continue
        out.append((str(exit_date), float(r.get("pnl") or 0.0) / float(cost)))
    return sorted(out, key=lambda x: x[0])


def live_curve(returns: list[tuple[str, float]], anchor: dict | None) -> dict:
    """앵커에서 이어받아 라이브 곡선을 굴린다.

    anchor 가 None 이면 1.0 에서 시작하되 `anchored=False` 로 표시한다 — 그 경우 낙폭은
    과소평가된 값이므로 화면에서 구분해야 한다.
    """
    if anchor:
        equity = float(anchor["equity"])
        peak = float(anchor["peak"])
        uw = int(anchor.get("uw_days") or 0)
        anchored = True
    else:
        equity = peak = 1.0
        uw = 0
        anchored = False

    uw_since = 0
    worst = None
    for _, ret in returns:
        equity *= 1 + ret
        if equity >= peak:
            peak = equity
            uw_since = 0
        else:
            uw_since += 1
        worst = ret if worst is None else min(worst, ret)

    return {
        "equity": equity,
        "peak": peak,
        "drawdown": equity / peak - 1 if peak else 0.0,
        "trades": len(returns),
        "uw_since_anchor": uw_since,
        "uw_total": uw + uw_since if anchored else uw_since,
        "worst_trade": worst,
        "anchored": anchored,
    }


def leg_status(returns: list[tuple[str, float]], anchor: dict | None,
               reference: dict | None) -> dict:
    """라이브 곡선 + 기준선 대조 결과.

    `ratio` = 현재 낙폭 / 기준 MDD. 1.0 을 넘으면 역대 최대 낙폭 갱신이다.
    """
    cur = live_curve(returns, anchor)
    ref_mdd = (reference or {}).get("mdd")
    ref_worst = (reference or {}).get("worst_trade")

    ratio = None
    mdd_renewed = False
    if ref_mdd:
        ratio = abs(cur["drawdown"] / ref_mdd)
        mdd_renewed = cur["drawdown"] < ref_mdd

    worst_renewed = bool(
        ref_worst is not None and cur["worst_trade"] is not None
        and cur["worst_trade"] < ref_worst
    )
    return {**cur, "ratio": ratio, "mdd_renewed": mdd_renewed,
            "worst_trade_renewed": worst_renewed,
            "ref_mdd": ref_mdd, "ref_worst_trade": ref_worst,
            "ref_max_uw": (reference or {}).get("max_uw_days")}


def status_level(st: dict) -> str:
    """신호등 — 표시 우선순위 판정."""
    if st.get("mdd_renewed") or st.get("worst_trade_renewed"):
        return "red"
    r = st.get("ratio")
    if r is None:
        return "none"
    if r >= 1.0:
        return "red"
    if r >= 0.7:
        return "orange"
    if r >= 0.5:
        return "yellow"
    return "green"


def returns_since(returns: list[tuple[str, float]], base_date: str | None) -> float:
    """기준일(포함) 이후 청산된 거래만 복리로 누적한 수익률.

    "우리 계좌가 이 날짜 이후 얼마 벌었나"를 재는 단순한 값이다. 낙폭 앵커처럼
    백테스트 이력을 물려받지 않으므로 그대로 체감 손익으로 읽으면 된다.
    """
    total = 1.0
    hit = False
    for d, ret in returns:
        if base_date and str(d)[:10] < base_date:
            continue
        total *= 1 + ret
        hit = True
    return total - 1 if hit else 0.0


def open_units(rows) -> int | None:
    """오픈 거래의 분할 진입 차수 — custom_data.units_used (없으면 1차로 간주).

    IBS 분할 전략 표시용. 오픈 거래가 없으면 None(미보유). 같은 레그에 오픈이
    여러 건이면(비정상) 최대값. 깨진 custom_data 는 1차로 방어.
    """
    import json

    units = None
    for r in rows:
        if not r.get("is_open"):
            continue
        try:
            u = int(json.loads(r.get("custom_data") or "{}").get("units_used", 1))
        except (ValueError, TypeError):
            u = 1
        units = u if units is None else max(units, u)
    return units


def bt_state_source(ref: dict) -> dict:
    """"지금 낙폭"/상태 배지용 곡선 선택 — mtm(미실현 포함)이 있으면 cycle 대신 쓴다.

    cycle(청산 시점 인덱스) 곡선은 포지션 보유 중 미실현 손실을 못 보고 마지막 청산
    시점에 멈춰 "신고점"으로 오표시된다. reference 가 mtm(일별 MTM equity)을 내보내면
    그걸 우선한다. 구 reference(mtm 없음)는 cycle 폴백 — 하위호환.
    성적(bt_since)·최근거래·라이브 앵커는 계속 cycle 단위다 (라이브 거래와 같은 단위).
    """
    return ref.get("mtm") or ref.get("cycle") or {}


def combine_legs(leg_returns: dict[str, float], weights: dict[str, float]) -> float | None:
    """레그 수익률 → 조합 수익률 (가중합). 거래 없는 레그는 0으로 친다.

    가중치는 백테스트 기준선과 같은 값을 쓴다 — IBS 는 mix×퍼프레버리지
    (SOXL 0.30 / AVGO 0.30×2 / TQQQ 0.40), 양변기는 SOXL·SOXS 각 0.5.
    전 레그가 비어 있으면 None (표시할 실적 없음).
    """
    if not any(t in leg_returns for t in weights):
        return None
    return sum(w * leg_returns.get(t, 0.0) for t, w in weights.items())
