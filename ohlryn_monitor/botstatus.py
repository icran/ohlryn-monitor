"""봇 현황 요약 — 순수 로직 (I/O 없음).

봇 /api/v1/bots 응답(엔진 목록)을 상태 대시보드 표시용으로 요약한다:
어떤 봇이 떠 있고, 어떤 전략(config)이 어떤 페어로 돌고 있으며, 문제(중지/정체)는 없는지.

중지/정체 판정 기준은 health.bot_issues와 동일 철학:
last_updated는 캔들 '오픈시각' 라벨이라 자연 지연이 있음 — 임계(stale_minutes)는 버퍼 포함.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _parse_ts(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        return None


def summarize_bot(
    name: str,
    engines: list[dict] | None,
    *,
    now: datetime,
    stale_minutes: int,
    error: str | None = None,
) -> dict:
    """봇 1개 요약.

    반환: {"name", "status": ok|warn|down, "detail", "strategies": [
        {"config", "pair_names", "stopped", "stale", "status": ok|warn}, ...]}
    - engines=None(+error) / 빈 목록 → down
    - 엔진을 전략(config_name)별로 묶고, 중지·정체 페어가 있으면 해당 전략과 봇을 warn
    """
    if engines is None:
        return {
            "name": name,
            "status": "down",
            "detail": f"API 응답 없음 ({error or 'unknown'})",
            "strategies": [],
        }
    if not engines:
        return {"name": name, "status": "down", "detail": "엔진 목록 비어있음", "strategies": []}

    groups: dict[str, dict] = {}
    for e in engines:
        key = e.get("config_name") or e.get("strategy") or "전략"
        g = groups.setdefault(key, {"config": key, "pair_names": [], "stopped": [], "stale": []})
        pair = e.get("pair", "?")
        g["pair_names"].append(pair)
        if not e.get("is_running"):
            g["stopped"].append(pair)
        ts = _parse_ts(e["last_updated"]) if e.get("last_updated") else None
        if ts and (now - ts).total_seconds() > stale_minutes * 60:
            g["stale"].append(pair)

    strategies = []
    for g in groups.values():
        g["status"] = "warn" if (g["stopped"] or g["stale"]) else "ok"
        strategies.append(g)

    n_warn = sum(1 for s in strategies if s["status"] == "warn")
    n_pairs = sum(len(s["pair_names"]) for s in strategies)
    detail = (
        f"문제 전략 {n_warn}개"
        if n_warn
        else f"전략 {len(strategies)}개 · {n_pairs}페어 실행 중"
    )
    return {
        "name": name,
        "status": "warn" if n_warn else "ok",
        "detail": detail,
        "strategies": strategies,
    }
