# SPDX-License-Identifier: MIT
"""Host-side tests for the Enttec Pro framing parser."""

import pytest

from nocturnation.director.dmx_parser import (
    DmxParser,
    NEED_MORE_BYTES,
    FRAME_COMPLETE,
    RESET,
    wrap_enttec_pro,
)


def _feed_all(p, data):
    """Drive the parser byte-at-a-time; return list of result codes."""
    return [p.feed_byte(b) for b in data]


def test_roundtrip_full_universe():
    """A correctly-framed full 512-channel universe parses cleanly."""
    payload = bytes(i & 0xFF for i in range(512))
    frame = wrap_enttec_pro(payload)
    p = DmxParser()
    results = _feed_all(p, frame)
    assert FRAME_COMPLETE in results
    assert results.count(FRAME_COMPLETE) == 1
    assert p.last_label() == 0x06
    assert p.last_payload() == payload
    assert p.frames_complete() == 1
    assert p.frames_dropped() == 0


def test_short_payload_pads_correctly():
    """wrap_enttec_pro pads short payloads to 512; parser sees full."""
    frame = wrap_enttec_pro(bytes([0x42, 0x43, 0x44]))
    p = DmxParser()
    _feed_all(p, frame)
    out = p.last_payload()
    assert len(out) == 512
    assert out[:3] == b"\x42\x43\x44"
    assert out[3:] == b"\x00" * 509


def test_garbage_before_sync_silently_dropped():
    """Bytes before the first 0x7E don't trip the parser."""
    payload = bytes(range(12)) + b"\x00" * 500
    frame = wrap_enttec_pro(payload)
    junk = b"\x00\xFF\xAA\x55" + frame
    p = DmxParser()
    results = _feed_all(p, junk)
    assert results.count(FRAME_COMPLETE) == 1
    assert p.last_payload() == payload


def test_bad_end_byte_drops_frame():
    """End byte != 0xE7 causes the frame to be dropped + resync."""
    payload = bytes(range(12)) + b"\x00" * 500
    frame = bytearray(wrap_enttec_pro(payload))
    frame[-1] = 0xFF   # corrupt end byte
    p = DmxParser()
    results = _feed_all(p, bytes(frame))
    assert FRAME_COMPLETE not in results
    assert RESET in results
    assert p.frames_complete() == 0
    assert p.frames_dropped() == 1


def test_payload_byte_0x7E_is_data_not_resync():
    """0x7E (126) is a legitimate channel value; the parser must not
    treat it as a frame-start sync marker once inside the payload.
    Real DMX universes routinely contain 0x7E channel values."""
    # Build a payload that contains 0x7E in several channel positions.
    payload = bytearray(512)
    payload[0] = 0x7E
    payload[100] = 0x7E
    payload[256] = 0x7E
    payload[511] = 0x7E
    frame = wrap_enttec_pro(bytes(payload))
    p = DmxParser()
    results = _feed_all(p, frame)
    # Exactly one frame parsed; no drops; payload preserved verbatim.
    assert results.count(FRAME_COMPLETE) == 1
    assert p.frames_dropped() == 0
    assert p.last_payload() == bytes(payload)


def test_header_resync_on_interrupted_frame():
    """A 0x7E appearing while we're reading the header (label / length
    bytes) is taken as a new frame-start, dropping the previous
    in-progress header. This is the legitimate use of the resync logic."""
    p = DmxParser()
    # Feed 0x7E + 0x06 (label) - parser is now in LEN_LO state.
    p.feed_byte(0x7E)
    p.feed_byte(0x06)
    # Now an unexpected 0x7E - parser should drop + restart.
    r = p.feed_byte(0x7E)
    assert r == RESET
    assert p.frames_dropped() == 1
    # The new 0x7E was consumed as a fresh frame start; parser is now
    # in LABEL state. Complete a small valid frame from here.
    rest = bytes((0x06, 0x01, 0x02, 0x00)) + b"\xAB" * 512 + bytes((0xE7,))
    results = [p.feed_byte(b) for b in rest]
    assert FRAME_COMPLETE in results
    assert p.last_payload() == b"\xAB" * 512


def test_back_to_back_frames():
    """Two valid frames in a row parse independently."""
    f1 = wrap_enttec_pro(b"\x01" * 12 + b"\x00" * 500)
    f2 = wrap_enttec_pro(b"\x02" * 12 + b"\x00" * 500)
    stream = f1 + f2
    p = DmxParser()
    results = _feed_all(p, stream)
    assert results.count(FRAME_COMPLETE) == 2
    assert p.frames_complete() == 2
    # last_payload reflects the most recent.
    assert p.last_payload()[:12] == b"\x02" * 12


def test_oversize_length_field_rejected():
    """A length field > _MAX_PAYLOAD is dropped without consuming the
    huge buffer that would follow."""
    # Manually craft a frame with length 0xFFFF.
    bad = bytes((0x7E, 0x06, 0xFF, 0xFF))
    p = DmxParser()
    results = _feed_all(p, bad)
    assert RESET in results
    assert p.frames_complete() == 0
    assert p.frames_dropped() == 1


def test_zero_length_rejected():
    """Length of 0 is a malformed frame."""
    bad = bytes((0x7E, 0x06, 0x00, 0x00))
    p = DmxParser()
    results = _feed_all(p, bad)
    assert RESET in results
    assert p.frames_complete() == 0


def test_feed_bytes_yields_completes():
    """The feed_bytes convenience method yields FRAME_COMPLETE per
    parsed frame and nothing else."""
    f1 = wrap_enttec_pro(b"\x01" * 512)
    f2 = wrap_enttec_pro(b"\x02" * 512)
    p = DmxParser()
    completes = list(p.feed_bytes(f1 + f2))
    assert completes == [FRAME_COMPLETE, FRAME_COMPLETE]
