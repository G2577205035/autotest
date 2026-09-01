"""Backward-compatible CLI launcher; application code lives under ``src``."""

from __future__ import annotations

import sys
from pathlib import Path


SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from auto_test.pipeline import runner as _runner  # noqa: E402

main = _runner.main
run_pipeline = _runner.run_pipeline


def __getattr__(name):
    """Forward legacy imports to the packaged CLI module."""
    return getattr(_runner, name)


if __name__ == "__main__":
    main()
