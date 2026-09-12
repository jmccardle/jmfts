"""BM25's leaf scan must not return a node that is still in flight.

``CLAUDE.md``: *"A document carries a ``settled`` column — retrieval indexes are partial on
it, so in-flight nodes are invisible to search until ``settling.py`` walks them complete."*
Three of the four retrieval paths in :mod:`jmfts_core.repositories.search` said so in SQL —
``vector_search`` twice, ``maxsim_search`` once, and BM25's *container* pass — and BM25's
*leaf* scan did not. Its ``doc_scores`` CTE joined ``search_term_postings`` to ``term_idf``
to ``search_index_entries`` and never reached ``documents`` at all unless a subtree, an
``as_of`` cutoff or an access-control root happened to force the join, so there was no
settled predicate and nowhere to put one.

``index_document`` is the reason postings for in-flight nodes exist to be found: it gates on
content and on ``bm25_exclude_usetypes`` and on nothing else, and the ingest pipeline's own
indexing rung runs *before* ``settling.py`` reaches the node. ``refresh_index`` is the
settled-only path; the incremental one is not.

The interesting half of this is :class:`TestTheGateIsNotAPostFilter`. A predicate applied
after ``LIMIT`` would return a short page rather than a wrong one, which is a quieter bug
and just as much a defect — so the gate belongs inside the scored CTE, before the
``GROUP BY``, and these tests fail if it is moved out.

``docs/SPRINT_0_4_0.md`` Part 0: an open defect is a numbered step whose entry condition is
a failing test. This file was that entry condition.
"""

from __future__ import annotations

import pytest

from jmfts_core.models.document import (
    Document,
    SETTLED_FAILED,
    SETTLED_IN_FLIGHT,
    SETTLED_SETTLED,
)
from jmfts_core.repositories.search import SearchRepository

INDEX = "bm25_settled_gate"


def _node(session, *, content, settled=SETTLED_SETTLED, parent=None, position=None, title="n"):
    doc = Document(
        title=title,
        content=content,
        usetype="chunk",
        parent_id=parent,
        position=position,
        settled=settled,
    )
    session.add(doc)
    session.flush()
    return doc


def _indexed(repo, session, **kwargs):
    """A node that carries a posting, whatever its lifecycle state.

    Indexed while it is whatever ``settled`` says, because ``index_document`` does not look
    at the column — which is the whole reason the read side has to.
    """
    doc = _node(session, **kwargs)
    assert repo.index_document(doc.id, INDEX), "the fixture must actually write postings"
    session.flush()
    return doc


@pytest.fixture
def repo(db_session):
    r = SearchRepository(db_session)
    r.create_index(INDEX)
    return r


class TestTheLeafScanGates:
    def test_an_in_flight_leaf_is_not_returned(self, repo, db_session):
        settled = _indexed(repo, db_session, content="alpha beta", title="settled")
        in_flight = _indexed(
            repo, db_session, content="alpha beta", settled=SETTLED_IN_FLIGHT, title="in flight"
        )

        found = {r.document.id for r in repo.bm25_search("alpha", index_name=INDEX, limit=50)}

        assert settled.id in found, "the gate must not swallow the settled corpus with it"
        assert in_flight.id not in found

    def test_a_failed_leaf_is_not_returned(self, repo, db_session):
        """``failed`` is a third state, not a synonym for in-flight. A node whose task died
        permanently is not retrievable either — the predicate is ``= 'settled'``, not
        ``<> 'in_flight'``, and this is the test that tells the two apart."""
        settled = _indexed(repo, db_session, content="gamma delta", title="settled")
        failed = _indexed(
            repo, db_session, content="gamma delta", settled=SETTLED_FAILED, title="failed"
        )

        found = {r.document.id for r in repo.bm25_search("gamma", index_name=INDEX, limit=50)}

        assert settled.id in found
        assert failed.id not in found

    def test_a_node_put_back_in_flight_leaves_the_result_set(self, repo, db_session):
        """Indexed while settled, then corrected. The posting outlives the state change —
        nothing deletes it — so the read side is the only thing that can notice."""
        doc = _indexed(repo, db_session, content="epsilon zeta")
        assert doc.id in {r.document.id for r in repo.bm25_search("epsilon", index_name=INDEX)}

        doc.settled = SETTLED_IN_FLIGHT
        db_session.flush()

        assert doc.id not in {r.document.id for r in repo.bm25_search("epsilon", index_name=INDEX)}

    def test_the_gate_holds_inside_a_requested_subtree(self, repo, db_session):
        """The scoped case already joined ``documents``; the unscoped one did not. Both have
        to gate, and this is the branch that reuses the existing join rather than adding a
        second one."""
        root = _node(db_session, content=None, title="root")
        settled = _indexed(repo, db_session, content="eta theta", parent=root.id, position=0)
        in_flight = _indexed(
            repo,
            db_session,
            content="eta theta",
            parent=root.id,
            position=1,
            settled=SETTLED_IN_FLIGHT,
        )

        found = {
            r.document.id
            for r in repo.bm25_search("eta", index_name=INDEX, parent_id=root.id, limit=50)
        }

        assert settled.id in found
        assert in_flight.id not in found


