"""Tests for PELT topic segmentation.

Tests the core segmentation logic with synthetic embedding sequences.
No database or model loading required.
"""

import numpy as np
import pytest

from jmfts_core.segmentation import pelt_segment, enforce_segment_bounds, Segment


def _make_block_embeddings(
    block_sizes: list[int],
    dim: int = 768,
    noise: float = 0.05,
    rng_seed: int = 42,
) -> tuple[np.ndarray, list[int]]:
    """Create a synthetic embedding matrix with distinct topic blocks.

    Each block gets a random centroid; points within the block are
    the centroid + small Gaussian noise, then L2-normalised (matching
    what EmbeddingService produces).

    Returns (embeddings, fake_child_ids).
    """
    rng = np.random.default_rng(rng_seed)
    embeddings = []
    for block_idx, size in enumerate(block_sizes):
        centroid = rng.standard_normal(dim)
        centroid /= np.linalg.norm(centroid)
        for _ in range(size):
            vec = centroid + rng.normal(0, noise, dim)
            vec /= np.linalg.norm(vec)
            embeddings.append(vec)

    embeddings = np.array(embeddings)
    child_ids = list(range(1, len(embeddings) + 1))
    return embeddings, child_ids


# --------------------------------------------------------------------------- #
# Basic behaviour
# --------------------------------------------------------------------------- #

class TestPeltSegmentBasic:
    """Core segmentation sanity checks."""

    def test_single_topic_returns_one_segment(self):
        embeddings, ids = _make_block_embeddings([10])
        segments = pelt_segment(embeddings, ids, penalty=1.0)
        assert len(segments) == 1
        assert segments[0].child_ids == ids

    def test_two_distinct_topics_detected(self):
        embeddings, ids = _make_block_embeddings([8, 8])
        # Low penalty to encourage splitting
        segments = pelt_segment(embeddings, ids, penalty=0.1)
        assert len(segments) >= 2
        # First segment should be roughly the first block
        assert segments[0].start == 0
        # Last segment should end at the total count
        assert segments[-1].end == 16

    def test_three_topics_detected(self):
        embeddings, ids = _make_block_embeddings([10, 10, 10])
        segments = pelt_segment(embeddings, ids, penalty=0.1)
        assert len(segments) >= 3

    def test_segments_cover_full_range(self):
        """Segments must be contiguous and cover [0, n)."""
        embeddings, ids = _make_block_embeddings([5, 5, 5])
        segments = pelt_segment(embeddings, ids, penalty=0.5)
        assert segments[0].start == 0
        assert segments[-1].end == 15
        for i in range(1, len(segments)):
            assert segments[i].start == segments[i - 1].end

    def test_child_ids_preserved(self):
        """All child_ids should appear exactly once across segments."""
        embeddings, ids = _make_block_embeddings([6, 6])
        segments = pelt_segment(embeddings, ids, penalty=0.5)
        all_ids = []
        for s in segments:
            all_ids.extend(s.child_ids)
        assert sorted(all_ids) == sorted(ids)


# --------------------------------------------------------------------------- #
# Penalty controls granularity
# --------------------------------------------------------------------------- #

class TestPenaltyGranularity:
    """Higher penalty → fewer segments."""

    def test_high_penalty_fewer_segments(self):
        embeddings, ids = _make_block_embeddings([8, 8, 8])
        seg_low = pelt_segment(embeddings, ids, penalty=0.01)
        seg_high = pelt_segment(embeddings, ids, penalty=100.0)
        assert len(seg_high) <= len(seg_low)

    def test_very_high_penalty_returns_one_segment(self):
        embeddings, ids = _make_block_embeddings([8, 8, 8])
        segments = pelt_segment(embeddings, ids, penalty=1000.0)
        assert len(segments) == 1


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #

class TestEdgeCases:
    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            pelt_segment(np.array([]).reshape(0, 768), [], penalty=1.0)

    def test_length_mismatch_raises(self):
        embeddings = np.random.randn(5, 768)
        with pytest.raises(ValueError, match="mismatch"):
            pelt_segment(embeddings, [1, 2, 3], penalty=1.0)

    def test_single_document_returns_one_segment(self):
        embeddings = np.random.randn(1, 768)
        embeddings /= np.linalg.norm(embeddings)
        segments = pelt_segment(embeddings, [42], penalty=1.0)
        assert len(segments) == 1
        assert segments[0].child_ids == [42]

    def test_min_size_respected(self):
        embeddings, ids = _make_block_embeddings([3, 3])
        segments = pelt_segment(embeddings, ids, penalty=0.01, min_size=4)
        # With min_size=4 and only 6 docs, can't split into two
        assert len(segments) == 1


