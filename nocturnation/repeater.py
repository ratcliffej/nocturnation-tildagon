"""Dynamic repeater FSM for audience-Tildagon mesh coverage.

Runs on each Tildagon in Lume mode when Repeat=AUTO. Observes admitted
frames from the app receive path, elects itself as a relay when a
per-output-hop vacancy is detected, and steps down when a peer takes
over. Cap at hop=3 enables up to three relay hops through the audience.

Pure logic (send / random / clock callbacks), so the full state machine
is exercised by host-side pytest without needing the badge radio.

State overview and design rationale: docs/tildagon-history.md.

ESP-NOW broadcast does NOT loop back on this platform - any received
frame matching a TX-ring entry is by construction from a peer device.
"""

# Byte offset of hop_count in the wire header. Sourced from the protocol
# constants module so a future header-layout change (e.g. the v2 -> v3
# source_id widening that broke this in 2026-08) only has to update one
# authority - see docs/tildagon-history.md for the drift incident.
from .protocol.constants import HOP_COUNT_OFFSET as HOP_COUNT_BYTE_OFFSET


# Wire-spec hop_count ceiling (protocol manual section 2.3). Frames at
# this hop are received but not relayed; system-wide limit.
MAX_HOP_COUNT = 3

# LISTENING / CANDIDATE per-frame peer-watch window. Sized for LR
# ESP-NOW airtime (~1.3 ms) + scheduling jitter, with 3-10x headroom.
PEER_WATCH_MS = 100

# CANDIDATE frame counter range. Minimum 2 absorbs the ~2% single-frame
# peer-relay loss; probability of two consecutive failed peer relays
# from a healthy peer is 0.04%.
CANDIDATE_MIN_FRAMES = 2
CANDIDATE_MAX_FRAMES = 9

# COOLDOWN counter range. Longer than CANDIDATE because we are already
# relaying during the assessment - no service interruption.
COOLDOWN_MIN_FRAMES = 5
COOLDOWN_MAX_FRAMES = 20

# Admitted frames without a matching peer-ring hit before we conclude
# the peer has stepped down and we can return to ACTIVE.
COOLDOWN_PEER_STOPPED_FRAMES = 3

# Bounded ring capacities. Sized larger than the in-flight frame count
# (retransmits + a few frames of scheduling latency) without unbounded
# growth.
TX_RING_SIZE = 32
WATCH_RING_SIZE = 32
RECENT_HOP_MEMORY_SIZE = 64

# ACTIVE / COOLDOWN idle timeout: without this, an ACTIVE(hop 2) device
# stayed stuck when its hop 1 upstream was turned off (received hop=0
# from Director but out of role to relay it). 3 s ~= 3 heartbeat cycles.
ACTIVE_IDLE_TIMEOUT_MS = 3000

# String state constants (no IntEnum on MicroPython). Log output and
# debug overlays are self-describing without a lookup table.
STATE_LISTENING = "LISTENING"
STATE_CANDIDATE = "CANDIDATE"
STATE_ACTIVE    = "ACTIVE"
STATE_COOLDOWN  = "COOLDOWN"


def _ticks_diff(a, b):
    # Plain int subtraction works on both because now_ms is monotonic
    # within a session; CPython doesn't have time.ticks_diff.
    return a - b


