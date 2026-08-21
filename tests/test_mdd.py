"""mdd 순수 로직 테스트 — 전략×티커별 라이브 낙폭을 백테스트 기준선에 이어 붙인다.

⚠ 핵심: 라이브 곡선을 1.0(낙폭 0%)에서 시작하면 안 된다. 전략은 연속된 하나의 과정이라
   라이브 시작 시점에 이미 어느 낙폭에 있었는지(앵커)를 이어받아야 한다.
   실제로 #29 양변기는 2026-07-28 시작 시점에 -29.86% 낙폭(기준 MDD -42.49%의 70%)이었다.
   0% 로 놓으면 "아직 여유"로 오독한다.

계좌마다 strategy_id 가 다르지만(_main/_sm_q/_w25_10s ...) 같은 전략·티커면 한 행으로
묶는다 — 계좌는 열로 나란히 둔다.
"""

import pytest

from ohlryn_monitor.mdd import (
    combine_legs,
    group_key,
    leg_status,
    live_curve,
    returns_since,
    trade_returns,
)

PREFIX = {"natas15_ibs": "IBS", "natas29": "YBG"}


# ── group_key: 계좌 변형 접미사를 무시하고 (전략, 티커)로 묶는다 ──────


def test_상태곡선은_mtm이_있으면_mtm을_쓴다():
    # "지금 낙폭" 배지는 미실현 포함(MTM) 곡선 우선 — cycle(청산 시점)만 보면
    # 보유 중 손실인데 신고점으로 오표시된다 (2026-08-21 IBS 3레그 지적)
    from ohlryn_monitor.mdd import bt_state_source
    mtm = {"mdd": -0.10, "anchors": {"2026-08-20": {"drawdown": -0.07}}}
    cyc = {"mdd": -0.18, "anchors": {"2026-07-30": {"drawdown": 0.0}}}
    assert bt_state_source({"mtm": mtm, "cycle": cyc}) is mtm
    assert bt_state_source({"cycle": cyc}) is cyc      # 구 reference 하위호환
    assert bt_state_source({}) == {}


def test_계좌_변형_접미사가_달라도_같은_키로_묶인다():
    ids = [
        "natas15_ibs_live_soxl_main",
        "natas15_ibs_live_soxl_pcopy_half",
        "natas15_ibs_live_soxl_sm_q",
        "natas15_ibs_live_soxl_w25_10s",
    ]
    keys = {group_key(i, "SOXL/USDT:USDT", PREFIX) for i in ids}
    assert keys == {("IBS", "SOXL")}


def test_전략이_다르면_다른_키다():
    assert group_key("natas29_live_soxl_main", "SOXL/USDT:USDT", PREFIX) == ("YBG", "SOXL")
    assert group_key("natas15_ibs_live_soxl_main", "SOXL/USDT:USDT", PREFIX) == ("IBS", "SOXL")


def test_알수없는_전략은_None():
    assert group_key("breakout_wfa_long", "BTC/USDT:USDT", PREFIX) is None


# ── trade_returns: 원가 대비 수익률 (백테스트 사이클 곡선과 같은 단위) ──


def test_원가는_max_stake_amount를_쓴다():
    """IBS 는 분할 진입이라 stake_amount(최초)가 아닌 max_stake_amount(총 원가)가 맞다."""
    rows = [{"exit_date": "2026-07-30", "pnl": 10.0, "stake_amount": 50.0,
             "max_stake_amount": 100.0, "leverage": 1, "is_open": 0}]
    assert trade_returns(rows) == [("2026-07-30", 0.10)]


def test_원가는_레버리지를_곱한_노셔널이다():
    """stake 는 증거금이다. lev 를 빼면 lev10 계좌 수익률이 기준선의 10배가 된다."""
    rows = [{"exit_date": "2026-07-30", "pnl": 10.0, "stake_amount": 100.0,
             "max_stake_amount": 100.0, "leverage": 10, "is_open": 0}]
    assert trade_returns(rows) == [("2026-07-30", 0.01)]


def test_레버리지가_달라도_같은_가격수익률이면_같은_값이다():
    """계좌별 lev·비중이 달라도 노셔널로 나누면 동일해진다."""
    a = trade_returns([{"exit_date": "d", "pnl": 2.0, "max_stake_amount": 100.0,
                        "leverage": 1, "is_open": 0}])
    b = trade_returns([{"exit_date": "d", "pnl": 2.0, "max_stake_amount": 10.0,
                        "leverage": 10, "is_open": 0}])
    assert a == b


def test_max_stake가_없으면_stake_amount로_폴백한다():
    rows = [{"exit_date": "2026-07-30", "pnl": 5.0, "stake_amount": 50.0,
             "max_stake_amount": None, "leverage": 1, "is_open": 0}]
    assert trade_returns(rows) == [("2026-07-30", 0.10)]


def test_오픈_거래는_제외한다():
    rows = [{"exit_date": None, "pnl": 3.0, "stake_amount": 10.0,
             "max_stake_amount": 10.0, "leverage": 1, "is_open": 1}]
    assert trade_returns(rows) == []


def test_원가가_0이면_건너뛴다():
    rows = [{"exit_date": "2026-07-30", "pnl": 1.0, "stake_amount": 0.0,
             "max_stake_amount": 0.0, "leverage": 1, "is_open": 0}]
    assert trade_returns(rows) == []


def test_청산일_순으로_정렬한다():
    rows = [
        {"exit_date": "2026-07-31", "pnl": 1.0, "stake_amount": 100.0, "max_stake_amount": 100.0, "leverage": 1, "is_open": 0},
        {"exit_date": "2026-07-29", "pnl": 2.0, "stake_amount": 100.0, "max_stake_amount": 100.0, "leverage": 1, "is_open": 0},
    ]
    assert [d for d, _ in trade_returns(rows)] == ["2026-07-29", "2026-07-31"]


