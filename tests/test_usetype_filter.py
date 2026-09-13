"""A positive ``usetype`` filter names a SET of globs, and matching any one of them is a hit.

**The defect this file was written against**, verified 2026-09-05. A search context's
``config`` carried a single ``usetype`` glob string and ``_usetype_to_like`` translated only
``*`` and ``?``. So the preset spelling that had already been written for "everything I
said" — ``"transcript:*,obsidian:*"`` — was translated whole, into
``LIKE 'transcript:%,obsidian:%'``, and matched NOTHING. A named preset, on every install,
returning an empty page with no error to say why. Two of the three presets planned for
0.5.0 are of that shape and neither could be expressed at all.

**Why the tests are where they are.** The translator was already covered — by
``tests/test_search_contexts.py``, which asserted against an INLINE COPY of it. The copy
agreed with the original exactly, and both were wrong in the same way, because a copy can
only ever confirm the behaviour it was copied from. Every test here drives the shipped
function: :func:`jmfts_client.contracts.search.usetype_globs` for the wire shape, and the
four retrieval methods for what a filter does to a query.

**Four paths, and they must agree.** ``vector_search`` and ``fulltext_search`` build the
predicate through the ORM; ``bm25_search`` post-filters rows in Python; ``maxsim_search``
interpolates a hand-written SQL fragment. A filter that means one thing in three of them
and something else in the fourth is the same class of defect as the one above, so each
gets the same fixture and the same expected set.
"""

import itertools
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql

from jmfts_client.contracts.search import AppliedFilters, SearchRequest, usetype_globs
from jmfts_core.config import get_settings
from jmfts_core.models.document import Document
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import (
    SearchRepository,
    effective_exclude_types,
    _usetype_matches,
    _usetype_predicate,
    _usetype_sql,
    _usetype_to_like,
)

from tests.conftest import requires_db

#: ``documents.embed`` is ``Vector(768)`` (``models/document.py:91``).
DIM = 768

#: A token every seeded document carries, so BM25 and full-text retrieve the whole fixture
#: and the only thing that narrows it is the usetype filter under test.
BEACON = "marmalade"

#: The fixture, by the name a test refers to it by. Two usetypes under ``transcript:``, one
#: under ``obsidian:``, one under neither, one held out by the default exclusion list, one
#: with NO usetype at all, and one carrying an apostrophe — that last is not decoration:
#: MaxSim's filter used to be formatted into its SQL with ``f"d.usetype = '{usetype}'"``,
#: and this row is what a bound parameter has to survive.
FIXTURE: dict[str, str | None] = {
    "daily": "transcript:daily",
    "twentysix": "transcript:2026",
    "note": "obsidian:note",
    "url": "wiki:url",
    "brien": "o'brien:note",
    "digest": "summary",
    "loose": None,
}

#: What ``transcript:*,obsidian:*`` names — the preset that matched nothing.
SAID = {"daily", "twentysix", "note"}


#: Which neighbourhood :func:`_seed` last placed the fixture in. Counted rather than fixed,
#: for the reason :func:`_document_vector` records: a vector this file has already written
#: and rolled back is the one vector it must not write again.
_ROUND = itertools.count()

#: The current neighbourhood's centre, set by :func:`_seed` and read by :func:`_query_vector`.
_BASE: list[float] | None = None


def _new_base() -> list[float]:
    """A fresh dense unit vector, one per :func:`_seed` call, stable across runs.

    Dense because a real embedder produces no zero coordinates. Seeded from a counter rather
    than from entropy so that a failure reproduces: round *n* of a run is round *n* of the
    next one, whatever the direction happens to be. Nothing here asserts on the direction —
    only on which documents came back — so the direction is free to move.
    """
    rng = np.random.default_rng(20260912 + next(_ROUND))
    vec = rng.standard_normal(DIM).astype(np.float32)
    return (vec / np.linalg.norm(vec)).tolist()


def _query_vector() -> list[float]:
    """The centre of the neighbourhood the current fixture was seeded in.

    A copy, so a test that mutates what it gets back cannot move the next query.
    """
    global _BASE
    if _BASE is None:
        _BASE = _new_base()
    return list(_BASE)


