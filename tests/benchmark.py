"""Latency benchmark: where a Jarvish turn actually spends its time.

Run against a live server:  python tests/benchmark.py
Compare two runs:           python tests/benchmark.py --save before
                            python tests/benchmark.py --compare before

This is a measuring instrument, not a test — it asserts nothing and fails
nothing. Its job is to make "Jarvish feels slow" into a number attached to a
stage, because the interesting question is never the total. Raw Ollama answers
in about a second; anything beyond that is ours, and this says which part.

Every stage is timed separately and every request is run several times, because
a single sample on a machine with a background monitor and a task runner is
noise. Medians are reported, with the spread, so a regression has to be larger
than the jitter before it is believed.
"""

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8000"
RESULTS_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                           "Jarvish", "benchmarks")

# One per request class, so a change that helps chat but hurts tool routing
# cannot hide behind an average.
REQUESTS = [
    ("conversation", "Say hello"),
    ("knowledge", "What is recursion?"),
    ("realtime-tool", "What time is it?"),
    ("system-tool", "Get my system information"),
]


def ms(seconds):
    return round(seconds * 1000.0, 1)


def _median(values):
    return round(statistics.median(values), 1) if values else None


def _spread(values):
    if len(values) < 2:
        return 0.0
    return round(max(values) - min(values), 1)


# --------------------------------------------------------------------------
# Stage 1-4: everything that happens before Ollama is called at all
# --------------------------------------------------------------------------

def _preflight(message, repeats):
    """Time the work `run_agent` does before it opens the model stream.

    These stages run on every single turn, so a hundred milliseconds here is a
    hundred milliseconds added to every reply including "hello".
    """
    import asyncio
    from jarvish import capabilities, config, models, session as session_mod

    out = {k: [] for k in ("system_prompt", "continuity", "classify",
                           "route_agent", "capability_select")}
    sess = session_mod.get()

    for _ in range(repeats):
        t = time.perf_counter(); system = config.build_system_prompt()
        out["system_prompt"].append(ms(time.perf_counter() - t))

        t = time.perf_counter(); sess.continuity_block()
        out["continuity"].append(ms(time.perf_counter() - t))

        t = time.perf_counter(); task = models.classify(message)
        out["classify"].append(ms(time.perf_counter() - t))

        t = time.perf_counter(); asyncio.run(models.route_agent(task))
        out["route_agent"].append(ms(time.perf_counter() - t))

        t = time.perf_counter(); schemas, report = capabilities.select(message)
        out["capability_select"].append(ms(time.perf_counter() - t))

    sizes = {
        "system_prompt_chars": len(config.build_system_prompt()),
        "schema_count": len(schemas),
        "schema_chars": len(json.dumps(schemas)),
    }
    sizes["prompt_chars_total"] = sizes["system_prompt_chars"] + sizes["schema_chars"]
    sizes["approx_prompt_tokens"] = sizes["prompt_chars_total"] // 4
    return out, sizes


# --------------------------------------------------------------------------
# Raw model: the floor Jarvish cannot go below
# --------------------------------------------------------------------------

def _raw_ollama(message, model, schemas, repeats):
    """Time Ollama directly with the same prompt Jarvish would send.

    Two variants: with the tool schemas attached and without. The gap between
    them is what offering tools costs in prompt-processing time, which is the
    number that decides whether trimming the tool list is worth anything.
    """
    from jarvish import config

    system = config.build_system_prompt()
    results = {"with_tools": {"ttft": [], "total": []},
               "no_tools": {"ttft": [], "total": []}}

    for label, tools in (("with_tools", schemas), ("no_tools", None)):
        for _ in range(repeats):
            body = {
                "model": model, "stream": True,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": message}],
                "options": {"temperature": 0.7},
            }
            if tools:
                body["tools"] = tools
            if "qwen3" in model or "deepseek-r1" in model:
                body["think"] = False

            data = json.dumps(body).encode()
            request = urllib.request.Request(
                "http://localhost:11434/api/chat", data=data,
                headers={"Content-Type": "application/json"})
            start = time.perf_counter()
            first = None
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    for line in response:
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue
                        content = (chunk.get("message") or {}).get("content") or ""
                        calls = (chunk.get("message") or {}).get("tool_calls")
                        if first is None and (content or calls):
                            first = time.perf_counter()
                        if chunk.get("done"):
                            break
            except Exception as exc:
                print("    raw ollama failed: " + str(exc)[:90])
                break
            end = time.perf_counter()
            results[label]["ttft"].append(ms((first or end) - start))
            results[label]["total"].append(ms(end - start))
    return results


