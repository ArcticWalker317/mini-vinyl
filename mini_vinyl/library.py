"""The song catalog shared between playback (mini_vinyl/players/youtube_player.py)
and the web "search & add" UI (mini_vinyl/web.py).

Every downloaded song - whether triggered by tapping an unrecognized
YouTube-URL tag and leaving it playing for a few seconds, or by adding it
from the web UI - ends up as a `<song_title>-<artist>.wav` file in the
cache directory, recorded in `library.json` alongside it. A "code" is
just that filename without the `.wav` extension (e.g.
`the_scientist-coldplay`); it's what gets burned onto a physical tag by
the web UI's write flow and looked up here at playback time.

Adding a song from the web UI is a two-phase, queue-backed process rather
than something that happens inline in the HTTP request, because a single
probe (metadata-only, still needed to compute a code) can take the better
part of a minute on a Pi Zero W's weak single core (see probe()), and the
whole point is to let someone queue up a stack of songs from their phone
and walk away - firing off 20 requests that each block on their own
yt-dlp probe would either serialize into a very long wait or, if fired
concurrently, pile up several yt-dlp/ffmpeg processes competing for that
one weak core at once. So enqueue() just records the url with
status="queued" and returns immediately; a single background worker
thread (started in __init__, alive for the process's lifetime) drains the
queue strictly one url at a time - probe, reserve a unique filename/code,
download, finalize - before moving to the next. Entries persist through
every stage (see _status_dict_locked's status values), so the web UI can
poll list_entries() to show progress and is free to be closed and reopened
- or the phone turned off entirely - without losing anything; the queue
itself is also durable across a process restart (see _requeue_unfinished).

A failed download's reservation (once one exists) is never freed for
reuse - once a code might be sitting on a physical tag, that filename is
permanently spoken for, download or no download; a retry just resumes
from the download step rather than re-probing or re-slugifying.
"""

import hashlib
import json
import queue
import re
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "mini-vinyl" / "youtube"

# Same fallback order used both when picking an artist in Python (probe())
# and in yt-dlp's own --print field-fallback syntax (_ARTIST_TEMPLATE) -
# regular YouTube videos usually only have an uploader/channel, while
# YouTube Music tracks have a proper artist tag.
_ARTIST_FIELDS = ("artist", "creator", "uploader", "channel")
_ARTIST_TEMPLATE = "%(artist,creator,uploader,channel)s"
_SEP = "\x1f"

_MAX_TITLE_SLUG_LEN = 40
_MAX_ARTIST_SLUG_LEN = 30


def _slugify(text: str, max_len: int | None = None) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    if max_len is not None:
        slug = slug[:max_len].rstrip("_")
    return slug or "unknown"


def _pick_artist(info: dict) -> str:
    for key in _ARTIST_FIELDS:
        value = info.get(key)
        if value:
            return value
    return "unknown"


def _code_of(entry: dict) -> str | None:
    return Path(entry["file"]).stem if entry.get("file") else None