#: How far each document sits off the query direction. Big enough that no two documents are
#: near-duplicates of one another — pairwise cosine distance is about 0.010, against the
#: 0.005 that separates each of them from the query — and small enough that all seven remain
#: the query's nearest neighbours by a wide margin.
OFF_AXIS = 0.1


def _document_vector(name: str) -> list[float]:
    """A DISTINCT vector for ``name``, the same cosine distance from the query as the rest.

    The offset is taken ORTHOGONAL to the query direction and then the result is normalised,
    so every document's cosine similarity to :func:`_query_vector` is ``1/sqrt(1 +
    OFF_AXIS**2)`` exactly, whatever ``name`` is. The fixture therefore keeps the property
    the tests below rely on — no method can rank one document above another on content —
    without giving two rows the same bytes.

    **Why the vectors move between calls**, which is what changed on 2026-09-12. A rolled
    back row leaves its node in the HNSW graph, so a file that seeds the same point in test
    after test searches a graph holding dead copies of the very vector it is looking for,
    and such a search returns a SUBSET of the live rows that match. Measured standalone on
    ``pgvector/pgvector:pg16``, PostgreSQL 16.15 with pgvector 0.8.6, over 200 rounds of
    "seed seven rows, ask for them back, roll back": 59 rounds short at pgvector's default
    scan, 24 at ``relaxed_order``, 2 at the ``strict_order`` that
    ``repositories/search.py:52`` sets — most of them returning ONE row of seven. Four tests
    in this file failed intermittently in CI for that reason, and they were measuring HNSW
    recall while claiming to measure a usetype filter.

    Distinct vectors at ONE point did not fix it — 40 rounds of 200 still came up short,
    because the dead copies pile up at each document's OWN point rather than at a shared
    one. Moving the neighbourhood is what fixes it, and it is also what a real corpus does:
    no two ingests store the same vector. This is not a workaround for the defect, which is
    real and outlives the fixture; it is this file declining to measure it.
    """
    base = np.asarray(_query_vector(), dtype=np.float64)
    rng = np.random.default_rng(1000 + list(FIXTURE).index(name))
    off = rng.standard_normal(DIM)
    off -= base * float(off @ base)
    off /= np.linalg.norm(off)
    vec = base + OFF_AXIS * off
    return (vec / np.linalg.norm(vec)).astype(np.float32).tolist()


def _seed(session) -> dict[str, int]:
    """One document per :data:`FIXTURE` entry, all equally good answers to every method.

    Every document carries the same body text and a vector the same distance from the query
    as every other document's, so no method can rank one above another on content. Whatever
    comes back is what the filter admitted. The vectors are distinct rather than identical
    for the reason :func:`_document_vector` records.
    """
    global _BASE
    _BASE = _new_base()
    repo = DocumentRepository(session)
    ids: dict[str, int] = {}
    for name, usetype in FIXTURE.items():
        doc = repo.create(
            title=f"{name} {BEACON}",
            content=f"{BEACON} beacon body for {name}",
            usetype=usetype,
            auto_embed=False,
        )
        doc.embed = _document_vector(name)
        ids[name] = doc.id
    session.flush()
    return ids


def _names(ids: dict[str, int], results) -> set[str]:
    """The fixture names behind a result list, so assertions read as the fixture reads."""
    by_id = {doc_id: name for name, doc_id in ids.items()}
    return {by_id[r.document.id] for r in results if r.document.id in by_id}


