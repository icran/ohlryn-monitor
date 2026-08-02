"""schedule_watch 순수 로직 테스트 — 파라미터 스케줄 만료 감시.

배경(2026-07-31 사고): WFA 스케줄이 조용히 만료된 채 봇이 재시작되면 파라미터가
기본값으로 회귀(필터 OFF)한다. 만료가 다가오면 D-N 경고, 만료되면 CRITICAL을 보내
"아무도 모르는 만료"를 구조적으로 차단한다.
"""

from datetime import datetime, timezone

from ohlryn_monitor.schedule import (
    last_end_from_csv,
    plan_schedule_alerts,
)


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# ── last_end_from_csv: CSV에서 마지막 end 시각 추출 ──────────────────

CSV = (
    "start,end,atr_spike_multiplier\n"
    "2026-05-29T00:00:00+00:00,2026-06-28T00:00:00+00:00,1.0\n"
    "2026-06-28T00:00:00+00:00,2026-07-28T00:00:00+00:00,1.0\n"
    "2026-07-28T00:00:00+00:00,2026-08-27T00:00:00+00:00,1.0\n"
)


def test_last_end_parses_final_row(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text(CSV)
    assert last_end_from_csv(str(p)) == dt("2026-08-27T00:00:00")


def test_last_end_rows_unordered(tmp_path):
    # 행 순서가 섞여 있어도 최댓값 end를 쓴다
    lines = CSV.splitlines()
    p = tmp_path / "s.csv"
    p.write_text("\n".join([lines[0], lines[3], lines[1], lines[2]]) + "\n")
    assert last_end_from_csv(str(p)) == dt("2026-08-27T00:00:00")


def test_last_end_missing_file_returns_none(tmp_path):
    assert last_end_from_csv(str(tmp_path / "nope.csv")) is None


# ── plan_schedule_alerts: 경고 단계 판정 (중복 발송 억제 포함) ────────


def plan(now, end, sent=None, warn_days=(7, 3)):
    return plan_schedule_alerts(
        name="wfa",
        end=end,
        now=dt(now),
        warn_days=warn_days,
        already_sent=sent or [],
    )


def test_far_from_expiry_no_alert():
    msgs, sent = plan("2026-08-01T00:00:00", dt("2026-08-27T00:00:00"))
    assert msgs == []


def test_d7_warning_once():
    end = dt("2026-08-27T00:00:00")
    msgs, sent = plan("2026-08-21T00:00:00", end)  # D-6 → 7일 임계 발동
    assert len(msgs) == 1 and "WARNING" in msgs[0] and "wfa" in msgs[0]
    assert "D-6" in msgs[0]
    # 같은 단계는 재발송하지 않음
    msgs2, _ = plan("2026-08-22T00:00:00", end, sent=sent)
    assert msgs2 == []


def test_d3_escalation_after_d7():
    end = dt("2026-08-27T00:00:00")
    _, sent = plan("2026-08-21T00:00:00", end)  # 7일 단계 발송됨
    msgs, sent2 = plan("2026-08-25T00:00:00", end, sent=sent)  # D-2 → 3일 단계
    assert len(msgs) == 1 and "D-2" in msgs[0]


def test_expired_is_critical():
    end = dt("2026-08-27T00:00:00")
    msgs, _ = plan("2026-08-28T01:00:00", end)
    assert len(msgs) == 1 and "CRITICAL" in msgs[0] and "만료" in msgs[0]


def test_expired_not_repeated():
    end = dt("2026-08-27T00:00:00")
    _, sent = plan("2026-08-28T01:00:00", end)
    msgs, _ = plan("2026-08-29T01:00:00", end, sent=sent)
    assert msgs == []


def test_missing_csv_alerts_critical():
    # CSV를 못 읽는 것 자체가 감시 사슬 고장 — CRITICAL
    msgs, _ = plan("2026-08-01T00:00:00", None)
    assert len(msgs) == 1 and "CRITICAL" in msgs[0]


def test_new_schedule_resets_sent_stages():
    # 스케줄이 갱신되어 end가 미래로 바뀌면 이전 발송 이력은 자동 무효
    # (already_sent는 (end_iso, stage) 쌍이므로 end가 바뀌면 매칭 안 됨)
    old_end = dt("2026-08-27T00:00:00")
    _, sent = plan("2026-08-26T00:00:00", old_end)  # D-1 단계 발송
    new_end = dt("2026-09-26T00:00:00")
    msgs, _ = plan("2026-08-28T00:00:00", new_end, sent=sent)
    assert msgs == []  # 새 end 기준 D-29 → 무경고 (이력에 안 걸림)


def test_dry_run_works_without_env_file(tmp_path, monkeypatch, capsys):
    # dry-run은 alert_env가 없어도 crash하지 않아야 (2026-07-27 사고 계급 회귀 방지)
    import json, sys
    import ohlryn_monitor.alerters.schedule_watch as sw
    csv = tmp_path / "s.csv"
    csv.write_text("start,end,x\n2026-01-01T00:00:00+00:00,2026-01-02T00:00:00+00:00,1\n")  # 만료됨
    cfg = {"repo": str(tmp_path), "alert_env": str(tmp_path / "missing.env"),
           "state_file": str(tmp_path / "st.json"), "schedules": [{"name": "t", "csv": "s.csv"}]}
    p = tmp_path / "cfg.json"; p.write_text(json.dumps(cfg))
    monkeypatch.setattr(sys, "argv", ["sw", "--config", str(p), "--dry-run"])
    sw.main()  # crash 없이 완료돼야
    out = capsys.readouterr().out
    assert "DRY-RUN telegram" in out and "CRITICAL" in out


def test_action_text_included_in_alerts():
    # config의 action 문구(실행 지시·기한)가 경고/만료 메시지에 포함된다
    end = dt("2026-08-27T00:00:00")
    act = 'Claude에게 "wfa 갱신해줘" (7일 내 완료 권장)'
    msgs, _ = plan_schedule_alerts("wfa", end, dt("2026-08-28T00:00:00"), warn_days=(7, 3), already_sent=[], action=act)
    assert len(msgs) == 1 and act in msgs[0] and "갱신 가능" in msgs[0]
    msgs2, _ = plan_schedule_alerts("wfa", end, dt("2026-08-21T00:00:00"), warn_days=(7, 3), already_sent=[], action=act)
    assert len(msgs2) == 1 and act in msgs2[0]