class Library:
    def __init__(self, cache_dir: Path | None = None):
        self._cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._entries_path = self._cache_dir / "library.json"
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = self._load()

        self._in_flight: set[str] = set()  # single-track urls with a download running
        self._caching_playlists: set[str] = set()  # playlist urls with a download running

        # Single sequential worker: on a one-core Pi Zero W, running two
        # yt-dlp/ffmpeg processes "concurrently" doesn't get anything done
        # faster, just makes both slower - so the add-queue is drained one
        # url at a time, on purpose.
        self._download_queue: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._worker_loop, daemon=True).start()
        self._requeue_unfinished()

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    # ---- persistence ----

    def _load(self) -> dict:
        if not self._entries_path.exists():
            return {}
        try:
            entries = json.loads(self._entries_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        # library.json predates the add-queue's "status" field - every
        # entry it ever wrote was already a finished download, so
        # anything missing one gets treated as "ready" from here on.
        for entry in entries.values():
            entry.setdefault("status", "ready")
        return entries

    def _save_locked(self) -> None:
        self._entries_path.write_text(
            json.dumps(self._entries, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _put_ready_locked(self, url: str, title: str, artist: str, path: Path) -> None:
        self._entries[url] = {
            "title": title,
            "artist": artist,
            "url": url,
            "file": str(path.relative_to(self._cache_dir)),
            "status": "ready",
        }
        self._save_locked()

    def _status_dict_locked(self, entry: dict) -> dict:
        return {
            "url": entry["url"],
            "status": entry["status"],
            "code": _code_of(entry),
            "title": entry.get("title"),
            "artist": entry.get("artist"),
            "detail": entry.get("detail", ""),
        }

    def _unique_filename_locked(self, dest_dir: Path, filename: str, *, check_catalog: bool) -> str:
        def taken(name: str) -> bool:
            if (dest_dir / name).exists():
                return True
            if check_catalog:
                return any(
                    e.get("file") and Path(e["file"]) == Path(name) for e in self._entries.values()
                )
            return False

        if not taken(filename):
            return filename
        stem, suffix = Path(filename).stem, Path(filename).suffix
        n = 2
        while taken(f"{stem}_{n}{suffix}"):
            n += 1
        return f"{stem}_{n}{suffix}"

    # ---- playback-time lookups (used by YoutubePlayer) ----

    def get_by_url(self, url: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(url)
            if entry is None or entry.get("status") != "ready":
                return None
            return entry if (self._cache_dir / entry["file"]).exists() else None

    def get_by_code(self, code: str) -> dict | None:
        # Only top-level cache_dir entries are code-addressable - playlist
        # tracks live under playlists/<hash>/ (more than one path part) and
        # never go through the add-queue, so they're excluded here by
        # construction rather than an explicit check.
        with self._lock:
            for entry in self._entries.values():
                if entry.get("status") != "ready" or not entry.get("file"):
                    continue
                file_rel = Path(entry["file"])
                if len(file_rel.parts) == 1 and file_rel.stem == code:
                    return entry if (self._cache_dir / file_rel).exists() else None
        return None

    def path_for(self, entry: dict) -> Path:
        return self._cache_dir / entry["file"]

    def code_for_url(self, url: str) -> str | None:
        """The code for `url` if it's already been fully downloaded, else
        None - used to flag already-downloaded search results before the
        user re-adds something they've already got."""
        entry = self.get_by_url(url)
        return _code_of(entry) if entry else None

    # ---- web "Add" flow ----

    def search(self, query: str, limit: int = 15) -> list[dict]:
        proc = subprocess.run(
            [
                "yt-dlp",
                f"ytsearch{limit}:{query}",
                "--flat-playlist",
                "-j",
                "--no-warnings",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        results = []
        for line in proc.stdout.strip().splitlines():
            try:
                info = json.loads(line)
            except json.JSONDecodeError:
                continue
            video_id = info.get("id")
            if not video_id:
                continue
            results.append(
                {
                    "id": video_id,
                    "title": info.get("title") or "(untitled)",
                    "uploader": info.get("uploader") or info.get("channel") or "",
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                }
            )
        return results

    def probe(self, url: str) -> dict:
        """Full (non-flat) metadata-only fetch. Slower than search()'s flat
        extraction but reliably surfaces a real `artist` tag when one
        exists, unlike flat-playlist search results. Still skips the
        download+ffmpeg step, but yt-dlp's YouTube signature-decryption
        runs in pure Python, so this can take the better part of a minute
        on a Pi Zero W's weak single core - nowhere near instant, but well
        short of the actual download's "tens of seconds" (plus ffmpeg
        transcoding) on top."""
        proc = subprocess.run(
            ["yt-dlp", "-j", "--skip-download", "--no-warnings", "--quiet", url],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(proc.stderr.strip()[-500:] or "yt-dlp probe failed")
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        return {"title": info.get("title") or url, "artist": _pick_artist(info)}

    def enqueue(self, url: str) -> dict:
        """Adds `url` to the add-queue if it isn't already known, or - if
        its last attempt failed - re-queues it for another try. Returns
        its current status immediately; never blocks on probing or
        downloading, which happen later on the background worker."""
        with self._lock:
            entry = self._entries.get(url)
            if entry is not None and entry["status"] != "failed":
                return self._status_dict_locked(entry)
            if entry is None:
                self._entries[url] = {
                    "url": url,
                    "status": "queued",
                    "title": None,
                    "artist": None,
                    "file": None,
                    "detail": "",
                }
            else:
                entry["status"] = "queued"
                entry["detail"] = ""
            self._save_locked()
            status = self._status_dict_locked(self._entries[url])
        self._download_queue.put(url)
        return status

    def list_entries(self) -> list[dict]:
        """Everything ever added through the web UI, most recently added
        first - playlist tracks excluded (see get_by_code)."""
        with self._lock:
            entries = [
                self._status_dict_locked(e)
                for e in self._entries.values()
                if not e.get("file") or len(Path(e["file"]).parts) == 1
            ]
        entries.reverse()
        return entries

    def _requeue_unfinished(self) -> None:
        # Recovers the queue across a process restart - anything that
        # hadn't reached "ready" or "failed" yet gets another pass.
        with self._lock:
            urls = [
                url
                for url, e in self._entries.items()
                if e["status"] in ("queued", "probing", "reserved", "downloading")
            ]
        for url in urls:
            self._download_queue.put(url)

    def _worker_loop(self) -> None:
        while True:
            url = self._download_queue.get()
            try:
                self._process_queued(url)
            except Exception as exc:  # a wedged queue is worse than a logged miss
                print(f"[library] add-queue entry {url} raised {exc!r}")
                self.mark_failed(url, str(exc))

    def _process_queued(self, url: str) -> None:
        # Whether a filename/code is already claimed - the ground truth
        # for "does this need probing" - is entry["file"], not status:
        # enqueue() resets a retried entry's status back to "queued" (so
        # _requeue_unfinished's status-based scan still picks it up after
        # a restart), even though it may already have a reserved file
        # from a previous, later-failed attempt.
        with self._lock:
            entry = self._entries.get(url)
            already_reserved = entry is not None and bool(entry.get("file"))
        if not already_reserved:
            with self._lock:
                entry = self._entries.get(url)
                if entry is None:
                    return
                entry["status"] = "probing"
                self._save_locked()
            try:
                info = self.probe(url)
            except (subprocess.TimeoutExpired, RuntimeError) as exc:
                self.mark_failed(url, str(exc))
                return
            with self._lock:
                filename = self._unique_filename_locked(
                    self._cache_dir,
                    f"{_slugify(info['title'], _MAX_TITLE_SLUG_LEN)}-"
                    f"{_slugify(info['artist'], _MAX_ARTIST_SLUG_LEN)}.wav",
                    check_catalog=True,
                )
                entry = self._entries.get(url)
                if entry is None:
                    return
                entry.update(title=info["title"], artist=info["artist"], file=filename, status="reserved")
                self._save_locked()
        self.download_reserved(url)

    def mark_downloading(self, url: str) -> None:
        with self._lock:
            entry = self._entries.get(url)
            if entry is not None and entry["status"] != "ready":
                entry["status"] = "downloading"
                self._save_locked()

    def mark_failed(self, url: str, detail: str = "") -> None:
        with self._lock:
            entry = self._entries.get(url)
            if entry is not None and entry["status"] != "ready":
                entry["status"] = "failed"
                entry["detail"] = detail
                self._save_locked()

    def finalize(self, url: str, tmp_path: Path) -> Path | None:
        """Renames a completed download into its pre-reserved final path
        and marks the entry ready. `url` must already have a claimed
        filename (status in _HAS_CODE_STATUSES)."""
        with self._lock:
            entry = self._entries.get(url)
            if entry is None or not entry.get("file") or not tmp_path.exists():
                return None
            final_path = self._cache_dir / entry["file"]
            tmp_path.rename(final_path)
            entry["status"] = "ready"
            entry.pop("detail", None)
            self._save_locked()
            return final_path

    def status_for_url(self, url: str) -> dict:
        with self._lock:
            entry = self._entries.get(url)
            if entry is None:
                return {"status": "unknown"}
            return self._status_dict_locked(entry)

    def status_for_code(self, code: str) -> dict:
        with self._lock:
            for entry in self._entries.values():
                if entry.get("file") and _code_of(entry) == code and len(Path(entry["file"]).parts) == 1:
                    return self._status_dict_locked(entry)
            return {"status": "unknown"}

    def download_reserved(self, url: str) -> None:
        """Real download for a url that already has a claimed filename:
        renames straight into the pre-reserved path, ignoring the
        download's own title/artist metadata so a probe/download
        discrepancy can never change a code already handed to the user.
        Called from the add-queue worker (already sequential), but also
        guards against overlapping with a concurrent tap-triggered cache
        of the same url via _in_flight."""
        with self._lock:
            entry = self._entries.get(url)
            if url in self._in_flight or entry is None or not entry.get("file"):
                return
            self._in_flight.add(url)
        self.mark_downloading(url)
        try:
            tmp_template = str(self._cache_dir / f".pending-{uuid4().hex}.%(ext)s")
            proc = subprocess.run(
                [
                    "yt-dlp",
                    "--quiet",
                    "-f",
                    "bestaudio",
                    "-x",
                    "--audio-format",
                    "wav",
                    "--postprocessor-args",
                    "ffmpeg:-map_metadata -1",
                    "--print",
                    "after_move:%(filepath)s",
                    "-o",
                    tmp_template,
                    url,
                ],
                capture_output=True,
                text=True,
            )
            lines = proc.stdout.strip().splitlines()
            if proc.returncode != 0 or not lines:
                self.mark_failed(url, proc.stderr.strip()[-500:])
                return
            if self.finalize(url, Path(lines[-1])) is None:
                self.mark_failed(url, "download finished but couldn't finalize")
        finally:
            with self._lock:
                self._in_flight.discard(url)

    # ---- tap-triggered background cache (used by YoutubePlayer) ----

    def download_and_catalog(self, url: str) -> None:
        """Tap-triggered background cache for a raw-URL tag: derives
        title/artist from the download itself, since this flow never
        hands a code to anyone ahead of time (unlike the add-queue)."""
        with self._lock:
            if url in self._in_flight or self.get_by_url(url) is not None:
                return
            self._in_flight.add(url)
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
                    "--postprocessor-args",
                    "ffmpeg:-map_metadata -1",
                    "--print",
                    f"after_move:%(title)s{_SEP}{_ARTIST_TEMPLATE}{_SEP}%(filepath)s",
                    "-o",
                    tmp_template,
                    url,
                ],
                capture_output=True,
                text=True,
            )
            lines = proc.stdout.strip().splitlines()
            if proc.returncode != 0 or not lines:
                print(f"[library] cache download failed for {url}: {proc.stderr.strip()[-500:]}")
                return

            title, artist, filepath = lines[-1].split(_SEP)
            tmp_path = Path(filepath)
            if not tmp_path.exists():
                return
            with self._lock:
                filename = self._unique_filename_locked(
                    self._cache_dir, f"{_slugify(title)}-{_slugify(artist)}.wav", check_catalog=True
                )
                final_path = self._cache_dir / filename
                tmp_path.rename(final_path)
                self._put_ready_locked(url, title, artist, final_path)
        finally:
            with self._lock:
                self._in_flight.discard(url)

    # ---- playlists (used by YoutubePlayer) ----

    def playlist_dir(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self._cache_dir / "playlists" / key

    def cached_playlist_tracks(self, playlist_dir: Path) -> list[Path]:
        if not (playlist_dir / ".complete").exists():
            return []
        return sorted(playlist_dir.glob("*.wav"))

    def maybe_start_playlist_download(self, url: str, playlist_dir: Path) -> None:
        with self._lock:
            if url in self._caching_playlists:
                return
            self._caching_playlists.add(url)
        self._start_playlist_download(url, playlist_dir)

    def _start_playlist_download(self, url: str, playlist_dir: Path) -> None:
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
                f"after_move:%(title)s{_SEP}{_ARTIST_TEMPLATE}{_SEP}%(webpage_url)s{_SEP}%(filepath)s",
                "-o",
                output_template,
                url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        threading.Thread(
            target=self._finish_playlist_download, args=(proc, playlist_dir, url), daemon=True
        ).start()

    def _finish_playlist_download(self, proc: subprocess.Popen, playlist_dir: Path, url: str) -> None:
        stdout, _ = proc.communicate()
        if proc.returncode == 0:
            for line in stdout.strip().splitlines():
                parts = line.split(_SEP)
                if len(parts) != 4:
                    continue
                title, artist, track_url, filepath = parts
                tmp_path = Path(filepath)
                if not tmp_path.exists():
                    continue
                with self._lock:
                    filename = self._unique_filename_locked(
                        playlist_dir, f"{_slugify(title)}-{_slugify(artist)}.wav", check_catalog=False
                    )
                    final_path = playlist_dir / filename
                    tmp_path.rename(final_path)
                    self._put_ready_locked(track_url, title, artist, final_path)
            (playlist_dir / ".complete").touch()
        else:
            print(f"[library] playlist download failed for {url} (exit {proc.returncode})")
        with self._lock:
            self._caching_playlists.discard(url)
