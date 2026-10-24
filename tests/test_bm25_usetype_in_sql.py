"""BM25 applies the usetype predicates in the scored statement, not after ``LIMIT`` (#57).

``bm25_search`` used to fetch ``limit * 2`` rows and drop the positive ``usetype`` filter and
``exclude_types`` in Python, so a query whose top hits were all of a filtered usetype came
back short. It also never applied ``JMFTS_SEARCH_EXCLUDE_USETYPES`` at query time while
``SearchService`` reported it under ``applied.exclude_types``.

The recipe this pins (#52, #57): hold a corpus out of default search with a
``search_exclude_usetypes`` glob and keep it retrievable from its own named index, which
``bm25_exclude_usetypes`` must therefore NOT name.
"""

from unittest.mock import patch

import pytest

from jmfts_core.config import Settings, get_settings
from jmfts_core.models.document import Document
from jmfts_core.repositories.search import SearchRepository

from tests import test_usetype_exclusion_globs as globs
from tests import test_usetype_filter as base
from tests.conftest import requires_db

INDEX = "bm25_usetype_sql"


def _doc(session, usetype, text):
    doc = Document(title=text[:20], content=text, usetype=usetype, settled="settled")
    session.add(doc)
    session.flush()
    return doc


def _seed_skewed(session, repo):
    """Six `registry:topic` docs that outscore four `note` docs on every query."""
    repo.create_index(INDEX)
    strong = [_doc(session, "registry:topic", "zephyr " * 8 + f"topic{i}") for i in range(6)]
    weak = [_doc(session, "note", f"zephyr plain filler words here note{i}") for i in range(4)]
    for doc in strong + weak:
        assert repo.index_document(doc.id, INDEX)
    session.flush()
    return {d.id for d in strong}, {d.id for d in weak}


@requires_db
class TestThePageStaysFull:
    def test_search_exclude_does_not_shorten_the_page(self, db_session, monkeypatch):
        monkeypatch.setattr(get_settings(), "bm25_exclude_usetypes", [])
        monkeypatch.setattr(get_settings(), "search_exclude_usetypes", ["registry:*"])
        repo = SearchRepository(db_session)
        strong, weak = _seed_skewed(db_session, repo)

        # limit * 2 == 6 rows fetched, all six of them `registry:topic`: the old post-filter
        # returned nothing.
        results = repo.bm25_search("zephyr", index_name=INDEX, limit=3)

        assert len(results) == 3
        assert {r.document.id for r in results} <= weak

    def test_request_exclude_types_does_not_shorten_the_page(self, db_session, monkeypatch):
        monkeypatch.setattr(get_settings(), "bm25_exclude_usetypes", [])
        monkeypatch.setattr(get_settings(), "search_exclude_usetypes", [])
        repo = SearchRepository(db_session)
        strong, weak = _seed_skewed(db_session, repo)

        results = repo.bm25_search(
            "zephyr", index_name=INDEX, limit=3, exclude_types=["registry:*"]
        )

        assert len(results) == 3
        assert {r.document.id for r in results} <= weak

    def test_positive_filter_does_not_shorten_the_page(self, db_session, monkeypatch):
        monkeypatch.setattr(get_settings(), "bm25_exclude_usetypes", [])
        monkeypatch.setattr(get_settings(), "search_exclude_usetypes", [])
        repo = SearchRepository(db_session)
        strong, weak = _seed_skewed(db_session, repo)

        results = repo.bm25_search("zephyr", index_name=INDEX, limit=3, usetype="note")

        assert len(results) == 3
        assert {r.document.id for r in results} <= weak

    def test_a_positive_filter_overrides_the_setting(self, db_session, monkeypatch):
        monkeypatch.setattr(get_settings(), "bm25_exclude_usetypes", [])
        monkeypatch.setattr(get_settings(), "search_exclude_usetypes", ["registry:*"])
        repo = SearchRepository(db_session)
        strong, _ = _seed_skewed(db_session, repo)

        results = repo.bm25_search("zephyr", index_name=INDEX, limit=3, usetype="registry:*")

        assert len(results) == 3
        assert {r.document.id for r in results} <= strong

    def test_the_container_pass_obeys_the_same_predicates(self, db_session, monkeypatch):
        from tests.test_container_bm25 import INDEX as CONTAINER_INDEX
        from tests.test_container_bm25 import _tree

        monkeypatch.setattr(get_settings(), "search_exclude_usetypes", [])
        repo, container, _ = _tree(db_session)

        def ids(**kw):
            found = repo.bm25_search("alpha", index_name=CONTAINER_INDEX, **kw)
            return {r.document.id for r in found}

        assert container.id in ids()
        assert container.id not in ids(exclude_types=["section"])
        assert container.id in ids(exclude_types=["sect*x"])
        assert container.id not in ids(usetype="chunk")
        assert container.id in ids(usetype="section")


