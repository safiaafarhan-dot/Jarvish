"""Developer mode: understanding a codebase, diagnosing it, and changing it safely.

This is built on the knowledge engine rather than beside it. `kb.py` already
indexes files, symbols, imports and the call graph incrementally; everything
here queries that index instead of rescanning the repository per request.

Three ideas run through the module:

**Evidence, not invention.** `diagnose` reads a traceback, extracts real frames,
and pulls the actual lines off disk. When the evidence does not identify a
cause it says so. It never guesses a culprit to look helpful.

**Nothing is overwritten blindly.** A change is proposed first — files, the
exact diff, the reason, the risk, the expected impact and how to test it — and
applying it is a separate, gated call that matches an exact snippet and keeps a
backup. Formatting and unrelated code are untouched because only the matched
span is replaced.

**Commands are not equally dangerous.** `git status` and `rm -rf` both go
through the same tool, so the tier is computed from the command itself:
read-only inspection runs freely, installs and writes are medium, and anything
destructive is critical and stops at the confirmation gate.
"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import kb
from .util import (DATA_DIR, IS_WINDOWS, as_bool, as_int, boolean, err, integer,
                   ok, string, tool)

BACKUP_DIR = DATA_DIR / "edits"

# How long any developer command may run before it is abandoned.
COMMAND_TIMEOUT = 300

# Marker files that identify a project root, best evidence first.
ROOT_MARKERS = (
    ".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
    "pom.xml", "build.gradle", "requirements.txt", "Gemfile", "composer.json",
)

# marker -> (language, package manager, how to read dependencies)
STACK_MARKERS = {
    "package.json": ("JavaScript/TypeScript", "npm"),
    "pnpm-lock.yaml": ("JavaScript/TypeScript", "pnpm"),
    "yarn.lock": ("JavaScript/TypeScript", "yarn"),
    "pyproject.toml": ("Python", "pip/poetry"),
    "requirements.txt": ("Python", "pip"),
    "Pipfile": ("Python", "pipenv"),
    "Cargo.toml": ("Rust", "cargo"),
    "go.mod": ("Go", "go"),
    "pom.xml": ("Java", "maven"),
    "build.gradle": ("Java/Kotlin", "gradle"),
    "Gemfile": ("Ruby", "bundler"),
    "composer.json": ("PHP", "composer"),
}

# Dependency names that identify a framework.
FRAMEWORK_HINTS = {
    "next": "Next.js", "react": "React", "vue": "Vue", "svelte": "Svelte",
    "angular": "Angular", "express": "Express", "nest": "NestJS",
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django",
    "uvicorn": "ASGI (uvicorn)", "starlette": "Starlette",
    "pytest": "pytest", "jest": "Jest", "vitest": "Vitest",
    "torch": "PyTorch", "tensorflow": "TensorFlow",
}

ENV_FILES = (".env", ".env.local", ".env.example", ".env.development")


# --------------------------------------------------------------------------
# Project detection
# --------------------------------------------------------------------------

def _root_for(path):
    """Walk up until a project marker appears; fall back to the path itself."""
    start = Path(os.path.expandvars(os.path.expanduser(str(path or ".")))).resolve()
    if start.is_file():
        start = start.parent
    current = start
    for _ in range(12):
        for marker in ROOT_MARKERS:
            if (current / marker).exists():
                return current
        if current.parent == current:
            break
        current = current.parent
    return start


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _python_dependencies(root):
    names = []
    requirements = root / "requirements.txt"
    if requirements.exists():
        try:
            for line in requirements.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    names.append(re.split(r"[<>=!\[;]", line)[0].strip())
        except Exception:
            pass
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            body = pyproject.read_text(encoding="utf-8")
            for match in re.finditer(r'^\s*"([A-Za-z0-9_.\-]+)[^"]*"\s*,?\s*$',
                                     body, re.MULTILINE):
                names.append(match.group(1))
        except Exception:
            pass
    return sorted(set(n for n in names if n))


def detect(path=".", refresh=False):
    """Work out what this project is, from the files that are actually there."""
    root = _root_for(path)
    key = str(root)

    connection = kb._connect()
    kb._schema(connection)
    try:
        if not as_bool(refresh):
            row = connection.execute(
                "SELECT detected, updated FROM projects WHERE root = ?", (key,)).fetchone()
            if row:
                try:
                    cached = json.loads(row["detected"])
                    # Cheap staleness check: has any marker changed since?
                    newest = max((os.path.getmtime(root / m) for m in ROOT_MARKERS
                                  if (root / m).exists()), default=0)
                    if newest <= row["updated"]:
                        cached["cached"] = True
                        return ok(**cached)
                except Exception:
                    pass

        present = {m for m in STACK_MARKERS if (root / m).exists()}
        languages, managers = [], []
        for marker in present:
            language, manager = STACK_MARKERS[marker]
            if language not in languages:
                languages.append(language)
            if manager not in managers:
                managers.append(manager)

        dependencies, scripts, frameworks = [], {}, []
        package = root / "package.json"
        if package.exists():
            data = _read_json(package)
            scripts = data.get("scripts") or {}
            dependencies += list((data.get("dependencies") or {}).keys())
            dependencies += list((data.get("devDependencies") or {}).keys())
        dependencies += _python_dependencies(root)
        dependencies = sorted(set(dependencies))

        for dependency in dependencies:
            base = dependency.lower().lstrip("@").split("/")[0]
            hit = FRAMEWORK_HINTS.get(base)
            if hit and hit not in frameworks:
                frameworks.append(hit)

        # Entry points, tests and config, from the index where possible.
        indexed = connection.execute(
            "SELECT path, name, kind FROM files WHERE path LIKE ?",
            (key + "%",)).fetchall()
        names = [(Path(r["path"]), r["name"]) for r in indexed]
        if not names:
            names = [(p, p.name) for p in root.rglob("*")
                     if p.is_file() and p.suffix in kb.KINDS
                     and not any(part in kb.SKIP_DIRS for part in p.parts)][:2000]

        entry_points = sorted({
            name for _p, name in names
            if name.lower() in {"main.py", "app.py", "server.py", "new.py", "cli.py",
                                "__main__.py", "manage.py", "index.js", "main.js",
                                "app.js", "index.ts", "server.js", "wsgi.py", "asgi.py"}})
        tests = sorted({
            str(p.relative_to(root)) for p, name in names
            if name.startswith("test_") or name.endswith("_test.py")
            or re.search(r"\.(test|spec)\.[jt]sx?$", name)
            or "tests" in {part.lower() for part in p.parts}})[:40]
        configs = sorted({
            name for _p, name in names
            if name in STACK_MARKERS or name in ("setup.cfg", "tox.ini", "Dockerfile",
                                                 "docker-compose.yml", "Makefile",
                                                 "tsconfig.json", "vite.config.js",
                                                 "next.config.js", "pytest.ini")})
        env_files = [name for name in ENV_FILES if (root / name).exists()]

        source_dirs = sorted({
            str(p.parent.relative_to(root)) for p, _n in names
            if p.parent != root and p.suffix in (".py", ".js", ".ts", ".tsx", ".jsx")
        })[:12]

        commands = _commands(root, scripts, present, bool(tests))
        git = _git_present(root)

        detected = {
            "root": key,
            "languages": languages or ["unknown"],
            "package_managers": managers,
            "frameworks": frameworks,
            "dependencies": dependencies[:60],
            "dependency_count": len(dependencies),
            "entry_points": entry_points,
            "source_dirs": source_dirs,
            "tests": tests,
            "configs": configs,
            "env_files": env_files,
            "scripts": scripts,
            "commands": commands,
            "git": git,
            "indexed_files": len(indexed),
            "cached": False,
        }

        connection.execute(
            "INSERT OR REPLACE INTO projects(root, detected, updated) VALUES (?,?,?)",
            (key, json.dumps(detected), time.time()))
        connection.commit()
        return ok(**detected)
    finally:
        connection.close()


def _commands(root, scripts, markers, has_tests):
    """The commands this project actually supports, inferred from its files."""
    found = {}
    if "package.json" in markers:
        found["install"] = "npm install"
        for name in ("dev", "start", "build", "test", "lint", "typecheck"):
            if name in scripts:
                found[name] = "npm run " + name
    if "requirements.txt" in markers:
        found.setdefault("install", "python -m pip install -r requirements.txt")
    if "pyproject.toml" in markers:
        found.setdefault("install", "python -m pip install -e .")
    if has_tests and shutil.which("pytest"):
        found.setdefault("test", "python -m pytest -q")
    if (root / "Makefile").exists():
        found.setdefault("build", "make")
    if "Cargo.toml" in markers:
        found.update({"build": "cargo build", "test": "cargo test"})
    if "go.mod" in markers:
        found.update({"build": "go build ./...", "test": "go test ./..."})
    return found


def _git_present(root):
    if not (root / ".git").exists():
        return {"repository": False}
    result = _run_raw(["git", "rev-parse", "--abbrev-ref", "HEAD"], root, timeout=15)
    return {"repository": True,
            "branch": result["stdout"].strip() if result["ok"] else None}


# --------------------------------------------------------------------------
# Code intelligence
# --------------------------------------------------------------------------

def find_callers(name, limit=25):
    """Who calls this function. Names only — no type inference is claimed."""
    needle = str(name or "").strip()
    if not needle:
        return err("No function name given.")
    connection = kb._connect()
    kb._schema(connection)
    try:
        rows = connection.execute(
            "SELECT path, caller, line FROM calls WHERE callee = ? ORDER BY path, line"
            " LIMIT ?", (needle, as_int(limit, 25, 1, 200))).fetchall()
        defined = connection.execute(
            "SELECT path, kind, line, parent FROM symbols WHERE name = ? LIMIT 5",
            (needle,)).fetchall()
    finally:
        connection.close()

    if not rows and not defined:
        return err("'" + needle + "' does not appear in the index. Index the project "
                   "first, or check the spelling.")
    return ok(
        symbol=needle,
        defined_in=[{"source": Path(r["path"]).name, "path": r["path"],
                     "kind": r["kind"], "line": r["line"], "parent": r["parent"]}
                    for r in defined],
        callers=[{"source": Path(r["path"]).name, "path": r["path"],
                  "caller": r["caller"], "line": r["line"]} for r in rows],
        caller_count=len(rows),
        note=("Callers are matched by name, so a same-named method on another class "
              "would also appear here."),
    )


def dependents(module, limit=40):
    """Which files import this module."""
    needle = str(module or "").strip().replace(".py", "")
    if not needle:
        return err("No module name given.")
    connection = kb._connect()
    kb._schema(connection)
    try:
        rows = connection.execute(
            "SELECT DISTINCT path FROM imports WHERE module = ? OR module LIKE ?"
            " OR module LIKE ? LIMIT ?",
            (needle, "%." + needle, needle + ".%", as_int(limit, 40, 1, 200))).fetchall()
        provides = connection.execute(
            "SELECT COUNT(*) AS n FROM symbols WHERE path LIKE ?",
            ("%" + needle + ".py",)).fetchone()["n"]
    finally:
        connection.close()

    if not rows:
        return err("Nothing in the index imports '" + needle + "'.")
    return ok(module=needle,
              imported_by=[{"source": Path(r["path"]).name, "path": r["path"]}
                           for r in rows],
              count=len(rows), defines_symbols=provides)


def architecture(path="."):
    """A structural summary of the project, from the index."""
    project = detect(path)
    if not project["ok"]:
        return project
    root = project["root"]

    connection = kb._connect()
    kb._schema(connection)
    try:
        modules = connection.execute(
            "SELECT f.name, f.path, f.chunks,"
            "  (SELECT COUNT(*) FROM symbols s WHERE s.path = f.path) AS symbols,"
            "  (SELECT COUNT(*) FROM imports i WHERE i.path = f.path) AS imports"
            " FROM files f WHERE f.path LIKE ? AND f.kind IN ('python','code')"
            " ORDER BY symbols DESC LIMIT 20", (root + "%",)).fetchall()
        hubs = connection.execute(
            "SELECT module, COUNT(*) AS n FROM imports WHERE path LIKE ?"
            " GROUP BY module ORDER BY n DESC LIMIT 12", (root + "%",)).fetchall()
        busiest = connection.execute(
            "SELECT callee, COUNT(*) AS n FROM calls WHERE path LIKE ?"
            " GROUP BY callee ORDER BY n DESC LIMIT 12", (root + "%",)).fetchall()
    finally:
        connection.close()

    if not modules:
        return err("Nothing is indexed under " + root +
                   ". Run index_folder on it first.")

    return ok(
        root=root,
        languages=project["languages"],
        frameworks=project["frameworks"],
        entry_points=project["entry_points"],
        modules=[{"name": m["name"], "symbols": m["symbols"],
                  "imports": m["imports"], "chunks": m["chunks"]} for m in modules],
        most_imported=[{"module": h["module"], "used_by": h["n"]} for h in hubs],
        most_called=[{"function": b["callee"], "calls": b["n"]} for b in busiest],
        commands=project["commands"],
    )


# --------------------------------------------------------------------------
# Error diagnosis
# --------------------------------------------------------------------------

_PY_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
_PY_ERROR = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning)):\s*(.*)$",
                       re.MULTILINE)
_JS_FRAME = re.compile(r"at\s+(?:([\w.<>$]+)\s+\()?([^\s()]+?):(\d+):(\d+)\)?")
_JS_ERROR = re.compile(r"^\s*(\w*Error):\s*(.+)$", re.MULTILINE)
_TS_ERROR = re.compile(r"^(.+?)\((\d+),(\d+)\):\s*error\s+(TS\d+):\s*(.+)$", re.MULTILINE)
_PYTEST_FAIL = re.compile(r"^(FAILED|ERROR)\s+(\S+?)(?:::(\S+))?\s*(?:-\s*(.*))?$",
                          re.MULTILINE)

CLASSES = {
    "ModuleNotFoundError": ("dependency", "A module is not installed or not importable."),
    "ImportError": ("dependency", "An import failed — wrong name, or a circular import."),
    "SyntaxError": ("syntax", "The file does not parse."),
    "IndentationError": ("syntax", "Indentation is inconsistent."),
    "NameError": ("runtime", "A name is used before it is defined."),
    "AttributeError": ("runtime", "An attribute does not exist on that object."),
    "TypeError": ("runtime", "A value of the wrong type, or the wrong arguments."),
    "ValueError": ("runtime", "A value was the right type but not an acceptable one."),
    "KeyError": ("runtime", "A dictionary key is missing."),
    "IndexError": ("runtime", "An index is out of range."),
    "FileNotFoundError": ("environment", "A path does not exist."),
    "PermissionError": ("environment", "The process lacks permission."),
    "ConnectionError": ("network", "A network connection failed."),
    "TimeoutError": ("network", "An operation timed out."),
    "AssertionError": ("test", "A test assertion failed."),
}


def _read_around(path, line, span=4):
    """The real lines around a frame, straight off disk."""
    try:
        target = Path(path)
        if not target.exists():
            return None
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, line - span)
        end = min(len(lines), line + span)
        return {
            "path": str(target),
            "source": target.name,
            "line": line,
            "excerpt": "\n".join(
                ("%5d %s %s" % (n, ">>" if n == line else "  ", lines[n - 1]))
                for n in range(start, end + 1)),
        }
    except Exception:
        return None


def diagnose(error, path=None):
    """Read an error, locate it, and pull the real code around it.

    Deliberately conservative: when the text does not contain enough to place
    the fault, this says so instead of naming a cause it cannot support.
    """
    text = str(error or "").strip()
    if not text:
        return err("No error text given. Paste the traceback or the failing output.")

    kind, exception, message, frames = "unknown", None, None, []

    py_frames = _PY_FRAME.findall(text)
    if py_frames:
        kind = "python_traceback"
        for file_path, line, function in py_frames:
            frames.append({"path": file_path, "line": int(line), "function": function})
        found = _PY_ERROR.findall(text)
        if found:
            exception, message = found[-1][0], found[-1][1].strip()

    if kind == "unknown":
        ts = _TS_ERROR.findall(text)
        if ts:
            kind = "typescript_error"
            for file_path, line, _col, code, detail in ts:
                frames.append({"path": file_path.strip(), "line": int(line),
                               "function": code})
            exception, message = ts[0][3], ts[0][4].strip()

    if kind == "unknown":
        js = _JS_FRAME.findall(text)
        if js:
            kind = "javascript_error"
            for function, file_path, line, _col in js:
                frames.append({"path": file_path, "line": int(line),
                               "function": function or "(anonymous)"})
            found = _JS_ERROR.findall(text)
            if found:
                exception, message = found[0][0], found[0][1].strip()

    if kind == "unknown":
        failures = _PYTEST_FAIL.findall(text)
        if failures:
            kind = "test_failure"
            for _label, file_path, test, detail in failures:
                frames.append({"path": file_path, "line": 0,
                               "function": test or "(module)"})
            exception = "AssertionError"
            message = failures[0][3] or "A test failed."

    category, meaning = CLASSES.get(exception or "", (None, None))

    # Only frames inside the user's own code are worth showing; library frames
    # are noise unless there is nothing else.
    project_root = _root_for(path or (frames[0]["path"] if frames else "."))
    own, foreign = [], []
    for frame in frames:
        try:
            resolved = Path(frame["path"]).resolve()
        except Exception:
            foreign.append(frame)
            continue
        target = own if str(resolved).startswith(str(project_root)) else foreign
        target.append(frame)

    interesting = own[-3:] if own else foreign[-2:]
    evidence = [context for context in
                (_read_around(f["path"], f["line"]) for f in interesting if f["line"])
                if context]

    if kind == "unknown":
        return ok(
            classified=False,
            kind="unknown",
            summary="This does not look like a traceback or compiler output I can parse.",
            advice=("Paste the full error, including the 'Traceback (most recent call "
                    "last)' block or the compiler's file:line output."),
            evidence=[],
        )

    fault = own[-1] if own else (frames[-1] if frames else None)
    confident = bool(fault and evidence)

    return ok(
        classified=True,
        kind=kind,
        exception=exception,
        message=message,
        category=category,
        meaning=meaning,
        frames=frames[-8:],
        in_your_code=own[-5:],
        likely_site=fault,
        evidence=evidence,
        confident=confident,
        summary=(
            (exception or kind) + (": " + message if message else "") +
            (" at " + Path(fault["path"]).name + ":" + str(fault["line"])
             if fault and fault.get("line") else "")),
        note=(None if confident else
              "The frames point outside this project or carry no line numbers, so the "
              "site could not be pinned down. Treat any cause as unconfirmed."),
    )


# --------------------------------------------------------------------------
# Safe modification
# --------------------------------------------------------------------------

def propose_change(file, find, replace, reason, test_plan=None):
    """Describe a change without making it. Nothing is written here."""
    target = Path(os.path.expandvars(os.path.expanduser(str(file or "")))).resolve()
    if not target.exists():
        return err("No such file: " + str(target))
    if not str(find or ""):
        return err("Nothing to find. Give the exact snippet to replace.")

    try:
        body = target.read_text(encoding="utf-8")
    except Exception as exc:
        return err("Could not read the file: " + str(exc))

    occurrences = body.count(find)
    if occurrences == 0:
        return err("That snippet does not appear in " + target.name +
                   ". Read the file again — it may have changed.")
    if occurrences > 1:
        return err("That snippet appears " + str(occurrences) + " times in " +
                   target.name + ". Include more surrounding context so it is unique.")

    position = body.index(find)
    line = body[:position].count("\n") + 1
    added = replace.count("\n") + 1
    removed = find.count("\n") + 1

    connection = kb._connect()
    kb._schema(connection)
    try:
        importers = connection.execute(
            "SELECT DISTINCT path FROM imports WHERE module = ? OR module LIKE ?",
            (target.stem, "%." + target.stem)).fetchall()
    finally:
        connection.close()

    risk_note = "low"
    if re.search(r"\b(def |class |import |return |raise )", find):
        risk_note = "medium — this touches a definition or control flow"
    if len(importers) > 3:
        risk_note = "medium — several modules import this file"

    return ok(
        proposal={
            "files": [str(target)],
            "change": {"at_line": line, "removing_lines": removed,
                       "adding_lines": added,
                       "before": find[:600], "after": replace[:600]},
            "reason": str(reason or "not stated"),
            "risk": risk_note,
            "expected_impact": (
                str(len(importers)) + " file(s) import this module" if importers
                else "No indexed file imports this module"),
            "test_plan": test_plan or _suggest_tests(target),
        },
        applied=False,
        next_step=("Call apply_change with the same file, find and replace to carry "
                   "this out. It is gated, so the user will be asked first."),
    )


def _suggest_tests(target):
    project = detect(target.parent)
    commands = project.get("commands", {}) if project["ok"] else {}
    if "test" in commands:
        return "Run: " + commands["test"]
    return "No test command detected; verify by running the affected entry point."


def apply_change(file, find, replace, reason=None, confirm=False):
    """Apply a previously described change, keeping a backup.

    Only the matched span is rewritten, so formatting and unrelated code in the
    file are untouched.
    """
    check = propose_change(file, find, replace, reason)
    if not check["ok"]:
        return check

    target = Path(check["proposal"]["files"][0])
    body = target.read_text(encoding="utf-8")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    # Millisecond resolution, then a counter. Two edits to one file inside the
    # same second are ordinary — propose, apply, correct, apply again — and a
    # colliding name would silently destroy the earlier backup.
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + ("%03d" % (int(time.time() * 1000) % 1000))
    backup = BACKUP_DIR / (target.name + "." + stamp + ".bak")
    suffix = 0
    while backup.exists():
        suffix += 1
        backup = BACKUP_DIR / (target.name + "." + stamp + "-" + str(suffix) + ".bak")
    try:
        backup.write_text(body, encoding="utf-8")
        target.write_text(body.replace(find, replace, 1), encoding="utf-8")
    except Exception as exc:
        return err("Could not write the file: " + str(exc))

    # Verify by reading back, and re-parse Python so a broken edit is caught now.
    after = target.read_text(encoding="utf-8")
    applied = replace in after and (find not in after or find in replace)
    parse_error = None
    if target.suffix in (".py", ".pyw"):
        import ast as _ast
        try:
            _ast.parse(after)
        except SyntaxError as exc:
            parse_error = "line " + str(exc.lineno) + ": " + str(exc.msg)

    if parse_error:
        target.write_text(body, encoding="utf-8")
        # The file is back exactly as it was, so this backup is noise — and
        # leaving it would make `revert_change` restore a state that was never
        # actually in effect.
        backup.unlink(missing_ok=True)
        return err("The edit produced invalid Python (" + parse_error +
                   "), so it was rolled back. The file is unchanged.")

    return ok(
        changed=str(target),
        backup=str(backup),
        verified=applied,
        parse_checked=target.suffix in (".py", ".pyw"),
        reason=str(reason or "not stated"),
        next_step="Run the tests now; use revert_change if it made things worse.",
    )


def revert_change(file=None):
    """Restore the most recent backup, for a file or overall."""
    if not BACKUP_DIR.exists():
        return err("There are no backups to restore.")
    backups = sorted(BACKUP_DIR.glob("*.bak"), key=lambda p: p.stat().st_mtime)
    if file:
        name = Path(str(file)).name
        backups = [b for b in backups if b.name.startswith(name + ".")]
    if not backups:
        return err("No backup found" + (" for " + str(file) if file else "") + ".")

    newest = backups[-1]
    # Backups are named "<original name>.<stamp>.bak", so the original is
    # everything before the last two segments — "sample.py", not "sample".
    original_name = newest.name.rsplit(".", 2)[0]
    matches = list(Path.cwd().rglob(original_name))
    if file:
        matches = [Path(os.path.expandvars(os.path.expanduser(str(file)))).resolve()]
    if not matches:
        return err("Could not work out where " + original_name + " belongs.")

    target = matches[0]
    try:
        target.write_text(newest.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception as exc:
        return err("Could not restore: " + str(exc))
    return ok(restored=str(target), from_backup=str(newest))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

# Anything matching these is destructive enough to demand confirmation.
DESTRUCTIVE = re.compile(
    r"(\brm\s+-[rf]|\brmdir\b|\bdel\s+/|\bformat\b|\bmkfs|>\s*/dev/|"
    r"git\s+push\s+.*--force|git\s+reset\s+--hard|git\s+clean\s+-[a-z]*f|"
    r"\bdrop\s+(table|database)\b|\bshutdown\b|\bdiskpart\b|"
    r"Remove-Item.*-Recurse|\btruncate\b)", re.IGNORECASE)

# Writes something outward, or changes the environment.
WRITING = re.compile(
    r"(\bgit\s+(push|commit|merge|rebase|checkout|switch|tag)\b|"
    r"\bnpm\s+(publish|install|i|ci|uninstall)\b|\bpip\s+(install|uninstall)\b|"
    r"\byarn\s+(add|remove)\b|\bpnpm\s+(add|remove)\b|\bcargo\s+(install|publish)\b|"
    r"\bdocker\s+(run|build|push)\b|\bdeploy\b|\bvercel\b)", re.IGNORECASE)

# Pure inspection: reads, lists, reports.
READ_ONLY = re.compile(
    r"^\s*(git\s+(status|diff|log|branch|show|remote|rev-parse|describe|blame)|"
    r"ls|dir|pwd|cat|type|head|tail|find|where|which|"
    r"node\s+--version|npm\s+(ls|list|outdated|view)|"
    r"python\s+--version|pip\s+(list|show|freeze)|"
    r"pytest(\s+--collect-only|\s+-q|\s+--version)?|python\s+-m\s+pytest|"
    r"flake8|mypy|eslint|tsc\s+--noEmit|npm\s+run\s+(lint|typecheck|test))\b",
    re.IGNORECASE)


def classify_command(command):
    """What tier a shell command deserves, judged from the command itself."""
    text = str(command or "").strip()
    if not text:
        return "safe", "empty"
    if DESTRUCTIVE.search(text):
        return "critical", "This command destroys data or rewrites history."
    if WRITING.search(text):
        return "high", "This command writes outward or changes the environment."
    if READ_ONLY.match(text):
        return "safe", "Read-only inspection."
    return "medium", "This runs a real command in the project."


def _run_raw(argv, cwd, timeout=COMMAND_TIMEOUT):
    try:
        completed = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, timeout=timeout,
            shell=isinstance(argv, str))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timed out after " + str(timeout) + "s.",
                "stdout": "", "stderr": ""}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "stdout": "", "stderr": ""}
    return {
        "ok": completed.returncode == 0,
        "code": completed.returncode,
        "stdout": (completed.stdout or b"").decode("utf-8", "replace"),
        "stderr": (completed.stderr or b"").decode("utf-8", "replace"),
    }


def run_command(command, path=".", timeout=None, confirm=False):
    """Run a development command in the project, and report what happened."""
    text = str(command or "").strip()
    if not text:
        return err("No command given.")
    root = _root_for(path)
    tier, why = classify_command(text)

    started = time.perf_counter()
    result = _run_raw(text, root, timeout=as_int(timeout, COMMAND_TIMEOUT, 5, 900))
    elapsed = round((time.perf_counter() - started) * 1000)

    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    payload = ok(
        command=text,
        cwd=str(root),
        risk=tier,
        risk_reason=why,
        exit_code=result.get("code"),
        succeeded=result["ok"],
        ms=elapsed,
        stdout=stdout[-6000:],
        stderr=stderr[-4000:],
        truncated=len(stdout) > 6000 or len(stderr) > 4000,
    )
    if not result["ok"] and result.get("error"):
        payload["error_detail"] = result["error"]
    # A failing command usually carries its own diagnosis; extract it.
    if not result["ok"]:
        combined = (stderr + "\n" + stdout)[-8000:]
        analysis = diagnose(combined, path=root)
        if analysis.get("classified"):
            payload["diagnosis"] = {
                "kind": analysis["kind"], "exception": analysis.get("exception"),
                "message": analysis.get("message"),
                "likely_site": analysis.get("likely_site"),
                "confident": analysis.get("confident"),
            }
    return payload


def run_tests(path=".", target=None):
    """Run the project's tests and summarise the result."""
    project = detect(path)
    if not project["ok"]:
        return project
    command = project["commands"].get("test")
    if not command:
        return err("No test command detected for this project. Configure one, or use "
                   "run_dev_command with the exact command.")
    if target:
        command = command + " " + str(target)

    outcome = run_command(command, path=path)
    if not outcome["ok"]:
        return outcome

    body = outcome["stdout"] + "\n" + outcome["stderr"]
    passed = re.search(r"(\d+)\s+passed", body)
    failed = re.search(r"(\d+)\s+failed", body)
    errors = re.search(r"(\d+)\s+error", body)
    outcome["summary"] = {
        "passed": int(passed.group(1)) if passed else None,
        "failed": int(failed.group(1)) if failed else None,
        "errors": int(errors.group(1)) if errors else None,
        "green": outcome["succeeded"],
    }
    if not outcome["succeeded"]:
        failures = _PYTEST_FAIL.findall(body)
        outcome["failures"] = [{"test": f[1] + ("::" + f[2] if f[2] else ""),
                                "detail": f[3]} for f in failures[:10]]
    return outcome


