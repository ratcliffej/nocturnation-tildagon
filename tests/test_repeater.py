"""Epic 17 B2: dynamic-repeater FSM host tests.

Exercises state transitions and side-effects without a badge radio:

  - LISTENING → CANDIDATE on any single uncovered peer-watch
  - CANDIDATE → LISTENING on peer coverage / TX-ring match
  - CANDIDATE → ACTIVE on frame counter elapsing
  - ACTIVE relays hop=0/1/2, caps at hop=3
  - ACTIVE ignores duplicates (no double-relay)
  - ACTIVE → COOLDOWN on peer relay matching TX ring
  - COOLDOWN keeps relaying, returns to ACTIVE if peer stops
  - COOLDOWN → LISTENING if peer persists past window
  - TX ring + watch ring bounds
"""

from nocturnation.repeater import (
    DynamicRepeater,
    STATE_LISTENING,
    STATE_CANDIDATE,
    STATE_ACTIVE,
    STATE_COOLDOWN,
    PEER_WATCH_MS,
    CANDIDATE_MIN_FRAMES,
    CANDIDATE_MAX_FRAMES,
    COOLDOWN_MIN_FRAMES,
    COOLDOWN_MAX_FRAMES,
    COOLDOWN_PEER_STOPPED_FRAMES,
    ACTIVE_IDLE_TIMEOUT_MS,
    TX_RING_SIZE,
    WATCH_RING_SIZE,
    HOP_COUNT_BYTE_OFFSET,
    MAX_HOP_COUNT,
)


class FakeFrame:
    """Minimal shape matching what the FSM reads: src/seq/hop only."""
    def __init__(self, src=1, seq=1, hop=0):
        self.source_id = src
        self.sequence_number = seq
        self.hop_count = hop


def make_buf(src=1, seq=1, hop=0):
    """Wire-format bytes matching FakeFrame. v3 header layout:
      bytes 0-1: magic 'NN'
      byte 2:    protocol_version (0x03)
      bytes 3-4: source_id LE u16
      byte 5:    sequence_number
      byte 6:    hop_count   <-- HOP_COUNT_BYTE_OFFSET
      byte 7:    message_type
      byte 8:    payload_len
    Only the hop_count byte is read/mutated by the FSM; padding bytes
    are placeholders. See the round-trip test in TestWireOffsetSanity
    for the drift-catcher that pins these offsets against the actual
    protocol encoder/parser.
    """
    buf = bytearray(16)
    buf[0:2] = b"NN"
    buf[2] = 0x03       # protocol version (v3)
    buf[3] = src & 0xFF
    buf[4] = (src >> 8) & 0xFF
    buf[5] = seq
    buf[6] = hop
    buf[7] = 0x00       # HEARTBEAT
    buf[8] = 0          # payload_len
    return bytes(buf)


class RepeaterHarness:
    """Convenience wrapper around DynamicRepeater with deterministic
    randomness + TX capture. `randoms` is a list consumed left-to-right.
    """
    def __init__(self, randoms=None):
        self.txs = []
        self._randoms = list(randoms or [])
        self._random_calls = 0

        def send_fn(payload):
            self.txs.append(bytes(payload))

        def random_int(lo, hi):
            if self._random_calls < len(self._randoms):
                v = self._randoms[self._random_calls]
                self._random_calls += 1
                # Clamp defensively; tests should be explicit though.
                return max(lo, min(hi, v))
            # Default: return the midpoint. Tests that care should
            # supply explicit values.
            return (lo + hi) // 2

        self.fsm = DynamicRepeater(send_fn, random_int, now_ms=0)

    def admit(self, frame, is_duplicate=False, now_ms=None, raw_buf=None):
        if raw_buf is None:
            raw_buf = make_buf(frame.source_id,
                               frame.sequence_number,
                               frame.hop_count)
        self.fsm.on_admitted_frame(frame, is_duplicate, now_ms, raw_buf)

    def tick(self, now_ms):
        self.fsm.tick(now_ms)


# ---------------------------------------------------------------------
# Initial state.
# ---------------------------------------------------------------------

class TestInitialState:
    def test_starts_in_listening(self):
        h = RepeaterHarness()
        assert h.fsm.state == STATE_LISTENING
        assert h.fsm.state_label() == "LISTEN"

    def test_no_relay_before_active(self):
        h = RepeaterHarness()
        h.admit(FakeFrame(hop=0), now_ms=0)
        assert h.txs == []


# ---------------------------------------------------------------------
# LISTENING → CANDIDATE (vacancy detection).
# ---------------------------------------------------------------------

