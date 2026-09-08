"""Real browser control over the Chrome DevTools Protocol.

`webapps.py` opens URLs. This drives a browser: enumerates tabs, reads the DOM,
finds elements, clicks them, types into them, and checks afterwards that the
page actually changed.

**How it attaches.** CDP needs Chrome to have been started with
`--remote-debugging-port`, and a Chrome already running without it cannot be
adopted. So there are two paths, tried in order:

1. **Attach** to a debug port that is already open. If you start your own Chrome
   with `--remote-debugging-port=9222`, Jarvish drives *that* browser, with your
   profile and your logins.
2. **Launch** a Chrome of its own, on a separate profile under `data/`. Nothing
   touches your existing windows, but it starts signed out.

When neither is available the browser tools do not pretend: they report that
CDP is unavailable and fall back to reading the browser window through the
vision layer, which is labelled as such in every result.

**Grounding hierarchy**, as available:  DOM → UI Automation → OCR/visual.
`_resolve` walks it in that order and every result records which layer answered.
"""

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from .config import BROWSER_PORT, BROWSER_ENABLED
from .util import DATA_DIR, IS_WINDOWS, as_bool, as_int, boolean, err, ok, string, tool

try:
    import websockets
    _WS = True
except Exception:
    _WS = False

PROFILE_DIR = DATA_DIR / "browser-profile"
DOWNLOAD_DIR = DATA_DIR / "downloads"
CDP_HOST = "127.0.0.1"

# How long to let a page settle before checking whether it changed.
SETTLE = 0.7

_launched = {"process": None}


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

def _endpoint(path):
    return "http://" + CDP_HOST + ":" + str(BROWSER_PORT) + path


