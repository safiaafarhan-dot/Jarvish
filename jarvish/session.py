"""Per-session state: continuity, the activity trail, and the emergency stop.

A session is one browser tab's conversation with Jarvish. It holds more than
the transcript: the tools that ran, what the last action was (so "do that
again" means something), pending confirmations, and a cancel flag the agent
loop checks between steps so the STOP control can actually halt work mid-chain.
"""

import asyncio
import threading
import time
import uuid
from collections import deque

# Sessions are dropped after this long without traffic, so a browser left open
# overnight does not pin an unbounded amount of history in memory.
IDLE_TIMEOUT = 60 * 60 * 6
MAX_ACTIVITY = 400
MAX_TOOL_MEMORY = 40


def _running_loop():
    """The event loop on this thread, or None if there is not one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _wake(event, loop):
    """Set an asyncio.Event that may belong to a different thread's loop.

    `Event.set()` flips a flag and then schedules the waiters' callbacks — on
    the loop it was created on, from the thread that owns that loop. Called
    from any other thread it does the first half and skips the second: the flag
    reads as set, and whoever is awaiting it goes on sleeping until something
    else happens to wake the loop.

    That is not theoretical here. The agent runs on the voice worker's own loop
    while the microphone thread is what hears "jarvis, stop" and what hears a
    spoken yes; both of those answer by setting one of these events, from the
    wrong thread. Hopping through the owning loop is what makes them land.
    """
    if loop is None or loop.is_closed():
        event.set()
        return
    if _running_loop() is loop:
        event.set()
        return
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:
        # The loop shut down between the check and the call; the flag is still
        # worth setting for anyone who polls it.
        event.set()


class Session:
    def __init__(self, session_id):
        self.id = session_id
        self.created = time.time()
        self.touched = time.time()

        # Rolling log the HUD renders as its activity stream.
        self.activity = deque(maxlen=MAX_ACTIVITY)
        # Every tool call that has run this session, newest last.
        self.tool_history = deque(maxlen=MAX_TOOL_MEMORY)

        # Set when the user hits STOP; the agent loop checks it between steps.
        self.cancel = asyncio.Event()
        # Which loop `cancel` belongs to, so a stop arriving from another
        # thread can be delivered to it rather than dropped.
        self.loop = None
        # Confirmations the UI has approved, keyed by the request id we handed it.
        self.approvals = {}
        self.state = "idle"
        # Background tasks have nobody watching the screen, so they are
        # allowed to wait far longer for a confirmation than a chat turn.
        self.background = False
        self.approval_wait = None
        # A mission agent restricts itself to these capability groups.
        self.capability_groups = None

    # -- continuity ------------------------------------------------------

    def note_tool(self, name, arguments, result):
        ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
        self.tool_history.append({
            "name": name,
            "arguments": arguments,
            "ok": ok,
            "at": time.time(),
        })

    def last_tool(self, only_successful=True):
        for entry in reversed(self.tool_history):
            if entry["ok"] or not only_successful:
                return entry
        return None

    def continuity_block(self):
        """A short recap of recent actions, folded into the system prompt.

        This is what lets "do that again" or "open the one from earlier"
        resolve without the user restating everything.
        """
        if not self.tool_history:
            return ""
        recent = list(self.tool_history)[-6:]
        lines = ["Actions you have already taken in this session, oldest first:"]
        for entry in recent:
            arguments = ", ".join(
                str(k) + "=" + str(v) for k, v in (entry["arguments"] or {}).items()
            )
            lines.append(
                "- " + entry["name"] + ("(" + arguments + ")" if arguments else "")
                + ("" if entry["ok"] else "  [failed]")
            )
        lines.append(
            'If the user says "do that again", "the same one", "continue" or similar, '
            "resolve it against this list instead of asking them to repeat themselves."
        )
        return "\n".join(lines)

    # -- activity trail --------------------------------------------------

    def log(self, kind, text, detail=None):
        entry = {
            "at": time.time(),
            "kind": kind,
            "text": text,
            "detail": detail,
        }
        self.activity.append(entry)
        return entry

    def trail(self, limit=120):
        return list(self.activity)[-limit:]

    # -- confirmation gate -----------------------------------------------

    def request_approval(self, request_id):
        """Open a pending confirmation and hand back the event to await.

        Called from inside the agent's loop, which is the loop the answer will
        have to be delivered to — so it is recorded here, while we are on it.
        """
        pending = {"event": asyncio.Event(), "approved": False,
                   "loop": _running_loop()}
        self.approvals[request_id] = pending
        return pending

    def resolve_approval(self, request_id, approved):
        """Answer a pending confirmation. False when nothing was waiting.

        The answer can arrive from the HUD's loop or from the voice thread, so
        the event is woken through the loop that is actually waiting on it.
        """
        pending = self.approvals.get(request_id)
        if pending is None:
            return False
        pending["approved"] = bool(approved)
        _wake(pending["event"], pending.get("loop"))
        return True

    def drop_approval(self, request_id):
        self.approvals.pop(request_id, None)

    # -- lifecycle -------------------------------------------------------

    def touch(self):
        self.touched = time.time()

    def stop(self):
        # STOP is pressed in the HUD and spoken to the microphone, neither of
        # which runs on the loop the agent is waiting on.
        _wake(self.cancel, self.loop)
        self.state = "stopped"
        # Release anything blocked on a confirmation, denying it as we go.
        for request_id in list(self.approvals):
            self.resolve_approval(request_id, False)
        self.log("stop", "Emergency stop — all activity halted")

    def rearm(self):
        self.cancel = asyncio.Event()
        # Remember the loop this belongs to, when there is one. `rearm` is
        # called once before the loop starts and again from inside it; the
        # second call is the one that binds.
        loop = _running_loop()
        if loop is not None:
            self.loop = loop

    @property
    def stopped(self):
        return self.cancel.is_set()


_sessions = {}
_lock = threading.Lock()


def get(session_id=None):
    """Fetch a session, creating it if the id is new or missing."""
    with _lock:
        _reap()
        if not session_id:
            session_id = uuid.uuid4().hex[:12]
        session = _sessions.get(session_id)
        if session is None:
            session = Session(session_id)
            _sessions[session_id] = session
        session.touch()
        return session


def peek(session_id):
    """Fetch a session without creating one. None when it does not exist."""
    with _lock:
        return _sessions.get(session_id)


def _reap():
    cutoff = time.time() - IDLE_TIMEOUT
    for key in [k for k, v in _sessions.items() if v.touched < cutoff]:
        _sessions.pop(key, None)


def count():
    with _lock:
        return len(_sessions)
