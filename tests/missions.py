"""P8 tests: orchestration, the task graph, persistence and the race conditions.

Uses a private database so it never competes with the server's own mission
runner — the double-execution trap found in P5. The final section runs a real
multi-agent mission against the live model.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvish import missions, risk   # noqa: E402

# Private store: the running server has a mission runner of its own, and these
# tests must never compete with it. A unique filename per run also means a WAL
# left locked by a previous run cannot leak state into this one — deleting the
# old file is best-effort, but starting from a fresh name is guaranteed.
import atexit   # noqa: E402
import tempfile  # noqa: E402

_scratch = Path(tempfile.mkdtemp(prefix="jarvish-missions-"))
missions.DB_PATH = _scratch / "missions.db"


@atexit.register
def _clean_scratch():
    import shutil
    shutil.rmtree(_scratch, ignore_errors=True)

P = F = 0


def ok(label, cond, extra=""):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
    print("  [%s] %-48s %s" % ("PASS" if cond else "FAIL", label, str(extra)[:42]))


def quiet():
    """Stop the background runner and requeue anything it had claimed.

    `create` and `resume` start the runner, and it would otherwise claim the
    very tasks these assertions are about to inspect — the test would be racing
    itself. Stopping it is not enough: a task claimed a moment earlier is left
    in `running`, so `recover()` puts it back. That is the product's own restart
    path, used here for exactly what it is for.
    """
    missions.shutdown()
    time.sleep(0.2)
    missions.recover()


def plan(*steps):
    """steps: (title, agent, instruction, [depends_on titles])"""
    return [{"title": t, "agent": a, "instruction": i, "depends_on": d}
            for t, a, i, d in steps]


# ── decomposition ────────────────────────────────────────────────────────
print("--- decomposition ---")
graph = missions._normalise_plan({"tasks": [
    {"title": "Look", "agent": "research", "instruction": "find facts",
     "depends_on": []},
    {"title": "Fix", "agent": "developer", "instruction": "apply the fix",
     "depends_on": ["Look"]},
    {"title": "Check", "agent": "verification", "instruction": "verify",
     "depends_on": ["Fix"]},
]}, "goal")
ok("plan normalised", graph is not None and len(graph) == 3)
ok("titles resolved to indices", graph[1]["depends_on"] == [0] and
   graph[2]["depends_on"] == [1], [g["depends_on"] for g in graph])

cyclic = missions._normalise_plan({"tasks": [
    {"title": "A", "agent": "research", "instruction": "a", "depends_on": ["B"]},
    {"title": "B", "agent": "research", "instruction": "b", "depends_on": ["A"]},
]}, "goal")
ok("forward-only deps break cycles",
   cyclic[0]["depends_on"] == [] and cyclic[1]["depends_on"] == [0],
   [g["depends_on"] for g in cyclic])

ok("unknown agent replaced", missions._normalise_plan({"tasks": [
    {"title": "X", "agent": "wizard", "instruction": "check the cpu and memory",
     "depends_on": []}]}, "g")[0]["agent"] in missions.AGENTS)
ok("garbage plan rejected", missions._normalise_plan({"tasks": []}, "g") is None)
ok("fallback is a real single task", len(missions._fallback_plan("fix the code")) == 1)
ok("fallback picks a sensible agent",
   missions._fallback_plan("fix the failing test")[0]["agent"] == "developer")
ok("json extracted from prose",
   missions._extract_json('here you go {"tasks":[]} thanks') == {"tasks": []})
ok("bad json returns None", missions._extract_json("no json here") is None)

# ── mission creation and the graph ───────────────────────────────────────
print("--- mission creation ---")
made = missions.create("test mission", plan=plan(
    ("Read A", "research", "read a", []),
    ("Read B", "knowledge", "read b", []),
    ("Combine", "verification", "combine", ["Read A", "Read B"]),
), autostart=True)
ok("mission created", made["ok"], made.get("mission"))
mission_id = made["mission"]
ok("three tasks stored", made["tasks"] == 3)
missions.pause(mission_id)
ok("a paused mission offers no work",
   missions._ready(missions._connect(), mission_id)[0] == [])
missions.resume(mission_id)
quiet()
ok("starts paused when asked",
   missions.create("paused one", plan=plan(("X", "research", "x", [])),
                   autostart=False)["state"] == "paused")
ok("empty goal refused", not missions.create("")["ok"])

state = missions.status(mission_id)
ok("task graph persisted",
   state["tasks"][2]["depends_on"] == [mission_id + "-t0", mission_id + "-t1"],
   state["tasks"][2]["depends_on"])
ok("progress starts at zero", state["progress"] == 0)
ok("unknown mission -> error", not missions.status("nope")["ok"])

# ── readiness and dependencies ───────────────────────────────────────────
print("--- dependencies ---")
quiet()
connection = missions._connect()
ready, _all = missions._ready(connection, mission_id)
ok("only independent tasks are ready", len(ready) == 2,
   [t["title"] for t in ready])
ok("dependent task is not ready", "Combine" not in [t["title"] for t in ready])

missions._set_task(connection, mission_id + "-t0", state="completed",
                   result=json.dumps({"summary": "A says apples"}))
ready, _all = missions._ready(connection, mission_id)
ok("still blocked on the second prerequisite",
   [t["title"] for t in ready] == ["Read B"], [t["title"] for t in ready])

missions._set_task(connection, mission_id + "-t1", state="completed",
                   result=json.dumps({"summary": "B says bananas"}))
ready, _all = missions._ready(connection, mission_id)
ok("dependent becomes ready once both finish",
   [t["title"] for t in ready] == ["Combine"])

handoff = missions._handoff(connection, ready[0])
ok("handoff carries prerequisite summaries",
   "apples" in handoff and "bananas" in handoff)
ok("handoff is a summary, not a transcript", len(handoff) < 500, len(handoff))
connection.close()

# ── dependency failure blocks dependents ─────────────────────────────────
print("--- dependency failure ---")
broken = missions.create("dependency failure", plan=plan(
    ("First", "research", "do first", []),
    ("Second", "research", "needs first", ["First"]),
), autostart=True)
quiet()
connection = missions._connect()
missions._set_task(connection, broken["mission"] + "-t0", state="failed",
                   failure_kind="permanent", error="nope")
ready, _all = missions._ready(connection, broken["mission"])
ok("dependent not run on a failed prerequisite", ready == [])
after = missions.status(broken["mission"])
ok("dependent marked blocked",
   after["tasks"][1]["state"] == "blocked", after["tasks"][1]["state"])
ok("blocked reason recorded",
   after["tasks"][1]["failure_kind"] == "dependency")
connection.close()

# ── parallel vs sequential batching ──────────────────────────────────────
print("--- concurrency rules ---")
readers = [{"agent": "research", "id": "1"}, {"agent": "knowledge", "id": "2"}]
ok("read-only agents batch together", len(missions._batch(readers)) == 1)
writers = [{"agent": "developer", "id": "1"}, {"agent": "browser", "id": "2"}]
ok("mutating agents run alone", len(missions._batch(writers)) == 2)
mixed = [{"agent": "research", "id": "1"}, {"agent": "developer", "id": "2"},
         {"agent": "knowledge", "id": "3"}]
batches = missions._batch(mixed)
ok("a writer splits the batch", len(batches) == 3, [len(b) for b in batches])
ok("parallelism is capped",
   all(len(b) <= missions.MAX_PARALLEL for b in
       missions._batch([{"agent": "research", "id": str(i)} for i in range(6)])))

# ── atomic claiming (race) ───────────────────────────────────────────────
print("--- race: duplicate execution ---")
racy = missions.create("race", plan=plan(("Only", "research", "do it", [])),
                       autostart=True)
quiet()
task_id = racy["mission"] + "-t0"
connection = missions._connect()
ok("first claim wins", missions._claim(connection, task_id))
ok("second claim loses", not missions._claim(connection, task_id))
connection.close()

results = []
def claimer():
    conn = missions._connect()
    results.append(missions._claim(conn, racy["mission"] + "-t0"))
    conn.close()

missions._set_task(missions._connect(), task_id, state="pending", owner=None)
threads = [threading.Thread(target=claimer) for _ in range(8)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
ok("exactly one of 8 concurrent claimers wins", sum(results) == 1,
   "%d winners" % sum(results))

# ── restart recovery ─────────────────────────────────────────────────────
print("--- race: restart during execution ---")
crashed = missions.create("crash", plan=plan(("Work", "research", "work", [])),
                          autostart=False)
connection = missions._connect()
missions._set_task(connection, crashed["mission"] + "-t0", state="running",
                   owner=missions.OWNER, started=time.time())
connection.close()
recovered = missions.recover()
ok("interrupted task requeued",
   missions.status(crashed["mission"])["tasks"][0]["state"] == "pending",
   recovered)

exhausted = missions.create("exhausted", plan=plan(("Work", "research", "w", [])),
                            autostart=False)
connection = missions._connect()
missions._set_task(connection, exhausted["mission"] + "-t0", state="running",
                   owner=missions.OWNER, attempts=5, max_attempts=2)
connection.close()
missions.recover()
ok("exhausted task fails rather than looping",
   missions.status(exhausted["mission"])["tasks"][0]["state"] == "failed")

connection = missions._connect()
missions._set_task(connection, exhausted["mission"] + "-t0", state="running",
                   owner="pid:999999")
connection.close()
missions.recover()
ok("another process's task is left alone",
   missions.status(exhausted["mission"])["tasks"][0]["state"] == "running")

# ── pause / resume / cancel ──────────────────────────────────────────────
print("--- pause, resume, cancel ---")
control = missions.create("control", plan=plan(
    ("One", "research", "one", []), ("Two", "research", "two", ["One"])))
quiet()
ok("starts running", missions.status(control["mission"])["state"] == "running")
ok("pause works", missions.pause(control["mission"])["state"] == "paused")
ok("resume works", missions.resume(control["mission"])["state"] == "running")
quiet()
ok("cancel works", missions.cancel(control["mission"])["state"] == "cancelled")
ok("cancelled cannot be paused", not missions.pause(control["mission"])["ok"])
ok("cancel marks tasks cancelled",
   all(t["state"] == "cancelled"
       for t in missions.status(control["mission"])["tasks"]))
ok("unknown mission refused", not missions.pause("nope")["ok"])

print("--- race: STOP during task startup ---")
startup = missions.create("startup race", plan=plan(("Go", "research", "go", [])))
quiet()
connection = missions._connect()
# Claimed but not yet in `_active` — the gap that in-memory-only stop misses.
missions._claim(connection, startup["mission"] + "-t0")
connection.close()
halted = missions.stop_all()
ok("stop_all reaches a claimed-but-unstarted task",
   missions.status(startup["mission"])["tasks"][0]["state"] == "pending",
   missions.status(startup["mission"])["tasks"][0]["state"])
ok("mission paused by stop",
   missions.status(startup["mission"])["state"] == "paused")
ok("stop reports what it halted", isinstance(halted, dict) and "missions" in halted)

print("--- race: cancellation during handoff ---")
handoff_race = missions.create("handoff race", plan=plan(
    ("A", "research", "a", []), ("B", "verification", "b", ["A"])))
quiet()
connection = missions._connect()
missions._set_task(connection, handoff_race["mission"] + "-t0", state="completed",
                   result=json.dumps({"summary": "done"}))
connection.close()
missions.cancel(handoff_race["mission"])
connection = missions._connect()
ready, _all = missions._ready(connection, handoff_race["mission"])
connection.close()
ok("cancelled mission yields no ready work", ready == [])
ok("dependent was cancelled, not started",
   missions.status(handoff_race["mission"])["tasks"][1]["state"] == "cancelled")

# ── failure classification ───────────────────────────────────────────────
print("--- failure classification ---")
ok("timeout classified",
   missions._classify_failure("Timed out after 60s", False, False) == "timeout")
ok("transient classified",
   missions._classify_failure("connection refused", False, False) == "transient")
ok("permanent classified",
   missions._classify_failure("KeyError: nope", False, False) == "permanent")
ok("cancellation classified",
   missions._classify_failure("anything", True, False) == "cancelled")
ok("permission denial classified",
   missions._classify_failure("anything", False, True) == "permission_denied")

retrying = missions.create("retry", plan=plan(("Work", "research", "w", [])),
                           autostart=False)
retry_task_id = retrying["mission"] + "-t0"
missions._fail_task({"id": retry_task_id, "mission": retrying["mission"],
                     "title": "Work"}, "connection refused", "transient", 10)
ok("transient failure retries",
   missions.status(retrying["mission"])["tasks"][0]["state"] == "pending")
missions._fail_task({"id": retry_task_id, "mission": retrying["mission"],
                     "title": "Work"}, "connection refused", "transient", 10)
ok("retries are bounded",
   missions.status(retrying["mission"])["tasks"][0]["state"] == "failed",
   missions.status(retrying["mission"])["tasks"][0]["attempts"])
missions._fail_task({"id": retry_task_id, "mission": retrying["mission"],
                     "title": "Work"}, "KeyError", "permanent", 10)
ok("permanent failure does not retry",
   missions.status(retrying["mission"])["tasks"][0]["state"] == "failed")
ok("retry_mission_task resets it",
   missions.retry_task(retry_task_id)["state"] == "pending")

# ── synthesis and observability ──────────────────────────────────────────
print("--- synthesis and observability ---")
synth = missions.create("synth", plan=plan(
    ("One", "research", "one", []), ("Two", "research", "two", [])),
    autostart=False)
connection = missions._connect()
for index, summary in enumerate(("first finding", "second finding")):
    missions._set_task(connection, synth["mission"] + "-t" + str(index),
                       state="completed",
                       result=json.dumps({"summary": summary}))
missions._advance(connection, synth["mission"])
connection.close()
done = missions.status(synth["mission"], include_log=True)
ok("mission completes when all tasks finish", done["state"] == "completed")
ok("result synthesised from real task output",
   "first finding" in done["result"] and "second finding" in done["result"])
ok("progress reaches 100", done["progress"] == 100)
ok("event log recorded", len(done["log"]) > 0, len(done["log"]))
observed = done["tasks"][0]
ok("observability fields present",
   all(k in observed for k in ("id", "title", "agent", "state", "attempts",
                               "latency", "failure_kind")))

listed = missions.listing()
ok("missions listed", listed["ok"] and listed["count"] > 0, listed["count"])
ok("listing reports agents", "research" in listed["missions"][0]["agents"])

# ── retention ────────────────────────────────────────────────────────────
print("--- retention ---")
connection = missions._connect()
missions._set_mission(connection, synth["mission"],
                      finished=time.time() - 40 * 86400)
connection.close()
pruned = missions.prune(days=7)
ok("old mission pruned", pruned["removed"] >= 1, pruned["removed"])
ok("and its tasks went with it", not missions.status(synth["mission"])["ok"])

# ── safety ───────────────────────────────────────────────────────────────
print("--- safety ---")
ok("start_mission is gated at medium", risk.level("start_mission") == "medium")
ok("mission_status is read-only", risk.level("mission_status") == "safe")
ok("cancel_mission is medium", risk.level("cancel_mission") == "medium")
ok("missions wait longer for approval", missions.APPROVAL_WAIT > 180)
ok("every agent declares whether it mutates",
   all("mutates" in spec for spec in missions.AGENTS.values()))
ok("verification agent is read-only",
   missions.AGENTS["verification"]["mutates"] is False)
ok("agents draw on real capability groups", all(
   group in __import__("jarvish.capabilities", fromlist=["x"]).GROUPS
   for spec in missions.AGENTS.values() for group in spec["groups"]))

print("\n%d passed, %d failed" % (P, F))
