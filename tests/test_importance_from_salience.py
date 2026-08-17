"""importance_from_salience — the WRITE side of the importance axis.

Pure-function tests: no database, no model. The point under test is the aggregation
arithmetic and its boundary behaviour, plus that its output is a valid input to the
read side (SearchRepository._importance_factor). See ROADMAP "importance_from_salience".
"""

import pytest

from jmfts_core.models.document import Document
from jmfts_core.repositories.search import SearchRepository
from jmfts_core.token_selection import importance_from_salience


def test_empty_scores_floor_to_one():
    """No tokens at all → the scale's floor, not a crash or a fabricated mid-value."""
    assert importance_from_salience([]) == 1.0


def test_all_nonpositive_scores_floor_to_one():
    """Stopwords/punctuation carry a large negative penalty and are excluded; a document
    with no positive content salience floors to 1.0 (= 0.0 read-side factor, neutral)."""
    assert importance_from_salience([-99.0, -100.0, 0.0]) == 1.0


def test_max_salience_maps_to_ten():
    assert importance_from_salience([1.0, 1.0, 1.0]) == 10.0


def test_midpoint_salience_maps_to_scale_midpoint():
    assert importance_from_salience([0.5, 0.5]) == 5.5  # 1 + 9*0.5


def test_only_positive_scores_are_averaged():
    """Negatives are filtered before the mean, so [1.0, -99.0] averages 1.0, not -49."""
    assert importance_from_salience([1.0, -99.0]) == 10.0


def test_out_of_range_high_score_is_clamped():
    """A stray >1 salience can never push importance past the read scale's ceiling."""
    assert importance_from_salience([5.0]) == 10.0


def test_monotonic_in_mean_salience():
    low = importance_from_salience([0.2, 0.2])
    high = importance_from_salience([0.8, 0.8])
    assert 1.0 <= low < high <= 10.0


def test_none_scores_are_ignored():
    assert importance_from_salience([None, 0.5, None, 0.5]) == 5.5


def test_output_is_a_valid_read_side_input():
    """Whatever the writer emits must round-trip through the reader without raising —
    _importance_factor rejects anything outside [1, 10], so this proves the contract."""
    imp = importance_from_salience([0.6, 0.4, 0.8])
    doc = Document(id=1, structured_content={"importance": imp})
    factor = SearchRepository._importance_factor(doc)
    assert 0.0 <= factor <= 1.0


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
