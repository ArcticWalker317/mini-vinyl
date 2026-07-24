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

Scanning is more involved than the other commands here because `scan on`
isn't a request/response call like `connect` or `info` - it's a
long-running background operation that streams `[NEW] Device ...` lines
to bluetoothctl's own stdout as it discovers things. That means a single
one-shot `bluetoothctl scan on` invocation doesn't work: bluetoothctl
starts the scan and the process exits right away, before anything's been
discovered. scan() instead keeps a bluetoothctl process alive on a pipe,
sends "scan on", sleeps in *this* process for the scan window while a
background thread continuously drains its stdout (so the OS pipe buffer
never fills up and blocks bluetoothctl mid-write), then sends "scan off"
+ "quit" and reads back everything that was collected.

list_paired() similarly went through a real bug: an earlier version used
`bluetoothctl paired-devices`, which isn't an actual bluetoothctl
command - it silently produced no output, so every device looked
unpaired no matter what. The correct, version-portable way is `devices`
(BlueZ's full known-device list, paired or not) filtered down by
checking each one's `info` output for `Paired: yes`.

None of this has been exercised against real hardware by anyone but the
person using it - if pairing/scanning still misbehaves, the [bluetooth]
lines this module prints to the server's own console are the place to
look; several of the "should never happen" branches below log the raw
bluetoothctl output specifically so a future failure is debuggable
without needing to add logging first.
"""

import re
import subprocess
import threading
import time

_MAC_RE = r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}"
_DEVICE_LINE_RE = re.compile(rf"^Device ({_MAC_RE})\s+(.*)$")
_NEW_DEVICE_RE = re.compile(rf"^\[NEW\] Device ({_MAC_RE})\s+(.*)$")
_CONNECTED_RE = re.compile(r"^\s*Connected:\s*(yes|no)\s*$", re.MULTILINE)
_PAIRED_RE = re.compile(r"^\s*Paired:\s*(yes|no)\s*$", re.MULTILINE)

SCAN_SECONDS = 10


def _run(args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout)


def _device_info(mac: str) -> str:
    try:
        proc = _run(["info", mac])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout


def is_connected(mac: str) -> bool:
    match = _CONNECTED_RE.search(_device_info(mac))
    return bool(match and match.group(1) == "yes")


def list_paired() -> list[dict]:
    try:
        proc = _run(["devices"])
    except FileNotFoundError:
        print("[bluetooth] bluetoothctl not found")
        return []
    except subprocess.TimeoutExpired:
        print("[bluetooth] 'devices' timed out")
        return []

    devices = []
    for line in proc.stdout.splitlines():
        match = _DEVICE_LINE_RE.match(line.strip())
        if not match:
            continue
        mac, name = match.groups()
        info = _device_info(mac)
        paired_match = _PAIRED_RE.search(info)
        if not (paired_match and paired_match.group(1) == "yes"):
            continue  # known to bluetoothd (e.g. seen in a past scan) but never paired
        connected_match = _CONNECTED_RE.search(info)
        connected = bool(connected_match and connected_match.group(1) == "yes")
        devices.append({"mac": mac, "name": name or mac, "connected": connected})
    return devices


def scan(seconds: int = SCAN_SECONDS) -> list[dict]:
    """Discovers nearby devices for `seconds`, excluding ones already
    paired (those show up via list_paired() instead)."""
    try:
        _run(["power", "on"], timeout=10)
    except FileNotFoundError:
        print("[bluetooth] bluetoothctl not found")
        return []
    except subprocess.TimeoutExpired:
        pass  # non-fatal - proceed and let the scan itself surface any real problem

    try:
        proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        print("[bluetooth] bluetoothctl not found")
        return []

    lines: list[str] = []

    def _drain_stdout() -> None:
        # Must keep reading for as long as the process is alive, even
        # after we've stopped caring about the content, or bluetoothctl
        # can block on a full stdout pipe and never see our later writes.
        for line in proc.stdout:
            lines.append(line)

    reader = threading.Thread(target=_drain_stdout, daemon=True)
    reader.start()

    try:
        proc.stdin.write("scan on\n")
        proc.stdin.flush()
        time.sleep(seconds)
        proc.stdin.write("scan off\n")
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        proc.wait(timeout=10)
    except (BrokenPipeError, subprocess.TimeoutExpired) as exc:
        print(f"[bluetooth] scan session ended unexpectedly: {exc!r}")
    finally:
        if proc.poll() is None:
            proc.kill()
        reader.join(timeout=3)

    output = "".join(lines)
    seen: dict[str, str] = {}
    for line in output.splitlines():
        match = _NEW_DEVICE_RE.match(line.strip())
        if match:
            mac, name = match.groups()
            seen.setdefault(mac, name or mac)

    if not seen:
        print(f"[bluetooth] scan found nothing; raw session output was:\n{output}")

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
        print(f"[bluetooth] pairing {mac} failed; raw output was:\n{output}")
        return (
            False,
            "pairing failed - make sure the device is in pairing mode, or it may need a "
            "passkey this page can't provide",
        )
    print(f"[bluetooth] unexpected pairing output for {mac}:\n{output}")
    return False, "unexpected response from bluetoothctl"


def connect(mac: str) -> bool:
    try:
        proc = _run(["connect", mac], timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    ok = "Connection successful" in proc.stdout
    if not ok:
        print(f"[bluetooth] connect {mac} failed; raw output was:\n{proc.stdout}")
    return ok


def disconnect(mac: str) -> bool:
    try:
        proc = _run(["disconnect", mac], timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    ok = "Successful disconnected" in proc.stdout
    if not ok:
        print(f"[bluetooth] disconnect {mac} failed; raw output was:\n{proc.stdout}")
    return ok
