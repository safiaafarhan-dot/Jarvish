"""Jarvish launcher.

    python new.py                start the server and open the UI
    python new.py --check        verify dependencies, Ollama, hardware and tools
    python new.py --doctor       find problems that have a known fix, and name the fix
    python new.py --stop         stop a Jarvish already listening on the port
    python new.py --backup       snapshot the project to a zip outside this folder
    python new.py --restore      list snapshots (or name one to unpack it)
    python new.py --models       every pulled model, and which fits this machine
    python new.py --free         unload idle models and name what else eats RAM
    python new.py --test         run the test suites and summarise
    python new.py --no-browser   start without opening a browser tab
    python new.py --autonomy 3   set the autonomy ceiling and exit

The launcher is the only part of Jarvish that runs before anything works, so it
is the right place to catch the failures that leave no other trace: a port still
held by yesterday's process, an Ollama that quietly fell back to the CPU, a
model that cannot fit in the memory this machine actually has free.
"""

import argparse
import asyncio
import datetime
import fnmatch
import glob
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile

GB = 1024 ** 3
HERE = os.path.dirname(os.path.abspath(__file__))

# This project lives under a path containing non-Latin characters, and the
# Windows console is cp1252. Printing any absolute path then raises
# UnicodeEncodeError and takes down a command that had already succeeded, which
# is a spectacularly unhelpful way to fail. Degrade the characters, not the run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass          # not a real console, or an old Python; nothing to fix


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------

def _require_dependencies():
    missing = []
    for module, package in (
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn[standard]"),
        ("httpx", "httpx"),
        ("psutil", "psutil"),
    ):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        print("Missing Python packages: " + ", ".join(missing))
        print("Install them with:\n")
        print("    python -m pip install -r requirements.txt\n")
        sys.exit(1)


# --------------------------------------------------------------------------
# Ports and the process holding them
# --------------------------------------------------------------------------

def _port_in_use(host, port):
    """True if something is already listening there."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.6)
    try:
        return probe.connect_ex((_dialable(host), port)) == 0
    finally:
        probe.close()


def _dialable(host):
    """The address to connect to when the server is bound to `host`."""
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def _free_port(host, start, tries=20):
    """The first free port at or after `start`, or None if they are all taken."""
    for port in range(start, start + tries):
        if not _port_in_use(host, port):
            return port
    return None


def _holder(port):
    """The process listening on `port`, as (pid, name), or (None, None).

    Knowing *what* holds the port is the difference between "Jarvish is already
    running" and "something else took 8000", which need opposite responses.
    """
    try:
        import psutil
    except ImportError:
        return None, None

    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                if conn.pid:
                    try:
                        return conn.pid, psutil.Process(conn.pid).name()
                    except Exception:
                        return conn.pid, "unknown"
    except Exception:
        pass          # net_connections needs privileges on some systems
    return None, None


def _is_jarvish(host, port, timeout=6.0, attempts=2):
    """Whether the thing on this port answers like Jarvish.

    Generous with time on purpose. `/api/health` inspects Ollama, the registry
    and the mission runner, and a server already busy with CPU inference can
    take seconds to answer. A short timeout makes Jarvish fail to recognise
    itself, and then `--stop` refuses to stop the very server it started —
    which is exactly the wrong answer to give about your own process.
    """
    url = "http://" + _dialable(host) + ":" + str(port) + "/api/health"
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            return isinstance(payload, dict) and "tool_count" in payload, payload
        except Exception:
            if attempt + 1 < attempts:
                time.sleep(1.0)
    return False, None


def _stop(host, port):
    """Stop a Jarvish listening on this port. Refuses to kill anything else."""
    if not _port_in_use(host, port):
        print("Nothing is listening on port " + str(port) + ".")
        return 0

    mine, payload = _is_jarvish(host, port)
    pid, name = _holder(port)

    if not mine:
        print("Port " + str(port) + " is held by " + str(name or "an unknown process") +
              " (pid " + str(pid) + "), which is not Jarvish.")
        print("Refusing to stop it. Pick another port instead:  python new.py --port 8001")
        return 1

    if pid is None:
        print("Jarvish is on port " + str(port) + " but its process could not be identified.")
        print("Close the window it is running in.")
        return 1

    try:
        import psutil
        process = psutil.Process(pid)
        process.terminate()
        try:
            process.wait(timeout=8)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except Exception as exc:
        print("Could not stop pid " + str(pid) + ": " + str(exc))
        return 1

    tools_seen = (payload or {}).get("tool_count")
    print("Stopped Jarvish on port " + str(port) + " (pid " + str(pid) +
          (", " + str(tools_seen) + " tools" if tools_seen else "") + ").")
    return 0


# --------------------------------------------------------------------------
# Hardware: what this machine can actually run
# --------------------------------------------------------------------------

def _memory():
    """(total_gb, available_gb) or (None, None)."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.total / GB, vm.available / GB
    except Exception:
        return None, None


def _run(command, timeout=8):
    """Run a command and return stdout, or None. Never raises."""
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout, shell=False)
        return done.stdout if done.returncode == 0 else None
    except Exception:
        return None


def _gpus():
    """Every NVIDIA GPU as {name, total_mb, used_mb}, or [] if none/no driver."""
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
                "--format=csv,noheader,nounits"])
    if not out:
        return []
    found = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                found.append({"name": parts[0],
                              "total_mb": int(parts[1]),
                              "used_mb": int(parts[2])})
            except ValueError:
                continue
    return found


