"""Epic 21 authenticated config channel frame codecs.

Three new post-EMF ESP-NOW frame types (see protocol-manual §8):

    0x0E  UID_ANNOUNCE    Lume -> Director during an operator-triggered
                          pairing-burst window. Carries {uid, secret,
                          role, host, friendly_name}. The secret is in
                          the clear during the burst - trust model is
                          "operator intent gated the window".
    0x0F  CONFIG_WRITE    Director -> UID. Carries {target_uid,
                          numonce, property_bag_tlv, hmac}. HMAC-
                          SHA256 truncated to 8 bytes authenticates
                          everything from envelope byte 0 through
                          the end of bag_tlv.
    0x10  CONFIG_ACK      Lume -> Director, best-effort. Not
                          authenticated (a forged ack only misleads
                          the Director's UI; actual state changes
                          are gated on the authenticated CONFIG_WRITE).

Separate from frame.py because the shape is distinct: variable-length
payloads, trailing HMAC region, encode-oriented API. Keeps the
show-rendering path's Frame type focused on LIGHT_* / HEARTBEAT /
TEXT / BITMAP decoding.

Payload layouts are little-endian for every multi-byte field. See
protocol-manual.md §8 for the normative spec. Kept byte-for-byte in
sync with nocturnation-stickc's src/transport/espnow/frame.{h,cpp}.
"""

from .constants import HEADER_SIZE, MAGIC_0, MAGIC_1, PROTOCOL_VERSION
from ._generated import MessageType


# ---------------------------------------------------------------------------
# Role / host enums (shared across announce + ack flows).
# ---------------------------------------------------------------------------

class Role:
    LUME     = 0
    DIRECTOR = 1


class Host:
    STICKC   = 0
    ATOM     = 1
    TILDAGON = 2


# ---------------------------------------------------------------------------
# Field offsets within payloads. Kept here (not inside the parsers)
# so native tests can assert against them without poking at private
# strings.
# ---------------------------------------------------------------------------

UID_LEN           = 4
SECRET_LEN        = 16
NUMONCE_LEN       = 8
HMAC_LEN          = 8

UID_ANNOUNCE_MIN_LEN  = UID_LEN + SECRET_LEN + 1 + 1 + 1          # name_len=0
UID_ANNOUNCE_MAX_LEN  = UID_ANNOUNCE_MIN_LEN + 20                 # name_len=20

CONFIG_WRITE_FIXED_PREFIX = UID_LEN + NUMONCE_LEN + 1             # 13 bytes
CONFIG_WRITE_MIN_LEN      = CONFIG_WRITE_FIXED_PREFIX + 0 + HMAC_LEN     # bag_len=0
CONFIG_WRITE_MAX_BAG_LEN  = 220
CONFIG_WRITE_MAX_LEN      = CONFIG_WRITE_FIXED_PREFIX + CONFIG_WRITE_MAX_BAG_LEN + HMAC_LEN

CONFIG_ACK_LEN            = UID_LEN + NUMONCE_LEN + 1 + 1         # 14 bytes


# ---------------------------------------------------------------------------
# Little-endian helpers. MicroPython has `struct.pack('<I', ...)` but
# avoiding the module keeps us independent of its availability and
# matches the hand-rolled style the rest of the Tildagon protocol
# codec uses.
# ---------------------------------------------------------------------------

def _pack_u32(v):
    return bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF])


def _unpack_u32(buf, off):
    return (buf[off] | (buf[off + 1] << 8)
            | (buf[off + 2] << 16) | (buf[off + 3] << 24))


def _pack_u64(v):
    out = bytearray(8)
    for i in range(8):
        out[i] = (v >> (i * 8)) & 0xFF
    return bytes(out)


def _unpack_u64(buf, off):
    v = 0
    for i in range(8):
        v |= buf[off + i] << (i * 8)
    return v


# ---------------------------------------------------------------------------
# Header builder. Shared between the three types - the envelope layout
# matches the v0x04 header (9 bytes) exactly; only message_type and
# payload_len differ per frame.
# ---------------------------------------------------------------------------

