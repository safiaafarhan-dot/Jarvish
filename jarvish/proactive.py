"""Proactive intelligence: watching the machine without becoming a nuisance.

The hard part of proactivity is not noticing things. It is deciding when to say
something. A monitor that reports 97% memory every five seconds is worse than
no monitor, because the user learns to ignore it.

So the pipeline separates four distinct things, and only the last one is ever
allowed to touch the machine:

    OBSERVATION   RAM is at 97.2%          - a fact, sampled
    INSIGHT       memory pressure          - a judgement about the fact
    SUGGESTION    close some heavy apps    - offered, never performed
    ACTION        (only when asked)        - goes through the normal risk gate

Nothing here calls the model. Insights are produced from rules over the
telemetry that already exists, which keeps the monitor cheap enough to run on a
machine that is short of memory in the first place.

Noise control is the point of the notification layer: a condition that persists
raises **one** notification, not one per sample. It re-raises only when it
meaningfully changes, and never inside a cooldown or during quiet hours.
"""

import json
import threading
import time
from collections import deque

from .util import DATA_DIR, as_bool, as_int, boolean, err, integer, ok, string, tool

SETTINGS_PATH = DATA_DIR / "proactive.json"

LEVELS = ("info", "low", "medium", "high", "critical")

# How often the monitor samples. Cheap, but no reason to be more eager.
INTERVAL = 8.0

# Per-level cooldowns: how long the same condition stays quiet after firing.
COOLDOWN = {
    "info": 30 * 60,
    "low": 20 * 60,
    "medium": 10 * 60,
    "high": 5 * 60,
    "critical": 3 * 60,
}

# Notifications older than this stop being shown.
EXPIRY = 60 * 60

CATEGORIES = ("system", "storage", "power", "network", "tasks", "browser")

_lock = threading.RLock()
_monitor = {"thread": None, "stop": None, "started": 0.0, "samples": 0}

# The live notification list, plus the memory that keeps it quiet.
_notifications = deque(maxlen=200)
_last_fired = {}        # key -> (when, level, fingerprint)
_history = deque(maxlen=400)

_defaults = {
    "enabled": True,
    "quiet": False,
    "quiet_from": None,          # e.g. 23 for 11pm
    "quiet_to": None,            # e.g. 8 for 8am
    "categories": {name: True for name in CATEGORIES},
    "min_level": "low",
}
_settings = dict(_defaults)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def _load_settings():
    global _settings
    try:
        if SETTINGS_PATH.exists():
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            merged = dict(_defaults)
            merged.update({k: v for k, v in stored.items() if k in _defaults})
            categories = dict(_defaults["categories"])
            categories.update(stored.get("categories") or {})
            merged["categories"] = categories
            _settings = merged
    except Exception:
        _settings = dict(_defaults)
    return _settings


def _save_settings():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(_settings, indent=2), encoding="utf-8")
    except Exception:
        pass


_load_settings()


def _in_quiet_hours():
    start, end = _settings.get("quiet_from"), _settings.get("quiet_to")
    if start is None or end is None:
        return False
    hour = time.localtime().tm_hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end       # a window that crosses midnight


def _muted(level, category):
    """Whether this notification should be suppressed outright."""
    if not _settings["enabled"]:
        return "proactive intelligence is off"
    if _settings["quiet"] and level != "critical":
        return "quiet mode"
    if _in_quiet_hours() and level not in ("high", "critical"):
        return "quiet hours"
    if not _settings["categories"].get(category, True):
        return "category '" + category + "' is off"
    if LEVELS.index(level) < LEVELS.index(_settings.get("min_level", "low")):
        return "below the minimum level"
    return None


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def raise_event(key, level, category, title, message, suggestion=None, data=None,
                fingerprint=None):
    """Record a notification, unless noise control says otherwise.

    `key` identifies the *condition*, not the occurrence — "memory pressure",
    not "memory pressure at 14:03" — which is what makes deduplication and
    cooldowns work. `fingerprint` lets a condition re-fire when it has
    meaningfully changed (crossing from high to critical, say) without firing
    for every wobble in between.
    """
    level = level if level in LEVELS else "low"
    now = time.time()

    with _lock:
        blocked = _muted(level, category)
        previous = _last_fired.get(key)

        if previous and not blocked:
            since = now - previous["at"]
            escalated = LEVELS.index(level) > LEVELS.index(previous["level"])
            changed = fingerprint is not None and fingerprint != previous.get("fingerprint")
            # Same condition, still inside its cooldown, nothing materially
            # different about it: stay quiet.
            if since < COOLDOWN[level] and not escalated and not changed:
                blocked = "cooldown"

        entry = {
            "id": "n" + str(int(now * 1000))[-10:],
            "key": key, "level": level, "category": category,
            "title": title, "message": message, "suggestion": suggestion,
            "data": data or {}, "at": now, "expires": now + EXPIRY,
            "seen": False,
        }
        _history.append(dict(entry, suppressed=blocked))

        if blocked:
            return {"raised": False, "reason": blocked, "key": key}

        # One live notification per condition: replace rather than accumulate.
        for existing in list(_notifications):
            if existing["key"] == key:
                _notifications.remove(existing)
        _notifications.append(entry)
        _last_fired[key] = {"at": now, "level": level, "fingerprint": fingerprint}
        return {"raised": True, "notification": entry}


