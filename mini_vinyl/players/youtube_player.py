"""Plays a YouTube video's audio, routed to the Bluetooth speaker through
PipeWire - which is what Raspberry Pi OS Bookworm/Trixie use for
Bluetooth A2DP audio out of the box (no bluealsa needed/wanted; running
both fights over the BlueZ audio profile).

Resolving+streaming a YouTube URL via yt-dlp is slow on a Pi Zero W's weak
single-core CPU (tens of seconds), so the first play of a tag streams live
via mpv and also downloads the full audio to disk as WAV in the
background. Later plays of that tag use `pw-play` (PipeWire's own minimal
player) instead of mpv - mpv's dependency stack (FFmpeg, libplacebo, etc.)
takes several seconds of pure CPU time just to start on this hardware
regardless of what it's playing, while pw-play plays raw PCM/WAV with
essentially no startup cost.
"""

import hashlib
import subprocess
from pathlib import Path

from mini_vinyl.config import TagEntry
from mini_vinyl.players.base import Player

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "mini-vinyl" / "youtube"


class YoutubePlayer(Player):
    def __init__(self, audio_output: str = "pipewire", cache_dir: Path | None = None):
        self._audio_output = audio_output
        self._proc: subprocess.Popen | None = None
        self._cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self._cache_dir / f"{key}.wav"

    def play(self, tag: TagEntry) -> None:
        self.stop()
        cache_path = self._cache_path(tag.id)

        if cache_path.exists():
            print(f"[youtube] playing {tag.id} from cache ({tag.title})")
            self._proc = subprocess.Popen(
                ["pw-play", str(cache_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return

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
        self._cache_in_background(tag.id, cache_path)

    def _cache_in_background(self, url: str, cache_path: Path) -> None:
        output_template = str(cache_path.with_suffix("")) + ".%(ext)s"
        subprocess.Popen(
            [
                "yt-dlp",
                "-f",
                "bestaudio",
                "-x",
                "--audio-format",
                "wav",
                "-o",
                output_template,
                url,
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
