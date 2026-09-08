"""The cognitive layer: one context view, autonomy levels, and strategy memory.

Three things live here, and they share one design rule: **nothing polls**. This
machine is memory-bound, and an always-on context builder would cost exactly
what the process-scan bug cost before it was found. Everything below is built
on demand, cached briefly, and assembled from data the other subsystems already
hold.

**Unified context** gathers what Jarvish can know right now — the screen, the
active application, the project, memory, missions, tasks, system state — ranks
it against the request, and returns only the parts that earn their place. Each
fragment carries its source and how it was obtained, so nothing claims to be
seen when it was only read.

**Autonomy levels** are a ceiling over the existing risk gate, never a bypass.
L0 refuses every tool; L5 permits the most. A level can only ever make Jarvish
*more* cautious than the gate already is — raising the level never lowers a
tool's tier, and `critical` is gated at every level.

**Strategy memory** records what actually worked: the approach, the tools, the
outcome, and whether it was verified. Before planning a similar goal, the
planner can look at what succeeded last time instead of starting cold.
"""

import json
import re
import sqlite3
import threading
import time
from collections import OrderedDict

from .util import DATA_DIR, as_bool, as_int, boolean, err, integer, ok, string, tool

DB_PATH = DATA_DIR / "cognition.db"
SETTINGS_PATH = DATA_DIR / "autonomy.json"

# Context is assembled on demand and reused for a few seconds, which covers the
# several calls a single turn makes without ever running on a timer.
CONTEXT_TTL = 8.0

_lock = threading.RLock()
# A few slots, not one: a single turn asks about several things, and a
# one-entry cache is evicted by the next question before it is ever reused.
_context_cache = OrderedDict()
CONTEXT_CACHE_SLOTS = 8


# --------------------------------------------------------------------------
# Autonomy
# --------------------------------------------------------------------------

# Each level names the highest risk tier Jarvish may act on without asking, and
# whether it may start autonomous work. These are ceilings: the risk engine
# still applies underneath, so a tool gated at `critical` is gated at L5 too.
LEVELS = {
    0: {"name": "Chat only", "max_tier": None, "missions": False,
        "detail": "No tools at all. Jarvish answers from what it already knows."},
    1: {"name": "Suggest", "max_tier": "safe", "missions": False,
        "detail": "Read-only tools. Jarvish can look, and propose, but not change."},
    2: {"name": "Execute with approval", "max_tier": "low", "missions": False,
        "detail": "Low-risk actions run; anything that changes state asks first."},
    3: {"name": "Autonomous safe actions", "max_tier": "medium", "missions": False,
        "detail": "State-changing actions run unasked. High risk still stops."},
    4: {"name": "Autonomous missions", "max_tier": "medium", "missions": True,
        "detail": "As L3, and Jarvish may run long background missions."},
    5: {"name": "Supervised operator", "max_tier": "high", "missions": True,
        "detail": "High-risk actions run unasked. Irreversible ones always stop."},
}

DEFAULT_LEVEL = 3
_autonomy = {"level": DEFAULT_LEVEL}


def _load_autonomy():
    try:
        if SETTINGS_PATH.exists():
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            level = int(stored.get("level", DEFAULT_LEVEL))
            if level in LEVELS:
                _autonomy["level"] = level
    except Exception:
        _autonomy["level"] = DEFAULT_LEVEL
    return _autonomy["level"]


def _save_autonomy():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(_autonomy, indent=2), encoding="utf-8")
    except Exception:
        pass


_load_autonomy()


def level():
    return _autonomy["level"]


def set_level(value):
    """Change the autonomy ceiling."""
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        return err("Autonomy level must be a number from 0 to 5.")
    if wanted not in LEVELS:
        return err("Autonomy level must be 0-5. " +
                   "; ".join(str(k) + " = " + v["name"] for k, v in LEVELS.items()))
    with _lock:
        _autonomy["level"] = wanted
        _save_autonomy()
    spec = LEVELS[wanted]
    return ok(level=wanted, name=spec["name"], detail=spec["detail"],
              runs_without_asking=spec["max_tier"] or "nothing",
              missions_allowed=spec["missions"])