def run_build(path="."):
    project = detect(path)
    if not project["ok"]:
        return project
    command = project["commands"].get("build")
    if not command:
        return err("No build command detected for this project.")
    return run_command(command, path=path)


# --------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------

def _git(args, path, timeout=30):
    root = _root_for(path)
    if not (root / ".git").exists():
        return None, err(str(root) + " is not a git repository.")
    return root, _run_raw(["git"] + args, root, timeout=timeout)


def git_status(path="."):
    root, result = _git(["status", "--porcelain=v1", "--branch"], path)
    if root is None:
        return result
    if not result["ok"]:
        return err(result.get("stderr") or result.get("error") or "git status failed.")

    lines = result["stdout"].splitlines()
    branch = lines[0][3:] if lines and lines[0].startswith("##") else None
    changes = []
    for line in lines[1:]:
        if len(line) > 3:
            changes.append({"status": line[:2].strip(), "file": line[3:].strip()})
    return ok(root=str(root), branch=branch, changes=changes,
              changed_files=len(changes), clean=not changes)


def git_diff(path=".", file=None, staged=False):
    args = ["diff"] + (["--staged"] if as_bool(staged) else [])
    if file:
        args += ["--", str(file)]
    root, result = _git(args, path, timeout=45)
    if root is None:
        return result
    if not result["ok"]:
        return err(result.get("stderr") or "git diff failed.")
    diff = result["stdout"]
    return ok(root=str(root), diff=diff[:12000], truncated=len(diff) > 12000,
              empty=not diff.strip())


