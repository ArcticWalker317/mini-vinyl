"""Locally-built playlists: named collections of already-downloaded
library songs. Written to a tag as the bare text `playlist:<slug>` -
the same "no-prefix URI record" trick mini_vinyl/library.py's song codes
use (see its module docstring), just with a distinct prefix so
YoutubePlayer can tell a playlist tag apart from a song tag on tap. No
downloading happens here - a song can only be added once it's already a
"ready" entry in the Library.

This is the only way to make a playlist - a tag written directly with a
YouTube playlist *URL* is not supported; only individual video URLs are.
A local playlist is built by hand from songs you've already got, then
shuffle-played through YoutubePlayer's queue mechanism (see
YoutubePlayer._play_local_playlist) once it's written to a tag.

A playlist is never meant to sit empty - mini_vinyl/web.py deletes one
outright the moment its last song is removed (or the user backs out of
one that was just created and never got a song added), rather than
leaving a dead entry around. delete() itself has no such opinion - it's
just storage - the empty-means-gone policy lives in the web layer.
"""

import json
import threading
from pathlib import Path

from mini_vinyl.library import Library, _slugify

CODE_PREFIX = "playlist:"
_MAX_NAME_SLUG_LEN = 50


class PlaylistStore:
    def __init__(self, library: Library, path: Path | None = None):
        self._library = library
        self._path = path or (library.cache_dir / "playlists.json")
        self._lock = threading.Lock()
        self._playlists: dict[str, dict] = self._load()

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_locked(self) -> None:
        self._path.write_text(
            json.dumps(self._playlists, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _unique_code_locked(self, name: str) -> str:
        base = _slugify(name, _MAX_NAME_SLUG_LEN)
        if base not in self._playlists:
            return base
        n = 2
        while f"{base}_{n}" in self._playlists:
            n += 1
        return f"{base}_{n}"

    def create(self, name: str) -> dict:
        with self._lock:
            code = self._unique_code_locked(name)
            self._playlists[code] = {"code": code, "name": name, "songs": []}
            self._save_locked()
            return dict(self._playlists[code])

    def list_playlists(self) -> list[dict]:
        with self._lock:
            return [dict(p) for p in self._playlists.values()]

    def get(self, code: str) -> dict | None:
        with self._lock:
            playlist = self._playlists.get(code)
            return dict(playlist) if playlist is not None else None

    def add_song(self, code: str, song_code: str) -> dict | None:
        """Returns the updated playlist, or None if the playlist or the
        song code doesn't exist (a song must already be a "ready"
        download - nothing here triggers a new download)."""
        if self._library.get_by_code(song_code) is None:
            return None
        with self._lock:
            playlist = self._playlists.get(code)
            if playlist is None:
                return None
            if song_code not in playlist["songs"]:
                playlist["songs"].append(song_code)
                self._save_locked()
            return dict(playlist)

    def remove_song(self, code: str, song_code: str) -> dict | None:
        with self._lock:
            playlist = self._playlists.get(code)
            if playlist is None:
                return None
            if song_code in playlist["songs"]:
                playlist["songs"].remove(song_code)
                self._save_locked()
            return dict(playlist)

    def delete(self, code: str) -> bool:
        with self._lock:
            if code not in self._playlists:
                return False
            del self._playlists[code]
            self._save_locked()
            return True

    def track_paths(self, code: str) -> list[Path]:
        """Resolves a playlist's song codes to their on-disk wav paths,
        used at playback time. A song code that's since gone missing
        (deleted from disk, catalog reset, etc.) is silently skipped
        rather than failing the whole playlist."""
        playlist = self.get(code)
        if playlist is None:
            return []
        paths = []
        for song_code in playlist["songs"]:
            entry = self._library.get_by_code(song_code)
            if entry is not None:
                paths.append(self._library.path_for(entry))
        return paths