def _build_header(source_id, sequence_number, hop_count, message_type, payload_len):
    return bytes([
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        source_id & 0xFF,
        (source_id >> 8) & 0xFF,
        sequence_number & 0xFF,
        hop_count & 0xFF,
        message_type & 0xFF,
        payload_len & 0xFF,
    ])


# ---------------------------------------------------------------------------
# UID_ANNOUNCE
# ---------------------------------------------------------------------------

class UidAnnouncePayload:
    __slots__ = ("uid", "secret", "role", "host", "friendly_name")

    def __init__(self, uid, secret, role, host, friendly_name=b""):
        self.uid = uid
        self.secret = bytes(secret)
        self.role = role
        self.host = host
        # Normalise to bytes; may arrive as a str from the Settings bag.
        if isinstance(friendly_name, str):
            friendly_name = friendly_name.encode("utf-8")
        if len(friendly_name) > 20:
            raise ValueError("friendly_name must be at most 20 bytes")
        self.friendly_name = bytes(friendly_name)


def encode_uid_announce(source_id, sequence_number, hop_count, payload):
    """Return the full wire-level bytes for a UID_ANNOUNCE frame."""
    if len(payload.secret) != SECRET_LEN:
        raise ValueError("secret must be 16 bytes")
    name_len = len(payload.friendly_name)
    if name_len > 20:
        raise ValueError("friendly_name longer than 20 bytes")
    body = bytearray()
    body += _pack_u32(payload.uid)
    body += payload.secret
    body.append(payload.role & 0xFF)
    body.append(payload.host & 0xFF)
    body.append(name_len)
    body += payload.friendly_name
    hdr = _build_header(source_id, sequence_number, hop_count,
                        MessageType.UID_ANNOUNCE, len(body))
    return bytes(hdr) + bytes(body)


def decode_uid_announce(payload_bytes):
    """Parse a UID_ANNOUNCE payload (post-header). Returns a
    UidAnnouncePayload. Raises ValueError on malformed input.
    """
    if len(payload_bytes) < UID_ANNOUNCE_MIN_LEN:
        raise ValueError("UID_ANNOUNCE payload too short")
    if len(payload_bytes) > UID_ANNOUNCE_MAX_LEN:
        raise ValueError("UID_ANNOUNCE payload too long")
    uid = _unpack_u32(payload_bytes, 0)
    secret = bytes(payload_bytes[UID_LEN:UID_LEN + SECRET_LEN])
    off = UID_LEN + SECRET_LEN
    role = payload_bytes[off]
    host = payload_bytes[off + 1]
    name_len = payload_bytes[off + 2]
    name_start = off + 3
    if name_len > 20 or name_start + name_len != len(payload_bytes):
        raise ValueError("UID_ANNOUNCE name_len inconsistent")
    friendly_name = bytes(payload_bytes[name_start:name_start + name_len])
    return UidAnnouncePayload(uid, secret, role, host, friendly_name)


# ---------------------------------------------------------------------------
# CONFIG_WRITE
# ---------------------------------------------------------------------------

class ConfigWritePayload:
    __slots__ = ("target_uid", "numonce", "bag_tlv", "hmac")

    def __init__(self, target_uid, numonce, bag_tlv=b"", hmac=b"\x00" * HMAC_LEN):
        self.target_uid = target_uid
        self.numonce = numonce
        self.bag_tlv = bytes(bag_tlv)
        self.hmac = bytes(hmac)


def encode_config_write_unsigned(source_id, sequence_number, hop_count,
                                 target_uid, numonce, bag_tlv):
    """Build a CONFIG_WRITE frame with a zero HMAC placeholder.

    Caller signs the returned bytes with
    `crypto.hmac_sha256.sign_t8(secret, frame[:-8])`, then overwrites
    the trailing 8 bytes with the resulting MAC. This split lets
    native tests round-trip encode/decode without pulling the crypto
    module into every protocol test.
    """
    if len(bag_tlv) > CONFIG_WRITE_MAX_BAG_LEN:
        raise ValueError("bag_tlv exceeds CONFIG_WRITE_MAX_BAG_LEN")
    body = bytearray()
    body += _pack_u32(target_uid)
    body += _pack_u64(numonce)
    body.append(len(bag_tlv))
    body += bag_tlv
    body += b"\x00" * HMAC_LEN
    hdr = _build_header(source_id, sequence_number, hop_count,
                        MessageType.CONFIG_WRITE, len(body))
    return bytes(hdr) + bytes(body)


