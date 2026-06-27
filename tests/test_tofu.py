"""TOFU lock + cross-range filter tests (Epic 5.5 B6).

Pure-logic tests: synthesise Frame objects directly without going
through parse_frame, since TOFU operates on already-validated frames
post-dedup. The same paths exercised here run unchanged on the badge.
"""

import pytest

from nocturnation.protocol import MessageType
from nocturnation.protocol.frame import Frame
from nocturnation.tofu import TofuLock, DEFAULT_TIMEOUT_MS, format_lock_label


def make_frame(source_id, message_type=MessageType.LIGHT_PULSE,
               sequence=1, hop_count=0):
    """Construct a minimal Frame for TOFU testing.

    TOFU only reads frame.source_id and frame.message_type, so the
    other fields are stubbed out to whatever's convenient. The frame
    is "already validated" - tests bypass parse_frame.
    """
    f = Frame()
    f.source_id       = source_id
    f.sequence_number = sequence
    f.hop_count       = hop_count
    f.message_type    = message_type
    f.payload_len     = 0
    f.payload         = b""
    return f


# =============================================================================
# Initial state
# =============================================================================

class TestInitialState:
    def test_not_locked_on_construction(self):
        t = TofuLock()
        assert t.is_locked() is False
        assert t.locked_id is None

    def test_default_timeout_matches_rescan_constant(self):
        t = TofuLock()
        # The chosen timeout (10 s) is the same as the M5 firmware's
        # channel-rescan kRescanMs, deliberately shared per the B6 design.
        assert DEFAULT_TIMEOUT_MS == 10000


# =============================================================================
# First-lock semantics
# =============================================================================

class TestFirstLock:
    def test_admits_first_frame_and_locks(self):
        t = TofuLock()
        admitted = t.admit(make_frame(source_id=0x05), channel=1, now_ms=100)
        assert admitted is True
        assert t.is_locked() is True
        assert t.locked_id == 0x05

    def test_first_frame_can_be_light_pulse_not_only_heartbeat(self):
        """Per B6 signoff: TOFU locks on any valid frame, not only HEARTBEAT.
        Mid-song joins would otherwise sit idle until the next heartbeat gap.
        """
        t = TofuLock()
        f = make_frame(source_id=0x42, message_type=MessageType.LIGHT_PULSE)
        admitted = t.admit(f, channel=11, now_ms=0)
        assert admitted is True
        assert t.locked_id == 0x42

    def test_broadcast_source_id_never_locks(self):
        """0xFF is the wildcard / anonymous slot; never a valid Director."""
        t = TofuLock()
        admitted = t.admit(make_frame(source_id=0xFF), channel=1, now_ms=0)
        assert admitted is False
        assert t.is_locked() is False


# =============================================================================
# Post-lock filter
# =============================================================================

class TestPostLockFilter:
    def test_admits_subsequent_frames_from_locked_source(self):
        t = TofuLock()
        t.admit(make_frame(source_id=0x05), channel=1, now_ms=100)
        # A second frame from the same Director: admitted.
        admitted = t.admit(make_frame(source_id=0x05, sequence=2),
                           channel=1, now_ms=200)
        assert admitted is True

    def test_drops_frames_from_different_source_after_lock(self):
        t = TofuLock()
        t.admit(make_frame(source_id=0x05), channel=1, now_ms=100)
        # A second Director chimes in: dropped silently.
        admitted = t.admit(make_frame(source_id=0x06),
                           channel=1, now_ms=200)
        assert admitted is False
        assert t.locked_id == 0x05   # original lock preserved


# =============================================================================
# Channel 11 cross-range filter
# =============================================================================

class TestChannel11CrossRangeFilter:
    def test_performance_range_locks_on_ch11(self):
        t = TofuLock()
        # 0x40 (Performance min) is eligible
        admitted = t.admit(make_frame(source_id=0x40), channel=11, now_ms=0)
        assert admitted is True
        assert t.locked_id == 0x40

    def test_community_range_dropped_on_ch11_without_lock(self):
        t = TofuLock()
        # 0x05 (community range) on ch 11: misconfigured Director.
        admitted = t.admit(make_frame(source_id=0x05), channel=11, now_ms=0)
        assert admitted is False
        assert t.is_locked() is False   # MUST NOT lock to a non-eligible source

    def test_community_range_dropped_on_ch11_even_after_perf_lock(self):
        """Cross-range filter applies before the post-lock filter."""
        t = TofuLock()
        t.admit(make_frame(source_id=0x4A), channel=11, now_ms=0)
        # A second Director on the same channel with a community-range
        # id (also misconfigured): dropped.
        admitted = t.admit(make_frame(source_id=0x05),
                           channel=11, now_ms=100)
        assert admitted is False
        assert t.locked_id == 0x4A

    def test_performance_max_0xFE_locks_on_ch11(self):
        t = TofuLock()
        admitted = t.admit(make_frame(source_id=0xFE), channel=11, now_ms=0)
        assert admitted is True
        assert t.locked_id == 0xFE