class TestListeningToCandidate:
    def test_watch_expires_uncovered_triggers_candidate(self):
        h = RepeaterHarness(randoms=[3])
        # Frame at hop=0 → watch created with deadline = now + PEER_WATCH_MS.
        h.admit(FakeFrame(seq=1, hop=0), now_ms=1000)
        assert h.fsm.state == STATE_LISTENING
        # Watch expires when now_ms - deadline >= 0.
        h.tick(now_ms=1000 + PEER_WATCH_MS + 1)
        assert h.fsm.state == STATE_CANDIDATE

    def test_next_admitted_frame_expires_watch(self):
        # Expiry can also fire on the NEXT admitted frame's clock read,
        # not only via tick().
        h = RepeaterHarness(randoms=[3])
        h.admit(FakeFrame(seq=1, hop=0), now_ms=1000)
        h.admit(FakeFrame(seq=2, hop=0), now_ms=1000 + PEER_WATCH_MS + 1)
        assert h.fsm.state == STATE_CANDIDATE

    def test_peer_covers_watch_stays_listening(self):
        h = RepeaterHarness()
        h.admit(FakeFrame(seq=1, hop=0), now_ms=1000)
        # Peer relay at hop=1 arrives inside the 100 ms window and
        # covers the hop 1 watch. State stays LISTENING. The peer's
        # hop=1 frame does NOT spawn a hop 2 watch here: we already
        # heard this (src, seq) direct at hop=0, so a next-hop relay
        # from us would serve no one - see
        # test_core_device_does_not_cascade_to_hop_2.
        h.admit(FakeFrame(seq=1, hop=1), now_ms=1010)
        assert h.fsm.state == STATE_LISTENING

    def test_core_device_does_not_cascade_to_hop_2(self):
        # A device that hears a frame BOTH direct (hop=0) and relayed
        # (hop=1) is in the coverage core, not the edge. It must not
        # elect itself hop 2: nothing downstream needs the frame pushed
        # a further hop, and no hop 2 peer exists to hand off to, so it
        # would park in REPEAT forever with almost nothing to relay.
        # (Bench-found 2026-07-01: Tildagon beside a StickC hop 1
        # repeater, both in earshot of the Director, stuck ACTIVE(hop 2)
        # with tx crawling and px=0.)
        h = RepeaterHarness(randoms=[3])
        h.admit(FakeFrame(seq=1, hop=0), now_ms=1000)
        h.admit(FakeFrame(seq=1, hop=1), now_ms=1010)
        h.tick(now_ms=1200)
        assert h.fsm.state == STATE_LISTENING
        # Sustained: several more direct+relayed pairs never elect.
        h.admit(FakeFrame(seq=2, hop=0), now_ms=2000)
        h.admit(FakeFrame(seq=2, hop=1), now_ms=2010)
        h.admit(FakeFrame(seq=3, hop=0), now_ms=3000)
        h.admit(FakeFrame(seq=3, hop=1), now_ms=3010)
        h.tick(now_ms=3200)
        assert h.fsm.state == STATE_LISTENING
        assert h.txs == []

    def test_edge_device_elects_hop_2_from_hop_1_only(self):
        # The legitimate cascade: a device that hears a frame ONLY at
        # hop=1 (it has walked out of the Director's direct range) is at
        # the edge and SHOULD extend coverage as hop 2 when no hop=2
        # relay covers it.
        h = RepeaterHarness(randoms=[3])
        h.admit(FakeFrame(seq=1, hop=1), now_ms=1010)
        h.tick(now_ms=1200)
        assert h.fsm.state == STATE_CANDIDATE
        assert h.fsm._output_hop == 2

    def test_out_of_order_direct_copy_cancels_hop_2_watch(self):
        # Relayed hop=1 can arrive before the direct hop=0 (the StickC
        # beats the through-wall direct path). The hop=1 arms a hop 2
        # watch, but when the direct hop=0 copy lands inside the window
        # it supersedes it. Any election that follows is at most hop 1
        # (output_hop=1) - the resolvable role that stands down once a
        # peer relay is detected - never the phantom, unresolvable
        # hop 2.
        h = RepeaterHarness(randoms=[3])
        h.admit(FakeFrame(seq=1, hop=1), now_ms=1000)   # relayed copy first
        h.admit(FakeFrame(seq=1, hop=0), now_ms=1050)   # direct copy, in-window
        h.tick(now_ms=1200)
        assert h.fsm._output_hop != 2
        # Here the hop=0 watch goes uncovered, so it elects hop 1.
        assert h.fsm.state == STATE_CANDIDATE
        assert h.fsm._output_hop == 1

    def test_hop_at_max_creates_no_watch(self):
        # A frame at hop=MAX_HOP_COUNT cannot be relayed (cap), so
        # there's no vacancy to detect - no watch created.
        h = RepeaterHarness()
        h.admit(FakeFrame(seq=1, hop=MAX_HOP_COUNT), now_ms=1000)
        h.tick(now_ms=2000)  # well past PEER_WATCH_MS
        assert h.fsm.state == STATE_LISTENING