def _ollama_compute():
    """What Ollama is actually running on.

    A loaded model is asked first, because it is current and unambiguous: if
    Ollama reports VRAM against it, the GPU is in use, full stop.

    The log is only the fallback, for when nothing is loaded yet. Reading it is
    also more delicate than it looks — Ollama writes this line **once, at
    startup**, and the log then grows for hours. An earlier version of this
    function read the last 4000 lines, which silently stopped finding the line
    as soon as the log outgrew that window. The launcher then concluded there
    was no GPU, judged qwen3:8b against RAM alone, and downgraded to a weaker
    model whose tool choices are measurably worse — turning a working setup
    slow for a reason that had nothing to do with the hardware. Scan all of it.
    """
    for name, vram in _loaded_models().items():
        if vram and vram > 0:
            gpus = _gpus()
            return {"library": "CUDA",
                    "name": gpus[0]["name"] if gpus else "GPU",
                    "total": None}

    log = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Ollama", "server.log")
    if not os.path.exists(log):
        return None
    try:
        with open(log, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception:
        return None

    for line in reversed(lines):
        if "inference compute" not in line:
            continue
        found = {"library": None, "name": None, "total": None}
        for key, field in (("library=", "library"), ("description=", "name"),
                           ("total=", "total")):
            if key in line:
                value = line.split(key, 1)[1]
                value = value.split(" ")[0] if not value.startswith('"') else \
                    value[1:].split('"')[0]
                found[field] = value.strip().strip('"')
        return found
    return None


def _stranded_installer_files():
    """Inno Setup temporaries left in the Ollama tree by an interrupted update.

    This is not a hypothetical: an update that cannot rename its payload leaves
    the file as `is-XXXXXXXX.tmp`, the GPU backend never lands, and Ollama falls
    back to the CPU silently. Nothing else reports it, so the launcher does.
    """
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama")
    if not os.path.isdir(base):
        return []
    stranded = []
    for path in glob.glob(os.path.join(base, "**", "is-*.tmp"), recursive=True):
        try:
            size_mb = os.path.getsize(path) / (1024 ** 2)
        except OSError:
            continue
        if size_mb >= 1:          # tiny temporaries are ordinary installer noise
            stranded.append({"path": path, "size_mb": round(size_mb, 1)})
    return stranded


def _model_bytes(model):
    """Size on disk of a pulled model, from Ollama's own inventory."""
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=4) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    for entry in payload.get("models", []):
        if entry.get("name") == model or entry.get("model") == model:
            return entry.get("size")
    return None


# Headroom the operating system, the server and the browser still need once a
# model is resident. Calibrated against measurements on this machine, not
# guessed: qwen3:8b (4.9 GB) loaded with 5.7 GB free left 0.8 GB and generation
# stalled completely; llama3.2 (2.6 GB) with the same 5.7 GB free left 2.7 GB
# and answered in seconds. The difference between those two is the number below.
RAM_HEADROOM_GB = 2.0

# VRAM is not shared with the rest of the system, so a model only has to cover
# itself plus its context there.
VRAM_OVERHEAD = 1.15


def _loaded_models():
    """Models Ollama currently holds resident, as {name: vram_bytes}.

    A model that is already loaded and answering has proved it fits, and no
    calculation should be allowed to argue otherwise.
    """
    try:
        with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=4) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return {}
    return {m.get("name"): m.get("size_vram", 0) for m in payload.get("models", [])}


def _fit(model_gb, vram_free_gb, ram_free_gb):
    """Where this model can actually run, in one sentence.

    "Fits" has to mean *runs usably*, not merely *loads*. A model that loads
    into the last of free RAM does not report an error — it swaps, and
    generation slows until replies stop arriving at all, which reads as a
    broken assistant rather than a full machine.

    VRAM and RAM are added, not chosen between: Ollama splits a model across
    both when it cannot fit one, and that split is the normal case on a 6 GB
    card. Treating them as either/or declares an 8B model unusable on a machine
    that is in fact running it at 22 tokens a second.
    """
    if model_gb is None:
        return None
    on_gpu = model_gb * VRAM_OVERHEAD
    needed_ram = model_gb + RAM_HEADROOM_GB

    if vram_free_gb is not None and on_gpu <= vram_free_gb:
        return ("gpu", "fits in VRAM (needs ~%.1f GB, %.1f GB free)" % (on_gpu, vram_free_gb))

    # Split: whatever VRAM holds is memory the system never has to find, so only
    # the remainder needs RAM headroom.
    if vram_free_gb is not None and ram_free_gb is not None:
        spill = max(model_gb - vram_free_gb, 0.0)
        if spill + RAM_HEADROOM_GB <= ram_free_gb:
            return ("split",
                    "splits across GPU and RAM (%.1f GB on the GPU, %.1f GB in RAM of %.1f GB free)"
                    % (min(model_gb, vram_free_gb), spill, ram_free_gb))

    if ram_free_gb is not None and needed_ram <= ram_free_gb:
        return ("cpu", "runs on the CPU in RAM (needs ~%.1f GB with headroom, %.1f GB free)"
                % (needed_ram, ram_free_gb))
    return ("tight", "would swap and stall (needs ~%.1f GB with headroom, RAM has %.1f GB free)"
            % (needed_ram, ram_free_gb if ram_free_gb is not None else 0.0))


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------

