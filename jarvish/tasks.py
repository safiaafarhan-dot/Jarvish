"""Durable background tasks, and the scheduler that runs them.

A task is a piece of work Jarvish does without the user waiting on it: watch a
deployment, re-index a folder every hour, check something in ten minutes. It
survives a restart, because the whole lifecycle lives in SQLite rather than in
memory.

    pending -> running -> completed
                  |  \\-> failed  -> (retry) -> pending
                  |  \\-> cancelled
                  \\--> paused   -> (resume) -> pending

**No safety bypass.** A task runs through `llm.run_agent`, exactly like a chat
turn, so it goes through the same planner, capability selection, risk grading
and confirmation gate. When a task hits a gated action it does not proceed and
it does not quietly skip: it parks in `paused`, raises a high-priority
notification asking for permission, and waits. Emergency stop cancels running
tasks the same way it cancels an interactive chain.

The runner is one background thread with its own event loop. Nothing here
blocks the web server, and the chat stays responsive while tasks run.
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from collections import deque

from .util import DATA_DIR, as_bool, as_int, boolean, err, integer, ok, string, tool

DB_PATH = DATA_DIR / "tasks.db"

PENDING, RUNNING, PAUSED = "pending", "running", "paused"
COMPLETED, FAILED, CANCELLED = "completed", "failed", "cancelled"
ACTIVE = (PENDING, RUNNING, PAUSED)

# How long a background task may wait for the user to approve a risky step.
# Much longer than the interactive gate, because nobody is watching the screen.
APPROVAL_WAIT = 30 * 60

# The runner wakes this often when there is work in flight, and backs off
# to IDLE_TICK when there is none. Polling an empty queue twice a second
# around the clock is pure waste on a memory-bound machine.
TICK = 2.0
IDLE_TICK = 10.0

# A task that runs longer than this is abandoned, so one wedged run cannot hold
# the queue forever.
DEFAULT_TIMEOUT = 15 * 60

# Identifies this process in the shared task table, so a runner only
# recovers work it owned and claims are attributable.
OWNER = "pid:" + str(os.getpid())

_lock = threading.RLock()
_runner = {"thread": None, "stop": None, "loop": None,
           "errors": deque(maxlen=20)}
_running = {}      # task id -> the session driving it, so STOP can reach them


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        title TEXT,
        prompt TEXT,
        state TEXT,
        progress TEXT,
        created REAL,
        next_run REAL,
        every REAL,
        runs INTEGER DEFAULT 0,
        max_runs INTEGER,
        attempts INTEGER DEFAULT 0,
        max_attempts INTEGER DEFAULT 2,
        timeout REAL,
        started REAL,
        finished REAL,
        result TEXT,
        error TEXT,
        needs TEXT,
        session TEXT,
        depends_on TEXT,
        owner TEXT
    );
    CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state, next_run);
    """)
    # `owner` arrived after the first schema; add it to existing databases.
    columns = {row["name"] for row in
               connection.execute("PRAGMA table_info(tasks)").fetchall()}
    if "owner" not in columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN owner TEXT")
    connection.commit()
    return connection


def _row(row):
    if row is None:
        return None
    task = dict(row)
    for key in ("result", "needs"):
        if task.get(key):
            try:
                task[key] = json.loads(task[key])
            except (TypeError, ValueError):
                pass
    return task


def _get(connection, task_id):
    return _row(connection.execute(
        "SELECT * FROM tasks WHERE id = ? OR title = ? COLLATE NOCASE LIMIT 1",
        (task_id, task_id)).fetchone())


def _set(connection, task_id, **fields):
    if not fields:
        return
    columns = ", ".join(key + " = ?" for key in fields)
    connection.execute("UPDATE tasks SET " + columns + " WHERE id = ?",
                       list(fields.values()) + [task_id])
    connection.commit()


# --------------------------------------------------------------------------
# Creating and controlling
# --------------------------------------------------------------------------

