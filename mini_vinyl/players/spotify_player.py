"""Controls playback on a librespot Spotify Connect device running on this
same Pi (librespot does the actual audio decoding/output; this just tells
it what to play via the Spotify Web API).

Requires:
  - A Spotify PREMIUM account (Web API playback control is Premium-only)
  - librespot running locally, output routed to the bluealsa ALSA device,
    advertising itself as SPOTIFY_DEVICE_NAME (see systemd/librespot.service)
"""

import time

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from mini_vinyl.config import TagEntry, env
from mini_vinyl.players.base import Player

SCOPES = "user-modify-playback-state user-read-playback-state"


class SpotifyPlayer(Player):
    def __init__(self, device_name: str):
        self._device_name = device_name
        self._sp = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=env("SPOTIFY_CLIENT_ID", required=True),
                client_secret=env("SPOTIFY_CLIENT_SECRET", required=True),
                redirect_uri=env("SPOTIFY_REDIRECT_URI", required=True),
                scope=SCOPES,
                cache_path=".spotify_token_cache",
            )
        )
        self._playing = False

    def _find_device_id(self, retries: int = 5) -> str | None:
        for _ in range(retries):
            devices = self._sp.devices().get("devices", [])
            for d in devices:
                if d["name"] == self._device_name:
                    return d["id"]
            time.sleep(1)
        return None

    def play(self, tag: TagEntry) -> None:
        device_id = self._find_device_id()
        if device_id is None:
            print(
                f"[spotify] device '{self._device_name}' not found - "
                "is librespot running?"
            )
            return

        print(f"[spotify] playing {tag.id} ({tag.title})")
        if tag.id.startswith("spotify:track:"):
            self._sp.start_playback(device_id=device_id, uris=[tag.id])
        else:
            self._sp.start_playback(device_id=device_id, context_uri=tag.id)
        self._playing = True

    def stop(self) -> None:
        if not self._playing:
            return
        try:
            self._sp.pause_playback()
        except spotipy.SpotifyException:
            pass  # already paused/stopped
        self._playing = False
