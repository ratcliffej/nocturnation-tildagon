"""Native tests for the 16-slot per-sender numonce LRU."""

from nocturnation.protocol.numonce_lru import NumonceLru, _SLOTS


def test_fresh_sender_admitted():
    lru = NumonceLru()
    assert lru.check_and_update(0x1234, 1) is True
    assert lru.highest(0x1234) == 1


def test_strictly_increasing_admitted():
    lru = NumonceLru()
    assert lru.check_and_update(0x1234, 1) is True
    assert lru.check_and_update(0x1234, 2) is True
    assert lru.check_and_update(0x1234, 100) is True
    assert lru.highest(0x1234) == 100


def test_replay_rejected():
    lru = NumonceLru()
    assert lru.check_and_update(0x1234, 5) is True
    assert lru.check_and_update(0x1234, 5) is False   # equal
    assert lru.check_and_update(0x1234, 4) is False   # lower
    assert lru.highest(0x1234) == 5


def test_zero_numonce_rejected_as_sentinel():
    lru = NumonceLru()
    assert lru.check_and_update(0x1234, 0) is False
    # Even on a sender we've never seen, 0 is reserved - no slot
    # written.
    assert lru.highest(0x1234) == 0


def test_multiple_senders_tracked_independently():
    lru = NumonceLru()
    assert lru.check_and_update(0x1234, 10) is True
    assert lru.check_and_update(0x5678, 1) is True
    assert lru.highest(0x1234) == 10
    assert lru.highest(0x5678) == 1
    # Senders don't cross-pollute: a low numonce from one doesn't
    # affect the other's gate.
    assert lru.check_and_update(0x1234, 11) is True


def test_lru_evicts_oldest_when_full():
    lru = NumonceLru()
    # Fill 16 slots with distinct senders.
    for i in range(_SLOTS):
        assert lru.check_and_update(i, 1) is True
    assert len(lru) == _SLOTS
    # One more sender evicts slot 0 (oldest).
    assert lru.check_and_update(999, 1) is True
    assert len(lru) == _SLOTS
    assert lru.highest(0) == 0        # evicted
    assert lru.highest(999) == 1
    # Senders 1..15 still present.
    for i in range(1, _SLOTS):
        assert lru.highest(i) == 1


def test_recent_use_moves_sender_to_mru():
    lru = NumonceLru()
    for i in range(_SLOTS):
        lru.check_and_update(i, 1)
    # Touch sender 0 - should move it to MRU.
    assert lru.check_and_update(0, 2) is True
    # Add a new sender - sender 1 should be evicted (next-oldest), not 0.
    assert lru.check_and_update(999, 1) is True
    assert lru.highest(0) == 2
    assert lru.highest(1) == 0        # evicted
    assert lru.highest(999) == 1


def test_clear_resets_all_slots():
    lru = NumonceLru()
    lru.check_and_update(0x1234, 5)
    lru.check_and_update(0x5678, 7)
    lru.clear()
    assert len(lru) == 0
    assert lru.highest(0x1234) == 0
    # Fresh admission after clear.
    assert lru.check_and_update(0x1234, 1) is True


def test_evicted_sender_restarts_from_any_numonce():
    """After eviction, a sender's history is gone - so any numonce
    other than 0 is admitted. In production the Director's counter
    is monotonic in NVS, so this reset-after-eviction can't be
    exploited by an attacker without the secret anyway (HMAC still
    gates the write).
    """
    lru = NumonceLru()
    lru.check_and_update(42, 100)
    # Fill + overflow to evict 42.
    for i in range(_SLOTS + 1):
        lru.check_and_update(1000 + i, 1)
    assert lru.highest(42) == 0
    assert lru.check_and_update(42, 1) is True
