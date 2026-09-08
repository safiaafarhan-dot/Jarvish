"""Screen, power and window control, plus the built-in help."""

import ctypes
from datetime import datetime

from .util import DATA_DIR, as_bool, as_int, err, ok, powershell, string, boolean, tool

# Power actions that interrupt the user's work need an explicit confirmation
# before they run, so the model has to ask first.
NEEDS_CONFIRM = {"shutdown", "restart", "sign_out", "hibernate"}


def power_action(action, confirm=False):
    """Lock, sleep, wake, sign out, restart or shut down this PC."""
    choice = str(action).strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "log_out": "sign_out", "logout": "sign_out", "log_off": "sign_out",
        "reboot": "restart", "power_off": "shutdown", "turn_off": "shutdown",
        "screen_off": "displays_off", "display_off": "displays_off",
        "wake": "wake_displays", "wake_up": "wake_displays", "open": "wake_displays",
    }
    choice = aliases.get(choice, choice)

    if choice in NEEDS_CONFIRM and not as_bool(confirm):
        return err(
            "'" + choice + "' will interrupt whatever the user is doing. Ask them to "
            "confirm out loud first, then call this again with confirm set to true."
        )

    try:
        if choice == "lock":
            ctypes.windll.user32.LockWorkStation()
            return ok(action="lock")

        if choice == "displays_off":
            # WM_SYSCOMMAND / SC_MONITORPOWER, 2 = power off.
            ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
            return ok(action="displays_off",
                      note="Move the mouse or press a key to bring the screen back.")

        if choice == "wake_displays":
            ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, -1)
            # Nudge the input stack so Windows registers real activity.
            ctypes.windll.user32.mouse_event(0x0001, 0, 0, 0, 0)
            return ok(action="wake_displays",
                      note="Screens are awake. Jarvish cannot type the user's password - "
                           "signing in has to be done by hand.")

        if choice == "sleep":
            result = powershell("Add-Type -AssemblyName System.Windows.Forms; "
                                "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)")
            return result if not result["ok"] else ok(action="sleep")

        if choice == "hibernate":
            result = powershell("shutdown /h")
            return result if not result["ok"] else ok(action="hibernate")

        if choice == "sign_out":
            result = powershell("shutdown /l")
            return result if not result["ok"] else ok(action="sign_out")

        if choice == "restart":
            result = powershell("shutdown /r /t 5")
            return result if not result["ok"] else ok(action="restart", in_seconds=5)

        if choice == "shutdown":
            result = powershell("shutdown /s /t 10")
            return result if not result["ok"] else ok(action="shutdown", in_seconds=10)

        if choice == "cancel":
            result = powershell("shutdown /a")
            return result if not result["ok"] else ok(action="cancelled")

    except Exception as exc:
        return err("Could not " + choice + ": " + str(exc))

    return err(
        "Unknown action '" + choice + "'. Options: lock, sleep, wake_displays, "
        "displays_off, sign_out, restart, shutdown, cancel."
    )


def set_brightness(percent):
    """Set the laptop screen brightness."""
    level = as_int(percent, 60, 0, 100)
    result = powershell(
        "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
        "-ErrorAction Stop).WmiSetBrightness(1," + str(level) + ")"
    )
    if not result["ok"]:
        return err(
            "Brightness control is not available on this display. It works on most "
            "laptop panels but not on external monitors. (" + result["error"][:150] + ")"
        )
    return ok(brightness_percent=level)


def get_brightness():
    """Read the current screen brightness."""
    result = powershell(
        "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
        "-ErrorAction Stop).CurrentBrightness"
    )
    if not result["ok"]:
        return err("Could not read brightness on this display.")
    return ok(brightness_percent=as_int(result["output"].strip(), 0, 0, 100))


