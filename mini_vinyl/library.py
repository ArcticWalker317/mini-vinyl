"""The song catalog shared between playback (mini_vinyl/players/youtube_player.py)
and the web "search & add" UI (mini_vinyl/web.py).

Every downloaded song - whether triggered by tapping an unrecognized
YouTube-URL tag and leaving it playing for a few seconds, or by clicking
Add on a search result - ends up as a `<song_title>-<artist>.wav` file in
the cache directory, recorded in `library.json` alongside it. A "code" is
just that filename without the `.wav` extension (e.g.
`the_scientist-coldplay`); it's what gets burned onto a physical tag by
the web UI's write flow and looked up here at playback time.

The Add flow needs to hand a code back to the browser fast, well before
the real (slow, tens-of-seconds-on-a-Pi-Zero-W) download finishes, so the
user can go write a tag immediately. get_or_reserve() claims a unique
filename up front from a quick metadata probe, persisting it with
status="reserved" right away; the real download later renames straight
into that pre-claimed path (finalize()) without ever re-deriving the
name. A failed download's reservation is never freed for reuse (see
get_or_reserve's docstring) - once a code might be sitting on a physical
tag, that filename is permanently spoken for, download or no download.
"""

import hashlib
import json
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


def _code_of(entry: dict) -> str:
    return Path(entry["file"]).stem


class Library:
    def __init__(self, cache_dir: Path | None = None):
        self._cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._entries_path = self._cache_dir / "library.json"
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = self._load()

        self._in_flight: set[str] = set()  # single-track urls with a download running
        self._caching_playlists: set[str] = set()  # playlist urls with a download running

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    # ---- persistence ----

    def _load(self) -> dict:
        if not self._entries_path.exists():
            return {}
        try:
            return json.loads(self._entries_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

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

    def _unique_filename_locked(self, dest_dir: Path, filename: str, *, check_catalog: bool) -> str:
        def taken(name: str) -> bool:
            if (dest_dir / name).exists():
                return True
            if check_catalog:
                return any(Path(e["file"]) == Path(name) for e in self._entries.values())
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
            if entry is None or entry.get("status", "ready") != "ready":
                return None
            return entry if (self._cache_dir / entry["file"]).exists() else None

    def get_by_code(self, code: str) -> dict | None:
        # Only top-level cache_dir entries are code-addressable - playlist
        # tracks live under playlists/<hash>/ (more than one path part) and
        # never went through get_or_reserve(), so they're excluded here by
        # construction rather than an explicit check.
        with self._lock:
            for entry in self._entries.values():
                if entry.get("status", "ready") != "ready":
                    continue
                file_rel = Path(entry["file"])
                if len(file_rel.parts) == 1 and file_rel.stem == code:
                    return entry if (self._cache_dir / file_rel).exists() else None
        return None

    def path_for(self, entry: dict) -> Path:
        return self._cache_dir / entry["file"]

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
            timeout=30,
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
        exists, unlike flat-playlist search results."""
        proc = subprocess.run(
            ["yt-dlp", "-j", "--skip-download", "--no-warnings", "--quiet", url],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(proc.stderr.strip()[-500:] or "yt-dlp probe failed")
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        return {"title": info.get("title") or url, "artist": _pick_artist(info)}

    def get_or_reserve(self, url: str, title: str, artist: str) -> str:
        """Returns the code for `url`, claiming a unique filename the
        first time it's seen. Idempotent - repeat calls for the same url
        always return the original code, regardless of whether that
        reservation ever finished downloading. This is deliberate: a
        code may already be burned onto a physical tag by the time a
        download fails, so its filename can never be handed to a
        different song later - a retry of the same title/artist gets a
        fresh `_2` suffix instead of reusing a dead reservation."""
        with self._lock:
            entry = self._entries.get(url)
            if entry is not None:
                return _code_of(entry)

            filename = self._unique_filename_locked(
                self._cache_dir,
                f"{_slugify(title, _MAX_TITLE_SLUG_LEN)}-{_slugify(artist, _MAX_ARTIST_SLUG_LEN)}.wav",
                check_catalog=True,
            )
            self._entries[url] = {
                "title": title,
                "artist": artist,
                "url": url,
                "file": filename,
                "status": "reserved",
            }
            self._save_locked()
            return Path(filename).stem

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
        and marks the entry ready. `url` must have already gone through
        get_or_reserve()."""
        with self._lock:
            entry = self._entries.get(url)
            if entry is None or not tmp_path.exists():
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
            return {"status": entry["status"], "code": _code_of(entry), "detail": entry.get("detail", "")}

    def status_for_code(self, code: str) -> dict:
        with self._lock:
            for entry in self._entries.values():
                if _code_of(entry) == code and len(Path(entry["file"]).parts) == 1:
                    return {"status": entry["status"], "url": entry["url"], "detail": entry.get("detail", "")}
            return {"status": "unknown"}

    def download_reserved(self, url: str) -> None:
        """Real download for a url already claimed via get_or_reserve():
        renames straight into the pre-reserved path, ignoring the
        download's own title/artist metadata so a probe/download
        discrepancy can never change a code already handed to the user.
        Meant to be run in a background thread."""
        with self._lock:
            if url in self._in_flight or url not in self._entries:
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
        hands a code to anyone ahead of time (unlike download_reserved)."""
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
