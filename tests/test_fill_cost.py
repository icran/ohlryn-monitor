"""fill_cost — 체결 비용 원장 집계·메시지 조립 테스트."""

from __future__ import annotations

import json

from ohlryn_monitor.fill_cost import (
    build_message,
    load_ledger,
    record_key,
    select_new,
    summarize,
)


def _rec(**kw) -> dict:
    """원장 레코드 기본형 (vector-backtester fill_ledger.jsonl 포맷)."""
    base = {
        "db": "hs_trade_main_binance.db",
        "trade_id": 1,
        "leg": "entry",
        "strategy_id": "natas15_ibs_live_soxl_main",
        "kind": "ibs",
        "pair": "SOXL/USDT:USDT",
        "ts_utc": "2026-07-29T20:00:02+00:00",
        "ts_et": "2026-07-29T16:00:02-04:00",
        "fill_price": 92.44,
        "expected_price": 92.15,
        "deviation_pct": 0.315,
        "deviation_usdt": 13.346,
        "fees": 1.701,
        "funding_fee": 0.0,
        "contaminated": False,
        "off_session": False,
    }
    base.update(kw)
    return base


def test_load_ledger_배제_규칙(tmp_path):
    """오염(다른 전략 신호)·세션밖 체결·기준가 없는 건은 비용 통계에서 배제한다."""
    p = tmp_path / "l.jsonl"
    p.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                _rec(trade_id=1),
                _rec(trade_id=2, contaminated=True),
                _rec(trade_id=3, off_session=True),
                _rec(trade_id=4, deviation_pct=None, deviation_usdt=None),
            ]
        )
    )
    recs = load_ledger(str(p))
    assert [r["trade_id"] for r in recs] == [1]


def test_load_ledger_없는_파일은_빈_리스트(tmp_path):
    """원장이 아직 없으면(첫 실행) 조용히 빈 리스트를 준다."""
    assert load_ledger(str(tmp_path / "nope.jsonl")) == []


def test_record_key_는_db_거래_leg_로_유일(tmp_path):
    """같은 trade의 entry/exit는 별개 항목이고, 다른 계좌의 같은 id도 구별된다."""
    a = record_key(_rec(trade_id=7, leg="entry"))
    b = record_key(_rec(trade_id=7, leg="exit"))
    c = record_key(_rec(trade_id=7, leg="entry", db="hs_binance_sub.db"))
    assert len({a, b, c}) == 3


def test_select_new_는_이미_알린_건을_제외():
    """cron 재실행·지연에도 같은 체결을 두 번 알리지 않는다 (시간창이 아닌 상태 기반)."""
    r1, r2 = _rec(trade_id=1), _rec(trade_id=2)
    seen = [record_key(r1)]
    fresh = select_new([r1, r2], seen)
    assert [r["trade_id"] for r in fresh] == [2]


def test_summarize_불리_방향_정규화():
    """진입은 비싸게(+)가 불리, 청산은 싸게(−)가 불리 → 둘 다 '불리=양수'로 모은다."""
    entry = _rec(leg="entry", deviation_pct=0.3, deviation_usdt=3.0)
    exit_ = _rec(leg="exit", trade_id=2, deviation_pct=-0.5, deviation_usdt=-5.0)
    s = summarize([entry, exit_])
    # 진입 +3.0(불리) + 청산 −(−5.0)=+5.0(불리) = 8.0
    assert s["groups"]["ibs"]["slip_usdt"] == 8.0
    assert s["groups"]["ibs"]["n_entry"] == 1
    assert s["groups"]["ibs"]["n_exit"] == 1


def test_summarize_수수료는_entry_leg만_합산():
    """수수료·펀딩은 trade 단위 누적값이라 leg마다 더하면 이중 계상된다."""
    entry = _rec(leg="entry", fees=1.0)
    exit_ = _rec(leg="exit", fees=1.0)  # 같은 trade의 청산 — 더해선 안 됨
    s = summarize([entry, exit_])
    assert s["groups"]["ibs"]["fees"] == 1.0


def test_summarize_전략별_분리():
    """IBS와 양변기를 섞지 않는다."""
    s = summarize([_rec(kind="ibs"), _rec(kind="yangbyeongi", trade_id=9)])
    assert set(s["groups"]) == {"ibs", "yangbyeongi"}


def test_build_message_아이콘과_소수점():
    """유리/불리는 색 아이콘으로, 금액은 소수점 2자리로 표기한다."""
    msg = build_message("[test]", "2026-07-30 06:00", summarize([_rec()]))
    assert "[test]" in msg
    assert "🔴" in msg              # 불리 건이므로
    assert "13.35" in msg           # 13.346 → 2자리
    assert "13.346" not in msg      # 4자리는 노출하지 않는다


def test_build_message_유리하면_초록_아이콘():
    """전체가 유리하면 총합이 '이득'으로 표기된다."""
    r = _rec(deviation_pct=-0.2, deviation_usdt=-9.0, fees=0.5)
    msg = build_message("[test]", "2026-07-30 06:00", summarize([r]))
    assert "🟢" in msg
    assert "이득" in msg


def test_build_message_빈_집계는_None():
    """알릴 체결이 없으면 메시지를 만들지 않는다 (침묵 = 정상)."""
    assert build_message("[test]", "2026-07-30 06:00", summarize([])) is None


