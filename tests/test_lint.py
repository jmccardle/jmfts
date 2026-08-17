"""Unit tests for jmfts_core.lint pure helpers (no DB).

Full lint endpoint behavior is covered by integration smoke tests.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from jmfts_core.lint import _windows_overlap


def _t(valid_from=None, valid_until=None):
    return SimpleNamespace(valid_from=valid_from, valid_until=valid_until)


class TestWindowsOverlap:
    def test_both_unbounded_overlap(self):
        assert _windows_overlap(_t(), _t()) is True

    def test_disjoint_after(self):
        a = _t(valid_until=datetime(2020, 1, 1, tzinfo=timezone.utc))
        b = _t(valid_from=datetime(2021, 1, 1, tzinfo=timezone.utc))
        assert _windows_overlap(a, b) is False
        assert _windows_overlap(b, a) is False

    def test_touching_boundaries_overlap(self):
        # a ends exactly when b starts → still considered overlapping
        # (because we use < not <=)
        ts = datetime(2020, 6, 1, tzinfo=timezone.utc)
        a = _t(valid_until=ts)
        b = _t(valid_from=ts)
        assert _windows_overlap(a, b) is True

    def test_overlap_in_middle(self):
        a = _t(
            valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
            valid_until=datetime(2020, 12, 31, tzinfo=timezone.utc),
        )
        b = _t(
            valid_from=datetime(2020, 6, 1, tzinfo=timezone.utc),
            valid_until=datetime(2021, 6, 1, tzinfo=timezone.utc),
        )
        assert _windows_overlap(a, b) is True

    def test_one_unbounded_lower_disjoint(self):
        a = _t(valid_until=datetime(2020, 1, 1, tzinfo=timezone.utc))
        b = _t(valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc))  # b starts after a ends
        assert _windows_overlap(a, b) is False

    def test_one_unbounded_upper_overlap(self):
        a = _t(valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc))
        b = _t(valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc))  # both still valid
        assert _windows_overlap(a, b) is True
