"""The phone-facing "search & add" web UI. Runs in a background thread of
the same process as the NFC poll loop (see main.py) so both share one
in-memory Library/WriteCoordinator with no cross-process locking needed.
No auth - meant for a trusted home LAN only, reached via mDNS
(http://<hostname>.local:8080), never exposed beyond it.

Adding a song only ever enqueues it (see Library.enqueue) - the actual
probing/downloading happens later on Library's own background worker, so
a burst of Adds returns instantly no matter how many songs are queued up,
and finishes on its own even if nobody's watching. /api/library is how
the frontend finds out what's ready to have a tag written for it.

/api/playlists/* manages locally-built playlists (mini_vinyl/playlists.py's
PlaylistStore) - hand-picked sets of already-downloaded songs. Writing one
to a tag reuses the exact same /api/write flow as a song; a playlist's
code just carries the "playlist:" prefix PlaylistStore/YoutubePlayer use
to tell the two apart.
"""

import logging
import subprocess

from flask import Flask, request, send_from_directory

from mini_vinyl.library import Library
from mini_vinyl.playlists import CODE_PREFIX as PLAYLIST_CODE_PREFIX
from mini_vinyl.playlists import PlaylistStore
from mini_vinyl.tag_writer import WriteCoordinator

# The frontend polls these every 1-3s while a modal/the library view is
# open, which would otherwise bury the process's own [main]/[youtube]/
# [library] prints under a wall of routine "GET ... 200" lines. Everything
# else (search, add, write, and any non-2xx response) still logs normally.
_QUIET_POLL_PATHS = ("/api/write/status", "/api/library")


class _QuietPollingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(f'"GET {path}' in message for path in _QUIET_POLL_PATHS)


def create_app(library: Library, playlist_store: PlaylistStore, write_coordinator: WriteCoordinator) -> Flask:
    logging.getLogger("werkzeug").addFilter(_QuietPollingFilter())

    app = Flask(__name__)

    def enrich_playlist(playlist: dict) -> dict:
        songs = []
        for song_code in playlist["songs"]:
            entry = library.get_by_code(song_code)
            songs.append(
                {
                    "code": song_code,
                    "title": entry["title"] if entry else song_code,
                    "artist": entry["artist"] if entry else None,
                    "missing": entry is None,
                }
            )
        return {"code": playlist["code"], "name": playlist["name"], "songs": songs}

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/search")
    def search():
        query = (request.args.get("q") or "").strip()
        if not query:
            return {"results": []}
        try:
            results = library.search(query)
        except subprocess.TimeoutExpired:
            return {"error": "search timed out"}, 504
        for r in results:
            r["code"] = library.code_for_url(r["url"])
        return {"results": results}

    @app.post("/api/songs")
    def add_song():
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        if not url:
            return {"error": "url is required"}, 400
        return library.enqueue(url)

    @app.get("/api/library")
    def list_library():
        return {"entries": library.list_entries()}

    @app.get("/api/songs/<code>/status")
    def song_status(code: str):
        return library.status_for_code(code)

    @app.get("/api/playlists")
    def list_playlists():
        return {"playlists": [enrich_playlist(p) for p in playlist_store.list_playlists()]}

    @app.post("/api/playlists")
    def create_playlist():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return {"error": "name is required"}, 400
        return enrich_playlist(playlist_store.create(name))

    @app.get("/api/playlists/<code>")
    def get_playlist(code: str):
        playlist = playlist_store.get(code)
        if playlist is None:
            return {"error": "unknown playlist"}, 404
        return enrich_playlist(playlist)

    @app.post("/api/playlists/<code>/songs")
    def add_song_to_playlist(code: str):
        data = request.get_json(silent=True) or {}
        song_code = (data.get("song_code") or "").strip()
        if not song_code:
            return {"error": "song_code is required"}, 400
        playlist = playlist_store.add_song(code, song_code)
        if playlist is None:
            return {"error": "unknown playlist or song"}, 404
        return enrich_playlist(playlist)

    @app.delete("/api/playlists/<code>/songs/<song_code>")
    def remove_song_from_playlist(code: str, song_code: str):
        playlist = playlist_store.remove_song(code, song_code)
        if playlist is None:
            return {"error": "unknown playlist"}, 404
        if not playlist["songs"]:
            # A playlist is never meant to sit empty - removing its last
            # song deletes it outright rather than leaving a dead entry.
            playlist_store.delete(code)
            return {"deleted": True}
        return enrich_playlist(playlist)

    @app.delete("/api/playlists/<code>")
    def delete_playlist(code: str):
        # Only ever used to clean up a playlist that was just created and
        # never got a song added (see the frontend's back-button
        # handling) - not a general-purpose delete.
        playlist = playlist_store.get(code)
        if playlist is None:
            return {"error": "unknown playlist"}, 404
        if playlist["songs"]:
            return {"error": "playlist is not empty"}, 400
        playlist_store.delete(code)
        return {"deleted": True}

    @app.post("/api/write")
    def write_tag():
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip()
        if not code:
            return {"error": "code is required"}, 400
        if code.startswith(PLAYLIST_CODE_PREFIX):
            if playlist_store.get(code[len(PLAYLIST_CODE_PREFIX) :]) is None:
                return {"error": "unknown playlist"}, 404
        elif library.status_for_code(code)["status"] == "unknown":
            return {"error": "unknown code"}, 404
        write_coordinator.start(code, force=bool(data.get("force")))
        return {"status": "waiting"}

    @app.get("/api/write/status")
    def write_status():
        code = (request.args.get("code") or "").strip()
        if not code:
            return {"error": "code is required"}, 400
        return write_coordinator.status_for(code)

    return app