def _report_hardware(model):
    """RAM, GPU and what Ollama decided to use. Facts, then the verdict."""
    total_gb, avail_gb = _memory()
    print()
    if total_gb:
        used_pct = (total_gb - avail_gb) / total_gb * 100
        print("RAM           %.1f GB total, %.1f GB free (%.0f%% used)"
              % (total_gb, avail_gb, used_pct))
        if used_gb_warning(used_pct, avail_gb):
            print("              LOW - a model loading now will push this machine into swap")

    gpus = _gpus()
    loaded = _loaded_models()
    vram_free_gb = None
    if not gpus:
        print("GPU           none detected (no NVIDIA driver on PATH)")
    for gpu in gpus:
        free_mb = gpu["total_mb"] - gpu["used_mb"]
        if vram_free_gb is None:
            # VRAM held by Ollama's own models is reclaimable — it evicts them
            # to load another. Counting it as spoken for makes this report say
            # a model will not fit while that very model is running.
            vram_free_gb = free_mb / 1024 + sum(loaded.values()) / GB
        print("GPU           %s" % gpu["name"])
        print("VRAM          %.1f GB total, %.1f GB free%s"
              % (gpu["total_mb"] / 1024, free_mb / 1024,
                 " (+%.1f GB held by Ollama, reclaimable)" % (sum(loaded.values()) / GB)
                 if loaded else ""))

    compute = _ollama_compute()
    if compute and compute.get("library"):
        library = compute["library"]
        where = compute.get("name") or library
        print("Ollama runs on %s (%s)" % (library, where))
        if gpus and library.lower() == "cpu":
            print("              MISMATCH - a GPU is present but Ollama is not using it.")
            print("              Run:  python new.py --doctor")
    else:
        print("Ollama runs on unknown (no startup line in the Ollama log yet)")

    size = _model_bytes(model)
    if size:
        model_gb = size / GB
        print("Model size    %.1f GB on disk" % model_gb)
        if model in loaded:
            print("Fit           loaded and answering now (%.1f GB of it in VRAM)"
                  % (loaded[model] / GB))
        else:
            verdict = _fit(model_gb, vram_free_gb, avail_gb)
            if verdict:
                print("Fit           %s" % verdict[1])

    return gpus, compute


def used_gb_warning(used_pct, avail_gb):
    """Whether memory pressure is worth saying out loud."""
    return used_pct >= 85 or (avail_gb is not None and avail_gb < 2.0)


def _report_cognition():
    """What Jarvish is currently allowed to do, and what it has learned.

    The autonomy ceiling decides how much runs without being asked, so it
    belongs in a pre-flight report: starting the server without knowing the
    level is starting it without knowing what it will do on its own.
    """
    from jarvish import cognition

    state = cognition.autonomy_status()
    ceiling = state["runs_without_asking"]
    print()
    print("Autonomy      L" + str(state["level"]) + " " + state["name"])
    print("Runs unasked  " + ("nothing" if ceiling == "nothing"
                              else ceiling + " risk and below"))
    print("Missions      " + ("permitted" if state["missions_allowed"]
                              else "not permitted at this level"))
    print("              change it with:  python new.py --autonomy N")

    # Strategy memory and tool statistics only exist once Jarvish has actually
    # run something, so an empty report here is a fact, not a fault.
    stats = cognition.reliability()
    print()
    if stats.get("total_runs"):
        print("Tool history  " + str(stats["total_runs"]) + " calls across " +
              str(stats["tools"]) + " tools")
        for row in stats.get("unreliable", [])[:3]:
            print("  unreliable  " + row["tool"] + "  " +
                  str(int(row["failure_rate"] * 100)) + "% failed over " +
                  str(row["runs"]) + " runs")
    else:
        print("Tool history  nothing recorded yet")


def _mcp(argv, host, port):
    """The `mcp` operator commands: list, status, tools, connect, disconnect, reload.

    These talk to the *running* Jarvish when there is one, because that is the
    process holding the connections — asking a fresh process would only ever
    report "not started". With nothing running they fall back to reporting the
    configuration, which is still the useful answer to "what would connect?".
    """
    import json as _json
    import urllib.error
    import urllib.request

    command = (argv[0] if argv else "status").lower()
    name = argv[1] if len(argv) > 1 else ""
    base = "http://%s:%d" % (host if host not in ("0.0.0.0", "::") else "127.0.0.1",
                             port)

    def api(path, payload=None):
        # urllib picks the method from whether there is a body, so an action
        # with nothing to say still has to send `{}` — otherwise `reload` goes
        # out as a GET and comes back 405.
        url = base + path
        data = _json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(request, timeout=30) as response:
            return _json.loads(response.read().decode("utf-8", "replace"))

    live = _port_in_use(host, port)

    if command in ("list", "status"):
        if not live:
            from jarvish import mcp_config
            report = mcp_config.load()
            if not report["configured"]:
                print("No mcp.json. Jarvish runs on its built-in capabilities.")
                print("Expected at: " + report["path"])
                return 0
            print("Configured (Jarvish is not running, so nothing is connected):")
            for server in report["servers"]:
                print("  %-14s %-7s %s" % (
                    server["name"], server["transport"],
                    "" if server["enabled"] else "disabled"))
            for bad in report["rejected"]:
                print("  %-14s REJECTED  %s" % (bad["name"], bad["error"]))
            return 0
        state = api("/api/mcp")
        if not state["configured"]:
            print("No mcp.json. Jarvish runs on its built-in capabilities.")
            return 0
        print("%d server(s), %d ready, %d capabilities"
              % (state["server_count"], state["ready_count"], state["tool_count"]))
        for server in state["servers"]:
            print("  %-14s %-13s %2d tools  %s" % (
                server["name"], server["state"], server["tool_count"],
                server["error"] or ""))
        for bad in state["rejected"]:
            print("  %-14s REJECTED      %s" % (bad["name"], bad["error"]))
        return 0

    if not live:
        print("Jarvish is not running on %s:%d — start it first." % (host, port))
        return 1

    if command == "tools":
        listed = api("/api/mcp/tools" + ("?server=" + name if name else ""))
        if not listed["count"]:
            print("No MCP capabilities are registered.")
            return 0
        print("%-30s %-11s %-8s %s" % ("NAME", "SERVER", "RISK", "CONFIRM"))
        for row in listed["tools"]:
            print("%-30s %-11s %-8s %s" % (
                row["name"], row["server"], row["risk_level"],
                "yes" if row["requires_confirmation"] else "no"))
        print("%d capabilities" % listed["count"])
        return 0

    if command in ("connect", "disconnect"):
        if not name:
            print("Name a server:  python new.py --mcp %s <server>" % command)
            return 1
        outcome = api("/api/mcp/" + command, {"server": name})
    elif command == "reload":
        outcome = api("/api/mcp/reload", {})
    else:
        print("Unknown command '%s'. Try: list, status, tools, connect, "
              "disconnect, reload." % command)
        return 1

    print(_json.dumps(outcome, indent=2)[:2000])
    return 0 if outcome.get("ok") else 1


