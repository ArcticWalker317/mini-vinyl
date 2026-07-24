"""Volume control via PipeWire's `wpctl` (already this project's audio
toolchain - see AUDIO_OUTPUT/players/youtube_player.py) against the
default audio sink, i.e. whatever's currently the active PipeWire
output - the paired Bluetooth speaker, once connected.

Not currently wired into anything - a web UI slider was tried and cut
for being too laggy (a full HTTP round trip plus a `wpctl` subprocess
call per drag movement isn't a good fit for "smooth slider" UX). Kept
around for a planned physical volume control on the Pi itself, which
these functions should work unchanged for.
"""

import re
import subprocess

_VOLUME_RE = re.compile(r"Volume:\s*([0-9.]+)\s*(\[MUTED\])?")


def get_volume() -> dict:
    try:
        proc = subprocess.run(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"level": None, "muted": None}
    match = _VOLUME_RE.search(proc.stdout)
    if not match:
        return {"level": None, "muted": None}
    return {"level": round(float(match.group(1)) * 100), "muted": match.group(2) is not None}


def set_volume(level: int) -> bool:
    level = max(0, min(100, level))
    try:
        proc = subprocess.run(
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level}%"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def set_mute(muted: bool) -> bool:
    try:
        proc = subprocess.run(
            ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if muted else "0"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0
