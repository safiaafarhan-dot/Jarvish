"""P5 tests: the background monitor, notification intelligence and task engine.

These exercise the real subsystems — a real SQLite store, a real runner thread,
real cooldown arithmetic — rather than mocks.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvish import proactive, risk, tasks   # noqa: E402

# The running server has a task runner of its own against the shared database.
# Now that claiming is atomic, whichever runner wins executes the task exactly
# once — which is correct, but means this process cannot assume it ran the task
# itself. Point the tests at their own database so they are deterministic and
# never compete with the live server.
import pathlib   # noqa: E402
tasks.DB_PATH = pathlib.Path(tasks.DATA_DIR) / "tasks-test.db"
for _stale in tasks.DB_PATH.parent.glob("tasks-test.db*"):
    try:
        _stale.unlink()
    except OSError:
        pass

P = F = 0


def ok(label, cond, extra=""):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
    print("  [%s] %-46s %s" % ("PASS" if cond else "FAIL", label, str(extra)[:48]))


def reset_notifications():
    proactive._notifications.clear()
    proactive._last_fired.clear()
    proactive._history.clear()
    proactive.configure(enabled=True, quiet=False, min_level="low")


# ── monitor lifecycle ────────────────────────────────────────────────────
print("--- monitor ---")
proactive.stop()
ok("monitor starts", proactive.start())
ok("second start is a no-op", not proactive.start())
time.sleep(0.3)
st = proactive.monitor_status()
ok("monitor reports running", st["running"], "interval=%ss" % st["interval"])
ok("monitor stops", proactive.stop())
ok("monitor reports stopped", not proactive.monitor_status()["running"])

# ── observation -> insight ───────────────────────────────────────────────
print("--- insight generation (no model involved) ---")
frame = {"memory": 97.2, "cpu": 12, "disk": 40, "battery": None, "disk_free_gb": 500}
found = proactive.evaluate(frame)
ok("memory pressure detected", any(i["key"] == "memory-pressure" for i in found))
insight = next(i for i in found if i["key"] == "memory-pressure")
ok("observation is factual", "97.2" in insight["observation"], insight["observation"])
ok("insight is separate from observation", insight["insight"] != insight["observation"])
ok("suggestion offered, not performed", bool(insight["suggestion"]))
ok("critical level at 97%", insight["level"] == "critical", insight["level"])

quiet_frame = {"memory": 40, "cpu": 10, "disk": 30, "battery": None, "disk_free_gb": 500}
ok("healthy machine raises nothing", proactive.evaluate(quiet_frame) == [])

low_batt = {"memory": 40, "cpu": 10, "disk": 30, "disk_free_gb": 500,
            "battery": {"percent": 8, "plugged": False, "minutes": 15}}
ok("low battery detected", any(i["key"] == "battery-low" for i in proactive.evaluate(low_batt)))

# ── thresholds ───────────────────────────────────────────────────────────
print("--- thresholds ---")
ok("89% memory is below threshold",
   not any(i["key"] == "memory-pressure" for i in proactive.evaluate(dict(frame, memory=89))))
ok("91% memory fires high",
   next(i for i in proactive.evaluate(dict(frame, memory=91))
        if i["key"] == "memory-pressure")["level"] == "high")

# ── deduplication and cooldown ───────────────────────────────────────────
print("--- notification noise control ---")
reset_notifications()
first = proactive.raise_event("test-cond", "high", "system", "T", "first", fingerprint="a")
ok("first notification raised", first["raised"])
second = proactive.raise_event("test-cond", "high", "system", "T", "again", fingerprint="a")
ok("duplicate suppressed by cooldown", not second["raised"], second.get("reason"))
ok("only one live notification", proactive.notifications()["count"] == 1)

changed = proactive.raise_event("test-cond", "high", "system", "T", "worse", fingerprint="b")
ok("re-raises on a real change", changed["raised"])
escalated = proactive.raise_event("test-cond", "critical", "system", "T", "worst",
                                  fingerprint="b")
ok("re-raises on escalation", escalated["raised"])
ok("still one live entry for the condition",
   sum(1 for n in proactive.notifications()["notifications"] if n["key"] == "test-cond") == 1)

reset_notifications()
proactive.raise_event("burst", "medium", "system", "T", "x", fingerprint="1")
suppressed = sum(1 for _ in range(50)
                 if not proactive.raise_event("burst", "medium", "system", "T", "x",
                                              fingerprint="1")["raised"])
ok("50 repeats produce no extra alerts", suppressed == 50 and
   proactive.notifications()["count"] == 1)

# ── quiet mode and categories ────────────────────────────────────────────
print("--- quiet mode, categories, levels ---")
reset_notifications()
proactive.configure(quiet=True)
r = proactive.raise_event("quiet-test", "high", "system", "T", "m")
ok("quiet mode blocks high", not r["raised"], r.get("reason"))
r = proactive.raise_event("quiet-crit", "critical", "system", "T", "m")
ok("quiet mode still lets critical through", r["raised"])
proactive.configure(quiet=False)

proactive.configure(category="storage", on=False)
r = proactive.raise_event("disk-test", "high", "storage", "T", "m")
ok("disabled category blocked", not r["raised"], r.get("reason"))
proactive.configure(category="storage", on=True)

proactive.configure(min_level="high")
r = proactive.raise_event("low-test", "low", "system", "T", "m")
ok("below minimum level blocked", not r["raised"], r.get("reason"))
proactive.configure(min_level="low")

proactive.configure(enabled=False)
r = proactive.raise_event("off-test", "critical", "system", "T", "m")
ok("proactive disabled blocks everything", not r["raised"], r.get("reason"))
proactive.configure(enabled=True)

ok("settings persisted to disk", proactive.SETTINGS_PATH.exists())

reset_notifications()
proactive.raise_event("ack-test", "medium", "system", "T", "m")
ok("acknowledge marks seen", proactive.acknowledge("ack-test")["acknowledged"] == 1)
ok("dismiss removes", proactive.dismiss("ack-test")["dismissed"] == 1)

# ── task engine ──────────────────────────────────────────────────────────
print("--- task engine ---")
tasks.shutdown()
for row in tasks.listing(limit=200)["tasks"]:
    tasks.cancel(row["id"])
tasks.clear_finished()

made = tasks.create("say hello", title="probe", delay=9999)
ok("task created", made["ok"], made.get("task"))
task_id = made["task"]
ok("starts pending", tasks.status(task_id)["state"] == "pending")
ok("persisted to sqlite", tasks.DB_PATH.exists())

ok("pause works", tasks.pause(task_id)["state"] == "paused")
ok("resume works", tasks.resume(task_id)["state"] == "pending")
ok("cancel works", tasks.cancel(task_id)["state"] == "cancelled")
ok("cancelled task cannot pause", not tasks.pause(task_id)["ok"])
ok("unknown task -> error", not tasks.status("nope-xyz")["ok"])
ok("empty prompt -> error", not tasks.create("")["ok"])
ok("too-frequent repeat -> error", not tasks.create("x", every=5)["ok"])

repeating = tasks.create("check something", title="repeater", delay=9999, every=60)
ok("recurring task accepted", repeating["ok"] and repeating["repeats_every"] == 60)
tasks.cancel(repeating["task"])

dep = tasks.create("second", title="dependent", delay=0, depends_on="nonexistent-task")
ok("dependency recorded", tasks.status(dep["task"])["depends_on"] == "nonexistent-task")
tasks.cancel(dep["task"])

# ── restart recovery ─────────────────────────────────────────────────────
print("--- restart recovery ---")
crashed = tasks.create("interrupted work", title="crashy", delay=9999)
connection = tasks._connect()
tasks._set(connection, crashed["task"], state="running", started=time.time())
connection.close()
ok("task looks running before recovery", tasks.status(crashed["task"])["state"] == "running")
recovered = tasks.recover()
ok("recovery requeued it", tasks.status(crashed["task"])["state"] == "pending",
   "recovered=%s" % recovered)

exhausted = tasks.create("doomed", title="doomed", delay=9999)
connection = tasks._connect()
tasks._set(connection, exhausted["task"], state="running", attempts=5, max_attempts=2)
connection.close()
tasks.recover()
ok("exhausted task marked failed", tasks.status(exhausted["task"])["state"] == "failed")
ok("retry resets a failed task", tasks.retry(exhausted["task"])["state"] == "pending")
tasks.cancel(crashed["task"]); tasks.cancel(exhausted["task"])

# ── real execution through the agent ─────────────────────────────────────
print("--- real background execution (uses the live model) ---")
tasks.ensure_runner()
live = tasks.create("Use get_time and report the time in one short sentence.",
                    title="live-probe", delay=0, timeout=300)
ok("runner started", tasks.listing()["runner"])
final = None
for _ in range(150):
    time.sleep(2)
    state = tasks.status(live["task"])
    if state["state"] in ("completed", "failed", "cancelled"):
        final = state
        break
if final is None:
    ok("task completed", False, "still %s" % tasks.status(live['task'])['state'])
else:
    ok("task completed", final["state"] == "completed", final["state"])
    ok("task recorded a result", bool(final.get("result")), str(final.get("result"))[:44])
    ok("task used a tool",
       "get_time" in (final.get("result") or {}).get("tools", []),
       (final.get("result") or {}).get("tools"))
    # The task is marked completed and *then* notified, so the notification
    # trails the state change by a moment. Wait for it rather than racing it.
    notified = False
    for _ in range(15):
        if any(n["key"].startswith("task-done")
               for n in proactive.notifications()["notifications"]):
            notified = True
            break
        time.sleep(0.4)
    ok("completion notified", notified)
    if not notified:
        row = tasks.status(live["task"])
        print("      live keys :", [n["key"] for n in proactive.notifications()["notifications"]])
        print("      attempted :", [(h["key"], h.get("suppressed"))
                                    for h in proactive.history(limit=12)["history"]])
        print("      task row  : state=%s runs=%s finished=%s error=%s"
              % (row.get("state"), row.get("runs"), row.get("finished"), row.get("error")))
        print("      expected  : task-done:%s" % live["task"])
        print("      runner err:", list(tasks._runner["errors"]))
        print("      last_fired:", list(proactive._last_fired.keys()))

# ── safety ───────────────────────────────────────────────────────────────
print("--- safety ---")
ok("schedule_task is gated at medium", risk.level("schedule_task") == "medium")
ok("cancel_task is gated at medium", risk.level("cancel_task") == "medium")
ok("list_tasks is read-only", risk.level("list_tasks") == "safe")
ok("background sessions wait longer for approval", tasks.APPROVAL_WAIT > 180)

held = tasks.create("Lock my screen.", title="gated-probe", delay=0, timeout=60)
time.sleep(6)
halted = tasks.stop_all()
ok("emergency stop halts running tasks", isinstance(halted, list))
after = tasks.status(held["task"])["state"]
ok("halted task is not left running", after != "running", after)
tasks.cancel(held["task"])

tasks.shutdown()
print("\n%d passed, %d failed" % (P, F))
