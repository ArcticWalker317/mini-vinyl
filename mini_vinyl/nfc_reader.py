"""Thin wrapper around a PN532 reader wired over I2C to a Raspberry Pi.

Only needs UID, so tags don't need to be preformatted with NDEF records -
any blank NTAG21x / Mifare tag works.
"""

import time

import board
import busio
from digitalio import DigitalInOut
from adafruit_pn532.i2c import PN532_I2C


def uid_to_str(uid: bytearray) -> str:
    return ":".join(f"{b:02X}" for b in uid)


class NfcReader:
    def __init__(self, irq_pin: int | None = None, reset_pin: int | None = None):
        i2c = busio.I2C(board.SCL, board.SDA)

        reset = DigitalInOut(getattr(board, f"D{reset_pin}")) if reset_pin else None
        self._pn532 = PN532_I2C(i2c, debug=False, reset=reset)

        ic, ver, rev, support = self._pn532.firmware_version
        print(f"Found PN532 firmware {ver}.{rev}")

        self._pn532.SAM_configuration()

    def poll(self, timeout: float = 0.3) -> str | None:
        """Returns the UID (hex, colon-separated) of a tag currently in
        range, or None if nothing is detected within `timeout` seconds."""
        uid = self._pn532.read_passive_target(timeout=timeout)
        if uid is None:
            return None
        return uid_to_str(uid)

    def wait_for_tag(self, poll_interval: float = 0.3):
        """Blocking generator yielding UID strings as tags come into range."""
        while True:
            uid = self.poll(timeout=poll_interval)
            if uid:
                yield uid
            else:
                time.sleep(0.05)
