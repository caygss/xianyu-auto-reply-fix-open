"""Resolve files relative to the source tree or compiled Windows app root."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def ensure_standard_streams(stream_module=sys) -> None:
    """Provide writable streams for PyInstaller's windowed mode."""
    for stream_name in ("stdout", "stderr"):
        if getattr(stream_module, stream_name, None) is None:
            setattr(
                stream_module,
                stream_name,
                open(os.devnull, "w", encoding="utf-8"),
            )


def app_root() -> Path:
    configured_root = str(os.getenv("XIANYU_APP_ROOT") or "").strip()
    if configured_root:
        return Path(configured_root).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent
