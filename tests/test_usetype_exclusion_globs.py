"""``JMFTS_SEARCH_EXCLUDE_USETYPES`` / ``JMFTS_BM25_EXCLUDE_USETYPES`` take globs (#52).

A client that keeps a non-content corpus (``registry:topic``, ``registry:source``) beside its
documents could keep it out of default search only by listing every usetype in server
settings, because the hold-out lists matched exactly. An entry containing ``*`` or ``?`` is
now a pattern, and EVERY reader of the lists — the ORM predicate, the MaxSim SQL, BM25
indexing and BM25's result post-filter — goes through one pair of helpers.

The fixture is built so that each way a pattern could be mistranslated selects a different
set: ``registrar`` shares ``registry:``'s first six characters and has no colon, and the
literals ``_``, ``%`` and a backslash each sit in an entry beside a document that a wildcard
reading of them would also take.
"""

from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import select

from jmfts_core.config import get_settings
from jmfts_core.models.document import Document
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import (
    SearchRepository,
    usetype_is_excluded,
    usetype_not_excluded,
)

from tests import test_usetype_filter as base
from tests.conftest import requires_db

FIXTURE = {
    "topic": "registry:topic",
    "source": "registry:source",
    "registrar": "registrar",
    "under": "x_y:one",
    "notunder": "xzy:one",
    "pct": "p%:one",
    "notpct": "pq:one",
    "slash": "b\\s:one",
    "notslash": "bxs:one",
    "loose": None,
}

#: The entry list under test; `x_y:*`, `p%:*` and `b\s:*` are globs whose literal part holds
#: a character LIKE would read as a wildcard.
PATTERNS = ["registry:*", "x_y:*", "p%:*", "b\\s:*"]

HELD_OUT = {"topic", "source", "under", "pct", "slash"}
KEPT = set(FIXTURE) - HELD_OUT


class _QueryStub(base._StubEmbeddingService):
    """Embeds every query at the fixture's centre so the vector leg sees all of it."""

    def embed_text(self, text_value, normalize=True, prefix=""):
        return np.asarray(base._query_vector(), dtype=np.float32)


@pytest.fixture
def seeded(db_session, monkeypatch):
    monkeypatch.setattr(base, "FIXTURE", FIXTURE)
    settings = get_settings()
    monkeypatch.setattr(settings, "search_exclude_usetypes", list(PATTERNS))
    monkeypatch.setattr(settings, "bm25_exclude_usetypes", list(PATTERNS))
    return base._seed(db_session)


class TestMatcher:
    """No database: the in-memory twin says what a pattern means."""

    def test_a_glob_takes_the_namespace_and_not_the_lookalike(self):
        assert usetype_is_excluded(["registry:*"], "registry:topic")
        assert usetype_is_excluded(["registry:*"], "registry:source")
        assert not usetype_is_excluded(["registry:*"], "registrar")

    def test_exact_entries_stay_exact(self):
        assert usetype_is_excluded(["summary"], "summary")
        assert not usetype_is_excluded(["summary"], "summary:x")

    def test_question_mark_is_one_character(self):
        assert usetype_is_excluded(["ab?"], "abc")
        assert not usetype_is_excluded(["ab?"], "ab")
        assert not usetype_is_excluded(["ab?"], "abcd")

    @pytest.mark.parametrize(
        "pattern,hit,miss",
        [
            ("x_y:*", "x_y:one", "xzy:one"),
            ("p%:*", "p%:one", "pq:one"),
            ("b\\s:*", "b\\s:one", "bxs:one"),
            ("a[bc]*", "a[bc]x", "abx"),
        ],
    )
    def test_only_star_and_question_mark_are_wildcards(self, pattern, hit, miss):
        assert usetype_is_excluded([pattern], hit)
        assert not usetype_is_excluded([pattern], miss)

    def test_null_is_never_excluded(self):
        assert not usetype_is_excluded(["*"], None)


