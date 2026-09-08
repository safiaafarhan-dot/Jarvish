"""Full regression across every Jarvish subsystem, run against the live server."""

import json
import pathlib
import threading
import time

import httpx

BASE = "http://127.0.0.1:8000"
c = httpx.Client(timeout=300)
P = F = 0


def ok(label, cond, extra=""):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
    print("  [%s] %-42s %s" % ("PASS" if cond else "FAIL", label, str(extra)[:52]))


print("--- core ---")
h = c.get(BASE + "/api/health").json()
ok("server online", h["online"], h["model"])
ok("at least 115 tools registered", h["tool_count"] >= 115, h["tool_count"])
ok("vision capabilities intact", all(h["vision"][k] for k in ("capture", "ocr", "ui_tree")))

t = c.get(BASE + "/api/tools").json()
ok("every tool graded", sum(t["tiers"].values()) == h["tool_count"], t["tiers"])
ok("gate still at high", t["gate"] == "high")

m = c.get(BASE + "/api/models").json()
ok("model routing works", m["agent"]["model"] in m["tool_capable"], m["agent"]["model"])
ok("vision honestly degraded", m["vision"]["degraded"] and m["vision"]["model"] is None)

tel = c.get(BASE + "/api/telemetry").json()
ok("telemetry live", "cpu" in tel, "cpu=%s" % tel["cpu"])

# Recursive globs. `**/*.md` matched nothing while `*.md` worked, because the
# pattern was tested against a bare filename and the walk was already
# recursive. It failed silently, so the agent answered that the project had no
# markdown files in it — which is the only reason anybody noticed.
def _find(pattern):
    return c.post(BASE + "/api/tool/find_files",
                  json={"pattern": pattern, "root": ".", "limit": 50}).json()

ok("a plain glob finds the readme",
   any(r["path"].endswith("README.md") for r in _find("*.md")["matches"]))
ok("a recursive glob finds it too",
   any(r["path"].endswith("README.md") for r in _find("**/*.md")["matches"]))
ok("the two agree",
   len(_find("**/*.md")["matches"]) == len(_find("*.md")["matches"]))
ok("a backslash recursive glob works", _find("**\*.py")["matches"])
ok("a folder-scoped glob works",
   all("tests" in r["path"] for r in _find("tests/*.py")["matches"]) and
   _find("tests/*.py")["matches"])
ok("a pattern matching nothing is still a clean answer",
   _find("nothing_matches_this*")["ok"] and
   _find("nothing_matches_this*")["matches"] == [])

print("--- vision (P1) ---")
s = c.post(BASE + "/api/vision/observe", json={}).json()
ok("vision observe works", s["ok"], "%s elements" % s.get("element_count"))
r = c.post(BASE + "/api/tool/read_screen_text", json={"region": "top"}).json()
ok("OCR through the API", r["ok"], "%s lines" % len(r.get("lines", [])))

print("--- risk gate ---")
ok("safe tool runs", c.post(BASE + "/api/tool/get_time", json={}).json()["ok"])
ok("gated tool refused 428", c.post(BASE + "/api/tool/lock_screen", json={}).status_code == 428)
r = c.post(BASE + "/api/tool/browser_click", json={"target": "Delete account"})
ok("click 'Delete account' -> critical",
   r.status_code == 428 and r.json().get("risk") == "critical", r.json().get("risk"))
r = c.post(BASE + "/api/tool/browser_click", json={"target": "Next"})
ok("click 'Next' not gated", r.status_code != 428, r.status_code)

print("--- browser (P2) ---")
# The browser tools need a controllable browser; start one the same way a
# user request would.
c.post(BASE + "/api/tool/browser_launch", json={})
b = c.get(BASE + "/api/browser/status").json()
ok("browser status endpoint", "connected" in b, "connected=%s" % b.get("connected"))
r = c.post(BASE + "/api/tool/browser_read", json={}).json()
ok("browser reads DOM via API", r.get("ok"), "%s elements" % r.get("element_count"))
r = c.post(BASE + "/api/tool/browser_tabs", json={}).json()
ok("browser tabs via API", r.get("ok"), "%s tabs" % r.get("count"))