def take_screenshot():
    """Capture the screen and save it as a PNG."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    filename = "screenshot-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".png"
    path = DATA_DIR / filename

    script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
        "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
        "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); "
        "$bmp.Save('" + str(path).replace("'", "''") + "'); "
        "$g.Dispose(); $bmp.Dispose(); Write-Output \"$($b.Width)x$($b.Height)\""
    )
    result = powershell(script, timeout=30)
    if not result["ok"]:
        return result
    if not path.exists():
        return err("The screenshot file was not written.")

    return ok(
        saved_to=str(path),
        view_url="/data/" + filename,
        resolution=result["output"].strip(),
        size_kb=round(path.stat().st_size / 1024, 1),
    )


def type_text(text):
    """Type text into whatever window is focused."""
    body = str(text or "")
    if not body:
        return err("No text given.")
    if len(body) > 2000:
        return err("That is too long to type safely. Keep it under 2000 characters.")

    # SendKeys treats these as control characters, so they must be braced.
    escaped = body
    for char in "+^%~(){}[]":
        escaped = escaped.replace(char, "{" + char + "}")
    escaped = escaped.replace("\n", "{ENTER}")

    result = powershell(
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Start-Sleep -Milliseconds 400; "
        "[System.Windows.Forms.SendKeys]::SendWait(@'\n" + escaped + "\n'@)"
    )
    if not result["ok"]:
        return result
    return ok(typed=body[:120], length=len(body),
              note="Text was typed into whichever window had focus.")


def list_windows():
    """List the open application windows."""
    script = (
        "Get-Process | Where-Object { $_.MainWindowTitle } | "
        "Select-Object -First 25 | ForEach-Object { $_.ProcessName + '|' + $_.MainWindowTitle }"
    )
    result = powershell(script)
    if not result["ok"]:
        return result
    windows = []
    for line in result["output"].splitlines():
        process, _, title = line.partition("|")
        if title.strip():
            windows.append({"process": process.strip(), "title": title.strip()})
    return ok(count=len(windows), windows=windows)


def focus_window(title):
    """Bring a window to the front by part of its title."""
    needle = str(title or "").strip()
    if not needle:
        return err("No window title given.")
    script = (
        "$p = Get-Process | Where-Object { $_.MainWindowTitle -like '*" +
        needle.replace("'", "''") + "*' } | Select-Object -First 1; "
        "if (-not $p) { Write-Error 'No matching window.'; exit 1 }; "
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        "[Microsoft.VisualBasic.Interaction]::AppActivate($p.Id); "
        "Write-Output $p.MainWindowTitle"
    )
    result = powershell(script)
    if not result["ok"]:
        return err("Could not find or focus a window matching '" + needle + "'.")
    return ok(focused=result["output"].strip())


def close_app(name):
    """Close an application by name."""
    target = str(name or "").strip()
    if not target:
        return err("No application name given.")
    cleaned = target.replace("'", "''").replace(".exe", "")
    script = (
        "$p = Get-Process -Name '" + cleaned + "' -ErrorAction SilentlyContinue; "
        "if (-not $p) { Write-Error 'Not running.'; exit 1 }; "
        "$p | ForEach-Object { $_.CloseMainWindow() | Out-Null }; "
        "Write-Output $p.Count"
    )
    result = powershell(script)
    if not result["ok"]:
        return err("'" + target + "' does not appear to be running.")
    return ok(closed=target, windows_closed=result["output"].strip(),
              note="Asked the app to close. Unsaved work will prompt the user.")


def help_overview():
    """Describe everything Jarvish can do."""
    from . import tools

    return ok(
        total_tools=len(tools.REGISTRY),
        categories={
            "personal memory": "Remembers your details and standing instructions. Say "
                               "'remember my favourite food is biryani' or 'always call me boss'.",
            "day to day": "Weather, news headlines, a morning briefing, currency rates, "
                          "web search, and reading any web page.",
            "web apps": "Opens around 80 sites by name - Gmail, YouTube, WhatsApp, "
                        "Netflix, Amazon, Maps, GitHub - and can search inside them.",
            "messaging": "Drafts WhatsApp messages and emails and opens them ready to "
                         "send. You press send, not Jarvish.",
            "this pc": "System status, running processes, open and close apps, focus "
                       "windows, type text, screenshots, volume, brightness.",
            "power": "Lock, sleep, screen off, wake, sign out, restart, shut down. The "
                     "disruptive ones ask you to confirm first.",
            "connectivity": "WiFi status, scanning, connecting to saved networks, radio "
                            "on/off, Bluetooth devices, network info, Settings pages.",
            "files": "Find files, list folders, read text files.",
            "phone": "Opens Phone Link, and gives you a URL to use Jarvish from your phone.",
        },
        cannot_do=[
            "Send WhatsApp or email by itself - it drafts, you send.",
            "Read your WhatsApp or email inbox - no API exists for personal accounts.",
            "Power on or sign in to a locked PC.",
            "Control your phone directly - Windows exposes no API for that.",
        ],
    )


SCHEMAS = [
    tool("power_action",
         "Lock, sleep, wake the screen, sign out, restart or shut down this PC. For "
         "shutdown, restart, sign_out and hibernate you must ask the user to confirm "
         "first, then call again with confirm true.",
         {"action": string("lock, sleep, wake_displays, displays_off, sign_out, restart, "
                           "shutdown, hibernate, or cancel",
                           ["lock", "sleep", "wake_displays", "displays_off", "sign_out",
                            "restart", "shutdown", "hibernate", "cancel"]),
          "confirm": boolean("Set true only after the user has confirmed out loud.")},
         ["action"]),
    tool("set_brightness", "Set the laptop screen brightness.",
         {"percent": string("Brightness from 0 to 100.")}, ["percent"]),
    tool("get_brightness", "Read the current screen brightness."),
    tool("take_screenshot", "Capture the screen and save it as a PNG."),
    tool("type_text",
         "Type text into whichever window currently has focus. Useful after opening an "
         "app such as Notepad.",
         {"text": string("The text to type.")}, ["text"]),
    tool("list_windows", "List the application windows that are currently open."),
    tool("focus_window", "Bring a window to the front by part of its title.",
         {"title": string("Part of the window title.")}, ["title"]),
    tool("close_app", "Close a running application by name.",
         {"name": string("The application or process name, for example notepad.")}, ["name"]),
    tool("help_overview",
         "Explain what Jarvish can and cannot do. Use this when the user asks what it "
         "can do, what its features are, or for help."),
]

REGISTRY = {
    "power_action": power_action,
    "set_brightness": set_brightness,
    "get_brightness": get_brightness,
    "take_screenshot": take_screenshot,
    "type_text": type_text,
    "list_windows": list_windows,
    "focus_window": focus_window,
    "close_app": close_app,
    "help_overview": help_overview,
}