# ---------------------------------------------------------------------
# CANDIDATE lifecycle.
# ---------------------------------------------------------------------

class TestCandidateToActive:
    def test_reaches_active_after_target_frames(self):
        # Random picks X=2 for CANDIDATE target.
        h = RepeaterHarness(randoms=[2])
        h.admit(FakeFrame(seq=1, hop=0), now_ms=0)
        h.tick(now_ms=PEER_WATCH_MS + 1)   # → CANDIDATE, target=2
        assert h.fsm.state == STATE_CANDIDATE
        # Two subsequent admitted frames without peer coverage push
        # us over the counter.
        h.admit(FakeFrame(seq=2, hop=0), now_ms=1000)
        h.admit(FakeFrame(seq=3, hop=0), now_ms=2000)
        assert h.fsm.state == STATE_ACTIVE

    def test_peer_coverage_cancels_candidate(self):
        h = RepeaterHarness(randoms=[9])   # long target so we can cancel
        h.admit(FakeFrame(seq=1, hop=0), now_ms=0)
        h.tick(now_ms=PEER_WATCH_MS + 1)
        assert h.fsm.state == STATE_CANDIDATE
        # A frame at hop=0 followed inside its own window by a peer
        # relay at hop=1 → covers the new watch → cancels candidacy.
        h.admit(FakeFrame(seq=2, hop=0), now_ms=1000)
        h.admit(FakeFrame(seq=2, hop=1), now_ms=1010)
        assert h.fsm.state == STATE_LISTENING


class TestCandidateRange:
    def test_target_frames_within_range(self):
        h = RepeaterHarness()   # default random = midpoint
        h.admit(FakeFrame(seq=1, hop=0), now_ms=0)
        h.tick(now_ms=PEER_WATCH_MS + 1)
        # Midpoint of [2, 9] = 5.
        assert h.fsm._target_frames == 5
        assert CANDIDATE_MIN_FRAMES <= h.fsm._target_frames <= CANDIDATE_MAX_FRAMES


class TestBenchScenario20260701:
    """Regression: Tildagon + Director + engineered StickC hop 1 repeater.

    Bench observation 2026-07-01: FSM oscillated LISTENING ↔ CANDIDATE
    without ever reaching ACTIVE. Root cause was CANDIDATE cancelling
    on ANY covered watch, including StickC's hop=1 covering hop=0
    watches while the FSM was actually electing for hop 2 (output_hop=2).
    Fix tracks candidate_output_hop on entry and only cancels on peer
    traffic at that specific hop.
    """

    def test_hop_2_candidate_not_cancelled_by_hop_1_peer(self):
        # Setup: enter CANDIDATE(output_hop=2) legitimately - an edge
        # device that hears seq=1 ONLY at hop=1 (never direct), with no
        # hop=2 relay covering it.
        h = RepeaterHarness(randoms=[3])
        h.admit(FakeFrame(seq=1, hop=1), now_ms=1010)
        h.tick(now_ms=1200)
        assert h.fsm.state == STATE_CANDIDATE
        assert h.fsm._output_hop == 2

        # Now a fresh Director hop=0 + StickC hop=1 arrives. The hop=1
        # relay covers a new hop=0 watch → covered_watch=True. Under
        # the old (buggy) FSM this cancelled candidacy back to LISTENING.
        # Under the fix, hop=1 != output_hop=2 → NO cancel, counter
        # advances toward election.
        h.admit(FakeFrame(seq=2, hop=0), now_ms=2000)
        h.admit(FakeFrame(seq=2, hop=1), now_ms=2010)
        assert h.fsm.state == STATE_CANDIDATE

        # Third sequence progresses the counter; target=3 reached
        # → ACTIVE for hop 2 role.
        h.admit(FakeFrame(seq=3, hop=0), now_ms=3000)
        h.admit(FakeFrame(seq=3, hop=1), now_ms=3010)
        assert h.fsm.state == STATE_ACTIVE

    def test_hop_2_candidate_cancelled_by_hop_2_peer(self):
        # Same edge setup, but this time a peer starts relaying at hop=2
        # (matching our target output hop). CANDIDATE should cancel.
        h = RepeaterHarness(randoms=[9])
        h.admit(FakeFrame(seq=1, hop=1), now_ms=1010)
        h.tick(now_ms=1200)
        assert h.fsm.state == STATE_CANDIDATE
        assert h.fsm._output_hop == 2

        # A peer's hop=2 relay proves someone else is filling the
        # hop 2 role. Cancel back to LISTENING.
        h.admit(FakeFrame(seq=2, hop=2), now_ms=2000)
        assert h.fsm.state == STATE_LISTENING

    def test_hop_1_candidate_cancelled_by_hop_1_peer(self):
        # Shell-1 candidacy (output_hop=1) is still correctly cancelled
        # by hop=1 peer traffic. Regression against over-correcting
        # the fix and breaking the hop 1 case too.
        h = RepeaterHarness(randoms=[9])
        h.admit(FakeFrame(seq=1, hop=0), now_ms=1000)
        h.tick(now_ms=1200)
        assert h.fsm.state == STATE_CANDIDATE
        assert h.fsm._output_hop == 1

        # Peer relays hop=1 (hop 1 coverage) → cancel back to
        # LISTENING because our target output IS hop=1.
        h.admit(FakeFrame(seq=2, hop=0), now_ms=2000)
        h.admit(FakeFrame(seq=2, hop=1), now_ms=2010)
        assert h.fsm.state == STATE_LISTENING


