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

Adding a song only ever needs a title (and, optionally, an artist) from
the web UI - see enqueue_search(). A YouTube search + pick-the-best-match
step (_pick_best_match) resolves that to an actual video before handing
off to the same enqueue()/download pipeline used everywhere else. That
resolution runs on its own background worker/queue, entirely separate
from the download queue and not persisted to library.json - a search
job is cheap to just redo if it's lost on a restart, unlike a download
that's already claimed a filename, so it doesn't need the same
durability. The web UI polls search_job_status() to find out what a
search resolved to (if anything).
"""

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

# Soft ranking signals for picking the best search result for a
# title/artist search - not hard filters, since a "Lyrics" video that's
# otherwise the only real match should still win over nothing. Matched
# against the candidate's title, case-insensitively. "remaster" (not
# "remastered") deliberately covers remaster/remastered/remasters as one
# substring match - listing both would double-count a title containing
# "Remastered", over-weighting it relative to "Official".
_PREFERRED_TERMS = {"official": 3, "remaster": 2}
_DEPRIORITIZED_TERMS = {"lyrics": 4, "lyric video": 4, "karaoke": 4}


def _slugify(text: str, max_len: int | None = None) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    if max_len is not None:
        slug = slug[:max_len].rstrip("_")
    return slug or "unknown"


def _match_score(result: dict, title: str, artist: str) -> int:
    text = result["title"].lower()
    uploader = (result.get("uploader") or "").lower()

    # Word-set intersection, not substring containment - "the" in title
    # words must match the whole word "the" in the candidate's title, not
    # just appear as a substring of an unrelated word like "other".
    text_words = set(re.findall(r"\w+", text))
    uploader_words = set(re.findall(r"\w+", uploader))
    title_words = set(re.findall(r"\w+", title.lower()))
    artist_words = set(re.findall(r"\w+", artist.lower()))

    score = 2 * len(title_words & text_words)
    score += len(artist_words & (text_words | uploader_words))

    for term, weight in _PREFERRED_TERMS.items():
        if term in text:
            score += weight
    for term, weight in _DEPRIORITIZED_TERMS.items():
        if term in text:
            score -= weight

    return score


def _pick_best_match(results: list[dict], title: str, artist: str) -> dict:
    """The highest-scoring candidate for `title`/`artist` - relevance
    (title/artist words actually present) dominates the score, with
    "official"/"remaster(ed)" nudging it up and "lyrics"/"karaoke"
    nudging it down among otherwise-similar candidates. Ties keep
    YouTube's own search ordering (max() returns the first of equal
    candidates, and `results` arrives in that order)."""
    return max(results, key=lambda r: _match_score(r, title, artist))


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

        # Single sequential worker: on a one-core Pi Zero W, running two
        # yt-dlp/ffmpeg processes "concurrently" doesn't get anything done
        # faster, just makes both slower - so the add-queue is drained one
        # url at a time, on purpose.
        self._download_queue: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._worker_loop, daemon=True).start()
        self._requeue_unfinished()

        # Title/artist -> best-matching video resolution, entirely
        # separate from the download queue above and never persisted
        # (see the module docstring) - its own worker so a search in
        # flight can't get stuck behind - or block - a download, or vice
        # versa.
        self._search_jobs: dict[str, dict] = {}
        self._search_queue: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._search_worker_loop, daemon=True).start()

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

    def _unique_filename_locked(self, dest_dir: Path, filename: str) -> str:
        def taken(name: str) -> bool:
            if (dest_dir / name).exists():
                return True
            return any(e.get("file") and Path(e["file"]) == Path(name) for e in self._entries.values())

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
        # Only top-level cache_dir entries are code-addressable. Nothing
        # written from here on ever has a multi-part `file` path, but a
        # library.json from before YouTube-playlist-URL caching was
        # removed may still have leftover playlist-track entries whose
        # `file` points into a playlists/<hash>/ subdirectory - the
        # part-count check keeps those correctly un-addressable rather
        # than surfacing them as if they were directly-added songs.
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

    # ---- web "Add" flow ----

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Flat (fast) search - a candidate pool for _pick_best_match to
        choose from, not something shown to the user directly. A search
        on this hardware is bottlenecked by yt-dlp actually
        fetching+parsing YouTube's search response, not by anything on
        our end, so asking for fewer results genuinely cuts the time this
        takes rather than just trimming a displayed list."""
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

    def enqueue_search(self, title: str, artist: str) -> str:
        """Kicks off a background search for the best-matching video for
        `title`/`artist`; returns a job id immediately (search_job_status()
        polls it) without waiting on the search itself, let alone a
        download - the whole thing (search, pick, then the normal
        enqueue()/download pipeline) runs on _search_worker_loop."""
        job_id = uuid4().hex
        with self._lock:
            self._search_jobs[job_id] = {
                "status": "searching",
                "title": title,
                "artist": artist,
                "code": None,
                "detail": "",
            }
        self._search_queue.put(job_id)
        return job_id

    def search_job_status(self, job_id: str) -> dict:
        with self._lock:
            job = self._search_jobs.get(job_id)
            return dict(job) if job is not None else {"status": "unknown"}

    def _resolve_search_job(
        self, job_id: str, status: str, *, code: str | None = None, detail: str = ""
    ) -> None:
        with self._lock:
            job = self._search_jobs.get(job_id)
            if job is not None:
                job["status"] = status
                job["code"] = code
                job["detail"] = detail

    def _search_worker_loop(self) -> None:
        while True:
            job_id = self._search_queue.get()
            try:
                self._process_search_job(job_id)
            except Exception as exc:  # a wedged queue is worse than a logged miss
                print(f"[library] search job {job_id} raised {exc!r}")
                self._resolve_search_job(job_id, "failed", detail=str(exc))

    def _process_search_job(self, job_id: str) -> None:
        job = self.search_job_status(job_id)
        title, artist = job.get("title", ""), job.get("artist", "")
        query = f"{title} {artist}".strip()

        try:
            results = self.search(query, limit=5)
        except subprocess.TimeoutExpired:
            self._resolve_search_job(job_id, "failed", detail="search timed out")
            return

        if not results:
            self._resolve_search_job(job_id, "failed", detail="no matching videos found")
            return

        best = _pick_best_match(results, title, artist)
        status = self.enqueue(best["url"])  # idempotent - reuses an existing entry if there is one
        self._resolve_search_job(job_id, "found", code=status["code"])

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
        filename (entry["file"] set)."""
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
                    self._cache_dir, f"{_slugify(title)}-{_slugify(artist)}.wav"
                )
                final_path = self._cache_dir / filename
                tmp_path.rename(final_path)
                self._put_ready_locked(url, title, artist, final_path)
        finally:
            with self._lock:
                self._in_flight.discard(url)
