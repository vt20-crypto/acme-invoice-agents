"""Structured JSON logging to stderr (stdout is reserved for the human-readable summary)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any


def jlog(event: str, **fields: Any) -> None:
    record = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **fields}
    print(json.dumps(record, default=str), file=sys.stderr, flush=True)
