"""Missions: long-running work decomposed across specialised agents.

A mission is a goal too big for one turn — "find out why the deployment
fails and fix it" — broken into a graph of tasks, each run by the agent best
suited to it, with dependencies respected and the whole thing durable across a
restart.

    goal -> decompose -> task graph -> claim -> run -> verify -> synthesise

**This is not a second orchestrator.** Every task is executed by
`llm.run_agent`, the same loop a chat turn uses, so planning, capability
selection, the risk gate, retries and verification are inherited rather than
reimplemented. An agent here is a *narrowing* — a name, a slice of the
capability registry, and a brief — not a separate engine.

**Concurrency follows the rule the tool executor already uses.** Tasks whose
agents only read may run together; anything that changes the machine runs alone
and in order. Claiming is the atomic `UPDATE ... WHERE state='pending'` pattern
proven in `tasks.py`, so two runners — or a runner and a restarted server —
cannot execute the same task twice.

**Handoff is structured.** A dependent task receives a short summary of what its
prerequisites found, not their transcripts. Context stays small, which matters
on a local model.

**Nothing is elevated.** A mission cannot grant itself privileges: a gated
action pauses the task, raises a notification, and waits for the same
confirmation a chat turn would.
"""

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import deque

from .util import DATA_DIR, as_bool, as_int, boolean, err, integer, ok, string, tool

DB_PATH = DATA_DIR / "missions.db"

PENDING, RUNNING, PAUSED = "pending", "running", "paused"
COMPLETED, FAILED, CANCELLED = "completed", "failed", "cancelled"
BLOCKED, WAITING = "blocked", "waiting_approval"
TERMINAL = (COMPLETED, FAILED, CANCELLED)

OWNER = "pid:" + str(os.getpid())

# The runner wakes this often while work is in flight, and backs off to
# IDLE_TICK when there is none. Polling an empty queue every two seconds around
# the clock is pure waste on a memory-bound machine.
TICK = 2.0
IDLE_TICK = 10.0
MAX_PARALLEL = 2                 # this machine is memory-bound; two is plenty
TASK_TIMEOUT = 10 * 60
APPROVAL_WAIT = 30 * 60
MAX_TASKS = 8                    # a graph larger than this is a planning failure
RETENTION_DAYS = 7

_lock = threading.RLock()
_runner = {"thread": None, "stop": None, "errors": deque(maxlen=20)}
_active = {}                     # task id -> session, so STOP can reach it


# --------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------

# An agent is a name, the capability groups it may draw on, and a brief. The
# `mutates` flag is what decides whether it may run beside another agent.
AGENTS = {
    "research": {
        "groups": ("live", "knowledge"),
        "mutates": False,
        "brief": ("You research. Gather facts from the web and the user's indexed "
                  "material. Report findings plainly, with sources. Change nothing."),
    },
    "knowledge": {
        "groups": ("knowledge", "files"),
        "mutates": False,
        "brief": ("You search the user's own files, code and documents. Cite the "
                  "file and line for everything you report. Change nothing."),
    },
    "vision": {
        "groups": ("vision",),
        "mutates": False,
        "brief": ("You look at the screen and report what is actually there. Say "
                  "when you are reading text rather than interpreting an image."),
    },
    "browser": {
        "groups": ("browser",),
        "mutates": True,
        "brief": ("You drive the browser. Read the page before acting, and report "
                  "whether each action was verified."),
    },
    "developer": {
        "groups": ("dev", "knowledge"),
        "mutates": True,
        "brief": ("You work on code. Diagnose from evidence, propose a change "
                  "before making one, and run the tests. Never claim a fix works "
                  "because a file changed."),
    },
    "system": {
        "groups": ("desktop", "network", "proactive"),
        "mutates": True,
        "brief": ("You inspect and adjust this machine. Prefer reporting over "
                  "acting; anything disruptive will stop for approval."),
    },
    "verification": {
        "groups": ("dev", "knowledge"),
        "mutates": False,
        "brief": ("You check whether the work actually succeeded. Look for "
                  "evidence. If you cannot confirm it, say so — do not assume."),
    },
}

DEFAULT_AGENT = "research"


