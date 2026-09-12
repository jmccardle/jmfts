"""The derived-edge lifecycle: ``derived_by``, re-derivation, and a score that moves.

``docs/SPRINT_0_5_0.md`` Block D, steps 15, 16 and 17. Three properties, one file, because
they are one story: an edge that a rule produced must say which rule produced it (15), a
re-run of that rule must be able to delete exactly its own output and rebuild it (16), and
an upsert of a WEIGHTED edge must carry the new weight rather than discard it (17).

Step 17 is the defect and this file is its entry condition. ``document_links`` is UNIQUE on
(source_id, target_id, link_type), so ``_upsert_link``'s ``on_conflict_do_nothing`` made a
re-derived edge whose relevance moved from 0.3 to 0.8 silently keep 0.3. The two behaviours
are pinned APART here: the unweighted ``mentions`` edge, whose second assertion carries no
new information, must still not churn its row.
"""

import pytest
from sqlalchemy import select, text

from jmfts_core.fact_extraction import MENTIONS_LINK_TYPE, _upsert_link
from jmfts_core.models.document import DocumentLink
from jmfts_core.repositories.document import (
    DerivedLink,
    DerivedLinkCollisionError,
    DocumentRepository,
)

#: A weighted edge type. Any type the caller passes a score for takes the update path —
#: step 17's rule is "did the caller pass a score", not a registry of scored type names
#: (which Part 3.4 owns), so this name is a stand-in for the reprojection edges to come and
#: carries no meaning of its own.
WEIGHTED_LINK_TYPE = "contains-keyword"


def _doc(session, title):
    return DocumentRepository(session).create(
        title=title, content=f"content for {title}", usetype="raw", auto_embed=False
    )


def _rows(session, source_id):
    return list(
        session.execute(select(DocumentLink).where(DocumentLink.source_id == source_id)).scalars()
    )


class TestUpsertLinkScore:
    """Step 17 — a changed score is an update, an unweighted re-assertion is not."""

    def test_weighted_reupsert_carries_the_new_score(self, db_session):
        """0.3 → 0.8. THIS IS THE DEFECT: ``on_conflict_do_nothing`` kept 0.3."""
        a, b = _doc(db_session, "a"), _doc(db_session, "b")

        _upsert_link(db_session, a.id, b.id, WEIGHTED_LINK_TYPE, score=0.3)
        db_session.flush()
        _upsert_link(db_session, a.id, b.id, WEIGHTED_LINK_TYPE, score=0.8)
        db_session.flush()

        rows = _rows(db_session, a.id)
        assert len(rows) == 1, "the UNIQUE constraint means one edge, not two"
        assert rows[0].score == pytest.approx(0.8)

    def test_weighted_first_write_stores_the_score(self, db_session):
        """The insert leg, so the update leg above cannot pass on a default."""
        a, b = _doc(db_session, "a"), _doc(db_session, "b")

        _upsert_link(db_session, a.id, b.id, WEIGHTED_LINK_TYPE, score=0.3)
        db_session.flush()

        assert _rows(db_session, a.id)[0].score == pytest.approx(0.3)

    def test_unweighted_reupsert_does_not_churn_the_row(self, db_session):
        """``mentions`` carries no new information the second time, so it writes nothing.

        The existing row is given a non-default score first: if the unweighted path ever
        became an update, it would overwrite that with the column default and this would
        fail. That is what pins the two behaviours apart rather than letting one swallow
        the other.
        """
        a, b = _doc(db_session, "a"), _doc(db_session, "b")
        DocumentRepository(db_session).create_link(
            source_id=a.id, target_id=b.id, link_type=MENTIONS_LINK_TYPE, score=0.42
        )
        db_session.flush()
        # Read the row back BEFORE comparing: ``created_at`` is written as a naive
        # ``datetime.utcnow`` and reloads from a TIMESTAMPTZ column as aware, so comparing
        # the in-memory default against the reloaded value would fail on the tzinfo alone
        # and say nothing about churn.
        db_session.expire_all()
        existing = _rows(db_session, a.id)[0]
        before_id, before_created = existing.id, existing.created_at

        _upsert_link(db_session, a.id, b.id, MENTIONS_LINK_TYPE)
        db_session.flush()
        db_session.expire_all()

        rows = _rows(db_session, a.id)
        assert len(rows) == 1
        assert rows[0].id == before_id
        assert rows[0].created_at == before_created
        assert rows[0].score == pytest.approx(0.42), "an unweighted assertion wrote a score"

    def test_unweighted_first_write_takes_the_column_default(self, db_session):
        """No score passed, no score in the INSERT — ``document_links.score`` DEFAULT 1.0."""
        a, b = _doc(db_session, "a"), _doc(db_session, "b")

        _upsert_link(db_session, a.id, b.id, MENTIONS_LINK_TYPE)
        db_session.flush()
        db_session.expire_all()

        assert _rows(db_session, a.id)[0].score == pytest.approx(1.0)


