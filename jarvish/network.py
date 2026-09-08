"""WiFi and Bluetooth control on Windows.

WiFi goes through netsh, which needs no admin rights for status, scanning, or
connecting to a saved profile. Bluetooth device listing goes through PnP.
Turning either radio on or off uses the WinRT Radio API, which requires the
"Let apps control device radios" privacy setting to be enabled.
"""

import re
import webbrowser

from .util import err, ok, powershell, string, tool

# --------------------------------------------------------------------------
# WiFi
# --------------------------------------------------------------------------

_FIELDS = {
    "state": r"^\s*State\s*:\s*(.+)$",
    "ssid": r"^\s*SSID\s*:\s*(.+)$",
    "signal": r"^\s*Signal\s*:\s*(.+)$",
    "radio_type": r"^\s*Radio type\s*:\s*(.+)$",
    "channel": r"^\s*Channel\s*:\s*(.+)$",
    "authentication": r"^\s*Authentication\s*:\s*(.+)$",
    "receive_rate": r"^\s*Receive rate \(Mbps\)\s*:\s*(.+)$",
    "transmit_rate": r"^\s*Transmit rate \(Mbps\)\s*:\s*(.+)$",
}


def wifi_status():
    """Which network you are on, and how good the signal is."""
    result = powershell("netsh wlan show interfaces")
    if not result["ok"]:
        return result

    output = result["output"]
    if "no wireless interface" in output.lower():
        return err("This machine has no wireless adapter, or the WLAN service is off.")

    status = {}
    for field, pattern in _FIELDS.items():
        match = re.search(pattern, output, re.M | re.I)
        if match:
            status[field] = match.group(1).strip()

    connected = status.get("state", "").lower().startswith("connected")
    return ok(connected=connected, **status)


def wifi_networks():
    """Scan for WiFi networks in range."""
    result = powershell("netsh wlan show networks", timeout=30)
    if not result["ok"]:
        return result

    networks = []
    current = {}
    for line in result["output"].splitlines():
        ssid = re.match(r"^SSID \d+\s*:\s*(.*)$", line.strip(), re.I)
        if ssid:
            if current.get("ssid"):
                networks.append(current)
            current = {"ssid": ssid.group(1).strip() or "(hidden)"}
            continue
        for key, pattern in (("authentication", r"^Authentication\s*:\s*(.+)$"),
                             ("encryption", r"^Encryption\s*:\s*(.+)$")):
            match = re.match(pattern, line.strip(), re.I)
            if match and current:
                current[key] = match.group(1).strip()
    if current.get("ssid"):
        networks.append(current)

    if not networks:
        return err("No networks found. The adapter may be off or still scanning.")
    return ok(count=len(networks), networks=networks)


def wifi_saved_networks():
    """List the WiFi networks this PC already has passwords for."""
    result = powershell("netsh wlan show profiles")
    if not result["ok"]:
        return result
    profiles = re.findall(r"All User Profile\s*:\s*(.+)", result["output"], re.I)
    names = [p.strip() for p in profiles if p.strip()]
    return ok(count=len(names), saved_networks=names)


def wifi_connect(name):
    """Connect to a saved WiFi network by name."""
    ssid = str(name).strip()
    if not ssid:
        return err("No network name given.")

    saved = wifi_saved_networks()
    if saved["ok"]:
        match = next((n for n in saved["saved_networks"] if n.lower() == ssid.lower()), None)
        if match is None:
            match = next((n for n in saved["saved_networks"] if ssid.lower() in n.lower()), None)
        if match is None:
            return err(
                "'" + ssid + "' is not a saved network. Windows can only auto-connect to "
                "networks whose password it already has. Saved: "
                + (", ".join(saved["saved_networks"]) or "none")
            )
        ssid = match

    result = powershell('netsh wlan connect name="' + ssid.replace('"', "") + '"')
    if not result["ok"]:
        return result
    return ok(connecting_to=ssid, note=result["output"] or "Connection request sent.")


def wifi_disconnect():
    """Disconnect from the current WiFi network."""
    result = powershell("netsh wlan disconnect")
    if not result["ok"]:
        return result
    return ok(disconnected=True)


# --------------------------------------------------------------------------
# Bluetooth
# --------------------------------------------------------------------------

_BT_DEVICES = (
    "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
    "Select-Object FriendlyName,Status | "
    "ForEach-Object { $_.Status + '|' + $_.FriendlyName }"
)


def bluetooth_devices():
    """List Bluetooth devices known to this PC and whether they are connected."""
    result = powershell(_BT_DEVICES)
    if not result["ok"]:
        return result

    # Windows lists Bluetooth protocol stack entries in the same class as real
    # peripherals. These are plumbing, not devices the user owns.
    noise = re.compile(
        r"\b(profile|service|enumerator|protocol|transport|rfcomm|avrcp|a2dp|hid|"
        r"personal area network|device id|generic attribute)\b", re.I)

    devices, plumbing = [], 0
    for line in result["output"].splitlines():
        if "|" not in line:
            continue
        status, _, name = line.partition("|")
        name = name.strip()
        if not name:
            continue
        if noise.search(name):
            plumbing += 1
            continue
        devices.append({
            "name": name,
            "status": status.strip(),
            "connected": status.strip().upper() == "OK",
        })

    if not devices and not plumbing:
        return err("No Bluetooth hardware found on this PC.")
    if not devices:
        return ok(count=0, devices=[], connected=[],
                  note="Bluetooth is present but nothing is paired yet.")
    connected = [d["name"] for d in devices if d["connected"]]
    return ok(count=len(devices), devices=devices, connected=connected)


