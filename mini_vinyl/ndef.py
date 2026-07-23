"""Minimal NDEF parsing/encoding: just enough to read a URI out of a
single well-known URI record (what phone NFC-writer apps like "NFC
Tools" produce when you write a URL to a tag), and to write one back -
used to burn a library "code" (see mini_vinyl/library.py) onto a tag
with URI Identifier Code 0x00 (no prefix abbreviation), so the payload
is just the plain code text and parse_ndef_message() reads it back
unchanged.
"""

# NFC Forum URI Record Type Definition, section 3.2.2.
URI_PREFIXES = {
    0x00: "",
    0x01: "http://www.",
    0x02: "https://www.",
    0x03: "http://",
    0x04: "https://",
    0x05: "tel:",
    0x06: "mailto:",
    0x07: "ftp://anonymous:anonymous@",
    0x08: "ftp://ftp.",
    0x09: "ftps://",
    0x0A: "sftp://",
    0x0B: "smb://",
    0x0C: "nfs://",
    0x0D: "ftp://",
    0x0E: "dav://",
    0x0F: "news:",
    0x10: "telnet://",
    0x11: "imap:",
    0x12: "rtsp://",
    0x13: "urn:",
    0x14: "pop:",
    0x15: "sip:",
    0x16: "sips:",
    0x17: "tftp:",
    0x18: "btspp://",
    0x19: "btl2cap://",
    0x1A: "btgoep://",
    0x1B: "tcpobex://",
    0x1C: "irdaobex://",
    0x1D: "file://",
    0x1E: "urn:epc:id:",
    0x1F: "urn:epc:tag:",
    0x20: "urn:epc:pat:",
    0x21: "urn:epc:raw:",
    0x22: "urn:epc:",
    0x23: "urn:nfc:",
}


def parse_ndef_message(message: bytes) -> str | None:
    """Parses the first record of an NDEF message and returns its URI if
    it's a well-known URI record, else None."""
    if not message:
        return None

    header = message[0]
    tnf = header & 0x07
    sr = bool(header & 0x10)  # short record: 1-byte payload length
    il = bool(header & 0x08)  # ID length field present

    idx = 1
    if idx >= len(message):
        return None
    type_length = message[idx]
    idx += 1

    if sr:
        if idx >= len(message):
            return None
        payload_length = message[idx]
        idx += 1
    else:
        if idx + 4 > len(message):
            return None
        payload_length = int.from_bytes(message[idx : idx + 4], "big")
        idx += 4

    id_length = 0
    if il:
        if idx >= len(message):
            return None
        id_length = message[idx]
        idx += 1

    record_type = message[idx : idx + type_length]
    idx += type_length

    if il:
        idx += id_length

    payload = message[idx : idx + payload_length]

    if tnf != 0x01 or record_type != b"U" or not payload:
        return None

    prefix = URI_PREFIXES.get(payload[0], "")
    return prefix + payload[1:].decode("utf-8", errors="replace")


def encode_ndef_uri_tlv(text: str, prefix_code: int = 0x00) -> bytes:
    """Builds a complete NDEF TLV (start TLV + a single well-known URI
    record + terminator TLV) ready to write to an NTAG21x's user memory
    starting at page 4, padded to a multiple of 4 bytes (the page size).

    Inverse of parse_ndef_message(): with the default prefix_code 0x00
    (no abbreviation), parse_ndef_message() on the written bytes returns
    `text` back unchanged.
    """
    payload = bytes([prefix_code]) + text.encode("utf-8")
    if len(payload) > 255:
        raise ValueError(f"text too long to fit a short NDEF record: {len(text)} bytes")

    record = bytes([0xD1, 0x01, len(payload)]) + b"U" + payload  # TNF=1, SR=1, MB=ME=1
    if len(record) > 255:
        raise ValueError(f"text too long to fit a single-byte NDEF TLV length: {len(text)} bytes")
    tlv = bytes([0x03, len(record)]) + record + bytes([0xFE])  # NDEF TLV + terminator TLV

    if len(tlv) % 4:
        tlv += bytes(4 - len(tlv) % 4)
    return tlv
