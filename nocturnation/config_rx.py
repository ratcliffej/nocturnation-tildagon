"""CONFIG_WRITE receive dispatcher (Epic 21).

Mirror of nocturnation-stickc's src/ble/config_rx.{h,cpp}. Called from
the main receive loop (NOT from the ESP-NOW IRQ, so HMAC verification
has the time it needs) after parse_admittable has decoded the frame
header. The caller passes the raw frame bytes alongside the Frame so
the HMAC can be computed over the exact signed region.

Verification order (silent-drop on any mismatch, no log attributable
to the forgery - no timing oracle):

  1. target_uid matches our dev_uid
  2. numonce LRU update (admits strictly-greater, rejects 0 sentinel)
     - update BEFORE HMAC verify so a flood of fake numonces can't
       exhaust the slot counter on a legitimate Director
  3. HMAC-SHA256 (truncated to 8 bytes) matches, constant-time compare
  4. property-bag TLV decodes cleanly against the known key namespace

On success the bag is applied through ble_service.apply_config_bag,
which is the single shared seam that keeps BLE writes + ESP-NOW
CONFIG_WRITE from diverging in what they do with the same bytes.

Returns a ConfigRxResult tuple describing the outcome. The caller
emits a CONFIG_ACK + visible flash on `ok=True`, drops silently
otherwise.
"""

from .ble_service import (
    STATUS_OK,
    STATUS_MALFORMED_TLV,
    STATUS_VALUE_OUT_OF_RANGE,
    apply_config_bag,
)
from .crypto.hmac_sha256 import verify_t8
from .protocol.config_frames import (
    CONFIG_WRITE_FIXED_PREFIX,
    HMAC_LEN,
    decode_config_write,
)


class ConfigRxResult:
    """Outcome of process_config_write.

    `ok` is True iff the apply succeeded (status 0 or partial). On a
    silent-drop (UID mismatch, replay, HMAC fail, decode fail) `ok` is
    False and `reason` is set for diagnostics - log at debug level,
    never emit it over the wire so an attacker can't learn why their
    frame was rejected.
    """

    __slots__ = ("ok", "status", "applied_keys", "numonce", "reason")

    def __init__(self, ok=False, status=0, applied_keys=0,
                 numonce=0, reason=""):
        self.ok = ok
        self.status = status
        self.applied_keys = applied_keys
        self.numonce = numonce
        self.reason = reason


_HEADER_SIZE = 9  # see protocol/_generated.py HEADER_SIZE


def process_config_write(frame, raw_buf, settings, numonce_lru):
    """Verify + decode + apply one CONFIG_WRITE frame.

    `frame` is the parsed Frame (any attributes beyond message_type
    + payload are ignored - we re-decode the payload here against the
    Epic 21 schema).
    `raw_buf` is the exact bytes received, needed because the HMAC
    covers the envelope header plus the payload up to but not
    including the trailing MAC.
    `settings` is the current Settings instance; dev_uid gates the
    address check and dev_secret_bytes() is the HMAC key.
    `numonce_lru` is a NumonceLru shared across the receive session.

    Returns a ConfigRxResult.
    """
    result = ConfigRxResult()
    # 1. Decode the payload. Length / shape errors here mean a malformed
    # frame slipped past parse_frame's generic size gate (which doesn't
    # cover variable-length types) - drop silently.
    try:
        payload = decode_config_write(frame.payload)
    except (ValueError, AttributeError):
        result.reason = "decode"
        return result

    # 2. Target UID gate: this is an addressing filter, not a credential
    # check. Non-match frames aren't ours to handle - drop before we
    # pay for the HMAC.
    if payload.target_uid != settings.dev_uid:
        result.reason = "not_for_us"
        return result

    # 3. Numonce replay guard. Update BEFORE verify so an unauthen-
    # ticated flood of fake-numonce frames can't exhaust the counter
    # space from under a legitimate Director. Zero is a reserved
    # sentinel and always rejected.
    if not numonce_lru.check_and_update(frame.source_id, payload.numonce):
        result.reason = "replay"
        return result

    # 4. HMAC verify over [header..end_of_bag_tlv] = raw frame minus the
    # trailing 8-byte MAC region. Constant-time compare in verify_t8.
    secret = settings.dev_secret_bytes()
    if not raw_buf:
        result.reason = "no_raw"
        return result
    signed_region = raw_buf[:-HMAC_LEN]
    if not verify_t8(secret, signed_region, payload.hmac):
        result.reason = "bad_hmac"
        return result

    # 5. Apply the bag. apply_config_bag is the shared dispatcher with
    # the BLE write path; same keys, same coercers, same STATUS_*
    # codes. Count applied keys naively from the TLV entry count minus
    # any that were out-of-range (we don't currently distinguish per-
    # key; `applied_keys` is the operator-UI friendly "something
    # happened" counter).
    status = apply_config_bag(payload.bag_tlv, settings)
    if status == STATUS_MALFORMED_TLV:
        result.reason = "malformed_tlv"
        return result

    # Count entries for the ACK's applied_keys field. decode_config_write
    # already validated bag_len; the TLV decoder's own iteration gives
    # the true count.
    from .protocol import property_bag_tlv as pb
    try:
        entries = list(pb.decode(payload.bag_tlv))
        applied = len(entries)
    except pb.TlvError:
        applied = 0

    result.ok           = True
    result.status       = status
    result.applied_keys = applied if applied <= 255 else 255
    result.numonce      = payload.numonce
    return result
