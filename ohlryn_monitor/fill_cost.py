"""체결 비용 원장 집계 — 수수료 + 백테스트 대비 슬리피지.

입력은 vector-backtester가 만드는 **`data/fill_ledger.jsonl`** 이다. 한 줄 = 한 체결(leg)이며,
"백테스트라면 얼마에 체결됐을까"(`expected_price`)가 이미 계산돼 들어 있다.

⭐ 이 모듈은 **전략 규칙을 모른다.** 기준가 산출(양변기 next_bar/LOC/MOO 판별, IBS 세션종가)은
엔진 도메인 지식이라 vector-backtester가 담당하고, 여기서는 그 결과만 읽어 집계·표현한다.
그래서 stdlib only가 유지되고, 전략 규칙이 바뀌어도 이 파일은 고칠 필요가 없다.

부호 규약
--------
`deviation_pct`/`deviation_usdt`는 원장 그대로 "체결 − 기준" 부호다. 사람이 읽을 때는
**불리하면 양수**가 직관적이므로 leg별로 방향을 맞춘다:
  · 진입(entry): 비싸게 사면 불리 → 부호 그대로
  · 청산(exit) : 싸게 팔면 불리 → 부호 반전
"""

from __future__ import annotations

import json
import os

KIND_LABEL = {"ibs": "IBS(#15)", "yangbyeongi": "양변기(#29)"}
ADVERSE, FAVOR = "🔴", "🟢"


def load_ledger(path: str) -> list[dict]:
    """원장 JSONL을 읽어 **비용 통계에 쓸 수 있는 건만** 반환한다.

    배제 대상:
      · `contaminated` — 다른 전략 신호로 진입한 건(앙상블 신호 오염 사고)
      · `off_session`  — 미국 세션 밖 체결(수동 개입 등)
      · 기준가 산출 실패(`deviation_pct` 없음) — 비교 대상이 없다
    파일이 없으면(첫 실행) 빈 리스트.
    """
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("contaminated") or r.get("off_session"):
                continue
            if r.get("deviation_pct") is None:
                continue
            out.append(r)
    return out


def record_key(rec: dict) -> str:
    """체결 식별자. 계좌(db)·거래·leg 조합이라 같은 trade의 진입/청산이 구별된다."""
    return f"{rec.get('db')}|{rec.get('trade_id')}|{rec.get('leg')}"


def select_new(recs: list[dict], seen: list[str]) -> list[dict]:
    """아직 알리지 않은 체결만 고른다.

    시간창(`--hours 24`)이 아니라 **상태 기반**이라 cron 재실행·지연·서버 재기동에도
    같은 체결을 두 번 알리지 않는다.
    """
    seen_set = set(seen)
    return [r for r in recs if record_key(r) not in seen_set]


def adverse_pct(rec: dict) -> float:
    """불리 방향으로 정규화한 괴리율(%). 양수 = 백테스트보다 나쁘게 체결."""
    d = float(rec["deviation_pct"])
    return d if rec.get("leg") == "entry" else -d


def adverse_usdt(rec: dict) -> float:
    """불리 방향으로 정규화한 괴리 금액(USDT)."""
    d = float(rec.get("deviation_usdt") or 0.0)
    return d if rec.get("leg") == "entry" else -d


def summarize(recs: list[dict]) -> dict:
    """전략(kind)별로 수수료·펀딩·슬리피지를 집계한다.

    ⚠ 수수료·펀딩은 **trade 단위 누적값**이라 entry leg만 더한다 — leg마다 더하면
    같은 거래의 수수료가 두 번 계상된다.
    """
    groups: dict[str, dict] = {}
    for r in recs:
        g = groups.setdefault(
            r.get("kind", "unknown"),
            {"n_entry": 0, "n_exit": 0, "fees": 0.0, "funding": 0.0,
             "slip_usdt": 0.0, "pcts": [], "items": []},
        )
        if r.get("leg") == "entry":
            g["n_entry"] += 1
            g["fees"] += float(r.get("fees") or 0.0)
            g["funding"] += float(r.get("funding_fee") or 0.0)
        else:
            g["n_exit"] += 1
        g["slip_usdt"] += adverse_usdt(r)
        g["pcts"].append(adverse_pct(r))
        g["items"].append(r)

    total_fee = sum(g["fees"] + g["funding"] for g in groups.values())
    total_slip = sum(g["slip_usdt"] for g in groups.values())
    return {"groups": groups, "total_fee": total_fee, "total_slip": total_slip,
            "n": len(recs)}


