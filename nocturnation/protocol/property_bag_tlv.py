"""Property-bag TLV codec (Epic 20 B5).

Bit-compatible with the C++ implementation in nocturnation-stickc's
src/ble/property_bag_tlv.{h,cpp} - both firmware sides encode and decode
the same wire format defined in Docs/manuals/ble-service.md section 4.

Wire format (packed, no padding):

    u8   entry_count
    repeat entry_count times:
      u8    key_len
      u8[]  key                  # ASCII, NOT null-terminated
      u8    value_type            # 0=u8, 1=u16 LE, 2=u32 LE, 3=bytes, 4=utf8, 5=bool
      u8    value_len
      u8[]  value                 # encoding per value_type

MicroPython gotchas to remember (see feedback-micropython-stdlib-gotchas):
    - No struct.pack format-string fallbacks; keep to standard int/bytes.
    - No dataclasses; use plain tuples / lists.
    - bytearray, memoryview, bytes are all present and behave as CPython.
"""


# Value types (must match src/ble/property_bag_tlv.h ValueType enum).
TYPE_U8 = 0x00
TYPE_U16 = 0x01
TYPE_U32 = 0x02
TYPE_BYTES = 0x03
TYPE_UTF8 = 0x04
TYPE_BOOL = 0x05


# Well-known keys (must match src/ble/property_bag_tlv.h key:: namespace).

# Lume role.
KEY_GROUP = "group"
KEY_LED_POWER = "led_power"
KEY_BOUND_SID = "bound_sid"
KEY_CHANNEL_PREF = "channel_pref"
KEY_STRIP_CHAIN = "strip_chain"
KEY_STRIP_GROUP_SIZE = "strip_group_size"

# Director role.
KEY_DIR_SID_PERF = "dir_sid_perf"
KEY_RETX_COUNT = "retx_count"

# Both roles.
KEY_PAIR_WIN_S = "pair_win_s"
KEY_FRIENDLY_NAME = "friendly_name"


class TlvError(Exception):
    """Raised by decode() on malformed input; matches C++
    DecodeError::Malformed semantics (offset overrun, zero key_len)."""


def encode(entries):
    """Encode an iterable of (key, value_type, value) tuples into bytes.

    key is str; value is bytes (already encoded per value_type). Use
    the helpers below to build primitive types.

    Raises ValueError if a key is empty, longer than 255 bytes, or the
    entry count would exceed 255.
    """
    entries = list(entries)
    if len(entries) > 255:
        raise ValueError("too many entries (max 255)")

    out = bytearray()
    out.append(len(entries))
    for key, value_type, value in entries:
        key_bytes = key.encode("ascii")
        if len(key_bytes) == 0 or len(key_bytes) > 255:
            raise ValueError("key length out of range: %r" % key)
        if len(value) > 255:
            raise ValueError("value length out of range for %r" % key)
        out.append(len(key_bytes))
        out.extend(key_bytes)
        out.append(value_type)
        out.append(len(value))
        out.extend(value)
    return bytes(out)


def decode(buf):
    """Yield (key, value_type, value) tuples from a TLV buffer.

    value is a bytes slice into buf; caller decodes per value_type using
    the helpers below (u8_from, u16_from, etc.).

    Raises TlvError on malformed input - key_len overruns, value_len
    overruns, or zero key_len. Matches C++ DecodeError::Malformed.
    """
    if buf is None or len(buf) == 0:
        return
    entry_count = buf[0]
    off = 1
    n = len(buf)
    for _ in range(entry_count):
        if off + 1 > n:
            raise TlvError("truncated at key_len")
        key_len = buf[off]
        off += 1
        if key_len == 0:
            raise TlvError("empty key")
        if off + key_len > n:
            raise TlvError("key overruns buffer")
        key = bytes(buf[off:off + key_len]).decode("ascii")
        off += key_len

        if off + 1 > n:
            raise TlvError("truncated at value_type")
        value_type = buf[off]
        off += 1
        if off + 1 > n:
            raise TlvError("truncated at value_len")
        value_len = buf[off]
        off += 1
        if off + value_len > n:
            raise TlvError("value overruns buffer")
        value = bytes(buf[off:off + value_len])
        off += value_len

        yield (key, value_type, value)


# ---- Primitive helpers -----------------------------------------------------

def u8(v):
    """Encode a uint8 as a single-byte bytes object."""
    return bytes((v & 0xFF,))


def u16(v):
    """Encode a uint16 as 2 bytes LE."""
    return bytes((v & 0xFF, (v >> 8) & 0xFF))


def u32(v):
    """Encode a uint32 as 4 bytes LE."""
    return bytes((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF))


def boolean(v):
    """Encode a bool as 0x00/0x01."""
    return bytes((0x01,)) if v else bytes((0x00,))


def utf8(s):
    """Encode a str as UTF-8 bytes (not NUL-terminated)."""
    return s.encode("utf-8")


# ---- Reader helpers --------------------------------------------------------

def u8_from(value):
    """Read a uint8 from a decoded value slice. Raises ValueError if
    the slice isn't 1 byte."""
    if len(value) != 1:
        raise ValueError("expected 1 byte, got %d" % len(value))
    return value[0]


def u16_from(value):
    """Read a uint16 LE from a decoded value slice."""
    if len(value) != 2:
        raise ValueError("expected 2 bytes, got %d" % len(value))
    return value[0] | (value[1] << 8)


def u32_from(value):
    """Read a uint32 LE from a decoded value slice."""
    if len(value) != 4:
        raise ValueError("expected 4 bytes, got %d" % len(value))
    return (value[0]
            | (value[1] << 8)
            | (value[2] << 16)
            | (value[3] << 24))


def bool_from(value):
    """Read a bool from a decoded value slice. Non-standard values
    treated as truthy (implementation-defined per spec section 4)."""
    if len(value) != 1:
        raise ValueError("expected 1 byte, got %d" % len(value))
    return bool(value[0])


def utf8_from(value):
    """Read a str from a decoded value slice (assumed valid UTF-8)."""
    return value.decode("utf-8")