# --------------------------------------------------------------------------
# End to end, exactly as the HUD sees it
# --------------------------------------------------------------------------

def _end_to_end(message, repeats):
    """Stream /api/chat and timestamp every stage boundary the server emits.

    Time-to-first-token is measured from the moment the request leaves, not
    from when the model starts, because that is what the person waiting
    actually experiences.
    """
    runs = []
    for _ in range(repeats):
        body = json.dumps({"messages": [{"role": "user", "content": message}]}).encode()
        request = urllib.request.Request(
            BASE + "/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        marks = {}
        tools = []
        text = ""
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                for line in response:
                    if not line.startswith(b"data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except Exception:
                        continue
                    kind = event.get("type")
                    now = ms(time.perf_counter() - start)
                    if kind == "capabilities":
                        marks["capabilities_emitted"] = now
                        marks["tools_offered"] = event.get("count")
                    elif kind == "state":
                        marks.setdefault("state_" + str(event.get("state")), now)
                    elif kind == "plan":
                        marks.setdefault("plan", now)
                    elif kind == "tool_start":
                        marks.setdefault("tool_start", now)
                    elif kind == "tool_end":
                        marks["tool_end"] = now
                        tools.append(event.get("name"))
                    elif kind == "token":
                        marks.setdefault("first_token", now)
                        text += event.get("text") or event.get("token") or ""
                    elif kind == "done":
                        marks["done"] = now
        except Exception as exc:
            print("    end-to-end failed: " + str(exc)[:90])
            continue
        marks["_tools"] = tools
        marks["_chars"] = len(text)
        runs.append(marks)
    return runs


def _collect(runs, key):
    return [r[key] for r in runs if isinstance(r.get(key), (int, float))]


# --------------------------------------------------------------------------

def run(repeats=3, quick=False):
    from jarvish import capabilities
    from jarvish.config import MODEL

    print("Jarvish latency benchmark")
    print("=" * 66)

    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=20) as r:
            health = json.loads(r.read())
        model = health.get("model", MODEL)
        print("server   : up, model %s, %s tools" % (model, health.get("tool_count")))
    except Exception as exc:
        print("No server on :8000 - start one with 'python new.py --no-browser'")
        print("  " + str(exc)[:80])
        return None

    hardware = _hardware()
    print("hardware : RAM %.1f GB free | VRAM %s" % (hardware["ram_free_gb"],
                                                     hardware["vram"]))
    print("repeats  : %d per request" % repeats)
    print()

    report = {"model": model, "repeats": repeats, "hardware": hardware,
              "at": time.time(), "requests": {}}

    for label, message in REQUESTS:
        print("-" * 66)
        print("%s  |  %r" % (label.upper(), message))

        pre, sizes = _preflight(message, repeats)
        schemas, _ = capabilities.select(message)

        print("  pre-model stages (run on every single turn):")
        pre_total = 0.0
        for stage in ("system_prompt", "continuity", "classify",
                      "route_agent", "capability_select"):
            med = _median(pre[stage])
            pre_total += med or 0
            print("    %-20s %8.1f ms   (spread %.1f)"
                  % (stage, med or 0, _spread(pre[stage])))
        print("    %-20s %8.1f ms" % ("PRE-MODEL TOTAL", pre_total))

        print("  prompt sent to the model:")
        print("    %-20s %8d chars" % ("system prompt", sizes["system_prompt_chars"]))
        print("    %-20s %8d chars (%d schemas)"
              % ("tool schemas", sizes["schema_chars"], sizes["schema_count"]))
        print("    %-20s %8d tokens (approx)" % ("TOTAL", sizes["approx_prompt_tokens"]))

        raw = None
        if not quick:
            raw = _raw_ollama(message, model, schemas, max(1, repeats - 1))
            wt, nt = raw["with_tools"], raw["no_tools"]
            print("  raw ollama (same prompt, no Jarvish):")
            print("    %-20s %8s ms ttft / %s ms total"
                  % ("with tool schemas", _median(wt["ttft"]), _median(wt["total"])))
            print("    %-20s %8s ms ttft / %s ms total"
                  % ("without tools", _median(nt["ttft"]), _median(nt["total"])))
            if _median(wt["total"]) and _median(nt["total"]):
                print("    %-20s %8.1f ms" % ("cost of offering tools",
                                              _median(wt["total"]) - _median(nt["total"])))

        runs = _end_to_end(message, repeats)
        if not runs:
            print("  end-to-end: FAILED")
            continue

        print("  end to end through Jarvish:")
        for key, name in (("capabilities_emitted", "capabilities picked"),
                          ("state_analyzing", "analysing began"),
                          ("plan", "plan emitted"),
                          ("tool_start", "tool started"),
                          ("tool_end", "tool finished"),
                          ("first_token", "FIRST TOKEN"),
                          ("done", "TOTAL")):
            values = _collect(runs, key)
            if values:
                print("    %-20s %8.1f ms   (spread %.1f)"
                      % (name, _median(values), _spread(values)))
        tools_used = runs[0].get("_tools") or []
        print("    %-20s %s" % ("tools called", tools_used or "none"))

        first = _median(_collect(runs, "first_token"))
        total = _median(_collect(runs, "done"))
        if raw and first:
            raw_ttft = _median(raw["with_tools"]["ttft"])
            if raw_ttft:
                print("    %-20s %8.1f ms  <-- Jarvish overhead before the model speaks"
                      % ("OVERHEAD vs raw", first - raw_ttft))

        report["requests"][label] = {
            "message": message, "pre_model": pre, "pre_model_total_ms": pre_total,
            "sizes": sizes, "raw": raw,
            "first_token_ms": first, "total_ms": total,
            "tools": tools_used,
        }
        print()

    print("=" * 66)
    print("SUMMARY                first token      total     tools")
    for label, _ in REQUESTS:
        entry = report["requests"].get(label)
        if not entry:
            continue
        print("  %-18s %10s ms %10s ms   %s"
              % (label, entry["first_token_ms"], entry["total_ms"],
                 entry["tools"] or "none"))
    return report


def _hardware():
    info = {"ram_free_gb": 0.0, "vram": "n/a"}
    try:
        import psutil
        info["ram_free_gb"] = round(psutil.virtual_memory().available / 2 ** 30, 1)
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.free",
                              "--format=csv,noheader"], capture_output=True,
                             text=True, timeout=8)
        if out.returncode == 0:
            info["vram"] = out.stdout.strip()
    except Exception:
        pass
    return info


