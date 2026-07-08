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

Lifting a tag mid-song remembers how far it got (per URL); placing the
same tag back on resumes from there. A different tag always starts from
the beginning, and a song that finishes naturally resets its own resume
point back to 0.
"""

import hashlib
import subprocess
import time
import wave
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

        self._positions: dict[str, float] = {}  # url -> resume point, seconds
        self._current_url: str | None = None
        self._current_base_position = 0.0
        self._played_since: float | None = None

    def _cache_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self._cache_dir / f"{key}.wav"

    def _resume_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self._cache_dir / f"{key}.resume.wav"

    def play(self, tag: TagEntry) -> None:
        self.stop()
        cache_path = self._cache_path(tag.id)
        resume_at = self._positions.get(tag.id, 0.0)

        if cache_path.exists():
            play_path = cache_path
            if resume_at > 0:
                trimmed = self._build_resume_clip(cache_path, tag.id, resume_at)
                if trimmed is not None:
                    play_path = trimmed
                else:
                    resume_at = 0.0  # trim failed or resume point past the end
            print(f"[youtube] playing {tag.id} from cache at {resume_at:.0f}s ({tag.title})")
            self._proc = subprocess.Popen(
                ["pw-play", str(play_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            print(f"[youtube] playing {tag.id} at {resume_at:.0f}s ({tag.title})")
            mpv_args = [
                "mpv",
                "--no-video",
                "--ytdl-format=bestaudio",
                f"--ao={self._audio_output}",
                "--really-quiet",
            ]
            if resume_at > 0:
                mpv_args.append(f"--start={resume_at}")
            mpv_args.append(tag.id)
            self._proc = subprocess.Popen(
                mpv_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self._cache_in_background(tag.id, cache_path)

        self._current_url = tag.id
        self._current_base_position = resume_at
        self._played_since = time.time()

    def _build_resume_clip(self, cache_path: Path, url: str, start_seconds: float) -> Path | None:
        try:
            with wave.open(str(cache_path), "rb") as src:
                frame_rate = src.getframerate()
                total_frames = src.getnframes()
                start_frame = int(start_seconds * frame_rate)
                if start_frame >= total_frames:
                    return None
                src.setpos(start_frame)
                remaining = src.readframes(total_frames - start_frame)
                params = src.getparams()

            resume_path = self._resume_path(url)
            with wave.open(str(resume_path), "wb") as dst:
                dst.setparams(params)
                dst.writeframes(remaining)
            return resume_path
        except (wave.Error, OSError) as exc:
            print(f"[youtube] couldn't build resume clip, starting over: {exc}")
            return None

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
        if self._proc is None:
            return

        finished_naturally = self._proc.poll() is not None

        if not finished_naturally:
            if self._current_url and self._played_since is not None:
                elapsed = time.time() - self._played_since
                self._positions[self._current_url] = self._current_base_position + elapsed
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        elif self._current_url:
            self._positions.pop(self._current_url, None)
            self._resume_path(self._current_url).unlink(missing_ok=True)

        self._proc = None
        self._current_url = None
        self._played_since = None
