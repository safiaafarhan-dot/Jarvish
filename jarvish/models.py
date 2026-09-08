"""Model inventory and routing.

Jarvish does not assume one model can do everything. Ollama reports what each
pulled model can actually do — `tools`, `vision`, `thinking` — so the router
works from facts rather than guessing from names.

Two decisions are made here, and they are separate:

* the **agent model** drives the tool-calling loop, so it *must* support tools;
* the **vision model** answers one-shot questions about an image, so it *must*
  support vision.

When the model a task wants is not installed, the route comes back marked
`degraded` with the reason attached. Nothing downstream is allowed to pretend
the missing capability was there — `llm` and `vision` both surface it.
"""

import asyncio
import re
import threading
import time

import httpx

from .config import (CODE_MODEL, FAST_CHAT, MODEL, OLLAMA_HOST, ROUTING_ENABLED,
                     VISION_MODEL)

# The inventory costs one /api/show per pulled model, and it is taken on the
# request path — nothing streams until routing has picked a model. Two things
# keep it off the critical path: the per-model calls go out together rather
# than one after another, and the answer is held for long enough that a normal
# session takes it once. Pulling a model is a deliberate act, so a ten-minute
# window cannot hand back a stale answer the user did not cause; `force=True`
# and the /api/models endpoint both bypass it when the HUD wants the truth now.
CACHE_TTL = 600.0
_cache = {"at": 0.0, "models": {}, "error": None}

# The cache is read and written from the HUD's event loop and from the voice
# thread's own short-lived loop. A plain threading lock is what makes that safe;
# an asyncio lock would bind itself to whichever loop touched it first and blow
# up on the other. Nothing is awaited while it is held.
_cache_lock = threading.Lock()

# Families worth preferring for a given kind of work, best first. Matched as a
# substring of the model name, so "qwen2.5-coder:7b" matches "qwen2.5-coder".
CODE_FAMILIES = ("qwen2.5-coder", "qwen3-coder", "deepseek-coder", "codestral",
                 "codellama", "starcoder", "granite-code")
REASONING_FAMILIES = ("qwen3", "deepseek-r1", "magistral", "gpt-oss",
                      "phi4-reasoning", "exaone-deep")

# Vision models that are small enough to be worth suggesting on a laptop.
SUGGESTED_VISION = (
    ("moondream", "1.7 GB", "tiny, fast, good for reading screens"),
    ("llava:7b", "4.7 GB", "solid general screen and image understanding"),
    ("llama3.2-vision:11b", "7.9 GB", "strongest of the three, needs the most RAM"),
)


class Route:
    """The outcome of a routing decision."""

    def __init__(self, model, task, reason, degraded=False, missing=None,
                 explanation=None, considered=()):
        self.model = model
        self.task = task
        self.reason = reason
        self.degraded = degraded
        self.missing = missing
        self.explanation = explanation
        self.considered = list(considered)

    def as_dict(self):
        return {
            "model": self.model,
            "task": self.task,
            "reason": self.reason,
            "degraded": self.degraded,
            "missing": self.missing,
            "explanation": self.explanation,
            "considered": self.considered,
        }

    def __repr__(self):
        return "<Route " + str(self.model) + " for " + self.task + \
               (" DEGRADED" if self.degraded else "") + ">"


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------

