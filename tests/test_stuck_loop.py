"""stuck_loop 순수 로직 테스트 — 봇이 조용히 멈춘 상태 감지.

배경(2026-08-04 사고, server1 :8014 양변기 SOXL): 부분 청산 후 잔량에 부동소수점
먼지(2.22e-16)가 남아 포지션이 닫히지 않았다. 엔진은 매 tick 청산을 재시도했고 최소
주문량 미달로 거부되어, **그 레그의 신규 진입이 영구 차단**됐다.

돈은 안 잃었지만 전략이 죽었고 **로그에만 흔적이 남아 아무도 몰랐다** — 사용자가 우연히
텔레그램 체결 알림을 보고 물어보지 않았다면 며칠 지났을 것이다. 같은 계열(잔차 soft-kill,
주문 반복 실패)을 포괄해 "같은 경고가 짧은 시간에 N회 반복되면 알린다"로 일반화한다.
"""

from datetime import datetime, timedelta, timezone

from ohlryn_monitor.stuck_loop import (
    count_recent_matches,
    parse_log_time,
    plan_stuck_alerts,
)


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


NOW = dt("2026-08-04T13:40:00")

LINES = [
    "[2026-08-04 13:32:40,847] [WARNING] [SOXL/USDT:USDT] Order amount 2.2e-16 < min_qty 0.01. Skipping.",
    "[2026-08-04 13:32:40,857] [INFO] [SOXL/USDT:USDT] Exit Signal Detected. Pass the next step",
    "[2026-08-04 13:32:45,847] [WARNING] [SOXL/USDT:USDT] Order amount 2.2e-16 < min_qty 0.01. Skipping.",
    "[2026-08-04 13:39:50,847] [WARNING] [SOXL/USDT:USDT] Order amount 2.2e-16 < min_qty 0.01. Skipping.",
    "무시되어야 하는 타임스탬프 없는 줄 < min_qty",
    "[2026-08-04 11:00:00,000] [WARNING] [SOXL/USDT:USDT] Order amount 2.2e-16 < min_qty 0.01. Skipping.",
]


# ── parse_log_time ──────────────────────────────────────────────────


def test_표준_로그_시각을_UTC로_파싱한다():
    assert parse_log_time(LINES[0]) == dt("2026-08-04T13:32:40")


def test_타임스탬프가_없으면_None을_반환한다():
    assert parse_log_time("타임스탬프 없는 줄") is None
    assert parse_log_time("") is None


# ── count_recent_matches ────────────────────────────────────────────


def test_윈도우_안의_매칭만_센다():
    # 10분 윈도우: 13:30 이후 → 13:32:40, 13:32:45, 13:39:50 = 3건 (11:00 건은 제외)
    since = NOW - timedelta(minutes=10)
    assert count_recent_matches(LINES, "< min_qty", since) == 3


def test_패턴이_다르면_세지_않는다():
    since = NOW - timedelta(minutes=10)
    assert count_recent_matches(LINES, "unattributed residual", since) == 0


def test_타임스탬프_없는_줄은_패턴이_맞아도_제외한다():
    """시각을 모르면 윈도우 판정을 할 수 없다 — 오래된 줄을 최근으로 오인하면 안 된다."""
    since = NOW - timedelta(minutes=10)
    assert count_recent_matches([LINES[4]], "< min_qty", since) == 0


# ── plan_stuck_alerts ───────────────────────────────────────────────


def _watch(count, threshold=20, name="sub/min_qty"):
    return [{"name": name, "label": ":8014 양변기 SOXL", "pattern": "< min_qty",
             "count": count, "threshold": threshold, "window_minutes": 10,
             "action": "bash ~/fix_dust_and_deploy.sh"}]


def test_임계_미만이면_알리지_않는다():
    msgs, sent = plan_stuck_alerts(_watch(19), NOW, already_sent={}, cooldown_minutes=360)
    assert msgs == []
    assert sent == {}


def test_임계_이상이면_알리고_상태를_기록한다():
    msgs, sent = plan_stuck_alerts(_watch(95), NOW, already_sent={}, cooldown_minutes=360)
    assert len(msgs) == 1
    assert "95" in msgs[0] and ":8014 양변기 SOXL" in msgs[0]
    assert "bash ~/fix_dust_and_deploy.sh" in msgs[0]
    assert sent["sub/min_qty"] == NOW.isoformat()


def test_쿨다운_안에는_반복_발송하지_않는다():
    prev = {"sub/min_qty": (NOW - timedelta(minutes=30)).isoformat()}
    msgs, sent = plan_stuck_alerts(_watch(95), NOW, already_sent=prev, cooldown_minutes=360)
    assert msgs == []
    assert sent == prev  # 상태 유지 (최초 발송 시각을 갱신하지 않는다)


def test_쿨다운이_지나면_다시_알린다():
    prev = {"sub/min_qty": (NOW - timedelta(minutes=400)).isoformat()}
    msgs, sent = plan_stuck_alerts(_watch(95), NOW, already_sent=prev, cooldown_minutes=360)
    assert len(msgs) == 1
    assert sent["sub/min_qty"] == NOW.isoformat()


def test_반복이_멈추면_해소를_알리고_상태를_지운다():
    prev = {"sub/min_qty": (NOW - timedelta(minutes=30)).isoformat()}
    msgs, sent = plan_stuck_alerts(_watch(0), NOW, already_sent=prev, cooldown_minutes=360)
    assert len(msgs) == 1
    assert "해소" in msgs[0]
    assert "sub/min_qty" not in sent


def test_알린적_없는데_0건이면_아무것도_안한다():
    msgs, sent = plan_stuck_alerts(_watch(0), NOW, already_sent={}, cooldown_minutes=360)
    assert msgs == []
    assert sent == {}


def test_여러_감시항목을_독립적으로_처리한다():
    watches = _watch(95) + [
        {"name": "main/residual", "label": ":8012", "pattern": "unattributed residual",
         "count": 3, "threshold": 1, "window_minutes": 60, "action": None}
    ]
    msgs, sent = plan_stuck_alerts(watches, NOW, already_sent={}, cooldown_minutes=360)
    assert len(msgs) == 2
    assert set(sent) == {"sub/min_qty", "main/residual"}
