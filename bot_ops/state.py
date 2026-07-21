"""JSON 상태 영속 — 시그널 알림의 직전 상태 저장/복원. I/O."""

from __future__ import annotations

import json
import os


def load_state(path: str) -> dict:
    """상태 파일 로드. 없거나 손상되면 빈 dict (첫 실행으로 취급)."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path: str, state: dict) -> None:
    """상태 파일 원자적 저장(임시파일 → rename)."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