def _report_mcp():
    """What the MCP layer is doing, if anything.

    Deliberately quiet when nothing is configured: no MCP server is the normal
    state, and a self-check that shouts about it teaches people to ignore it.
    """
    from jarvish import mcp_client, mcp_config

    installed, why = mcp_client.available()
    report = mcp_config.load()

    print("")
    print("MCP")
    if not report["enabled"]:
        print("  disabled (JARVISH_MCP_ENABLED=0)")
        return
    if not report["configured"]:
        print("  no mcp.json - running on built-in capabilities only")
        print("  add one at " + report["path"] + " to extend Jarvish")
        return
    if not installed:
        print("  mcp.json found, but the SDK is missing: " + str(why))
        print("      pip install mcp")
        return

    print("  config      " + report["path"])
    for server in report["servers"]:
        bits = [server["transport"]]
        if not server["enabled"]:
            bits.append("disabled")
        if server["missing_env"]:
            bits.append("missing env: " + ", ".join(server["missing_env"]))
        print("  %-12s %s" % (server["name"], ", ".join(bits)))
    for bad in report["rejected"]:
        print("  %-12s REJECTED - %s" % (bad["name"], bad["error"]))
    if report["error"]:
        print("  error       " + report["error"])
    print("  Servers connect when Jarvish starts. Use 'mcp status' for live state.")


def _check():
    """Print a diagnostic report without starting the server."""
    from jarvish import llm, tools
    from jarvish.config import MODEL, OLLAMA_HOST

    print("Jarvish self-check\n" + "-" * 52)
    print("Python        " + sys.version.split()[0])
    print("Ollama host   " + OLLAMA_HOST)
    print("Model         " + MODEL)

    status = asyncio.run(llm.health())
    if not status["online"]:
        print("Ollama        OFFLINE - " + status.get("error", "no response"))
        print("\n  Install it from https://ollama.com/download, then run:")
        print("      ollama pull " + MODEL)
    else:
        print("Ollama        online")
        print("Models pulled " + (", ".join(status["models"]) or "none"))
        if not status.get("model_installed"):
            print("\n  Model '" + MODEL + "' is not pulled yet. Run:")
            print("      ollama pull " + MODEL)

    print("\nTools registered: " + str(len(tools.REGISTRY)))
    probe = tools.get_time()
    print("Sample tool call get_time -> " + probe["time"] + " on " + probe["date"])
    print("Shell access  " + ("ENABLED" if tools.ALLOW_SHELL else "disabled"))

    _report_hardware(MODEL)
    _report_cognition()
    _report_mcp()

    print("-" * 52)
    return 0 if status["online"] else 1


# --------------------------------------------------------------------------
# Doctor: only problems that have a known fix
# --------------------------------------------------------------------------

def _doctor(host, port):
    """Find what is wrong and say exactly how to fix it.

    `--check` reports state; this reports *problems*. A clean run printing
    nothing but 'no problems found' is the intended common case, so anything
    listed here has to be worth acting on.
    """
    from jarvish.config import MODEL

    print("Jarvish doctor\n" + "-" * 52)
    problems = []

    # 1. A port held by something that is not Jarvish, or by a stale Jarvish.
    if _port_in_use(host, port):
        mine, _ = _is_jarvish(host, port)
        pid, name = _holder(port)
        if mine:
            problems.append((
                "Jarvish is already running on port " + str(port) +
                " (pid " + str(pid) + ").",
                "That is why a second start exits immediately. Either use it at "
                "http://" + _dialable(host) + ":" + str(port) +
                " or stop it:  python new.py --stop"))
        else:
            problems.append((
                "Port " + str(port) + " is held by " + str(name or "another process") +
                " (pid " + str(pid) + ").",
                "Start on a different port:  python new.py --port " +
                str(_free_port(host, port + 1) or port + 1)))

    # 2. Ollama on the CPU while a GPU sits idle - the expensive silent failure.
    gpus = _gpus()
    compute = _ollama_compute()
    if gpus and compute and (compute.get("library") or "").lower() == "cpu":
        problems.append((
            "Ollama is running on the CPU even though " + gpus[0]["name"] +
            " is present.",
            "Every model then loads into system RAM instead of VRAM. See the "
            "next item if a stranded installer file is reported."))

    # 3. The exact shape of that failure: an interrupted Ollama update. Only a
    #    problem while the GPU is going unused — once Ollama is on CUDA the
    #    leftovers are inert, and reporting them then sends the user to fix
    #    something that is already fixed.
    if not (compute and (compute.get("library") or "").lower() != "cpu"):
        for stranded in _stranded_installer_files():
            problems.append((
                "An interrupted Ollama update left " + os.path.basename(stranded["path"]) +
                " (" + str(stranded["size_mb"]) + " MB) in " +
                os.path.dirname(stranded["path"]) + ".",
                "A file this size is a runner library that never got renamed into "
                "place, which is what silently drops Ollama to the CPU. Complete "
                "the update with:  winget upgrade --id Ollama.Ollama"))

    # 4. Memory pressure severe enough that loading a model will swap.
    total_gb, avail_gb = _memory()
    if total_gb and used_gb_warning((total_gb - avail_gb) / total_gb * 100, avail_gb):
        size = _model_bytes(MODEL)
        detail = ""
        if size:
            detail = (" Loading " + MODEL + " needs about %.1f GB." % (size / GB * 1.15))
        hogs = _memory_hogs(4)
        naming = "; ".join("%s %.1f GB" % (h["name"], h["gb"]) for h in hogs)
        problems.append((
            "Only %.1f GB of %.1f GB RAM is free." % (avail_gb, total_gb),
            "Biggest consumers: " + naming + ". Free what Jarvish can with "
            "'python new.py --free'." + detail))

    # 5. Ollama unreachable, or the configured model not pulled.
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
            pulled = [m.get("name") for m in
                      json.loads(r.read().decode("utf-8", "replace")).get("models", [])]
        if MODEL not in pulled:
            problems.append(("The configured model '" + MODEL + "' is not pulled.",
                             "Run:  ollama pull " + MODEL))
    except Exception:
        problems.append(("Ollama is not answering on localhost:11434.",
                         "Start it from the Start menu, or install it from "
                         "https://ollama.com/download"))

    if not problems:
        print("No problems found.")
        print("-" * 52)
        return 0

    for index, (problem, fix) in enumerate(problems, 1):
        print()
        print(str(index) + ". " + problem)
        print("   FIX: " + fix)
    print()
    print("-" * 52)
    return 1


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------