def agent_names():
    return sorted(AGENTS)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS missions (
        id TEXT PRIMARY KEY, goal TEXT, state TEXT, owner TEXT,
        created REAL, updated REAL, finished REAL,
        result TEXT, error TEXT, note TEXT
    );
    CREATE TABLE IF NOT EXISTS mtasks (
        id TEXT PRIMARY KEY, mission TEXT, seq INTEGER,
        title TEXT, agent TEXT, instruction TEXT, depends_on TEXT,
        state TEXT, result TEXT, error TEXT, failure_kind TEXT,
        attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 2,
        started REAL, finished REAL, latency REAL, owner TEXT,
        needs TEXT, tools TEXT
    );
    CREATE INDEX IF NOT EXISTS mtasks_mission ON mtasks(mission, state);
    CREATE TABLE IF NOT EXISTS mlog (
        mission TEXT, task TEXT, at REAL, kind TEXT, text TEXT
    );
    CREATE INDEX IF NOT EXISTS mlog_mission ON mlog(mission, at);
    """)
    connection.commit()
    return connection


def _row(row):
    if row is None:
        return None
    record = dict(row)
    for key in ("depends_on", "result", "needs", "tools"):
        if record.get(key):
            try:
                record[key] = json.loads(record[key])
            except (TypeError, ValueError):
                pass
    return record


def _log(connection, mission, kind, text, task=None):
    connection.execute(
        "INSERT INTO mlog(mission, task, at, kind, text) VALUES (?,?,?,?,?)",
        (mission, task, time.time(), kind, str(text)[:400]))
    connection.commit()


def _set_mission(connection, mission_id, **fields):
    fields["updated"] = time.time()
    columns = ", ".join(key + " = ?" for key in fields)
    connection.execute("UPDATE missions SET " + columns + " WHERE id = ?",
                       list(fields.values()) + [mission_id])
    connection.commit()


def _set_task(connection, task_id, **fields):
    columns = ", ".join(key + " = ?" for key in fields)
    connection.execute("UPDATE mtasks SET " + columns + " WHERE id = ?",
                       list(fields.values()) + [task_id])
    connection.commit()


# --------------------------------------------------------------------------
# Decomposition
# --------------------------------------------------------------------------

_PLAN_PROMPT = """Break this goal into the fewest steps that actually achieve it.

Goal: {goal}

Available agents:
{agents}

Reply with JSON only, no prose, in exactly this shape:
{{"tasks":[{{"title":"short name","agent":"one of the agents above",
"instruction":"what that agent should do, in one or two sentences",
"depends_on":[]}}]}}

Rules:
- At most {limit} tasks. Fewer is better.
- `depends_on` holds the *titles* of tasks that must finish first. Use [] for none.
- Steps that only read can be independent; steps that change something must depend
  on whatever they need to see first.
