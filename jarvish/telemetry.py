"""Live system telemetry for the HUD, plus the insights drawn from it.

The interface streams this once a second so the readouts move on their own.
Everything here has to be cheap: `psutil.cpu_percent` is called non-blocking
against a module-level baseline rather than sleeping for an interval.
"""

import shutil
import time

import psutil

from .util import IS_WINDOWS

# Prime the CPU counter so the first real read is not a meaningless 0.0.
psutil.cpu_percent(interval=None)

_net_baseline = {"at": time.time(), "sent": 0, "recv": 0}


def _network_rates():
    """Bytes per second since the previous sample."""
    global _net_baseline
    try:
        counters = psutil.net_io_counters()
    except Exception:
        return 0.0, 0.0

    now = time.time()
    elapsed = max(now - _net_baseline["at"], 0.001)
    up = max(counters.bytes_sent - _net_baseline["sent"], 0) / elapsed
    down = max(counters.bytes_recv - _net_baseline["recv"], 0) / elapsed

    first_sample = _net_baseline["sent"] == 0 and _net_baseline["recv"] == 0
    _net_baseline = {"at": now, "sent": counters.bytes_sent, "recv": counters.bytes_recv}
    return (0.0, 0.0) if first_sample else (up, down)


def _battery():
    try:
        battery = psutil.sensors_battery()
    except Exception:
        return None
    if battery is None:
        return None
    return {
        "percent": round(battery.percent),
        "plugged": bool(battery.power_plugged),
        "minutes": (
            None if battery.secsleft in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN)
            else round(battery.secsleft / 60)
        ),
    }


def _temperature():
    """Only some machines expose this, and almost no Windows ones do."""
    try:
        readings = psutil.sensors_temperatures()
    except (AttributeError, Exception):
        return None
    for entries in (readings or {}).values():
        for entry in entries:
            if entry.current:
                return round(entry.current)
    return None


def snapshot():
    """One frame of telemetry."""
    memory = psutil.virtual_memory()
    try:
        disk = shutil.disk_usage("C:\\" if IS_WINDOWS else "/")
        disk_percent = round(disk.used / disk.total * 100, 1)
        disk_free_gb = round(disk.free / 1024 ** 3, 1)
    except Exception:
        disk_percent, disk_free_gb = 0.0, 0.0

    up, down = _network_rates()

    return {
        "at": time.time(),
        "cpu": round(psutil.cpu_percent(interval=None), 1),
        "cpu_cores": psutil.cpu_count(logical=True) or 1,
        "memory": round(memory.percent, 1),
        "memory_used_gb": round(memory.used / 1024 ** 3, 1),
        "memory_total_gb": round(memory.total / 1024 ** 3, 1),
        "disk": disk_percent,
        "disk_free_gb": disk_free_gb,
        "net_up": round(up),
        "net_down": round(down),
        "processes": len(psutil.pids()),
        "battery": _battery(),
        "temperature": _temperature(),
        "uptime": round(time.time() - psutil.boot_time()),
    }


# Thresholds that turn a number into something worth saying out loud.
_RULES = (
    ("cpu", 90, "critical", "CPU is saturated at {value}%."),
    ("cpu", 75, "warning", "CPU load is high — {value}%."),
    ("memory", 92, "critical", "Memory is nearly exhausted at {value}%."),
    ("memory", 80, "warning", "Memory usage is climbing — {value}%."),
    ("disk", 95, "critical", "Storage is critically full — {value}% used."),
    ("disk", 88, "warning", "Storage is filling up — {value}% used."),
)


def insights(frame=None):
    """Plain-language observations about the current frame, worst first."""
    frame = frame or snapshot()
    found = []
    seen = set()

    for metric, threshold, severity, template in _RULES:
        if metric in seen:
            continue
        value = frame.get(metric)
        if isinstance(value, (int, float)) and value >= threshold:
            found.append({
                "metric": metric,
                "severity": severity,
                "text": template.format(value=value),
            })
            seen.add(metric)

    battery = frame.get("battery")
    if battery and not battery["plugged"]:
        if battery["percent"] <= 10:
            found.append({"metric": "battery", "severity": "critical",
                          "text": "Battery is at " + str(battery["percent"]) + "% and unplugged."})
        elif battery["percent"] <= 20:
            found.append({"metric": "battery", "severity": "warning",
                          "text": "Battery is low — " + str(battery["percent"]) + "%."})

    order = {"critical": 0, "warning": 1}
    found.sort(key=lambda item: order.get(item["severity"], 2))
    return found


# Walking every process is by far the most expensive thing this module does —
# measured at ~2.3 s on a machine with 350 processes. Running it on a short
# interval was costing roughly a quarter of a core continuously, which raised
# memory pressure, which triggered more scans. It is cached hard.
PROCESS_CACHE_TTL = 60.0
_process_cache = {"at": 0.0, "rows": []}


def top_processes(limit=5, force=False):
    """The heaviest processes by memory. Cached: the scan is expensive.

    `memory_info().rss` is read directly and divided by total RAM once, rather
    than asking psutil for `memory_percent` per process — same answer, one
    system call less per process.
    """
    now = time.time()
    if not force and _process_cache["rows"] and \
            now - _process_cache["at"] < PROCESS_CACHE_TTL:
        return _process_cache["rows"][:limit]

    try:
        total = psutil.virtual_memory().total or 1
    except Exception:
        return _process_cache["rows"][:limit]

    rows = []
    for process in psutil.process_iter(["name", "memory_info"]):
        try:
            info = process.info
            memory = info.get("memory_info")
            if memory and memory.rss:
                rows.append({
                    "name": info.get("name") or "?",
                    "memory": round(memory.rss / total * 100, 1),
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda row: row["memory"], reverse=True)
    _process_cache.update({"at": now, "rows": rows[:20]})
    return rows[:limit]
