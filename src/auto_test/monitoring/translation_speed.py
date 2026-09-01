"""Translation throughput derived from persisted task progress events."""

from __future__ import annotations

import re
from typing import Any, Iterable


_TRANSLATION_PROGRESS_RE = re.compile(
    r"翻译(?:进度|完成)\s*[：:]\s*(\d+)\s*/\s*(\d+)"
)


def summarize_translation_speed(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return an auditable average translation rate from progress timestamps.

    The rate is the sum of positive completed-item deltas divided by the sum of
    their observed wall-clock intervals. A lower current count or a changed
    total starts a new segment, so multiple users/batches are not mixed into a
    single artificial interval.
    """

    points: list[dict[str, Any]] = []
    for event in events:
        match = _TRANSLATION_PROGRESS_RE.search(str(event.get("message") or ""))
        if not match:
            continue
        try:
            created_at = float(event.get("created_at"))
            current = int(match.group(1))
            total = int(match.group(2))
            event_id = int(event.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if created_at < 0 or current < 0 or total <= 0 or current > total:
            continue
        points.append(
            {
                "created_at": created_at,
                "current": current,
                "total": total,
                "id": event_id,
            }
        )

    points.sort(key=lambda item: (item["created_at"], item["id"]))
    if not points:
        return {
            "state": "waiting",
            "items_per_minute": None,
            "translated_count": 0,
            "elapsed_seconds": 0.0,
            "sample_count": 0,
            "interval_count": 0,
            "segment_count": 0,
            "current": None,
            "total": None,
            "started_at": None,
            "updated_at": None,
        }

    translated_count = 0
    elapsed_seconds = 0.0
    interval_count = 0
    segment_count = 1
    previous = points[0]

    for point in points[1:]:
        if point["total"] != previous["total"] or point["current"] < previous["current"]:
            segment_count += 1
            previous = point
            continue

        elapsed = point["created_at"] - previous["created_at"]
        if elapsed > 0:
            translated_count += point["current"] - previous["current"]
            elapsed_seconds += elapsed
            interval_count += 1
        previous = point

    latest = points[-1]
    state = "available" if interval_count else "measuring"
    items_per_minute = None
    if interval_count and elapsed_seconds > 0:
        items_per_minute = round(translated_count * 60.0 / elapsed_seconds, 2)

    return {
        "state": state,
        "items_per_minute": items_per_minute,
        "translated_count": translated_count,
        "elapsed_seconds": round(elapsed_seconds, 2),
        "sample_count": len(points),
        "interval_count": interval_count,
        "segment_count": segment_count,
        "current": latest["current"],
        "total": latest["total"],
        "started_at": points[0]["created_at"],
        "updated_at": latest["created_at"],
    }


def format_duration(seconds: float | int | None) -> str:
    """Format a duration for the Chinese UI/report without hiding precision."""

    total_seconds = max(0, int(round(float(seconds or 0))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"