class TestDerivedByColumn:
    """Step 15 — one nullable column and one partial index, mirroring ``triples``."""

    def test_column_is_nullable_and_defaults_to_asserted(self, db_session):
        a, b = _doc(db_session, "a"), _doc(db_session, "b")
        link = DocumentRepository(db_session).create_link(
            source_id=a.id, target_id=b.id, link_type="ref"
        )
        db_session.flush()

        assert link.derived_by is None, "NULL means asserted, as it does on triples"

    def test_partial_index_exists_and_is_predicated_on_not_null(self, db_session):
        """The index holds only the derived rows; the asserted majority is served by
        not being in it. Same shape as ``ix_triples_derived_by``."""
        row = db_session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_links_derived_by'")
        ).scalar_one()

        assert "derived_by" in row
        assert "WHERE (derived_by IS NOT NULL)" in row

    def test_migration_019_is_in_the_ledger(self, db_session):
        """A schema.sql-built database must not report 019 as pending."""
        source = db_session.execute(
            text("SELECT source FROM schema_migrations WHERE name = '019_link_derived_by.sql'")
        ).scalar_one()

        assert source == "schema"


class TestRederiveLinks:
    """Step 16 — delete-then-insert, scoped by rule.

    Nothing in this sprint calls :meth:`DocumentRepository.rederive_links`: no rule writes
    links yet. These tests are its only caller, deliberately, and the docstring on the
    method records that so the dead-code audit does not delete it.
    """

    def test_rederive_replaces_only_this_rule_s_edges(self, db_session):
        repo = DocumentRepository(db_session)
        a, b, c = _doc(db_session, "a"), _doc(db_session, "b"), _doc(db_session, "c")

        repo.rederive_links(
            "keyword:v1",
            [
                DerivedLink(a.id, b.id, WEIGHTED_LINK_TYPE, 0.3),
                DerivedLink(a.id, c.id, WEIGHTED_LINK_TYPE, 0.5),
            ],
        )
        db_session.flush()

        # The re-run drops b entirely and moves c's weight. No diffing, no reconciliation.
        deleted, inserted = repo.rederive_links(
            "keyword:v1", [DerivedLink(a.id, c.id, WEIGHTED_LINK_TYPE, 0.9)]
        )
        db_session.flush()
        db_session.expire_all()

        assert (deleted, inserted) == (2, 1)
        rows = _rows(db_session, a.id)
        assert [(r.target_id, r.score, r.derived_by) for r in rows] == [
            (c.id, pytest.approx(0.9), "keyword:v1")
        ]

    def test_rederive_leaves_asserted_and_other_rules_alone(self, db_session):
        repo = DocumentRepository(db_session)
        a, b, c, d = (_doc(db_session, t) for t in ("a", "b", "c", "d"))
        repo.create_link(source_id=a.id, target_id=b.id, link_type="ref")
        repo.rederive_links("other:v1", [DerivedLink(a.id, c.id, WEIGHTED_LINK_TYPE, 0.2)])
        repo.rederive_links("keyword:v1", [DerivedLink(a.id, d.id, WEIGHTED_LINK_TYPE, 0.4)])
        db_session.flush()

        deleted, inserted = repo.rederive_links("keyword:v1", [])
        db_session.flush()
        db_session.expire_all()

        assert (deleted, inserted) == (1, 0)
        assert {(r.target_id, r.derived_by) for r in _rows(db_session, a.id)} == {
            (b.id, None),
            (c.id, "other:v1"),
        }

    def test_rederive_refuses_to_claim_an_edge_it_does_not_own(self, db_session):
        """A rule whose output collides with an edge another writer owns is a conflict,
        not a no-op: DO NOTHING there would leave the rule's output silently incomplete."""
        repo = DocumentRepository(db_session)
        a, b = _doc(db_session, "a"), _doc(db_session, "b")
        repo.create_link(source_id=a.id, target_id=b.id, link_type=WEIGHTED_LINK_TYPE)
        db_session.flush()

        with pytest.raises(DerivedLinkCollisionError) as excinfo:
            repo.rederive_links("keyword:v1", [DerivedLink(a.id, b.id, WEIGHTED_LINK_TYPE, 0.7)])

        assert excinfo.value.rule == "keyword:v1"
        assert (a.id, b.id, WEIGHTED_LINK_TYPE) in excinfo.value.collisions
