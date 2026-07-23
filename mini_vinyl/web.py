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
"""

import logging
import subprocess

from flask import Flask, request, send_from_directory

from mini_vinyl.library import Library
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


def create_app(library: Library, write_coordinator: WriteCoordinator) -> Flask:
    logging.getLogger("werkzeug").addFilter(_QuietPollingFilter())

    app = Flask(__name__)

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

    @app.post("/api/write")
    def write_tag():
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip()
        if not code:
            return {"error": "code is required"}, 400
        if library.status_for_code(code)["status"] == "unknown":
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
