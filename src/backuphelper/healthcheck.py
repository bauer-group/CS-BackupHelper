"""Functional healthcheck — reflects last-backup success/staleness.

Reads the newest sidecar manifest's ``created_at`` and reports healthy when it
is within ``max_age_hours``. A missing manifest is treated as healthy (grace)
so a freshly started daemon that hasn't run yet is not killed; process liveness
is covered separately by the Docker ``pgrep`` probe.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _newest_created_at(data_dir: Path) -> Optional[datetime]:
    newest: Optional[datetime] = None
    for path in Path(data_dir).glob("*.manifest.json"):
        try:
            created = _parse(json.loads(path.read_text())["created_at"])
        except (OSError, ValueError, KeyError):
            continue
        if newest is None or created > newest:
            newest = created
    return newest


def is_healthy(data_dir: Path, max_age_hours: float, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    newest = _newest_created_at(data_dir)
    if newest is None:
        return True  # grace: nothing has run yet
    return now - newest <= timedelta(hours=max_age_hours)
