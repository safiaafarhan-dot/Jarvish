"""Tools Jarvish can call to act on the local machine.

Every tool is a plain synchronous function returning a JSON-serialisable dict.
The server runs them on a worker thread, so blocking calls are fine here.
"""

import ctypes
import datetime as _dt
import html
import os
import platform
import re
import shutil
import subprocess
import webbrowser
from pathlib import Path

import httpx
import psutil

from .config import ALLOW_SHELL
from .util import IS_WINDOWS, as_int, err as _err, expand as _expand, ok, tool as _tool

# Friendly name -> what to hand to the shell. This table is also the allowlist:
# a name that is not in it cannot be launched. Add an entry to permit a new
# application.
APP_ALIASES = {
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "paint": "mspaint",
    "file explorer": "explorer",
    "explorer": "explorer",
    "files": "explorer",
    "command prompt": "cmd",
    "cmd": "cmd",
    "powershell": "powershell",
    "terminal": "wt",
    "windows terminal": "wt",
    "task manager": "taskmgr",
    "settings": "ms-settings:",
    "control panel": "control",
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "firefox": "firefox",
    "vs code": "code",
    "vscode": "code",
    "visual studio code": "code",
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "spotify": "spotify",
    "camera": "microsoft.windows.camera:",
    "snipping tool": "snippingtool",
}

# Directories that are never worth walking when searching for a file.
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "AppData", "Windows",
    "$Recycle.Bin", "System Volume Information", ".venv", "venv", "site-packages",
}


# --------------------------------------------------------------------------
# Time and system state
# --------------------------------------------------------------------------

def get_time():
    """Current local date and time."""
    now = _dt.datetime.now()
    return {
        "ok": True,
        "time": now.strftime("%I:%M %p").lstrip("0"),
        "date": now.strftime("%A, %d %B %Y"),
        "iso": now.isoformat(timespec="seconds"),
    }


def system_info():
    """CPU, memory, disk and battery status for this machine."""
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(os.path.abspath(os.sep))
    info = {
        "ok": True,
        "os": platform.system() + " " + platform.release(),
        "machine": platform.node(),
        "cpu_percent": psutil.cpu_percent(interval=0.4),
        "cpu_cores": psutil.cpu_count(logical=True),
        "ram_used_gb": round(mem.used / 1e9, 1),
        "ram_total_gb": round(mem.total / 1e9, 1),
        "ram_percent": mem.percent,
        "disk_free_gb": round(disk.free / 1e9, 1),
        "disk_total_gb": round(disk.total / 1e9, 1),
        "uptime_hours": round((_dt.datetime.now().timestamp() - psutil.boot_time()) / 3600, 1),
    }
    try:
        battery = psutil.sensors_battery()
        if battery is not None:
            info["battery_percent"] = round(battery.percent)
            info["battery_plugged_in"] = battery.power_plugged
    except Exception:
        pass
    return info


