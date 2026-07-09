"""Plays a YouTube video's audio, routed to the Bluetooth speaker through
PipeWire - which is what Raspberry Pi OS Bookworm/Trixie use for
Bluetooth A2DP audio out of the box (no bluealsa needed/wanted; running
both fights over the BlueZ audio profile).

Resolving+streaming a YouTube URL via yt-dlp is slow on a Pi Zero W's weak
single-core CPU (tens of seconds), so the first play of a tag streams live
via mpv. If the tag is still playing CACHE_AFTER_SECONDS later (i.e. it
wasn't just a brief/accidental tap), a background download to disk as WAV
starts too - independent of the mpv process, so lifting the tag after
that point doesn't stop it. Later plays of that tag use `pw-play`
(PipeWire's own minimal player) instead of mpv - mpv's dependency stack
(FFmpeg, libplacebo, etc.) takes several seconds of pure CPU time just to
start on this hardware regardless of what it's playing, while pw-play
plays raw PCM/WAV with essentially no startup cost.

Lifting a tag mid-song and placing the *same* tag back on resumes from
where it got to (only once it's playing from the cached, near-instant
path - during the live phase a fresh mpv process always pays the full
yt-dlp resolve cost regardless of a --start offset, so there's nothing to
gain by tracking position there). Only one resume point is ever
remembered at a time: playing any *different* tag - even briefly -
invalidates it, so going back to the original later always starts from
the beginning rather than resuming a stale position. A song that finishes
playing to completion also clears its own resume point.

A tag whose URL is a YouTube *playlist* (has a `list=` query param) gets
its own path: every placement reshuffles and plays from track one - there
is no mid-playlist resume, unlike single videos. The first time a
playlist tag is seen, mpv streams it live with `--shuffle` (mpv resolves
and shuffles the playlist itself), and if it's still playing
CACHE_AFTER_SECONDS later a background yt-dlp job downloads every track
in the playlist to its own directory. Once that finishes, later
placements shuffle the list of local files in Python and play them back
to back through `pw-play`, one track at a time.
"""

import hashlib
import random
import subprocess
import threading
import time
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mini_vinyl.config import TagEntry
from mini_vinyl.players.base import Player

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "mini-vinyl" / "youtube"
CACHE_AFTER_SECONDS = 3.0


def _is_playlist_url(url: str) -> bool:
    return "list" in parse_qs(urlparse(url).query)


