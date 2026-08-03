"""botstatus 순수 로직 테스트 — 봇 현황 요약 (전략 그룹핑·중지/정체 판정)."""

from datetime import datetime, timedelta, timezone

from ohlryn_monitor.botstatus import summarize_bot

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _eng(pair, config="alpha", running=True, ago_min=5):
    return {
        "pair": pair,
        "config_name": config,
        "is_running": running,
        "last_updated": (NOW - timedelta(minutes=ago_min)).isoformat(),
    }


def test_down_bot():
    # API 응답 없음 → down + 오류명이 상세에 표기된다
    s = summarize_bot("b1", None, now=NOW, stale_minutes=40, error="URLError")
    assert s["status"] == "down"
    assert "URLError" in s["detail"]
    assert s["strategies"] == []


def test_empty_engines_is_down():
    # 엔진 목록이 비어있으면 봇이 정상 기동한 게 아니다 → down
    s = summarize_bot("b1", [], now=NOW, stale_minutes=40)
    assert s["status"] == "down"


def test_ok_grouping_by_config():
    # 같은 config의 두 페어는 전략 1개로 묶이고, 전체 정상이면 ok
    engines = [_eng("BTC"), _eng("ETH"), _eng("SOL", config="beta")]
    s = summarize_bot("b1", engines, now=NOW, stale_minutes=40)
    assert s["status"] == "ok"
    by = {x["config"]: x for x in s["strategies"]}
    assert by["alpha"]["pair_names"] == ["BTC", "ETH"]
    assert by["beta"]["pair_names"] == ["SOL"]
    assert all(x["status"] == "ok" for x in s["strategies"])


def test_stopped_and_stale_detected():
    # is_running=False → 중지, last_updated 오래됨 → 정체. 하나라도 있으면 warn
    engines = [_eng("BTC", running=False), _eng("ETH", ago_min=120)]
    s = summarize_bot("b1", engines, now=NOW, stale_minutes=40)
    assert s["status"] == "warn"
    st = s["strategies"][0]
    assert st["stopped"] == ["BTC"]
    assert st["stale"] == ["ETH"]
    assert st["status"] == "warn"


def test_config_name_fallback():
    # config_name이 없으면 strategy 필드로, 그것도 없으면 "전략"으로 묶인다
    engines = [{"pair": "BTC", "strategy": "S1", "is_running": True}]
    s = summarize_bot("b1", engines, now=NOW, stale_minutes=40)
    assert s["strategies"][0]["config"] == "S1"