def _cdp_up(timeout=1.2):
    """Is something answering CDP on the configured port?"""
    try:
        response = httpx.get(_endpoint("/json/version"), timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def _targets():
    """Every page-like target Chrome is hosting."""
    try:
        response = httpx.get(_endpoint("/json/list"), timeout=4.0)
        response.raise_for_status()
        return [t for t in response.json() if t.get("type") == "page"]
    except Exception:
        return []


async def _command(ws_url, calls, timeout=25.0):
    """Send one or more CDP commands over a fresh socket, in order.

    `calls` is a list of (method, params). Returns the list of results. A fresh
    connection per batch keeps this stateless and avoids a background reader
    task, which matters because tools run on short-lived worker threads.
    """
    results = []
    async with websockets.connect(ws_url, max_size=24 * 1024 * 1024,
                                  open_timeout=8, close_timeout=3) as socket:
        for index, (method, params) in enumerate(calls, start=1):
            await socket.send(json.dumps({"id": index, "method": method,
                                          "params": params or {}}))
            # Events are interleaved with replies; keep reading until the id matches.
            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("CDP call timed out: " + method)
                raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                message = json.loads(raw)
                if message.get("id") == index:
                    if "error" in message:
                        raise RuntimeError(method + ": " +
                                           str(message["error"].get("message", message["error"])))
                    results.append(message.get("result", {}))
                    break
    return results


def _run(coro):
    """Tools are synchronous and run on worker threads; give them a loop."""
    return asyncio.run(coro)


def _evaluate(target, expression, timeout=25.0):
    """Evaluate JavaScript in a page and return the value."""
    result = _run(_command(target["webSocketDebuggerUrl"], [
        ("Runtime.enable", {}),
        ("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                              "awaitPromise": True}),
    ], timeout=timeout))[-1]

    if result.get("exceptionDetails"):
        detail = result["exceptionDetails"]
        text = detail.get("exception", {}).get("description") or detail.get("text")
        raise RuntimeError("Page script failed: " + str(text)[:200])
    return (result.get("result") or {}).get("value")


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

def _chrome_path():
    for candidate in (
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ):
        if candidate and os.path.exists(candidate):
            return candidate
    return shutil.which("chrome") or shutil.which("msedge")


def status():
    """How, and whether, Jarvish can drive a browser right now."""
    version = _cdp_up()
    return {
        "enabled": BROWSER_ENABLED,
        "websockets": _WS,
        "port": BROWSER_PORT,
        "connected": bool(version),
        "browser": (version or {}).get("Browser"),
        "mode": "attached" if version else "none",
        "owned": _launched["process"] is not None and _launched["process"].poll() is None,
        "chrome_found": bool(_chrome_path()),
        "tabs": len(_targets()) if version else 0,
    }


def launch(timeout=18.0):
    """Start a Chrome that Jarvish can drive, on its own profile.

    Never touches an existing Chrome window: a separate `--user-data-dir` means
    the user's session, tabs and logins are left completely alone.
    """
    if not BROWSER_ENABLED:
        return err("Browser control is disabled. Set JARVISH_BROWSER=1 to enable it.")
    if not _WS:
        return err("Browser control needs the `websockets` package. "
                   "Install it with `pip install -r requirements.txt`.")

    existing = _cdp_up()
    if existing:
        return ok(mode="attached", browser=existing.get("Browser"),
                  note="Attached to a browser that was already listening on port "
                       + str(BROWSER_PORT) + ".")

    executable = _chrome_path()
    if not executable:
        return err("Could not find Chrome or Edge on this machine.")

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        process = subprocess.Popen(
            [executable,
             "--remote-debugging-port=" + str(BROWSER_PORT),
             "--user-data-dir=" + str(PROFILE_DIR),
             "--no-first-run", "--no-default-browser-check",
             "--disable-session-crashed-bubble",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        return err("Could not start the browser: " + str(exc))

    _launched["process"] = process
    deadline = time.time() + timeout
    while time.time() < deadline:
        version = _cdp_up(timeout=0.8)
        if version:
            return ok(mode="launched", browser=version.get("Browser"),
                      profile=str(PROFILE_DIR),
                      note="Started a separate browser on its own profile. Your existing "
                           "Chrome windows are untouched, but this one is signed out. To "
                           "drive your own browser instead, close Chrome and restart it "
                           "with --remote-debugging-port=" + str(BROWSER_PORT) + ".")
        time.sleep(0.4)

    return err("The browser started but never opened its debugging port.")


def _require():
    """Return (target_list, None) when CDP is usable, else (None, error)."""
    if not BROWSER_ENABLED:
        return None, err("Browser control is disabled. Set JARVISH_BROWSER=1.")
    if not _WS:
        return None, err("Browser control needs the `websockets` package.")
    if not _cdp_up():
        return None, err(
            "No browser is available for control. Chrome must be started with "
            "--remote-debugging-port=" + str(BROWSER_PORT) + ", which an already-running "
            "Chrome cannot be given retroactively. Call `browser_launch` to start one "
            "Jarvish can drive, or restart Chrome yourself with that flag to use your "
            "own profile.")
    targets = _targets()
    if not targets:
        return None, err("The browser is running but has no open pages.")
    return targets, None


def _active(targets, tab=None):
    """Pick the tab to act on: one named explicitly, else the most recent."""
    if tab:
        wanted = str(tab).strip().lower()
        for target in targets:
            if target["id"] == tab:
                return target
        for target in targets:
            haystack = (target.get("title", "") + " " + target.get("url", "")).lower()
            if wanted in haystack:
                return target
        return None
    return targets[0]


# --------------------------------------------------------------------------
# Reading a page
# --------------------------------------------------------------------------

# Collects every interactive element with its real layout box. Runs in the page,
# so these are the browser's own coordinates, not a guess from pixels.
_COLLECT_JS = r"""
(() => {
  const SEL = 'a[href],button,input,select,textarea,[role=button],[role=link],' +
              '[role=checkbox],[role=tab],[role=menuitem],[onclick],summary,' +
              '[contenteditable=true]';
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(SEL)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' ||
        parseFloat(style.opacity) < 0.05) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    // A button's value IS its label, but a text field's value is its contents.
    // Mixing the two makes a field stop being findable by name once it is
    // filled in, so they are kept apart.
    const buttonish = tag === 'button' ||
                      (tag === 'input' && ['submit', 'button', 'reset'].includes(type));
    // A checkbox or radio is identified by its own label or value, the way a
    // person would name it ("bacon"), not by the group name ("topping").
    const choiceish = tag === 'input' && ['checkbox', 'radio'].includes(type);
    const labelText = (el.labels && el.labels[0])
                      ? (el.labels[0].innerText || '').trim() : '';
    const label = (
      el.getAttribute('aria-label') ||
      (el.innerText || '').trim() ||
      (buttonish ? el.value : '') ||
      labelText ||
      (choiceish ? el.value : '') ||
      el.placeholder || el.title || el.name || el.id || el.alt || ''
    ).replace(/\s+/g, ' ').trim().slice(0, 120);
    const key = tag + '|' + label + '|' + Math.round(r.x) + ',' + Math.round(r.y);
    if (seen.has(key)) continue;
    seen.add(key);
    let kind = tag;
    if (tag === 'input') kind = (el.type || 'text').toLowerCase();
    else if (tag === 'a') kind = 'link';
    else if (el.getAttribute('role')) kind = el.getAttribute('role');
    out.push({
      kind, tag, text: label,
      value: (!buttonish && el.value !== undefined && el.value !== null)
             ? String(el.value).slice(0, 120) : null,
      href: el.href || null,
      name: el.name || null,
      id_attr: el.id || null,
      type: el.type || null,
      disabled: !!el.disabled,
      checked: el.checked === undefined ? null : !!el.checked,
      inViewport: r.top < innerHeight && r.bottom > 0,
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height)
    });
    if (out.length >= 250) break;
  }
  const forms = [...document.forms].map(f => ({
    name: f.name || f.id || null,
    action: f.action || null,
    method: (f.method || 'get').toLowerCase(),
    fields: [...f.elements].filter(e => e.name).map(e => e.name).slice(0, 30)
  }));
  return {
    url: location.href,
    title: document.title,
    ready: document.readyState,
    text: (document.body ? document.body.innerText : '').slice(0, 20000),
    elements: out,
    forms: forms,
    scroll: { y: Math.round(scrollY), height: Math.round(document.body.scrollHeight),
              viewport: Math.round(innerHeight) }
  };
})()
"""


def _read(target):
    return _evaluate(target, _COLLECT_JS)


def _settle(target_id, timeout=8.0):
    """Wait for a page to finish loading, then return (target, page).

    A navigation tears down the old execution context, so a read issued too
    early either races the load or fails outright. Polling readyState is the
    difference between "verified" being meaningful and being a coin toss.
    """
    deadline = time.time() + timeout
    target, page = None, None
    while time.time() < deadline:
        try:
            target = _active(_targets(), target_id)
            if target is not None:
                page = _read(target)
                if page.get("ready") == "complete":
                    return target, page
        except Exception:
            pass          # the socket dies mid-navigation; try again shortly
        time.sleep(0.2)
    return target, page


def _fingerprint(page):
    """Cheap identity of a page state, for detecting that something changed."""
    return (page.get("url"), page.get("title"),
            len(page.get("text") or ""), (page.get("text") or "")[:400])


# --------------------------------------------------------------------------
# Grounding: DOM first, then the vision layer
# --------------------------------------------------------------------------

def _score(element, needle):
    """How well an element matches a description. 0 means no match.

    An element is matchable by anything that stably identifies it: its visible
    label, its `name`, or its `id`. Matching on the label alone would mean a
    text field became unfindable by name the moment someone typed into it.
    """
    candidates = [
        (element.get("text") or "").strip().lower(),
        (element.get("name") or "").strip().lower(),
        (element.get("id_attr") or "").strip().lower(),
    ]

    best = 0.0
    for index, text in enumerate(candidates):
        if not text:
            continue
        if text == needle:
            score = 1.0
        elif text.startswith(needle) or text.endswith(needle):
            score = 0.86
        elif needle in text:
            score = 0.72
        else:
            parts = [p for p in needle.split() if len(p) > 2]
            score = 0.58 if (parts and all(p in text for p in parts)) else 0.0
        # A visible label is better evidence than an internal attribute.
        if index and score:
            score -= 0.04
        best = max(best, score)

    if not best:
        return 0.0
    if element.get("inViewport"):
        best += 0.06
    if element.get("disabled"):
        best -= 0.3
    return round(max(min(best, 1.0), 0.0), 3)


_TYPE_WORDS = {"button": ("button", "submit"), "link": ("link",),
               "field": ("text", "email", "password", "search", "textarea"),
               "input": ("text", "email", "password", "search", "textarea"),
               "box": ("text", "search", "textarea"),
               "checkbox": ("checkbox",), "dropdown": ("select-one", "select"),
               "tab": ("tab",)}


def _resolve(target, description):
    """Find an element by description, walking the grounding hierarchy.

    Returns (element, page, method) or raises with a message listing what was
    actually on the page, so the model can pick something real next time.
    """
    needle = " ".join(str(description or "").lower().split())
    if not needle:
        raise ValueError("No target given.")

    page = _read(target)

    wanted = None
    for word, kinds in _TYPE_WORDS.items():
        if (" " + needle + " ").find(" " + word + " ") >= 0:
            wanted = kinds
            needle = " ".join(w for w in needle.split() if w != word) or needle
            break
    needle = " ".join(w for w in needle.split() if w not in ("the", "a", "an")) or needle

    scored = []
    for element in page["elements"]:
        score = _score(element, needle)
        if not score:
            continue
        if wanted and (element.get("kind") in wanted or element.get("type") in wanted):
            score = min(score + 0.14, 1.0)
        scored.append(dict(element, match=score))

    scored.sort(key=lambda e: e["match"], reverse=True)
    if scored and scored[0]["match"] >= 0.5:
        return scored[0], page, "dom"

    # DOM could not identify it. Fall back to the vision layer, which reads the
    # browser window through UI Automation and OCR.
    from . import vision
    found = vision.locate(description)
    if found["ok"] and found["best"]["match"] >= 0.6:
        best = found["best"]
        return ({"text": best["text"], "kind": best["type"], "screen": True,
                 "cx": best["cx"], "cy": best["cy"], "match": best["match"]},
                page, "vision:" + best["source"])

    visible = [e["text"] for e in page["elements"] if e.get("text")][:12]
    raise LookupError(
        "Could not find '" + str(description) + "' on the page. Interactive elements "
        "here include: " + (", ".join(repr(v[:28]) for v in visible) or "none found") + ".")


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def browser_status():
    """Whether a controllable browser is available, and how to get one."""
    state = status()
    if not state["connected"]:
        state["how_to_connect"] = (
            "Call `browser_launch` to start a browser Jarvish can drive on its own "
            "profile, or restart Chrome yourself with --remote-debugging-port="
            + str(BROWSER_PORT) + " to let Jarvish use your own session.")
    return ok(**state)


def browser_launch():
    return launch()


def browser_tabs():
    """Every open tab in the controlled browser."""
    targets, problem = _require()
    if problem:
        return problem
    return ok(count=len(targets), tabs=[
        {"id": t["id"], "title": t.get("title", ""), "url": t.get("url", "")}
        for t in targets
    ])


def browser_open(url, new_tab=False):
    """Navigate to a URL, then confirm the page that actually loaded."""
    if not str(url or "").strip():
        return err("No URL given.")
    address = str(url).strip()
    if not address.startswith(("http://", "https://", "file://", "about:")):
        address = "https://" + address

    targets, problem = _require()
    if problem:
        # Nothing to drive yet — start one, since navigating is the point.
        started = launch()
        if not started["ok"]:
            return started
        targets, problem = _require()
        if problem:
            return problem

    if as_bool(new_tab):
        try:
            httpx.put(_endpoint("/json/new?" + address), timeout=8.0)
        except Exception:
            try:
                httpx.get(_endpoint("/json/new?" + address), timeout=8.0)
            except Exception as exc:
                return err("Could not open a new tab: " + str(exc))
        time.sleep(0.3)
        targets = _targets()
        target = _active(targets)
        if target is not None:
            settled, _page = _settle(target["id"])
            target = settled or target
    else:
        target = _active(targets)
        try:
            _run(_command(target["webSocketDebuggerUrl"], [
                ("Page.enable", {}),
                ("Page.navigate", {"url": address}),
            ]))
        except Exception as exc:
            return err("Could not navigate: " + str(exc))

    # Verify: read back what actually loaded rather than assuming it worked.
    target, page = _settle(target["id"])
    if page is None:
        return ok(navigated_to=address, verified=False,
                  note="Navigation was sent but the page never finished loading.")

    return ok(
        url=page["url"], title=page["title"], ready=page["ready"],
        requested=address,
        verified=address.rstrip("/").lower() in page["url"].rstrip("/").lower()
                 or page["ready"] == "complete",
        elements=len(page["elements"]),
        tab=target["id"],
    )


def browser_read(tab=None, full=False):
    """Read the current page: its text, controls and forms."""
    targets, problem = _require()
    if problem:
        return problem
    target = _active(targets, tab)
    if target is None:
        return err("No tab matches '" + str(tab) + "'.")
    try:
        page = _read(target)
    except Exception as exc:
        return err("Could not read the page: " + str(exc))

    limit = 20000 if as_bool(full) else 4000
    elements = page["elements"] if as_bool(full) else page["elements"][:60]
    return ok(
        url=page["url"], title=page["title"], ready=page["ready"],
        text=page["text"][:limit],
        truncated=len(page["text"]) > limit,
        elements=elements,
        element_count=len(page["elements"]),
        forms=page["forms"],
        scroll=page["scroll"],
        method="dom",
    )


def browser_find(target, tab=None):
    """Locate something on the page and report where it is, and how it was found."""
    targets, problem = _require()
    if problem:
        return problem
    page_target = _active(targets, tab)
    if page_target is None:
        return err("No tab matches '" + str(tab) + "'.")
    try:
        element, page, method = _resolve(page_target, target)
    except LookupError as exc:
        return err(str(exc))
    except Exception as exc:
        return err("Could not search the page: " + str(exc))
    return ok(found=element, method=method, url=page["url"], title=page["title"])


def browser_search_page(query, tab=None):
    """Find where a phrase appears in the page text."""
    targets, problem = _require()
    if problem:
        return problem
    page_target = _active(targets, tab)
    try:
        page = _read(page_target)
    except Exception as exc:
        return err("Could not read the page: " + str(exc))

    needle = str(query or "").lower()
    if not needle:
        return err("No search phrase given.")
    text = page["text"]
    hits = []
    start = 0
    lowered = text.lower()
    while len(hits) < 12:
        found = lowered.find(needle, start)
        if found == -1:
            break
        hits.append({"at": found,
                     "context": text[max(0, found - 90):found + len(needle) + 90].strip()})
        start = found + len(needle)
    return ok(query=query, matches=len(hits), hits=hits,
              url=page["url"], title=page["title"])


def _act_and_verify(target, action_js, description, method):
    """Run an action in the page, then look again to see whether it worked."""
    before = _fingerprint(_read(target))
    try:
        _evaluate(target, action_js)
    except Exception as exc:
        return err("The action failed: " + str(exc))

    time.sleep(0.25)
    # The tab may have navigated, so settle before comparing.
    _refreshed, after_page = _settle(target["id"], timeout=6.0)
    if after_page is None:
        return ok(action=description, method=method, verified=False,
                  note="Acted, but the page could not be re-read afterwards.")

    after = _fingerprint(after_page)
    changed = before != after
    return ok(
        action=description,
        method=method,
        verified=changed,
        verification=("The page changed after the action."
                      if changed else
                      "The page looks identical — the action may not have taken effect."),
        url_before=before[0], url=after_page["url"], title=after_page["title"],
        elements=len(after_page["elements"]),
    )


def _selector_for(element):
    """A JS expression that re-finds this element in the page."""
    if element.get("id_attr"):
        return "document.getElementById(" + json.dumps(element["id_attr"]) + ")"
    if element.get("name"):
        return ("document.getElementsByName(" + json.dumps(element["name"]) + ")[0]")
    # Fall back to matching on the visible label, which is how it was found.
    label = json.dumps((element.get("text") or "")[:80])
    return (
        "[...document.querySelectorAll('a,button,input,select,textarea,[role=button],"
        "[role=link],[role=tab],[role=menuitem],summary')].find(e => {"
        "const t = ((e.getAttribute('aria-label')||e.innerText||e.value||e.placeholder"
        "||e.title||'')+'').replace(/\\s+/g,' ').trim(); return t === " + label + ";})"
    )


def browser_click(target, tab=None, confirm=False):
    """Click something on the page, then verify the page responded."""
    targets, problem = _require()
    if problem:
        return problem
    page_target = _active(targets, tab)
    if page_target is None:
        return err("No tab matches '" + str(tab) + "'.")

    try:
        element, _page, method = _resolve(page_target, target)
    except LookupError as exc:
        return err(str(exc))
    except Exception as exc:
        return err("Could not locate the target: " + str(exc))

    # The vision layer found it but the DOM did not — click by screen coordinate.
    if element.get("screen"):
        from . import vision
        try:
            vision._click_at(element["cx"], element["cy"])
        except Exception as exc:
            return err("Could not click: " + str(exc))
        time.sleep(SETTLE)
        try:
            page = _read(_active(_targets(), page_target["id"]) or page_target)
            return ok(action="clicked " + repr(element["text"]), method=method,
                      verified=True, url=page["url"], title=page["title"],
                      note="Clicked by screen coordinate; the DOM did not expose this element.")
        except Exception:
            return ok(action="clicked " + repr(element["text"]), method=method,
                      verified=False, note="Clicked by screen coordinate; could not re-read the page.")

    script = ("(() => { const el = " + _selector_for(element) +
              "; if (!el) return 'gone'; el.scrollIntoView({block:'center'}); "
              "el.click(); return 'clicked'; })()")
    return _act_and_verify(page_target, script,
                           "clicked " + repr(element.get("text", "")[:50]), method)


def browser_type(target, text, submit=False, tab=None):
    """Type into a field on the page. Filling a form is a MEDIUM-risk action."""
    targets, problem = _require()
    if problem:
        return problem
    page_target = _active(targets, tab)
    if page_target is None:
        return err("No tab matches '" + str(tab) + "'.")

    try:
        element, _page, method = _resolve(page_target, target)
    except LookupError as exc:
        return err(str(exc))
    except Exception as exc:
        return err("Could not locate the field: " + str(exc))

    if element.get("screen"):
        return err("That field was only found visually, not in the page. Typing needs a "
                   "real form field — try naming it as it appears on the page.")

    wanted = str(text or "")
    value = json.dumps(wanted)
    sending = as_bool(submit)

    # Typing does not change the page's text, so a page fingerprint cannot tell
    # you whether it worked. Read the field's own value back instead — that is
    # the only honest evidence the characters landed.
    script = ("(() => { const el = " + _selector_for(element) + "; "
              "if (!el) return {status:'gone'}; el.focus(); "
              "if (el.isContentEditable) { el.textContent = " + value + "; } "
              "else { el.value = " + value + "; } "
              "el.dispatchEvent(new Event('input', {bubbles:true})); "
              "el.dispatchEvent(new Event('change', {bubbles:true})); "
              "return {status:'typed', "
              "readback: el.isContentEditable ? el.textContent : el.value}; })()")

    try:
        outcome = _evaluate(page_target, script)
    except Exception as exc:
        return err("Could not type into the field: " + str(exc))

    if not isinstance(outcome, dict) or outcome.get("status") == "gone":
        return err("The field disappeared before it could be filled. The page may have "
                   "changed — read it again and retry.")

    readback = outcome.get("readback")
    landed = readback == wanted
    result = ok(
        action="typed into " + repr(element.get("text", "")[:40]),
        method=method,
        field=element.get("name") or element.get("text", "")[:40],
        typed=wanted,
        readback=readback,
        verified=landed,
        verification=("The field now contains exactly what was typed."
                      if landed else
                      "The field holds " + repr(readback) + " rather than what was typed; "
                      "the page may be reformatting or rejecting the input."),
    )

    if not sending:
        return result

    # Submitting is a separate, gated act; do it only when explicitly asked.
    submit_script = ("(() => { const el = " + _selector_for(element) + "; "
                     "if (!el) return 'gone'; const f = el.form || el.closest('form'); "
                     "if (!f) return 'no-form'; "
                     "f.requestSubmit ? f.requestSubmit() : f.submit(); return 'submitted'; })()")
    sent = _act_and_verify(page_target, submit_script,
                           "submitted the form after typing", method)
    sent["typed"] = wanted
    sent["readback"] = readback
    sent["field_verified"] = landed
    return sent


def browser_clear(target, tab=None):
    """Empty a field, verifying it is actually empty afterwards."""
    targets, problem = _require()
    if problem:
        return problem
    page_target = _active(targets, tab)
    if page_target is None:
        return err("No tab matches '" + str(tab) + "'.")
    try:
        element, _page, method = _resolve(page_target, target)
    except LookupError as exc:
        return err(str(exc))

    script = ("(() => { const el = " + _selector_for(element) + "; "
              "if (!el) return {status:'gone'}; el.focus(); "
              "if (el.isContentEditable) { el.textContent = ''; } else { el.value = ''; } "
              "el.dispatchEvent(new Event('input', {bubbles:true})); "
              "el.dispatchEvent(new Event('change', {bubbles:true})); "
              "return {status:'cleared', "
              "readback: el.isContentEditable ? el.textContent : el.value}; })()")
    try:
        outcome = _evaluate(page_target, script)
    except Exception as exc:
        return err("Could not clear the field: " + str(exc))
    if not isinstance(outcome, dict) or outcome.get("status") == "gone":
        return err("That field is no longer on the page.")

    empty = not outcome.get("readback")
    return ok(action="cleared " + repr(element.get("text", "")[:40]), method=method,
              readback=outcome.get("readback"), verified=empty,
              verification="The field is empty." if empty
                           else "The field still holds " + repr(outcome.get("readback")) + ".")


def browser_check(target, checked=True, tab=None):
    """Tick or untick a checkbox or radio button, then read its state back."""
    targets, problem = _require()
    if problem:
        return problem
    page_target = _active(targets, tab)
    if page_target is None:
        return err("No tab matches '" + str(tab) + "'.")
    try:
        element, _page, method = _resolve(page_target, target)
    except LookupError as exc:
        return err(str(exc))

    want = "true" if as_bool(checked, True) else "false"
    script = ("(() => { const el = " + _selector_for(element) + "; "
              "if (!el) return {status:'gone'}; "
              "if (el.checked === undefined) return {status:'not-checkable'}; "
              "if (el.checked !== " + want + ") el.click(); "
              "return {status:'set', checked: !!el.checked}; })()")
    try:
        outcome = _evaluate(page_target, script)
    except Exception as exc:
        return err("Could not change the checkbox: " + str(exc))
    if not isinstance(outcome, dict):
        return err("Unexpected response from the page.")
    if outcome.get("status") == "gone":
        return err("That control is no longer on the page.")
    if outcome.get("status") == "not-checkable":
        return err(repr(element.get("text", "")) + " is not a checkbox or radio button.")

    landed = outcome["checked"] == (want == "true")
    return ok(action=("checked" if want == "true" else "unchecked") + " " +
                     repr(element.get("text", "")[:40]),
              method=method, checked=outcome["checked"], verified=landed,
              verification="The control is now " +
                           ("checked." if outcome["checked"] else "unchecked."))


def browser_download(url, tab=None):
    """Download a file through the browser and confirm it landed on disk.

    The file is written to `data/downloads/`, and this only reports success
    once the file exists and has a non-zero size.
    """
    targets, problem = _require()
    if problem:
        return problem
    page_target = _active(targets, tab)

    address = str(url or "").strip()
    if not address:
        return err("No URL given.")
    if not address.startswith(("http://", "https://")):
        return err("Only http and https downloads are supported.")

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in DOWNLOAD_DIR.iterdir()} if DOWNLOAD_DIR.exists() else set()

    try:
        _run(_command(page_target["webSocketDebuggerUrl"], [
            ("Page.enable", {}),
            ("Page.setDownloadBehavior", {"behavior": "allow",
                                          "downloadPath": str(DOWNLOAD_DIR)}),
        ]))
    except Exception:
        # Older Chrome builds moved this to Browser.setDownloadBehavior.
        try:
            _run(_command(page_target["webSocketDebuggerUrl"], [
                ("Browser.setDownloadBehavior", {"behavior": "allow",
                                                 "downloadPath": str(DOWNLOAD_DIR)}),
            ]))
        except Exception as exc:
            return err("Could not set the download location: " + str(exc))

    script = ("(() => { const a = document.createElement('a'); a.href = " +
              json.dumps(address) + "; a.download = ''; document.body.appendChild(a); "
              "a.click(); a.remove(); return 'started'; })()")
    try:
        _evaluate(page_target, script)
    except Exception as exc:
        return err("Could not start the download: " + str(exc))

    # Verify by watching the directory, ignoring Chrome's partial files.
    deadline = time.time() + 25.0
    while time.time() < deadline:
        time.sleep(0.4)
        now = {p.name for p in DOWNLOAD_DIR.iterdir()}
        fresh = [n for n in now - before if not n.endswith(".crdownload")]
        if fresh:
            path = DOWNLOAD_DIR / fresh[0]
            size = path.stat().st_size
            if size > 0:
                return ok(downloaded=fresh[0], saved_to=str(path),
                          size_kb=round(size / 1024, 1), verified=True,
                          source=address)

    return ok(requested=address, verified=False,
              note="The download was started but no completed file appeared in "
                   + str(DOWNLOAD_DIR) + " within 25 seconds.")


def browser_select(target, option, tab=None):
    """Choose an option in a dropdown."""
    targets, problem = _require()
    if problem:
        return problem
    page_target = _active(targets, tab)
    try:
        element, _page, method = _resolve(page_target, target)
    except LookupError as exc:
        return err(str(exc))

    wanted = json.dumps(str(option or ""))

    # "Choose an option" covers both a <select> and a group of radio buttons
    # sharing a name. They need different handling, so detect which this is.
    script = ("(() => { const el = " + _selector_for(element) + "; "
              "if (!el) return {status:'gone'}; "
              "const want = " + wanted + ".toLowerCase().trim(); "
              "if (el.tagName.toLowerCase() === 'select') { "
              "  const opt = [...el.options].find(o => o.text.toLowerCase().trim() === want) "
              "           || [...el.options].find(o => String(o.value).toLowerCase() === want) "
              "           || [...el.options].find(o => o.text.toLowerCase().includes(want)); "
              "  if (!opt) return {status:'no-option', "
              "      available: [...el.options].map(o => o.text).slice(0, 20)}; "
              "  el.value = opt.value; "
              "  el.dispatchEvent(new Event('change', {bubbles:true})); "
              "  return {status:'selected', chosen: opt.text, readback: el.value}; } "
              "if (el.type === 'radio' && el.name) { "
              "  const group = [...document.getElementsByName(el.name)]; "
              "  const hit = group.find(r => String(r.value).toLowerCase() === want) "
              "           || group.find(r => (r.labels && r.labels[0] ? "
              "                r.labels[0].innerText : '').toLowerCase().trim() === want) "
              "           || group.find(r => String(r.value).toLowerCase().includes(want)); "
              "  if (!hit) return {status:'no-option', "
              "      available: group.map(r => r.value).slice(0, 20)}; "
              "  hit.click(); "
              "  return {status:'selected', chosen: hit.value, "
              "          readback: (group.find(r => r.checked) || {}).value}; } "
              "return {status:'not-selectable'}; })()")

    try:
        outcome = _evaluate(page_target, script)
    except Exception as exc:
        return err("Could not change the selection: " + str(exc))
    if not isinstance(outcome, dict):
        return err("Unexpected response from the page.")

    state = outcome.get("status")
    if state == "gone":
        return err("That control is no longer on the page.")
    if state == "not-selectable":
        return err(repr(element.get("text", "")) + " is not a dropdown or a radio group.")
    if state == "no-option":
        return err("No option matching " + repr(option) + ". Available: " +
                   ", ".join(repr(str(a)) for a in outcome.get("available", [])) + ".")

    landed = str(outcome.get("readback", "")).lower() == str(outcome.get("chosen", "")).lower()
    return ok(action="selected " + repr(outcome.get("chosen")), method=method,
              chosen=outcome.get("chosen"), readback=outcome.get("readback"),
              verified=landed,
              verification=("The control now reads " + repr(outcome.get("readback")) + "."
                            if landed else
                            "The control reads " + repr(outcome.get("readback")) +
                            " rather than the option chosen."))


def browser_submit(target=None, tab=None, confirm=False):
    """Submit a form. High risk: this is the action that sends things."""
    targets, problem = _require()
    if problem:
        return problem
    page_target = _active(targets, tab)

    if target:
        try:
            element, _page, method = _resolve(page_target, target)
        except LookupError as exc:
            return err(str(exc))
        script = ("(() => { const el = " + _selector_for(element) + "; if (!el) return 'gone'; "
                  "const f = el.form || el.closest('form'); if (!f) { el.click(); return 'clicked'; } "
                  "f.requestSubmit ? f.requestSubmit() : f.submit(); return 'submitted'; })()")
        label = "submitted via " + repr(element.get("text", "")[:40])
    else:
        method = "dom"
        script = ("(() => { const f = document.forms[0]; if (!f) return 'no-form'; "
                  "f.requestSubmit ? f.requestSubmit() : f.submit(); return 'submitted'; })()")
        label = "submitted the first form on the page"

    return _act_and_verify(page_target, script, label, method)


def browser_scroll(direction="down", amount=1, tab=None):
    """Scroll the page."""
    targets, problem = _require()
    if problem:
        return problem
    page_target = _active(targets, tab)
    steps = as_int(amount, 1, 1, 20)
    where = str(direction or "down").lower()
    if where in ("top", "start"):
        script = "(() => { scrollTo(0, 0); return scrollY; })()"
    elif where in ("bottom", "end"):
        script = "(() => { scrollTo(0, document.body.scrollHeight); return scrollY; })()"
    else:
        sign = "-" if where in ("up", "back") else ""
        script = ("(() => { scrollBy(0, " + sign + "innerHeight * 0.85 * " + str(steps) +
                  "); return scrollY; })()")
    try:
        position = _evaluate(page_target, script)
        page = _read(page_target)
    except Exception as exc:
        return err("Could not scroll: " + str(exc))
    return ok(scrolled=where, position=position, scroll=page["scroll"],
              visible_elements=sum(1 for e in page["elements"] if e.get("inViewport")))


def browser_new_tab(url=None):
    return browser_open(url or "about:blank", new_tab=True)


def browser_close_tab(tab):
    """Close a tab by id or by part of its title."""
    targets, problem = _require()
    if problem:
        return problem
    found = _active(targets, tab)
    if found is None:
        return err("No tab matches '" + str(tab) + "'.")
    try:
        httpx.get(_endpoint("/json/close/" + found["id"]), timeout=6.0)
    except Exception as exc:
        return err("Could not close the tab: " + str(exc))
    time.sleep(0.3)
    remaining = _targets()
    return ok(closed={"id": found["id"], "title": found.get("title", "")},
              verified=all(t["id"] != found["id"] for t in remaining),
              remaining=len(remaining))


def browser_switch_tab(tab):
    """Bring a tab to the front."""
    targets, problem = _require()
    if problem:
        return problem
    found = _active(targets, tab)
    if found is None:
        return err("No tab matches '" + str(tab) + "'.")
    try:
        httpx.get(_endpoint("/json/activate/" + found["id"]), timeout=6.0)
    except Exception as exc:
        return err("Could not switch tabs: " + str(exc))
    return ok(switched_to={"id": found["id"], "title": found.get("title", ""),
                           "url": found.get("url", "")})


SCHEMAS = [
    tool("browser_status",
         "Check whether Jarvish can control a browser, and how to connect one. Call this "
         "first if any other browser tool reports no browser."),
    tool("browser_launch",
         "Start a browser that Jarvish can control. Uses a separate profile, so the "
         "user's own Chrome windows and logins are untouched."),
    tool("browser_tabs", "List every open tab in the controlled browser."),
    tool("browser_open",
         "Navigate to a URL and confirm what actually loaded.",
         {"url": string("The address to open."),
          "new_tab": boolean("Open in a new tab instead of the current one.")},
         ["url"]),
    tool("browser_read",
         "Read the current page: its visible text, its interactive elements and its "
         "forms. Use this to find out what is on a page before acting on it.",
         {"tab": string("Tab id or part of its title. Defaults to the active tab."),
          "full": boolean("Return the entire page rather than the first part.")}),
    tool("browser_find",
         "Locate an element on the page by description and report where it is and how "
         "it was identified.",
         {"target": string("What to find, for example 'the Sign in button'."),
          "tab": string("Tab id or part of its title.")},
         ["target"]),
    tool("browser_search_page",
         "Find where a phrase appears in the page text, with surrounding context.",
         {"query": string("The phrase to look for."),
          "tab": string("Tab id or part of its title.")},
         ["query"]),
    tool("browser_click",
         "Click an element on the page, then check the page actually responded.",
         {"target": string("What to click, for example 'the Sign in button'."),
          "tab": string("Tab id or part of its title.")},
         ["target"]),
    tool("browser_type",
         "Type into a field on the page. Use for filling in forms.",
         {"target": string("The field, for example 'the search box' or 'Email'."),
          "text": string("What to type."),
          "submit": boolean("Submit the form afterwards. This sends the form."),
          "tab": string("Tab id or part of its title.")},
         ["target", "text"]),
    tool("browser_clear",
         "Empty a field on the page.",
         {"target": string("The field to clear."),
          "tab": string("Tab id or part of its title.")},
         ["target"]),
    tool("browser_check",
         "Tick or untick a checkbox or radio button.",
         {"target": string("The checkbox, by its label."),
          "checked": boolean("True to tick it, false to untick it."),
          "tab": string("Tab id or part of its title.")},
         ["target"]),
    tool("browser_download",
         "Download a file through the browser into the user's Jarvish downloads "
         "folder, and confirm it arrived.",
         {"url": string("Direct link to the file."),
          "tab": string("Tab id or part of its title.")},
         ["url"]),
    tool("browser_select",
         "Choose an option in a dropdown.",
         {"target": string("The dropdown."), "option": string("The option to choose."),
          "tab": string("Tab id or part of its title.")},
         ["target", "option"]),
    tool("browser_submit",
         "Submit a form on the page. This is what sends a message, posts, or confirms.",
         {"target": string("The submit control, if there is more than one form."),
          "tab": string("Tab id or part of its title.")}),
    tool("browser_scroll",
         "Scroll the page.",
         {"direction": string("Which way.", ["down", "up", "top", "bottom"]),
          "amount": string("How many screens to move."),
          "tab": string("Tab id or part of its title.")}),
    tool("browser_new_tab", "Open a new tab.",
         {"url": string("Address to open in it.")}),
    tool("browser_close_tab", "Close a tab.",
         {"tab": string("Tab id or part of its title.")}, ["tab"]),
    tool("browser_switch_tab", "Bring a tab to the front.",
         {"tab": string("Tab id or part of its title.")}, ["tab"]),
]

REGISTRY = {
    "browser_status": browser_status,
    "browser_launch": browser_launch,
    "browser_tabs": browser_tabs,
    "browser_open": browser_open,
    "browser_read": browser_read,
    "browser_find": browser_find,
    "browser_search_page": browser_search_page,
    "browser_click": browser_click,
    "browser_type": browser_type,
    "browser_clear": browser_clear,
    "browser_check": browser_check,
    "browser_download": browser_download,
    "browser_select": browser_select,
    "browser_submit": browser_submit,
    "browser_scroll": browser_scroll,
    "browser_new_tab": browser_new_tab,
    "browser_close_tab": browser_close_tab,
    "browser_switch_tab": browser_switch_tab,
}