async def inventory(force=False):
    """Every pulled model with the capabilities Ollama reports for it.

    Returns {name: {tools, vision, thinking, params, family, size_gb}}. An
    empty dict means Ollama is unreachable; `_cache["error"]` says why.
    """
    now = time.time()
    if not force:
        with _cache_lock:
            if _cache["models"] and now - _cache["at"] < CACHE_TTL:
                return _cache["models"]

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            tags = await client.get(OLLAMA_HOST + "/api/tags")
            tags.raise_for_status()
            listed = tags.json().get("models", [])
            names = [m.get("name", "") for m in listed if m.get("name")]
            sizes = {m.get("name"): m.get("size", 0) for m in listed}

            async def describe(name):
                """What Ollama says this one model can do."""
                entry = {
                    "name": name,
                    "size_gb": round(sizes.get(name, 0) / 1e9, 1),
                    "tools": False, "vision": False, "thinking": False,
                    "params": None, "family": None,
                }
                try:
                    shown = await client.post(OLLAMA_HOST + "/api/show",
                                              json={"model": name})
                    shown.raise_for_status()
                    body = shown.json()
                    reported = set(body.get("capabilities") or ())
                    entry["tools"] = "tools" in reported
                    entry["vision"] = "vision" in reported
                    entry["thinking"] = "thinking" in reported
                    details = body.get("details") or {}
                    entry["params"] = details.get("parameter_size")
                    entry["family"] = details.get("family")
                except Exception:
                    # An older Ollama may not implement /api/show capabilities.
                    # Leave the flags false rather than inventing them.
                    pass
                return entry

            # Together, not one after another. These are independent reads of a
            # server that is about to be asked for a whole answer; making the
            # user wait through them serially adds the sum of every model's
            # latency to the silence before the first token.
            found = {entry["name"]: entry
                     for entry in await asyncio.gather(*(describe(n) for n in names))}
    except Exception as exc:
        # Keep whatever the last good sweep found. A momentary failure should
        # not make Jarvish forget which models can use tools and fall back to a
        # degraded route it does not need.
        with _cache_lock:
            _cache["error"] = str(exc)
            return _cache["models"] or {}

    with _cache_lock:
        _cache.update({"at": now, "models": found, "error": None})
    return found


def cached_models():
    """Whatever the last inventory found, without going to the network."""
    return _cache["models"]


def _matches(name, configured):
    """Ollama tags carry a :tag suffix; match the bare name or the full one."""
    if not configured:
        return False
    return name == configured or name.split(":")[0] == str(configured).split(":")[0]


def _prefer(models, families):
    """First model whose name contains one of these families, best first."""
    for family in families:
        for name in models:
            if family in name.lower():
                return name
    return None


def _smallest(models, catalogue):
    """The lightest model, for work where latency matters more than depth."""
    if not models:
        return None
    return min(models, key=lambda name: catalogue.get(name, {}).get("size_gb") or 99)


# --------------------------------------------------------------------------
# Task classification
# --------------------------------------------------------------------------

_VISION_HINTS = re.compile(
    r"\b(screen|screenshot|see this|look at|looking at|what am i|on my display|"
    r"this image|this picture|read this|what'?s wrong|this error|visible|"
    r"what does (this|it) say)\b", re.IGNORECASE)

_CODE_HINTS = re.compile(
    r"\b(code|function|class|bug|traceback|stack ?trace|exception|compile|"
    r"refactor|unit test|repository|repo|git |commit|import error|syntax)\b",
    re.IGNORECASE)

_HARD_HINTS = re.compile(
    r"\b(plan|analys|analyz|compare|why|explain|design|architect|strategy|"
    r"step by step|work out|figure out|diagnose)\b", re.IGNORECASE)


def classify(text, has_image=False):
    """What kind of work the latest user message implies."""
    body = str(text or "")
    if has_image:
        return "vision"
    if _VISION_HINTS.search(body):
        return "vision"
    if _CODE_HINTS.search(body):
        return "code"
    if _HARD_HINTS.search(body) or len(body) > 260:
        return "reasoning"
    return "chat"


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

