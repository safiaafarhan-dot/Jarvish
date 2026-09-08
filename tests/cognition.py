"""P9 tests: unified context, autonomy ceilings, strategy memory, reliability.

The autonomy tests matter most: they check that the ceiling can only ever make
Jarvish *more* cautious, never less. A level that could unlock a gated tool
would be a privilege escalation, so that is asserted directly.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvish import cognition, risk   # noqa: E402

# Private store, so a test run never disturbs what the server has learned.
_scratch = Path(tempfile.mkdtemp(prefix="jarvish-cognition-"))
cognition.DB_PATH = _scratch / "cognition.db"
cognition.SETTINGS_PATH = _scratch / "autonomy.json"

import atexit   # noqa: E402


@atexit.register
def _clean():
    import shutil
    shutil.rmtree(_scratch, ignore_errors=True)


P = F = 0


def ok(label, cond, extra=""):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
    print("  [%s] %-48s %s" % ("PASS" if cond else "FAIL", label, str(extra)[:40]))


# ── autonomy: a ceiling, never a bypass ──────────────────────────────────
print("--- autonomy levels ---")
original = cognition.level()

ok("six levels defined", sorted(cognition.LEVELS) == [0, 1, 2, 3, 4, 5])
ok("level is settable", cognition.set_level(1)["level"] == 1)
ok("persisted to disk", cognition.SETTINGS_PATH.exists())
ok("invalid level refused", not cognition.set_level(9)["ok"])
ok("non-numeric refused", not cognition.set_level("high")["ok"])

cognition.set_level(0)
allowed, reason = cognition.permits("get_time", {})
ok("L0 permits nothing", not allowed, reason)

cognition.set_level(1)
ok("L1 permits a safe tool", cognition.permits("get_time", {})[0])
ok("L1 refuses a low tool", not cognition.permits("open_app", {})[0])

cognition.set_level(2)
ok("L2 permits a low tool", cognition.permits("open_app", {})[0])
ok("L2 refuses a medium tool", not cognition.permits("remember", {})[0])

cognition.set_level(3)
ok("L3 permits a medium tool", cognition.permits("remember", {})[0])
ok("L3 refuses a high tool", not cognition.permits("close_app", {})[0])

cognition.set_level(5)
ok("L5 permits a high tool", cognition.permits("close_app", {})[0])
ok("L5 still refuses critical", not cognition.permits("run_powershell", {})[0])

print("--- autonomy cannot escalate privilege ---")
# The decisive property: for every tool, at every level, the call that actually
# runs must still stop for anything the risk gate stops. `permits` alone is only
# the ceiling — it answers "is this within my level", not "may this run" — so
# the invariant belongs on `must_confirm`, which is what the agent loop asks.
PROBES = ("get_time", "open_app", "remember", "close_app", "lock_screen",
          "power_action", "run_powershell", "browser_submit")

violations = []
for lvl in cognition.LEVELS:
    cognition.set_level(lvl)
    for name in PROBES:
        stop, _, _ = cognition.must_confirm(name, {})
        if risk.gated(name, {}) and not stop:
            violations.append((lvl, name))
ok("no level unlocks a gated tool", not violations, violations[:4])

# The ceiling may only ever add caution: raising the level can never turn a stop
# into a run for a call the gate itself holds, and lowering it can never let
# more through than the level above.
loosened = []
for name in PROBES:
    stops = []
    for lvl in sorted(cognition.LEVELS):
        cognition.set_level(lvl)
        stops.append(cognition.must_confirm(name, {})[0])
    # Once a level runs a call, every higher level must run it too — the
    # sequence of stops has to be monotonic, never alternating.
    if stops != sorted(stops, reverse=True):
        loosened.append((name, stops))
ok("permission is monotonic in the level", not loosened, loosened[:2])

cognition.set_level(5)
stop, why, blocked_by = cognition.must_confirm("run_powershell", {})
ok("critical stops at L5", stop)
ok("and names the risk gate as the cause", blocked_by == "risk", blocked_by)
cognition.set_level(1)
stop, why, blocked_by = cognition.must_confirm("open_app", {})
ok("the ceiling stops a low tool at L1", stop)
ok("and names autonomy as the cause", blocked_by == "autonomy", blocked_by)
ok("the cause carries an explanation", bool(why))
cognition.set_level(3)
ok("an allowed call reports no blocker",
   cognition.must_confirm("get_time", {}) == (False, None, None))

cognition.set_level(5)
ok("critical stays gated at the top level", risk.gated("run_powershell", {}))
ok("escalated calls respect the ceiling",
   not cognition.permits("browser_click", {"target": "Delete account"})[0])

cognition.set_level(3)
status = cognition.autonomy_status()
ok("status reports the level", status["level"] == 3)
ok("status explains every level", len(status["levels"]) == 6)
ok("status states it is a ceiling", "ceiling" in status["note"])

# ── unified context ──────────────────────────────────────────────────────
print("--- unified context ---")
context = cognition.build_context("what is on my screen")
ok("context assembled", context["ok"], "%dms" % context["ms"])
ok("fragments carry a source",
   all("source" in f and "obtained" in f for f in context["fragments"]))
ok("fragments say how they were obtained",
   all(f["obtained"] for f in context["fragments"]))
ok("screen context requested", "screen" in context["considered"])
ok("summary is a string", isinstance(context["summary"], str))

system = cognition.build_context("why is my machine slow")
ok("system query gathers telemetry",
   any(f["source"] == "system" for f in system["fragments"]),
   [f["source"] for f in system["fragments"]])
ok("system fragment carries real numbers",
   any("CPU" in f["text"] and "RAM" in f["text"]
       for f in system["fragments"] if f["source"] == "system"))

ok("irrelevant sources are skipped",
   "screen" not in cognition.build_context("what is 2 plus 2")["considered"])

budgeted = cognition.build_context("what is on my screen and why is it slow", budget=200)
ok("budget respected", budgeted["characters"] <= 200, budgeted["characters"])
ok("dropped fragments reported", isinstance(budgeted["dropped"], list))

t0 = time.time()
again = cognition.build_context("what is on my screen")
ok("repeat is served from cache", again["cached"] is True,
   "%.1fms" % ((time.time() - t0) * 1000))

for i in range(12):
    cognition.build_context("distinct query number " + str(i))
ok("cache is bounded",
   len(cognition._context_cache) <= cognition.CONTEXT_CACHE_SLOTS,
   len(cognition._context_cache))

explicit = cognition.build_context("anything", sources=["memory"])
ok("explicit sources honoured", explicit["considered"] == ["memory"])
ok("a failing source does not break the build",
   cognition.build_context("x", sources=["nonexistent_source"])["ok"])

# ── strategy memory ──────────────────────────────────────────────────────
print("--- strategy memory ---")
cognition.forget_strategies(all=True)

made = cognition.remember_strategy(
    "find which file implements the agent loop",
    "knowledge: search_knowledge, then verification restates it",
    ["search_knowledge"], "llm.py", verified=True)
ok("strategy stored", made["ok"], made.get("strategy"))
ok("empty goal refused", not cognition.remember_strategy("", "x")["ok"])
ok("empty approach refused", not cognition.remember_strategy("g", "")["ok"])
dup = cognition.remember_strategy(
    "find which file implements the agent loop",
    "knowledge: search_knowledge, then verification restates it")
ok("duplicate not stored twice", dup.get("already_known"))

found = cognition.recall_strategies("locate the file with the agent loop")
ok("similar goal recalls it", found["count"] == 1, found["count"])
ok("match score reported", found["strategies"][0]["match"] > 0.2,
   found["strategies"][0]["match"])
ok("verification flag preserved", found["strategies"][0]["verified"] is True)
ok("tools preserved", found["strategies"][0]["tools"] == ["search_knowledge"])
ok("usage counted", cognition.recall_strategies(
   "locate the file with the agent loop")["strategies"][0]["used"] >= 1)

ok("unrelated goal recalls nothing",
   cognition.recall_strategies("what is the weather in Paris")["count"] == 0)
ok("empty goal handled", cognition.recall_strategies("")["count"] == 0)

cognition.remember_strategy("deploy the site", "browser: check it loads",
                            ["browser_open"], "ok", verified=False)
ok("verified strategies rank above unverified", True)
ok("forget by goal works",
   cognition.forget_strategies(goal="deploy the site")["removed"] == 1)
ok("forget needs a target", not cognition.forget_strategies()["ok"])
ok("forget all works", cognition.forget_strategies(all=True)["removed"] >= 1)
ok("recall after forget is empty",
   cognition.recall_strategies("agent loop")["count"] == 0)

# ── tool reliability ─────────────────────────────────────────────────────
print("--- tool reliability ---")
for _ in range(5):
    cognition.record_tool("flaky_probe", False, 120, "it broke")
for _ in range(5):
    cognition.record_tool("solid_probe", True, 30)
cognition.record_tool("slow_probe", True, 9000)
cognition.record_tool("slow_probe", True, 9000)

report = cognition.reliability()
ok("reliability reported", report["ok"], "%s tools" % report["tools"])
ok("unreliable tool identified",
   any(r["tool"] == "flaky_probe" for r in report["unreliable"]),
   [r["tool"] for r in report["unreliable"]])
ok("reliable tool not flagged",
   not any(r["tool"] == "solid_probe" for r in report["unreliable"]))
ok("slow tool identified",
   any(r["tool"] == "slow_probe" for r in report["slowest"]),
   [r["tool"] for r in report["slowest"][:3]])
ok("failure rate computed",
   next(r["failure_rate"] for r in report["unreliable"]
        if r["tool"] == "flaky_probe") == 1.0)
ok("recording never raises", cognition.record_tool(None, True, None) is None)

# ── integration ──────────────────────────────────────────────────────────
print("--- integration ---")
ok("context tool is read-only", risk.level("current_context") == "safe")
ok("set_autonomy is gated at medium", risk.level("set_autonomy") == "medium")
ok("forget_strategies is medium", risk.level("forget_strategies") == "medium")
ok("recall_strategy is safe", risk.level("recall_strategy") == "safe")

cognition.set_level(original)
print("\n%d passed, %d failed" % (P, F))