def create(prompt, title=None, delay=None, every=None, max_runs=None,
           timeout=None, depends_on=None):
    """Schedule work. Runs once by default, or repeatedly when `every` is given."""
    text = str(prompt or "").strip()
    if not text:
        return err("A task needs something to do.")

    now = time.time()
    wait = float(as_int(delay, 0, 0, 60 * 60 * 24 * 30)) if delay else 0.0
    interval = float(as_int(every, 0, 0, 60 * 60 * 24 * 7)) if every else None
    if interval is not None and interval < 30:
        return err("The shortest repeat interval is 30 seconds.")

    task_id = "t" + uuid.uuid4().hex[:10]
    record = {
        "id": task_id,
        "title": (title or text)[:80],
        "prompt": text,
        "state": PENDING,
        "progress": "queued",
        "created": now,
        "next_run": now + wait,
        "every": interval,
        "runs": 0,
        "max_runs": as_int(max_runs, 0, 0, 10000) or None,
        "attempts": 0,
        "max_attempts": 2,
        "timeout": float(as_int(timeout, DEFAULT_TIMEOUT, 10, 3600)),
        "started": None, "finished": None, "result": None, "error": None,
        "needs": None, "session": None,
        "depends_on": str(depends_on) if depends_on else None,
    }
    with _lock:
        connection = _connect()
        try:
            connection.execute(
                "INSERT INTO tasks (id,title,prompt,state,progress,created,next_run,"
                "every,runs,max_runs,attempts,max_attempts,timeout,started,finished,"
                "result,error,needs,session,depends_on) VALUES "
                "(:id,:title,:prompt,:state,:progress,:created,:next_run,:every,:runs,"
                ":max_runs,:attempts,:max_attempts,:timeout,:started,:finished,:result,"
                ":error,:needs,:session,:depends_on)", record)
            connection.commit()
        finally:
            connection.close()

    ensure_runner()
    return ok(task=record["id"], title=record["title"],
              starts_in=round(wait), repeats_every=interval,
              note="Running in the background. The chat stays available.")


def _transition(task_id, allowed, new_state, progress, **extra):
    with _lock:
        connection = _connect()
        try:
            task = _get(connection, task_id)
            if task is None:
                return err("No task called '" + str(task_id) + "'.")
            if task["state"] not in allowed:
                return err("Task '" + task["title"] + "' is " + task["state"] +
                           ", which cannot be " + new_state + ".")
            _set(connection, task["id"], state=new_state, progress=progress, **extra)
            return ok(task=task["id"], title=task["title"], state=new_state)
        finally:
            connection.close()


def pause(task_id):
    """Stop a task running again. A run already in flight is cancelled."""
    result = _transition(task_id, (PENDING, RUNNING, PAUSED), PAUSED, "paused by the user")
    if result["ok"]:
        session = _running.get(result["task"])
        if session is not None:
            session.stop()
    return result


def resume(task_id):
    outcome = _transition(task_id, (PAUSED, FAILED), PENDING, "queued")
    if outcome["ok"]:
        with _lock:
            connection = _connect()
            try:
                _set(connection, outcome["task"], next_run=time.time(), error=None)
            finally:
                connection.close()
        ensure_runner()
    return outcome


def cancel(task_id):
    result = _transition(task_id, ACTIVE, CANCELLED, "cancelled",
                         finished=time.time())
    if result["ok"]:
        session = _running.get(result["task"])
        if session is not None:
            session.stop()
    return result


def retry(task_id):
    with _lock:
        connection = _connect()
        try:
            task = _get(connection, task_id)
            if task is None:
                return err("No task called '" + str(task_id) + "'.")
            _set(connection, task["id"], state=PENDING, progress="retrying",
                 next_run=time.time(), attempts=0, error=None)
        finally:
            connection.close()
    ensure_runner()
    return ok(task=task_id, state=PENDING, note="Queued for another attempt.")


def listing(state=None, limit=40):
    with _lock:
        connection = _connect()
        try:
            if state:
                rows = connection.execute(
                    "SELECT * FROM tasks WHERE state = ? ORDER BY created DESC LIMIT ?",
                    (str(state), as_int(limit, 40, 1, 200))).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM tasks ORDER BY "
                    " CASE state WHEN 'running' THEN 0 WHEN 'paused' THEN 1"
                    "            WHEN 'pending' THEN 2 ELSE 3 END, created DESC LIMIT ?",
                    (as_int(limit, 40, 1, 200),)).fetchall()
            tasks = [_row(r) for r in rows]
            counts = {r["state"]: r["n"] for r in connection.execute(
                "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state").fetchall()}
        finally:
            connection.close()
    return ok(tasks=tasks, count=len(tasks), states=counts,
              running=len(_running), runner=_runner["thread"] is not None)