def git_log(path=".", count=10):
    root, result = _git(["log", "--oneline", "-n", str(as_int(count, 10, 1, 100)),
                         "--date=short", "--pretty=%h|%ad|%an|%s"], path)
    if root is None:
        return result
    if not result["ok"]:
        return err(result.get("stderr") or "git log failed.")
    commits = []
    for line in result["stdout"].splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({"hash": parts[0], "date": parts[1],
                            "author": parts[2], "subject": parts[3]})
    return ok(root=str(root), commits=commits, count=len(commits))


def git_branches(path="."):
    root, result = _git(["branch", "-a", "--format=%(refname:short)|%(HEAD)"], path)
    if root is None:
        return result
    if not result["ok"]:
        return err(result.get("stderr") or "git branch failed.")
    branches, current = [], None
    for line in result["stdout"].splitlines():
        name, _, head = line.partition("|")
        branches.append(name)
        if head.strip() == "*":
            current = name
    return ok(root=str(root), branches=branches, current=current)


def propose_commit(path=".", message=None):
    """Show what a commit would contain. Nothing is committed here."""
    status = git_status(path)
    if not status["ok"]:
        return status
    if status["clean"]:
        return ok(nothing_to_commit=True, branch=status["branch"],
                  note="The working tree is clean.")

    diff = git_diff(path)
    files = [c["file"] for c in status["changes"]]
    suggested = message or _suggest_message(status["changes"])
    return ok(
        branch=status["branch"],
        files=files,
        file_count=len(files),
        proposed_message=suggested,
        diff_preview=(diff.get("diff", "")[:3000] if diff["ok"] else None),
        applied=False,
        next_step=("Nothing has been committed. Ask the user to confirm the message "
                   "and files, then run the commit with run_dev_command."),
    )


