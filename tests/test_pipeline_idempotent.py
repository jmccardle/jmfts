"""Unit tests for content_hash idempotency in the ingest pipeline."""

from jmfts_core.repositories.document import compute_content_hash


class TestComputeContentHash:
    def test_identical_content_same_hash(self):
        a = compute_content_hash("hello world")
        b = compute_content_hash("hello world")
        assert a == b
        assert len(a) == 64  # SHA-256 hex

    def test_different_content_different_hash(self):
        a = compute_content_hash("hello")
        b = compute_content_hash("world")
        assert a != b

    def test_unicode_handled(self):
        a = compute_content_hash("café")
        b = compute_content_hash("café")
        assert a == b

    def test_whitespace_matters(self):
        # Idempotency operates on raw content — leading/trailing whitespace
        # produces a distinct hash. Callers responsible for any normalization.
        a = compute_content_hash("hello")
        b = compute_content_hash(" hello ")
        assert a != b

    def test_none_returns_none(self):
        assert compute_content_hash(None) is None

    def test_empty_returns_none(self):
        # Empty string is treated as "no content" — keeps content_hash NULL on
        # documents created without content.
        assert compute_content_hash("") is None