# WinRT radio access. Async in PowerShell needs a small await shim.
_RADIO_SHIM = """
$null = [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime]
$null = [Windows.Devices.Radios.RadioAccessStatus,Windows.System.Devices,ContentType=WindowsRuntime]
Function Await($op, $type) {
  $task = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } |
    Select-Object -First 1
  $task = $task.MakeGenericMethod($type).Invoke($null, @($op))
  $task.Wait(5000) | Out-Null
  $task.Result
}
$access = Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus])
if ("$access" -ne 'Allowed') { Write-Error "Radio access denied: $access"; exit 1 }
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
"""


def _radio(kind, action):
    """Turn the WiFi or Bluetooth radio on or off through WinRT."""
    wanted = "Bluetooth" if kind == "bluetooth" else "WiFi"
    state = "On" if action == "on" else "Off"
    script = _RADIO_SHIM + """
$target = $radios | Where-Object { $_.Kind -eq '""" + wanted + """' } | Select-Object -First 1
if (-not $target) { Write-Error 'No """ + wanted + """ radio found.'; exit 1 }
$result = Await ($target.SetStateAsync([Windows.Devices.Radios.RadioState]::""" + state + """)) ([Windows.Devices.Radios.RadioAccessStatus])
Write-Output "$($target.Name) -> $result"
"""
    result = powershell(script, timeout=30)
    if not result["ok"]:
        return err(
            result["error"]
            + " | If this says access was denied, enable Settings > Privacy & security > "
              "Radios > 'Let apps control device radios'."
        )
    return ok(radio=wanted, state=state.lower(), detail=result["output"])


def bluetooth_power(action):
    """Turn Bluetooth on or off."""
    choice = str(action).strip().lower()
    if choice not in ("on", "off"):
        return err("Action must be 'on' or 'off'.")
    return _radio("bluetooth", choice)


def wifi_power(action):
    """Turn the WiFi radio on or off."""
    choice = str(action).strip().lower()
    if choice not in ("on", "off"):
        return err("Action must be 'on' or 'off'.")
    return _radio("wifi", choice)


# --------------------------------------------------------------------------
# Settings shortcuts
# --------------------------------------------------------------------------

SETTINGS_PAGES = {
    "bluetooth": "ms-settings:bluetooth",
    "wifi": "ms-settings:network-wifi",
    "network": "ms-settings:network",
    "display": "ms-settings:display",
    "sound": "ms-settings:sound",
    "battery": "ms-settings:batterysaver",
    "power": "ms-settings:powersleep",
    "storage": "ms-settings:storagesense",
    "apps": "ms-settings:appsfeatures",
    "printers": "ms-settings:printers",
    "phone": "ms-settings:mobile-devices",
    "privacy": "ms-settings:privacy",
    "update": "ms-settings:windowsupdate",
    "personalisation": "ms-settings:personalization",
    "accounts": "ms-settings:yourinfo",
    "radios": "ms-settings:privacy-radios",
}


def open_settings(page):
    """Open a specific Windows Settings page."""
    key = str(page).strip().lower()
    target = SETTINGS_PAGES.get(key)
    if target is None:
        match = next((v for k, v in SETTINGS_PAGES.items() if key and key in k), None)
        if match is None:
            return err("Unknown settings page. Options: " + ", ".join(sorted(SETTINGS_PAGES)) + ".")
        target = match
    try:
        webbrowser.open(target)
    except Exception as exc:
        return err("Could not open settings: " + str(exc))
    return ok(opened=target)


def network_info():
    """IP address and adapter summary for this PC."""
    script = (
        "Get-NetIPConfiguration -ErrorAction SilentlyContinue | "
        "Where-Object { $_.IPv4Address } | ForEach-Object { "
        "$_.InterfaceAlias + '|' + $_.IPv4Address.IPAddress + '|' + "
        "$(if ($_.IPv4DefaultGateway) { $_.IPv4DefaultGateway.NextHop } else { '-' }) }"
    )
    result = powershell(script)
    if not result["ok"]:
        return result

    adapters = []
    for line in result["output"].splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            adapters.append({"interface": parts[0].strip(),
                             "ip": parts[1].strip(),
                             "gateway": parts[2].strip()})
    if not adapters:
        return err("No active network adapters found.")
    return ok(count=len(adapters), adapters=adapters)


SCHEMAS = [
    tool("wifi_status", "Show which WiFi network this PC is connected to and the signal strength."),
    tool("wifi_networks", "Scan for WiFi networks currently in range."),
    tool("wifi_saved_networks", "List WiFi networks this PC already has saved passwords for."),
    tool("wifi_connect",
         "Connect to a saved WiFi network by name. Only works for networks Windows "
         "already has the password for.",
         {"name": string("The network name (SSID).")}, ["name"]),
    tool("wifi_disconnect", "Disconnect from the current WiFi network."),
    tool("wifi_power", "Turn the WiFi radio on or off.",
         {"action": string("on or off", ["on", "off"])}, ["action"]),
    tool("bluetooth_devices", "List Bluetooth devices paired with this PC and which are connected."),
    tool("bluetooth_power", "Turn Bluetooth on or off.",
         {"action": string("on or off", ["on", "off"])}, ["action"]),
    tool("network_info", "Show this PC's IP addresses and network adapters."),
    tool("open_settings",
         "Open a Windows Settings page, for example bluetooth, wifi, display, sound, battery.",
         {"page": string("Which settings page to open.")}, ["page"]),
]

REGISTRY = {
    "wifi_status": wifi_status,
    "wifi_networks": wifi_networks,
    "wifi_saved_networks": wifi_saved_networks,
    "wifi_connect": wifi_connect,
    "wifi_disconnect": wifi_disconnect,
    "wifi_power": wifi_power,
    "bluetooth_devices": bluetooth_devices,
    "bluetooth_power": bluetooth_power,
    "network_info": network_info,
    "open_settings": open_settings,
}