class _StubEmbeddingService:
    """Deterministic vectors, no model. Enough for the two methods that embed a query.

    ``maxsim_search`` embeds its query and reads ``token_embeddings`` rows, so a MaxSim test
    needs both an embedder for the query and token rows on the documents. Both come from
    here; the vectors are arbitrary but stable, which is all that is needed when the
    assertion is about WHICH documents came back rather than in what order.
    """

    dim = DIM

    def _unit(self, seed: str):
        rng = np.random.default_rng(abs(hash(seed)) % (2**31))
        vec = rng.standard_normal(self.dim).astype(np.float32)
        return vec / np.linalg.norm(vec)

    def embed_text(self, text_value, normalize=True, prefix=""):
        return self._unit(text_value)

    def embed_with_tokens(self, text_value, top_percent=0.35, token_selector=None, prefix=""):
        from jmfts_core.embedding import EmbeddingResult, TokenEmbeddingResult

        words = (text_value.split() or ["empty"])[:3]
        return EmbeddingResult(
            document_embedding=self._unit(text_value),
            token_embeddings=[
                TokenEmbeddingResult(
                    token_idx=i,
                    token_text=word,
                    importance_score=1.0 - i * 0.2,
                    embedding=self._unit(f"{text_value}_{i}"),
                )
                for i, word in enumerate(words)
            ],
        )

    def truncate_embedding(self, embedding, target_dim, normalize=True):
        trunc = embedding[:target_dim].copy()
        if normalize:
            norm = np.linalg.norm(trunc)
            if norm > 0:
                trunc /= norm
        return trunc


# =============================================================================
# The wire shape — no database
# =============================================================================


class TestUsetypeGlobs:
    """What a caller may write, and what it resolves to."""

    def test_the_preset_that_matched_nothing_now_names_two_globs(self):
        """The defect, at the point where it was introduced."""
        assert usetype_globs("transcript:*,obsidian:*") == ("transcript:*", "obsidian:*")

    def test_a_single_glob_is_a_one_element_set(self):
        assert usetype_globs("chunk") == ("chunk",)
        assert usetype_globs("conversation/*") == ("conversation/*",)

    def test_the_list_form_says_the_same_thing(self):
        assert usetype_globs(["transcript:*", "obsidian:*"]) == ("transcript:*", "obsidian:*")

    def test_whitespace_around_a_separator_is_formatting(self):
        assert usetype_globs("transcript:*, obsidian:*") == ("transcript:*", "obsidian:*")

    def test_the_list_form_never_splits_so_a_comma_is_nameable(self):
        """The escape hatch. ``documents.usetype`` is an open string and may hold a comma."""
        assert usetype_globs(["odd,name", "plain"]) == ("odd,name", "plain")

    def test_none_means_no_positive_filter(self):
        assert usetype_globs(None) is None

    @pytest.mark.parametrize("empty", ["", "   ", ",", "a,", [], ["a", ""]])
    def test_an_empty_set_is_refused_rather_than_widened(self, empty):
        """The Fail Early case: a filter that names nothing must not become 'match all'."""
        with pytest.raises(ValueError):
            usetype_globs(empty)

    @pytest.mark.parametrize("dupe", ["chunk,chunk", ["chunk", "chunk"]])
    def test_a_duplicate_glob_is_a_typo(self, dupe):
        with pytest.raises(ValueError, match="duplicate"):
            usetype_globs(dupe)

    @pytest.mark.parametrize("wrong", [5, [5], {"a": 1}])
    def test_a_non_string_glob_raises(self, wrong):
        with pytest.raises(ValueError):
            usetype_globs(wrong)

    def test_a_set_filter_still_overrides_the_hold_out_list(self):
        """The three-way rule in ``effective_exclude_types`` is unchanged by the set.

        A positive filter of any size means the caller said what they want, so nothing is
        held out; ``None`` means the configured exclusions apply; an empty one raises here
        too, so the response's ``applied`` block cannot disagree with the query.
        """
        assert effective_exclude_types(["transcript:*", "obsidian:*"], None) == []
        assert effective_exclude_types("transcript:*,obsidian:*", None) == []
        assert effective_exclude_types(None, None) == list(get_settings().search_exclude_usetypes)
        with pytest.raises(ValueError):
            effective_exclude_types("", None)

    def test_a_set_filter_can_be_echoed_back(self):
        """``applied.usetype`` reports what ran, so it has to hold what a caller may send."""
        assert AppliedFilters(limit=10, usetype=["a", "b"]).usetype == ["a", "b"]
        assert AppliedFilters(limit=10, usetype="a,b").usetype == "a,b"

    def test_the_contract_rejects_an_empty_filter_at_the_edge(self):
        """A request never reaches the repository with a filter the repository would refuse."""
        assert SearchRequest(query="q", usetype="transcript:*,obsidian:*").usetype
        assert SearchRequest(query="q", usetype=["a", "b"]).usetype == ["a", "b"]
        with pytest.raises(ValueError):
            SearchRequest(query="q", usetype="")


