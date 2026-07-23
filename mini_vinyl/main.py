"""Entry point.

Usage:
    python -m mini_vinyl.main         # run the player
    python -m mini_vinyl.main --scan  # print UID + NDEF URI of scanned tags
"""

import argparse
import sys
import threading
import time

from mini_vinyl import web
from mini_vinyl.config import TagEntry, load_secrets, env
from mini_vinyl.library import Library
from mini_vinyl.nfc_reader import NfcReader
from mini_vinyl.player_manager import PlayerManager
from mini_vinyl.players.youtube_player import YoutubePlayer
from mini_vinyl.playlists import PlaylistStore
from mini_vinyl.tag_writer import WriteCoordinator, WriteRequest

# How many consecutive empty polls before we consider the tag removed.
# The PN532 occasionally misses a poll even while a tag sits still, so a
# single miss shouldn't stop playback.
REMOVAL_THRESHOLD = 3
POLL_TIMEOUT = 0.3
WEB_PORT = 8080


def run_scan() -> None:
    reader = NfcReader()
    print("Hold a tag to the reader (Ctrl+C to quit)...")
    last_uid = None
    try:
        for uid in reader.wait_for_tag():
            if uid != last_uid:
                uri = reader.read_ndef_uri()
                print(f"UID: {uid}  URI: {uri!r}")
                last_uid = uid
    except KeyboardInterrupt:
        pass


def tag_entry_from_uri(uid: str, uri: str) -> TagEntry:
    return TagEntry(uid=uid, type="youtube", id=uri)


def _handle_pending_write(
    reader: NfcReader, write_coordinator: WriteCoordinator, pending: WriteRequest
) -> bool:
    # Refuse to clobber a tag that already has data on it - guards against
    # a stale/forgotten pending write firing against, say, a vinyl resting
    # on the reader mid-playback rather than a genuinely blank tag. The web
    # UI can override this per-request (pending.force) once the user's
    # explicitly confirmed they want to overwrite what's on it.
    if not pending.force and reader.read_ndef_uri() is not None:
        print(f"[main] refusing to write {pending.code!r} - tag already has data on it")
        write_coordinator.resolve(pending.code, False, "tag already has data")
        return False
    if reader.write_ndef_uri(pending.code):
        print(f"[main] wrote {pending.code!r} successfully")
        write_coordinator.resolve(pending.code, True)
        return True
    print(f"[main] write of {pending.code!r} failed (I2C write errors after retrying)")
    write_coordinator.resolve(pending.code, False, "write failed")
    return False


def _start_web_ui(library: Library, playlist_store: PlaylistStore, write_coordinator: WriteCoordinator) -> None:
    app = web.create_app(library, playlist_store, write_coordinator)
    thread = threading.Thread(
        target=app.run,
        kwargs={"host": "0.0.0.0", "port": WEB_PORT, "threaded": True, "use_reloader": False},
        daemon=True,
    )
    thread.start()
    print(f"[main] web UI listening on port {WEB_PORT}")


def run_player() -> None:
    load_secrets()

    reader = NfcReader(
        irq_pin=_int_or_none(env("PN532_IRQ_PIN")),
        reset_pin=_int_or_none(env("PN532_RESET_PIN")),
    )

    library = Library()
    playlist_store = PlaylistStore(library)
    write_coordinator = WriteCoordinator()
    players = {
        "youtube": YoutubePlayer(library, playlist_store, audio_output=env("AUDIO_OUTPUT", "pipewire"))
    }

    manager = PlayerManager(players)

    _start_web_ui(library, playlist_store, write_coordinator)

    # UID -> URI, populated as tags are read. Re-reading all 12 NDEF pages
    # over I2C on every single tap is real overhead on this hardware; a
    # tag's content doesn't change between taps, so remember it in memory
    # and skip straight to playback next time (until this process restarts).
    uri_cache: dict[str, str] = {}

    current_uid = None
    # The last uid found to have no readable NDEF data, if any - without
    # this, a blank/unwritten tag left sitting on the reader gets a full
    # multi-page NDEF re-read (and a repeated log line) on every single
    # poll, ~7x/second, instead of just once until it's lifted.
    blank_uid = None
    # The last uid a write was attempted against, if any - regardless of
    # whether that attempt succeeded, was refused ("tag already has
    # data"), or failed outright. Without this, a tag sitting on the
    # reader through a refused write (e.g. while the user is deciding
    # whether to hit "Overwrite") falls through to normal tag handling on
    # the very next poll - since the resolved write request is no longer
    # "waiting" and take_pending() stops returning it - and starts
    # playing whatever old song/playlist is still written on it. Cleared
    # only once the tag is actually lifted, so a fresh placement of the
    # same physical tag later behaves normally again.
    write_attempted_uid = None
    misses = 0

    print("Ready. Waiting for tags...")
    try:
        while True:
            uid = reader.poll(timeout=POLL_TIMEOUT)

            if uid:
                misses = 0
                pending = write_coordinator.take_pending()
                if pending is not None:
                    print(f"[main] writing tag with code {pending.code!r}")
                    write_attempted_uid = uid
                    if _handle_pending_write(reader, write_coordinator, pending):
                        uri_cache[uid] = pending.code
                        current_uid = uid
                        blank_uid = None
                elif uid != current_uid and uid != blank_uid and uid != write_attempted_uid:
                    uri = uri_cache.get(uid)
                    if uri is None:
                        uri = reader.read_ndef_uri()
                        if uri is not None:
                            uri_cache[uid] = uri
                    if uri is None:
                        print(f"[main] no NDEF URI found on tag {uid}")
                        blank_uid = uid
                    else:
                        current_uid = uid
                        manager.handle_tag_present(tag_entry_from_uri(uid, uri))
            else:
                misses += 1
                if misses >= REMOVAL_THRESHOLD:
                    if current_uid is not None:
                        print("[main] tag removed")
                        manager.handle_tag_absent()
                        current_uid = None
                    blank_uid = None
                    write_attempted_uid = None

            time.sleep(0.05)
    except KeyboardInterrupt:
        manager.handle_tag_absent()


def _int_or_none(v: str | None) -> int | None:
    return int(v) if v else None


def main() -> None:
    parser = argparse.ArgumentParser(description="mini-vinyl NFC record player")
    parser.add_argument(
        "--scan", action="store_true", help="print UID + NDEF URI of scanned tags and exit"
    )
    args = parser.parse_args()

    if args.scan:
        run_scan()
    else:
        run_player()


if __name__ == "__main__":
    sys.exit(main())
