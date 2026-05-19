"""Tests for source_id partitioning (protocol manual §3.4)."""

from nocturnation.protocol import (
    SourceId,
    is_community_range,
    is_performance_range,
)
from nocturnation.protocol.source_id import (
    SourceId as SourceIdFromModule,
    is_community_range as is_community_range_from_module,
    is_performance_range as is_performance_range_from_module,
)


class TestBoundaryValues:
    def test_community_range_is_0x00_to_0x3F(self):
        assert SourceId.COMMUNITY_MIN == 0x00
        assert SourceId.COMMUNITY_MAX == 0x3F

    def test_performance_range_is_0x40_to_0xFE(self):
        assert SourceId.PERFORMANCE_MIN == 0x40
        assert SourceId.PERFORMANCE_MAX == 0xFE

    def test_broadcast_is_0xFF(self):
        assert SourceId.BROADCAST == 0xFF

    def test_ranges_are_contiguous_and_dont_overlap(self):
        # The two ranges meet exactly at 0x3F/0x40 - no gap, no overlap.
        assert SourceId.COMMUNITY_MAX + 1 == SourceId.PERFORMANCE_MIN

    def test_performance_range_stops_one_short_of_broadcast(self):
        assert SourceId.PERFORMANCE_MAX + 1 == SourceId.BROADCAST

    def test_re_exported_from_protocol_package(self):
        # SourceId is reachable both via `from nocturnation.protocol import ...`
        # and directly from the source_id submodule. The two MUST be the
        # same class (not a separate import that diverges silently).
        assert SourceId is SourceIdFromModule


class TestIsCommunityRange:
    def test_zero_is_community(self):
        assert is_community_range(0x00) is True

    def test_one_is_community(self):
        assert is_community_range(0x01) is True

    def test_mid_range_is_community(self):
        assert is_community_range(0x20) is True

    def test_upper_boundary_0x3F_is_community(self):
        assert is_community_range(0x3F) is True

    def test_0x40_is_not_community_first_performance_slot(self):
        assert is_community_range(0x40) is False

    def test_0xFE_is_not_community(self):
        assert is_community_range(0xFE) is False

    def test_broadcast_0xFF_is_not_community(self):
        assert is_community_range(0xFF) is False

    def test_re_exported(self):
        # The function exported from the package and the function
        # defined in the submodule MUST be the same callable.
        assert is_community_range is is_community_range_from_module


class TestIsPerformanceRange:
    def test_zero_is_not_performance(self):
        assert is_performance_range(0x00) is False

    def test_last_community_is_not_performance(self):
        assert is_performance_range(0x3F) is False

    def test_lower_boundary_0x40_is_performance(self):
        assert is_performance_range(0x40) is True

    def test_mid_range_is_performance(self):
        assert is_performance_range(0x7F) is True

    def test_upper_boundary_0xFE_is_performance(self):
        assert is_performance_range(0xFE) is True

    def test_broadcast_0xFF_is_not_performance(self):
        assert is_performance_range(0xFF) is False

    def test_re_exported(self):
        assert is_performance_range is is_performance_range_from_module


class TestExhaustivePartition:
    """For every possible source_id byte (0x00..0xFF), exactly one
    of {is_community_range, is_performance_range, == BROADCAST} is
    true. No source_id falls in a gap; none lands in two categories.
    """

    def test_every_byte_lands_in_exactly_one_partition(self):
        for source_id in range(0x100):
            in_community = is_community_range(source_id)
            in_performance = is_performance_range(source_id)
            is_broadcast = source_id == SourceId.BROADCAST
            categories = sum([in_community, in_performance, is_broadcast])
            assert categories == 1, (
                "source_id 0x%02X lands in %d categories "
                "(community=%s, performance=%s, broadcast=%s); expected exactly 1"
                % (source_id, categories, in_community, in_performance, is_broadcast)
            )