# Kept deliberately outside the project tree, and outside OneDrive: a snapshot
# stored beside the thing it protects is not a snapshot. This project has been
# lost once already to a delete that took the whole folder.
BACKUP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                          "Jarvish", "backups")

# Source is worth keeping; caches, browser profiles and screen captures are not,
# and they are what makes the folder 225 MB instead of 2.
SKIP_DIRS = ("__pycache__", ".git", "node_modules",
             os.path.join("data", "browser-profile"),
             os.path.join("data", "captures"))
SKIP_FILES = ("*.pyc", "*.pyo", "*.log", "tasks-test.db")


def _skipped(rel):
    """Whether this project-relative path stays out of a snapshot."""
    parts = rel.replace("/", os.sep).split(os.sep)
    for skip in SKIP_DIRS:
        pieces = skip.split(os.sep)
        if parts[:len(pieces)] == pieces or skip in parts:
            return True
    return any(fnmatch.fnmatch(parts[-1], pattern) for pattern in SKIP_FILES)


def _backup(destination=None):
    """Zip the project somewhere it cannot be taken out by the same delete."""
    target_dir = destination or BACKUP_DIR
    try:
        os.makedirs(target_dir, exist_ok=True)
    except Exception as exc:
        print("Could not create " + target_dir + ": " + str(exc))
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = os.path.join(target_dir, "jarvish-" + stamp + ".zip")

    count, raw = 0, 0
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for root, dirs, files in os.walk(HERE):
                rel_root = os.path.relpath(root, HERE)
                rel_root = "" if rel_root == "." else rel_root
                dirs[:] = [d for d in dirs
                           if not _skipped(os.path.join(rel_root, d) if rel_root else d)]
                for name in files:
                    rel = os.path.join(rel_root, name) if rel_root else name
                    if _skipped(rel):
                        continue
                    full = os.path.join(root, name)
                    try:
                        bundle.write(full, rel)
                        raw += os.path.getsize(full)
                        count += 1
                    except Exception:
                        continue      # a locked db is not worth failing the run
    except Exception as exc:
        print("Snapshot failed: " + str(exc))
        return 1

    size = os.path.getsize(archive)
    print("Snapshot written")
    print("  " + archive)
    print("  %d files, %.1f MB compressed from %.1f MB" % (count, size / 1e6, raw / 1e6))
    existing = _snapshots(target_dir)
    print("  %d snapshot%s kept in %s" % (len(existing), "" if len(existing) == 1 else "s",
                                          target_dir))
    return 0


def _snapshots(target_dir=None):
    """Every snapshot, newest first."""
    target_dir = target_dir or BACKUP_DIR
    if not os.path.isdir(target_dir):
        return []
    found = []
    for name in os.listdir(target_dir):
        if name.startswith("jarvish-") and name.endswith(".zip"):
            path = os.path.join(target_dir, name)
            found.append({"path": path, "name": name,
                          "size": os.path.getsize(path),
                          "at": os.path.getmtime(path)})
    return sorted(found, key=lambda s: -s["at"])


def _restore(archive=None):
    """Unpack a snapshot *beside* the project, never over it.

    Restoring in place would mean a mistyped argument destroys current work, so
    this always extracts to a new directory and leaves the comparison to you.
    """
    snapshots = _snapshots()
    if archive is None:
        if not snapshots:
            print("No snapshots yet. Make one with:  python new.py --backup")
            return 1
        print("Snapshots in " + BACKUP_DIR + ":\n")
        for entry in snapshots:
            when = datetime.datetime.fromtimestamp(entry["at"]).strftime("%Y-%m-%d %H:%M")
            print("  %-34s %6.1f MB   %s" % (entry["name"], entry["size"] / 1e6, when))
        print("\nRestore one with:  python new.py --restore <name>")
        return 0

    path = archive if os.path.isfile(archive) else os.path.join(BACKUP_DIR, archive)
    if not os.path.isfile(path):
        print("No such snapshot: " + archive)
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(os.path.dirname(HERE),
                       os.path.basename(HERE) + "-restored-" + stamp)
    try:
        with zipfile.ZipFile(path) as bundle:
            names = bundle.namelist()
            bundle.extractall(out)
    except Exception as exc:
        print("Restore failed: " + str(exc))
        return 1

    print("Restored %d files to:" % len(names))
    print("  " + out)
    print("\nNothing in your working folder was touched. Compare the two, then")
    print("move what you want across yourself.")
    return 0


