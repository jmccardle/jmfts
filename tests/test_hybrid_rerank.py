"""Recency/importance rerank on hybrid_search (M2 mechanism).

These exercise the scoring math directly on the static helpers plus a stubbed fusion,
so they need no database: the point under test is the rerank arithmetic and its
failure modes, not retrieval itself.
"""

from datetime import datetime, timedelta, timezone

import pytest

from jmfts_core.models.document import Document
from jmfts_core.repositories.search import SearchRepository

NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


_UNSET = object()  # so an explicit created_at=None is distinguishable from "not given"


def _doc(doc_id=1, *, event_time=None, created_at=_UNSET, importance=None):
    doc = Document(
        id=doc_id,
        title="t",
        content="c",
        structured_content={} if importance is None else {"importance": importance},
    )
    doc.event_time = event_time
    doc.created_at = NOW if created_at is _UNSET else created_at
    return doc


# --- recency -------------------------------------------------------------------


def test_recency_decays_by_halflife():
    doc = _doc(event_time=NOW - timedelta(days=7))
    assert SearchRepository._recency_factor(doc, NOW, 7.0) == pytest.approx(0.5)


def test_recency_of_now_is_one():
    assert SearchRepository._recency_factor(_doc(event_time=NOW), NOW, 7.0) == pytest.approx(1.0)


def test_event_time_overrides_created_at():
    """The whole point of the column: ingest time must not drive recency."""
    doc = _doc(event_time=NOW - timedelta(days=700), created_at=NOW)
    assert SearchRepository._recency_factor(doc, NOW, 7.0) < 0.001


def test_created_at_is_the_fallback_clock():
    doc = _doc(event_time=None, created_at=NOW - timedelta(days=7))
    assert SearchRepository._recency_factor(doc, NOW, 7.0) == pytest.approx(0.5)


def test_naive_timestamp_read_as_utc():
    """The ORM writes naive utcnow() into TIMESTAMPTZ, so naive must mean UTC."""
    doc = _doc(event_time=(NOW - timedelta(days=7)).replace(tzinfo=None))
    assert SearchRepository._recency_factor(doc, NOW, 7.0) == pytest.approx(0.5)


def test_future_timestamp_clamps_to_one():
    doc = _doc(event_time=NOW + timedelta(days=30))
    assert SearchRepository._recency_factor(doc, NOW, 7.0) == pytest.approx(1.0)


def test_no_clock_at_all_is_neutral():
    doc = _doc(event_time=None, created_at=None)
    assert SearchRepository._recency_factor(doc, NOW, 7.0) == 0.0


# --- importance ----------------------------------------------------------------


def test_importance_normalises_scale_endpoints():
    assert SearchRepository._importance_factor(_doc(importance=1)) == pytest.approx(0.0)
    assert SearchRepository._importance_factor(_doc(importance=10)) == pytest.approx(1.0)
    assert SearchRepository._importance_factor(_doc(importance=5.5)) == pytest.approx(0.5)


def test_absent_importance_is_neutral_not_defaulted():
    assert SearchRepository._importance_factor(_doc(importance=None)) == 0.0


@pytest.mark.parametrize("bad", ["7", True, None.__class__, [7]])
def test_malformed_importance_raises(bad):
    """A broken writer must surface, not get silently coerced."""
    if bad is None.__class__:
        pytest.skip("type object is not a plausible payload")
    with pytest.raises(ValueError, match="importance"):
        SearchRepository._importance_factor(_doc(importance=bad))


@pytest.mark.parametrize("bad", [0, 11, -3, 10.5])
def test_out_of_scale_importance_raises(bad):
    with pytest.raises(ValueError, match="1-10 scale"):
        SearchRepository._importance_factor(_doc(importance=bad))


# --- fusion integration --------------------------------------------------------


class _StubRepo(SearchRepository):
    """hybrid_search with the retrieval methods stubbed to a fixed candidate ranking."""

    def __init__(self, docs):
        self._docs = docs

    def vector_search_text(self, query_text, **kwargs):
        from jmfts_core.repositories.search import SearchResult

        return [SearchResult(document=d, score=1.0, method="vector") for d in self._docs]


def _run(docs, **kw):
    return _StubRepo(docs).hybrid_search("q", limit=10, methods=["vector"], **kw)


def test_weights_default_to_off_leaving_plain_rrf():
    """Existing callers must see byte-identical behaviour."""
    old = _doc(1, event_time=NOW - timedelta(days=365))
    new = _doc(2, event_time=NOW)
    ranked = _run([old, new])
    assert [r.document.id for r in ranked] == [1, 2]  # pure rank order, recency ignored


def test_recency_promotes_the_newer_document():
    """The STALE lever: a later observation must outrank an earlier one."""
    old = _doc(1, event_time=NOW - timedelta(days=365))
    new = _doc(2, event_time=NOW)
    ranked = _run([old, new], recency_weight=1.0, recency_halflife_days=7.0, now=NOW)
    assert [r.document.id for r in ranked] == [2, 1]


def test_importance_promotes_the_more_important_document():
    dull = _doc(1, importance=1)
    vital = _doc(2, importance=10)
    ranked = _run([dull, vital], importance_weight=1.0, now=NOW)
    assert [r.document.id for r in ranked] == [2, 1]


def test_negative_weight_raises():
    with pytest.raises(ValueError, match="non-negative"):
        _run([_doc()], recency_weight=-1.0)


def test_non_positive_halflife_raises():
    with pytest.raises(ValueError, match="halflife"):
        _run([_doc()], recency_weight=1.0, recency_halflife_days=0)