def _live():
    now = time.time()
    return [n for n in _notifications if n["expires"] > now]


def notifications(include_seen=True, limit=30):
    with _lock:
        items = _live()
        if not include_seen:
            items = [n for n in items if not n["seen"]]
        items = sorted(items, key=lambda n: (-LEVELS.index(n["level"]), -n["at"]))
        grouped = {}
        for item in items:
            grouped.setdefault(item["category"], []).append(item)
        return ok(
            notifications=items[:as_int(limit, 30, 1, 100)],
            count=len(items),
            unseen=sum(1 for n in items if not n["seen"]),
            by_category={k: len(v) for k, v in grouped.items()},
            settings=dict(_settings),
            monitoring=_monitor["thread"] is not None,
        )


def acknowledge(key=None):
    """Mark notifications as seen, so the HUD can stop drawing attention."""
    with _lock:
        touched = 0
        for item in _notifications:
            if key in (None, item["key"], item["id"]):
                item["seen"] = True
                touched += 1
        return ok(acknowledged=touched)


def dismiss(key=None):
    with _lock:
        before = len(_notifications)
        keep = [n for n in _notifications if key not in (None, n["key"], n["id"])]
        _notifications.clear()
        _notifications.extend(keep)
        return ok(dismissed=before - len(_notifications))


def configure(enabled=None, quiet=None, category=None, on=None,
              min_level=None, quiet_from=None, quiet_to=None):
    """Change how talkative Jarvish is allowed to be."""
    with _lock:
        if enabled is not None:
            _settings["enabled"] = as_bool(enabled, True)
        if quiet is not None:
            _settings["quiet"] = as_bool(quiet, False)
        if category:
            name = str(category).strip().lower()
            if name not in CATEGORIES:
                return err("Unknown category '" + name + "'. Options: " +
                           ", ".join(CATEGORIES) + ".")
            _settings["categories"][name] = as_bool(on, True)
        if min_level:
            level = str(min_level).strip().lower()
            if level not in LEVELS:
                return err("Unknown level. Options: " + ", ".join(LEVELS) + ".")
            _settings["min_level"] = level
        if quiet_from is not None:
            _settings["quiet_from"] = as_int(quiet_from, 23, 0, 23)
        if quiet_to is not None:
            _settings["quiet_to"] = as_int(quiet_to, 8, 0, 23)
        _save_settings()
        return ok(settings=dict(_settings))


# --------------------------------------------------------------------------
# Observation -> insight
# --------------------------------------------------------------------------

def _band(value, thresholds):
    """Which band a value falls into, as a coarse fingerprint."""
    for name, floor in thresholds:
        if value >= floor:
            return name
    return "normal"


def evaluate(frame=None):
    """Turn one telemetry frame into insights. Pure: nothing is raised here."""
    from . import telemetry
    frame = frame or telemetry.snapshot()
    found = []

    memory = frame.get("memory") or 0
    if memory >= 90:
        band = _band(memory, [("critical", 96), ("high", 92), ("elevated", 90)])
        # Several windows of one app show up as separate processes; naming
        # "Code.exe, Code.exe" reads like a glitch, so collapse by name.
        heavy = []
        if memory >= 94:
            # Cached deliberately: naming the heaviest apps is a nicety, and
            # forcing a 2-second process scan every time memory is high made the
            # very pressure it was reporting on worse.
            for process in telemetry.top_processes(8):
                if process["name"] not in heavy:
                    heavy.append(process["name"])
                if len(heavy) == 3:
                    break
        heavy = [{"name": name} for name in heavy]
        found.append({
            "key": "memory-pressure",
            "level": "critical" if memory >= 96 else "high",
            "category": "system",
            "title": "Memory pressure",
            "observation": "RAM is at " + str(memory) + "%.",
            "insight": ("The machine is close to swapping, which is what makes "
                        "everything feel slow."),
            "suggestion": ("Closing the heaviest apps would help" +
                           (" — " + ", ".join(p["name"] for p in heavy) if heavy else "") +
                           "."),
            "fingerprint": band,
        })

    cpu = frame.get("cpu") or 0
    if cpu >= 88:
        found.append({
            "key": "cpu-load",
            "level": "medium" if cpu < 95 else "high",
            "category": "system",
            "title": "Sustained CPU load",
            "observation": "CPU is at " + str(cpu) + "%.",
            "insight": "Something is working hard enough to slow other things down.",
            "suggestion": "Ask me what is using the most CPU if this is unexpected.",
            "fingerprint": _band(cpu, [("pegged", 95), ("busy", 88)]),
        })

    disk = frame.get("disk") or 0
    if disk >= 88:
        found.append({
            "key": "disk-space",
            "level": "critical" if disk >= 95 else "medium",
            "category": "storage",
            "title": "Storage filling up",
            "observation": str(disk) + "% of the disk is used, " +
                           str(frame.get("disk_free_gb")) + " GB free.",
            "insight": "Windows starts misbehaving when the system drive runs out.",
            "suggestion": "Clearing Downloads and the recycle bin is usually the quickest win.",
            "fingerprint": _band(disk, [("critical", 95), ("low", 88)]),
        })

    battery = frame.get("battery")
    if battery and not battery["plugged"]:
        percent = battery["percent"]
        if percent <= 20:
            found.append({
                "key": "battery-low",
                "level": "critical" if percent <= 10 else "high",
                "category": "power",
                "title": "Battery low",
                "observation": "Battery is at " + str(percent) + "% and unplugged.",
                "insight": "Unsaved work is at risk once this runs out.",
                "suggestion": "Plug in when you get a chance.",
                "fingerprint": _band(percent, [("critical", 0), ("low", 11)])
                               if percent <= 10 else "low",
            })

    return found


