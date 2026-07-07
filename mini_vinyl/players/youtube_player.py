"""Plays a YouTube video's audio via mpv (mpv resolves the stream itself
using yt-dlp), routed to the Bluetooth speaker through PipeWire - which is
what Raspberry Pi OS Bookworm/Trixie use for Bluetooth A2DP audio out of
the box (no bluealsa needed/wanted; running both fights over the BlueZ
audio profile).
"""

import subprocess

from mini_vinyl.config import TagEntry
from mini_vinyl.players.base import Player


class YoutubePlayer(Player):
    def __init__(self, audio_output: str = "pipewire"):
        self._audio_output = audio_output
        self._proc: subprocess.Popen | None = None

    def play(self, tag: TagEntry) -> None:
        self.stop()
        print(f"[youtube] playing {tag.id} ({tag.title})")
        self._proc = subprocess.Popen(
            [
                "mpv",
                "--no-video",
                "--ytdl-format=bestaudio",
                f"--ao={self._audio_output}",
                "--really-quiet",
                tag.id,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
