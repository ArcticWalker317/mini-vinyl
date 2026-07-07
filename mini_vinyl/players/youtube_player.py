"""Plays a YouTube video's audio via mpv, routed to the Bluetooth speaker
through PipeWire - which is what Raspberry Pi OS Bookworm/Trixie use for
Bluetooth A2DP audio out of the box (no bluealsa needed/wanted; running
both fights over the BlueZ audio profile).

Resolving+streaming a YouTube URL via yt-dlp is slow on a Pi Zero W's weak
CPU (tens of seconds). To avoid that wait on repeat plays, the first play
of a tag also caches just the first CLIP_SECONDS of audio to disk in the
background. On every later play of that tag: the cached clip starts
playing instantly, while a second mpv process resolves+seeks the live
stream to CLIP_SECONDS in and sits paused; the moment the clip finishes,
playback hands off to the live stream so the rest of the song continues
with no audible gap (as long as resolving finished within CLIP_SECONDS).
"""

import hashlib
import json
import socket
import subprocess
import threading
import uuid
from pathlib import Path

from mini_vinyl.config import TagEntry
from mini_vinyl.players.base import Player

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "mini-vinyl" / "youtube"
CLIP_SECONDS = 60


class YoutubePlayer(Player):
    def __init__(
        self,
        audio_output: str = "pipewire",
        cache_dir: Path | None = None,
        clip_seconds: int = CLIP_SECONDS,
    ):
        self._audio_output = audio_output
        self._clip_seconds = clip_seconds
        self._cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._proc: subprocess.Popen | None = None
        self._handoff_proc: subprocess.Popen | None = None
        self._handoff_thread: threading.Thread | None = None
        self._ipc_path: str | None = None
        self._stopping = threading.Event()

    def _cache_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self._cache_dir / f"{key}.opus"

    def _spawn_mpv(self, extra_args: list[str]) -> subprocess.Popen:
        return subprocess.Popen(
            ["mpv", "--no-video", f"--ao={self._audio_output}", "--really-quiet", *extra_args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def play(self, tag: TagEntry) -> None:
        self.stop()
        self._stopping.clear()
        cache_path = self._cache_path(tag.id)

        if cache_path.exists():
            print(f"[youtube] playing cached clip, handing off to live stream ({tag.title})")
            self._proc = self._spawn_mpv([str(cache_path)])

            self._ipc_path = f"/tmp/mini-vinyl-mpv-{uuid.uuid4().hex}.sock"
            self._handoff_proc = self._spawn_mpv(
                [
                    f"--start={self._clip_seconds}",
                    "--pause",
                    f"--input-ipc-server={self._ipc_path}",
                    "--ytdl-format=bestaudio",
                    tag.id,
                ]
            )
            self._handoff_thread = threading.Thread(
                target=self._handoff_when_clip_ends,
                args=(self._proc, self._ipc_path),
                daemon=True,
            )
            self._handoff_thread.start()
        else:
            print(f"[youtube] playing {tag.id} ({tag.title})")
            self._proc = self._spawn_mpv(["--ytdl-format=bestaudio", tag.id])
            self._cache_clip_in_background(tag.id, cache_path)

    def _handoff_when_clip_ends(self, clip_proc: subprocess.Popen, ipc_path: str) -> None:
        clip_proc.wait()
        if self._stopping.is_set():
            return  # tag was lifted / replaced mid-clip, not a natural finish
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(ipc_path)
                s.sendall(
                    json.dumps({"command": ["set_property", "pause", False]}).encode() + b"\n"
                )
        except OSError:
            pass  # live stream wasn't ready in time; accept the gap

    def _cache_clip_in_background(self, url: str, cache_path: Path) -> None:
        output_template = str(cache_path.with_suffix("")) + ".%(ext)s"
        subprocess.Popen(
            [
                "yt-dlp",
                "-f",
                "bestaudio",
                "-x",
                "--audio-format",
                "opus",
                "--download-sections",
                f"*0-{self._clip_seconds}",
                "-o",
                output_template,
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        self._stopping.set()
        for proc in (self._proc, self._handoff_proc):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._proc = None
        self._handoff_proc = None
        if self._ipc_path:
            Path(self._ipc_path).unlink(missing_ok=True)
        self._ipc_path = None
