"""Shared helpers for evaluation backends."""

from __future__ import annotations

from auto_test.evaluation.contracts import BackendEvent, EventCallback


def emit(
    callback: EventCallback | None,
    event_type: str,
    message: str,
    *,
    phase: str = "",
    progress: int | None = None,
    **data,
) -> None:
    if callback is not None:
        callback(
            BackendEvent(
                event_type=event_type,
                message=message,
                phase=phase,
                progress=progress,
                data=data,
            )
        )