print("--- knowledge (P4) ---")
k = c.get(BASE + "/api/knowledge/status").json()
ok("knowledge status endpoint", k["ok"], "%s files / %s chunks" % (k["files"], k["chunks"]))
ok("keyword search always on", k["keyword_search"])
ok("semantic flag honest", k["semantic_search"] == (k["embedded_chunks"] > 0),
   "semantic=%s embedded=%s" % (k["semantic_search"], k["embedded_chunks"]))
r = c.post(BASE + "/api/knowledge/search",
           json={"query": "risk confirmation gate", "limit": 3}).json()
ok("knowledge search via API", r["ok"] and r["count"] > 0,
   "top=%s" % (r["results"][0]["source"] if r.get("results") else None))
ok("results carry real paths",
   all(pathlib.Path(x["path"]).exists() for x in r.get("results", [])))
ok("results carry line numbers", all(x.get("lines") for x in r.get("results", [])))
ok("method label truthful",
   all(x["method"] in ("keyword", "semantic", "keyword+semantic") for x in r.get("results", [])))
r = c.post(BASE + "/api/tool/find_symbol", json={"name": "run_agent"}).json()
ok("find_symbol via API", r["ok"], r.get("matches", [{}])[0].get("source"))
r = c.post(BASE + "/api/tool/project_overview", json={}).json()
ok("project_overview via API", r["ok"], r.get("stack"))

print("--- frontend ---")
for path, needle in [("/", "panel-knowledge"), ("/", "panel-vision"),
                     ("/static/app.js", "knowledgePanel"),
                     ("/static/app.js", "visionPanel"),
                     ("/static/style.css", "[hidden]"),
                     ("/static/style.css", ".source-chunk")]:
    ok("served %-24s" % (path + " " + needle[:14]), needle in c.get(BASE + path).text)

print("--- proactive (P5) ---")
pr = c.get(BASE + "/api/proactive").json()
ok("proactive endpoint", pr["ok"], "%s alerts" % pr["count"])
ok("monitor running", pr["monitor"]["running"], "%s samples" % pr["monitor"]["samples"])
ok("settings exposed", "quiet" in pr["settings"])

before = c.post(BASE + "/api/proactive/settings", json={"quiet": True}).json()
ok("quiet mode settable", before["settings"]["quiet"])
after = c.post(BASE + "/api/proactive/settings", json={"quiet": False}).json()
ok("quiet mode clearable", not after["settings"]["quiet"])

ins = c.post(BASE + "/api/tool/system_insights", json={}).json()
ok("system_insights via API", ins["ok"], ins.get("summary", "")[:44])

tk = c.get(BASE + "/api/tasks").json()
ok("tasks endpoint", tk["ok"], "runner=%s" % tk["runner"])
ok("task runner alive", tk["runner"])

made = c.post(BASE + "/api/tool/schedule_task",
              json={"what": "Use get_time and report it.", "title": "regress-probe",
                    "in_seconds": 3600}).json()
ok("schedule a task via API", made["ok"], made.get("task"))
probe = made.get("task")
ok("pause via API", c.post(BASE + "/api/tasks/pause", json={"task": probe}).json()["state"] == "paused")
ok("resume via API", c.post(BASE + "/api/tasks/resume", json={"task": probe}).json()["state"] == "pending")
ok("cancel via API", c.post(BASE + "/api/tasks/cancel", json={"task": probe}).json()["state"] == "cancelled")
ok("unknown task action -> 404", c.post(BASE + "/api/tasks/bogus", json={"task": probe}).status_code == 404)

ok("schedule_task graded medium", t["tools"]["schedule_task"]["risk"] == "medium")
ok("list_tasks graded safe", t["tools"]["list_tasks"]["risk"] == "safe")

sel = c.get(BASE + "/api/capabilities", params={"q": "remind me in ten minutes"}).json()["selection"]
ok("task capability group selected", "tasks" in sel["groups"], sel["groups"])
ok("schedule_task offered", "schedule_task" in sel["offered"])