class TestPredicateShape:
    """What the two SQL builders emit, checked without a server."""

    @staticmethod
    def _sql(clause) -> str:
        return str(clause.compile(dialect=postgresql.dialect()))

    def test_a_single_exact_glob_compiles_to_the_statement_it_always_did(self):
        """Backward compatibility, at the level that decides the query plan.

        A one-glob filter must not gain an ``OR`` wrapper or become a ``LIKE``: it stays the
        equality test ``idx_documents_usetype`` is searchable by.
        """
        assert self._sql(_usetype_predicate(("chunk",))) == self._sql(Document.usetype == "chunk")

    def test_a_single_wildcard_glob_compiles_to_the_statement_it_always_did(self):
        assert self._sql(_usetype_predicate(("a/*",))) == self._sql(Document.usetype.like("a/%"))

    def test_exact_globs_share_one_membership_test(self):
        """Two exact names are ``IN``, not two ``LIKE``s, so the btree is still searchable."""
        compiled = self._sql(_usetype_predicate(("chunk", "section")))
        assert " IN " in compiled
        assert "LIKE" not in compiled

    def test_a_mixed_set_ors_the_two_kinds(self):
        compiled = self._sql(_usetype_predicate(("chunk", "a/*")))
        assert " OR " in compiled and "LIKE" in compiled

    def test_the_maxsim_fragment_binds_its_globs(self):
        """Nothing from the filter reaches the statement text.

        The two lines this replaced formatted the value in directly, so a usetype carrying
        an apostrophe was a syntax error and a usetype carrying SQL was worse.
        """
        fragment, params = _usetype_sql(("o'brien:*", "chunk"), "d")
        assert "o'brien" not in fragment
        assert params == {"usetype_exact": ["chunk"], "usetype_globs": ["o'brien:%"]}

    def test_in_memory_matching_is_any_not_all(self):
        globs = ("transcript:*", "obsidian:*")
        assert _usetype_matches(globs, "transcript:daily") is True
        assert _usetype_matches(globs, "obsidian:note") is True
        assert _usetype_matches(globs, "wiki:url") is False
        assert _usetype_matches(globs, None) is False


# =============================================================================
# What a filter does to each retrieval method
# =============================================================================


@requires_db
def test_the_comma_string_matches_what_it_names(db_session):
    """THE DEFECT. ``transcript:*,obsidian:*`` used to match nothing at all."""
    ids = _seed(db_session)
    repo = SearchRepository(db_session)
    results = repo.vector_search(_query_vector(), limit=50, usetype="transcript:*,obsidian:*")
    assert _names(ids, results) == SAID

    # The red baseline, on this same fixture: the predicate the old code built — the whole
    # filter translated as ONE glob — selects nothing. Three documents match the filter and
    # the query returns none of them, with no error anywhere, which is why the preset could
    # ship. Kept here so the test above cannot be read as passing vacuously.
    was = db_session.execute(
        select(Document.id).where(
            Document.usetype.like(_usetype_to_like("transcript:*,obsidian:*"))
        )
    ).all()
    assert was == []


@requires_db
def test_the_list_form_names_the_same_set(db_session):
    ids = _seed(db_session)
    repo = SearchRepository(db_session)
    results = repo.vector_search(_query_vector(), limit=50, usetype=["transcript:*", "obsidian:*"])
    assert _names(ids, results) == SAID


@requires_db
def test_a_single_glob_behaves_exactly_as_before(db_session):
    """The whole existing surface: one exact name, one wildcard, unchanged."""
    ids = _seed(db_session)
    repo = SearchRepository(db_session)

    exact = repo.vector_search(_query_vector(), limit=50, usetype="obsidian:note")
    assert _names(ids, exact) == {"note"}

    wildcard = repo.vector_search(_query_vector(), limit=50, usetype="transcript:*")
    assert _names(ids, wildcard) == {"daily", "twentysix"}