# ---------------------------------------------------------------------
# ACTIVE — relay behaviour.
# ---------------------------------------------------------------------

class TestActiveRelay:
    def _make_active(self, output_hop=1, randoms=None):
        """Set up FSM in ACTIVE for the given elected output hop (1/2/3).
        ACTIVE relays are role-specific (post-2026-07-01 bench fix): a
        hop-N-elected device only relays input frames at hop=(N-1) so it
        doesn't compete with peers at other hops. Each role needs its
        own election, driven by a watch at hop=(N-1) expiring uncovered.
        """
        h = RepeaterHarness(randoms=(randoms or [2]))
        input_hop = output_hop - 1
        h.admit(FakeFrame(seq=1, hop=input_hop), now_ms=0)
        h.tick(now_ms=PEER_WATCH_MS + 1)   # → CANDIDATE at output_hop
        h.admit(FakeFrame(seq=2, hop=input_hop), now_ms=1000)
        h.admit(FakeFrame(seq=3, hop=input_hop), now_ms=2000)
        assert h.fsm.state == STATE_ACTIVE
        assert h.fsm._output_hop == output_hop
        return h

    def test_relays_hop_zero_as_hop_one(self):
        # Shell-1 elected: hop=0 → hop=1.
        h = self._make_active(output_hop=1)
        pre = len(h.txs)
        h.admit(FakeFrame(src=1, seq=100, hop=0), now_ms=3000,
                raw_buf=make_buf(src=1, seq=100, hop=0))
        assert len(h.txs) == pre + 1
        assert h.txs[-1][HOP_COUNT_BYTE_OFFSET] == 1
        # Same src+seq preserved on the wire (v3 header: source LE u16
        # at bytes 3-4, sequence_number at byte 5).
        assert h.txs[-1][3] == 1     # source_id LSB
        assert h.txs[-1][4] == 0     # source_id MSB
        assert h.txs[-1][5] == 100   # sequence_number

    def test_relays_hop_one_as_hop_two(self):
        # Shell-2 elected: hop=1 → hop=2.
        h = self._make_active(output_hop=2)
        pre = len(h.txs)
        h.admit(FakeFrame(seq=101, hop=1), now_ms=3000,
                raw_buf=make_buf(seq=101, hop=1))
        assert len(h.txs) == pre + 1
        assert h.txs[-1][HOP_COUNT_BYTE_OFFSET] == 2

    def test_relays_hop_two_as_hop_three(self):
        # Shell-3 elected: hop=2 → hop=3.
        h = self._make_active(output_hop=3)
        pre = len(h.txs)
        h.admit(FakeFrame(seq=102, hop=2), now_ms=3000,
                raw_buf=make_buf(seq=102, hop=2))
        assert len(h.txs) == pre + 1
        assert h.txs[-1][HOP_COUNT_BYTE_OFFSET] == 3

    def test_hop_1_role_ignores_hop_1_input(self):
        # Regression for bench-found 2026-07-01: a hop 1 elected
        # device MUST ignore hop=1 inputs (would produce hop=2, but
        # a peer at hop=2 is hop 2's business, not ours). Before
        # the fix, ACTIVE relayed hop=1 → hop=2 too, which caused
        # hop 1 contention loops.
        h = self._make_active(output_hop=1)
        pre = len(h.txs)
        h.admit(FakeFrame(seq=104, hop=1), now_ms=3000,
                raw_buf=make_buf(seq=104, hop=1))
        assert len(h.txs) == pre   # no relay - out of role

    def test_hop_2_role_ignores_hop_0_input(self):
        # Regression for bench-found 2026-07-01: a hop 2 elected
        # device MUST ignore hop=0 inputs (would produce hop=1, but
        # that's hop 1's business - and competing with an engineered
        # hop 1 repeater is exactly the loop we're trying to avoid).
        h = self._make_active(output_hop=2)
        pre = len(h.txs)
        h.admit(FakeFrame(seq=105, hop=0), now_ms=3000,
                raw_buf=make_buf(seq=105, hop=0))
        assert len(h.txs) == pre   # no relay - out of role

    def test_does_not_relay_hop_three(self):
        # hop=3 → hop=4 exceeds MAX_HOP_COUNT regardless of elected
        # role. In hop 3 role (output_hop=3), the condition
        # frame.hop_count + 1 == output_hop is 4 == 3 → False, so no
        # relay.
        h = self._make_active(output_hop=3)
        pre = len(h.txs)
        h.admit(FakeFrame(seq=103, hop=MAX_HOP_COUNT), now_ms=3000,
                raw_buf=make_buf(seq=103, hop=MAX_HOP_COUNT))
        assert len(h.txs) == pre  # no relay

    def test_duplicates_do_not_relay(self):
        # First-seen relays; a following duplicate must not.
        h = self._make_active(output_hop=1)
        h.admit(FakeFrame(seq=200, hop=0), now_ms=3000,
                raw_buf=make_buf(seq=200, hop=0))
        pre = len(h.txs)
        h.admit(FakeFrame(seq=200, hop=0), is_duplicate=True, now_ms=3100,
                raw_buf=make_buf(seq=200, hop=0))
        assert len(h.txs) == pre