for path, needle in [("/", "panel-tasks"), ("/", "panel-alerts"),
                     ("/static/app.js", "refreshProactive"),
                     ("/static/style.css", ".alert-head")]:
    ok("served %-24s" % (path + " " + needle[:14]), needle in c.get(BASE + path).text)

print("--- developer mode (P6) ---")
pi = c.post(BASE + "/api/tool/project_info", json={"path": "."}).json()
ok("project detected via API", pi["ok"], pi.get("languages"))
ok("commands inferred", "test" in pi.get("commands", {}), pi.get("commands", {}).get("test"))
ok("entry points found", "new.py" in pi.get("entry_points", []))

fc = c.post(BASE + "/api/tool/find_callers", json={"name": "raise_event"}).json()
ok("call graph via API", fc["ok"] and fc["caller_count"] > 0, fc.get("caller_count"))
ok("callers cite real files",
   all(pathlib.Path(x["path"]).exists() for x in fc.get("callers", [])))

fd = c.post(BASE + "/api/tool/find_dependents", json={"module": "risk"}).json()
ok("dependency graph via API", fd["ok"] and fd["count"] >= 2, fd.get("count"))

dg = c.post(BASE + "/api/tool/diagnose_error", json={
    "error": "Traceback (most recent call last):\n"
             '  File "jarvish/llm.py", line 1, in <module>\n'
             "ModuleNotFoundError: No module named 'ghost'"}).json()
ok("traceback diagnosed via API", dg.get("kind") == "python_traceback", dg.get("kind"))
ok("dependency error categorised", dg.get("category") == "dependency")

vague = c.post(BASE + "/api/tool/diagnose_error", json={"error": "broken"}).json()
ok("unparseable error admitted", vague.get("classified") is False)

ran = c.post(BASE + "/api/tool/run_dev_command", json={"command": "python --version"})
ok("safe command runs", ran.status_code == 200 and ran.json().get("succeeded"),
   ran.json().get("stdout", "").strip()[:20])
gated = c.post(BASE + "/api/tool/run_dev_command", json={"command": "rm -rf /"})
ok("destructive command gated", gated.status_code == 428 and
   gated.json().get("risk") == "critical", gated.json().get("risk"))
install = c.post(BASE + "/api/tool/run_dev_command", json={"command": "npm install"})
ok("install command gated", install.status_code == 428, install.status_code)

ok("propose_change is read-only", t["tools"]["propose_change"]["risk"] == "safe")
ok("apply_change is graded medium", t["tools"]["apply_change"]["risk"] == "medium")
ok("git reads are safe", t["tools"]["git_status"]["risk"] == "safe")

gs = c.post(BASE + "/api/tool/git_status", json={"path": "."}).json()
# Honest either way. This used to assert the project was *not* a repository,
# which was true when it was written and stopped being true the moment one was
# initialised — a test that fails on a change to its surroundings rather than
# on a change to the code. What actually matters is that git_status never
# invents an answer: a real repository comes back with a branch and a list of
# changes, and the absence of one is reported as such.
ok("git status reported honestly",
   (gs["ok"] and gs.get("branch") and isinstance(gs.get("changes"), list))
   or (not gs["ok"] and "not a git repository" in gs.get("error", "")),
   gs.get("branch") or gs.get("error", "")[:30])

sel = c.get(BASE + "/api/capabilities", params={"q": "why is this test failing"}).json()["selection"]
ok("dev capability group selected", "dev" in sel["groups"], sel["groups"])
ok("diagnose_error offered", "diagnose_error" in sel["offered"])

for path, needle in [("/", "panel-dev"), ("/static/app.js", "developer.absorb"),
                     ("/static/style.css", ".dev-row")]:
    ok("served %-24s" % (path + " " + needle[:14]), needle in c.get(BASE + path).text)

print("--- capability registry (P7) ---")
reg = c.get(BASE + "/api/registry").json()
ok("registry endpoint", reg["count"] > 0, "%s capabilities" % reg["count"])
ok("built-ins registered through it", reg["sources"].get("builtin", 0) >= 110,
   reg["sources"])
