"""Project ownership metadata for staged browser uploads."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")


def _owner_path(upload_root: Path, upload_id: str) -> Path:
    if not UPLOAD_ID_PATTERN.fullmatch(str(upload_id or "")):
        raise ValueError("invalid upload id")
    return Path(upload_root) / f".{upload_id}.owner.json"


def write_upload_owner(
    upload_root: Path,
    upload_id: str,
    *,
    project_id: str,
    user_id: str,
) -> None:
    """Persist non-secret ownership beside, rather than inside, the payload tree."""

    _owner_path(upload_root, upload_id).write_text(
        json.dumps(
            {
                "version": 1,
                "project_id": str(project_id or ""),
                "created_by_user_id": str(user_id or ""),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def read_upload_owner(upload_root: Path, upload_id: str) -> dict[str, Any] | None:
    owner_path = _owner_path(upload_root, upload_id)
    if not owner_path.is_file():
        return None
    try:
        value = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise PermissionError("upload ownership metadata is invalid") from exc
    try:
        version = int(value.get("version") or 0) if isinstance(value, dict) else 0
    except (TypeError, ValueError) as exc:
        raise PermissionError("upload ownership metadata is invalid") from exc
    if not isinstance(value, dict) or version != 1:
        raise PermissionError("upload ownership metadata is invalid")
    return value


def authorize_upload(
    upload_root: Path,
    upload_id: str,
    *,
    project_id: str,
    is_superuser: bool,
) -> None:
    """Enforce current-project ownership; only admins may use unowned legacy uploads."""

    owner = read_upload_owner(upload_root, upload_id)
    if owner is None:
        if is_superuser:
            return
        raise PermissionError("uploaded files not found")
    if not project_id or str(owner.get("project_id") or "") != project_id:
        raise PermissionError("uploaded files not found")
