"""Hands a "please write this code onto the next tag placed on the
reader" request from the web UI's write route (mini_vinyl/web.py) over to
the NFC poll loop (mini_vinyl/main.py), which owns the PN532 hardware and
is the only thing allowed to touch it. The two sides never call into each
other directly - they only ever go through this lock-protected mailbox.
"""

import threading
import time
from dataclasses import dataclass, field

DEFAULT_TIMEOUT = 30.0


@dataclass
class WriteRequest:
    code: str
    deadline: float
    status: str = "waiting"  # waiting | success | error | timeout | superseded
    detail: str = field(default="")


class WriteCoordinator:
    def __init__(self):
        self._lock = threading.Lock()
        self._current: WriteRequest | None = None

    def start(self, code: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Arms a new pending write, superseding any prior one still
        waiting (rather than leaving its browser tab polling a request
        that's silently been replaced)."""
        with self._lock:
            if self._current is not None and self._current.status == "waiting":
                self._current.status = "superseded"
            self._current = WriteRequest(code=code, deadline=time.time() + timeout)

    def take_pending(self) -> WriteRequest | None:
        """Called from the poll loop when a tag is present. Returns the
        live request to act on, or None if there's nothing waiting (or
        it just expired)."""
        with self._lock:
            req = self._current
            if req is None or req.status != "waiting":
                return None
            if time.time() > req.deadline:
                req.status = "timeout"
                return None
            return req

    def resolve(self, code: str, success: bool, detail: str = "") -> None:
        with self._lock:
            if self._current is not None and self._current.code == code and self._current.status == "waiting":
                self._current.status = "success" if success else "error"
                self._current.detail = detail

    def status_for(self, code: str) -> dict:
        with self._lock:
            if self._current is None or self._current.code != code:
                return {"status": "superseded"}
            if self._current.status == "waiting" and time.time() > self._current.deadline:
                self._current.status = "timeout"
            return {"status": self._current.status, "detail": self._current.detail}