def _suggest_message(changes):
    added = sum(1 for c in changes if c["status"] in ("A", "??"))
    modified = sum(1 for c in changes if c["status"] == "M")
    deleted = sum(1 for c in changes if c["status"] == "D")
    parts = []
    if added:
        parts.append("add " + str(added) + " file" + ("s" if added != 1 else ""))
    if modified:
        parts.append("update " + str(modified) + " file" + ("s" if modified != 1 else ""))
    if deleted:
        parts.append("remove " + str(deleted) + " file" + ("s" if deleted != 1 else ""))
    return ", ".join(parts).capitalize() or "Update project files"


# --------------------------------------------------------------------------
# Project memory
# --------------------------------------------------------------------------

NOTE_KINDS = ("architecture", "command", "issue", "fix", "limitation")


def remember_project(note, kind="architecture", path="."):
    """Keep a durable fact about this project. Not conversation memory."""
    text = str(note or "").strip()
    if not text:
        return err("No note given.")
    category = str(kind or "architecture").strip().lower()
    if category not in NOTE_KINDS:
        return err("Unknown kind. Options: " + ", ".join(NOTE_KINDS) + ".")

    root = str(_root_for(path))
    connection = kb._connect()
    kb._schema(connection)
    try:
        existing = connection.execute(
            "SELECT 1 FROM project_notes WHERE root=? AND kind=? AND note=?",
            (root, category, text)).fetchone()
        if existing:
            return ok(already_known=text, root=root)
        connection.execute(
            "INSERT INTO project_notes(root, kind, note, at) VALUES (?,?,?,?)",
            (root, category, text, time.time()))
        connection.commit()
    finally:
        connection.close()
    return ok(remembered=text, kind=category, root=root)