# --------------------------------------------------------------------------
# The monitor
# --------------------------------------------------------------------------

def _check_tasks():
    """Notice tasks that need a human: waiting for permission, or failed."""
    from . import tasks
    try:
        state = tasks.listing(limit=60)
    except Exception:
        return
    for task in state.get("tasks", []):
        if task["state"] == "paused" and task.get("needs"):
            raise_event(
                key="approval:" + task["id"], level="high", category="tasks",
                title="Permission needed",
                message="Background task " + repr(task["title"]) + " is waiting on you.",
                suggestion=str(task["progress"]),
                data={"task": task["id"]}, fingerprint=task["state"])


def _sample():
    from . import telemetry
    frame = telemetry.snapshot()
    _monitor["samples"] += 1

    for insight in evaluate(frame):
        raise_event(
            key=insight["key"], level=insight["level"], category=insight["category"],
            title=insight["title"],
            message=insight["observation"] + " " + insight["insight"],
            suggestion=insight["suggestion"],
            data={"observation": insight["observation"]},
            fingerprint=insight.get("fingerprint"),
        )

    _check_tasks()
    return frame


def _loop(stop_event):
    while not stop_event.is_set():
        try:
            if _settings["enabled"]:
                _sample()
        except Exception:
            pass        # the monitor must never take the server down
        stop_event.wait(INTERVAL)


def start():
    with _lock:
        thread = _monitor["thread"]
        if thread is not None and thread.is_alive():
            return False
        stop_event = threading.Event()
        thread = threading.Thread(target=_loop, args=(stop_event,),
                                  name="jarvish-monitor", daemon=True)
        _monitor.update({"thread": thread, "stop": stop_event, "started": time.time(),
                         "samples": 0})
        thread.start()
        return True


def stop():
    with _lock:
        if _monitor["stop"] is not None:
            _monitor["stop"].set()
        was = _monitor["thread"] is not None
        _monitor["thread"] = None
        return was


def monitor_status():
    with _lock:
        return ok(
            running=_monitor["thread"] is not None and _monitor["thread"].is_alive(),
            samples=_monitor["samples"],
            uptime=round(time.time() - _monitor["started"]) if _monitor["started"] else 0,
            interval=INTERVAL,
            settings=dict(_settings),
            live=len(_live()),
            suppressed=sum(1 for h in _history if h.get("suppressed")),
        )


def history(limit=40):
    with _lock:
        return ok(history=list(_history)[-as_int(limit, 40, 1, 200):])


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def tool_notifications(unseen_only=False):
    return notifications(include_seen=not as_bool(unseen_only))


def tool_insights():
    """What the monitor can see right now, whether or not it would notify."""
    found = evaluate()
    if not found:
        return ok(insights=[], summary="Nothing needs attention.")
    return ok(insights=found, count=len(found),
              summary="; ".join(i["observation"] for i in found))


SCHEMAS = [
    tool("system_insights",
         "Check whether anything about the machine needs attention right now — "
         "memory, CPU, disk, battery. Returns the observation, what it means and "
         "what could be done, without doing anything."),
    tool("notifications",
         "List the proactive notifications Jarvish has raised.",
         {"unseen_only": boolean("Only show ones the user has not seen yet.")}),
    tool("proactive_settings",
         "Change how and when Jarvish is allowed to interrupt: turn proactive "
         "intelligence on or off, set quiet mode, quiet hours, the minimum level, "
         "or switch a category off.",
         {"enabled": boolean("Turn proactive monitoring on or off entirely."),
          "quiet": boolean("Quiet mode: only critical notifications get through."),
          "category": string("A category to enable or disable.", list(CATEGORIES)),
          "on": boolean("Whether that category is enabled."),
          "min_level": string("Lowest level worth raising.", list(LEVELS)),
          "quiet_from": integer("Hour quiet hours begin, 0-23."),
          "quiet_to": integer("Hour quiet hours end, 0-23.")}),
    tool("dismiss_notifications",
         "Clear notifications that have been dealt with.",
         {"key": string("A specific notification key, or omit for all.")}),
]

REGISTRY = {
    "system_insights": tool_insights,
    "notifications": tool_notifications,
    "proactive_settings": configure,
    "dismiss_notifications": lambda key=None: dismiss(key),
}
