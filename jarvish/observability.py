"""Structured events for everything the agent does.

Jarvish already reports itself in two ways, and both are for people: the
session activity trail the HUD renders, and `cognition.record_tool`, which
stores per-tool reliability so the agent can prefer what works. Neither is a
log. Neither survives the process, and neither can answer "how often did the
github server drop out yesterday".

This is the third thing: an append-only stream of typed events with a fixed
shape, cheap enough to leave on. Each event carries a timestamp, the session
and request it belongs to, the capability involved, a duration, an outcome and
- when something failed - one of the categories from `errors.py`. That is what
makes them countable.

Two rules hold.

**Nothing sensitive is ever written.** Every payload goes through
`security.redact` on the way in, so a token in an argument or an API key in an
error message never reaches the file. This is a log, and logs get pasted into
issues.

**Logging cannot break the thing being logged.** Every entry point swallows its
own errors. A full disk, a locked file or a value that will not serialise must
not take down a tool call that was otherwise going to work.

The file is JSON Lines under `data/`, rotated by size, and readers get it back
through `recent()`.
"""

import json
import os
import threading
import time
from collections import deque

from . import security
from .util import DATA_DIR

LOG_PATH = DATA_DIR / "agent-events.jsonl"

# Rotate at 4 MB, keeping one previous file. Big enough for weeks of ordinary
# use, small enough that nothing has to stream it to read it.
MAX_BYTES = 4 * 1024 * 1024

# The last events stay in memory as well, so the HUD and the tests can read
# them without touching the disk at all.
_recent = deque(maxlen=400)
_lock = threading.Lock()

# Set false to keep the in-memory ring but stop writing to disk.
ENABLED = os.environ.get("JARVISH_EVENT_LOG", "1") == "1"

# Every event type this module knows how to emit. Named as a closed set so a
# typo becomes a visible unknown rather than a silently new category.
AGENT_STARTED = "agent_started"
AGENT_FINISHED = "agent_finished"
TOOL_SELECTED = "tool_selected"
TOOL_STARTED = "tool_started"
TOOL_FINISHED = "tool_finished"
TOOL_FAILED = "tool_failed"
CONFIRMATION_REQUESTED = "confirmation_requested"
CONFIRMATION_GRANTED = "confirmation_granted"
CONFIRMATION_DENIED = "confirmation_denied"
MCP_CONNECTED = "mcp_connected"
MCP_DISCONNECTED = "mcp_disconnected"
MCP_ERROR = "mcp_error"

EVENTS = (
    AGENT_STARTED, AGENT_FINISHED, TOOL_SELECTED, TOOL_STARTED,
    TOOL_FINISHED, TOOL_FAILED, CONFIRMATION_REQUESTED,
    CONFIRMATION_GRANTED, CONFIRMATION_DENIED,
    MCP_CONNECTED, MCP_DISCONNECTED, MCP_ERROR,
)


def _rotate():
    """Move the log aside once it is large enough. Never raises."""
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_BYTES:
            previous = LOG_PATH.with_suffix(".jsonl.1")
            if previous.exists():
                previous.unlink()
            LOG_PATH.rename(previous)
    except Exception:
        pass


def emit(event, session=None, request=None, tool=None, ms=None, ok=None,
         error_category=None, **fields):
    """Record one structured event.

    Returns the entry, so a caller can attach it to something else without
    building the dict twice. Failures here are swallowed on purpose: a log that
    can break a tool call is worse than no log.
    """
    entry = {
        "at": round(time.time(), 3),
        "event": str(event),
        "session": session,
        "request": request,
        "tool": tool,
    }
    if ms is not None:
        entry["ms"] = ms
    if ok is not None:
        entry["ok"] = bool(ok)
    if error_category:
        entry["error_category"] = error_category

    try:
        entry.update(security.redact(fields))
    except Exception:
        entry["detail"] = "unserialisable"

    with _lock:
        _recent.append(entry)
        if not ENABLED:
            return entry
        try:
            _rotate()
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass
    return entry


def recent(limit=100, event=None, session=None):
    """The most recent events, newest last. Read from memory, never the disk."""
    with _lock:
        rows = list(_recent)
    if event:
        wanted = {event} if isinstance(event, str) else set(event)
        rows = [r for r in rows if r["event"] in wanted]
    if session:
        rows = [r for r in rows if r.get("session") == session]
    return rows[-limit:]


def summary():
    """Counts by event type and by error category, for a status panel."""
    with _lock:
        rows = list(_recent)
    events, failures = {}, {}
    durations = []
    for row in rows:
        events[row["event"]] = events.get(row["event"], 0) + 1
        category = row.get("error_category")
        if category:
            failures[category] = failures.get(category, 0) + 1
        if row["event"] == TOOL_FINISHED and isinstance(row.get("ms"), (int, float)):
            durations.append(row["ms"])
    durations.sort()
    return {
        "events": events,
        "failures": failures,
        "tracked": len(rows),
        "median_tool_ms": durations[len(durations) // 2] if durations else None,
        "log": str(LOG_PATH),
        "enabled": ENABLED,
    }


def clear():
    """Drop the in-memory ring. The file on disk is left alone."""
    with _lock:
        _recent.clear()