# ---------------------------------------------------------------------
# ACTIVE → COOLDOWN on peer detection.
# ---------------------------------------------------------------------

class TestActiveToCooldown:
    def _make_active(self):
        h = RepeaterHarness(randoms=[2, 5])   # candidate target, cooldown target
        h.admit(FakeFrame(seq=1, hop=0), now_ms=0)
        h.tick(now_ms=PEER_WATCH_MS + 1)
        h.admit(FakeFrame(seq=2, hop=0), now_ms=1000)
        h.admit(FakeFrame(seq=3, hop=0), now_ms=2000)
        return h

    def test_peer_match_flips_to_cooldown(self):
        h = self._make_active()
        # I relay seq=100 hop=0 → TX hop=1. TX ring now has
        # (src=1, seq=100, hop_txed=1). A peer's hop=1 of the same
        # (src, seq) is now a match.
        h.admit(FakeFrame(seq=100, hop=0), now_ms=3000,
                raw_buf=make_buf(seq=100, hop=0))
        assert h.fsm.state == STATE_ACTIVE
        # Peer's hop=1 relay - is_duplicate=True because dedup already
        # saw seq=100.
        h.admit(FakeFrame(seq=100, hop=1), is_duplicate=True, now_ms=3050,
                raw_buf=make_buf(seq=100, hop=1))
        assert h.fsm.state == STATE_COOLDOWN

    def test_downstream_relay_does_not_flip(self):
        # A hop 2 device's hop=2 relay is downstream, NOT a peer.
        # Shell-1 (us) TXes hop=1; hop=2 is not a peer collision.
        h = self._make_active()
        h.admit(FakeFrame(seq=100, hop=0), now_ms=3000,
                raw_buf=make_buf(seq=100, hop=0))
        h.admit(FakeFrame(seq=100, hop=2), is_duplicate=True, now_ms=3050,
                raw_buf=make_buf(seq=100, hop=2))
        assert h.fsm.state == STATE_ACTIVE


# ---------------------------------------------------------------------
# COOLDOWN behaviour.
# ---------------------------------------------------------------------