# ── live_curve: 앵커에서 이어받기 ────────────────────────────────────

ANCHOR = {"equity": 0.70, "peak": 1.00, "drawdown": -0.30, "uw_days": 25}


def test_라이브는_앵커_지분에서_시작한다():
    """1.0 이 아니라 백테스트 마지막 지분(0.70)에서 이어받는다."""
    c = live_curve([("2026-07-29", 0.10)], ANCHOR)
    assert c["equity"] == pytest.approx(0.77)
    assert c["peak"] == pytest.approx(1.00)          # 백테스트 고점 계승
    assert c["drawdown"] == pytest.approx(-0.23)     # 0.77/1.00 - 1


def test_거래가_없으면_앵커_그대로다():
    c = live_curve([], ANCHOR)
    assert c["equity"] == pytest.approx(0.70)
    assert c["drawdown"] == pytest.approx(-0.30)
    assert c["trades"] == 0


def test_앵커_고점을_넘으면_고점이_갱신된다():
    c = live_curve([("2026-07-29", 0.50)], ANCHOR)   # 0.70 -> 1.05
    assert c["peak"] == pytest.approx(1.05)
    assert c["drawdown"] == pytest.approx(0.0)


def test_고점_갱신_후_다시_빠지면_새_고점_대비로_잰다():
    c = live_curve([("2026-07-29", 0.50), ("2026-07-30", -0.10)], ANCHOR)
    assert c["peak"] == pytest.approx(1.05)
    assert c["equity"] == pytest.approx(0.945)
    assert c["drawdown"] == pytest.approx(-0.10)


def test_언더워터_거래수는_앵커에서_이어진다():
    """신고점을 못 넘으면 앵커의 언더워터가 계속 누적된다."""
    c = live_curve([("2026-07-29", -0.05), ("2026-07-30", -0.05)], ANCHOR)
    assert c["uw_since_anchor"] == 2
    c2 = live_curve([("2026-07-29", 0.50), ("2026-07-30", -0.05)], ANCHOR)
    assert c2["uw_since_anchor"] == 1                # 고점 갱신 후 1건만


def test_앵커가_없으면_1_0에서_시작하되_표시한다():
    c = live_curve([("2026-07-29", 0.10)], None)
    assert c["equity"] == pytest.approx(1.10)
    assert c["anchored"] is False


# ── leg_status: 기준선 대조 ─────────────────────────────────────────

REF = {"mdd": -0.4249, "mdd_date": "2020-04-03", "max_uw_days": 202,
       "worst_trade": -0.2970}


def test_기준_대비_비율과_최악거래_갱신을_판정한다():
    st = leg_status([("2026-07-29", -0.10)], ANCHOR, REF)
    assert st["drawdown"] == pytest.approx(-0.37)          # 0.63/1.00 - 1
    assert st["ratio"] == pytest.approx(0.37 / 0.4249, rel=1e-3)
    assert st["worst_trade"] == pytest.approx(-0.10)
    assert st["worst_trade_renewed"] is False


def test_기준_최악거래를_넘으면_갱신으로_표시한다():
    st = leg_status([("2026-07-29", -0.35)], ANCHOR, REF)
    assert st["worst_trade_renewed"] is True


def test_기준_MDD를_넘으면_갱신으로_표시한다():
    st = leg_status([("2026-07-29", -0.30)], ANCHOR, REF)   # 0.49/1.00 - 1 = -51%
    assert st["drawdown"] < REF["mdd"]
    assert st["mdd_renewed"] is True


def test_기준선이_없으면_비율은_None이다():
    st = leg_status([("2026-07-29", -0.10)], ANCHOR, None)
    assert st["ratio"] is None
    assert st["mdd_renewed"] is False


# ── returns_since: 기준일 이후 수익률 ────────────────────────────────

RETS = [("2026-07-30", 0.10), ("2026-08-01", 0.05), ("2026-08-04", -0.02)]


def test_기준일_이후_거래만_복리로_센다():
    # 08-01 이후: (1+0.05)*(1-0.02)-1
    assert returns_since(RETS, "2026-08-01") == pytest.approx(1.05 * 0.98 - 1)


def test_기준일_당일_청산도_포함한다():
    assert returns_since([("2026-08-01", 0.05)], "2026-08-01") == pytest.approx(0.05)


def test_기준일_이후_거래가_없으면_0이다():
    assert returns_since([("2026-07-30", 0.10)], "2026-08-01") == 0.0


def test_기준일이_없으면_전체를_센다():
    assert returns_since(RETS, None) == pytest.approx(1.10 * 1.05 * 0.98 - 1)


# ── combine_legs: 조합 성적 ──────────────────────────────────────────


def test_레그_수익률을_가중합한다():
    legs = {"SOXL": 0.10, "AVGO": 0.05, "TQQQ": -0.02}
    w = {"SOXL": 0.30, "AVGO": 0.60, "TQQQ": 0.40}   # AVGO 는 퍼프 2배 반영
    assert combine_legs(legs, w) == pytest.approx(0.30 * 0.10 + 0.60 * 0.05 + 0.40 * -0.02)


def test_거래없는_레그는_0으로_친다():
    assert combine_legs({"SOXL": 0.10}, {"SOXL": 0.5, "SOXS": 0.5}) == pytest.approx(0.05)


def test_모든_레그가_비면_None이다():
    assert combine_legs({}, {"SOXL": 0.5, "SOXS": 0.5}) is None
