"""Entry point.

Usage:
    python -m mini_vinyl.main         # run the player
    python -m mini_vinyl.main --scan  # print UIDs of tags held to the reader
"""

import argparse
import sys
import time

from mini_vinyl.config import load_secrets, load_tags, env
from mini_vinyl.nfc_reader import NfcReader
from mini_vinyl.player_manager import PlayerManager
from mini_vinyl.players.youtube_player import YoutubePlayer
from mini_vinyl.players.spotify_player import SpotifyPlayer

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
                print(f"UID: {uid}")
                last_uid = uid
    except KeyboardInterrupt:
        pass


def run_player() -> None:
    load_secrets()
    tags = load_tags()
    print(f"Loaded {len(tags)} tag(s)")

    reader = NfcReader(
        irq_pin=_int_or_none(env("PN532_IRQ_PIN")),
        reset_pin=_int_or_none(env("PN532_RESET_PIN")),
    )

    players = {
        "youtube": YoutubePlayer(audio_output=env("AUDIO_OUTPUT", "pipewire")),
        "spotify": SpotifyPlayer(device_name=env("SPOTIFY_DEVICE_NAME", "mini-vinyl")),
    }
    manager = PlayerManager(players)

    current_uid = None
    misses = 0

    print("Ready. Waiting for tags...")
    try:
        while True:
            uid = reader.poll(timeout=POLL_TIMEOUT)

            if uid:
                misses = 0
                if uid != current_uid:
                    tag = tags.get(uid)
                    if tag is None:
                        print(f"[main] unknown tag UID: {uid}")
                    else:
                        current_uid = uid
                        manager.handle_tag_present(tag)
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
        "--scan", action="store_true", help="print UIDs of scanned tags and exit"
    )
    args = parser.parse_args()

    if args.scan:
        run_scan()
    else:
        run_player()


if __name__ == "__main__":
    sys.exit(main())
