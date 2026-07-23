"""Epic 17: dynamic repeater FSM for audience-Tildagon mesh coverage.

Runs on each Tildagon in Lume mode when Repeat=AUTO. Observes admitted
frames from the app receive path, elects itself as a relay when a
per-output-hop vacancy is detected, and steps down when a peer takes
over. Cap at hop=3 enables up to three relay hops through the
audience.

The FSM is pure logic: it depends only on a small callback set (send,
random, now-ms clock), so the full state machine is exercised by the
host-side pytest suite without needing the badge radio.

Spec: Docs/epics/epic-17-dynamic-repeater.md

The five states:

  LISTENING   default; sets a 100 ms peer-watch on each admitted frame
              (hop < 3) that arrives at the LOWEST hop we hear it at.
              A frame we can also hear closer to the source arms no
              watch - we're in its coverage core, not at an edge that a
              next-hop relay would serve. Any single "uncovered"
              watch (deadline passes with no matching peer relay at
              hop+1) → CANDIDATE.

  CANDIDATE   election pending; counts down X frames (random 2-9).
              Any peer relay observed during the count → LISTENING.
              Counter elapses → ACTIVE.

  ACTIVE      relays every admitted first-seen frame at hop+1. Records
              (src, seq, hop_txed) in a ring so a peer's frame matching
              on all three signals we are redundant → COOLDOWN.

  COOLDOWN    still relaying, but assessing peer. Counts X frames
              (random 5-20). If peer relays stop (no ring match for
              ~3 director-frames) → ACTIVE. If X elapses with peer
              still active → LISTENING.

  DISABLED    Repeat=OFF from Config; observer is not wired, FSM never
              transitions. Not a runtime state - this is what "no FSM
              instance" looks like from the app's POV.

ESP-NOW broadcast does NOT loop back on this platform (bench-confirmed
2026-07-01 via tools/espnow_loopback_probe.py). So any received frame
matching a TX-ring entry is by construction from a peer device.
"""

# Wire-spec hop_count ceiling (protocol manual section 2.3). Frames at
# this hop are received but not relayed; system-wide limit.
MAX_HOP_COUNT = 3

# LISTENING / CANDIDATE per-frame peer-watch: how long to wait for a
# peer's hop+1 relay to arrive before marking the frame "uncovered".
# Sized for LR ESP-NOW airtime (~1.3 ms) + scheduling jitter, with
# 3-10x headroom. Independent of the 1 Hz heartbeat cadence.
PEER_WATCH_MS = 100

# CANDIDATE frame counter range. Minimum 2 gives the settling period
# that absorbs the ~2% single-frame peer-relay loss - probability of
# TWO consecutive failed peer relays from a healthy peer is 0.04%.
CANDIDATE_MIN_FRAMES = 2
CANDIDATE_MAX_FRAMES = 9

# COOLDOWN frame counter range. Longer than CANDIDATE because we are
# already relaying while assessing - no service interruption during
# cooldown, so we can afford a wider assessment window.
COOLDOWN_MIN_FRAMES = 5
COOLDOWN_MAX_FRAMES = 20

# COOLDOWN "peer stopped" threshold: this many admitted frames without
# a matching peer-ring hit before we conclude the peer has stepped
# down and we can return to ACTIVE.
COOLDOWN_PEER_STOPPED_FRAMES = 3

# TX ring capacity. Bounds peer-detection memory - larger than the
# expected in-flight frame count (retransmits + a few frames of
# scheduling latency) without unbounded growth.
TX_RING_SIZE = 32

# Peer-watch ring capacity. Same rationale as TX ring; sized so we
# never lose a pending watch to eviction under normal traffic.
WATCH_RING_SIZE = 32

# Lowest-hop memory capacity. Tracks the lowest hop each recent
# (src, seq) has been received at, so a device that also hears a frame
# closer to the source never arms an outer-hop watch for it. Sized
# like the other rings - comfortably beyond the in-flight frame count.
RECENT_HOP_MEMORY_SIZE = 64

# ACTIVE / COOLDOWN idle timeout: if we haven't successfully relayed
# a frame for this long, the elected role is obsolete (upstream at
# our input hop has stopped producing traffic) - step down to
# LISTENING and let re-election pick a role fitting the current
# topology. Bench-found 2026-07-01: without this, an ACTIVE(hop 2)
# device stayed stuck when its hop 1 upstream (StickC) was turned
# off, receiving hop=0 from Director but unable to relay it (out of
# role). 3 s ≈ 3 heartbeat cycles; long enough to bridge a normal
# quiet gap, short enough for responsive recovery.
ACTIVE_IDLE_TIMEOUT_MS = 3000

