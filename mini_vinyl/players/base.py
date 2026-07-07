from abc import ABC, abstractmethod

from mini_vinyl.config import TagEntry


class Player(ABC):
    """One Player subclass per source type ("youtube", "spotify", ...)."""

    @abstractmethod
    def play(self, tag: TagEntry) -> None:
        """Start playback for this tag. Must not block."""

    @abstractmethod
    def stop(self) -> None:
        """Stop/pause whatever this player is currently doing. Idempotent."""