class TestChannel1Permissive:
    def test_community_range_locks_on_ch1(self):
        t = TofuLock()
        admitted = t.admit(make_frame(source_id=0x05), channel=1, now_ms=0)
        assert admitted is True
        assert t.locked_id == 0x05

    def test_performance_range_also_locks_on_ch1(self):
        """Channel 1 is permissive: any non-broadcast id is eligible.
        (A Director MUST allocate from the community range on ch 1 per
        spec §3.4, but the Lume MUST accept any non-broadcast id.)
        """
        t = TofuLock()
        admitted = t.admit(make_frame(source_id=0x42), channel=1, now_ms=0)
        assert admitted is True
        assert t.locked_id == 0x42


class TestChannel6Permissive:
    def test_any_non_broadcast_id_locks_on_ch6(self):
        """Channel 6 is operator-discretionary; Lume accepts any id."""
        t = TofuLock()
        admitted = t.admit(make_frame(source_id=0x05), channel=6, now_ms=0)
        assert admitted is True
        assert t.locked_id == 0x05


# =============================================================================
# Timeout expiry
# =============================================================================

class TestTimeoutExpiry:
    def test_tick_before_timeout_keeps_lock(self):
        t = TofuLock(timeout_ms=10000)
        t.admit(make_frame(source_id=0x05), channel=1, now_ms=0)

        # 9 s elapsed - still locked.
        expired = t.tick(now_ms=9000)
        assert expired is False
        assert t.is_locked() is True

    def test_tick_past_timeout_expires_lock(self):
        t = TofuLock(timeout_ms=10000)
        t.admit(make_frame(source_id=0x05), channel=1, now_ms=0)

        # 10 s elapsed - lock expires.
        expired = t.tick(now_ms=10000)
        assert expired is True
        assert t.is_locked() is False

        # Subsequent ticks return False (already expired).
        assert t.tick(now_ms=20000) is False

    def test_admit_extends_timeout(self):
        t = TofuLock(timeout_ms=10000)
        t.admit(make_frame(source_id=0x05), channel=1, now_ms=0)
        # Frame at t=5s defers the expiry to t=15s.
        t.admit(make_frame(source_id=0x05), channel=1, now_ms=5000)
        assert t.tick(now_ms=14999) is False
        assert t.is_locked() is True
        assert t.tick(now_ms=15000) is True
        assert t.is_locked() is False

    def test_tick_on_unlocked_state_is_noop(self):
        t = TofuLock()
        assert t.tick(now_ms=99999) is False
        assert t.is_locked() is False


# =============================================================================
# Manual clear
# =============================================================================

class TestClear:
    def test_clear_unlocks(self):
        t = TofuLock()
        t.admit(make_frame(source_id=0x05), channel=1, now_ms=0)
        assert t.is_locked() is True
        t.clear()
        assert t.is_locked() is False
        assert t.locked_id is None

    def test_after_clear_next_frame_locks_fresh(self):
        t = TofuLock()
        t.admit(make_frame(source_id=0x05), channel=1, now_ms=0)
        t.clear()
        # A different Director can now lock.
        t.admit(make_frame(source_id=0x07), channel=1, now_ms=100)
        assert t.locked_id == 0x07

    def test_clear_when_unlocked_is_idempotent(self):
        t = TofuLock()
        t.clear()
        t.clear()
        assert t.is_locked() is False


# =============================================================================
# Reboot semantics (verified implicitly: fresh instance == unlocked)
# =============================================================================

class TestRebootIsImplicitClear:
    def test_fresh_instance_does_not_persist_old_lock(self):
        """The TofuLock holds no NVS state - construction is a clean slate.
        Reboot is therefore an implicit clear() with no extra code needed.
        """
        # First instance locks.
        t1 = TofuLock()
        t1.admit(make_frame(source_id=0x05), channel=1, now_ms=0)
        assert t1.is_locked() is True

        # Second instance (post-reboot) starts unlocked regardless.
        t2 = TofuLock()
        assert t2.is_locked() is False


# =============================================================================
# Lock-label formatter (Epic 5.5 B7)
# =============================================================================