def evaluate(summary: dict, assumed_pct: float, alert_ratio: float = 1.5) -> dict:
    """비용이 백테스트 가정 범위 안인지 판정한다.

    ⚠ 판정 축은 **USDT 금액이 아니라 노셔널 대비 %**다. 절대액은 계좌 규모·레버리지에
    좌우되므로(IBS는 10x라 같은 %도 금액이 11배로 보인다) 기준이 흔들린다.

    `assumed_pct` = 백테스트가 가정한 편도 비용률(%). 나타스 캠페인 기준은 0.07%로,
    저자가 제시한 비용 수치(#29 79.2%→53.7%, #10 70.0%→34.8%)가 모두 이 값이었다.
    `alert_ratio` = 가정의 몇 배를 넘으면 이탈로 볼지(기본 1.5배 — 표본 노이즈 여유).
    """
    notional = 0.0
    for g in (summary.get("groups") or {}).values():
        for r in g["items"]:
            if r.get("leg") == "entry":
                notional += float(r.get("fill_price") or 0.0) * float(r.get("amount") or 0.0)

    cost = summary.get("total_fee", 0.0) + summary.get("total_slip", 0.0)
    if notional <= 0:
        # 노셔널을 모르면(amount 결손) 비율 판정을 하지 않는다 — 0으로 나누지 않는다
        return {"notional": notional, "cost_usdt": cost, "cost_pct": None,
                "ratio": None, "assumed_pct": assumed_pct, "exceeded": False}

    cost_pct = cost / notional * 100
    ratio = cost_pct / assumed_pct if assumed_pct > 0 else None
    return {
        "notional": notional, "cost_usdt": cost, "cost_pct": cost_pct,
        "ratio": ratio, "assumed_pct": assumed_pct,
        "fee_pct": summary.get("total_fee", 0.0) / notional * 100,
        "slip_pct": summary.get("total_slip", 0.0) / notional * 100,
        "exceeded": bool(ratio is not None and ratio > alert_ratio),
    }


def should_notify(evaluation: dict, mismatches: list | None = None,
                  always: bool = False) -> bool:
    """알릴지 판단 — **이상 징후만**. 정상이면 침묵(monitor 철학).

    `always=True`면 정상이어도 보낸다(요약을 매일 받고 싶을 때 config로 켠다).
    """
    if always:
        return True
    if evaluation.get("exceeded"):
        return True
    return bool(mismatches)


def verdict(amount: float) -> str:
    """금액을 '손해/이득'으로 명시. 부호만 쓰면 규약을 아는 사람만 읽을 수 있다."""
    if abs(amount) < 0.005:
        return f"{FAVOR} 0.00 (차이 없음)"
    return (f"{ADVERSE} {amount:.2f} 손해" if amount > 0
            else f"{FAVOR} {abs(amount):.2f} 이득")


def _account(rec: dict) -> str:
    """db 파일명에서 계좌 구분 — 같은 전략의 계좌별 A/B를 읽으려면 필요하다."""
    return "sub" if "sub" in str(rec.get("db", "")) else "main"