def permits(tool_name, arguments=None):
    """Whether the current autonomy level allows this call to run unattended.

    Returns (allowed, reason). This is a *ceiling* on top of `risk.gated`: it
    can stop something the gate would have allowed, and can never permit
    something the gate stops.
    """
    from . import risk

    spec = LEVELS[_autonomy["level"]]
    ceiling = spec["max_tier"]

    if ceiling is None:
        return False, ("Autonomy is at L0 (chat only), so no tool may run. "
                       "Raise it with set_autonomy.")

    tier = risk.effective_level(tool_name, arguments)
    if risk.ORDER.index(tier) > risk.ORDER.index(ceiling):
        return False, ("This is a " + tier + " action and autonomy is at L" +
                       str(_autonomy["level"]) + " (" + spec["name"] +
                       "), which stops at " + ceiling + ".")
    return True, None


def must_confirm(tool_name, arguments=None):
    """Whether this call has to stop and ask. The single authority.

    Two independent things can stop a call, and either is sufficient: the risk
    gate, and the autonomy ceiling. Expressing that in one place — rather than
    inline at the call site — is what makes it checkable that autonomy can only
    ever *add* caution. Raising the level cannot clear the risk gate, because
    the gate is consulted first and short-circuits.

    Returns (stop, reason, blocked_by), where `blocked_by` is "risk" when the
    risk gate stopped it, "autonomy" when the ceiling did, and None when
    nothing did. The caller needs that distinction to tell the user *which*
    rule is in the way — "this is irreversible" and "your autonomy level is too
    low" call for different responses.
    """
    from . import risk

    if risk.gated(tool_name, arguments):
        return True, risk.reason(tool_name, arguments), "risk"
    allowed, why = permits(tool_name, arguments)
    if not allowed:
        return True, why, "autonomy"
    return False, None, None


def autonomy_status():
    spec = LEVELS[_autonomy["level"]]
    return ok(level=_autonomy["level"], name=spec["name"], detail=spec["detail"],
              runs_without_asking=spec["max_tier"] or "nothing",
              missions_allowed=spec["missions"],
              levels={k: {"name": v["name"], "detail": v["detail"]}
                      for k, v in LEVELS.items()},
              note=("This is a ceiling over the risk gate, not a replacement. "
                    "Irreversible actions stop for confirmation at every level."))


# --------------------------------------------------------------------------
# Storage for strategies
# --------------------------------------------------------------------------