# --------------------------------------------------------------------------
# Models and tests
# --------------------------------------------------------------------------

def _models():
    """Every pulled model, with an honest verdict on whether it fits here."""
    from jarvish.config import MODEL

    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as r:
            entries = json.loads(r.read().decode("utf-8", "replace")).get("models", [])
    except Exception as exc:
        print("Ollama is not answering: " + str(exc))
        return 1

    _, avail_gb = _memory()
    gpus = _gpus()
    vram_free_gb = (gpus[0]["total_mb"] - gpus[0]["used_mb"]) / 1024 if gpus else None
    compute = _ollama_compute()
    on_cpu = bool(compute and (compute.get("library") or "").lower() == "cpu")

    print("Models pulled\n" + "-" * 52)
    if on_cpu and gpus:
        print("Ollama is on the CPU, so VRAM is unavailable no matter what fits.")
        print("Run 'python new.py --doctor' first.\n")
        vram_free_gb = None

    # Largest that fits, not smallest. A smaller model is not a better one: the
    # measured failure on this project is that a lighter model stops calling
    # tools correctly, which costs far more than the seconds of latency saved.
    best = None
    for entry in sorted(entries, key=lambda e: e.get("size", 0)):
        name = entry.get("name", "?")
        size_gb = entry.get("size", 0) / GB
        verdict = _fit(size_gb, vram_free_gb, avail_gb)
        mark = " <- configured" if name == MODEL else ""
        print("  %-22s %5.1f GB  %s%s" % (name, size_gb, verdict[1] if verdict else "", mark))
        if verdict and verdict[0] in ("gpu", "cpu"):
            best = name          # ascending order, so the last match is the largest

    print()
    if best is None:
        print("Nothing pulled fits comfortably in the memory free right now.")
        print("Close something, or run 'python new.py --doctor'.")
    elif best == MODEL:
        print("The configured model is the largest that fits right now. Keep it.")
    else:
        print("The largest model that fits right now is '" + best + "'.")
        print("Switch with:  set JARVISH_MODEL=" + best + "   (then restart Jarvish)")
        print()
        print("Only do this if you have to. A smaller model was measured on this")
        print("project failing to call tools it was offered, which breaks far more")
        print("than the latency it saves. Freeing memory is the better fix.")
    print("-" * 52)
    return 0


def _tests():
    """Run the suites and summarise. The live one needs a server; say so."""
    suites = ["cognition", "missions", "plugins", "devmode", "proactive",
              "voice", "security", "actions", "deploy", "agent", "mcp", "regression"]
    live = _port_in_use("127.0.0.1", 8000)

    print("Running the test suites\n" + "-" * 52)
    if not live:
        print("No server on :8000 - 'regression' will be skipped.")
        print("Start one first for the full sweep:  python new.py --no-browser\n")

    total_pass, total_fail, failed_suites = 0, 0, []
    for suite in suites:
        path = os.path.join(HERE, "tests", suite + ".py")
        if not os.path.isfile(path):
            print("  %-12s missing" % suite)
            continue
        if suite == "regression" and not live:
            print("  %-12s skipped (needs a running server)" % suite)
            continue
        try:
            done = subprocess.run([sys.executable, path], capture_output=True,
                                  text=True, timeout=900, cwd=HERE)
        except Exception as exc:
            print("  %-12s could not run: %s" % (suite, exc))
            failed_suites.append(suite)
            continue
        # Find the "N passed, N failed" line rather than the last line: a suite
        # may print notes after its summary, and reading those as the result
        # reports a passing suite as broken.
        lines = [line.strip() for line in (done.stdout or "").splitlines() if line.strip()]
        scored = [line for line in lines if "passed," in line and "failed" in line]
        summary = scored[-1] if scored else (lines[-1] if lines else "no output")
        print("  %-12s %s" % (suite, summary))
        try:
            passed = int(summary.split(" passed")[0].split()[-1])
            failed = int(summary.split(",")[1].split("failed")[0].strip())
            total_pass += passed
            total_fail += failed
            if failed:
                failed_suites.append(suite)
        except Exception:
            failed_suites.append(suite)

    print("-" * 52)
    print("%d passed, %d failed" % (total_pass, total_fail))
    if failed_suites:
        print("Look at: " + ", ".join(failed_suites))
    return 1 if total_fail or failed_suites else 0


def _memory_hogs(limit=6):
    """The biggest memory consumers, grouped by process name.

    "Close what you are not using" is useless advice on its own. Naming the
    processes turns it into something the user can act on in ten seconds, and
    routinely surfaces things nobody chose to run — Widgets and the Copilot
    host between them are usually a gigabyte.
    """
    try:
        import psutil
    except ImportError:
        return []

    groups = {}
    for process in psutil.process_iter(["name", "memory_info"]):
        try:
            name = process.info["name"]
            rss = process.info["memory_info"].rss
        except Exception:
            continue
        entry = groups.setdefault(name, {"name": name, "count": 0, "bytes": 0})
        entry["count"] += 1
        entry["bytes"] += rss

    ranked = sorted(groups.values(), key=lambda e: -e["bytes"])[:limit]
    for entry in ranked:
        entry["gb"] = round(entry["bytes"] / GB, 2)
    return ranked