@requires_db
def test_a_container_past_limit_by_term_hits_but_top_by_score_still_makes_the_page(
    db_session, monkeypatch
):
    """The candidate pass orders by distinct terms matched, not BM25; it must look past `limit`."""
    from tests.test_container_bm25 import _node

    monkeypatch.setattr(get_settings(), "search_exclude_usetypes", [])
    repo = SearchRepository(db_session)
    repo.create_index("container_depth")
    filler = " ".join(["lorem"] * 100)
    leaves = []
    # Two containers matching all three terms, weakly (long leaves): hits = 3.
    for i in range(2):
        weak = _node(db_session, usetype="section", title=f"weak{i}")
        leaves.append(
            _node(db_session, parent=weak.id, position=0, content=f"alpha beta gamma {filler}")
        )
    # One matching two terms, strongly: third by hits, first by score.
    strong = _node(db_session, usetype="section", title="strong")
    leaves.append(_node(db_session, parent=strong.id, position=0, content="alpha alpha alpha beta"))
    for i in range(10):
        leaves.append(_node(db_session, content=f"epsilon zeta unrelated{i}"))
    for leaf in leaves:
        assert repo.index_document(leaf.id, "container_depth")
    db_session.flush()

    results = repo.bm25_search("alpha beta gamma", index_name="container_depth", limit=2)

    assert len(results) == 2
    assert strong.id in {r.document.id for r in results}


@requires_db
class TestTheNamedIndexRecipe:
    """`registry:topic` is held out of default search and found by asking for it."""

    @pytest.fixture
    def seeded(self, db_session, monkeypatch):
        monkeypatch.setattr(base, "FIXTURE", globs.FIXTURE)
        monkeypatch.setattr(get_settings(), "search_exclude_usetypes", ["registry:*"])
        monkeypatch.setattr(get_settings(), "bm25_exclude_usetypes", [])
        ids = base._seed(db_session)
        repo = SearchRepository(db_session)
        for doc_id in ids.values():
            assert repo.index_document(doc_id, "registry_recipe")
        return repo, ids

    def test_it_is_indexed_but_not_in_default_results(self, seeded):
        repo, ids = seeded
        found = repo.bm25_search(base.BEACON, index_name="registry_recipe", limit=50)
        names = base._names(ids, found)
        assert "topic" not in names and "source" not in names
        assert {"registrar", "loose"} <= names

    def test_asking_for_it_returns_it(self, seeded):
        repo, ids = seeded
        results = repo.bm25_search(
            base.BEACON, index_name="registry_recipe", limit=50, usetype="registry:*"
        )
        assert base._names(ids, results) == {"topic", "source"}

    def test_hybrid_agrees(self, seeded):
        repo, ids = seeded

        def hybrid(**kw):
            with patch(
                "jmfts_core.repositories.search.get_embedding_service",
                return_value=globs._QueryStub(),
            ):
                return base._names(
                    ids,
                    repo.hybrid_search(
                        base.BEACON,
                        limit=50,
                        methods=["vector", "bm25"],
                        index_name="registry_recipe",
                        **kw,
                    ),
                )

        assert not hybrid() & {"topic", "source"}
        assert hybrid(usetype="registry:*") == {"topic", "source"}


def test_default_settings_make_the_query_time_exclusion_a_no_op():
    """For LEAVES, search_exclude is a subset of bm25_exclude, so nothing the index holds is dropped.

    Containers are the exception: they have no postings, so ``bm25_exclude_usetypes`` never
    applied to them, and a contentless container whose usetype is in the default
    ``search_exclude_usetypes`` is now dropped from default BM25 (as vector search already did).
    """
    fields = Settings.model_fields
    search = set(fields["search_exclude_usetypes"].default)
    bm25 = set(fields["bm25_exclude_usetypes"].default)
    assert search <= bm25
