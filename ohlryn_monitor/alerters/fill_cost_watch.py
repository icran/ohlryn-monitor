"""fill_cost_watch — 라이브 체결의 거래비용(수수료 + 백테스트 대비 슬리피지) 알림.

무엇을 알리나
-------------
라이브는 백테스트와 **진입 판정이 같아도 체결가가 다르다**. 그 차이를 매 체결마다
"백테스트라면 얼마에 체결됐을까"와 비교해 수수료와 함께 요약한다. 전략이 이론값 대비
실제로 얼마를 내고 있는지가 한눈에 보인다.

**체결이 없으면 아무것도 보내지 않는다** — 침묵 = 정상.

전제: vector-backtester가 원장(`data/fill_ledger.jsonl`)을 만들어 둔 상태여야 한다.
기준가 계산은 전략 규칙에 의존하므로 그쪽 담당이고, 이 알리미는 결과만 읽는다.
  vector-backtester$ python scripts/natas/fill_ledger.py --db <봇DB> [--db ...]

이미 알린 체결은 **상태 파일로 기억**한다(시간창이 아니라 키 기반) — cron 재실행·지연·
서버 재기동에도 중복 발송이 없다.

사용:
  python3 -m ohlryn_monitor.alerters.fill_cost_watch --config config/fill_cost_watch.myserver.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

from ohlryn_monitor.fill_cost import (
    build_message,
    evaluate,
    load_mismatches,
    load_ledger,
    record_key,
    select_new,
    should_notify,
    summarize,
)
from ohlryn_monitor.notify import parse_env, telegram_send
from ohlryn_monitor.state import load_state, save_state

# 상태에 남길 최근 키 개수 — 무한 증가를 막는다. 하루 수 건이라 넉넉하다.
MAX_SEEN = 3000


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", required=True, help="JSON config 경로")
    ap.add_argument("--dry-run", action="store_true", help="전송/상태저장 안 함")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    repo = cfg["repo"]
    ledger = cfg.get("ledger") or os.path.join(repo, "data", "fill_ledger.jsonl")

    state = load_state(cfg["state_file"])
    seen: list[str] = state.setdefault("seen", [])

    recs = load_ledger(ledger)
    fresh = select_new(recs, seen)
    print(f"  원장 {len(recs)}건 · 신규 {len(fresh)}건 ({ledger})")

    if not fresh:
        if args.dry_run:
            print("DRY-RUN send=False (신규 체결 없음 — 침묵)")
        return

    now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
    summary = summarize(fresh)
    evaluation = evaluate(
        summary,
        assumed_pct=float(cfg.get("assumed_cost_pct", 0.07)),
        alert_ratio=float(cfg.get("cost_alert_ratio", 1.5)),
    )
    audit_path = cfg.get("audit") or os.path.join(repo, "data", "fill_audit.jsonl")
    mismatches = load_mismatches(audit_path, int(cfg.get("audit_recent_days", 2)))
    always = bool(cfg.get("always_notify", False))
    send = should_notify(evaluation, mismatches, always=always)

    message = build_message(
        cfg.get("alert_prefix", "[fill-cost]"), now_kst.strftime("%Y-%m-%d %H:%M"),
        summary, evaluation=evaluation, mismatches=mismatches,
    )
    for r in fresh:
        print(f"    {r.get('ts_et')} {r.get('kind')} {r.get('pair')} {r.get('leg')}"
              f" dev={r.get('deviation_pct'):+.3f}%")
    cp = evaluation.get("cost_pct")
    print(f"  판정: 비용 {'보류' if cp is None else f'{cp:.3f}%'}"
          f" (가정 {evaluation['assumed_pct']:.2f}%)"
          f" · 이탈={evaluation['exceeded']} · 불일치={len(mismatches)}")

    if args.dry_run:
        print(f"DRY-RUN send={send}" + (f"\n{message}" if message and send else ""))
        return

    # 이상 없으면 침묵하되 **상태는 저장**한다 — 안 그러면 같은 체결을 매일 다시 판정한다
    if send and message:
        env = parse_env(os.path.join(repo, cfg["alert_env"]))
        try:
            telegram_send(env.get("TELEGRAM_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", ""), message)
        except Exception as e:  # noqa: BLE001 — 전송 실패 시 상태를 남기지 않아 다음 실행이 재시도한다
            print(f"telegram 발송 실패: {e}")
            return
    else:
        print("  이상 없음 — 침묵")

    state["seen"] = (seen + [record_key(r) for r in fresh])[-MAX_SEEN:]
    save_state(cfg["state_file"], state)


if __name__ == "__main__":
    main()
