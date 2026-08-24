"""``find_path`` must not walk through a literal — SPRINT_0_3_0.md 13.1.

``triples.object_id`` became nullable in 0.3.0. ``find_path`` was written when it was
``NOT NULL`` and computes the far end of an edge as

    next_id = triple.object_id if triple.subject_id == current_id else triple.subject_id

so a literal fact yields ``next_id = None``. That gets queued, and the next round calls
``query_triples(entity_id=None, ...)`` — an unscoped query returning the first 200 triples
in the store, every one of which is then treated as adjacent to the literal's subject.

The result is a path made of edges that do not connect, served by ``GET /triples/path``.

Tier 2: Integration tests — real DB (savepoint rollback).
"""

import pytest
from sqlalchemy import text as sa_text

try:
    from jmfts_core.database import get_engine
    from jmfts_core.repositories.document import DocumentRepository
    from jmfts_core.repositories.triple import TripleRepository
    from jmfts_core.services.triple_service import TripleService

    _engine = get_engine()
    with _engine.connect() as _conn:
        _conn.execute(sa_text("SELECT 1"))
    _DB_AVAILABLE = True
except Exception:
    _DB_AVAILABLE = False

requires_db = pytest.mark.skipif(not _DB_AVAILABLE, reason="Database not available")


def _disconnected_store(session):
    """Two entities with no edge between them, and one literal fact on the first.

    ``Acme`` has exactly one fact and it is a literal. ``NYC`` has one unrelated fact to
    a third entity. There is no resource edge from ``Acme`` to anything, so there is no
    path from ``Acme`` to ``NYC`` at any depth.
    """
    docs = DocumentRepository(session)
    triples = TripleRepository(session)

    acme = docs.create(title="Acme", content="Acme entity node.", usetype="entity")
    nyc = docs.create(title="NYC", content="NYC entity node.", usetype="entity")
    mars = docs.create(title="Mars", content="Mars entity node.", usetype="entity")
    session.flush()

    founded_in = triples.create_predicate(name="zz_founded_in")
    unrelated = triples.create_predicate(name="zz_unrelated")
    session.flush()

    triples.create_triple(
        subject_id=acme.id,
        predicate_id=founded_in.id,
        object_literal="1999",
        object_datatype="http://www.w3.org/2001/XMLSchema#gYear",
    )
    triples.create_triple(subject_id=nyc.id, predicate_id=unrelated.id, object_id=mars.id)
    session.flush()

    return acme, nyc, mars


@requires_db
class TestFindPathStopsAtLiterals:
    def test_literal_fact_is_not_a_path(self, db_session):
        """A literal is a leaf: it cannot be walked through to reach another entity."""
        acme, nyc, _ = _disconnected_store(db_session)
        repo = TripleRepository(db_session)

        paths = repo.find_path(from_id=acme.id, to_id=nyc.id, max_depth=5)

        assert paths == [], (
            "find_path returned a path between two entities that share no edge; "
            f"steps: {[[(t.id, t.subject_id, t.object_id, t.object_literal) for t in p] for p in paths]}"
        )

    def test_a_literal_never_becomes_a_hop(self, db_session):
        """Every step of every returned path ends at an entity, never at a value."""
        acme, _, mars = _disconnected_store(db_session)
        repo = TripleRepository(db_session)
        docs = DocumentRepository(db_session)
        triples = TripleRepository(db_session)

        # Give Acme a real resource edge as well, so a path DOES exist and we can check
        # what its steps carry rather than only that the walk refused.
        hub = docs.create(title="Hub", content="Hub entity node.", usetype="entity")
        db_session.flush()
        near = triples.create_predicate(name="zz_near")
        db_session.flush()
        triples.create_triple(subject_id=acme.id, predicate_id=near.id, object_id=hub.id)
        triples.create_triple(subject_id=hub.id, predicate_id=near.id, object_id=mars.id)
        db_session.flush()

        paths = repo.find_path(from_id=acme.id, to_id=mars.id, max_depth=5)

        assert paths, "the resource edges Acme -> Hub -> Mars should be a path"
        for path in paths:
            for step in path:
                assert step.object_id is not None
                assert step.object_literal is None

    def test_the_served_verb_answers_no_path(self, db_session):
        """``GET /triples/path`` is where the wrong answer was served; check the verb.

        ``PathStep.object_id`` is a non-null ``int``, so a literal edge reaching a returned
        path is a response-validation error, not a quiet null. Nothing to catch here —
        this asserts the verb both refuses the bogus path AND validates.
        """
        acme, nyc, _ = _disconnected_store(db_session)

        response = TripleService(db_session).find_path(from_id=acme.id, to_id=nyc.id)

        assert response.paths == []
        assert response.total_paths == 0


@requires_db
class TestQueryTriplesScope:
    """A scope that was asked for and came out empty must not widen to unscoped."""

    def test_empty_entity_ids_returns_nothing_not_everything(self, db_session):
        acme, nyc, mars = _disconnected_store(db_session)
        repo = TripleRepository(db_session)

        assert repo.query_triples(entity_id=nyc.id, limit=100), "sanity: NYC has a fact"

        # An empty cluster is what `rdf/serialize` produces when the caller may read no
        # member of it, and what a coreference resolution can produce in general.
        assert repo.query_triples(entity_ids=[], limit=100) == []
        # ...including when a (now-overridden) entity_id is passed beside it.
        assert repo.query_triples(entity_id=nyc.id, entity_ids=[], limit=100) == []

    def test_no_scope_still_means_the_whole_store(self, db_session):
        """Unscoped paging is a real, used mode — GET /triples and the Turtle export."""
        _disconnected_store(db_session)
        repo = TripleRepository(db_session)

        assert len(repo.query_triples(limit=100)) >= 2
