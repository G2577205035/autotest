"""Backward-compatible Web launcher; the FastAPI app lives under ``src``."""

from __future__ import annotations

import sys
from pathlib import Path


SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from auto_test import web as _web  # noqa: E402

app = _web.app


def __getattr__(name):
    """Forward legacy imports to the packaged Web module."""
    return getattr(_web, name)


if __name__ == "__main__":
    _web.main()
