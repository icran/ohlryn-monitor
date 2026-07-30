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