ok("every record has an id", all(":" in e["id"] for e in reg["capabilities"]))
ok("every record has version + permissions",
   all(e["version"] and e["permissions"] is not None for e in reg["capabilities"]))
ok("every record has health", all("status" in e["health"] for e in reg["capabilities"]))
ok("kinds reported", "tool" in reg["kinds"], reg["kinds"])

ok("example plugin loaded", any(info["loaded"] for info in reg["plugins"].values()),
   list(reg["plugins"]))
ok("no plugin failed to load",
   all(info["loaded"] for info in reg["plugins"].values()),
   [n for n, i in reg["plugins"].items() if not i["loaded"]])

one = c.get(BASE + "/api/registry/convert_units").json()
ok("capability detail via API", one["ok"] and one["kind"] == "tool", one.get("version"))
ok("plugin tool graded safe", one["risk"] == "safe", one["risk"])
ok("permissions recorded", one["permissions"] == ["read"], one["permissions"])

ran = c.post(BASE + "/api/tool/convert_units",
             json={"value": 5, "from_unit": "kilometres", "to_unit": "miles"}).json()
ok("plugin tool executes via API", ran["ok"] and "3.1069" in ran["said"], ran.get("said"))

sel = c.get(BASE + "/api/capabilities", params={"q": "convert 5 km to miles"}).json()["selection"]
ok("plugin tool is selectable", "convert_units" in sel["offered"])

health = c.post(BASE + "/api/registry/action/health", json={}).json()
ok("health sweep via API", health["ok"], health.get("summary"))

reloaded = c.post(BASE + "/api/registry/action/reload", json={}).json()
ok("hot reload via API", reloaded["ok"] and not reloaded["failed"],
   "%s loaded" % len(reloaded["loaded"]))
ok("tool survives reload",
   c.post(BASE + "/api/tool/convert_units",
          json={"value": 1, "from_unit": "m", "to_unit": "cm"}).json()["value"] == 100)

off = c.post(BASE + "/api/registry/action/disable",
             json={"capability": "example_units:convert_units"}).json()
ok("disable via API", off["ok"] and off["enabled"] is False)
ok("disabled tool is gone", c.post(BASE + "/api/tool/convert_units",
                                   json={"value": 1, "from_unit": "m",
                                         "to_unit": "cm"}).status_code == 404)
on = c.post(BASE + "/api/registry/action/enable",
            json={"capability": "example_units:convert_units"}).json()
ok("enable via API", on["ok"] and on["enabled"] is True)
ok("re-enabled tool works again",
   c.post(BASE + "/api/tool/convert_units",
          json={"value": 1, "from_unit": "m", "to_unit": "cm"}).json()["value"] == 100)
ok("unknown registry action -> 404",
   c.post(BASE + "/api/registry/action/bogus", json={}).status_code == 404)

vs = c.get(BASE + "/api/vision/status").json()
ok("vision surfaces registered providers", "registered_providers" in vs)

for path, needle in [("/", "panel-caps"), ("/static/app.js", "registry.refresh"),
                     ("/static/style.css", ".cap-head")]:
    ok("served %-24s" % (path + " " + needle[:14]), needle in c.get(BASE + path).text)

print("--- missions (P8) ---")
ms = c.get(BASE + "/api/missions").json()
ok("missions endpoint", ms["ok"], "%s missions" % ms["count"])
ok("mission runner alive", ms["runner"])
ok("agents registered", len(h["missions"]["agents"]) == 7, h["missions"]["agents"])

started = c.post(BASE + "/api/missions/action/start", json={
    "goal": "regression probe",
    "plan": [{"title": "A", "agent": "research", "instruction": "a", "depends_on": []},
             {"title": "B", "agent": "verification", "instruction": "b",
              "depends_on": ["A"]}]}).json()
ok("mission started via API", started["ok"], started.get("mission"))
probe = started["mission"]
ok("graph persisted", started["tasks"] == 2)

detail = c.get(BASE + "/api/missions/" + probe).json()
ok("mission detail via API", detail["ok"] and len(detail["tasks"]) == 2)
ok("dependency recorded",
   detail["tasks"][1]["depends_on"] == [probe + "-t0"], detail["tasks"][1]["depends_on"])