class TestCooldown:
    def _to_cooldown(self, cooldown_target=5):
        h = RepeaterHarness(randoms=[2, cooldown_target])
        # LISTENING → CANDIDATE → ACTIVE
        h.admit(FakeFrame(seq=1, hop=0), now_ms=0)
        h.tick(now_ms=PEER_WATCH_MS + 1)
        h.admit(FakeFrame(seq=2, hop=0), now_ms=1000)
        h.admit(FakeFrame(seq=3, hop=0), now_ms=2000)
        assert h.fsm.state == STATE_ACTIVE
        # ACTIVE → COOLDOWN via peer match.
        h.admit(FakeFrame(seq=100, hop=0), now_ms=3000,
                raw_buf=make_buf(seq=100, hop=0))
        h.admit(FakeFrame(seq=100, hop=1), is_duplicate=True, now_ms=3050,
                raw_buf=make_buf(seq=100, hop=1))
        assert h.fsm.state == STATE_COOLDOWN
        return h

    def test_still_relays_in_cooldown(self):
        h = self._to_cooldown()
        pre = len(h.txs)
        h.admit(FakeFrame(seq=200, hop=0), now_ms=4000,
                raw_buf=make_buf(seq=200, hop=0))
        # Relay fired even though we're in COOLDOWN.
        assert len(h.txs) == pre + 1
        assert h.txs[-1][HOP_COUNT_BYTE_OFFSET] == 1

    def test_returns_to_active_when_peer_stops(self):
        # 3 consecutive frames without a peer-ring hit → back to ACTIVE.
        h = self._to_cooldown(cooldown_target=20)
        for i in range(COOLDOWN_PEER_STOPPED_FRAMES):
            seq = 10 + i
            h.admit(FakeFrame(seq=seq, hop=0), now_ms=5000 + i * 100,
                    raw_buf=make_buf(seq=seq, hop=0))
        assert h.fsm.state == STATE_ACTIVE

    def test_falls_to_listening_when_peer_persists(self):
        # cooldown_target=5 (COOLDOWN_MIN_FRAMES). Every frame arrives
        # with a matching peer relay - _frames_since_peer stays at 0,
        # cooldown counter accumulates - and we time out into LISTENING
        # (not into ACTIVE via the peer-stopped path).
        # Post-2026-07-01 bench fix: hop 2 cascade watches can then
        # drive LISTENING → CANDIDATE(output_hop=2) as correct-by-design
        # cascade behaviour. So we track ALL states seen during the
        # loop and just verify LISTENING appears (COOLDOWN → LISTENING
        # transition happened) and ACTIVE does NOT appear (ruling out
        # the peer-stopped path).
        h = self._to_cooldown(cooldown_target=5)
        states_seen = []
        for i in range(3):
            seq = 20 + i
            h.admit(FakeFrame(seq=seq, hop=0), now_ms=5000 + i * 100,
                    raw_buf=make_buf(seq=seq, hop=0))
            states_seen.append(h.fsm.state)
            h.admit(FakeFrame(seq=seq, hop=1), is_duplicate=True,
                    now_ms=5000 + i * 100 + 50,
                    raw_buf=make_buf(seq=seq, hop=1))
            states_seen.append(h.fsm.state)
        assert STATE_LISTENING in states_seen, \
            "expected COOLDOWN → LISTENING via counter timeout, got %r" % states_seen
        assert STATE_ACTIVE not in states_seen, \
            "unexpected ACTIVE - peer-stopped path fired instead of counter"


# ---------------------------------------------------------------------
# Ring buffer bounds.
# ---------------------------------------------------------------------

class TestRingBounds:
    def test_watch_ring_bounded(self):
        h = RepeaterHarness()
        # Fill past capacity within one peer-watch window so no watch
        # expires while we're still adding. Time increments of 1 ms
        # keep the whole batch inside the 100 ms window.
        for i in range(WATCH_RING_SIZE + 5):
            h.admit(FakeFrame(seq=i + 1, hop=0), now_ms=i)
        assert len(h.fsm._watches) == WATCH_RING_SIZE

    def test_tx_ring_bounded(self):
        h = RepeaterHarness(randoms=[2])
        h.admit(FakeFrame(seq=1, hop=0), now_ms=0)
        h.tick(now_ms=PEER_WATCH_MS + 1)
        h.admit(FakeFrame(seq=2, hop=0), now_ms=1000)
        h.admit(FakeFrame(seq=3, hop=0), now_ms=2000)
        assert h.fsm.state == STATE_ACTIVE
        # Seq is 1 byte on the wire; keep values <= 255.
        for i in range(TX_RING_SIZE + 5):
            seq = 100 + i   # 100..136 stays within byte range
            h.admit(FakeFrame(seq=seq, hop=0), now_ms=3000 + i * 10,
                    raw_buf=make_buf(seq=seq, hop=0))
        assert len(h.fsm._tx_ring) == TX_RING_SIZE


# ---------------------------------------------------------------------
# State indicator (LCD char).
# ---------------------------------------------------------------------