class TestFormatLockLabel:
    def test_scanner_unlocked_shows_scan(self):
        # No channel lock yet: the badge is hunting for a Director.
        assert format_lock_label(11, scanner_locked=False,
                                 tofu_locked_id=None) == "ch 11 scan"
        assert format_lock_label(1, scanner_locked=False,
                                 tofu_locked_id=None) == "ch 1 scan"

    def test_scanner_locked_but_tofu_unlocked_shows_listen(self):
        # Channel locked, but no TOFU peer (initial state or post-rescan
        # / post-timeout). Waiting for the next valid frame.
        assert format_lock_label(11, scanner_locked=True,
                                 tofu_locked_id=None) == "ch 11 listen"

    def test_community_range_shows_C_prefix(self):
        assert format_lock_label(1, scanner_locked=True,
                                 tofu_locked_id=0x03) == "ch 1 C:03"
        # Boundaries:
        assert format_lock_label(1, scanner_locked=True,
                                 tofu_locked_id=0x00) == "ch 1 C:00"
        assert format_lock_label(1, scanner_locked=True,
                                 tofu_locked_id=0x3F) == "ch 1 C:3F"

    def test_performance_range_shows_P_prefix(self):
        assert format_lock_label(11, scanner_locked=True,
                                 tofu_locked_id=0x4F) == "ch 11 P:4F"
        # Boundaries:
        assert format_lock_label(11, scanner_locked=True,
                                 tofu_locked_id=0x40) == "ch 11 P:40"
        assert format_lock_label(11, scanner_locked=True,
                                 tofu_locked_id=0xFE) == "ch 11 P:FE"

    def test_out_of_range_id_shows_question_prefix(self):
        # Defensive: should never happen for a conforming Director,
        # but the label surface stays informative if it does.
        assert format_lock_label(11, scanner_locked=True,
                                 tofu_locked_id=0xFF) == "ch 11 ?:FF"


# =============================================================================
# Display-family source-match admission (EMF multi-show phase 3, 2026-06-24)
# =============================================================================
#
# Pre-phase-3: the orchestrator emitted display frames with source_id =
# 0xFF (broadcast) because it didn't know which Director it was bridged
# through. A carve-out admitted these when a TOFU lock was held.
#
# Post-phase-3: the Director Stick's bridge passthrough re-stamps the
# source_id of bridged display frames to its own id BEFORE broadcasting,
# so display frames arrive identified and go through the same source-
# match gate as wash / pulse / heartbeat. The carve-out is gone.
#
# The multi-show property this gains: show A's Lume locked to Director A
# now rejects show B's display frames (they arrive with B's source_id),
# so show B's lyric overlay no longer leaks onto show A's audience.

class TestDisplayFamilySourceMatch:

    def test_any_broadcast_source_dropped_even_for_display_family(self):
        """Pre-phase-3 admitted these when locked. Post-phase-3 the
        Director re-stamps source_id, so a frame arriving with 0xFF
        means an unstamped / misconfigured upstream - drop it."""
        t = TofuLock()
        t.admit(make_frame(source_id=0x42), channel=1, now_ms=100)
        for mt in (
            MessageType.TEXT_DISPLAY,
            MessageType.CLEAR_SCREEN,
            MessageType.BITMAP_HEADER,
            MessageType.BITMAP_PLANE,
        ):
            f = make_frame(source_id=0xFF, message_type=mt)
            assert t.admit(f, channel=1, now_ms=200) is False, mt

    def test_display_frame_from_locked_director_admitted(self):
        """Director-stamped display frame (source_id == locked
        Director's id) admits through the normal post-lock filter."""
        t = TofuLock()
        t.admit(make_frame(source_id=0x42), channel=1, now_ms=100)
        for mt in (
            MessageType.TEXT_DISPLAY,
            MessageType.BITMAP_HEADER,
            MessageType.BITMAP_PLANE,
            MessageType.CLEAR_SCREEN,
        ):
            f = make_frame(source_id=0x42, message_type=mt)
            assert t.admit(f, channel=1, now_ms=200) is True, mt

    def test_display_frame_from_different_director_dropped(self):
        """Multi-show partitioning property: show A's Lume locked to
        Director A must NOT render show B's lyric overlays."""
        t = TofuLock()
        t.admit(make_frame(source_id=0x42), channel=1, now_ms=100)
        f = make_frame(source_id=0x43, message_type=MessageType.TEXT_DISPLAY)
        assert t.admit(f, channel=1, now_ms=200) is False
        f = make_frame(source_id=0x43, message_type=MessageType.BITMAP_PLANE)
        assert t.admit(f, channel=1, now_ms=201) is False

    def test_display_frame_from_locked_director_resets_liveness(self):
        """Behaviour change at phase 3: display frames from the locked
        Director DO count toward liveness. Arrival of a bridged display
        frame is evidence the Director is up enough to forward."""
        t = TofuLock(timeout_ms=1000)
        t.admit(make_frame(source_id=0x42), channel=1, now_ms=0)
        # Display frame from locked Director at t=500 -> extends timer.
        t.admit(
            make_frame(source_id=0x42, message_type=MessageType.TEXT_DISPLAY),
            channel=1, now_ms=500,
        )
        # tick at t=1100: 1100 ms after original wash, but only 600 ms
        # since the display frame -> still within timeout.
        assert t.tick(now_ms=1100) is False
        assert t.is_locked() is True

    def test_non_display_broadcast_still_rejected_when_locked(self):
        t = TofuLock()
        t.admit(make_frame(source_id=0x42), channel=1, now_ms=100)
        f = make_frame(source_id=0xFF, message_type=MessageType.LIGHT_PULSE)
        assert t.admit(f, channel=1, now_ms=200) is False