def test_build_message_평균도_방향을_단어로():
    """평균 괴리율도 부호만 쓰면 모호하다 — 금액과 같이 '불리/유리'를 붙인다."""
    adverse = build_message("[t]", "ts", summarize([_rec(deviation_pct=0.315)]))
    assert "0.315% 불리" in adverse
    favor = build_message(
        "[t]", "ts", summarize([_rec(leg="exit", deviation_pct=0.515, deviation_usdt=1.28)])
    )
    assert "0.515% 유리" in favor


# ── 판정 (이상 징후만 알림) ────────────────────────────────────────────

def test_evaluate_노셔널_대비_비용률():
    """USDT 절대액은 계좌·레버리지에 좌우되므로 판정은 노셔널 대비 %로 한다."""
    from ohlryn_monitor.fill_cost import evaluate
    # 노셔널 = 92.15 * 46.13 ≈ 4251, 비용 = 수수료 1.701 + 슬리피지 13.346 ≈ 15.05
    r = _rec(fill_price=92.44, amount=46.13, fees=1.701, deviation_usdt=13.346)
    ev = evaluate(summarize([r]), assumed_pct=0.07)
    assert ev["notional"] > 4000
    assert 0.3 < ev["cost_pct"] < 0.4          # ≈ 0.353%
    assert ev["ratio"] > 4                      # 가정의 4배 이상
    assert ev["exceeded"] is True


def test_evaluate_가정_이내면_통과():
    """비용이 가정 안이면 이탈 아님."""
    from ohlryn_monitor.fill_cost import evaluate
    r = _rec(fill_price=100.0, amount=100.0, fees=1.0, deviation_usdt=1.0)  # 0.02%
    ev = evaluate(summarize([r]), assumed_pct=0.07)
    assert ev["exceeded"] is False


def test_evaluate_노셔널_0이면_판정_보류():
    """amount가 없으면(데이터 결손) 0으로 나누지 않고 판정을 보류한다."""
    from ohlryn_monitor.fill_cost import evaluate
    r = _rec(amount=0.0)
    ev = evaluate(summarize([r]), assumed_pct=0.07)
    assert ev["cost_pct"] is None and ev["exceeded"] is False


def test_should_notify_이상만():
    """정상이면 침묵, 비용 이탈이나 진입 불일치가 있으면 알린다."""
    from ohlryn_monitor.fill_cost import should_notify
    assert should_notify({"exceeded": False}, mismatches=[]) is False
    assert should_notify({"exceeded": True}, mismatches=[]) is True
    assert should_notify({"exceeded": False}, mismatches=[{"kind": "ibs"}]) is True
    # always=True면 정상이어도 보낸다 (요약을 매일 받고 싶을 때)
    assert should_notify({"exceeded": False}, mismatches=[], always=True) is True


def test_build_message_판정_헤더():
    """메시지 맨 위에 판정 한 줄이 온다 — 숫자를 읽기 전에 OK/이탈이 보여야 한다."""
    from ohlryn_monitor.fill_cost import evaluate
    r = _rec(fill_price=92.44, amount=46.13, fees=1.701, deviation_usdt=13.346)
    s = summarize([r])
    msg = build_message("[t]", "ts", s, evaluation=evaluate(s, assumed_pct=0.07))
    assert "판정" in msg
    assert "0.07%" in msg          # 가정치를 함께 보여준다
    assert "배" in msg             # 가정 대비 배수


def test_load_mismatches_최근만(tmp_path):
    """감사 파일에는 과거 사고 기록이 남는다 — 이미 처리된 건을 매일 다시 알리지 않도록
    최근 N일만 읽는다."""
    from datetime import date, timedelta
    from ohlryn_monitor.fill_cost import load_mismatches
    p = tmp_path / "a.jsonl"
    old = (date.today() - timedelta(days=10)).isoformat()
    new = (date.today() - timedelta(days=1)).isoformat()
    p.write_text("\n".join(json.dumps(r) for r in [
        {"date": old, "mismatch": True, "kind": "ibs", "reason": "오진입"},
        {"date": new, "mismatch": True, "kind": "ibs", "reason": "미진입"},
        {"date": new, "mismatch": False, "kind": "ibs", "reason": "일치"},
    ]))
    got = load_mismatches(str(p), recent_days=2)
    assert [r["reason"] for r in got] == ["미진입"]


def test_load_mismatches_없는_파일(tmp_path):
    """감사가 아직 가동 안 됐으면 빈 목록 (알림 판정에서 무시)."""
    from ohlryn_monitor.fill_cost import load_mismatches
    assert load_mismatches(str(tmp_path / "none.jsonl"), recent_days=2) == []


def test_account_알려진_db는_계좌_통칭으로():
    """건별 표기가 어느 계좌인지 즉시 읽혀야 한다 (계좌별 A/B 판독의 전제)."""
    from ohlryn_monitor.fill_cost import _account
    assert _account({"db": "hs_binance_sub.db"}) == "sub"
    assert _account({"db": "hs_trade_main_binance.db"}) == "main"
    assert _account({"db": "/home/ubuntu/vb/hs_private_copy_binance.db"}) == "hs_private_copy"


def test_account_모르는_db가_main으로_떨어지지_않는다():
    """부분일치 판정('sub' in db)은 새 계좌를 조용히 main으로 오표기한다.

    실제로 hs_private_copy_binance.db 를 원장에 추가하자 main 거래로 보였다.
    모르는 DB는 추측하지 말고 파일명(stem)을 그대로 드러내 이상을 눈에 띄게 한다.
    """
    from ohlryn_monitor.fill_cost import _account
    assert _account({"db": "brand_new_account.db"}) == "brand_new_account"
    assert _account({"db": ""}) == "?"
