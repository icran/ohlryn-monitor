"""health_check 알리미 조립 테스트 — heartbeat ping 의미론 (2026-07-30 변경).

새 규약:
  - healthchecks DOWN = "서버/알림 사슬 사망"만 의미한다.
  - CRITICAL이어도 텔레그램 발송에 성공했으면 정상 ping (/fail 발화 제거).
  - 텔레그램 발송 실패 시 ping을 생략 → 침묵 경보(grace 후 DOWN)로 전환.
"""

import json
import sys

import ohlryn_monitor.alerters.health_check as hc


def _setup(tmp_path, monkeypatch, *, issues, telegram_raises=False):
    """임시 config/env/state로 main()을 실행할 수 있게 조립한다."""
    env_file = tmp_path / ".env_alert"
    env_file.write_text("TELEGRAM_TOKEN=t\nTELEGRAM_CHAT_ID=c\n")
    cfg = {
        "repo": str(tmp_path),
        "alert_env": env_file.name,
        "state_file": str(tmp_path / "state.json"),
        "ping_url": "https://hc-ping.com/UUID",
        "bots": [],
    }
    cfg_path = tmp_path / "hc.json"
    cfg_path.write_text(json.dumps(cfg))

    # 이슈 주입: 봇 없음 → system_issues만 경유
    monkeypatch.setattr(hc, "read_system_metrics", lambda: {})
    monkeypatch.setattr(hc, "system_issues", lambda metrics, limits: issues)

    sent, pinged = [], []
    def fake_send(token, chat_id, text, **kw):
        if telegram_raises:
            raise OSError("telegram down")
        sent.append(text)
    monkeypatch.setattr(hc, "telegram_send", fake_send)
    monkeypatch.setattr(hc, "ping", lambda url: pinged.append(url))

    monkeypatch.setattr(sys, "argv", ["health_check", "--config", str(cfg_path)])
    return sent, pinged


def test_all_ok_pings_success(tmp_path, monkeypatch):
    # 이슈 없음 → 발송 없음 + 정상 ping
    sent, pinged = _setup(tmp_path, monkeypatch, issues=[])
    hc.main()
    assert sent == []
    assert pinged == ["https://hc-ping.com/UUID"]


def test_critical_with_telegram_ok_still_pings_success(tmp_path, monkeypatch):
    # CRITICAL이어도 텔레그램 발송 성공 → /fail 없이 정상 ping (DOWN 오해 제거)
    issues = [("sys:mem", "CRITICAL 메모리 잔여 10 MB")]
    sent, pinged = _setup(tmp_path, monkeypatch, issues=issues)
    hc.main()
    assert len(sent) == 1 and "CRITICAL" in sent[0]
    assert pinged == ["https://hc-ping.com/UUID"]  # /fail 미부착
    assert all("/fail" not in u for u in pinged)


def test_telegram_failure_skips_ping(tmp_path, monkeypatch):
    # 발송 실패 → ping 생략 → healthchecks가 침묵으로 DOWN (알림 사슬 고장 신호)
    issues = [("sys:mem", "CRITICAL 메모리 잔여 10 MB")]
    sent, pinged = _setup(tmp_path, monkeypatch, issues=issues, telegram_raises=True)
    hc.main()
    assert sent == []
    assert pinged == []


def test_dry_run_never_pings(tmp_path, monkeypatch):
    sent, pinged = _setup(tmp_path, monkeypatch, issues=[])
    monkeypatch.setattr(sys, "argv", sys.argv + ["--dry-run"])
    hc.main()
    assert pinged == []