def list_processes(limit=8):
    """The processes currently using the most memory."""
    procs = []
    for proc in psutil.process_iter(["name", "cpu_percent", "memory_info"]):
        try:
            info = proc.info
            procs.append({
                "name": info["name"],
                "cpu_percent": info["cpu_percent"] or 0.0,
                "ram_mb": round((info["memory_info"].rss if info["memory_info"] else 0) / 1e6, 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda p: p["ram_mb"], reverse=True)
    return {"ok": True, "processes": procs[: as_int(limit, 8, 1, 25)]}


# --------------------------------------------------------------------------
# Launching things
# --------------------------------------------------------------------------

# Every launch target this tool will accept. Derived from APP_ALIASES rather
# than written out again, so adding an alias is the only thing needed to allow
# a new application and the two can never disagree.
ALLOWED_APPS = frozenset(APP_ALIASES.values())


def open_app(name):
    """Launch an allowlisted desktop application by name."""
    key = str(name).strip().lower()
    if not key:
        return _err("No application name given.")

    # Resolution used to fall back to the caller's own string, which meant the
    # name was never really checked: `open_app("cmd.exe /c del C:\\Windows")`
    # resolved to itself and went straight into `cmd /c start`, where the
    # arguments are a command line, not a filename. The model could reach any
    # executable with any arguments through a tool graded `low` risk that never
    # asks for confirmation. Membership of the allowlist is now required.
    target = APP_ALIASES.get(key)
    if target is None:
        if key in ALLOWED_APPS:
            target = key          # the underlying name, e.g. "msedge"
        else:
            return _err(
                "'" + str(name) + "' is not an application Jarvish may launch. "
                "Allowed: " + ", ".join(sorted(APP_ALIASES)) + "."
            )

    try:
        if IS_WINDOWS:
            # `start` handles .exe on PATH, UWP protocol handles and app aliases
            # alike. `target` is safe to pass here only because it came out of
            # the table above - never straight from the caller.
            subprocess.Popen(["cmd", "/c", "start", "", target], shell=False)
        elif shutil.which("open"):
            subprocess.Popen(["open", "-a", target])
        else:
            subprocess.Popen([target])
        return {"ok": True, "opened": name, "command": target}
    except Exception as exc:
        return _err("Could not launch " + str(name) + ": " + str(exc))


# The only schemes a web address may use. Everything else is refused rather
# than handed to the browser: `file:` reads local files - including the
# credential paths the filesystem guard blocks everywhere else - and
# `javascript:` and `data:` execute in whatever page happens to be focused.
ALLOWED_URL_SCHEMES = ("http", "https")

_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")


def open_url(url):
    """Open an http or https address in the default browser."""
    url = str(url).strip()
    if not url:
        return _err("No URL given.")

    match = _SCHEME_RE.match(url)
    if match:
        scheme = match.group(1).lower()
        if scheme not in ALLOWED_URL_SCHEMES:
            return _err(
                "Only http and https addresses can be opened, not '" +
                scheme + ":'."
            )
        # A scheme on its own is not an address. "http://" passes the check
        # above and then opens a blank page while reporting success.
        if not re.match(r"^https?://[A-Za-z0-9]", url, re.I):
            return _err("'" + url + "' has no host to open.")
    else:
        # A bare address like "youtube.com". It has to still look like a host,
        # or "not-a-url" silently became "https://not-a-url" and reported
        # success for a page that could never load.
        if not re.match(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+([/?#].*)?$", url):
            return _err(
                "'" + url + "' is not a web address. Give a full http or https "
                "URL, or a domain such as example.com."
            )
        url = "https://" + url

    try:
        # `webbrowser.open` reports whether a browser was actually found. The
        # result used to be discarded, so a failed launch still came back as a
        # success and the assistant said it had opened something it had not.
        launched = webbrowser.open(url)
    except Exception as exc:
        return _err("Could not open " + url + ": " + str(exc))
    if not launched:
        return _err("No browser could be launched for " + url + ".")
    return {"ok": True, "opened": url}


# --------------------------------------------------------------------------
# Web search
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_RESULT_RE = re.compile(
    r'<a[^>]*class="result__a"[^>]*>(?P<title>.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.S,
)


def _clean(fragment):
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def web_search(query, limit=5):
    """Search the web and return the top result titles and snippets."""
    query = str(query).strip()
    if not query:
        return _err("No search query given.")
    limit = as_int(limit, 5, 1, 8)
    try:
        response = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=20.0,
            follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:
        return _err("Search request failed: " + str(exc))

    results = []
    for match in _RESULT_RE.finditer(response.text):
        title = _clean(match.group("title"))
        snippet = _clean(match.group("snippet"))
        if title:
            results.append({"title": title, "snippet": snippet[:300]})
        if len(results) >= limit:
            break

    if not results:
        return _err("No results found.")
    return {"ok": True, "query": query, "results": results}


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def find_files(pattern, root=None, limit=20):
    """Search the filesystem for files whose name matches a glob pattern."""
    pattern = str(pattern).strip()
    if not pattern:
        return _err("No search pattern given.")
    if "*" not in pattern and "?" not in pattern:
        pattern = "*" + pattern + "*"

    base = _expand(root) if root else Path.home()
    if not base.is_dir():
        return _err(str(base) + " is not a folder.")

    # The walk below is already recursive, so a leading `**/` adds nothing —
    # but leaving it in the pattern is fatal rather than merely redundant. The
    # match is against a bare filename, and `**/*.md` compiles to a regex that
    # requires a slash, so it can never match anything. A model asking for the
    # conventional recursive glob got an empty list back and reported, with no
    # error anywhere, that the project contained no markdown files.
    pattern = pattern.replace("\\", "/")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    while pattern.startswith("**/"):
        pattern = pattern[3:]
    # A pattern that still names a folder (`docs/*.md`) is matched against the
    # path relative to the search root instead of the filename alone.
    against_path = "/" in pattern

    limit = as_int(limit, 20, 1, 50)
    matcher = re.compile(
        "^" + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".") + "$",
        re.I,
    )
    matches = []
    for current_root, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in filenames:
            full = Path(current_root) / filename
            if against_path:
                try:
                    candidate = full.relative_to(base).as_posix()
                except ValueError:
                    candidate = filename
            else:
                candidate = filename
            if matcher.match(candidate):
                try:
                    size = full.stat().st_size
                except OSError:
                    size = 0
                matches.append({"path": str(full), "size_kb": round(size / 1024, 1)})
                if len(matches) >= limit:
                    return {"ok": True, "searched": str(base), "matches": matches, "truncated": True}
    return {"ok": True, "searched": str(base), "matches": matches, "truncated": False}


def list_directory(path=None):
    """List the contents of a folder."""
    target = _expand(path) if path else Path.home()
    if not target.is_dir():
        return _err(str(target) + " is not a folder.")
    folders, files = [], []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: e.name.lower()):
            if entry.is_dir():
                folders.append(entry.name)
            else:
                files.append(entry.name)
    except PermissionError:
        return _err("Permission denied reading " + str(target) + ".")
    return {"ok": True, "path": str(target), "folders": folders[:60], "files": files[:60]}


def read_text_file(path, max_chars=4000):
    """Read the beginning of a text file."""
    target = _expand(path)
    if not target.is_file():
        return _err(str(target) + " is not a file.")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return _err("Could not read " + str(target) + ": " + str(exc))
    max_chars = as_int(max_chars, 4000, 200, 20000)
    return {
        "ok": True,
        "path": str(target),
        "content": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


# --------------------------------------------------------------------------
# Machine control
# --------------------------------------------------------------------------

_VK = {
    "volume_mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
    "play_pause": 0xB3,
    "next_track": 0xB0,
    "previous_track": 0xB1,
}

KEYEVENTF_KEYUP = 0x0002


def _tap(vk, times=1):
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def media_control(action, steps=5):
    """Adjust volume or control media playback with the system media keys."""
    action = str(action).strip().lower().replace(" ", "_").replace("-", "_")
    if not IS_WINDOWS:
        return _err("Media control is only wired up for Windows.")
    if action not in _VK:
        return _err("Unknown action '" + action + "'. Options: " + ", ".join(_VK) + ".")
    repeats = as_int(steps, 5, 1, 25) if action in ("volume_up", "volume_down") else 1
    try:
        _tap(_VK[action], repeats)
    except Exception as exc:
        return _err("Media key failed: " + str(exc))
    return {"ok": True, "action": action, "steps": repeats}


def lock_screen():
    """Lock the workstation."""
    if not IS_WINDOWS:
        return _err("Locking is only wired up for Windows.")
    try:
        ctypes.windll.user32.LockWorkStation()
    except Exception as exc:
        return _err("Could not lock: " + str(exc))
    return {"ok": True, "locked": True}


def run_powershell(command):
    """Run an arbitrary PowerShell command. Disabled unless JARVISH_ALLOW_SHELL=1."""
    if not ALLOW_SHELL:
        return _err("Shell access is disabled. Restart the server with JARVISH_ALLOW_SHELL=1 to enable it.")
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", str(command)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return _err("Command timed out after 60 seconds.")
    except Exception as exc:
        return _err("Command failed: " + str(exc))
    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": completed.stdout[:4000],
        "stderr": completed.stderr[:2000],
    }


# --------------------------------------------------------------------------
# Schemas handed to the model
# --------------------------------------------------------------------------

SCHEMAS = [
    _tool("get_time", "Get the current local date and time.", {}),
    _tool("system_info", "Get CPU, memory, disk, uptime and battery status of this PC.", {}),
    _tool("list_processes", "List the running processes using the most memory.", {
        "limit": {"type": "integer", "description": "How many processes to return. Default 8."},
    }),
    _tool("open_app", "Launch a desktop application, for example notepad, chrome or spotify.", {
        "name": {"type": "string", "description": "Name of the application to open."},
    }, ["name"]),
    _tool("open_url", "Open a web page in the default browser.", {
        "url": {"type": "string", "description": "The address to open."},
    }, ["url"]),
    _tool("web_search", "Search the web for current information and return result snippets.", {
        "query": {"type": "string", "description": "What to search for."},
        "limit": {"type": "integer", "description": "How many results to return. Default 5."},
    }, ["query"]),
    _tool("find_files", "Find files on this PC by name or glob pattern.", {
        "pattern": {"type": "string", "description": "Filename or glob such as report*.pdf."},
        "root": {"type": "string", "description": "Folder to search in. Defaults to the home folder."},
        "limit": {"type": "integer", "description": "Maximum matches to return."},
    }, ["pattern"]),
    _tool("list_directory", "List the folders and files inside a directory.", {
        "path": {"type": "string", "description": "Folder path. Defaults to the home folder."},
    }),
    _tool("read_text_file", "Read the contents of a text file on this PC.", {
        "path": {"type": "string", "description": "Full path to the file."},
        "max_chars": {"type": "integer", "description": "Maximum characters to read."},
    }, ["path"]),
    _tool("media_control", "Control system volume or media playback.", {
        "action": {
            "type": "string",
            "enum": list(_VK),
            "description": "One of volume_up, volume_down, volume_mute, play_pause, next_track, previous_track.",
        },
        "steps": {"type": "integer", "description": "How many volume steps to apply. Default 5."},
    }, ["action"]),
    _tool("lock_screen", "Lock this computer's screen.", {}),
    _tool("run_powershell",
          "Run a PowerShell command on this PC. Only works if the server was started with shell access enabled.", {
              "command": {"type": "string", "description": "The PowerShell command to run."},
          }, ["command"]),
]

REGISTRY = {
    "get_time": get_time,
    "system_info": system_info,
    "list_processes": list_processes,
    "open_app": open_app,
    "open_url": open_url,
    "web_search": web_search,
    "find_files": find_files,
    "list_directory": list_directory,
    "read_text_file": read_text_file,
    "media_control": media_control,
    "lock_screen": lock_screen,
    "run_powershell": run_powershell,
}


def call(name, arguments):
    """Dispatch a tool call from the model, never raising."""
    func = REGISTRY.get(name)
    if func is None:
        return _err("Unknown tool '" + str(name) + "'.")
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        return func(**arguments)
    except TypeError as exc:
        return _err("Bad arguments for " + str(name) + ": " + str(exc))
    except Exception as exc:
        return _err(str(name) + " failed: " + str(exc))


# --------------------------------------------------------------------------
# Feature modules. Each one exposes its own SCHEMAS and REGISTRY, merged here
# so the model sees a single flat tool list.
# --------------------------------------------------------------------------

from . import (browser, cognition, desktop, dev, kb, knowledge,  # noqa: E402
               mcp_manager, messaging, missions, network, personal, proactive,
               registry, tasks, vision, voice, webapps)

MODULES = (personal, knowledge, kb, webapps, messaging, network, desktop,
           vision, browser, tasks, proactive, dev, registry, missions,
           cognition, voice, mcp_manager)

for _module in MODULES:
    for _schema in _module.SCHEMAS:
        _name = _schema["function"]["name"]
        if _name in REGISTRY:
            raise RuntimeError("Duplicate tool name across modules: " + _name)
        SCHEMAS.append(_schema)
    REGISTRY.update(_module.REGISTRY)
