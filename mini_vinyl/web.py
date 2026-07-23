"""The phone-facing "search & add" web UI. Runs in a background thread of
the same process as the NFC poll loop (see main.py) so both share one
in-memory Library/WriteCoordinator with no cross-process locking needed.
No auth - meant for a trusted home LAN only, reached via mDNS
(http://<hostname>.local:8080), never exposed beyond it.
"""

import subprocess
import threading

from flask import Flask, request, send_from_directory

from mini_vinyl.library import Library
from mini_vinyl.tag_writer import WriteCoordinator


def create_app(library: Library, write_coordinator: WriteCoordinator) -> Flask:
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

        status = library.status_for_url(url)
        if status["status"] == "unknown":
            try:
                info = library.probe(url)
            except (subprocess.TimeoutExpired, RuntimeError) as exc:
                return {"error": f"couldn't fetch video info: {exc}"}, 502
            code = library.get_or_reserve(url, info["title"], info["artist"])
            threading.Thread(target=library.download_reserved, args=(url,), daemon=True).start()
            return {"code": code, "status": "reserved"}

        if status["status"] in ("reserved", "failed"):
            # Not currently running (a fresh reservation, or a previous
            # attempt that failed) - (re)start the download under the same
            # already-claimed code.
            threading.Thread(target=library.download_reserved, args=(url,), daemon=True).start()

        return {"code": status["code"], "status": status["status"]}

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
        write_coordinator.start(code)
        return {"status": "waiting"}

    @app.get("/api/write/status")
    def write_status():
        code = (request.args.get("code") or "").strip()
        if not code:
            return {"error": "code is required"}, 400
        return write_coordinator.status_for(code)

    return app
