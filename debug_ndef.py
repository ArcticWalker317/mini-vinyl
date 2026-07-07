"""One-off diagnostic: dump raw NTAG pages with per-page retry counts."""

import time

from mini_vinyl.nfc_reader import NfcReader

reader = NfcReader()
print("Hold the written tag to the reader...")

uid = None
while uid is None:
    uid = reader.poll(timeout=0.5)
print(f"UID: {uid}")

for page in range(4, 20):
    attempts = 0
    block = None
    for attempt in range(15):
        attempts += 1
        try:
            block = reader._pn532.ntag2xx_read_block(page)
        except Exception as e:
            print(f"page {page} attempt {attempts}: EXC {e!r}")
            block = None
        if block is not None:
            break
        time.sleep(0.05)

    if block is None:
        print(f"page {page}: FAILED after {attempts} attempts")
        break
    print(f"page {page}: {block.hex()}  {bytes(block)!r}  (took {attempts} attempt(s))")