- End with a verification task using the "verification" agent when anything was changed.
"""


def _fallback_plan(goal):
    """One task, for when the model cannot produce a usable graph.

    A single-step mission is still a real mission — it persists, it is
    claimable, it can be paused. Better that than a fabricated graph.
    """
    return [{"title": "Do the work", "agent": _guess_agent(goal),
             "instruction": str(goal), "depends_on": []}]


_AGENT_HINTS = (
    ("developer", re.compile(r"\b(code|bug|test|build|error|traceback|refactor|"
                             r"repo|git|compile|fix)\b", re.I)),
    ("browser", re.compile(r"\b(browser|website|page|url|tab|deploy|form|click)\b", re.I)),
    ("vision", re.compile(r"\b(screen|see|look|visible|screenshot)\b", re.I)),
    ("knowledge", re.compile(r"\b(file|files|document|notes|project|indexed)\b", re.I)),
    ("system", re.compile(r"\b(cpu|memory|ram|disk|wifi|network|process|battery)\b", re.I)),
)


def _guess_agent(text):
    for name, pattern in _AGENT_HINTS:
        if pattern.search(str(text or "")):
            return name
    return DEFAULT_AGENT


def _extract_json(text):
    """Pull the first JSON object out of a model reply."""
    body = str(text or "")
    start = body.find("{")
    while start != -1:
        depth, index = 0, start
        while index < len(body):
            if body[index] == "{":
                depth += 1
            elif body[index] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(body[start:index + 1])
                    except json.JSONDecodeError:
                        break
            index += 1
        start = body.find("{", start + 1)
    return None


def _normalise_plan(raw, goal):
    """Turn whatever the model produced into a valid, acyclic graph."""
    tasks = (raw or {}).get("tasks") if isinstance(raw, dict) else None
    if not isinstance(tasks, list) or not tasks:
        return None

    cleaned, titles = [], {}
    for index, entry in enumerate(tasks[:MAX_TASKS]):
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or ("Step " + str(index + 1)))[:60].strip()
        instruction = str(entry.get("instruction") or "").strip()
        if not instruction:
            continue
        agent = str(entry.get("agent") or "").strip().lower()
        if agent not in AGENTS:
            agent = _guess_agent(title + " " + instruction)
        titles[title.lower()] = index
        cleaned.append({"title": title, "agent": agent,
                        "instruction": instruction,
                        "depends_on_titles": entry.get("depends_on") or []})

    if not cleaned:
        return None

    # Resolve dependency titles to indices, dropping anything unknown or
    # backwards — a cycle would deadlock the mission forever.
    for index, entry in enumerate(cleaned):
        resolved = []
        for reference in entry.pop("depends_on_titles"):
            position = titles.get(str(reference).lower().strip())
            if position is not None and position < index:
                resolved.append(position)
        entry["depends_on"] = sorted(set(resolved))
    return cleaned


def decompose(goal, use_model=True):
    """Turn a goal into a task graph. Falls back to a single task."""
    text = str(goal or "").strip()
    if not text:
        return None, "No goal given."

    if not as_bool(use_model, True):
        return _fallback_plan(text), "planner disabled"

    import asyncio
    from . import llm, session as sessions

    listing = "\n".join("- " + name + ": " + spec["brief"].split(".")[0]
                        for name, spec in AGENTS.items())
    prompt = _PLAN_PROMPT.format(goal=text, agents=listing, limit=MAX_TASKS)

    # Consult what worked for a similar goal before planning from scratch. This
    # is the point of strategy memory: a second attempt at a familiar problem
    # should start further along than the first did.
    try:
        from . import cognition
        prior = cognition.recall_strategies(text, limit=2)
        if prior.get("strategies"):
            prompt += ("\n\nApproaches that worked for similar goals before:\n" +
                       "\n".join("- " + s["approach"][:200] +
                                 (" (tools: " + ", ".join(s["tools"][:4]) + ")"
                                  if s["tools"] else "")
                                 for s in prior["strategies"]))
    except Exception:
        pass          # planning must work with or without the memory

    async def plan():
        conversation = sessions.get("plan:" + uuid.uuid4().hex[:8])
        conversation.rearm()
        answer = ""
        # Tools off: this turn is asked for a plan, not for actions.
        async for event in llm.run_agent([{"role": "user", "content": prompt}],
                                         session=conversation):
            if event.get("type") == "token":
                answer += event["text"]
            elif event.get("type") == "error":
                raise RuntimeError(event["message"])
        return answer

    try:
        answer = asyncio.run(plan())
    except Exception as exc:
        return _fallback_plan(text), "planner failed (" + str(exc)[:80] + ")"

    graph = _normalise_plan(_extract_json(answer), text)
    if not graph:
        return _fallback_plan(text), "planner returned no usable graph"
    return graph, None


# --------------------------------------------------------------------------
# Creating missions
# --------------------------------------------------------------------------

def create(goal, plan=None, autostart=True, use_model=True):
    """Start a mission. `plan` skips decomposition, which the tests rely on."""
    text = str(goal or "").strip()
    if not text:
        return err("A mission needs a goal.")

    note = None
    if plan is None:
        plan, note = decompose(text, use_model=use_model)
        if plan is None:
            return err(note or "Could not plan this mission.")
    else:
        plan = _normalise_plan({"tasks": plan}, text) or _fallback_plan(text)

    mission_id = "m" + uuid.uuid4().hex[:10]
    now = time.time()

    with _lock:
        connection = _connect()
        try:
            connection.execute(
                "INSERT INTO missions(id, goal, state, owner, created, updated,"
                " finished, result, error, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (mission_id, text, RUNNING if as_bool(autostart, True) else PAUSED,
                 None, now, now, None, None, None, note))
            identifiers = []
            for index, entry in enumerate(plan):
                task_id = mission_id + "-t" + str(index)
                identifiers.append(task_id)
            for index, entry in enumerate(plan):
                depends = [identifiers[position] for position in entry["depends_on"]]
                connection.execute(
                    "INSERT INTO mtasks(id, mission, seq, title, agent, instruction,"
                    " depends_on, state, attempts, max_attempts) "
                    "VALUES (?,?,?,?,?,?,?,?,0,2)",
                    (identifiers[index], mission_id, index, entry["title"],
                     entry["agent"], entry["instruction"], json.dumps(depends),
                     PENDING))
            _log(connection, mission_id, "created",
                 "Mission created with " + str(len(plan)) + " task(s).")
            connection.commit()
        finally:
            connection.close()

    ensure_runner()
    return ok(mission=mission_id, goal=text, tasks=len(plan), note=note,
              graph=[{"title": e["title"], "agent": e["agent"],
                      "depends_on": e["depends_on"]} for e in plan],
              state=RUNNING if as_bool(autostart, True) else PAUSED)


# --------------------------------------------------------------------------
# Reading state
# --------------------------------------------------------------------------

def _progress(tasks):
    if not tasks:
        return 0
    done = sum(1 for t in tasks if t["state"] in TERMINAL)
    return round(done / len(tasks) * 100)


def status(mission_id, include_log=False):
    with _lock:
        connection = _connect()
        try:
            mission = _row(connection.execute(
                "SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone())
            if mission is None:
                return err("No mission called '" + str(mission_id) + "'.")
            tasks = [_row(r) for r in connection.execute(
                "SELECT * FROM mtasks WHERE mission = ? ORDER BY seq",
                (mission_id,)).fetchall()]
            entries = []
            if include_log:
                entries = [dict(r) for r in connection.execute(
                    "SELECT at, task, kind, text FROM mlog WHERE mission = ?"
                    " ORDER BY at DESC LIMIT 60", (mission_id,)).fetchall()]
        finally:
            connection.close()

    current = next((t for t in tasks if t["state"] == RUNNING), None)
    waiting = [t for t in tasks if t["state"] == WAITING]
    return ok(
        mission=mission["id"], goal=mission["goal"], state=mission["state"],
        progress=_progress(tasks),
        tasks=[{
            "id": t["id"], "title": t["title"], "agent": t["agent"],
            "state": t["state"], "depends_on": t["depends_on"] or [],
            "attempts": t["attempts"], "failure_kind": t["failure_kind"],
            "latency": t["latency"], "error": t["error"],
            "tools": t.get("tools") or [],
            "result": (t["result"] or {}).get("summary")
                      if isinstance(t["result"], dict) else None,
        } for t in tasks],
        current=current["title"] if current else None,
        current_agent=current["agent"] if current else None,
        awaiting_approval=[{"task": t["id"], "title": t["title"],
                            "needs": t["needs"]} for t in waiting],
        result=mission["result"], error=mission["error"], note=mission["note"],
        created=mission["created"], finished=mission["finished"],
        log=entries,
    )


def listing(state=None, limit=25):
    with _lock:
        connection = _connect()
        try:
            if state:
                rows = connection.execute(
                    "SELECT * FROM missions WHERE state = ? ORDER BY created DESC"
                    " LIMIT ?", (state, as_int(limit, 25, 1, 100))).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM missions ORDER BY "
                    " CASE state WHEN 'running' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,"
                    " created DESC LIMIT ?", (as_int(limit, 25, 1, 100),)).fetchall()
            missions = []
            for row in rows:
                mission = _row(row)
                tasks = [_row(r) for r in connection.execute(
                    "SELECT state, title, agent FROM mtasks WHERE mission = ?"
                    " ORDER BY seq", (mission["id"],)).fetchall()]
                mission["progress"] = _progress(tasks)
                mission["task_count"] = len(tasks)
                mission["agents"] = sorted({t["agent"] for t in tasks})
                missions.append(mission)
            counts = {r["state"]: r["n"] for r in connection.execute(
                "SELECT state, COUNT(*) AS n FROM missions GROUP BY state").fetchall()}
        finally:
            connection.close()
    return ok(missions=missions, count=len(missions), states=counts,
              running=len(_active), runner=_runner["thread"] is not None)


# --------------------------------------------------------------------------
# Control
# --------------------------------------------------------------------------

def _control(mission_id, allowed, new_state, note):
    with _lock:
        connection = _connect()
        try:
            mission = _row(connection.execute(
                "SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone())
            if mission is None:
                return err("No mission called '" + str(mission_id) + "'.")
            if mission["state"] not in allowed:
                return err("Mission is " + mission["state"] + ", which cannot be " +
                           new_state + ".")
            _set_mission(connection, mission_id, state=new_state)
            _log(connection, mission_id, "state", note)

            if new_state == CANCELLED:
                # Cancellation propagates to *every* task that has not already
                # finished — including ones still pending. Leaving those behind
                # would let a cancelled mission resume work later.
                connection.execute(
                    "UPDATE mtasks SET state = ?, owner = NULL, finished = ?"
                    " WHERE mission = ? AND state NOT IN (?,?,?)",
                    (CANCELLED, time.time(), mission_id,
                     COMPLETED, FAILED, CANCELLED))
                connection.commit()
            elif new_state == PAUSED:
                # A pause must be resumable, so work in flight goes back to
                # pending rather than being destroyed. Anything mid-claim is
                # released so a resume does not find it stuck in `running`.
                connection.execute(
                    "UPDATE mtasks SET state = ?, owner = NULL"
                    " WHERE mission = ? AND state IN (?,?)",
                    (PENDING, mission_id, RUNNING, WAITING))
                connection.commit()
        finally:
            connection.close()

    if new_state in (PAUSED, CANCELLED):
        for task_id, session in list(_active.items()):
            if task_id.startswith(mission_id):
                session.stop()
    return ok(mission=mission_id, state=new_state)


def pause(mission_id):
    return _control(mission_id, (RUNNING, PENDING), PAUSED, "Paused by the user.")


def resume(mission_id):
    outcome = _control(mission_id, (PAUSED, FAILED), RUNNING, "Resumed.")
    if outcome["ok"]:
        ensure_runner()
    return outcome


def cancel(mission_id):
    outcome = _control(mission_id, (RUNNING, PENDING, PAUSED, WAITING), CANCELLED,
                       "Cancelled by the user.")
    if outcome["ok"]:
        with _lock:
            connection = _connect()
            try:
                _set_mission(connection, mission_id, finished=time.time())
            finally:
                connection.close()
    return outcome


def retry_task(task_id):
    with _lock:
        connection = _connect()
        try:
            task = _row(connection.execute(
                "SELECT * FROM mtasks WHERE id = ?", (task_id,)).fetchone())
            if task is None:
                return err("No task called '" + str(task_id) + "'.")
            _set_task(connection, task_id, state=PENDING, attempts=0, error=None,
                      failure_kind=None, owner=None)
            _set_mission(connection, task["mission"], state=RUNNING, error=None)
            _log(connection, task["mission"], "retry", "Retrying " + task["title"],
                 task=task_id)
        finally:
            connection.close()
    ensure_runner()
    return ok(task=task_id, state=PENDING)


def stop_all():
    """Emergency stop: pause every live mission and halt its tasks."""
    halted = []
    for task_id, session in list(_active.items()):
        session.stop()
        halted.append(task_id)
    with _lock:
        connection = _connect()
        try:
            rows = connection.execute(
                "SELECT id FROM missions WHERE state IN (?,?)",
                (RUNNING, PENDING)).fetchall()
            for row in rows:
                _set_mission(connection, row["id"], state=PAUSED)
                _log(connection, row["id"], "stop", "Halted by emergency stop.")
            # The database is authoritative, not the in-memory map: a task
            # between "claimed" and "started" is not in `_active` yet.
            connection.execute(
                "UPDATE mtasks SET state = ?, owner = NULL WHERE state IN (?,?)",
                (PENDING, RUNNING, WAITING))
            connection.commit()
            stopped = [row["id"] for row in rows]
        finally:
            connection.close()
    return {"missions": stopped, "tasks": halted}


# --------------------------------------------------------------------------
# Recovery
# --------------------------------------------------------------------------

def recover():
    """Requeue work this process was running when it died."""
    with _lock:
        connection = _connect()
        try:
            rows = connection.execute(
                "SELECT id, mission, attempts, max_attempts FROM mtasks"
                " WHERE state = ? AND (owner IS NULL OR owner = ?)",
                (RUNNING, OWNER)).fetchall()
            recovered = []
            for row in rows:
                if row["attempts"] >= row["max_attempts"]:
                    _set_task(connection, row["id"], state=FAILED,
                              failure_kind="restart",
                              error="The server restarted while this was running.",
                              finished=time.time(), owner=None)
                else:
                    _set_task(connection, row["id"], state=PENDING, owner=None)
                recovered.append(row["id"])
                _log(connection, row["mission"], "recovery",
                     "Requeued after a restart.", task=row["id"])
        finally:
            connection.close()
    return recovered


def prune(days=RETENTION_DAYS):
    """Drop finished missions past their retention window."""
    cutoff = time.time() - float(as_int(days, RETENTION_DAYS, 0, 365)) * 86400
    with _lock:
        connection = _connect()
        try:
            rows = connection.execute(
                "SELECT id FROM missions WHERE state IN (?,?,?) AND"
                " COALESCE(finished, updated) < ?",
                (COMPLETED, FAILED, CANCELLED, cutoff)).fetchall()
            for row in rows:
                connection.execute("DELETE FROM mtasks WHERE mission = ?", (row["id"],))
                connection.execute("DELETE FROM mlog WHERE mission = ?", (row["id"],))
                connection.execute("DELETE FROM missions WHERE id = ?", (row["id"],))
            connection.commit()
        finally:
            connection.close()
    return ok(removed=len(rows), older_than_days=days)


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

_TRANSIENT = re.compile(r"timed out|timeout|temporarily|connection|refused|"
                        r"unreachable|reset by peer|try again", re.IGNORECASE)


def _classify_failure(message, stopped, declined):
    if stopped:
        return "cancelled"
    if declined:
        return "permission_denied"
    body = str(message or "")
    if "timed out" in body.lower() or "timeout" in body.lower():
        return "timeout"
    if _TRANSIENT.search(body):
        return "transient"
    return "permanent"


def _ready(connection, mission_id):
    """Tasks whose dependencies are all completed.

    The mission's own state is checked first. The runner only queries running
    missions, but a pause or cancel can land between that query and this call,
    and nothing should be handed out for a mission that is no longer live.
    """
    mission = connection.execute(
        "SELECT state FROM missions WHERE id = ?", (mission_id,)).fetchone()
    if mission is None or mission["state"] != RUNNING:
        return [], []

    rows = [_row(r) for r in connection.execute(
        "SELECT * FROM mtasks WHERE mission = ? ORDER BY seq", (mission_id,)).fetchall()]
    finished = {t["id"]: t for t in rows if t["state"] in TERMINAL}
    ready = []
    for task in rows:
        if task["state"] != PENDING:
            continue
        depends = task["depends_on"] or []
        if any(d not in finished for d in depends):
            continue
        # Never run a dependent on a failed prerequisite.
        if any(finished[d]["state"] != COMPLETED for d in depends):
            _set_task(connection, task["id"], state=BLOCKED,
                      failure_kind="dependency",
                      error="A prerequisite did not complete.")
            continue
        ready.append(task)
    return ready, rows


def _claim(connection, task_id):
    cursor = connection.execute(
        "UPDATE mtasks SET state = ?, owner = ?, started = ?"
        " WHERE id = ? AND state = ?",
        (RUNNING, OWNER, time.time(), task_id, PENDING))
    connection.commit()
    return cursor.rowcount == 1


def _handoff(connection, task):
    """A compact digest of what this task's prerequisites found.

    Deliberately summaries, not transcripts — a local model has little context
    to spare, and a wall of prior conversation makes the work worse, not better.
    """
    depends = task["depends_on"] or []
    if not depends:
        return ""
    lines = []
    for identifier in depends:
        row = _row(connection.execute(
            "SELECT title, agent, result FROM mtasks WHERE id = ?",
            (identifier,)).fetchone())
        if not row:
            continue
        result = row["result"] if isinstance(row["result"], dict) else {}
        summary = str(result.get("summary") or "").strip()
        if summary:
            lines.append("- " + row["title"] + " (" + row["agent"] + " agent): " +
                         summary[:600])
    if not lines:
        return ""
    return ("\n\nWhat earlier steps of this mission found:\n" + "\n".join(lines) +
            "\nUse these findings; do not repeat that work.")


async def _execute(task, mission_goal, handoff):
    """Run one task as an agent-scoped turn of the ordinary loop."""
    from . import llm, session as sessions

    spec = AGENTS.get(task["agent"], AGENTS[DEFAULT_AGENT])
    session = sessions.get("mission:" + task["id"])
    session.rearm()
    session.background = True
    session.approval_wait = APPROVAL_WAIT
    _active[task["id"]] = session

    prompt = (
        "You are the " + task["agent"] + " agent working on one step of a larger "
        "mission.\n\nMission goal: " + mission_goal + "\n\n" + spec["brief"] +
        "\n\nYour step: " + task["instruction"] + handoff +
        "\n\nWhen you are done, finish with one line beginning 'SUMMARY:' giving "
        "the finding or outcome another agent would need. Keep it to one or two "
        "sentences.")

    # Scope the agent by restricting its tool list, never by adding words to the
    # prompt. An earlier version appended this agent's capability trigger words
    # to the text; the model read them as instructions and the verification
    # agent went off to describe a settings window instead of checking the work.
    session.capability_groups = spec["groups"]

    answer, used, declined, gated = "", [], False, None
    try:
        async for event in llm.run_agent([{"role": "user", "content": prompt}],
                                         session=session):
            kind = event.get("type")
            if kind == "token":
                answer += event["text"]
            elif kind == "tool_start":
                used.append(event["name"])
            elif kind == "confirm":
                gated = {"tool": event["name"], "risk": event.get("risk"),
                         "reason": event.get("reason"), "id": event["id"],
                         "session": session.id}
                _await_approval(task, gated)
            elif kind == "tool_end" and event.get("declined"):
                declined = True
            elif kind == "error":
                raise RuntimeError(event["message"])
    finally:
        _active.pop(task["id"], None)

    return answer.strip(), used, declined, session.stopped, gated


def _await_approval(task, gated):
    """Park the task as waiting and tell the user, without losing the mission."""
    with _lock:
        connection = _connect()
        try:
            _set_task(connection, task["id"], state=WAITING,
                      needs=json.dumps(gated))
            _log(connection, task["mission"], "approval",
                 "Waiting for permission to run " + gated["tool"], task=task["id"])
        finally:
            connection.close()
    from . import proactive
    proactive.raise_event(
        key="mission-approval:" + task["id"], level="high", category="tasks",
        title="Mission needs permission",
        message=("The " + task["agent"] + " agent wants to run " + gated["tool"] +
                 " for " + repr(task["title"]) + "."),
        suggestion=gated.get("reason"),
        data={"mission": task["mission"], "task": task["id"], "approval": gated})


def _summary_of(answer):
    match = re.search(r"SUMMARY:\s*(.+)", answer, re.IGNORECASE | re.DOTALL)
    if match:
        return " ".join(match.group(1).split())[:600]
    return " ".join(answer.split())[:400]


def _finish_task(task, answer, used, declined, stopped, gated, elapsed):
    with _lock:
        connection = _connect()
        try:
            current = _row(connection.execute(
                "SELECT state FROM mtasks WHERE id = ?", (task["id"],)).fetchone())
            if current is None or current["state"] == CANCELLED:
                return

            if stopped:
                _set_task(connection, task["id"], state=PENDING, owner=None,
                          error="Halted before finishing.", latency=elapsed)
                return

            if declined:
                _set_task(connection, task["id"], state=FAILED,
                          failure_kind="permission_denied",
                          error="The user declined a required action.",
                          finished=time.time(), latency=elapsed, owner=None,
                          tools=json.dumps(used))
                _log(connection, task["mission"], "declined",
                     "Declined: " + task["title"], task=task["id"])
                return

            payload = {"summary": _summary_of(answer), "answer": answer[:3000],
                       "tools": used, "agent": task["agent"]}
            _set_task(connection, task["id"], state=COMPLETED,
                      result=json.dumps(payload), finished=time.time(),
                      latency=elapsed, owner=None, error=None,
                      tools=json.dumps(used), needs=None)
            _log(connection, task["mission"], "done",
                 task["title"] + " -> " + payload["summary"][:120], task=task["id"])
        finally:
            connection.close()


def _fail_task(task, message, kind, elapsed):
    with _lock:
        connection = _connect()
        try:
            current = _row(connection.execute(
                "SELECT attempts, max_attempts, state FROM mtasks WHERE id = ?",
                (task["id"],)).fetchone())
            if current is None or current["state"] == CANCELLED:
                return
            attempts = (current["attempts"] or 0) + 1
            retryable = kind in ("transient", "timeout")

            if retryable and attempts < (current["max_attempts"] or 2):
                _set_task(connection, task["id"], state=PENDING, attempts=attempts,
                          error=str(message)[:300], failure_kind=kind, owner=None)
                _log(connection, task["mission"], "retry",
                     task["title"] + " failed (" + kind + "), retrying",
                     task=task["id"])
            else:
                _set_task(connection, task["id"], state=FAILED, attempts=attempts,
                          error=str(message)[:300], failure_kind=kind,
                          finished=time.time(), latency=elapsed, owner=None)
                _log(connection, task["mission"], "failed",
                     task["title"] + ": " + str(message)[:150], task=task["id"])
        finally:
            connection.close()


def _synthesise(connection, mission_id, tasks):
    """Assemble the mission's answer from what each task actually reported."""
    completed = [t for t in tasks if t["state"] == COMPLETED]
    failed = [t for t in tasks if t["state"] in (FAILED, BLOCKED)]
    lines = []
    for task in sorted(completed, key=lambda t: t["seq"]):
        result = task["result"] if isinstance(task["result"], dict) else {}
        summary = result.get("summary")
        if summary:
            lines.append(task["title"] + ": " + summary)
    for task in failed:
        lines.append(task["title"] + " did not complete (" +
                     str(task["failure_kind"] or "failed") + ").")
    return "\n".join(lines) or "No step produced a result."