def project_notes(path=".", kind=None):
    root = str(_root_for(path))
    connection = kb._connect()
    kb._schema(connection)
    try:
        if kind:
            rows = connection.execute(
                "SELECT kind, note, at FROM project_notes WHERE root=? AND kind=?"
                " ORDER BY at DESC LIMIT 60", (root, str(kind).lower())).fetchall()
        else:
            rows = connection.execute(
                "SELECT kind, note, at FROM project_notes WHERE root=?"
                " ORDER BY at DESC LIMIT 60", (root,)).fetchall()
    finally:
        connection.close()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["kind"], []).append(row["note"])
    return ok(root=root, notes=grouped, count=len(rows))


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

SCHEMAS = [
    tool("project_info",
         "Work out what a project is: language, framework, package manager, "
         "dependencies, entry points, tests, config and how to build and test it. "
         "Use before answering anything about the user's code.",
         {"path": string("A folder inside the project. Defaults to the working directory."),
          "refresh": boolean("Re-detect instead of using the cached result.")}),
    tool("project_architecture",
         "Summarise how a project is put together: its main modules, which are most "
         "imported, and which functions are called most.",
         {"path": string("A folder inside the project.")}),
    tool("find_callers",
         "Find what calls a function, and where it is defined.",
         {"name": string("The function or method name.")},
         ["name"]),
    tool("find_dependents",
         "Find which files import a module.",
         {"module": string("Module name, without the extension.")},
         ["module"]),
    tool("diagnose_error",
         "Read an error, traceback, test failure or compiler output; work out what "
         "kind it is, where it happened, and show the real code at that line. Use "
         "whenever the user pastes an error or a command fails.",
         {"error": string("The full error text or traceback."),
          "path": string("The project the error came from.")},
         ["error"]),
    tool("propose_change",
         "Describe a code change without making it: the file, the exact before and "
         "after, the reason, the risk and how to test it. Always do this before "
         "editing anything.",
         {"file": string("The file to change."),
          "find": string("The exact snippet to replace. Must appear once."),
          "replace": string("What to put in its place."),
          "reason": string("Why this change is being made."),
          "test_plan": string("How the change should be verified.")},
         ["file", "find", "replace", "reason"]),
    tool("apply_change",
         "Carry out a change previously described with propose_change. Replaces only "
         "the matched snippet, keeps a backup, and rolls back automatically if the "
         "result does not parse.",
         {"file": string("The file to change."),
          "find": string("The exact snippet to replace."),
          "replace": string("What to put in its place."),
          "reason": string("Why this change is being made.")},
         ["file", "find", "replace"]),
    tool("revert_change",
         "Restore the most recent backup taken by apply_change.",
         {"file": string("Which file to restore. Omit for the most recent edit.")}),
    tool("run_dev_command",
         "Run a development command in the project — tests, build, lint, install, git. "
         "The risk is judged from the command itself, so destructive ones stop for "
         "confirmation.",
         {"command": string("The command to run."),
          "path": string("A folder inside the project."),
          "timeout": integer("Seconds to allow. Default 300.")},
         ["command"]),
    tool("run_tests",
         "Run the project's test suite and summarise passes, failures and errors.",
         {"path": string("A folder inside the project."),
          "target": string("A specific test file or test to run.")}),
    tool("run_build",
         "Run the project's build command.",
         {"path": string("A folder inside the project.")}),
    tool("git_status", "Show the working tree status: branch and changed files.",
         {"path": string("A folder inside the repository.")}),
    tool("git_diff", "Show the current diff.",
         {"path": string("A folder inside the repository."),
          "file": string("Limit the diff to one file."),
          "staged": boolean("Show staged changes instead of unstaged.")}),
    tool("git_log", "Show recent commits.",
         {"path": string("A folder inside the repository."),
          "count": integer("How many commits. Default 10.")}),
    tool("git_branches", "List branches and show which one is checked out.",
         {"path": string("A folder inside the repository.")}),
    tool("propose_commit",
         "Show what a commit would contain and suggest a message. Commits nothing.",
         {"path": string("A folder inside the repository."),
          "message": string("A message to use instead of the suggested one.")}),
    tool("remember_project",
         "Save a durable fact about this project — its architecture, a build command, "
         "a recurring issue, a past fix or a known limitation. Separate from `remember`, "
         "which holds facts about the user.",
         {"note": string("The fact worth keeping."),
          "kind": string("What sort of fact.", list(NOTE_KINDS)),
          "path": string("A folder inside the project.")},
         ["note"]),
    tool("project_notes",
         "Recall what is known about this project from earlier sessions.",
         {"path": string("A folder inside the project."),
          "kind": string("Only this sort of note.", list(NOTE_KINDS))}),
]

REGISTRY = {
    "project_info": detect,
    "project_architecture": architecture,
    "find_callers": find_callers,
    "find_dependents": dependents,
    "diagnose_error": diagnose,
    "propose_change": propose_change,
    "apply_change": apply_change,
    "revert_change": revert_change,
    "run_dev_command": run_command,
    "run_tests": run_tests,
    "run_build": run_build,
    "git_status": git_status,
    "git_diff": git_diff,
    "git_log": git_log,
    "git_branches": git_branches,
    "propose_commit": propose_commit,
    "remember_project": remember_project,
    "project_notes": project_notes,
}