def status(task_id):
    with _lock:
        connection = _connect()
        try:
            task = _get(connection, task_id)
        finally:
            connection.close()
    if task is None:
        return err("No task called '" + str(task_id) + "'.")
    return ok(**task)


def clear_finished():
    with _lock:
        connection = _connect()
        try:
            cursor = connection.execute(
                "DELETE FROM tasks WHERE state IN (?,?,?)",
                (COMPLETED, FAILED, CANCELLED))
            connection.commit()
        finally:
            connection.close()
    return ok(removed=cursor.rowcount)


# --------------------------------------------------------------------------
# Restart recovery
# --------------------------------------------------------------------------

def recover():
    """Put anything that was mid-flight when the process died back in the queue.

    A task marked `running` with no runner behind it is a crash artefact, not a
    live task, so it goes back to `pending` rather than being lost or left
    stuck. This is what makes tasks survive a restart.
    """
    with _lock:
        connection = _connect()
        try:
            rows = connection.execute(
                "SELECT id, title, attempts, max_attempts FROM tasks"
                " WHERE state = ? AND (owner IS NULL OR owner = ?)",
                (RUNNING, OWNER)).fetchall()
            recovered = []
            for row in rows:
                if row["attempts"] >= row["max_attempts"]:
                    _set(connection, row["id"], state=FAILED,
                         progress="abandoned after a restart",
                         error="The server restarted while this was running.",
                         finished=time.time())
                else:
                    _set(connection, row["id"], state=PENDING,
                         progress="requeued after a restart",
                         next_run=time.time() + 5)
                recovered.append(row["title"])
        finally:
            connection.close()
    return recovered


# --------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------

def _due(connection):
    now = time.time()
    rows = connection.execute(
        "SELECT * FROM tasks WHERE state = ? AND next_run <= ? ORDER BY next_run LIMIT 5",
        (PENDING, now)).fetchall()
    ready = []
    for row in rows:
        task = _row(row)
        blocker = task.get("depends_on")
        if blocker:
            other = _get(connection, blocker)
            # Hold until the dependency has actually finished successfully.
            if other is not None and other["state"] != COMPLETED:
                continue
        ready.append(task)
    return ready


def _claim(connection, task_id):
    """Move one task pending -> running, but only if nobody else already has.

    `UPDATE ... WHERE state = 'pending'` is atomic in SQLite, so exactly one
    runner can win regardless of how many processes are polling.
    """
    cursor = connection.execute(
        "UPDATE tasks SET state = ?, progress = 'starting', started = ?, owner = ?"
        " WHERE id = ? AND state = ?",
        (RUNNING, time.time(), OWNER, task_id, PENDING))
    connection.commit()
    return cursor.rowcount == 1


async def _execute(task):
    """Run one task through the ordinary agent, gate and all."""
    from . import llm, session as sessions

    # STOP (or a pause) may have landed between the runner marking this RUNNING
    # and this coroutine starting. The stored state is the authority.
    with _lock:
        connection = _connect()
        try:
            current = _get(connection, task["id"])
        finally:
            connection.close()
    if current is None or current["state"] != RUNNING:
        return "", [], None, True

    session = sessions.get("task:" + task["id"])
    session.rearm()
    session.background = True
    session.approval_wait = APPROVAL_WAIT
    _running[task["id"]] = session

    history = [{"role": "user", "content": task["prompt"]}]
    answer, tools_used, awaiting = "", [], None

    try:
        async for event in llm.run_agent(history, session=session):
            kind = event.get("type")
            if kind == "token":
                answer += event["text"]
            elif kind == "tool_start":
                tools_used.append(event["name"])
                _progress(task["id"], "running " + event["name"])
            elif kind == "confirm":
                awaiting = {"tool": event["name"], "id": event["id"],
                            "reason": event.get("reason"),
                            "risk": event.get("risk"),
                            "session": session.id}
                _progress(task["id"], "waiting for permission to " + event["name"])
                _notify_approval(task, awaiting)
            elif kind == "error":
                raise RuntimeError(event["message"])
    finally:
        _running.pop(task["id"], None)

    return answer.strip(), tools_used, awaiting, session.stopped


