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

Downloaded files are named `<song_title>-<artist>.wav` (e.g.
`the_scientist-coldplay.wav`), and every completed download is recorded
in `library.json` in the cache directory, keyed by the source URL, with
its title/artist/filename - that catalog is also how a tag's cached file
is found on later plays, since the filename can't be derived from the URL
alone. Title/artist come from yt-dlp's own metadata (falling back through
artist -> creator -> uploader -> channel, since regular YouTube videos
usually only have an uploader/channel, while YouTube Music tracks have a
proper artist tag).

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
in the playlist to its own directory, using the same
`<song_title>-<artist>.wav` naming and adding every track to the same
library.json. Once that finishes, later placements shuffle the list of
local files in Python and play them back to back through `pw-play`, one
track at a time.
"""

import hashlib
import json
import random
import re
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

# yt-dlp field-fallback template: prefer a proper artist tag (YouTube
# Music tracks have one), falling back to whoever uploaded the video.
_ARTIST_FIELD = "%(artist,creator,uploader,channel)s"
_METADATA_SEP = "\x1f"


def _is_playlist_url(url: str) -> bool:
    return "list" in parse_qs(urlparse(url).query)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "unknown"


class YoutubePlayer(Player):
    def __init__(self, audio_output: str = "pipewire", cache_dir: Path | None = None):
        self._audio_output = audio_output
        self._proc: subprocess.Popen | None = None
        self._cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._catalog_path = self._cache_dir / "library.json"
        self._catalog_lock = threading.Lock()
        self._catalog: dict[str, dict] = self._load_catalog()

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

    def _load_catalog(self) -> dict:
        if not self._catalog_path.exists():
            return {}
        try:
            return json.loads(self._catalog_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _catalog_put(self, url: str, title: str, artist: str, path: Path) -> None:
        with self._catalog_lock:
            self._catalog[url] = {
                "title": title,
                "artist": artist,
                "url": url,
                "file": str(path.relative_to(self._cache_dir)),
            }
            self._catalog_path.write_text(
                json.dumps(self._catalog, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    def _lookup_cached_file(self, url: str) -> Path | None:
        entry = self._catalog.get(url)
        if entry is None:
            return None
        path = self._cache_dir / entry["file"]
        return path if path.exists() else None

    def _resume_path(self, url: str) -> Path:
        # Ephemeral trimmed-clip scratch file, unrelated to the
        # library's <title>-<artist>.wav naming - one per url, overwritten
        # on every resume.
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self._cache_dir / f".resume_{key}.wav"

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

        cache_path = self._lookup_cached_file(tag.id)

        resume_at = self._paused_position if tag.id == self._paused_url else 0.0
        self._paused_url = None
        self._paused_position = 0.0

        if cache_path is not None:
            play_path = cache_path
            if resume_at > 0:
                trimmed = self._build_resume_clip(cache_path, tag.id, resume_at)
                if trimmed is not None:
                    play_path = trimmed
                else:
                    resume_at = 0.0  # trim failed or resume point past the end
            title = self._catalog.get(tag.id, {}).get("title", tag.id)
            print(f"[youtube] playing {title!r} from cache at {resume_at:.0f}s")
            self._proc = subprocess.Popen(
                ["pw-play", str(play_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._current_is_cached = True
            self._current_base_position = resume_at
        else:
            print(f"[youtube] playing {tag.id}")
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
                    CACHE_AFTER_SECONDS, self._maybe_start_caching, args=(tag.id,)
                )
                self._cache_timer.daemon = True
                self._cache_timer.start()

        self._current_url = tag.id
        self._played_since = time.time()

    def _maybe_start_caching(self, url: str) -> None:
        # Fires CACHE_AFTER_SECONDS after play() started; only actually
        # cache if this tag is still the one playing (wasn't lifted early -
        # stop() cancels this timer, but guard here too against any race).
        if self._current_url == url and url not in self._caching_urls:
            self._caching_urls.add(url)
            threading.Thread(target=self._download_and_catalog, args=(url,), daemon=True).start()

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

    def _download_and_catalog(self, url: str) -> None:
        try:
            tmp_template = str(self._cache_dir / "%(id)s.%(ext)s")
            proc = subprocess.run(
                [
                    "yt-dlp",
                    "--quiet",
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
                    "--print",
                    f"after_move:%(title)s{_METADATA_SEP}{_ARTIST_FIELD}{_METADATA_SEP}%(filepath)s",
                    "-o",
                    tmp_template,
                    url,
                ],
                capture_output=True,
                text=True,
            )
            lines = proc.stdout.strip().splitlines()
            if proc.returncode != 0 or not lines:
                print(f"[youtube] cache download failed for {url}: {proc.stderr.strip()[-500:]}")
                return

            title, artist, filepath = lines[-1].split(_METADATA_SEP)
            final_path = self._finalize_download(Path(filepath), title, artist)
            if final_path is not None:
                self._catalog_put(url, title, artist, final_path)
        finally:
            self._caching_urls.discard(url)

    def _finalize_download(
        self, tmp_path: Path, title: str, artist: str, dest_dir: Path | None = None
    ) -> Path | None:
        if not tmp_path.exists():
            return None
        dest_dir = dest_dir or self._cache_dir
        final_path = self._unique_path(dest_dir, f"{_slugify(title)}-{_slugify(artist)}.wav")
        tmp_path.rename(final_path)
        return final_path

    def _unique_path(self, dest_dir: Path, filename: str) -> Path:
        path = dest_dir / filename
        if not path.exists():
            return path
        stem, suffix = path.stem, path.suffix
        n = 2
        while True:
            candidate = dest_dir / f"{stem}_{n}{suffix}"
            if not candidate.exists():
                return candidate
            n += 1

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
        output_template = str(playlist_dir / "%(id)s.%(ext)s")
        proc = subprocess.Popen(
            [
                "yt-dlp",
                "--quiet",
                "--yes-playlist",
                "-f",
                "bestaudio",
                "-x",
                "--audio-format",
                "wav",
                "--postprocessor-args",
                "ffmpeg:-map_metadata -1",
                "--print",
                f"after_move:%(title)s{_METADATA_SEP}{_ARTIST_FIELD}{_METADATA_SEP}"
                f"%(webpage_url)s{_METADATA_SEP}%(filepath)s",
                "-o",
                output_template,
                url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        threading.Thread(
            target=self._finish_playlist_caching, args=(proc, playlist_dir, url), daemon=True
        ).start()

    def _finish_playlist_caching(
        self, proc: subprocess.Popen, playlist_dir: Path, url: str
    ) -> None:
        stdout, _ = proc.communicate()
        if proc.returncode == 0:
            for line in stdout.strip().splitlines():
                parts = line.split(_METADATA_SEP)
                if len(parts) != 4:
                    continue
                title, artist, track_url, filepath = parts
                final_path = self._finalize_download(
                    Path(filepath), title, artist, dest_dir=playlist_dir
                )
                if final_path is not None:
                    self._catalog_put(track_url, title, artist, final_path)
            (playlist_dir / ".complete").touch()
        else:
            print(f"[youtube] playlist download failed for {url} (exit {proc.returncode})")
        self._caching_playlists.discard(url)