class YoutubePlayer(Player):
    def __init__(self, audio_output: str = "pipewire", cache_dir: Path | None = None):
        self._audio_output = audio_output
        self._proc: subprocess.Popen | None = None
        self._cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # The single most recently paused tag, if any. Starting a
        # *different* tag invalidates this outright (see play()).
        self._paused_url: str | None = None
        self._paused_position = 0.0

        self._current_url: str | None = None
        self._current_base_position = 0.0
        self._current_is_cached = False
        self._played_since: float | None = None
        self._caching_urls: set[str] = set()  # urls with a background cache job in flight
        self._cache_timer: threading.Timer | None = None

        # Playlist queue playback (cached path only - a background thread
        # feeds tracks to pw-play one at a time). Playlists never resume,
        # so there's no paused-position state to track for them.
        self._current_is_playlist_queue = False
        self._playlist_thread: threading.Thread | None = None
        self._playlist_stop_event: threading.Event | None = None
        self._caching_playlists: set[str] = set()  # playlist urls with a full download in flight

    def _cache_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self._cache_dir / f"{key}.wav"

    def _resume_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self._cache_dir / f"{key}.resume.wav"

    def _playlist_dir(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self._cache_dir / "playlists" / key

    def _cached_playlist_tracks(self, playlist_dir: Path) -> list[Path]:
        if not (playlist_dir / ".complete").exists():
            return []
        return sorted(playlist_dir.glob("*.wav"))

    def play(self, tag: TagEntry) -> None:
        self.stop()

        if tag.id != self._paused_url and self._paused_url is not None:
            self._resume_path(self._paused_url).unlink(missing_ok=True)
            self._paused_url = None
            self._paused_position = 0.0

        if _is_playlist_url(tag.id):
            self._play_playlist(tag)
            return

        cache_path = self._cache_path(tag.id)

        resume_at = self._paused_position if tag.id == self._paused_url else 0.0
        self._paused_url = None
        self._paused_position = 0.0

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
            self._current_is_cached = True
            self._current_base_position = resume_at
        else:
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
            self._current_is_cached = False
            self._current_base_position = 0.0
            if tag.id not in self._caching_urls:
                self._cache_timer = threading.Timer(
                    CACHE_AFTER_SECONDS, self._maybe_start_caching, args=(tag.id, cache_path)
                )
                self._cache_timer.daemon = True
                self._cache_timer.start()

        self._current_url = tag.id
        self._played_since = time.time()

    def _maybe_start_caching(self, url: str, cache_path: Path) -> None:
        # Fires CACHE_AFTER_SECONDS after play() started; only actually
        # cache if this tag is still the one playing (wasn't lifted early -
        # stop() cancels this timer, but guard here too against any race).
        if self._current_url == url and url not in self._caching_urls:
            self._caching_urls.add(url)
            self._cache_in_background(url, cache_path)

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
        except (wave.Error, OSError, OverflowError, EOFError) as exc:
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
                # Strip metadata (title/artist tags) so ffmpeg writes a plain
                # fmt+data WAV - extra chunks confuse Python's `wave` module
                # (used for resume-clip trimming) into misreading frame counts.
                "--postprocessor-args",
                "ffmpeg:-map_metadata -1",
                "-o",
                output_template,
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self._cache_timer is not None:
            self._cache_timer.cancel()
            self._cache_timer = None

        if self._current_is_playlist_queue:
            if self._playlist_stop_event is not None:
                self._playlist_stop_event.set()
            if self._proc is not None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            if self._playlist_thread is not None:
                self._playlist_thread.join(timeout=3)
            self._playlist_thread = None
            self._playlist_stop_event = None
            self._proc = None
            self._current_url = None
            self._current_is_playlist_queue = False
            self._played_since = None
            return

        if self._proc is None:
            return

        finished_naturally = self._proc.poll() is not None

        if not finished_naturally:
            if self._current_is_cached and self._current_url and self._played_since is not None:
                elapsed = time.time() - self._played_since
                self._paused_url = self._current_url
                self._paused_position = self._current_base_position + elapsed
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        elif self._current_is_cached and self._current_url:
            self._resume_path(self._current_url).unlink(missing_ok=True)

        self._proc = None
        self._current_url = None
        self._current_is_cached = False
        self._played_since = None

    def _play_playlist(self, tag: TagEntry) -> None:
        playlist_dir = self._playlist_dir(tag.id)
        tracks = self._cached_playlist_tracks(playlist_dir)

        if tracks:
            random.shuffle(tracks)
            print(f"[youtube] playing playlist {tag.id} from cache, shuffled ({len(tracks)} tracks)")
            self._start_playlist_queue(tracks)
        else:
            print(f"[youtube] playing playlist {tag.id} live, shuffled")
            self._proc = subprocess.Popen(
                [
                    "mpv",
                    "--no-video",
                    "--shuffle",
                    "--ytdl-format=bestaudio",
                    f"--ao={self._audio_output}",
                    "--really-quiet",
                    tag.id,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if tag.id not in self._caching_playlists:
                self._cache_timer = threading.Timer(
                    CACHE_AFTER_SECONDS,
                    self._maybe_start_caching_playlist,
                    args=(tag.id, playlist_dir),
                )
                self._cache_timer.daemon = True
                self._cache_timer.start()

        self._current_url = tag.id
        self._played_since = time.time()

    def _start_playlist_queue(self, tracks: list[Path]) -> None:
        self._current_is_playlist_queue = True
        self._proc = None
        stop_event = threading.Event()
        self._playlist_stop_event = stop_event
        thread = threading.Thread(
            target=self._run_playlist_queue, args=(tracks, stop_event), daemon=True
        )
        self._playlist_thread = thread
        thread.start()

    def _run_playlist_queue(self, tracks: list[Path], stop_event: threading.Event) -> None:
        for track_path in tracks:
            if stop_event.is_set():
                return
            proc = subprocess.Popen(
                ["pw-play", str(track_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._proc = proc
            proc.wait()
            if stop_event.is_set():
                return
        # Whole shuffled playlist played through to the end naturally.
        self._proc = None
        self._current_url = None
        self._current_is_playlist_queue = False

    def _maybe_start_caching_playlist(self, url: str, playlist_dir: Path) -> None:
        # Mirrors _maybe_start_caching: only start the (long) full-playlist
        # download if the tag wasn't just a brief/accidental tap.
        if self._current_url == url and url not in self._caching_playlists:
            self._caching_playlists.add(url)
            self._cache_playlist_in_background(url, playlist_dir)

    def _cache_playlist_in_background(self, url: str, playlist_dir: Path) -> None:
        playlist_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(playlist_dir / "%(playlist_index)s_%(id)s.%(ext)s")
        proc = subprocess.Popen(
            [
                "yt-dlp",
                "--yes-playlist",
                "-f",
                "bestaudio",
                "-x",
                "--audio-format",
                "wav",
                "--postprocessor-args",
                "ffmpeg:-map_metadata -1",
                "-o",
                output_template,
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        threading.Thread(
            target=self._finish_playlist_caching, args=(proc, playlist_dir, url), daemon=True
        ).start()

    def _finish_playlist_caching(
        self, proc: subprocess.Popen, playlist_dir: Path, url: str
    ) -> None:
        proc.wait()
        if proc.returncode == 0:
            (playlist_dir / ".complete").touch()
        else:
            print(f"[youtube] playlist download failed for {url} (exit {proc.returncode})")
        self._caching_playlists.discard(url)