def _progress(task_id, text):
    with _lock:
        connection = _connect()
        try:
            _set(connection, task_id, progress=text[:120])
        finally:
            connection.close()


def _notify_approval(task, awaiting):
    from . import proactive
    proactive.raise_event(
        key="approval:" + task["id"],
        level="high",
        category="tasks",
        title="Permission needed",
        message=("Background task " + repr(task["title"]) + " wants to run " +
                 awaiting["tool"] + "."),
        suggestion=awaiting.get("reason") or "Approve or decline it in the tasks panel.",
        data={"task": task["id"], "approval": awaiting},
    )


def _finish(task, answer, tools_used, awaiting, stopped):
    now = time.time()
    with _lock:
        connection = _connect()
        try:
            current = _get(connection, task["id"])
            if current is None or current["state"] == CANCELLED:
                return
            runs = (current["runs"] or 0) + 1

            if stopped:
                _set(connection, task["id"], state=PAUSED, progress="halted",
                     finished=now, runs=runs,
                     error="Stopped before finishing.")
                return

            payload = json.dumps({"answer": answer[:4000], "tools": tools_used})
            repeats = current["every"]
            exhausted = current["max_runs"] and runs >= current["max_runs"]

            if repeats and not exhausted:
                _set(connection, task["id"], state=PENDING,
                     progress="waiting for the next run", runs=runs,
                     next_run=now + repeats, result=payload, finished=now,
                     attempts=0, error=None,
                     needs=json.dumps(awaiting) if awaiting else None)
            else:
                _set(connection, task["id"], state=COMPLETED, progress="done",
                     runs=runs, result=payload, finished=now, error=None,
                     needs=json.dumps(awaiting) if awaiting else None)

            notify = {
                "key": "task-done:" + task["id"],
                "level": "low", "category": "tasks",
                "title": "Task finished",
                "message": repr(current["title"]) + " completed.",
                "suggestion": (answer[:180] or None),
                "data": {"task": task["id"]},
            }
        finally:
            connection.close()

    # Raised outside the database lock. Holding `_lock` while taking the
    # notifier's lock is a lock-ordering hazard, and any failure here would
    # otherwise be swallowed by the runner and look like "never completed".
    try:
        from . import proactive
        proactive.raise_event(**notify)
    except Exception as exc:
        _runner["errors"].append({"at": time.time(),
                                  "error": "notify failed: " + repr(exc)[:200]})


def _fail(task, message):
    now = time.time()
    with _lock:
        connection = _connect()
        try:
            current = _get(connection, task["id"])
            if current is None or current["state"] == CANCELLED:
                return
            attempts = (current["attempts"] or 0) + 1
            if attempts < (current["max_attempts"] or 2):
                # Back off a little before trying again.
                _set(connection, task["id"], state=PENDING, attempts=attempts,
                     progress="retrying after a failure",
                     next_run=now + 20 * attempts, error=str(message)[:400])
            else:
                _set(connection, task["id"], state=FAILED, attempts=attempts,
                     progress="failed", finished=now, error=str(message)[:400])
                from . import proactive
                proactive.raise_event(
                    key="task-failed:" + task["id"],
                    level="high", category="tasks",
                    title="Task failed",
                    message=repr(current["title"]) + " failed: " + str(message)[:140],
                    suggestion="Retry it, or check the activity log.",
                    data={"task": task["id"]},
                )
        finally:
            connection.close()