# --------------------------------------------------------------------------- #
# enforce_segment_bounds
# --------------------------------------------------------------------------- #


def _segs(ranges: list[tuple[int, int]], id_start: int = 1) -> list[Segment]:
    """Helper: build Segment list from (start, end) tuples with sequential IDs."""
    result = []
    for start, end in ranges:
        result.append(
            Segment(
                start=start,
                end=end,
                child_ids=list(range(id_start + start, id_start + end)),
            )
        )
    return result


class TestEnforceSegmentBoundsMerge:
    """Phase 1: small segments get merged into neighbours."""

    def test_no_change_when_all_above_min(self):
        segs = _segs([(0, 5), (5, 10)])
        result = enforce_segment_bounds(segs, min_segment=3)
        assert len(result) == 2

    def test_single_small_segment_merged(self):
        # Two segments: size 2 (below min_segment=3) and size 5
        segs = _segs([(0, 2), (2, 7)])
        result = enforce_segment_bounds(segs, min_segment=3, max_segment=20)
        assert len(result) == 1
        assert result[0].start == 0
        assert result[0].end == 7
        assert len(result[0].child_ids) == 7

    def test_middle_small_segment_merges_with_smaller_neighbour(self):
        # Three segments: size 5, size 1, size 3
        segs = _segs([(0, 5), (5, 6), (6, 9)])
        result = enforce_segment_bounds(segs, min_segment=3, max_segment=20)
        assert len(result) == 2
        # The size-1 segment merges with the size-3 neighbour (smaller)
        assert result[0].end - result[0].start == 5
        assert result[1].end - result[1].start == 4

    def test_all_small_collapses_to_one(self):
        segs = _segs([(0, 2), (2, 4), (4, 6)])
        result = enforce_segment_bounds(segs, min_segment=5, max_segment=20)
        assert len(result) == 1
        assert result[0].child_ids == list(range(1, 7))

    def test_empty_input(self):
        assert enforce_segment_bounds([], min_segment=3) == []

    def test_single_segment_below_min_kept(self):
        segs = _segs([(0, 2)])
        result = enforce_segment_bounds(segs, min_segment=5)
        # Only one segment — nothing to merge with
        assert len(result) == 1


class TestEnforceSegmentBoundsSplit:
    """Phase 2: oversized segments get split."""

    def test_no_split_when_within_max(self):
        segs = _segs([(0, 8)])
        result = enforce_segment_bounds(segs, min_segment=1, max_segment=10)
        assert len(result) == 1

    def test_split_on_exact_boundary(self):
        segs = _segs([(0, 20)])
        result = enforce_segment_bounds(segs, min_segment=1, max_segment=10)
        assert len(result) == 2
        assert result[0].end - result[0].start == 10
        assert result[1].end - result[1].start == 10

    def test_split_uneven(self):
        segs = _segs([(0, 15)])
        result = enforce_segment_bounds(segs, min_segment=1, max_segment=10)
        assert len(result) == 2
        sizes = [r.end - r.start for r in result]
        assert sum(sizes) == 15
        assert all(s <= 10 for s in sizes)

    def test_split_preserves_child_ids(self):
        segs = _segs([(0, 12)])
        result = enforce_segment_bounds(segs, min_segment=1, max_segment=5)
        all_ids = []
        for s in result:
            all_ids.extend(s.child_ids)
        assert sorted(all_ids) == list(range(1, 13))

    def test_split_contiguous(self):
        segs = _segs([(0, 25)])
        result = enforce_segment_bounds(segs, min_segment=1, max_segment=10)
        for i in range(1, len(result)):
            assert result[i].start == result[i - 1].end


class TestEnforceSegmentBoundsCombined:
    """Both merge and split in one pass."""

    def test_merge_then_split(self):
        # Two tiny segments (size 1 each) get merged, then a big one gets split
        segs = _segs([(0, 1), (1, 2), (2, 17)])
        result = enforce_segment_bounds(segs, min_segment=3, max_segment=10)
        # After merge: [0,2) and [2,17).  Then [2,17) is split (size 15 > 10)
        sizes = [r.end - r.start for r in result]
        assert all(s <= 10 for s in sizes)
        total = sum(sizes)
        assert total == 17
