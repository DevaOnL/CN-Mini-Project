"""Persistent GUI configuration helpers."""

from __future__ import annotations

import json
import pathlib

from client.gui.validation import is_valid_host


CONFIG_PATH = pathlib.Path.home() / ".multiplayer_engine_config.json"
DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 9000,
    "name": "Player",
    "fps": 60,
    "interp_ms": 100,
    "show_debug": True,
}


def _coerce_int(
    value,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def normalize_config(raw_config: dict | None) -> dict:
    if not isinstance(raw_config, dict):
        raw_config = {}

    host = str(raw_config.get("host", DEFAULT_CONFIG["host"])).strip()
    if not host or not is_valid_host(host):
        host = DEFAULT_CONFIG["host"]

    name = str(raw_config.get("name", DEFAULT_CONFIG["name"])).strip()[:16]
    if not name:
        name = DEFAULT_CONFIG["name"]

    return {
        "host": host,
        "port": _coerce_int(
            raw_config.get("port", DEFAULT_CONFIG["port"]),
            DEFAULT_CONFIG["port"],
            minimum=1024,
            maximum=65535,
        ),
        "name": name,
        "fps": _coerce_int(
            raw_config.get("fps", DEFAULT_CONFIG["fps"]),
            DEFAULT_CONFIG["fps"],
            minimum=10,
            maximum=240,
        ),
        "interp_ms": _coerce_int(
            raw_config.get("interp_ms", DEFAULT_CONFIG["interp_ms"]),
            DEFAULT_CONFIG["interp_ms"],
            minimum=0,
            maximum=500,
        ),
        "show_debug": _coerce_bool(
            raw_config.get("show_debug", DEFAULT_CONFIG["show_debug"]),
            DEFAULT_CONFIG["show_debug"],
        ),
    }


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return normalize_config(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CONFIG_PATH.with_suffix(f"{CONFIG_PATH.suffix}.tmp")
    temp_path.write_text(json.dumps(normalize_config(cfg), indent=2))
    temp_path.replace(CONFIG_PATH)
