"""``document_evidence`` — the store, the repository over it, and the migration into it.

``SPRINT_JOBS.md`` Phase 2b. :mod:`tests.test_evidence_registry` checks what the registry
SAYS; this checks what the table DOES, and it has one job the other cannot do: hold
``migrations/015_evidence_rows.sql`` against the registry it was generated from.

Part 14: "No phase adds a second list of something the code already knows. Derive it, or
state a rule and audit the rule against what actually ran." SQL cannot import
:mod:`jmfts_core.evidence`, so the migration writes the key list out and this parses it back
and compares. A drift fails here with the corrected block printed, which is the difference
between a second list and a copy with a ratchet on it.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from jmfts_core import evidence as ev
from jmfts_core.models.document_evidence import STATE_STALE, STATE_WRITTEN, DocumentEvidence
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository, evidence_of, evidence_value
from jmfts_core.sql import migration_sql, schema_sql

#: The block migration 015 fences off for this test to read. A marker rather than a line
#: number, so reformatting the file cannot silently make the audit read the wrong thing.
_FENCE = re.compile(
    r"-- BEGIN EVIDENCE KEYS.*?VALUES\n(.*?);\n-- END EVIDENCE KEYS",
    re.DOTALL,
)
_PAIR = re.compile(r"\('([^']+)', '([^']+)'\)")


@pytest.fixture
def sample_document(db_session):
    """One plain node to hang evidence on. No ingest, so nothing else writes to it."""
    doc = DocumentRepository(db_session).create(
        title="sample", content="a short body", usetype="chunk", auto_embed=False
    )
    db_session.flush()
    return doc


def _migration_pairs() -> dict[str, str]:
    """``{old structured_content key: evidence name}``, as the migration states it."""
    body = _FENCE.search(migration_sql("015_evidence_rows.sql"))
    assert body is not None, "the fenced key list is gone from 015_evidence_rows.sql"
    return dict(_PAIR.findall(body.group(1)))


class TestTheMigrationMatchesTheRegistry:
    def test_the_key_list_is_exactly_the_registry_rows(self):
        stated = set(_migration_pairs().values())
        expected = ev.rows()
        if stated != expected:
            corrected = "\n".join(
                f"    ('{ev.REGISTRY[name].store.row}', '{name}'),"
                for name in sorted(expected)
                if name in ev.REGISTRY
            )
            pytest.fail(
                "015_evidence_rows.sql does not name the registry's rows.\n"
                f"  missing from the migration: {sorted(expected - stated)}\n"
                f"  named there and not registered: {sorted(stated - expected)}\n"
                f"paste this between the fence markers:\n{corrected}"
            )

    def test_the_two_renamed_keys_are_the_two_the_registry_renames(self):
        """2.5's finding 5, as the migration has to express it.

        A row is named for what it asserts, and twice that is not what the column key was.
        A migration that took the column key for the name would leave `citation`'s writer
        and its reader looking at two different rows.
        """
        renamed = {old: new for old, new in _migration_pairs().items() if old != new}
        assert renamed == {
            "anchor": "source_anchor",
            "anchor_unresolved": "source_anchor.unresolved",
        }

    def test_schema_and_migration_agree_about_the_table(self):
        """`schema.sql` is the complete current schema and 015 is how an old database gets
        there. A column in one and not the other is a fresh install that behaves
        differently from a migrated one."""
        schema = schema_sql()
        for fragment in ("CREATE TABLE document_evidence", "ck_document_evidence_state"):
            assert fragment in schema, f"{fragment!r} is missing from schema.sql"
        for index in (
            "idx_document_evidence_name",
            "idx_document_evidence_value",
            "idx_document_evidence_stale",
        ):
            assert index in schema, f"{index} is missing from schema.sql"


class TestTheRepositoryRefusesWhatTheRegistryRefuses:
    def test_an_unregistered_name_is_a_typo_and_not_a_row(self, db_session):
        repo = EvidenceRepository(db_session)
        with pytest.raises(KeyError):
            repo.write(1, "mached", {"format": "pdf"})

    def test_a_name_stored_somewhere_else_says_where(self, db_session):
        """`text` is a column and `blob` is a large object. Answering ABSENT for either
        would send a caller looking for a value that is right there."""
        repo = EvidenceRepository(db_session)
        for name in ("text", "blob", "child_count", "embedding.tokens"):
            with pytest.raises(ValueError, match="not in document_evidence"):
                repo.read(1, name)

    def test_a_leaf_is_read_and_not_written(self, db_session):
        """An atom produces a block (Part 2). Writing a leaf would be a writer with no
        atom behind it."""
        repo = EvidenceRepository(db_session)
        with pytest.raises(ValueError, match="leaf inside"):
            repo.write(1, "matched.format", "pdf")

    def test_a_bad_value_is_refused_at_the_write(self, db_session, sample_document):
        repo = EvidenceRepository(db_session)
        with pytest.raises(ev.EvidenceTypeError):
            repo.write(sample_document.id, "chunk_index", "third")


class TestRoundTrip:
    def test_a_row_is_written_and_read_back_by_name(self, db_session, sample_document):
        repo = EvidenceRepository(db_session)
        repo.write(sample_document.id, "matched", {"format": "pdf", "patterns": {"pages": 3}})
        assert repo.read(sample_document.id, "matched.format") == "pdf"
        assert evidence_of(db_session, sample_document.id)["matched"]["format"] == "pdf"

    def test_a_null_is_a_result_and_an_absent_row_is_not(self, db_session, sample_document):
        """3.2, at the store. A written null and a missing row are the two states the
        JSONB column could not tell apart once staling meant deleting the block."""
        repo = EvidenceRepository(db_session)
        assert repo.read(sample_document.id, "source_span") is ev.ABSENT
        repo.write(sample_document.id, "source_span", None)
        assert repo.read(sample_document.id, "source_span") is None
        assert "source_span" in evidence_of(db_session, sample_document.id)
        repo.delete(sample_document.id, "source_span")
        assert repo.read(sample_document.id, "source_span") is ev.ABSENT

    def test_a_null_is_refused_where_no_null_result_exists(self, db_session, sample_document):
        with pytest.raises(ev.EvidenceTypeError):
            EvidenceRepository(db_session).write(sample_document.id, "chunk_index", None)

    def test_a_written_null_is_sql_null_and_not_jsonb_null(self, db_session, sample_document):
        """One representation, and this test exists because there were briefly two.

        ``value`` is JSONB, so a null can be stored two ways: SQL NULL, or the JSONB scalar
        ``'null'``. Both come back through SQLAlchemy as Python ``None``, which is exactly
        why the difference would go unnoticed at every read — and then ``WHERE value IS
        NULL``, the query Part 9 wants for "which names produced nothing", would answer for
        rows the repository wrote and not for rows migration 015 moved. The migration's
        ``NULLIF(..., 'null'::jsonb)`` is what makes the two agree, and this pins the side
        it agrees on.
        """
        EvidenceRepository(db_session).write(sample_document.id, "source_span", None)
        db_session.flush()
        found = db_session.execute(
            select(DocumentEvidence.value.is_(None)).where(
                DocumentEvidence.document_id == sample_document.id,
                DocumentEvidence.name == "source_span",
            )
        ).scalar_one()
        assert found is True

    def test_a_rewrite_replaces_the_row_and_clears_its_state(self, db_session, sample_document):
        repo = EvidenceRepository(db_session)
        repo.write(sample_document.id, "structure", {"primary_rung": "declared"})
        repo.stale([sample_document.id], ["structure"])
        assert self._state(db_session, sample_document.id, "structure") == STATE_STALE
        repo.write(sample_document.id, "structure", {"primary_rung": "inferred"})
        assert self._state(db_session, sample_document.id, "structure") == STATE_WRITTEN
        assert repo.read(sample_document.id, "structure") == {"primary_rung": "inferred"}

    def test_read_many_answers_for_every_id_asked_about(self, db_session, sample_document):
        repo = EvidenceRepository(db_session)
        repo.write(sample_document.id, "rung", "declared")
        found = repo.read_many([sample_document.id, sample_document.id + 10_000])
        assert found[sample_document.id] == {"rung": "declared"}
        assert found[sample_document.id + 10_000] == {}

    @staticmethod
    def _state(session, document_id: int, name: str) -> str:
        row = session.get(DocumentEvidence, (document_id, name))
        return row.state


class TestConcurrentWritesToDifferentNames:
    def test_two_names_on_one_node_both_survive(self, db_session, sample_document):
        """13.1'S FIRST FACT, WHICH IS THE WHOLE REASON THIS TABLE EXISTS.

        Against the ``structured_content`` column this was a read-modify-write per name, so
        two writers touching one node lost one of the writes with nothing raised.
        ``scripts/evidence_bench.py`` shows it against both shapes: the column wrote two
        names and one survived.

        One session here rather than two, because that is what the appliance actually
        does — two handlers on one node run in one worker thread each with its own session,
        and what made the old form lose a write was the whole-column assignment, not the
        transaction boundary. Two rows are two statements and neither reads the other.
        """
        repo = EvidenceRepository(db_session)
        repo.write(sample_document.id, "matched", {"format": "pdf"})
        repo.write(sample_document.id, "extraction", {"source": "pdf_text_layer"})
        found = evidence_of(db_session, sample_document.id)
        assert sorted(found) == ["extraction", "matched"]


class TestTheAttemptLog:
    def test_appending_does_not_read_first(self, db_session, sample_document):
        """Spec 5.6's durable append-only log, as one statement.

        ``value || entries`` against the row, so nothing between a read and a write can
        lose a record.
        """
        repo = EvidenceRepository(db_session)
        repo.append(sample_document.id, "attempts", [{"task": "probe", "status": "pending"}])
        repo.append(sample_document.id, "attempts", [{"task": "probe", "status": "completed"}])
        log = repo.read(sample_document.id, "attempts")
        assert [entry["status"] for entry in log] == ["pending", "completed"]

    def test_appending_nothing_writes_nothing(self, db_session, sample_document):
        repo = EvidenceRepository(db_session)
        repo.append(sample_document.id, "attempts", [])
        assert repo.read(sample_document.id, "attempts") is ev.ABSENT

    def test_append_refuses_a_name_that_is_not_a_list(self, db_session, sample_document):
        with pytest.raises(ValueError, match="append is for a list"):
            EvidenceRepository(db_session).append(sample_document.id, "matched", [{"a": 1}])

    def test_the_repository_reads_the_log_from_the_row(self, db_session, sample_document):
        """`DocumentRepository.attempt_log` is the one place that knows where it lives, so
        the queue and the ingest service do not have to."""
        EvidenceRepository(db_session).append(
            sample_document.id, "attempts", [{"task": "probe", "status": "completed"}]
        )
        log = DocumentRepository(db_session).attempt_log(sample_document)
        assert [entry["task"] for entry in log] == ["probe"]


class TestDeletingANodeTakesItsEvidence:
    def test_the_cascade_removes_the_rows(self, db_session):
        repo = DocumentRepository(db_session)
        doc = repo.create(title="temporary", content=None, auto_embed=False)
        EvidenceRepository(db_session).write(doc.id, "rung", "declared")
        db_session.flush()
        doc_id = doc.id
        repo.delete(doc_id)
        db_session.flush()
        assert db_session.get(DocumentEvidence, (doc_id, "rung")) is None


class TestCreateWritesEvidence:
    def test_a_node_born_knowing_something_gets_rows_not_column_keys(self, db_session):
        doc = DocumentRepository(db_session).create(
            title="chunk",
            content="text",
            usetype="chunk",
            structured_content={"tag": "mine"},
            evidence={"rung": "declared", "chunk_index": 0},
            auto_embed=False,
        )
        db_session.flush()
        assert doc.structured_content == {"tag": "mine"}
        assert evidence_of(db_session, doc.id) == {"rung": "declared", "chunk_index": 0}
        assert evidence_value(db_session, doc.id, "rung") == "declared"

    def test_a_misspelled_name_fails_at_the_create(self, db_session):
        with pytest.raises(KeyError):
            DocumentRepository(db_session).create(
                title="chunk", content="text", evidence={"rungg": "declared"}, auto_embed=False
            )


class TestStaling:
    def test_staling_is_one_statement_over_a_resolved_id_list(self, db_session):
        """13.1's second fact. Part 9's rebinding marks a subtree's evidence stale, and
        against the column that had to DELETE the block — which loses 3.2's distinction
        between "stale" and "never attempted" every time."""
        repo = DocumentRepository(db_session)
        evidence = EvidenceRepository(db_session)
        ids = []
        for index in range(3):
            doc = repo.create(title=f"n{index}", content=None, auto_embed=False)
            evidence.write(doc.id, "structure", {"primary_rung": "declared"})
            ids.append(doc.id)
        db_session.flush()
        assert evidence.stale(ids, ["structure"]) == 3
        for doc_id in ids:
            row = db_session.get(DocumentEvidence, (doc_id, "structure"))
            assert row.state == STATE_STALE
            # THE VALUE IS STILL THERE, which is the point. A stale row says "this was
            # derived and its inputs have moved", and it still holds what was derived.
            assert row.value == {"primary_rung": "declared"}

    def test_staling_nothing_is_not_an_error(self, db_session):
        assert EvidenceRepository(db_session).stale([], ["structure"]) == 0


class TestTheColumnIsTheCallers:
    def test_a_whole_object_update_cannot_reach_a_row(self, db_session):
        """13.3's consequence, at the one verb that used to need a gate against it."""
        repo = DocumentRepository(db_session)
        doc = repo.create(title="doc", content="text", auto_embed=False)
        EvidenceRepository(db_session).write(doc.id, "matched", {"format": "pdf"})
        db_session.flush()
        repo.update(doc.id, structured_content={"matched": "not mine to set"}, re_embed=False)
        db_session.flush()
        assert doc.structured_content == {"matched": "not mine to set"}
        assert evidence_value(db_session, doc.id, "matched.format") == "pdf"

    def test_the_document_model_dict_carries_only_the_column(self, db_session):
        doc = DocumentRepository(db_session).create(
            title="doc", content="text", structured_content={"importance": 0.5}, auto_embed=False
        )
        EvidenceRepository(db_session).write(doc.id, "rung", "declared")
        db_session.flush()
        assert doc.to_dict()["structured_content"] == {"importance": 0.5}