def _advance(connection, mission_id):
    """Close a mission out when nothing is left to run."""
    tasks = [_row(r) for r in connection.execute(
        "SELECT * FROM mtasks WHERE mission = ? ORDER BY seq",
        (mission_id,)).fetchall()]
    if not tasks:
        return
    if any(t["state"] in (PENDING, RUNNING, WAITING) for t in tasks):
        return

    failed = [t for t in tasks if t["state"] in (FAILED, BLOCKED)]
    result = _synthesise(connection, mission_id, tasks)
    _set_mission(
        connection, mission_id,
        state=FAILED if failed and not any(t["state"] == COMPLETED for t in tasks)
              else COMPLETED,
        result=result, finished=time.time(),
        error=("; ".join(str(t["error"])[:120] for t in failed) if failed else None))
    _log(connection, mission_id, "complete",
         "Mission finished with " +
         str(sum(1 for t in tasks if t["state"] == COMPLETED)) + "/" +
         str(len(tasks)) + " tasks completed.")

    from . import proactive
    proactive.raise_event(
        key="mission-done:" + mission_id, level="low", category="tasks",
        title="Mission finished",
        message=result[:160],
        data={"mission": mission_id})

    # Keep what worked, so a similar goal starts from experience next time.
    # Only a mission where nothing failed is worth recording as strategy.
    if not failed:
        try:
            from . import cognition
            row = connection.execute(
                "SELECT goal FROM missions WHERE id = ?", (mission_id,)).fetchone()
            used = sorted({name for task in tasks for name in (task.get("tools") or [])})
            approach = " -> ".join(
                task["agent"] + ": " + task["title"]
                for task in sorted(tasks, key=lambda x: x["seq"])
                if task["state"] == COMPLETED)
            verified = any(task["agent"] == "verification" and
                           task["state"] == COMPLETED for task in tasks)
            if row and approach:
                cognition.remember_strategy(row["goal"], approach, used,
                                            result[:400], verified,
                                            outcome="succeeded")
        except Exception:
            pass      # learning is a bonus, never a reason to fail a mission


