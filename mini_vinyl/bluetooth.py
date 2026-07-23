"""Shells out to `bluetoothctl` (BlueZ's CLI) for the web UI's Settings
page - listing paired devices, scanning for new ones, and
pairing/connecting/disconnecting. There's no persistent Bluetooth state
kept in this project; every call here reflects BlueZ's own live state
directly, the same as running these commands by hand in a terminal (see
the README's original "Bluetooth pairing" walkthrough - this supplements
it, not replaces it; if pairing from here doesn't work on your BlueZ
version, that manual route still does).

Pairing runs a short scripted `bluetoothctl` session (a sequence of
commands piped in over stdin) that registers a NoInputNoOutput agent
before pairing, so BlueZ auto-accepts "Just Works" pairing - used by the
overwhelming majority of Bluetooth speakers/headphones - without needing
an interactive yes/no confirmation on either side. A device that
specifically requires a passkey entry/numeric comparison won't be
pairable this way; it'd still need the manual bluetoothctl route.

Scanning uses `bluetoothctl --timeout N scan on`, which runs discovery
for N seconds and exits on its own - available in reasonably recent BlueZ
(5.65+). Neither this nor the pairing flow has been exercised against
real hardware; both are a best effort at scripting bluetoothctl's normal
interactive behavior non-interactively, worth confirming on the Pi.
"""

import re
import subprocess

_MAC_RE = r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}"
_DEVICE_LINE_RE = re.compile(rf"^Device ({_MAC_RE})\s+(.*)$")
_NEW_DEVICE_RE = re.compile(rf"^\[NEW\] Device ({_MAC_RE})\s+(.*)$")
_CONNECTED_RE = re.compile(r"^\s*Connected:\s*(yes|no)\s*$", re.MULTILINE)

SCAN_SECONDS = 10


def _run(args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout)


def is_connected(mac: str) -> bool:
    try:
        proc = _run(["info", mac])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    match = _CONNECTED_RE.search(proc.stdout)
    return bool(match and match.group(1) == "yes")


def list_paired() -> list[dict]:
    try:
        proc = _run(["paired-devices"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    devices = []
    for line in proc.stdout.splitlines():
        match = _DEVICE_LINE_RE.match(line.strip())
        if match:
            mac, name = match.groups()
            devices.append({"mac": mac, "name": name or mac, "connected": is_connected(mac)})
    return devices


def scan(seconds: int = SCAN_SECONDS) -> list[dict]:
    """Discovers nearby devices for `seconds`, excluding ones already
    paired (those show up via list_paired() instead)."""
    try:
        _run(["power", "on"], timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    output = ""
    try:
        proc = _run(["--timeout", str(seconds), "scan", "on"], timeout=seconds + 10)
        output = proc.stdout
    except FileNotFoundError:
        return []
    except subprocess.TimeoutExpired as exc:
        # --timeout should make bluetoothctl exit on its own; this is a
        # safety net in case that flag isn't supported on this BlueZ
        # version and `scan on` just kept running until we force-killed
        # it - still salvage whatever was discovered before then.
        output = exc.stdout or ""

    seen: dict[str, str] = {}
    for line in output.splitlines():
        match = _NEW_DEVICE_RE.match(line.strip())
        if match:
            mac, name = match.groups()
            seen.setdefault(mac, name or mac)

    paired_macs = {d["mac"] for d in list_paired()}
    return [{"mac": mac, "name": name} for mac, name in seen.items() if mac not in paired_macs]


def pair(mac: str) -> tuple[bool, str]:
    script = f"agent NoInputNoOutput\ndefault-agent\npair {mac}\ntrust {mac}\nconnect {mac}\nquit\n"
    try:
        proc = subprocess.run(
            ["bluetoothctl"], input=script, capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        return False, "bluetoothctl not found"
    except subprocess.TimeoutExpired:
        return False, "timed out - the device may not be in pairing mode"

    output = proc.stdout
    if "Pairing successful" in output or "AlreadyExists" in output:
        return True, ""
    if "Failed to pair" in output:
        return (
            False,
            "pairing failed - make sure the device is in pairing mode, or it may need a "
            "passkey this page can't provide",
        )
    return False, "unexpected response from bluetoothctl"


def connect(mac: str) -> bool:
    try:
        proc = _run(["connect", mac], timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return "Connection successful" in proc.stdout


def disconnect(mac: str) -> bool:
    try:
        proc = _run(["disconnect", mac], timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return "Successful disconnected" in proc.stdout