def _save(report, name):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, name + ".json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print("\nsaved: " + path)


def _compare(report, name):
    path = os.path.join(RESULTS_DIR, name + ".json")
    if not os.path.isfile(path):
        print("\nNo saved run called '" + name + "' in " + RESULTS_DIR)
        return
    with open(path, encoding="utf-8") as handle:
        old = json.load(handle)

    print("\n" + "=" * 66)
    print("BEFORE (%s)  vs  NOW" % name)
    print("%-18s %14s %14s %12s" % ("request", "first token", "total", "change"))
    for label, _ in REQUESTS:
        a = (old.get("requests") or {}).get(label)
        b = (report.get("requests") or {}).get(label)
        if not a or not b:
            continue
        before, after = a.get("total_ms"), b.get("total_ms")
        fb, fa = a.get("first_token_ms"), b.get("first_token_ms")
        if before and after:
            delta = (after - before) / before * 100.0
            print("  %-16s %6s -> %-6s %6s -> %-6s  %+7.1f%%"
                  % (label, fb, fa, before, after, delta))


def main():
    parser = argparse.ArgumentParser(description="Jarvish latency benchmark")
    parser.add_argument("--repeats", type=int, default=3,
                        help="samples per request (default 3)")
    parser.add_argument("--quick", action="store_true",
                        help="skip the raw-Ollama comparison")
    parser.add_argument("--save", metavar="NAME", help="store this run under NAME")
    parser.add_argument("--compare", metavar="NAME", help="diff against a stored run")
    args = parser.parse_args()

    report = run(repeats=args.repeats, quick=args.quick)
    if report is None:
        sys.exit(1)
    if args.save:
        _save(report, args.save)
    if args.compare:
        _compare(report, args.compare)


if __name__ == "__main__":
    main()