# Things that are safe to name as closeable, with what they actually are. A
# process list means nothing to most people; "that is the Widgets panel" does.
_KNOWN_HOGS = {
    "msedgewebview2.exe": "Windows Widgets and the Copilot host — closeable, "
                          "nothing depends on it",
    "chrome.exe": "Chrome — each tab is a process; closing unused tabs frees the most",
    "msedge.exe": "Edge — often running only for Widgets",
    "Code.exe": "VS Code — each window and extension host is a process",
    "llama-server.exe": "the model Jarvish is using; 'python new.py --free' unloads it",
    "OneDrive.exe": "OneDrive sync",
    "SearchIndexer.exe": "Windows Search indexing",
}


def _free_memory():
    """Release what Jarvish can, and name precisely what it cannot.

    Jarvish can honestly free one thing: the model it asked Ollama to load. It
    cannot close the user's browser, and pretending otherwise by killing
    processes would be worse than the problem. So it unloads its own model and
    reports the rest as facts.
    """
    total_gb, before_gb = _memory()
    print("Freeing what Jarvish can\n" + "-" * 52)
    print("RAM free before  %.2f GB of %.2f GB" % (before_gb or 0, total_gb or 0))

    loaded = _loaded_models()
    if loaded:
        for name in loaded:
            body = json.dumps({"model": name, "keep_alive": 0}).encode()
            request = urllib.request.Request(
                "http://localhost:11434/api/generate", data=body,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=30):
                    pass
                print("  unloaded         " + name)
            except Exception as exc:
                print("  could not unload " + name + ": " + str(exc)[:50])
        time.sleep(3)
    else:
        print("  no model was loaded")

    _, after_gb = _memory()
    print("RAM free after   %.2f GB  (recovered %.2f GB)"
          % (after_gb or 0, (after_gb or 0) - (before_gb or 0)))
    print()
    print("The model reloads by itself on the next request, a few seconds slower.")
    print()

    print("Biggest consumers now — Jarvish will not close these for you:")
    for entry in _memory_hogs():
        note = _KNOWN_HOGS.get(entry["name"], "")
        print("  %-24s %5.2f GB  %2d proc%s" %
              (entry["name"], entry["gb"], entry["count"],
               "" if entry["count"] == 1 else "s"))
        if note:
            print("      %s" % note)
    print("-" * 52)
    return 0


def _model_preflight(config, allow_fallback=True):
    """Refuse to start on a model that cannot fit in the memory actually free.

    A model larger than available RAM does not fail loudly. Ollama loads it,
    the machine starts swapping, and generation slows until nothing ever
    finishes — measured here as a request that reaches "analysing" and then
    produces no tokens at all. To the user that is indistinguishable from
    Jarvish being broken, which is the worst possible way for this to present.

    So the fit is checked before the server starts, and if the configured model
    cannot run, the largest one that *can* is used for this run only. Nothing is
    written to disk: fix the memory or the GPU and the next start goes back to
    what you configured.
    """
    size = _model_bytes(config.MODEL)
    if size is None:
        return                    # Ollama is down, or the model is not pulled;
                                  # --check and --doctor both report that.

    # Already resident and answering: it fits, whatever the arithmetic says.
    if config.MODEL in _loaded_models():
        return

    _, avail_gb = _memory()
    gpus = _gpus()
    compute = _ollama_compute()
    on_gpu = bool(compute and (compute.get("library") or "").lower() != "cpu")

    # Count VRAM held by *other* Ollama models as reclaimable — Ollama evicts
    # them to make room. Counting it as taken makes the launcher refuse a model
    # the machine can actually run.
    vram_free_gb = None
    if gpus and on_gpu:
        held_by_ollama = sum(_loaded_models().values()) / GB
        vram_free_gb = (gpus[0]["total_mb"] - gpus[0]["used_mb"]) / 1024 + held_by_ollama

    verdict = _fit(size / GB, vram_free_gb, avail_gb)
    if verdict is None or verdict[0] in ("gpu", "split", "cpu"):
        return                    # it fits somewhere; nothing to do

    print("Warning: " + config.MODEL + " (%.1f GB) does not fit in the %.1f GB of RAM"
          % (size / GB, avail_gb or 0.0))
    print("         free right now. Loading it would swap to disk and replies")
    print("         would stop arriving rather than merely being slow.")

    if not allow_fallback:
        print("         Starting anyway because --no-model-fallback was given.")
        return

    # Largest alternative that fits, so capability is given up only as far as
    # the memory forces. A smaller model has been measured on this project
    # choosing tools less reliably, so this is a fallback, never an upgrade.
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as r:
            entries = json.loads(r.read().decode("utf-8", "replace")).get("models", [])
    except Exception:
        entries = []

    choice = None
    for entry in sorted(entries, key=lambda e: e.get("size", 0)):
        name = entry.get("name")
        if name == config.MODEL:
            continue
        if _fit(entry.get("size", 0) / GB, vram_free_gb, avail_gb)[0] in ("gpu", "split", "cpu"):
            choice = name

    if choice is None:
        print("         Nothing else pulled fits either. Close some applications,")
        print("         then run:  python new.py --doctor")
        return

    wanted = config.MODEL
    os.environ["JARVISH_MODEL"] = choice
    config.MODEL = choice
    print("         Falling back to " + choice + " for this run only.")
    print("         Its tool choices are less reliable. Free memory or fix the")
    print("         GPU (python new.py --doctor) to get " + wanted + " back.")


def _set_autonomy(value):
    """Set the ceiling from the command line, for a headless or scripted start."""
    from jarvish import cognition

    result = cognition.set_level(value)
    if not result.get("ok"):
        # cognition already names every level in its refusal; repeating them
        # here only made the error twice as long as the answer.
        print(result.get("error", "That level was refused."))
        return 1
    print("Autonomy set to L" + str(result["level"]) + " - " + result["name"])
    print(result["detail"])
    print("Runs without asking: " + str(result["runs_without_asking"]) + ".")
    print("This is a ceiling over the risk gate. Irreversible actions still stop.")
    return 0


