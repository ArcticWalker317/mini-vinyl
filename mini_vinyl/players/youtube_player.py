"""Plays a YouTube video's audio, routed to the Bluetooth speaker through
PipeWire - which is what Raspberry Pi OS Bookworm/Trixie use for
Bluetooth A2DP audio out of the box (no bluealsa needed/wanted; running
both fights over the BlueZ audio profile).

A tag's content (`TagEntry.id`) is either a raw YouTube URL (written
directly to the tag, the original workflow) or a bare "code" - a
`<song_title>-<artist>` string with no URL scheme, burned onto the tag by
the web "search & add" UI (mini_vinyl/web.py) after a song has already
been added to the library. Catalog/download/naming logic all lives in
mini_vinyl/library.py's `Library`, shared with the web UI; this module
only handles the actual playback process management.

Resolving+streaming a YouTube URL via yt-dlp is slow on a Pi Zero W's weak
single-core CPU (tens of seconds), so the first play of an uncached
URL-tag streams live via mpv. If it's still playing CACHE_AFTER_SECONDS
later (i.e. it wasn't just a brief/accidental tap), a background download
to disk as WAV starts too - independent of the mpv process, so lifting
the tag after that point doesn't stop it. A code-tag has no live-stream
fallback at all: a code only ever exists once the web UI's Add flow has
already reserved/started downloading it, so a code with nothing cached
yet just logs an error and plays nothing.

Every cached play (whether reached via a URL-tag cache hit or a
code-tag) uses `pw-play` (PipeWire's own minimal player) instead of mpv -
mpv's dependency stack (FFmpeg, libplacebo, etc.) takes several seconds
of pure CPU time just to start on this hardware regardless of what it's
playing, while pw-play plays raw PCM/WAV with essentially no startup
cost.

Lifting a tag mid-song and placing the *same* tag back on resumes from
where it got to (only once it's playing from the cached, near-instant
path - during the live phase a fresh mpv process always pays the full
yt-dlp resolve cost regardless of a --start offset, so there's nothing to
gain by tracking position there). The resume point is keyed by the
underlying YouTube URL, not the tag's raw content, so retapping a
code-tag resumes correctly too (see _resolve_resume_key). Only one resume
point is ever remembered at a time: playing any *different* tag - even
briefly - invalidates it, so going back to the original later always
starts from the beginning rather than resuming a stale position. A song
that finishes playing to completion also clears its own resume point.

A tag whose URL is a YouTube *playlist* (has a `list=` query param) gets
its own path: every placement reshuffles and plays from track one - there
is no mid-playlist resume, unlike single videos, and playlists have no
code-tag equivalent (they're not reachable through the web Add flow).
The first time a playlist tag is seen, mpv streams it live with
`--shuffle` (mpv resolves and shuffles the playlist itself), and if it's
still playing CACHE_AFTER_SECONDS later a background yt-dlp job (via
Library) downloads every track in the playlist to its own directory.
Once that finishes, later placements shuffle the list of local files in
Python and play them back to back through `pw-play`, one track at a
time.
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
from mini_vinyl.library import Library
from mini_vinyl.players.base import Player

CACHE_AFTER_SECONDS = 3.0


def _is_playlist_url(url: str) -> bool:
    return "list" in parse_qs(urlparse(url).query)


def _looks_like_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


class YoutubePlayer(Player):
    def __init__(self, library: Library, audio_output: str = "pipewire"):
        self._library = library
        self._audio_output = audio_output
        self._proc: subprocess.Popen | None = None

        # The single most recently paused url, if any. Starting a
        # *different* tag (by resolved url) invalidates this outright.
        self._paused_url: str | None = None
        self._paused_position = 0.0

        self._current_url: str | None = None
        self._current_base_position = 0.0
        self._current_is_cached = False
        self._played_since: float | None = None
        self._cache_timer: threading.Timer | None = None

        # Playlist queue playback (cached path only - a background thread
        # feeds tracks to pw-play one at a time). Playlists never resume,
        # so there's no paused-position state to track for them.
        self._current_is_playlist_queue = False
        self._playlist_thread: threading.Thread | None = None
        self._playlist_stop_event: threading.Event | None = None

    def _resume_path(self, url: str) -> Path:
        # Ephemeral trimmed-clip scratch file, unrelated to the
        # library's <title>-<artist>.wav naming - one per url, overwritten
        # on every resume.
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self._library.cache_dir / f".resume_{key}.wav"

    def _resolve_resume_key(self, tag_id: str) -> str | None:
        """The underlying YouTube URL a tag's resume state is keyed by -
        the tag's own id for a URL-tag, or the looked-up url for a
        code-tag (None if the code isn't in the library at all)."""
        if _looks_like_url(tag_id):
            return tag_id
        entry = self._library.get_by_code(tag_id)
        return entry["url"] if entry else None

    def _reset_stale_pause(self, resume_key: str | None) -> None:
        if resume_key != self._paused_url and self._paused_url is not None:
            self._resume_path(self._paused_url).unlink(missing_ok=True)
            self._paused_url = None
            self._paused_position = 0.0

    def _take_resume(self, resume_key: str | None) -> float:
        resume_at = (
            self._paused_position if resume_key is not None and resume_key == self._paused_url else 0.0
        )
        self._paused_url = None
        self._paused_position = 0.0
        return resume_at

    def play(self, tag: TagEntry) -> None:
        self.stop()

        resume_key = self._resolve_resume_key(tag.id)
        self._reset_stale_pause(resume_key)

        if _looks_like_url(tag.id):
            if _is_playlist_url(tag.id):
                self._play_playlist(tag)
            else:
                self._play_url(tag.id, resume_key)
        else:
            self._play_code(tag.id, resume_key)

    def _play_url(self, url: str, resume_key: str | None) -> None:
        resume_at = self._take_resume(resume_key)

        entry = self._library.get_by_url(url)
        if entry is not None:
            self._play_from_cache(url, entry, resume_at)
            return

        print(f"[youtube] playing {url}")
        self._proc = subprocess.Popen(
            [
                "mpv",
                "--no-video",
                "--ytdl-format=bestaudio",
                f"--ao={self._audio_output}",
                "--really-quiet",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._current_is_cached = False
        self._current_base_position = 0.0
        self._cache_timer = threading.Timer(CACHE_AFTER_SECONDS, self._maybe_start_caching, args=(url,))
        self._cache_timer.daemon = True
        self._cache_timer.start()

        self._current_url = url
        self._played_since = time.time()

    def _play_code(self, code: str, resume_key: str | None) -> None:
        entry = self._library.get_by_code(code)
        if entry is None:
            print(f"[youtube] no downloaded song found for code {code!r}")
            return
        resume_at = self._take_resume(resume_key)
        self._play_from_cache(entry["url"], entry, resume_at)

    def _play_from_cache(self, resume_key: str, entry: dict, resume_at: float) -> None:
        cache_path = self._library.path_for(entry)
        play_path = cache_path
        if resume_at > 0:
            trimmed = self._build_resume_clip(cache_path, resume_key, resume_at)
            if trimmed is not None:
                play_path = trimmed
            else:
                resume_at = 0.0  # trim failed or resume point past the end

        print(f"[youtube] playing {entry['title']!r} from cache at {resume_at:.0f}s")
        self._proc = subprocess.Popen(
            ["pw-play", str(play_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._current_is_cached = True
        self._current_base_position = resume_at
        self._current_url = resume_key
        self._played_since = time.time()

    def _maybe_start_caching(self, url: str) -> None:
        # Fires CACHE_AFTER_SECONDS after play() started; only actually
        # cache if this url is still the one playing (wasn't lifted early -
        # stop() cancels this timer, but guard here too against any race).
        # Library.download_and_catalog no-ops on its own if a download for
        # this url is already in flight, so it's fine to call this more
        # than once for the same url.
        if self._current_url == url:
            threading.Thread(target=self._library.download_and_catalog, args=(url,), daemon=True).start()

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
        playlist_dir = self._library.playlist_dir(tag.id)
        tracks = self._library.cached_playlist_tracks(playlist_dir)

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
            self._cache_timer = threading.Timer(
                CACHE_AFTER_SECONDS,
                self._library.maybe_start_playlist_download,
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
