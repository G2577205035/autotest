"""Map EvalScope public artifacts into a stable platform result envelope."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from auto_test.evaluation.contracts import SAFE_REFERENCE_KEYS, SENSITIVE_KEY_FRAGMENTS


def _redact(value: Any, secret_values: Iterable[str] = ()) -> Any:
    secrets = tuple(str(item) for item in secret_values if str(item))
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized not in SAFE_REFERENCE_KEYS and any(
                fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS
            ):
                result[key] = (
                    "***"
                    if child is not None and child != "" and child is not False
                    else child
                )
            else:
                result[key] = _redact(child, secrets)
        return result
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            redacted = redacted.replace(secret, "***")
        return redacted
    return value


class EvalScopeResultMapper:
    schema_version = "1.0"

    def map_directory(
        self, work_dir: str | Path, *, secret_values: Iterable[str] = ()
    ) -> dict[str, Any]:
        root = Path(work_dir).resolve()
        if not root.is_dir():
            raise FileNotFoundError(root)
        json_documents: list[dict[str, Any]] = []
        jsonl_samples: list[dict[str, Any]] = []
        csv_rows = 0
        artifacts: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            artifacts.append(relative)
            suffix = path.suffix.lower()
            try:
                if suffix == ".json":
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        json_documents.append({"path": relative, "payload": payload})
                elif suffix == ".jsonl":
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        payload = json.loads(line)
                        if isinstance(payload, dict):
                            jsonl_samples.append({"path": relative, **payload})
                elif suffix == ".csv":
                    with path.open("r", encoding="utf-8-sig", newline="") as handle:
                        csv_rows += sum(1 for _ in csv.DictReader(handle))
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                continue
        primary = next(
            (
                item["payload"]
                for item in json_documents
                if item["path"].endswith(("summary.json", "result.json", "output.json"))
            ),
            json_documents[0]["payload"] if json_documents else {},
        )
        summary = primary.get("summary") if isinstance(primary.get("summary"), dict) else {}
        return _redact(
            {
                "schema_version": self.schema_version,
                "backend": "evalscope",
                "summary": summary,
                "raw_primary": primary,
                "samples": jsonl_samples,
                "artifact_files": artifacts,
                "json_document_count": len(json_documents),
                "jsonl_sample_count": len(jsonl_samples),
                "csv_row_count": csv_rows,
            },
            secret_values,
        )