def _batch(tasks):
    """Group ready tasks: read-only agents may share a batch, writers run alone."""
    parallel, batches = [], []
    for task in tasks:
        if AGENTS.get(task["agent"], {}).get("mutates", True):
            if parallel:
                batches.append(parallel)
                parallel = []
            batches.append([task])
        else:
            parallel.append(task)
            if len(parallel) >= MAX_PARALLEL:
                batches.append(parallel)
                parallel = []
    if parallel:
        batches.append(parallel)
    return batches


def _loop(stop_event):
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    recover()

    while not stop_event.is_set():
        try:
            with _lock:
                connection = _connect()
                try:
                    missions = [_row(r) for r in connection.execute(
                        "SELECT * FROM missions WHERE state = ?", (RUNNING,)).fetchall()]
                    work = []
                    for mission in missions:
                        ready, all_tasks = _ready(connection, mission["id"])
                        if not ready:
                            _advance(connection, mission["id"])
                            continue
                        claimed = [t for t in ready if _claim(connection, t["id"])]
                        for task in claimed:
                            work.append((mission, task, _handoff(connection, task)))
                finally:
                    connection.close()

            for batch in _batch([task for _m, task, _h in work]):
                if stop_event.is_set():
                    break
                context = {t["id"]: (m, h) for m, t, h in work}
                if len(batch) == 1:
                    _run_one(loop, batch[0], context)
                else:
                    _run_many(loop, batch, context)
            busy = bool(work)
        except Exception as exc:
            _runner["errors"].append({"at": time.time(), "error": repr(exc)[:300]})
            busy = False
        # Poll briskly only while something is actually running.
        stop_event.wait(TICK if busy else IDLE_TICK)

    loop.close()


