"""Reads WiFi connection status for the web UI's Settings page - purely
informational (SSID/signal/IP); there's no way to switch networks from
here. Assumes NetworkManager (`nmcli`), the default on Raspberry Pi OS
Bookworm/Trixie (this project's documented target - see README); an
older Bullseye-based image using wpa_supplicant/dhcpcd directly instead
won't have `nmcli`, and this just reports everything as unknown rather
than erroring.
"""

import subprocess


def wifi_status() -> dict:
    ssid = None
    signal = None
    try:
        proc = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid,signal", "dev", "wifi"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in proc.stdout.splitlines():
            # nmcli terse output escapes literal colons within a field as
            # "\:"; a naive split like this mis-parses an SSID that
            # contains one - an accepted, unlikely-to-matter limitation.
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] == "yes":
                ssid = parts[1] or None
                try:
                    signal = int(parts[2])
                except ValueError:
                    signal = None
                break
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    ip = None
    try:
        proc = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
        candidates = proc.stdout.split()
        ip = candidates[0] if candidates else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return {"ssid": ssid, "signal": signal, "ip": ip}
