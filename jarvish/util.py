"""Shared helpers used by every tool module."""

import os
import subprocess
from pathlib import Path

IS_WINDOWS = os.name == "nt"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def ok(**fields):
    return dict(ok=True, **fields)


def err(message):
    return {"ok": False, "error": message}


def as_int(value, default, low, high):
    """Coerce a model-supplied number, which often arrives as a string."""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default
    return max(low, min(number, high))


def as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def expand(path):
    return Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()


def powershell(script, timeout=45):
    """Run a fixed internal PowerShell snippet.

    This is for Jarvish's own plumbing (netsh, WMI, radios). It is deliberately
    separate from the `run_powershell` tool, which executes model-authored
    commands and stays disabled unless the user opts in.
    """
    if not IS_WINDOWS:
        return err("This feature only works on Windows.")

    # Tools like netsh emit the OEM codepage, which Python would otherwise decode
    # as cp1252 and choke on. Force UTF-8 out of PowerShell and decode leniently.
    preamble = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", preamble + script],
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return err("The command timed out.")
    except Exception as exc:
        return err("Command failed: " + str(exc))

    stdout = (completed.stdout or b"").decode("utf-8", "replace").strip()
    stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()

    if completed.returncode != 0:
        return err((stderr or stdout)[:500]
                   or "Command exited with code " + str(completed.returncode))
    return ok(output=stdout)


def tool(name, description, properties=None, required=()):
    """Build one JSON schema entry for the model's tool list."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
            },
        },
    }


def string(description, enum=None):
    field = {"type": "string", "description": description}
    if enum:
        field["enum"] = list(enum)
    return field


def integer(description):
    return {"type": "integer", "description": description}


def boolean(description):
    return {"type": "boolean", "description": description}