class DynamicRepeater:
    """Per-Tildagon dynamic repeater FSM.

    Not thread-safe. Called from the app receive loop only.

    Callbacks the caller must supply:

      send_fn(payload_bytes)     TX the given ESP-NOW broadcast payload.
                                 Called by ACTIVE / COOLDOWN when we
                                 elect to relay.

      random_int_fn(lo, hi)      Return a uniform random integer in
                                 [lo, hi].
    """

    def __init__(self, send_fn, random_int_fn, now_ms=0,
                 on_relay_state_change=None, skip_tx=False):
        self._send_fn = send_fn
        self._random_int = random_int_fn
        # Notified whenever the FSM enters or leaves a relay-eligible
        # state. Signature: fn(enabled: bool, output_hop: int). Used by
        # the app to keep the IRQ-context fast relay path in step with
        # the FSM's elected role.
        self._on_relay_state_change = on_relay_state_change
        # When True, _transmit_relay skips the actual send_fn() call
        # (the IRQ-context handler in app.py already TX'd). Bookkeeping
        # still runs so peer detection stays accurate. When False, this
        # is the sole TX path.
        self._skip_tx = skip_tx

        self._state = STATE_LISTENING
        self._state_entered_ms = now_ms

        self._target_frames = 0
        self._frames_in_state = 0

        # {src, seq, hop, deadline_ms} entries. List rather than dict so
        # we can evict oldest when full without a separate ordering
        # structure.
        self._watches = []

        # Lowest hop each recent (src, seq) has been received at. Gates
        # watch creation: only arm a peer-watch at the LOWEST hop we
        # personally receive a frame at. See docs/tildagon-history.md.
        self._recent_lo = {}
        self._recent_lo_order = []

        self._tx_ring = []

        # How many admitted frames since we last observed a peer relay
        # matching our TX ring.
        self._frames_since_peer = 0

        # Elected output hop: the hop level we would (CANDIDATE) or do
        # (ACTIVE/COOLDOWN) TX at. Preserved through the CANDIDATE ->
        # ACTIVE -> COOLDOWN chain; reset in _to_listening. Two
        # load-bearing invariants (see docs/tildagon-history.md):
        # (1) CANDIDATE cancel triggers only on peer traffic at THIS
        # hop. (2) ACTIVE/COOLDOWN relay ONLY frames where hop+1
        # matches this hop.
        self._output_hop = 0

        # Drives ACTIVE/COOLDOWN idle timeout.
        self._last_relay_ms = 0

        # Session stats surfaced to the debug overlay.
        self._relayed_count  = 0
        self._peer_seen_count = 0
        self._elections      = 0
        self._activations    = 0

    # ------------------------------------------------------------------
    # Public API - called from the app.
    # ------------------------------------------------------------------

    @property
    def state(self):
        return self._state

    @property
    def relayed_count(self):
        return self._relayed_count

    @property
    def peer_seen_count(self):
        return self._peer_seen_count

    def state_label(self):
        return {
            STATE_LISTENING: "LISTEN",
            STATE_CANDIDATE: "LISTEN?",
            STATE_ACTIVE:    "REPEAT",
            STATE_COOLDOWN:  "CDOWN",
        }[self._state]

    def on_admitted_frame(self, frame, is_duplicate, now_ms, raw_buf):
        """Observer entry point. Called by app for every admitted frame.

        ``frame`` is the parsed Frame; ``is_duplicate`` is the dedup
        result (True = dedup-suppressed for rendering). ``now_ms`` is a
        monotonic millisecond clock; may be None in host tests. ``raw_buf``
        is the untouched wire payload so we can relay by mutating the
        hop_count byte (see HOP_COUNT_BYTE_OFFSET / constants.HOP_COUNT_OFFSET).
        """
        if now_ms is None:
            # Host tests may not supply a clock. Fake a monotonic tick
            # per admission - peer-watch expiry needs a monotonic tick.
            now_ms = self._state_entered_ms + self._frames_in_state * 1000

        # Fires the "uncovered" trigger in LISTENING / CANDIDATE if no
        # peer relay arrived in time.
        self._expire_watches(now_ms)

        # A peer's relay at our TX'd hop is by construction a duplicate
        # from our POV, so this runs against duplicates too.
        peer_hit = self._match_tx_ring(frame)
        if peer_hit:
            self._peer_seen_count += 1
            self._frames_since_peer = 0
        else:
            # Only tick since-peer counter while in COOLDOWN.
            if self._state == STATE_COOLDOWN:
                self._frames_since_peer += 1

        # A frame at hop=N+1 matching (src, seq) of a watched frame at
        # hop=N covers the watch.
        covered_watch = self._resolve_watch_cover(frame)

        if self._state == STATE_LISTENING:
            self._on_frame_listening(frame, now_ms, covered_watch)
        elif self._state == STATE_CANDIDATE:
            self._on_frame_candidate(frame, now_ms,
                                     covered_watch=covered_watch,
                                     peer_hit=peer_hit)
        elif self._state == STATE_ACTIVE:
            self._on_frame_active(frame, is_duplicate, now_ms, raw_buf,
                                  peer_hit=peer_hit)
        elif self._state == STATE_COOLDOWN:
            self._on_frame_cooldown(frame, is_duplicate, now_ms, raw_buf,
                                    peer_hit=peer_hit)

        # New peer-watch if this frame is a potential input to a future
        # relay AND this is the lowest hop we hear it at. Lowest-hop
        # gating stops a device that also receives the frame closer to
        # the source from seeding an outer-hop election it can neither
        # hand off nor usefully serve.
        watch_worthy = self._note_hop_should_watch(frame)
        if watch_worthy and frame.hop_count < MAX_HOP_COUNT:
            self._add_watch(frame, now_ms)

    def tick(self, now_ms):
        """Time-based sweep from the app's poll loop.

        Watch expiry normally fires on the next admitted frame, but in
        complete quiet we still need the FSM to eventually trigger.
        Also enforces the ACTIVE/COOLDOWN idle timeout.
        """
        self._expire_watches(now_ms)
        if self._state == STATE_ACTIVE or self._state == STATE_COOLDOWN:
            if _ticks_diff(now_ms, self._last_relay_ms) \
                    >= ACTIVE_IDLE_TIMEOUT_MS:
                self._to_listening(now_ms)

    # ------------------------------------------------------------------
    # State handlers.
    # ------------------------------------------------------------------

    def _on_frame_listening(self, frame, now_ms, covered_watch):
        # LISTENING transitions to CANDIDATE only via watch expiry
        # (already fired by _expire_watches).
        pass

    def _on_frame_candidate(self, frame, now_ms, covered_watch, peer_hit):
        # Cancel ONLY on peer traffic at MY target output hop. Covering
        # ANY watch previously fired the cancel, causing LISTENING <->
        # CANDIDATE oscillation when an engineered hop 1 repeater was
        # active but hop 2 was still vacant. See docs/tildagon-history.md.
        if frame.hop_count == self._output_hop:
            self._to_listening(now_ms)
            return
        self._frames_in_state += 1
        if self._frames_in_state >= self._target_frames:
            self._to_active(now_ms)

    def _on_frame_active(self, frame, is_duplicate, now_ms, raw_buf,
                         peer_hit):
        # ACTIVE relays ONLY frames where hop+1 matches our elected
        # output hop - a hop 2 device ignores hop=0 inputs so it doesn't
        # compete with the engineered hop 1 repeater.
        if not is_duplicate \
                and frame.hop_count + 1 == self._output_hop \
                and self._output_hop <= MAX_HOP_COUNT \
                and raw_buf is not None and self._send_fn is not None:
            self._transmit_relay(frame, raw_buf, now_ms)
        if peer_hit:
            self._to_cooldown(now_ms)

    def _on_frame_cooldown(self, frame, is_duplicate, now_ms, raw_buf,
                           peer_hit):
        if not is_duplicate \
                and frame.hop_count + 1 == self._output_hop \
                and self._output_hop <= MAX_HOP_COUNT \
                and raw_buf is not None and self._send_fn is not None:
            self._transmit_relay(frame, raw_buf, now_ms)

        # Peer relays that stopped arriving flip us back to ACTIVE.
        if self._frames_since_peer >= COOLDOWN_PEER_STOPPED_FRAMES:
            self._to_active(now_ms)
            return

        # Otherwise count the frame against our cooldown window; on
        # expiry with peer still active, step down to LISTENING.
        self._frames_in_state += 1
        if self._frames_in_state >= self._target_frames:
            self._to_listening(now_ms)

    # ------------------------------------------------------------------
    # State transitions.
    # ------------------------------------------------------------------

    def _to_candidate(self, now_ms, output_hop):
        self._state = STATE_CANDIDATE
        self._state_entered_ms = now_ms
        self._target_frames = self._random_int(CANDIDATE_MIN_FRAMES,
                                                CANDIDATE_MAX_FRAMES)
        self._frames_in_state = 0
        # Set here and preserved through ACTIVE / COOLDOWN until we step
        # back to LISTENING.
        self._output_hop = output_hop
        self._elections += 1

    def _to_active(self, now_ms):
        self._state = STATE_ACTIVE
        self._state_entered_ms = now_ms
        self._target_frames = 0
        self._frames_in_state = 0
        self._frames_since_peer = 0
        # Prime the idle-timeout clock so a freshly-elected ACTIVE
        # doesn't immediately step down before it's had a chance to
        # relay.
        self._last_relay_ms = now_ms
        self._activations += 1
        if self._on_relay_state_change is not None:
            self._on_relay_state_change(True, self._output_hop)

    def _to_cooldown(self, now_ms):
        self._state = STATE_COOLDOWN
        self._state_entered_ms = now_ms
        self._target_frames = self._random_int(COOLDOWN_MIN_FRAMES,
                                                COOLDOWN_MAX_FRAMES)
        self._frames_in_state = 0
        self._frames_since_peer = 0
        # Still relay-enabled (COOLDOWN keeps relaying at the same hop).
        if self._on_relay_state_change is not None:
            self._on_relay_state_change(True, self._output_hop)

    def _to_listening(self, now_ms):
        self._state = STATE_LISTENING
        self._state_entered_ms = now_ms
        self._target_frames = 0
        self._frames_in_state = 0
        self._output_hop = 0
        # Any inbound frame hitting the IRQ handler now will NOT be
        # relayed.
        if self._on_relay_state_change is not None:
            self._on_relay_state_change(False, 0)

    # ------------------------------------------------------------------
    # Peer-watch mechanics.
    # ------------------------------------------------------------------

    def _note_hop_should_watch(self, frame):
        """Record the lowest hop (src, seq) has been received at and
        report whether this frame warrants a peer-watch.

        Returns True on the first sighting of (src, seq), or when a
        strictly-lower hop arrives out of order (which also cancels any
        higher-hop watch already armed for it). Returns False for an
        equal-or-higher-hop copy. seq == 0 keeps the prior always-watch
        behaviour.
        """
        seq = frame.sequence_number
        if seq == 0:
            return True
        key = (frame.source_id, seq)
        prev = self._recent_lo.get(key)
        if prev is None:
            self._recent_lo[key] = frame.hop_count
            self._recent_lo_order.append(key)
            if len(self._recent_lo_order) > RECENT_HOP_MEMORY_SIZE:
                evicted = self._recent_lo_order.pop(0)
                self._recent_lo.pop(evicted, None)
            return True
        if frame.hop_count < prev:
            # A lower-hop copy arrived out of order - supersedes any
            # higher-hop watch we already armed for this frame.
            self._recent_lo[key] = frame.hop_count
            self._cancel_watches_above(frame.source_id, seq, frame.hop_count)
            return True
        return False

    def _cancel_watches_above(self, src, seq, hop):
        # A lower-hop sighting obsoletes higher-hop watches for the
        # same (src, seq).
        self._watches = [w for w in self._watches
                         if not (w[0] == src and w[1] == seq and w[2] > hop)]

    def _add_watch(self, frame, now_ms):
        deadline = now_ms + PEER_WATCH_MS
        entry = (frame.source_id, frame.sequence_number,
                 frame.hop_count, deadline)
        self._watches.append(entry)
        while len(self._watches) > WATCH_RING_SIZE:
            self._watches.pop(0)

    def _resolve_watch_cover(self, frame):
        # hop=0 frames never trigger coverage (no watch at hop=-1 exists).
        if frame.hop_count == 0:
            return False
        watched_hop = frame.hop_count - 1
        for i, (src, seq, hop, _dl) in enumerate(self._watches):
            if src == frame.source_id and seq == frame.sequence_number \
                    and hop == watched_hop:
                self._watches.pop(i)
                return True
        return False

    def _expire_watches(self, now_ms):
        # If multiple watches expire in one sweep, elect for the
        # LOWEST-hop vacancy first (filling hop 1 might obviate the hop
        # 2 need). Chosen vacancy hop becomes CANDIDATE's target output.
        lowest_expired_hop = None
        i = 0
        while i < len(self._watches):
            _src, _seq, hop, dl = self._watches[i]
            if _ticks_diff(now_ms, dl) >= 0:
                if lowest_expired_hop is None or hop < lowest_expired_hop:
                    lowest_expired_hop = hop
                self._watches.pop(i)
            else:
                i += 1
        if lowest_expired_hop is not None and self._state == STATE_LISTENING:
            # output_hop = triggering_watch_hop + 1: a hop=0 watch
            # expiring uncovered elects our role at hop=1.
            self._to_candidate(now_ms, lowest_expired_hop + 1)

    # ------------------------------------------------------------------
    # TX ring mechanics.
    # ------------------------------------------------------------------

    def _record_tx(self, src, seq, hop_txed, now_ms):
        entry = (src, seq, hop_txed, now_ms)
        self._tx_ring.append(entry)
        while len(self._tx_ring) > TX_RING_SIZE:
            self._tx_ring.pop(0)

    def _match_tx_ring(self, frame):
        # True if this frame matches a recent TX of ours - i.e. a peer
        # also relayed the same (src, seq) at the same output hop.
        for src, seq, hop_txed, _ts in self._tx_ring:
            if src == frame.source_id and seq == frame.sequence_number \
                    and hop_txed == frame.hop_count:
                return True
        return False

    # ------------------------------------------------------------------
    # Relay TX.
    # ------------------------------------------------------------------

    def _transmit_relay(self, frame, raw_buf, now_ms):
        # When _skip_tx is True, the IRQ handler in app.py already TX'd;
        # we only run bookkeeping. Copy raw_buf first when we do TX so
        # we don't corrupt the caller's buffer (used downstream for
        # diagnostics).
        new_hop = frame.hop_count + 1
        if new_hop > MAX_HOP_COUNT:
            return
        if not self._skip_tx:
            out = bytearray(raw_buf)
            out[HOP_COUNT_BYTE_OFFSET] = new_hop & 0xFF
            try:
                self._send_fn(bytes(out))
            except Exception:
                # A radio failure is not FSM-fatal - the receive loop
                # keeps supplying frames and peer detection is
                # unchanged.
                return
        self._relayed_count += 1
        self._last_relay_ms = now_ms
        self._record_tx(frame.source_id, frame.sequence_number,
                        new_hop, now_ms)