def decode_config_write(payload_bytes):
    """Parse a CONFIG_WRITE payload (post-header). Returns a
    ConfigWritePayload. Raises ValueError on malformed input.
    """
    if len(payload_bytes) < CONFIG_WRITE_MIN_LEN:
        raise ValueError("CONFIG_WRITE payload too short")
    if len(payload_bytes) > CONFIG_WRITE_MAX_LEN:
        raise ValueError("CONFIG_WRITE payload too long")
    target_uid = _unpack_u32(payload_bytes, 0)
    numonce = _unpack_u64(payload_bytes, UID_LEN)
    bag_len = payload_bytes[UID_LEN + NUMONCE_LEN]
    if bag_len > CONFIG_WRITE_MAX_BAG_LEN:
        raise ValueError("CONFIG_WRITE bag_len exceeds max")
    bag_start = CONFIG_WRITE_FIXED_PREFIX
    hmac_start = bag_start + bag_len
    if hmac_start + HMAC_LEN != len(payload_bytes):
        raise ValueError("CONFIG_WRITE bag_len inconsistent")
    bag_tlv = bytes(payload_bytes[bag_start:bag_start + bag_len])
    hmac = bytes(payload_bytes[hmac_start:hmac_start + HMAC_LEN])
    return ConfigWritePayload(target_uid, numonce, bag_tlv, hmac)


def config_write_signed_region(frame_bytes):
    """Return the slice of a CONFIG_WRITE frame that the HMAC covers
    (envelope header + payload up to but not including the trailing
    HMAC). Used by both signer (sender) and verifier (receiver).
    """
    if len(frame_bytes) < HEADER_SIZE + CONFIG_WRITE_MIN_LEN:
        raise ValueError("frame too short for CONFIG_WRITE")
    return frame_bytes[:-HMAC_LEN]


# ---------------------------------------------------------------------------
# CONFIG_ACK
# ---------------------------------------------------------------------------

# Status values on CONFIG_ACK.
class AckStatus:
    OK              = 0
    APPLIED_PARTIAL = 1   # some keys rejected (range error, unknown)
    DECODE_FAILURE  = 2
    OTHER           = 3


class ConfigAckPayload:
    __slots__ = ("responder_uid", "responder_numonce", "status", "applied_keys")

    def __init__(self, responder_uid, responder_numonce, status, applied_keys):
        self.responder_uid = responder_uid
        self.responder_numonce = responder_numonce
        self.status = status
        self.applied_keys = applied_keys


def encode_config_ack(source_id, sequence_number, hop_count, payload):
    """Return the full wire-level bytes for a CONFIG_ACK frame."""
    body = bytearray()
    body += _pack_u32(payload.responder_uid)
    body += _pack_u64(payload.responder_numonce)
    body.append(payload.status & 0xFF)
    body.append(payload.applied_keys & 0xFF)
    assert len(body) == CONFIG_ACK_LEN
    hdr = _build_header(source_id, sequence_number, hop_count,
                        MessageType.CONFIG_ACK, len(body))
    return bytes(hdr) + bytes(body)


def decode_config_ack(payload_bytes):
    """Parse a CONFIG_ACK payload (post-header). Returns a
    ConfigAckPayload. Raises ValueError on malformed input.
    """
    if len(payload_bytes) != CONFIG_ACK_LEN:
        raise ValueError("CONFIG_ACK payload length wrong")
    responder_uid = _unpack_u32(payload_bytes, 0)
    responder_numonce = _unpack_u64(payload_bytes, UID_LEN)
    status = payload_bytes[UID_LEN + NUMONCE_LEN]
    applied_keys = payload_bytes[UID_LEN + NUMONCE_LEN + 1]
    return ConfigAckPayload(responder_uid, responder_numonce, status, applied_keys)