def _verdict_header(evaluation: dict, mismatches: list | None) -> list[str]:
    """메시지 맨 위 판정 블록 — 숫자를 읽기 전에 OK/이탈이 보여야 한다."""
    out = []
    cost_pct, ratio = evaluation.get("cost_pct"), evaluation.get("ratio")
    assumed = evaluation.get("assumed_pct")
    if cost_pct is None:
        out.append("판정: ⚪ 비용 보류 — 노셔널 미확인(amount 결손)")
    elif evaluation.get("exceeded"):
        out.append(f"판정: {ADVERSE} <b>비용 이탈</b> — 편도 {cost_pct:.3f}%"
                   f" (가정 {assumed:.2f}%의 {ratio:.1f}배)")
    else:
        out.append(f"판정: {FAVOR} 비용 정상 — 편도 {cost_pct:.3f}%"
                   f" (가정 {assumed:.2f}% 이내, {ratio:.1f}배)")
    if cost_pct is not None:
        out.append(f"        수수료 {evaluation.get('fee_pct', 0):.3f}%"
                   f" · 슬리피지 {evaluation.get('slip_pct', 0):.3f}%")
    if mismatches:
        out.append(f"      {ADVERSE} <b>진입 불일치 {len(mismatches)}건</b>")
        for m in mismatches[:6]:
            out.append(f"        {m.get('date', '')} {m.get('strategy_id', m.get('kind', ''))}"
                       f" {m.get('pair', '')} — {m.get('reason', '')}")
    else:
        out.append(f"      {FAVOR} 진입 일치")
    return out


def build_message(prefix: str, ts_label: str, summary: dict,
                  evaluation: dict | None = None,
                  mismatches: list | None = None) -> str | None:
    """텔레그램 메시지(HTML). 알릴 체결이 없으면 **None** — 침묵 = 정상."""
    groups = summary.get("groups") or {}
    if not groups:
        return None

    lines = [f"{prefix} 💰 체결 비용 요약", ts_label]
    if evaluation is not None:
        lines.append("")
        lines.extend(_verdict_header(evaluation, mismatches))
    for kind in sorted(groups):
        g = groups[kind]
        avg = sum(g["pcts"]) / len(g["pcts"]) if g["pcts"] else 0.0
        head = (f"\n<b>{KIND_LABEL.get(kind, kind)}</b>"
                f" 진입 {g['n_entry']} · 청산 {g['n_exit']}"
                f"\n  수수료 {g['fees']:.2f}")
        if g["funding"]:
            head += f" · 펀딩 {g['funding']:+.2f}"
        # 평균도 부호만 쓰면 모호하다 — 금액과 같은 방식으로 방향을 단어로 붙인다
        avg_dir = "차이 없음" if abs(avg) < 0.0005 else ("불리" if avg > 0 else "유리")
        head += (f"\n  백테스트 대비 {verdict(g['slip_usdt'])}"
                 f" (평균 {abs(avg):.3f}% {avg_dir})")
        lines.append(head)
        for r in sorted(g["items"], key=lambda x: str(x.get("ts_et"))):
            adv = adverse_pct(r)
            lines.append(
                f"    {str(r.get('ts_et'))[11:16]} [{_account(r)}]"
                f" {str(r.get('pair', '')).split('/')[0]} {r.get('leg')}"
                f" {float(r['fill_price']):.2f} ← 기준 {float(r['expected_price']):.2f}"
                f" {ADVERSE if adv > 0 else FAVOR} {abs(adv):.3f}%"
            )

    fee, slip = summary["total_fee"], summary["total_slip"]
    total = fee + slip
    lines.append("\n━━━━━━━━━━━━━━━━")
    lines.append(f"수수료·펀딩  {fee:.2f}  (항상 비용)")
    lines.append(f"슬리피지    {verdict(slip)}")
    lines.append(f"{ADVERSE if total > 0 else FAVOR} "
                 f"<b>총 {'비용' if total > 0 else '이득'} {abs(total):.2f} USDT</b>")
    lines.append("  (USDT 절대액은 레버리지 노셔널에 비례)")
    return "\n".join(lines)