@requires_db
def test_no_filter_is_not_the_same_as_an_empty_one(db_session):
    """``None`` widens deliberately; ``""`` and ``[]`` are refused.

    Before this change both took the same branch — ``if usetype:`` — so a caller who sent an
    empty filter got the unfiltered page silently. The two must be distinguishable, and the
    second must not be the first.
    """
    ids = _seed(db_session)
    repo = SearchRepository(db_session)

    unfiltered = _names(ids, repo.vector_search(_query_vector(), limit=50, usetype=None))
    # Everything except the one the DEFAULT hold-out list removes. The document with no
    # usetype at all is admitted: exclusion never removes a NULL.
    assert unfiltered == set(FIXTURE) - {"digest"}

    for empty in ("", []):
        with pytest.raises(ValueError):
            repo.vector_search(_query_vector(), limit=50, usetype=empty)


@requires_db
def test_fulltext_takes_the_same_set(db_session):
    ids = _seed(db_session)
    repo = SearchRepository(db_session)
    results = repo.fulltext_search(BEACON, limit=50, usetype="transcript:*,obsidian:*")
    assert _names(ids, results) == SAID


@requires_db
def test_bm25_takes_the_same_set(db_session):
    """The post-filter path: BM25 narrows rows in Python rather than in SQL."""
    ids = _seed(db_session)
    repo = SearchRepository(db_session)
    index_name = "usetype_filter_test"
    for doc_id in ids.values():
        repo.index_document(doc_id, index_name)

    results = repo.bm25_search(
        BEACON, index_name=index_name, limit=50, usetype="transcript:*,obsidian:*"
    )
    assert _names(ids, results) == SAID

    with pytest.raises(ValueError):
        repo.bm25_search(BEACON, index_name=index_name, limit=50, usetype=[])


@requires_db
def test_maxsim_takes_the_same_set_and_survives_a_quoted_usetype(db_session):
    """The hand-written SQL path, and the reason its globs are bound.

    ``o'brien:note`` is in the fixture because MaxSim's filter used to be formatted into the
    statement. Naming it here is the assertion that it no longer is: an interpolated
    apostrophe is a syntax error, not a wrong answer, so this test cannot pass by accident.
    """
    stub = _StubEmbeddingService()
    with patch("jmfts_core.repositories.document.get_embedder", return_value=stub):
        ids = _seed(db_session)
        # Token rows, which `_seed` skips by asking for no embedding at all.
        doc_repo = DocumentRepository(db_session)
        for doc_id in ids.values():
            doc_repo.embed_document(doc_id, with_tokens=True)
    db_session.flush()

    with patch("jmfts_core.repositories.search.get_embedding_service", return_value=stub):
        repo = SearchRepository(db_session)
        said = repo.maxsim_search(BEACON, limit=50, usetype="transcript:*,obsidian:*")
        assert _names(ids, said) == SAID

        quoted = repo.maxsim_search(BEACON, limit=50, usetype="o'brien:note")
        assert _names(ids, quoted) == {"brien"}

        both = repo.maxsim_search(BEACON, limit=50, usetype="o'brien:*,wiki:*")
        assert _names(ids, both) == {"brien", "url"}


@requires_db
def test_the_orm_predicate_and_the_maxsim_fragment_select_the_same_rows(db_session):
    """The two builders are separate code and must not disagree.

    ``_usetype_sql`` is the only place a usetype reaches the server as raw SQL rather than
    through the ORM, so it is compared against the ORM's answer on the same rows rather
    than against a hand-written expectation.
    """
    ids = _seed(db_session)
    globs = ("transcript:*", "obsidian:*", "o'brien:note")

    orm = {
        row[0]
        for row in db_session.execute(
            Document.__table__.select()
            .with_only_columns(Document.id)
            .where(_usetype_predicate(globs))
        ).all()
    }

    fragment, params = _usetype_sql(globs, "d")
    raw = {
        row[0]
        for row in db_session.execute(
            text(f"SELECT d.id FROM documents d WHERE {fragment}"), params
        ).all()
    }

    assert orm == raw
    assert orm & set(ids.values()) == {ids[name] for name in SAID | {"brien"}}
