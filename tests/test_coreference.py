"""The non-destructive `same_as` coreference leg.

An "entity" here is a ``usetype="entity"`` document; the same real-world thing accretes
several such nodes across ingest runs (the write-time resolver's cache is per-run). This
leg lets a query on one alias see the facts recorded under all of them, WITHOUT a
destructive merge: coreference is a plain ``same_as`` ``DocumentLink``, asserted with the
existing ``create_link`` verb and read back symmetrically.

Two layers:
1. ``resolve_coreferent_ids`` — the cluster resolver (symmetric + transitive over
   ``same_as`` edges, cycle-guarded).
2. ``TripleService.query_triples(coreferent=True)`` — opt-in fact union across the
   cluster; default off is byte-identical to a single-entity query (canary).
"""

from jmfts_core.graph_analysis import SAME_AS_LINK_TYPE, resolve_coreferent_ids
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository
from jmfts_core.services.triple_service import TripleService


def _entity(docs, name):
    return docs.create(title=name, content=name, usetype="entity", auto_embed=False)


class TestResolveCoreferentCluster:
    def test_single_directional_edge_is_read_symmetrically(self, db_session):
        docs = DocumentRepository(db_session)
        a = _entity(docs, "John")
        b = _entity(docs, "John McCardle")
        db_session.flush()
        # One directional edge a -> b only.
        docs.create_link(a.id, b.id, link_type=SAME_AS_LINK_TYPE)
        db_session.flush()

        # Resolving from either endpoint yields the same cluster — same_as is symmetric.
        assert set(resolve_coreferent_ids(db_session, a.id)) == {a.id, b.id}
        assert set(resolve_coreferent_ids(db_session, b.id)) == {a.id, b.id}

    def test_cluster_is_transitive(self, db_session):
        docs = DocumentRepository(db_session)
        a, b, c = _entity(docs, "J"), _entity(docs, "John"), _entity(docs, "J. McCardle")
        db_session.flush()
        docs.create_link(a.id, b.id, link_type=SAME_AS_LINK_TYPE)
        docs.create_link(b.id, c.id, link_type=SAME_AS_LINK_TYPE)
        db_session.flush()
        assert set(resolve_coreferent_ids(db_session, a.id)) == {a.id, b.id, c.id}

    def test_entity_is_always_first_and_isolated_entity_is_singleton(self, db_session):
        docs = DocumentRepository(db_session)
        lonely = _entity(docs, "Nobody")
        db_session.flush()
        assert resolve_coreferent_ids(db_session, lonely.id) == [lonely.id]

    def test_only_same_as_edges_form_clusters(self, db_session):
        """A different link_type (e.g. a RAPTOR bridge) does not coreference."""
        docs = DocumentRepository(db_session)
        a, b = _entity(docs, "John"), _entity(docs, "Unrelated")
        db_session.flush()
        docs.create_link(a.id, b.id, link_type="bridge")
        db_session.flush()
        assert resolve_coreferent_ids(db_session, a.id) == [a.id]


class TestCoreferentFactUnion:
    def _setup_split_entity(self, db_session):
        """Same person recorded as two entities, each carrying a distinct fact."""
        docs = DocumentRepository(db_session)
        triples = TripleRepository(db_session)
        john_a = _entity(docs, "John")
        john_b = _entity(docs, "John McCardle")
        nyc = _entity(docs, "NYC")
        acme = _entity(docs, "Acme")
        db_session.flush()
        lives_in = triples.create_predicate(name="lives_in")
        works_at = triples.create_predicate(name="works_at")
        db_session.flush()
        # Fact recorded under alias A, the other under alias B.
        triples.create_triple(subject_id=john_a.id, predicate_id=lives_in.id, object_id=nyc.id)
        triples.create_triple(subject_id=john_b.id, predicate_id=works_at.id, object_id=acme.id)
        db_session.flush()
        return docs, john_a, john_b

    def test_default_query_sees_only_the_queried_aliass_facts(self, db_session):
        """Canary: without coreferent=True, aliases are separate — the gap this closes."""
        docs, john_a, john_b = self._setup_split_entity(db_session)
        service = TripleService(db_session)
        a_facts = service.query_triples(entity_id=john_a.id)
        assert len(a_facts) == 1
        assert a_facts[0].predicate.name == "lives_in"

    def test_coreferent_query_unions_facts_across_the_cluster(self, db_session):
        docs, john_a, john_b = self._setup_split_entity(db_session)
        docs.create_link(john_a.id, john_b.id, link_type=SAME_AS_LINK_TYPE)
        db_session.flush()

        service = TripleService(db_session)
        preds = {
            f.predicate.name for f in service.query_triples(entity_id=john_a.id, coreferent=True)
        }
        assert preds == {"lives_in", "works_at"}  # both aliases' facts, one query
        # Symmetric: querying from the other alias yields the same union.
        preds_b = {
            f.predicate.name for f in service.query_triples(entity_id=john_b.id, coreferent=True)
        }
        assert preds_b == {"lives_in", "works_at"}

    def test_coreferent_flag_is_inert_without_a_same_as_edge(self, db_session):
        """No cluster ⇒ coreferent=True degrades to the single-entity result."""
        docs, john_a, john_b = self._setup_split_entity(db_session)
        service = TripleService(db_session)
        with_flag = service.query_triples(entity_id=john_a.id, coreferent=True)
        assert len(with_flag) == 1
