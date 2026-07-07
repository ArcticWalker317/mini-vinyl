"""Minimal NDEF parsing: just enough to pull a URI out of a single
well-known URI record, which is what phone NFC-writer apps (e.g. "NFC
Tools") produce when you write a URL to a tag.
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