def _run_one(loop, task, context):
    import asyncio
    mission, handoff = context[task["id"]]
    started = time.perf_counter()
    try:
        outcome = loop.run_until_complete(
            asyncio.wait_for(_execute(task, mission["goal"], handoff),
                             timeout=TASK_TIMEOUT))
        elapsed = round((time.perf_counter() - started) * 1000)
        answer, used, declined, stopped, gated = outcome
        _finish_task(task, answer, used, declined, stopped, gated, elapsed)
    except asyncio.TimeoutError:
        _fail_task(task, "Timed out after " + str(TASK_TIMEOUT) + "s.", "timeout",
                   round((time.perf_counter() - started) * 1000))
    except Exception as exc:
        _fail_task(task, exc, _classify_failure(exc, False, False),
                   round((time.perf_counter() - started) * 1000))


def _run_many(loop, batch, context):
    """Run several read-only tasks together."""
    import asyncio

    async def gather():
        async def one(task):
            mission, handoff = context[task["id"]]
            started = time.perf_counter()
            try:
                outcome = await asyncio.wait_for(
                    _execute(task, mission["goal"], handoff), timeout=TASK_TIMEOUT)
                return task, outcome, None, round((time.perf_counter() - started) * 1000)
            except asyncio.TimeoutError:
                return task, None, "timeout", round((time.perf_counter() - started) * 1000)
            except Exception as exc:
                return task, None, exc, round((time.perf_counter() - started) * 1000)
        return await asyncio.gather(*(one(t) for t in batch))

    for task, outcome, failure, elapsed in loop.run_until_complete(gather()):
        if failure is None:
            answer, used, declined, stopped, gated = outcome
            _finish_task(task, answer, used, declined, stopped, gated, elapsed)
        elif failure == "timeout":
            _fail_task(task, "Timed out after " + str(TASK_TIMEOUT) + "s.",
                       "timeout", elapsed)
        else:
            _fail_task(task, failure, _classify_failure(failure, False, False), elapsed)


