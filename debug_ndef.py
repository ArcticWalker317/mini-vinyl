"""One-off diagnostic: dump raw NTAG pages so we can see what's actually
on the tag when read_ndef_uri() returns None."""

from mini_vinyl.nfc_reader import NfcReader

reader = NfcReader()
print("Hold the written tag to the reader...")

uid = None
while uid is None:
    uid = reader.poll(timeout=0.5)
print(f"UID: {uid}")

for page in range(4, 20):
    try:
        block = reader._pn532.ntag2xx_read_block(page)
    except Exception as e:
        print(f"page {page}: ERROR {e!r}")
        break
    if block is None:
        print(f"page {page}: None (read failed)")
        break
    print(f"page {page}: {block.hex()}  {bytes(block)!r}")