async def route_agent(task="chat"):
    """Pick the model that will drive the tool-calling loop.

    Tool support is non-negotiable here — without it the agent cannot call
    anything, so a model lacking it is never chosen even if it is configured.
    """
    catalogue = await inventory()

    if not catalogue:
        return Route(MODEL, task, "Ollama unreachable; using the configured default",
                     degraded=True, missing="inventory",
                     explanation=_cache["error"] or "Could not list models.")

    configured = next((name for name in catalogue if _matches(name, MODEL)), None)
    capable = [name for name, info in catalogue.items() if info["tools"]]

    if not capable:
        return Route(MODEL, task,
                     "No pulled model reports tool support",
                     degraded=True, missing="tools",
                     explanation="Jarvish can talk but cannot use any of its tools. "
                                 "Pull a tool-capable model, for example "
                                 "`ollama pull qwen3:8b`.",
                     considered=list(catalogue))

    # Routing off, or the configured model is fine: use it and say so.
    if not ROUTING_ENABLED:
        return Route(configured or MODEL, task, "Routing disabled; using JARVISH_MODEL")

    chosen = None
    reason = ""

    if task == "code":
        explicit = next((name for name in capable if _matches(name, CODE_MODEL)), None)
        chosen = explicit or _prefer(capable, CODE_FAMILIES)
        if chosen:
            reason = "Coding task, and " + chosen + " is a code-specialised model"
    elif task == "reasoning":
        chosen = _prefer(capable, REASONING_FAMILIES)
        if chosen:
            reason = "Multi-step request, routed to a reasoning model"
    elif task == "chat" and FAST_CHAT and configured in capable:
        # Dropping to a smaller model for short messages looks like a free
        # latency win, and is not: "what time is it?" is short *and* needs a
        # tool, and a 3B model picks badly from a large tool list. Selecting
        # the right tool matters more here than a second of latency, so this
        # is off unless JARVISH_FAST_CHAT is set.
        light = _smallest(capable, catalogue)
        big = catalogue.get(configured, {}).get("size_gb") or 0
        small = catalogue.get(light, {}).get("size_gb") or 0
        if light and light != configured and big - small >= 2.0:
            chosen, reason = light, "Short exchange, routed to the lighter model for latency"

    if not chosen:
        chosen = configured if configured in capable else capable[0]
        reason = ("Using the configured model" if chosen == configured
                  else "Configured model is unavailable or lacks tool support")

    return Route(chosen, task, reason, considered=capable)


async def route_vision():
    """Pick a model that can actually look at an image.

    There is no substitute for this capability, so when nothing supports vision
    the route comes back degraded and the caller must say so out loud.
    """
    catalogue = await inventory()

    if not catalogue:
        return Route(None, "vision", "Ollama unreachable", degraded=True,
                     missing="inventory",
                     explanation=_cache["error"] or "Could not list models.")

    capable = [name for name, info in catalogue.items() if info["vision"]]

    if not capable:
        suggestions = ", ".join("`ollama pull " + name + "` (" + size + ")"
                                for name, size, _ in SUGGESTED_VISION)
        return Route(None, "vision", "No pulled model supports vision",
                     degraded=True, missing="vision",
                     explanation="No vision-capable model is installed, so the image "
                                 "itself cannot be interpreted. Jarvish can still read "
                                 "the screen's text and interface structure. To enable "
                                 "true image understanding: " + suggestions,
                     considered=list(catalogue))

    explicit = next((name for name in capable if _matches(name, VISION_MODEL)), None)
    chosen = explicit or _prefer(capable, ("llama3.2-vision", "llava", "moondream",
                                           "minicpm-v", "qwen2.5vl", "gemma3")) or capable[0]
    return Route(chosen, "vision",
                 "Vision request routed to " + chosen, considered=capable)


async def overview():
    """Everything the HUD's model panel needs."""
    catalogue = await inventory()
    agent = await route_agent("chat")
    vision = await route_vision()
    return {
        "models": catalogue,
        "count": len(catalogue),
        "routing": ROUTING_ENABLED,
        "default": MODEL,
        "agent": agent.as_dict(),
        "vision": vision.as_dict(),
        "tool_capable": sorted(n for n, i in catalogue.items() if i["tools"]),
        "vision_capable": sorted(n for n, i in catalogue.items() if i["vision"]),
        "suggested_vision": [
            {"name": n, "size": s, "note": note} for n, s, note in SUGGESTED_VISION
        ],
        "error": _cache["error"],
    }