ok("observability fields present",
   all(k in detail["tasks"][0] for k in ("id", "agent", "state", "attempts",
                                         "latency", "failure_kind")))

paused = c.post(BASE + "/api/missions/action/pause", json={"mission": probe}).json()
ok("pause via API", paused["ok"] and paused["state"] == "paused")
resumed = c.post(BASE + "/api/missions/action/resume", json={"mission": probe}).json()
ok("resume via API", resumed["ok"] and resumed["state"] == "running")
cancelled = c.post(BASE + "/api/missions/action/cancel", json={"mission": probe}).json()
ok("cancel via API", cancelled["ok"] and cancelled["state"] == "cancelled")
after = c.get(BASE + "/api/missions/" + probe).json()
ok("cancellation reached every task",
   all(x["state"] == "cancelled" for x in after["tasks"]),
   [x["state"] for x in after["tasks"]])

ok("no goal -> 400",
   c.post(BASE + "/api/missions/action/start", json={}).status_code == 400)
ok("unknown action -> 404",
   c.post(BASE + "/api/missions/action/bogus", json={}).status_code == 404)
ok("unknown mission -> error",
   not c.get(BASE + "/api/missions/nope").json()["ok"])

ok("start_mission graded medium", t["tools"]["start_mission"]["risk"] == "medium")
ok("mission_status is read-only", t["tools"]["mission_status"]["risk"] == "safe")

sel = c.get(BASE + "/api/capabilities",
            params={"q": "investigate why the build fails and fix it"}).json()["selection"]
ok("mission capability group selectable",
   "missions" in sel["groups"] or "start_mission" in sel["offered"], sel["groups"])

for path, needle in [("/", "panel-mission"), ("/static/app.js", "mission.refresh"),
                     ("/static/style.css", ".mtask")]:
    ok("served %-24s" % (path + " " + needle[:14]), needle in c.get(BASE + path).text)

print("--- cognition (P9) ---")

# The ceiling has to survive a round trip through the API, and it has to be
# restored afterwards: this suite runs against the live installation, and
# leaving it at L0 would silently disarm the machine.
before = c.get(BASE + "/api/autonomy").json()
ok("autonomy reported", before["ok"], "L%s %s" % (before["level"], before["name"]))
ok("every level described", len(before["levels"]) == 6)
ok("health carries the ceiling too",
   c.get(BASE + "/api/health").json().get("autonomy", {}).get("level") == before["level"])
ok("the note says it is a ceiling", "ceiling" in before["note"])

set_low = c.post(BASE + "/api/autonomy", json={"level": 1}).json()
ok("level set through the API", set_low["ok"] and set_low["level"] == 1)
ok("and reads back", c.get(BASE + "/api/autonomy").json()["level"] == 1)
ok("bad level refused", not c.post(BASE + "/api/autonomy", json={"level": 9}).json()["ok"])
ok("missing level -> 400", c.post(BASE + "/api/autonomy", json={}).status_code == 400)

# A direct tool call is the user acting, not Jarvish acting unattended, so the
# ceiling must not disarm the HUD's own buttons — only the risk gate applies.
ok("direct tool call ignores the ceiling",
   c.post(BASE + "/api/tool/get_time", json={}).json()["ok"])
ok("but the risk gate still holds at L1",
   c.post(BASE + "/api/tool/lock_screen", json={}).status_code == 428)

restored = c.post(BASE + "/api/autonomy", json={"level": before["level"]}).json()
ok("ceiling restored", restored["level"] == before["level"], "L%s" % restored["level"])

ctx = c.get(BASE + "/api/context", params={"q": "what is running on my machine"}).json()
ok("context assembled on demand", ctx["ok"], "%sms" % ctx.get("ms"))
ok("every fragment names its source",
   all("source" in f and "obtained" in f for f in ctx["fragments"]))
tight = c.get(BASE + "/api/context", params={"q": "system", "budget": 200}).json()
ok("context respects a budget", tight["characters"] <= tight["budget"],
   "%s/%s chars" % (tight["characters"], tight["budget"]))

