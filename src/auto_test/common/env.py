"""Environment-variable access for the automation testing platform."""

from __future__ import annotations

import os
from typing import TypeVar


T = TypeVar("T")
CURRENT_PREFIX = "LIEMA_"


def get_env(name: str, default: T | None = None) -> str | T | None:
    return os.environ.get(f"{CURRENT_PREFIX}{name}", default)


def has_env(name: str) -> bool:
    return bool(get_env(name))


def set_env_default(name: str, value: str) -> str:
    existing = get_env(name)
    if existing is not None:
        return str(existing)
    key = f"{CURRENT_PREFIX}{name}"
    os.environ[key] = value
    return value