def _loop(stop_event):
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _runner["loop"] = loop

    for title in recover():
        pass    # recovery is recorded in the row itself

    while not stop_event.is_set():
        try:
            with _lock:
                connection = _connect()
                try:
                    # Claim each task atomically. The database is shared, so a
                    # second process running its own runner — the web server and
                    # a test script, say — would otherwise both pick up the same
                    # task and execute it twice. A conditional UPDATE means only
                    # one runner can move it out of `pending`, and only the
                    # winner runs it.
                    ready = [task for task in _due(connection)
                             if _claim(connection, task["id"])]
                finally:
                    connection.close()

            for task in ready:
                if stop_event.is_set():
                    break
                try:
                    outcome = loop.run_until_complete(
                        asyncio.wait_for(_execute(task), timeout=task["timeout"]))
                    _finish(task, *outcome)
                except asyncio.TimeoutError:
                    _fail(task, "Timed out after " + str(round(task["timeout"])) + "s.")
                except Exception as exc:
                    _fail(task, exc)
            busy = bool(ready)
        except Exception as exc:
            # The runner must never die; a bad task is not a fatal condition.
            # But swallowing the reason silently hides real bugs, so keep it.
            _runner["errors"].append({"at": time.time(),
                                      "error": repr(exc)[:300]})
            busy = False
        # Poll briskly only while something is actually running.
        stop_event.wait(TICK if busy else IDLE_TICK)

    loop.close()
    _runner["loop"] = None


def ensure_runner():
    """Start the background runner if it is not already going."""
    with _lock:
        thread = _runner["thread"]
        if thread is not None and thread.is_alive():
            return False
        stop_event = threading.Event()
        thread = threading.Thread(target=_loop, args=(stop_event,),
                                  name="jarvish-tasks", daemon=True)
        _runner.update({"thread": thread, "stop": stop_event})
        thread.start()
        return True


def shutdown():
    with _lock:
        if _runner["stop"] is not None:
            _runner["stop"].set()
        _runner["thread"] = None


def stop_all():
    """Emergency stop: halt every task that is running or about to.

    Signalling only the sessions in `_running` leaves a gap: a task the runner
    has just marked RUNNING but has not yet begun executing is registered
    nowhere, and would carry on after STOP. So the database is authoritative
    here — anything running or queued is marked paused, and `_execute` re-checks
    that state before doing any work.
    """
    halted = []
    for task_id, session in list(_running.items()):
        session.stop()
        halted.append(task_id)

    with _lock:
        connection = _connect()
        try:
            rows = connection.execute(
                "SELECT id FROM tasks WHERE state IN (?,?)", (RUNNING, PENDING)).fetchall()
            for row in rows:
                _set(connection, row["id"], state=PAUSED, progress="halted by STOP")
                if row["id"] not in halted:
                    halted.append(row["id"])
        finally:
            connection.close()
    return halted


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def tool_create(what, title=None, in_seconds=None, every_seconds=None, times=None):
    return create(what, title=title, delay=in_seconds, every=every_seconds,
                  max_runs=times)


SCHEMAS = [
    tool("schedule_task",
         "Run something in the background, now, later, or on a repeat. Use this when "
         "the user asks you to keep an eye on something, do something in N minutes, or "
         "check something regularly. The conversation stays usable while it runs.",
         {"what": string("What to do, written as an instruction to yourself."),
          "title": string("Short name for the task."),
          "in_seconds": integer("Wait this long before the first run."),
          "every_seconds": integer("Repeat this often. Minimum 30."),
          "times": integer("Stop after this many runs.")},
         ["what"]),
    tool("list_tasks", "List background tasks and their state.",
         {"state": string("Only show tasks in this state.",
                          ["pending", "running", "paused", "completed",
                           "failed", "cancelled"])}),
    tool("task_status", "Full detail on one background task.",
         {"task": string("Task id or title.")}, ["task"]),
    tool("pause_task", "Pause a background task.",
         {"task": string("Task id or title.")}, ["task"]),
    tool("resume_task", "Resume a paused or failed background task.",
         {"task": string("Task id or title.")}, ["task"]),
    tool("cancel_task", "Cancel a background task for good.",
         {"task": string("Task id or title.")}, ["task"]),
    tool("retry_task", "Run a failed task again from the start.",
         {"task": string("Task id or title.")}, ["task"]),
]

REGISTRY = {
    "schedule_task": tool_create,
    "list_tasks": lambda state=None: listing(state),
    "task_status": lambda task: status(task),
    "pause_task": lambda task: pause(task),
    "resume_task": lambda task: resume(task),
    "cancel_task": lambda task: cancel(task),
    "retry_task": lambda task: retry(task),
}
