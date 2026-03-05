"""helpers for loading repository-level runtime configuration."""

from __future__ import annotations

from pathlib import Path


DEFAULT_OPENCODE_MODEL = "openai/gpt-5.1-codex-mini"


def _config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config.yaml"


def get_opencode_model() -> str:
    """read `model` from config.yaml; fallback to default when unavailable."""
    try:
        config_text = _config_path().read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_OPENCODE_MODEL

    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("model:"):
            continue
        value = line.split(":", 1)[1].strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        if value:
            return value

    return DEFAULT_OPENCODE_MODEL