# Byte offset of hop_count in the wire format (magic NN + version +
# source_id + sequence + hop_count). Relay increments this in place.
HOP_COUNT_BYTE_OFFSET = 5

# FSM state constants. Strings rather than an IntEnum so log output
# and debug overlays are self-describing without a lookup table, and
# MicroPython's enum module is anyway unavailable.
STATE_LISTENING = "LISTENING"
STATE_CANDIDATE = "CANDIDATE"
STATE_ACTIVE    = "ACTIVE"
STATE_COOLDOWN  = "COOLDOWN"


def _ticks_diff(a, b):
    """Signed diff a-b. Wraps like MicroPython's time.ticks_diff.

    On MicroPython we could import time.ticks_diff, but the FSM is
    tested on CPython where ticks_diff doesn't exist. Plain int
    subtraction works fine for both because now_ms is monotonic
    within a session.
    """
    return a - b


class DynamicRepeater:
    """Per-Tildagon dynamic repeater FSM.

    Not thread-safe. Called from the app receive loop only.

    Callbacks the caller must supply:

      send_fn(payload_bytes)     TX the given ESP-NOW broadcast payload.
                                 Called by ACTIVE / COOLDOWN when we
                                 elect to relay.

      random_int_fn(lo, hi)      Return a uniform random integer in
                                 [lo, hi]. Per-device entropy comes
                                 from the caller (e.g. seeded with
                                 machine.unique_id() XOR ticks_us()).

    The observer entry point ``on_admitted_frame`` is bound to the
    app's ``_repeater_observer`` slot in Lume mode.
    """

    def __init__(self, send_fn, random_int_fn, now_ms=0,
                 on_relay_state_change=None, skip_tx=False):
        self._send_fn = send_fn
        self._random_int = random_int_fn
        # Notified whenever the FSM enters or leaves an relay-eligible
        # state (ACTIVE, COOLDOWN). Signature: fn(enabled: bool,
        # output_hop: int). Used by the app to keep an IRQ-context fast
        # relay path in step with the FSM's elected role - see
        # _espnow_irq_handler in app.py.
        self._on_relay_state_change = on_relay_state_change
        # When True, _transmit_relay skips the actual send_fn() call and
        # only updates bookkeeping (relayed_count, _record_tx, etc.).
        # The IRQ-context fast path handled the physical TX first;
        # duplicating it here would double airtime per hop for no
        # benefit. When IRQ isn't installed (older firmware) this stays
        # False and _transmit_relay is the sole TX path.
        self._skip_tx = skip_tx

        self._state = STATE_LISTENING
        self._state_entered_ms = now_ms

        # Frame counters for CANDIDATE / COOLDOWN. `_target_frames` is
        # the random-picked total; `_frames_in_state` counts admitted
        # frames since entering the state.
        self._target_frames = 0
        self._frames_in_state = 0

        # Peer-watch ring: entries are dicts {src, seq, hop, deadline_ms}.
        # A list rather than a dict keyed by (src, seq, hop) so we can
        # evict oldest when full without a separate ordering structure.
        self._watches = []

        # Lowest hop each recent (src, seq) has been received at. Gates
        # watch creation: we only arm a peer-watch (the seed of self-
        # election) at the LOWEST hop we personally hear a frame at. A
        # frame we can also hear closer to the source needs no outer
        # relay hop from us - electing for one strands us at a hop
        # with no peer to hand off to and almost no fresh input to relay
        # (bench-found 2026-07-01: a badge beside a StickC hop 1
        # repeater parked in ACTIVE(hop 2), tx crawling, px stuck at 0,
        # never standing down).
        self._recent_lo = {}          # (src, seq) -> lowest hop seen
        self._recent_lo_order = []    # FIFO of keys for bounded eviction

        # TX ring: entries are dicts {src, seq, hop_txed, ts_ms}.
        # Used only in ACTIVE / COOLDOWN for peer detection.
        self._tx_ring = []

        # COOLDOWN "peer stopped" tracker: how many admitted frames
        # since we last observed a peer relay matching our TX ring.
        self._frames_since_peer = 0

        # Elected output hop: the hop level we would (CANDIDATE) or do
        # (ACTIVE/COOLDOWN) TX at. Set in _to_candidate from the
        # triggering watch's hop+1; preserved through the CANDIDATE →
        # ACTIVE → COOLDOWN chain; reset in _to_listening. Load-bearing
        # bench-found 2026-07-01:
        #   1. CANDIDATE cancel triggers only on peer traffic at this
        #      hop (a hop 2 candidate ignores hop 1 traffic).
        #   2. ACTIVE / COOLDOWN relay ONLY frames where hop+1 matches
        #      this hop - a hop 2 elected device produces hop=2 only,
        #      ignoring hop=0 inputs so it doesn't compete with an
        #      engineered hop 1 repeater at hop=1.
        self._output_hop = 0

        # Timestamp of the last successful relay TX. Drives the
        # ACTIVE/COOLDOWN idle timeout so an elected device whose
        # upstream disappears steps down to LISTENING and re-elects.
        self._last_relay_ms = 0

        # Session stats surfaced to the debug overlay (B3).
        self._relayed_count  = 0
        self._peer_seen_count = 0
        self._elections      = 0   # LISTENING → CANDIDATE transitions
        self._activations    = 0   # → ACTIVE transitions

    # ------------------------------------------------------------------
    # Public API — called from the app.
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
        """Short LCD label for the current state - explicit words rather
        than single characters. LISTEN means we're passively observing,
        LISTEN? means we've picked up a vacancy and are about to elect,
        REPEAT means we're actively relaying, CDOWN means we're still
        relaying but assessing peer contention.
        """
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
        is the untouched wire payload so we can relay by mutating byte 5.
        """
        if now_ms is None:
            # Host tests may not supply a clock. Peer-watch expiry needs
            # a monotonic tick; without one, we can only advance on
            # frame arrivals. Fake a monotonic tick per admission.
            now_ms = self._state_entered_ms + self._frames_in_state * 1000

        # Expire past-deadline watches before processing this frame -
        # this is what fires the "uncovered" trigger in LISTENING /
        # CANDIDATE if no peer relay arrived in time.
        self._expire_watches(now_ms)

        # Peer detection based on TX ring (ACTIVE / COOLDOWN). Matches
        # first because a peer's relay of (src, seq) at our TX'd hop
        # is by construction a duplicate from our POV, so this runs
        # against duplicates too.
        peer_hit = self._match_tx_ring(frame)
        if peer_hit:
            self._peer_seen_count += 1
            self._frames_since_peer = 0
        else:
            # Only tick since-peer counter on non-matching frames while
            # in COOLDOWN - LISTENING/CANDIDATE ignore this counter.
            if self._state == STATE_COOLDOWN:
                self._frames_since_peer += 1

        # Peer-covers-my-watch check: does this frame cover any pending
        # peer-watch? A frame at hop=N+1 matching (src, seq) of a
        # watched frame at hop=N covers it.
        covered_watch = self._resolve_watch_cover(frame)

        # Handle state-specific reactions.
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

        # New peer-watch for THIS frame if it's a potential input to a
        # future relay (hop < MAX) AND this is the lowest hop we hear it
        # at. Gating on lowest-hop stops a device that also receives the
        # frame closer to the source from seeding an outer-hop election
        # it can neither hand off nor usefully serve (see
        # _note_hop_should_watch). All states track watches - the data is
        # used by LISTENING/CANDIDATE and cheap to maintain in
        # ACTIVE/COOLDOWN too.
        watch_worthy = self._note_hop_should_watch(frame)
        if watch_worthy and frame.hop_count < MAX_HOP_COUNT:
            self._add_watch(frame, now_ms)

    def tick(self, now_ms):
        """Time-based sweep called from the app's poll loop.

        Watch expiry normally fires on the next admitted frame, but in
        a completely quiet moment (no traffic at all) we still need
        the FSM to eventually trigger. tick() runs the same expiry
        pass so the LISTENING → CANDIDATE trigger fires on the deadline
        rather than on the next frame arrival.

        Also enforces the ACTIVE/COOLDOWN idle timeout - an elected
        device whose upstream disappears stops relaying, and after
        ACTIVE_IDLE_TIMEOUT_MS with no relays we step down to
        LISTENING so re-election can pick a fitting role.
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
        # LISTENING transitions to CANDIDATE only via watch expiry;
        # ``_expire_watches`` already fired that if applicable. Nothing
        # to do on a covered-watch (peer is doing its job).
        pass

    def _on_frame_candidate(self, frame, now_ms, covered_watch, peer_hit):
        # Cancel ONLY on peer traffic at MY target output hop -
        # proving another device is filling the specific vacancy that
        # triggered my candidacy. A hop 2 CANDIDATE (output_hop=2)
        # must ignore hop 1 peer relays (hop=1 frames covering hop=0
        # watches) because those are hop 1's business, not hop 2's.
        # Bench-found 2026-07-01: covering ANY watch previously fired
        # the cancel, causing LISTENING ↔ CANDIDATE oscillation when
        # an engineered hop 1 repeater was active but hop 2 was
        # still vacant.
        if frame.hop_count == self._output_hop:
            self._to_listening(now_ms)
            return
        self._frames_in_state += 1
        if self._frames_in_state >= self._target_frames:
            self._to_active(now_ms)

    def _on_frame_active(self, frame, is_duplicate, now_ms, raw_buf,
                         peer_hit):
        # ACTIVE relays ONLY frames where hop+1 matches our elected
        # output hop. A hop 2 elected device (output_hop=2) relays
        # hop=1 → hop=2 and ignores hop=0 inputs so it doesn't compete
        # with the engineered hop 1 repeater. Duplicates do NOT
        # trigger a fresh relay (we already TXed on first-seen);
        # peer_hit is meaningful on either.
        if not is_duplicate \
                and frame.hop_count + 1 == self._output_hop \
                and self._output_hop <= MAX_HOP_COUNT \
                and raw_buf is not None and self._send_fn is not None:
            self._transmit_relay(frame, raw_buf, now_ms)
        if peer_hit:
            self._to_cooldown(now_ms)

    def _on_frame_cooldown(self, frame, is_duplicate, now_ms, raw_buf,
                           peer_hit):
        # COOLDOWN keeps relaying the same as ACTIVE - same output-hop
        # restriction applies.
        if not is_duplicate \
                and frame.hop_count + 1 == self._output_hop \
                and self._output_hop <= MAX_HOP_COUNT \
                and raw_buf is not None and self._send_fn is not None:
            self._transmit_relay(frame, raw_buf, now_ms)

        # Peer relays that stopped arriving flip us back to ACTIVE.
        # _frames_since_peer was updated in on_admitted_frame before
        # dispatch, so this check uses the current value.
        if self._frames_since_peer >= COOLDOWN_PEER_STOPPED_FRAMES:
            self._to_active(now_ms)
            return

        # Otherwise count the frame against our cooldown window. On
        # expiry with peer still active (frames_since_peer < threshold),
        # step down to LISTENING.
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
        # _output_hop set here and preserved through ACTIVE / COOLDOWN
        # until we step back to LISTENING.
        self._output_hop = output_hop
        self._elections += 1

    def _to_active(self, now_ms):
        # _output_hop inherited from CANDIDATE (or preserved across
        # COOLDOWN → ACTIVE peer-stopped path).
        self._state = STATE_ACTIVE
        self._state_entered_ms = now_ms
        self._target_frames = 0
        self._frames_in_state = 0
        self._frames_since_peer = 0
        # Prime the idle-timeout clock so a freshly-elected ACTIVE
        # doesn't immediately step down before it's had a chance
        # to relay.
        self._last_relay_ms = now_ms
        self._activations += 1
        # Notify the IRQ fast-relay path that we're now expected to TX
        # at this hop. From COOLDOWN → ACTIVE the state was already
        # relay-enabled, but re-notifying is idempotent and cheap.
        if self._on_relay_state_change is not None:
            self._on_relay_state_change(True, self._output_hop)

    def _to_cooldown(self, now_ms):
        # _output_hop inherited from ACTIVE.
        self._state = STATE_COOLDOWN
        self._state_entered_ms = now_ms
        self._target_frames = self._random_int(COOLDOWN_MIN_FRAMES,
                                                COOLDOWN_MAX_FRAMES)
        self._frames_in_state = 0
        self._frames_since_peer = 0
        # Still relay-enabled (COOLDOWN keeps relaying at the same hop
        # until the peer stops or the frame window elapses). Re-notify
        # in case the IRQ globals drifted.
        if self._on_relay_state_change is not None:
            self._on_relay_state_change(True, self._output_hop)

    def _to_listening(self, now_ms):
        self._state = STATE_LISTENING
        self._state_entered_ms = now_ms
        self._target_frames = 0
        self._frames_in_state = 0
        # Reset elected role. Next election picks a fresh output_hop
        # based on which watch expires uncovered.
        self._output_hop = 0
        # Tell the IRQ fast-relay path to stand down. Any inbound
        # frame that hits the IRQ handler now will NOT be relayed.
        if self._on_relay_state_change is not None:
            self._on_relay_state_change(False, 0)

    # ------------------------------------------------------------------
    # Peer-watch mechanics.
    # ------------------------------------------------------------------

    def _note_hop_should_watch(self, frame):
        """Record the lowest hop (src, seq) has been received at and report
        whether this frame warrants a peer-watch.

        A watch at hop H is the seed of hop (H+1) self-election. We only
        want to arm one at the LOWEST hop we personally receive a frame at:
        if we can also hear the same frame closer to the source, an outer
        relay hop from us serves nobody we don't already reach. Electing
        anyway strands us at an outer hop with no peer to hand off to and
        almost no fresh input to relay.

        Returns True on the first sighting of a (src, seq), or when a
        strictly-lower hop arrives out of order (which also cancels any
        higher-hop watch already armed for it). Returns False for an
        equal-or-higher-hop copy of a frame already seen. seq == 0 ("no
        sequencing") can't be tracked per-frame, so it keeps the prior
        always-watch behaviour.
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
            # A lower-hop copy arrived out of order: it supersedes any
            # higher-hop watch we already armed for this frame.
            self._recent_lo[key] = frame.hop_count
            self._cancel_watches_above(frame.source_id, seq, frame.hop_count)
            return True
        # Already heard this frame at an equal or lower hop; a higher-or-
        # equal copy is not our vacancy to fill.
        return False

    def _cancel_watches_above(self, src, seq, hop):
        """Drop pending watches for (src, seq) at a hop strictly greater
        than ``hop``. A lower-hop sighting of the frame has obsoleted them
        - we are closer to the source than any outer hop they'd elect."""
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
        """If ``frame`` is a peer relay at hop+1 of a pending watch,
        drop that watch and return True. Else False.

        A frame's hop is always > 0 when covering a pending watch, so
        hop=0 frames never trigger coverage (they might create a watch,
        but no watch at hop=-1 exists).
        """
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
        """Drop past-deadline watches. Any expiry with an active
        LISTENING state fires the vacancy trigger → CANDIDATE.
        In CANDIDATE, expiries just drop; the frame counter drives the
        target-elapsed transition and cover-resolution drives cancels.

        Multiple watches may expire in one sweep; if so we elect for
        the LOWEST-hop vacancy first (hop 1 upstream of hop 2 -
        filling hop 1 might obviate the hop 2 need). The chosen
        vacancy hop becomes the CANDIDATE's target output hop.
        """
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
            # output_hop = triggering_watch_hop + 1: if I watched a
            # hop=0 frame that went uncovered, my role is hop=1
            # (hop 1). A hop=1 frame's expiry → hop=2 (hop 2).
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
        """True if this frame matches a recent TX of ours - i.e. a peer
        also relayed the same (src, seq) at the same output hop.
        """
        for src, seq, hop_txed, _ts in self._tx_ring:
            if src == frame.source_id and seq == frame.sequence_number \
                    and hop_txed == frame.hop_count:
                return True
        return False

    # ------------------------------------------------------------------
    # Relay TX.
    # ------------------------------------------------------------------

    def _transmit_relay(self, frame, raw_buf, now_ms):
        """Update bookkeeping for a relayed frame and TX it (unless the
        IRQ fast-relay path already did the TX).

        When ``self._skip_tx`` is True, the IRQ-context handler in
        app.py._espnow_irq_handler already mutated hop_count and called
        esp.send() at radio-callback latency (~5 ms) rather than at
        async-poll latency (~85 ms). We still run the bookkeeping so
        peer detection (_record_tx), self-diagnostics (_relayed_count,
        _last_relay_ms), and cooldown timers stay in sync. When False,
        this is the sole TX path - preserves compatibility with older
        MicroPython builds that don't expose espnow.irq().

        Copy raw_buf first when we do TX so we don't corrupt the
        caller's buffer - the same raw_buf is used downstream for
        diagnostics.
        """
        new_hop = frame.hop_count + 1
        if new_hop > MAX_HOP_COUNT:
            return
        if not self._skip_tx:
            out = bytearray(raw_buf)
            out[HOP_COUNT_BYTE_OFFSET] = new_hop & 0xFF
            try:
                self._send_fn(bytes(out))
            except Exception:
                # A radio failure is not FSM-fatal; the receive loop will
                # keep supplying frames and the peer detector unchanged.
                return
        self._relayed_count += 1
        self._last_relay_ms = now_ms
        self._record_tx(frame.source_id, frame.sequence_number,
                        new_hop, now_ms)
