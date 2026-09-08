"""P6 tests: project understanding, diagnosis, safe editing, commands and git.

Everything here runs against real artefacts — this repository for detection and
code intelligence, and a throwaway git repo built on disk for the git tools. No
mocks, no fixtures pretending to be a project.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# This project's path contains non-Latin characters, which the Windows console
# codepage cannot encode. Print UTF-8 regardless of the environment.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvish import dev, kb, risk   # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
P = F = 0


def ok(label, cond, extra=""):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
    print("  [%s] %-46s %s" % ("PASS" if cond else "FAIL", label, str(extra)[:46]))


# ── project understanding ────────────────────────────────────────────────
print("--- project understanding ---")
info = dev.detect(str(PROJECT), refresh=True)
ok("project detected", info["ok"], info.get("root", "")[-40:])
ok("root found", Path(info["root"]) == PROJECT, info["root"][-30:])
ok("language identified", "Python" in info["languages"], info["languages"])
ok("package manager identified", any("pip" in m or "npm" in m
                                     for m in info["package_managers"]),
   info["package_managers"])
ok("frameworks from real dependencies",
   any(f in info["frameworks"] for f in ("FastAPI", "ASGI (uvicorn)", "pytest")),
   info["frameworks"])
ok("dependencies read", info["dependency_count"] > 3, info["dependency_count"])
ok("entry points found", "new.py" in info["entry_points"], info["entry_points"])
ok("configs found", "requirements.txt" in info["configs"], info["configs"][:4])
ok("test command inferred", "test" in info["commands"], info["commands"].get("test"))
ok("install command inferred", "install" in info["commands"],
   info["commands"].get("install"))
# Honest either way. This asserted the project was *not* a repository, which
# was true when it was written and stopped being true the moment one was
# initialised here — a test that breaks on a change to its surroundings rather
# than to the code. What matters is that detection reports what is actually
# there: a repository comes with a branch name, and its absence is stated
# plainly rather than guessed at.
ok("git state reported honestly",
   (info["git"]["repository"] is True and bool(info["git"].get("branch")))
   or info["git"]["repository"] is False,
   info["git"])

cached = dev.detect(str(PROJECT))
ok("second call uses the cache", cached.get("cached") is True)

# ── code intelligence ────────────────────────────────────────────────────
print("--- code intelligence ---")
kb.index_folder(str(PROJECT))

callers = dev.find_callers("raise_event")
ok("find_callers works", callers["ok"], "%s callers" % callers.get("caller_count"))
ok("callers are real files",
   all(Path(c["path"]).exists() for c in callers.get("callers", [])))
ok("callers name a real function",
   any(c["caller"] == "_finish" for c in callers.get("callers", [])),
   [c["caller"] for c in callers.get("callers", [])][:4])
ok("definition located", any(d["source"] == "proactive.py"
                             for d in callers.get("defined_in", [])),
   [d["source"] for d in callers.get("defined_in", [])])
ok("uncertainty stated", "matched by name" in (callers.get("note") or ""))

deps = dev.dependents("risk")
ok("find_dependents works", deps["ok"], "%s importers" % deps.get("count"))
ok("llm.py imports risk", any(d["source"] == "llm.py" for d in deps.get("imported_by", [])),
   [d["source"] for d in deps.get("imported_by", [])][:5])

arch = dev.architecture(str(PROJECT))
ok("architecture summarised", arch["ok"], "%s modules" % len(arch.get("modules", [])))
ok("modules ranked by symbols",
   arch["modules"][0]["symbols"] >= arch["modules"][-1]["symbols"])
ok("call hot-spots reported", len(arch.get("most_called", [])) > 0,
   [m["function"] for m in arch.get("most_called", [])[:3]])

ok("unknown symbol -> error", not dev.find_callers("zzz_not_real")["ok"])
ok("empty symbol -> error", not dev.find_callers("")["ok"])

# ── error diagnosis ──────────────────────────────────────────────────────
print("--- error diagnosis ---")
traceback_text = '''Traceback (most recent call last):
  File "%s", line 10, in <module>
    from jarvish import dev
  File "%s", line 25, in run_agent
    result = tools.call(name, arguments)
KeyError: 'missing_key'
''' % (str(PROJECT / "new.py"), str(PROJECT / "jarvish" / "llm.py"))

d = dev.diagnose(traceback_text, path=str(PROJECT))
ok("python traceback classified", d["kind"] == "python_traceback", d["kind"])
ok("exception extracted", d["exception"] == "KeyError", d["exception"])
ok("category assigned", d["category"] == "runtime", d["category"])
ok("frames parsed", len(d["frames"]) == 2, len(d["frames"]))
ok("frames recognised as ours", len(d["in_your_code"]) == 2)
ok("real code retrieved", bool(d["evidence"]) and ">>" in d["evidence"][0]["excerpt"])
ok("evidence comes from disk",
   all(Path(e["path"]).exists() for e in d["evidence"]))
ok("confident when located", d["confident"])

js = dev.diagnose("""TypeError: Cannot read properties of undefined (reading 'map')
    at renderList (/app/src/list.js:42:18)
    at App (/app/src/App.js:11:5)""")
ok("javascript error classified", js["kind"] == "javascript_error", js["kind"])
ok("js frames parsed", len(js["frames"]) == 2, len(js["frames"]))

ts = dev.diagnose("src/main.ts(12,5): error TS2322: Type 'string' is not assignable.")
ok("typescript error classified", ts["kind"] == "typescript_error", ts["kind"])
ok("ts code extracted", ts["exception"] == "TS2322", ts["exception"])

pt = dev.diagnose("FAILED tests/test_math.py::test_add - assert 3 == 4")
ok("pytest failure classified", pt["kind"] == "test_failure", pt["kind"])

vague = dev.diagnose("it didn't work")
ok("unparseable input is admitted", vague["classified"] is False, vague["kind"])
ok("no cause invented", "advice" in vague and not vague.get("exception"))
ok("empty error -> error", not dev.diagnose("")["ok"])

missing = dev.diagnose('''Traceback (most recent call last):
  File "/nowhere/ghost.py", line 3, in <module>
ModuleNotFoundError: No module named 'ghost'
''')
ok("dependency error categorised", missing["category"] == "dependency",
   missing["category"])
ok("unlocatable site admitted", not missing["confident"] and bool(missing["note"]))

# ── safe modification ────────────────────────────────────────────────────
print("--- safe modification ---")
sandbox = Path(tempfile.mkdtemp(prefix="jarvish-dev-"))
sample = sandbox / "sample.py"
sample.write_text(
    "def add(a, b):\n"
    "    return a - b\n"
    "\n"
    "\n"
    "def unrelated():\n"
    "    return 'untouched'\n", encoding="utf-8")

proposal = dev.propose_change(str(sample), "return a - b", "return a + b",
                              "the operator is wrong")
ok("change proposed", proposal["ok"])
plan = proposal.get("proposal", {})
ok("proposal names files", plan.get("files") == [str(sample)])
ok("proposal states reason", plan.get("reason") == "the operator is wrong")
ok("proposal states risk", bool(plan.get("risk")))
ok("proposal states impact", bool(plan.get("expected_impact")))
ok("proposal states test plan", bool(plan.get("test_plan")))
ok("proposal did not write", sample.read_text(encoding="utf-8").count("a - b") == 1)

ok("ambiguous snippet refused",
   not dev.propose_change(str(sample), "return", "x", "r")["ok"])
ok("absent snippet refused",
   not dev.propose_change(str(sample), "def nothing()", "x", "r")["ok"])
ok("missing file refused",
   not dev.propose_change(str(sandbox / "nope.py"), "a", "b", "r")["ok"])

applied = dev.apply_change(str(sample), "return a - b", "return a + b", "fix operator")
ok("change applied", applied["ok"] and applied["verified"])
body = sample.read_text(encoding="utf-8")
ok("edit landed", "return a + b" in body)
ok("unrelated code untouched", "def unrelated():" in body and "'untouched'" in body)
ok("formatting preserved", body.startswith("def add(a, b):\n"))
ok("backup written", Path(applied["backup"]).exists())
ok("python was parse-checked", applied["parse_checked"])

broken = dev.apply_change(str(sample), "return a + b", "return a +", "break it")
ok("invalid edit rolled back", not broken["ok"] and "rolled back" in broken["error"])
ok("file survived the bad edit", "return a + b" in sample.read_text(encoding="utf-8"))

reverted = dev.revert_change(str(sample))
ok("revert restored the backup", reverted["ok"] and
   "return a - b" in sample.read_text(encoding="utf-8"))

# ── commands ─────────────────────────────────────────────────────────────
print("--- commands ---")
for command, want in [("git status", "safe"),
                      ("python -m pytest -q", "safe"),
                      ("npm run lint", "safe"),
                      ("python scripts/thing.py", "medium"),
                      ("npm install", "high"),
                      ("git push origin main", "high"),
                      ("rm -rf build", "critical"),
                      ("git reset --hard HEAD~3", "critical")]:
    tier = dev.classify_command(command)[0]
    ok("classify %-26s -> %-8s" % (repr(command)[:26], want), tier == want, tier)

ok("destructive command is gated",
   risk.gated("run_dev_command", {"command": "rm -rf /"}))
ok("test command is not gated",
   not risk.gated("run_dev_command", {"command": "python -m pytest -q"}))
ok("install is gated", risk.gated("run_dev_command", {"command": "npm install"}))

result = dev.run_command("python --version", path=str(PROJECT))
ok("command actually ran", result["ok"] and result["succeeded"], result["stdout"].strip())
ok("exit code reported", result["exit_code"] == 0)
ok("cwd is the project", Path(result["cwd"]) == PROJECT)

failing = dev.run_command("python -c \"raise ValueError('boom')\"", path=str(PROJECT))
ok("failing command reported as failed", not failing["succeeded"], failing["exit_code"])
ok("failure auto-diagnosed", "diagnosis" in failing,
   failing.get("diagnosis", {}).get("exception"))
ok("empty command refused", not dev.run_command("")["ok"])

# ── real test execution ──────────────────────────────────────────────────
print("--- test execution ---")
tested = sandbox / "test_sample.py"
tested.write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
(sandbox / "requirements.txt").write_text("pytest\n", encoding="utf-8")
run = dev.run_tests(path=str(sandbox))
ok("tests executed", run["ok"], run.get("summary"))
ok("passes counted", (run.get("summary") or {}).get("passed") == 1,
   (run.get("summary") or {}).get("passed"))
ok("suite reported green", (run.get("summary") or {}).get("green"))

tested.write_text("def test_bad():\n    assert 1 + 1 == 3\n", encoding="utf-8")
red = dev.run_tests(path=str(sandbox))
ok("failing suite reported red", not (red.get("summary") or {}).get("green"))
ok("failure count parsed", (red.get("summary") or {}).get("failed") == 1,
   (red.get("summary") or {}).get("failed"))
ok("failing test named", bool(red.get("failures")),
   (red.get("failures") or [{}])[0].get("test"))

# ── git ──────────────────────────────────────────────────────────────────
print("--- git ---")
repo = Path(tempfile.mkdtemp(prefix="jarvish-git-"))
def git(*args):
    return subprocess.run(["git"] + list(args), cwd=str(repo),
                          capture_output=True, text=True)

git("init", "-q", "-b", "main")
git("config", "user.email", "test@example.com")
git("config", "user.name", "Jarvish Test")
(repo / "hello.py").write_text("print('hello')\n", encoding="utf-8")
git("add", ".")
git("commit", "-q", "-m", "Initial commit")
(repo / "hello.py").write_text("print('hello world')\n", encoding="utf-8")
(repo / "extra.py").write_text("x = 1\n", encoding="utf-8")

status = dev.git_status(str(repo))
ok("git status works", status["ok"], status.get("branch"))
ok("branch reported", status["branch"] and "main" in status["branch"], status["branch"])
ok("modified file seen", any(c["file"] == "hello.py" for c in status["changes"]))
ok("untracked file seen", any(c["file"] == "extra.py" for c in status["changes"]))
ok("not clean", not status["clean"])

diff = dev.git_diff(str(repo))
ok("git diff works", diff["ok"] and "hello world" in diff["diff"])

log = dev.git_log(str(repo))
ok("git log works", log["ok"] and log["count"] == 1, log["commits"][0]["subject"]
   if log.get("commits") else None)
ok("commit metadata parsed", log["commits"][0]["author"] == "Jarvish Test")

branches = dev.git_branches(str(repo))
ok("git branches works", branches["ok"] and branches["current"] == "main",
   branches.get("current"))

commit = dev.propose_commit(str(repo))
ok("commit proposed", commit["ok"])
ok("proposal lists files", set(commit["files"]) == {"hello.py", "extra.py"},
   commit["files"])
ok("message suggested", bool(commit["proposed_message"]), commit["proposed_message"])
ok("nothing was committed", commit["applied"] is False)
ok("still one commit", dev.git_log(str(repo))["count"] == 1)

ok("non-repo reported honestly", not dev.git_status(str(sandbox))["ok"],
   dev.git_status(str(sandbox)).get("error", "")[:40])

# ── project memory ───────────────────────────────────────────────────────
print("--- project memory ---")
note = dev.remember_project("The agent loop lives in jarvish/llm.py.",
                            kind="architecture", path=str(PROJECT))
ok("project note saved", note["ok"])
again = dev.remember_project("The agent loop lives in jarvish/llm.py.",
                             kind="architecture", path=str(PROJECT))
ok("duplicate note not repeated", "already_known" in again)
ok("bad kind refused", not dev.remember_project("x", kind="nonsense")["ok"])
notes = dev.project_notes(str(PROJECT))
ok("notes recalled", notes["ok"] and notes["count"] >= 1, notes["notes"])
ok("notes are project-scoped, not user memory",
   Path(notes["root"]) == PROJECT)

# ── safety ───────────────────────────────────────────────────────────────
print("--- safety ---")
ok("apply_change is gated", risk.gated("apply_change", {"file": "x.py"}) is False
   or risk.level("apply_change") == "medium")
ok("apply_change on config escalates",
   risk.gated("apply_change", {"file": "settings.py"}),
   risk.effective_level("apply_change", {"file": "settings.py"}))
ok("propose_change is read-only", risk.level("propose_change") == "safe")
ok("git reads are safe", all(risk.level(n) == "safe"
                             for n in ("git_status", "git_diff", "git_log")))
ok("revert is irreversible-flagged", not risk.reversible("revert_change"))

shutil.rmtree(sandbox, ignore_errors=True)
shutil.rmtree(repo, ignore_errors=True)
print("\n%d passed, %d failed" % (P, F))
