from mini_vinyl.config import TagEntry
from mini_vinyl.players.base import Player


class PlayerManager:
    """Routes tag events to the right Player and guarantees only one
    source is ever playing at a time."""

    def __init__(self, players: dict[str, Player]):
        self._players = players  # e.g. {"youtube": ..., "spotify": ...}
        self._active: Player | None = None

    def handle_tag_present(self, tag: TagEntry) -> None:
        player = self._players.get(tag.type)
        if player is None:
            print(f"[player_manager] no player registered for type '{tag.type}'")
            return

        if self._active is not None and self._active is not player:
            self._active.stop()

        player.play(tag)
        self._active = player

    def handle_tag_absent(self) -> None:
        if self._active is not None:
            self._active.stop()
            self._active = None