@requires_db
class TestEveryMethod:
    def test_the_sql_predicate_agrees_with_the_matcher(self, seeded, db_session):
        kept = set(
            db_session.execute(
                select(Document.usetype).where(usetype_not_excluded(PATTERNS))
            ).scalars()
        )
        for value in set(FIXTURE.values()) - {None}:
            assert (value in kept) == (not usetype_is_excluded(PATTERNS, value)), value

    def test_vector(self, seeded, db_session):
        results = SearchRepository(db_session).vector_search(base._query_vector(), limit=50)
        assert base._names(seeded, results) == KEPT

    def test_fulltext(self, seeded, db_session):
        results = SearchRepository(db_session).fulltext_search(base.BEACON, limit=50)
        assert base._names(seeded, results) == KEPT

    def test_maxsim(self, db_session, monkeypatch):
        monkeypatch.setattr(base, "FIXTURE", FIXTURE)
        monkeypatch.setattr(get_settings(), "search_exclude_usetypes", list(PATTERNS))
        stub = base._StubEmbeddingService()
        with patch("jmfts_core.repositories.document.get_embedder", return_value=stub):
            ids = base._seed(db_session)
            for doc_id in ids.values():
                DocumentRepository(db_session).embed_document(doc_id, with_tokens=True)
        db_session.flush()
        with patch("jmfts_core.repositories.search.get_embedding_service", return_value=stub):
            results = SearchRepository(db_session).maxsim_search(base.BEACON, limit=50)
        assert base._names(ids, results) == KEPT

    def test_bm25_never_indexes_a_held_out_usetype(self, seeded, db_session):
        repo = SearchRepository(db_session)
        for doc_id in seeded.values():
            repo.index_document(doc_id, "exclusion_globs")
        results = repo.bm25_search(base.BEACON, index_name="exclusion_globs", limit=50)
        assert base._names(seeded, results) == KEPT

    def test_bm25_rebuild_honours_the_same_list(self, seeded, db_session):
        repo = SearchRepository(db_session)
        repo.create_index("exclusion_globs_rebuild")
        for doc_id in seeded.values():
            repo.add_root_to_index("exclusion_globs_rebuild", doc_id)
        db_session.flush()  # the members are only add()ed; the rebuild reads them by query
        refreshed = repo.refresh_index("exclusion_globs_rebuild")
        assert refreshed["indexed"] == len(KEPT), refreshed
        results = repo.bm25_search(base.BEACON, index_name="exclusion_globs_rebuild", limit=50)
        assert base._names(seeded, results) == KEPT

    def test_bm25_runtime_exclude_types_takes_a_glob(self, seeded, db_session, monkeypatch):
        # Indexing holds nothing out, so what removes the namespace is the request's list.
        monkeypatch.setattr(get_settings(), "bm25_exclude_usetypes", [])
        repo = SearchRepository(db_session)
        for doc_id in seeded.values():
            repo.index_document(doc_id, "exclusion_globs_runtime")
        # `exclude_types=[]` because the fixture's search list names the namespace and BM25
        # applies that list at query time since #57.
        everything = repo.bm25_search(
            base.BEACON, index_name="exclusion_globs_runtime", limit=50, exclude_types=[]
        )
        assert base._names(seeded, everything) == set(FIXTURE)
        narrowed = repo.bm25_search(
            base.BEACON,
            index_name="exclusion_globs_runtime",
            limit=50,
            exclude_types=["registry:*"],
        )
        assert base._names(seeded, narrowed) == set(FIXTURE) - {"topic", "source"}

    def test_hybrid(self, seeded, db_session):
        repo = SearchRepository(db_session)
        for doc_id in seeded.values():
            repo.index_document(doc_id, "exclusion_globs_hybrid")
        with patch(
            "jmfts_core.repositories.search.get_embedding_service", return_value=_QueryStub()
        ):
            results = repo.hybrid_search(
                base.BEACON,
                limit=50,
                methods=["vector", "bm25"],
                index_name="exclusion_globs_hybrid",
            )
        assert base._names(seeded, results) == KEPT

    def test_an_explicit_positive_filter_still_overrides_the_list(self, seeded, db_session):
        results = SearchRepository(db_session).vector_search(
            base._query_vector(), limit=50, usetype="registry:*"
        )
        assert base._names(seeded, results) == {"topic", "source"}