class TestTheGateIsNotAPostFilter:
    """Dropping in-flight rows after ``LIMIT`` returns a SHORT page, not a wrong one.

    Quieter than returning them and still a defect: a caller asking for ten results gets
    three, with no way to tell a thin corpus from a filtered page. The gate has to prune
    before the ``GROUP BY`` so the top-k is computed over the retrievable corpus.
    """

    def test_a_full_page_of_settled_results_survives_many_better_scoring_in_flight_ones(
        self, repo, db_session
    ):
        # The in-flight nodes repeat the term, so BM25 ranks every one of them above every
        # settled node. A post-filter applied to `limit * 2` scored rows would return
        # nothing at all here.
        for i in range(20):
            _indexed(
                repo,
                db_session,
                content="iota iota iota iota",
                settled=SETTLED_IN_FLIGHT,
                title=f"in flight {i}",
            )
        settled = [
            _indexed(repo, db_session, content=f"iota kappa{i}", title=f"settled {i}")
            for i in range(5)
        ]

        results = repo.bm25_search("iota", index_name=INDEX, limit=5)

        assert len(results) == 5, "a full page, not what survived a filter"
        assert {r.document.id for r in results} == {d.id for d in settled}


class TestTheContainerPassIsADifferentFilter:
    """``_container_candidates`` gates ``a.settled`` — the CONTAINER row. The new gate is on
    ``d`` — the descendant whose posting matched. Neither subsumes the other, and this holds
    the distinction so a later reader does not delete one as redundant.
    """

    def test_a_settled_container_over_an_in_flight_leaf_is_still_held_out(self, repo, db_session):
        """Not by the container gate — the container is settled — but because settling is
        bottom-up (``schema.sql``: *"its own work is done AND every child is settled"*), so
        this tree cannot occur in a well-formed corpus. Constructed by hand to pin the
        behaviour anyway: the container is returned on its child's text, and the in-flight
        child itself is not.
        """
        container = _node(db_session, content=None, title="the section")
        leaf = _indexed(
            repo,
            db_session,
            content="lambda mu",
            parent=container.id,
            position=0,
            settled=SETTLED_IN_FLIGHT,
        )

        found = {r.document.id for r in repo.bm25_search("lambda", index_name=INDEX, limit=50)}

        assert leaf.id not in found, "the leaf gate"
        assert container.id in found, "the container pass scores an unsettled frontier"

    def test_an_in_flight_container_over_a_settled_leaf_is_held_out_by_the_container_gate(
        self, repo, db_session
    ):
        """The converse, and the case the ``a.settled`` gate is actually for. The leaf gate
        cannot reach it: the container has no posting of its own."""
        container = _node(db_session, content=None, title="the section")
        container.settled = SETTLED_IN_FLIGHT
        leaf = _indexed(repo, db_session, content="nu xi", parent=container.id, position=0)
        db_session.flush()

        found = {r.document.id for r in repo.bm25_search("nu", index_name=INDEX, limit=50)}

        assert leaf.id in found
        assert container.id not in found


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