def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS strategies (
        id INTEGER PRIMARY KEY, goal TEXT, approach TEXT, tools TEXT,
        result TEXT, verified INTEGER, agent TEXT, outcome TEXT,
        used INTEGER DEFAULT 0, at REAL
    );
    CREATE INDEX IF NOT EXISTS strategies_goal ON strategies(goal);
    CREATE TABLE IF NOT EXISTS tool_stats (
        tool TEXT PRIMARY KEY, runs INTEGER DEFAULT 0, failures INTEGER DEFAULT 0,
        total_ms REAL DEFAULT 0, last_error TEXT, last_at REAL
    );
    """)
    connection.commit()
    return connection


# --------------------------------------------------------------------------
# Tool reliability
# --------------------------------------------------------------------------

def record_tool(name, ok_flag, ms, error=None):
    """Note how a tool call went. Called from the agent loop; must stay cheap."""
    try:
        with _lock:
            connection = _connect()
            try:
                connection.execute(
                    "INSERT INTO tool_stats(tool, runs, failures, total_ms, "
                    "last_error, last_at) VALUES (?,1,?,?,?,?) "
                    "ON CONFLICT(tool) DO UPDATE SET "
                    "runs = runs + 1, failures = failures + ?, "
                    "total_ms = total_ms + ?, last_error = COALESCE(?, last_error), "
                    "last_at = ?",
                    (name, 0 if ok_flag else 1, ms or 0,
                     None if ok_flag else str(error)[:200], time.time(),
                     0 if ok_flag else 1, ms or 0,
                     None if ok_flag else str(error)[:200], time.time()))
                connection.commit()
            finally:
                connection.close()
    except Exception:
        pass          # telemetry must never break a tool call


def reliability(limit=15):
    """Which tools are slow or unreliable, from real recorded runs."""
    with _lock:
        connection = _connect()
        try:
            rows = [dict(r) for r in connection.execute(
                "SELECT tool, runs, failures, total_ms, last_error, last_at"
                " FROM tool_stats WHERE runs > 0 ORDER BY runs DESC LIMIT 200"
            ).fetchall()]
        finally:
            connection.close()

    for row in rows:
        row["avg_ms"] = round(row["total_ms"] / max(row["runs"], 1))
        row["failure_rate"] = round(row["failures"] / max(row["runs"], 1), 3)
    unreliable = sorted((r for r in rows if r["runs"] >= 3 and r["failure_rate"] > 0.3),
                        key=lambda r: -r["failure_rate"])[:limit]
    slow = sorted((r for r in rows if r["runs"] >= 2),
                  key=lambda r: -r["avg_ms"])[:limit]
    return ok(tools=len(rows), total_runs=sum(r["runs"] for r in rows),
              unreliable=unreliable[:limit], slowest=slow[:limit],
              note=("Measured from real calls in this installation. A tool with "
                    "few runs is not yet judged."))


# --------------------------------------------------------------------------
# Strategy memory
# --------------------------------------------------------------------------

_STOP = {"the", "a", "an", "and", "for", "with", "my", "this", "that", "then",
         "to", "of", "in", "on", "is", "it", "me", "i"}


def _keywords(text):
    words = re.findall(r"[a-z0-9_]{3,}", str(text or "").lower())
    return [w for w in words if w not in _STOP]


def remember_strategy(goal, approach, tools=(), result=None, verified=False,
                      agent=None, outcome="succeeded"):
    """Record something that worked, compactly, for reuse."""
    text = str(goal or "").strip()
    if not text:
        return err("A strategy needs the goal it solved.")
    if not str(approach or "").strip():
        return err("A strategy needs the approach that worked.")

    with _lock:
        connection = _connect()
        try:
            existing = connection.execute(
                "SELECT id FROM strategies WHERE goal = ? AND approach = ?",
                (text, str(approach))).fetchone()
            if existing:
                return ok(already_known=True, strategy=existing["id"])
            cursor = connection.execute(
                "INSERT INTO strategies(goal, approach, tools, result, verified,"
                " agent, outcome, at) VALUES (?,?,?,?,?,?,?,?)",
                (text, str(approach)[:1200], json.dumps(list(tools)),
                 str(result or "")[:800], 1 if as_bool(verified) else 0,
                 agent, outcome, time.time()))
            connection.commit()
        finally:
            connection.close()
    return ok(strategy=cursor.lastrowid, goal=text, tools=list(tools),
              verified=bool(verified))


def recall_strategies(goal, limit=3):
    """Strategies that solved a similar goal before, best match first."""
    wanted = set(_keywords(goal))
    if not wanted:
        return ok(strategies=[], count=0, note="Nothing to match on.")

    with _lock:
        connection = _connect()
        try:
            rows = [dict(r) for r in connection.execute(
                "SELECT * FROM strategies ORDER BY at DESC LIMIT 300").fetchall()]
        finally:
            connection.close()

    scored = []
    for row in rows:
        have = set(_keywords(row["goal"]))
        if not have:
            continue
        overlap = len(wanted & have) / len(wanted | have)
        if overlap < 0.2:
            continue
        # A verified strategy is worth more than one that merely finished.
        score = overlap * (1.3 if row["verified"] else 1.0)
        try:
            row["tools"] = json.loads(row["tools"] or "[]")
        except (TypeError, ValueError):
            row["tools"] = []
        scored.append(dict(row, match=round(score, 3)))

    scored.sort(key=lambda r: r["match"], reverse=True)
    chosen = scored[:as_int(limit, 3, 1, 10)]

    if chosen:
        with _lock:
            connection = _connect()
            try:
                for row in chosen:
                    connection.execute(
                        "UPDATE strategies SET used = used + 1 WHERE id = ?",
                        (row["id"],))
                connection.commit()
            finally:
                connection.close()

    return ok(strategies=[{
        "goal": r["goal"], "approach": r["approach"], "tools": r["tools"],
        "verified": bool(r["verified"]), "outcome": r["outcome"],
        "match": r["match"], "used": r["used"],
    } for r in chosen], count=len(chosen), searched=len(rows))


def forget_strategies(goal=None, all=False):
    """Delete stored strategies — explicitly, by goal or entirely."""
    with _lock:
        connection = _connect()
        try:
            if as_bool(all):
                cursor = connection.execute("DELETE FROM strategies")
            elif goal:
                cursor = connection.execute(
                    "DELETE FROM strategies WHERE goal LIKE ?", ("%" + str(goal) + "%",))
            else:
                return err("Give a goal to forget, or set all to true.")
            connection.commit()
        finally:
            connection.close()
    return ok(removed=cursor.rowcount)


# --------------------------------------------------------------------------
# Unified context
# --------------------------------------------------------------------------

def _fragment(source, how, text, weight):
    return {"source": source, "obtained": how, "text": str(text)[:900],
            "weight": weight}


def _screen_fragment():
    """What is on screen — read, never inferred from pixels."""
    from . import vision
    if not vision.available()["capture"]:
        return None
    window = vision.active_window()
    if not window:
        return None
    return _fragment(
        "screen", "accessibility tree + OCR",
        "Active window: " + str(window.get("title", "?")) +
        (" (" + window["app"] + ")" if window.get("app") else ""),
        0.6)


def _project_fragment():
    from . import dev
    try:
        info = dev.detect(".")
    except Exception:
        return None
    if not info.get("ok"):
        return None
    return _fragment(
        "project", "detected from files on disk",
        "Project at " + str(info["root"]) + ": " +
        ", ".join(info["languages"]) +
        (" using " + ", ".join(info["frameworks"]) if info["frameworks"] else "") +
        (". Test command: " + info["commands"]["test"]
         if info.get("commands", {}).get("test") else ""),
        0.5)


def _memory_fragment():
    from . import personal
    profile = personal.recall()
    facts = profile.get("facts") or {}
    if not facts:
        return None
    return _fragment(
        "memory", "stored profile",
        "; ".join(k.replace("_", " ") + ": " + str(v) for k, v in list(facts.items())[:8]),
        0.7)


def _system_fragment():
    """Always report the numbers, not only when something is wrong.

    Returning nothing on a healthy machine meant "why is my machine slow?"
    produced no system context at all — exactly the question that needs it.
    """
    from . import telemetry
    frame = telemetry.snapshot()
    notes = telemetry.insights(frame)
    reading = ("CPU " + str(frame["cpu"]) + "%, RAM " + str(frame["memory"]) +
               "% (" + str(frame["memory_used_gb"]) + "/" +
               str(frame["memory_total_gb"]) + " GB), disk " +
               str(frame["disk"]) + "% used")
    if notes:
        reading += ". " + "; ".join(n["text"] for n in notes)
    return _fragment("system", "live telemetry", reading, 0.8 if notes else 0.55)


def _mission_fragment():
    from . import missions
    state = missions.listing()
    live = [m for m in state.get("missions", [])
            if m["state"] in ("running", "paused")]
    if not live:
        return None
    return _fragment(
        "missions", "mission store",
        "; ".join(m["goal"][:80] + " (" + m["state"] + ", " +
                  str(m["progress"]) + "%)" for m in live[:3]),
        0.9)


def _task_fragment():
    from . import tasks
    state = tasks.listing()
    live = [t for t in state.get("tasks", [])
            if t["state"] in ("running", "pending", "paused")]
    if not live:
        return None
    return _fragment("tasks", "task store",
                     "; ".join(t["title"][:60] + " (" + t["state"] + ")"
                               for t in live[:4]), 0.7)


def _knowledge_fragment(query):
    from . import kb
    if not query:
        return None
    try:
        found = kb.search(query, limit=2)
    except Exception:
        return None
    if not found.get("ok") or not found.get("results"):
        return None
    return _fragment(
        "knowledge", found.get("method", "search"),
        "; ".join(r["source"] + " L" + str(r["lines"][0]) + ": " +
                  " ".join(r["chunk"].split())[:180] for r in found["results"]),
        0.85)


# Which sources are worth gathering for a given kind of request. Assembling
# everything every time would be slow and would drown the model in noise.
RELEVANCE = {
    "screen": ("screen", "look", "see", "this", "visible", "window", "here"),
    "project": ("code", "project", "file", "test", "build", "error", "bug", "function"),
    "knowledge": ("code", "file", "document", "project", "wrote", "where", "find"),
    "system": ("slow", "memory", "cpu", "disk", "battery", "system", "performance"),
    "missions": ("mission", "running", "progress", "continue", "unfinished", "waiting"),
    "tasks": ("task", "background", "scheduled", "reminder", "running"),
    "memory": (),          # always relevant: it is who the user is
}

BUILDERS = {
    "screen": _screen_fragment,
    "project": _project_fragment,
    "memory": _memory_fragment,
    "system": _system_fragment,
    "missions": _mission_fragment,
    "tasks": _task_fragment,
}


def build_context(query="", sources=None, budget=1400):
    """Assemble the relevant context for a request, and only the relevant part.

    Nothing here runs on a timer. A short cache covers the repeated calls a
    single turn makes without turning this into a background process.
    """
    text = str(query or "").lower()
    key = (text[:120], tuple(sources or ()))

    with _lock:
        hit = _context_cache.get(key)
        if hit and time.time() - hit["at"] < CONTEXT_TTL:
            _context_cache.move_to_end(key)
            return dict(hit["value"], cached=True)

    words = set(re.findall(r"[a-z']+", text))
    wanted = list(sources) if sources else []
    if not wanted:
        for name, triggers in RELEVANCE.items():
            if not triggers or words & set(triggers):
                wanted.append(name)
        # Live work is always worth knowing about; it is cheap to check.
        for always in ("missions", "tasks"):
            if always not in wanted:
                wanted.append(always)

    started = time.perf_counter()
    fragments, failed = [], []
    for name in wanted:
        builder = BUILDERS.get(name)
        if builder is None:
            continue
        try:
            fragment = builder()
            if fragment:
                fragments.append(fragment)
        except Exception as exc:
            failed.append({"source": name, "error": str(exc)[:120]})

    if "knowledge" in wanted or (words & set(RELEVANCE["knowledge"])):
        try:
            fragment = _knowledge_fragment(query)
            if fragment:
                fragments.append(fragment)
        except Exception as exc:
            failed.append({"source": "knowledge", "error": str(exc)[:120]})

    # Rank, then trim to a budget so the model is never handed a wall of text.
    fragments.sort(key=lambda f: f["weight"], reverse=True)
    kept, used, dropped = [], 0, []
    seen = set()
    for fragment in fragments:
        digest = fragment["text"][:120]
        if digest in seen:                     # deduplicate before compressing
            continue
        seen.add(digest)
        if used + len(fragment["text"]) > budget:
            dropped.append(fragment["source"])
            continue
        kept.append(fragment)
        used += len(fragment["text"])

    summary = "\n".join("- [" + f["source"] + ", " + f["obtained"] + "] " + f["text"]
                        for f in kept)

    payload = ok(
        query=query,
        considered=wanted,
        fragments=kept,
        dropped=dropped,
        failed=failed,
        characters=used,
        budget=budget,
        summary=summary,
        ms=round((time.perf_counter() - started) * 1000),
        cached=False,
        note=("Assembled on demand. Screen fragments come from the accessibility "
              "tree and OCR, never from interpreting pixels."),
    )
    with _lock:
        _context_cache[key] = {"at": time.time(), "value": payload}
        _context_cache.move_to_end(key)
        while len(_context_cache) > CONTEXT_CACHE_SLOTS:
            _context_cache.popitem(last=False)
    return payload


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def tool_context(query=None, budget=1400):
    return build_context(query or "", budget=as_int(budget, 1400, 200, 6000))


SCHEMAS = [
    tool("current_context",
         "Assemble what is going on right now — the screen, the project, live "
         "missions and tasks, system state and what you remember about the user — "
         "ranked for a particular question. Use when the user says 'this', 'here', "
         "'continue', or asks what is happening.",
         {"query": string("What the context is being gathered for."),
          "budget": integer("Maximum characters of context. Default 1400.")}),
    tool("autonomy_level",
         "Report how much Jarvish is allowed to do without asking, and what each "
         "level means."),
    tool("set_autonomy",
         "Change how much Jarvish may do unattended, from 0 (chat only) to 5 "
         "(supervised operator). This is a ceiling over the risk gate, never a "
         "way around it.",
         {"level": integer("A number from 0 to 5.")},
         ["level"]),
    tool("recall_strategy",
         "Look up how a similar goal was solved before, so the same ground is not "
         "covered twice. Use before planning something that feels familiar.",
         {"goal": string("The goal to match against."),
          "limit": integer("How many to return. Default 3.")},
         ["goal"]),
    tool("remember_strategy",
         "Record an approach that worked, so it can be reused.",
         {"goal": string("What was being achieved."),
          "approach": string("What actually worked, in a sentence or two."),
          "tools": string("Comma-separated tools that were used."),
          "result": string("What came of it."),
          "verified": boolean("Whether the outcome was actually confirmed.")},
         ["goal", "approach"]),
    tool("forget_strategies",
         "Delete stored strategies.",
         {"goal": string("Only those matching this goal."),
          "all": boolean("Delete every stored strategy.")}),
    tool("tool_reliability",
         "Report which tools have been failing or running slowly in this "
         "installation, measured from real calls."),
]


def _tool_remember(goal, approach, tools=None, result=None, verified=False):
    names = [t.strip() for t in str(tools or "").split(",") if t.strip()]
    return remember_strategy(goal, approach, names, result, verified)


REGISTRY = {
    "current_context": tool_context,
    "autonomy_level": autonomy_status,
    "set_autonomy": lambda level: set_level(level),
    "recall_strategy": lambda goal, limit=3: recall_strategies(goal, limit),
    "remember_strategy": _tool_remember,
    "forget_strategies": lambda goal=None, all=False: forget_strategies(goal, all),
    "tool_reliability": reliability,
}