rel = c.get(BASE + "/api/strategies").json()
ok("tool reliability reported", rel["ok"], "%s tools" % rel["tools"])
ok("strategy recall answers", c.get(BASE + "/api/strategies",
                                    params={"goal": "nothing was ever done like this"}
                                    ).json()["ok"])

t = c.get(BASE + "/api/tools").json()
ok("set_autonomy is gated at medium", t["tools"]["set_autonomy"]["risk"] == "medium")
ok("context tool is read-only", t["tools"]["current_context"]["risk"] == "safe")
ok("reading the level is read-only", t["tools"]["autonomy_level"]["risk"] == "safe")

for path, needle in [("/", "panel-autonomy"), ("/", "link-autonomy"),
                     ("/static/app.js", "autonomy.refresh"),
                     ("/static/app.js", "autonomy_blocked"),
                     ("/static/style.css", ".autonomy-level")]:
    ok("served %-24s" % (path + " " + needle[:14]), needle in c.get(BASE + path).text)

print("--- humanoid presence (visual only) ---")

hud = c.get(BASE + "/").text
js = c.get(BASE + "/static/humanoid.js").text
app = c.get(BASE + "/static/app.js").text
css = c.get(BASE + "/static/style.css").text

ok("humanoid canvas served", 'id="humanoid"' in hud)
ok("renderer script served", "jarvishHumanoid" in js, "%d KB" % (len(js) // 1024))
ok("layer styled and click-through",
   ".humanoid" in css and "pointer-events: none" in css)

# It must be a mirror. The moment this owns state, there are two answers to
# "what is Jarvish doing" and they will disagree.
ok("reads the agent state", "state.agent" in app)
ok("reads the voice state", "persistentVoice.state" in app)
ok("maps every documented state",
   all(w in js for w in ("listening", "processing", "speaking", "confirming", "error")))
ok("no second state machine", "setState(" in js and "run_agent" not in js)
ok("never calls the agent or any tool",
   "/api/chat" not in js and "/api/tool" not in js and "/api/confirm" not in js)
ok("cannot approve anything", "resolve_approval" not in js and "approved" not in js)

# Performance and lifecycle properties that are checkable without a browser.
ok("one draw call per frame", js.count("drawArrays") == 1)
ok("animation lives in the shader, not a JS particle loop",
   "gl_Position" in js and "for (let" not in js.split("draw(now)")[1][:1200])
ok("quality tiers present", "4500" in js and "9000" in js and "16000" in js)
ok("disposes GPU resources", all(w in js for w in
   ("deleteBuffer", "deleteVertexArray", "deleteProgram", "loseContext")))
ok("removes its listeners", js.count("removeEventListener") >= 3)
ok("stops drawing in a hidden tab", "visibilitychange" in js and "document.hidden" in js)
ok("honours reduced motion", "prefers-reduced-motion" in js)
ok("degrades without WebGL2", "webgl2" in js and "canvas.hidden = true" in app)

print("--- emergency stop ---")
sid = None


def stopper():
    for _ in range(500):
        if sid:
            break
        time.sleep(0.02)
    time.sleep(2.0)
    c.post(BASE + "/api/stop", json={"session": sid})


threading.Thread(target=stopper, daemon=True).start()
stopped, ran = False, 0
with c.stream("POST", BASE + "/api/chat", json={"messages": [{"role": "user",
              "content": "Check the time, then system info, then processes, then wifi status."}]}) as r:
    for line in r.iter_lines():
        if not line.startswith("data:"):
            continue
        e = json.loads(line[5:].strip())
        if e["type"] == "session":
            sid = e["id"]
        elif e["type"] == "tool_end":
            ran += 1
        elif e["type"] == "done":
            stopped = bool(e.get("stopped"))
ok("emergency stop halts chain", stopped, "%d tools ran" % ran)
halt = c.post(BASE + "/api/stop", json={}).json()
ok("stop also halts background tasks", "tasks_halted" in halt,
   halt.get("tasks_halted"))
ok("stop also halts missions", "missions_halted" in halt,
   halt.get("missions_halted"))

print("\n%d passed, %d failed" % (P, F))
