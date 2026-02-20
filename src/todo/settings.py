from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8001},
}


def load_settings(path: Path) -> dict[str, Any]:
    settings = _deep_copy_dict(DEFAULTS)
    if path.exists():
        with open(path) as f:
            _deep_merge(settings, json.load(f))
    return settings


def _deep_copy_dict(d: dict) -> dict:
    return {k: _deep_copy_dict(v) if isinstance(v, dict) else v for k, v in d.items()}


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