def ensure_runner():
    with _lock:
        thread = _runner["thread"]
        if thread is not None and thread.is_alive():
            return False
        stop_event = threading.Event()
        thread = threading.Thread(target=_loop, args=(stop_event,),
                                  name="jarvish-missions", daemon=True)
        _runner.update({"thread": thread, "stop": stop_event})
        thread.start()
        return True


def shutdown():
    with _lock:
        if _runner["stop"] is not None:
            _runner["stop"].set()
        _runner["thread"] = None


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def tool_start(goal, plan_it=True):
    return create(goal, use_model=as_bool(plan_it, True))


SCHEMAS = [
    tool("start_mission",
         "Start a long-running mission: a goal too big for one answer, broken into "
         "steps and run by specialised agents in the background. Use for 'find out "
         "why X is failing and fix it', 'research X then do Y'. The conversation "
         "stays usable while it runs.",
         {"goal": string("What the mission should achieve."),
          "plan_it": boolean("Let the planner decompose it. Default true.")},
         ["goal"]),
    tool("mission_status",
         "Show a mission's progress: its task graph, which agent is working, what "
         "each step found, and anything waiting for permission.",
         {"mission": string("The mission id."),
          "include_log": boolean("Include the event log.")},
         ["mission"]),
    tool("list_missions", "List missions and their progress.",
         {"state": string("Only missions in this state.",
                          ["pending", "running", "paused", "completed",
                           "failed", "cancelled"])}),
    tool("pause_mission", "Pause a running mission.",
         {"mission": string("The mission id.")}, ["mission"]),
    tool("resume_mission", "Resume a paused mission.",
         {"mission": string("The mission id.")}, ["mission"]),
    tool("cancel_mission", "Cancel a mission for good.",
         {"mission": string("The mission id.")}, ["mission"]),
    tool("retry_mission_task", "Retry one failed step of a mission.",
         {"task": string("The task id, like m1234-t2.")}, ["task"]),
]

REGISTRY = {
    "start_mission": tool_start,
    "mission_status": lambda mission, include_log=False: status(mission, include_log),
    "list_missions": lambda state=None: listing(state),
    "pause_mission": lambda mission: pause(mission),
    "resume_mission": lambda mission: resume(mission),
    "cancel_mission": lambda mission: cancel(mission),
    "retry_mission_task": lambda task: retry_task(task),
}