# --------------------------------------------------------------------------
# Start
# --------------------------------------------------------------------------

def _resolve_port(host, port, allow_shift):
    """Decide which port to actually bind, explaining any change.

    Exiting because the port is busy is correct but unhelpful when the holder is
    a Jarvish the user forgot about, and needlessly strict when it is anything
    else and the next port is free.
    """
    if not _port_in_use(host, port):
        return port

    mine, payload = _is_jarvish(host, port)
    if mine:
        url = "http://" + _dialable(host) + ":" + str(port)
        print("Jarvish is already running at " + url +
              " (" + str((payload or {}).get("tool_count", "?")) + " tools).")
        print("Opening that instead of starting a second one.")
        print("To restart it:  python new.py --stop  &&  python new.py")
        return None

    pid, name = _holder(port)
    print("Port " + str(port) + " is held by " + str(name or "another process") +
          (" (pid " + str(pid) + ")" if pid else "") + ".")

    if not allow_shift:
        print("Pick another port:  python new.py --port " +
              str(_free_port(host, port + 1) or port + 1))
        return None

    shifted = _free_port(host, port + 1)
    if shifted is None:
        print("No free port found nearby. Pass one explicitly with --port.")
        return None
    print("Starting on port " + str(shifted) + " instead.")
    return shifted


def main():
    parser = argparse.ArgumentParser(description="Jarvish voice assistant")
    parser.add_argument("--check", action="store_true", help="run diagnostics and exit")
    parser.add_argument("--doctor", action="store_true",
                        help="report problems that have a known fix, and exit")
    parser.add_argument("--stop", action="store_true",
                        help="stop a Jarvish listening on the port, and exit")
    parser.add_argument("--backup", nargs="?", const=True, default=None, metavar="DIR",
                        help="snapshot the project to a zip outside this folder")
    parser.add_argument("--restore", nargs="?", const=True, default=None, metavar="NAME",
                        help="list snapshots, or unpack one beside the project")
    parser.add_argument("--free", action="store_true",
                        help="unload idle models and name what else is using RAM")
    parser.add_argument("--models", action="store_true",
                        help="every pulled model, and which one fits this machine")
    parser.add_argument("--mcp", nargs="*", metavar="CMD",
                        help="MCP servers: list, status, tools, connect <name>, "
                             "disconnect <name>, reload")
    parser.add_argument("--test", action="store_true",
                        help="run the test suites and summarise")
    parser.add_argument("--autonomy", type=int, default=None, metavar="N",
                        help="set the autonomy ceiling (0-5) and exit")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    parser.add_argument("--no-port-shift", action="store_true",
                        help="fail instead of moving to the next free port")
    parser.add_argument("--no-model-fallback", action="store_true",
                        help="start on the configured model even if it cannot fit")
    parser.add_argument("--host", default=None, help="bind address")
    parser.add_argument("--port", type=int, default=None, help="port to listen on")
    args = parser.parse_args()

    _require_dependencies()

    # Snapshots deliberately come before anything that imports the package: if
    # the tree is damaged, taking a copy of it must still work.
    if args.backup is not None:
        sys.exit(_backup(None if args.backup is True else args.backup))

    if args.restore is not None:
        sys.exit(_restore(None if args.restore is True else args.restore))

    if args.autonomy is not None:
        sys.exit(_set_autonomy(args.autonomy))

    if args.test:
        sys.exit(_tests())

    if args.models:
        sys.exit(_models())

    if args.free:
        sys.exit(_free_memory())

    from jarvish import config

    host = args.host or config.HOST
    port = args.port or config.PORT

    if args.stop:
        sys.exit(_stop(host, port))

    if args.mcp is not None:
        sys.exit(_mcp(args.mcp, host, port))

    if args.doctor:
        sys.exit(_doctor(host, port))

    if args.check:
        sys.exit(_check())

    # An explicit --port is a decision; moving off it would ignore the user.
    resolved = _resolve_port(host, port, allow_shift=not (args.no_port_shift or args.port))

    if resolved is None:
        # Already-running Jarvish: show it rather than leaving a dead terminal.
        if _port_in_use(host, port) and _is_jarvish(host, port)[0] and not args.no_browser:
            webbrowser.open("http://" + _dialable(host) + ":" + str(port))
        sys.exit(1)
    port = resolved

    total_gb, avail_gb = _memory()
    if total_gb and used_gb_warning((total_gb - avail_gb) / total_gb * 100, avail_gb):
        print("Warning: only %.1f GB of %.1f GB RAM free. Run 'python new.py --doctor'."
              % (avail_gb, total_gb))

    # Must happen before `server` is imported: llm.py binds the model name at
    # import time, so a decision made after that would be ignored.
    _model_preflight(config, allow_fallback=not args.no_model_fallback)

    from jarvish import server

    if not args.no_browser:
        url = "http://" + _dialable(host) + ":" + str(port)

        def open_when_ready():
            # Poll rather than sleep a fixed time: a cold start that loads
            # plugins takes longer than a warm one, and a browser opened early
            # lands on a connection error the user then has to reload past.
            for _ in range(40):
                if _port_in_use(host, port):
                    break
                time.sleep(0.25)
            webbrowser.open(url)

        threading.Thread(target=open_when_ready, daemon=True).start()

    try:
        server.serve(host, port)
    except KeyboardInterrupt:
        print("\nJarvish stopped.")


if __name__ == "__main__":
    main()
