"""Entry point.

Usage:
    python -m mini_vinyl.main         # run the player
    python -m mini_vinyl.main --scan  # print UID + NDEF URI of scanned tags
"""

import argparse
import sys
import time

from mini_vinyl.config import TagEntry, load_secrets, env
from mini_vinyl.nfc_reader import NfcReader
from mini_vinyl.player_manager import PlayerManager
from mini_vinyl.players.youtube_player import YoutubePlayer

# How many consecutive empty polls before we consider the tag removed.
# The PN532 occasionally misses a poll even while a tag sits still, so a
# single miss shouldn't stop playback.
REMOVAL_THRESHOLD = 3
POLL_TIMEOUT = 0.3


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


def run_player() -> None:
    load_secrets()

    reader = NfcReader(
        irq_pin=_int_or_none(env("PN532_IRQ_PIN")),
        reset_pin=_int_or_none(env("PN532_RESET_PIN")),
    )

    players = {"youtube": YoutubePlayer(audio_output=env("AUDIO_OUTPUT", "pipewire"))}

    manager = PlayerManager(players)

    # UID -> URI, populated as tags are read. Re-reading all 12 NDEF pages
    # over I2C on every single tap is real overhead on this hardware; a
    # tag's content doesn't change between taps, so remember it in memory
    # and skip straight to playback next time (until this process restarts).
    uri_cache: dict[str, str] = {}

    current_uid = None
    misses = 0

    print("Ready. Waiting for tags...")
    try:
        while True:
            uid = reader.poll(timeout=POLL_TIMEOUT)

            if uid:
                misses = 0
                if uid != current_uid:
                    uri = uri_cache.get(uid)
                    if uri is None:
                        uri = reader.read_ndef_uri()
                        if uri is not None:
                            uri_cache[uid] = uri
                    if uri is None:
                        print(f"[main] no NDEF URI found on tag {uid}")
                    else:
                        current_uid = uid
                        manager.handle_tag_present(tag_entry_from_uri(uid, uri))
            else:
                misses += 1
                if current_uid is not None and misses >= REMOVAL_THRESHOLD:
                    print("[main] tag removed")
                    manager.handle_tag_absent()
                    current_uid = None

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
