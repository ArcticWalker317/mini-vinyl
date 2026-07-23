"""Thin wrapper around a PN532 reader wired over I2C to a Raspberry Pi.

Tags are expected to be NTAG213/215/216 stickers with a single NDEF URI
record written to them - either a full YouTube URL (e.g. written via a
phone app like "NFC Tools") read straight off the tag with no on-Pi
mapping needed, or a short library "code" burned on by this project's own
web UI via write_ndef_uri(), looked up in library.json at playback time.
"""

import time

import board
import busio
from digitalio import DigitalInOut
from adafruit_pn532.i2c import PN532_I2C

from mini_vinyl.ndef import encode_ndef_uri_tlv, parse_ndef_message


def uid_to_str(uid: bytearray) -> str:
    return ":".join(f"{b:02X}" for b in uid)


class NfcReader:
    def __init__(
        self,
        irq_pin: int | None = None,
        reset_pin: int | None = None,
        init_retries: int = 3,
        init_retry_delay: float = 1.0,
    ):
        i2c = busio.I2C(board.SCL, board.SDA)
        reset = DigitalInOut(getattr(board, f"D{reset_pin}")) if reset_pin else None

        # Right after a previous process exits, or right at boot before the
        # PN532 has finished powering up, it sometimes isn't ready yet.
        # That shows up two different ways: init raises
        # RuntimeError("Did not receive expected ACK") if the PN532 ACKs
        # the bus but doesn't handshake, or ValueError("No I2C device at
        # address...") from the underlying I2CDevice probe if it doesn't
        # even ACK the bus yet. Retrying after a short pause reliably
        # recovers from both.
        last_exc: RuntimeError | ValueError | None = None
        self._pn532 = None
        for attempt in range(init_retries):
            try:
                self._pn532 = PN532_I2C(i2c, debug=False, reset=reset)
                break
            except (RuntimeError, ValueError) as exc:
                last_exc = exc
                time.sleep(init_retry_delay)
        if self._pn532 is None:
            raise last_exc

        ic, ver, rev, support = self._pn532.firmware_version
        print(f"Found PN532 firmware {ver}.{rev}")

        self._pn532.SAM_configuration()

    def poll(self, timeout: float = 0.3) -> str | None:
        """Returns the UID (hex, colon-separated) of a tag currently in
        range, or None if nothing is detected within `timeout` seconds."""
        try:
            uid = self._pn532.read_passive_target(timeout=timeout)
        except RuntimeError:
            # Occasional I2C ACK hiccup, even mid-playback with a tag
            # sitting still. Treat it like a missed poll rather than
            # crashing the whole player - the caller already tolerates
            # a few consecutive misses before deciding a tag is gone.
            return None
        if uid is None:
            return None
        return uid_to_str(uid)

    def _read_page(self, page: int, retries: int = 8, retry_delay: float = 0.05):
        """A single ntag2xx_read_block call occasionally comes back None
        (I2C timing hiccup / tag briefly out of range) even mid-read of a
        tag that's otherwise sitting still - retry a few times before
        giving up on the whole read."""
        for attempt in range(retries):
            try:
                block = self._pn532.ntag2xx_read_block(page)
            except RuntimeError:
                block = None
            if block is not None:
                return block
            time.sleep(retry_delay)
        return None

    def _read_ndef_uri_once(self, max_pages: int, inter_page_delay: float) -> str | None:
        data = bytearray()
        for page in range(4, max_pages):
            block = self._read_page(page)
            if block is None:
                return None
            data += block
            time.sleep(inter_page_delay)  # avoid hammering the I2C bus

            if len(data) >= 2 and data[0] == 0x03:
                length = data[1]
                if len(data) >= 2 + length:
                    return parse_ndef_message(bytes(data[2 : 2 + length]))

        return None

    def read_ndef_uri(
        self, max_pages: int = 42, inter_page_delay: float = 0.02, attempts: int = 3
    ) -> str | None:
        """Reads the NDEF TLV starting at page 4 of an NTAG21x tag
        currently in range and returns the URI from its first record, or
        None if there's no tag, no NDEF data, or it's not a URI record.

        A multi-page read takes long enough (a dozen+ separate PN532
        transactions) that marginal RF coupling - e.g. a tag that's
        slightly off-center or gets nudged mid-read - can desync the
        PN532 from the tag partway through, at which point per-page
        retries alone won't help. If a read comes up short, re-select the
        tag from scratch and try the whole thing again.
        """
        for _ in range(attempts):
            if self._pn532.read_passive_target(timeout=0.5) is None:
                return None  # tag no longer in range at all
            uri = self._read_ndef_uri_once(max_pages, inter_page_delay)
            if uri is not None:
                return uri
        return None

    def _write_page(self, page: int, data: bytes, retries: int = 8, retry_delay: float = 0.05) -> bool:
        """Mirrors _read_page's retry behavior for the write side."""
        for attempt in range(retries):
            try:
                if self._pn532.ntag2xx_write_block(page, data):
                    return True
            except RuntimeError:
                pass
            time.sleep(retry_delay)
        return False

    def write_ndef_uri(self, text: str, attempts: int = 3) -> bool:
        """Writes `text` as a well-known URI record (no prefix abbreviation,
        so it reads back byte-for-byte via read_ndef_uri()/parse_ndef_message)
        starting at page 4 of an NTAG21x tag currently in range. Returns
        whether the write succeeded.

        Like read_ndef_uri(), a tag nudged mid-write can desync the PN532
        from the tag partway through a multi-page write; re-select and
        retry the whole write rather than resuming a partial one.
        """
        tlv = encode_ndef_uri_tlv(text)
        pages = [tlv[i : i + 4] for i in range(0, len(tlv), 4)]

        for _ in range(attempts):
            if self._pn532.read_passive_target(timeout=0.5) is None:
                return False  # tag no longer in range at all
            if all(self._write_page(4 + i, page) for i, page in enumerate(pages)):
                return True
        return False

    def wait_for_tag(self, poll_interval: float = 0.3):
        """Blocking generator yielding UID strings as tags come into range."""
        while True:
            uid = self.poll(timeout=poll_interval)
            if uid:
                yield uid
            else:
                time.sleep(0.05)