class TestIdleTimeout:
    """Regression: ACTIVE / COOLDOWN devices whose upstream disappears
    should step down to LISTENING so re-election can pick a role
    fitting the new topology. Bench-found 2026-07-01: a Tildagon in
    ACTIVE(hop 2) got stuck when its hop 1 upstream (StickC) was
    turned off - no more hop=1 inputs → no more relays → but state
    never transitioned back to LISTENING.
    """

    def _make_active_hop_1(self):
        # Election via hop=0 watch expiry, then progression to ACTIVE.
        h = RepeaterHarness(randoms=[2])
        h.admit(FakeFrame(seq=1, hop=0), now_ms=0)
        h.tick(now_ms=PEER_WATCH_MS + 1)
        h.admit(FakeFrame(seq=2, hop=0), now_ms=1000)
        h.admit(FakeFrame(seq=3, hop=0), now_ms=2000)
        assert h.fsm.state == STATE_ACTIVE
        return h

    def test_active_steps_down_after_idle(self):
        h = self._make_active_hop_1()
        # Relay once to prime _last_relay_ms.
        h.admit(FakeFrame(seq=10, hop=0), now_ms=3000,
                raw_buf=make_buf(seq=10, hop=0))
        assert h.fsm.state == STATE_ACTIVE
        # Tick past the idle timeout with no further relays.
        h.tick(now_ms=3000 + ACTIVE_IDLE_TIMEOUT_MS + 1)
        assert h.fsm.state == STATE_LISTENING
        # Elected role is reset so next election can pick a fresh hop.
        assert h.fsm._output_hop == 0

    def test_active_relay_defers_idle_timeout(self):
        h = self._make_active_hop_1()
        # First relay at t=3000.
        h.admit(FakeFrame(seq=10, hop=0), now_ms=3000,
                raw_buf=make_buf(seq=10, hop=0))
        # Second relay just before the timeout would fire.
        h.admit(FakeFrame(seq=11, hop=0),
                now_ms=3000 + ACTIVE_IDLE_TIMEOUT_MS - 500,
                raw_buf=make_buf(seq=11, hop=0))
        # Tick at what would have been the original timeout - still
        # ACTIVE because the second relay refreshed the clock.
        h.tick(now_ms=3000 + ACTIVE_IDLE_TIMEOUT_MS + 100)
        assert h.fsm.state == STATE_ACTIVE

    def test_freshly_elected_active_does_not_immediately_step_down(self):
        # _to_active primes _last_relay_ms so a brand-new ACTIVE
        # doesn't step down on its first tick.
        h = self._make_active_hop_1()
        # Same-ms tick shouldn't trigger.
        h.tick(now_ms=2001)
        assert h.fsm.state == STATE_ACTIVE


class TestPunterWalksScenario:
    """Bench-driven scenario 2026-07-01: a punter carrying a Tildagon
    acting as hop 1 repeater walks deeper into the audience. They
    lose Director's hop=0 signal but still hear hop=1 from other
    repeaters. The FSM should:

      1. Idle-timeout out of ACTIVE(hop 1) after 3 s of no relays.
      2. Transition to LISTENING (fresh _output_hop=0).
      3. Notice the hop=1 traffic without hop=2 coverage → elect
         hop 2 in the new position.
    """

    def test_walk_re_elects_from_hop_1_to_hop_2(self):
        # Setup ACTIVE(hop 1): hop=0 watch expires, CANDIDATE, then
        # ACTIVE via 2 more admits.
        h = RepeaterHarness(randoms=[2, 3])  # CANDIDATE=2, later CANDIDATE=3
        h.admit(FakeFrame(seq=1, hop=0), now_ms=0)
        h.tick(now_ms=PEER_WATCH_MS + 1)
        h.admit(FakeFrame(seq=2, hop=0), now_ms=1000)
        h.admit(FakeFrame(seq=3, hop=0), now_ms=2000)
        assert h.fsm.state == STATE_ACTIVE
        assert h.fsm._output_hop == 1

        # A relay happens in the front-of-crowd position.
        h.admit(FakeFrame(seq=10, hop=0), now_ms=3000,
                raw_buf=make_buf(seq=10, hop=0))
        assert len(h.txs) == 1   # relay TXed

        # Punter walks. No more hop=0 arrives. Only hop=1 (from other
        # hop 1 repeaters covering the new position).
        pre_tx = len(h.txs)
        h.admit(FakeFrame(seq=20, hop=1), now_ms=4000,
                raw_buf=make_buf(seq=20, hop=1))
        # No relay - hop=1 input doesn't match hop 1 role.
        assert len(h.txs) == pre_tx

        # Idle timeout fires 3 s after last relay.
        h.tick(now_ms=3000 + ACTIVE_IDLE_TIMEOUT_MS + 1)
        assert h.fsm.state == STATE_LISTENING
        assert h.fsm._output_hop == 0

        # Next hop=1 frame in the new position creates a hop=1 watch.
        h.admit(FakeFrame(seq=30, hop=1), now_ms=6200,
                raw_buf=make_buf(seq=30, hop=1))
        # No peer at hop=2 covers → watch expires → CANDIDATE(hop 2).
        h.tick(now_ms=6200 + PEER_WATCH_MS + 1)
        assert h.fsm.state == STATE_CANDIDATE
        assert h.fsm._output_hop == 2

        # Progress CANDIDATE with 3 hop=1 admits (target=3).
        h.admit(FakeFrame(seq=31, hop=1), now_ms=7000,
                raw_buf=make_buf(seq=31, hop=1))
        h.admit(FakeFrame(seq=32, hop=1), now_ms=8000,
                raw_buf=make_buf(seq=32, hop=1))
        h.admit(FakeFrame(seq=33, hop=1), now_ms=9000,
                raw_buf=make_buf(seq=33, hop=1))
        assert h.fsm.state == STATE_ACTIVE
        assert h.fsm._output_hop == 2

        # New role now relays hop=1 → hop=2 correctly.
        pre_tx = len(h.txs)
        h.admit(FakeFrame(seq=40, hop=1), now_ms=10000,
                raw_buf=make_buf(seq=40, hop=1))
        assert len(h.txs) == pre_tx + 1
        assert h.txs[-1][HOP_COUNT_BYTE_OFFSET] == 2


class TestStateLabel:
    def test_label_per_state(self):
        h = RepeaterHarness(randoms=[2, 5])
        assert h.fsm.state_label() == "LISTEN"
        h.admit(FakeFrame(seq=1, hop=0), now_ms=0)
        h.tick(now_ms=PEER_WATCH_MS + 1)
        assert h.fsm.state_label() == "LISTEN?"
        h.admit(FakeFrame(seq=2, hop=0), now_ms=1000)
        h.admit(FakeFrame(seq=3, hop=0), now_ms=2000)
        assert h.fsm.state_label() == "REPEAT"
        h.admit(FakeFrame(seq=100, hop=0), now_ms=3000,
                raw_buf=make_buf(seq=100, hop=0))
        h.admit(FakeFrame(seq=100, hop=1), is_duplicate=True, now_ms=3050,
                raw_buf=make_buf(seq=100, hop=1))
        assert h.fsm.state_label() == "CDOWN"


class TestWireOffsetSanity:
    """Drift-catcher for the hop_count byte offset.

    The v2 -> v3 header change (source_id widened u8 -> u16) shifted
    hop_count from byte 5 to byte 6. The repeater's HOP_COUNT_BYTE_OFFSET
    and app.py's IRQ-hot-path duplicate constant weren't updated in
    lockstep; the peer-facing symptom was "both Tildagons stuck REPEAT
    with Hop:0 () everywhere" because no relay TX ever actually left
    the radio (the gate read seq+1 instead of hop+1).

    These tests would have caught that: they use the ACTUAL protocol
    encoder + parser to round-trip a hop_count mutation, so if
    HOP_COUNT_BYTE_OFFSET points at the wrong byte, the parsed
    hop_count won't match what was written.
    """

    def test_repeater_offset_matches_protocol_offset(self):
        # Belt-and-braces: the constants are supposed to be the same
        # value from the same authority (repeater imports from
        # protocol.constants), but a future refactor could re-hard-code
        # one and drift again. Assert they're equal here.
        from nocturnation.protocol.constants import HOP_COUNT_OFFSET
        assert HOP_COUNT_BYTE_OFFSET == HOP_COUNT_OFFSET

    def test_relay_mutation_round_trips_through_parser(self):
        # Encode a real LIGHT_PULSE frame with the protocol encoder,
        # then mutate the hop-count byte the way the FSM's
        # _transmit_relay does, then parse it back with the protocol
        # parser. The parsed hop_count MUST equal what we wrote. If
        # HOP_COUNT_BYTE_OFFSET points at the wrong byte, the parser
        # will read a completely different field (in v3 offset 5 is
        # sequence_number, offset 6 is hop_count).
        from nocturnation.protocol import (
            encode_light_pulse, parse_frame, MessageType,
        )
        raw = encode_light_pulse(
            source_id=0x42, sequence_number=17,
            target_class=0, target_group=0,
            r=1, g=2, b=3,
            attack=0, sustain=0, release=0, chance=0,
            hop_count=0,
        )
        # Sanity: fresh frame has hop=0 per the parser.
        f0 = parse_frame(raw)
        assert f0.hop_count == 0
        assert f0.sequence_number == 17
        assert f0.source_id == 0x42
        assert f0.message_type == MessageType.LIGHT_PULSE

        # Now mutate the hop-count byte the way the repeater does.
        mutated = bytearray(raw)
        mutated[HOP_COUNT_BYTE_OFFSET] = 2
        f1 = parse_frame(bytes(mutated))
        # The parser must see the mutated value on the hop_count
        # field, NOT on sequence_number or any other adjacent field.
        assert f1.hop_count == 2, \
            "HOP_COUNT_BYTE_OFFSET=%d points at wrong byte for v3 wire" \
            % HOP_COUNT_BYTE_OFFSET
        # And every other header field is untouched.
        assert f1.sequence_number == 17
        assert f1.source_id == 0x42
        assert f1.message_type == MessageType.LIGHT_PULSE
