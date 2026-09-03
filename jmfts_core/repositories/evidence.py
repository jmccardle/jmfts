"""EvidenceRepository — the one door to ``document_evidence``. ``SPRINT_JOBS.md`` Part 3.

Every named fact the ingest pipeline learns about a node is a row here, and every read and
write of one goes through this class. Phase 2b is what made that true: before it, twenty-nine
of these names were keys in ``Document.structured_content`` and thirty-odd call sites did
their own ``structured = dict(node.structured_content or {})`` — the read-modify-write 13.1
measured losing a concurrent write to a different name, with nothing raised.

THIS IS THE SEAM 14 SAYS PHASES 3 THROUGH 7 SIT ON. "3 through 7 read and write evidence
through :mod:`jmfts_core.evidence`, so where the values physically sit is behind that module
and the migration is a swap underneath it." The registry says what a name means and this
says where it is; a phase that adds ``produced_by``, a fingerprint or a staleness sweep adds
a method here and not a second store.

FOUR RULES, AND EACH ONE CLOSES SOMETHING 3.1 OR 3.2 RAISED.

1. **A name that is not in the registry is a typo, and writing one raises.** The vocabulary
   is closed (:func:`jmfts_core.evidence.get` says so), and a row named ``mached`` would
   read as evidence nothing writes rather than as the mistake it is.
2. **A value is type-checked against its registration on the way in.** ``evidence.check``
   is the one place that answers what a name holds, so a guard in Phase 4 and a fan-out
   bound today cannot come to disagree about what ``rows`` is.
3. **A null is a result and an absent row is not.** 3.2: an atom writes every name in its
   ``produces`` on success, null included, because producing nothing is a result. So
   :meth:`write` of ``None`` creates a row, and :meth:`read` answers
   :data:`~jmfts_core.evidence.ABSENT` only when there is no row at all.
4. **Appending never reads first.** :meth:`append` is one ``value || entries`` statement.
   ``attempts`` is spec 5.6's durable append-only log and the queue writes it from more
   than one place; a read-modify-write there loses attempt records under exactly the
   concurrency the log exists to explain.

WHAT DOES NOT LIVE HERE. Four registered names are stored elsewhere and this class refuses
them rather than inventing a row: ``text`` and ``embedding`` are columns on ``Document``,
``blob`` is a large object behind :class:`~jmfts_core.repositories.blob.BlobRepository`,
``embedding.tokens`` is ``token_embeddings``, and ``child_count`` is read off the shape of
the tree. :func:`jmfts_core.evidence.get` names the store in the error, so a caller that
asked here for one of them is told where it actually is.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Session

from jmfts_core import evidence as ev
from jmfts_core.models.document_evidence import STATE_WRITTEN, DocumentEvidence


class EvidenceRepository:
    """Read and write one node's evidence rows, by registry name."""

    def __init__(self, session: Session):
        self.session = session

    # =========================================================================
    # Resolving a name to a row
    # =========================================================================

    @staticmethod
    def _row_of(name: str) -> str:
        """The ``document_evidence`` row ``name`` lives in, or raise saying where it is.

        A leaf resolves to its row and keeps its path — ``matched.format`` is the ``format``
        key of the ``matched`` row, not a row of its own. :attr:`Store.row` is what says so,
        and it is a separate field from the path for the one case where a dotted name is its
        own row: ``source_anchor.unresolved``.
        """
        entry = ev.get(name)
        if entry.store.kind != ev.STORE_EVIDENCE:
            raise ValueError(
                f"evidence {name!r} lives in {entry.store}, not in document_evidence; "
                "EvidenceRepository cannot answer for it"
            )
        assert entry.store.row is not None  # Store.__post_init__ guarantees it
        return entry.store.row

    # =========================================================================
    # Read
    # =========================================================================

    def read_all(self, document_id: int) -> dict[str, Any]:
        """Every evidence row on one node, as ``{name: value}``.

        One query. 13.1 measured this against the JSONB column it replaced on the case it
        should be worst for — one node, every value, one row against six — and the join cost
        seven microseconds.

        A row holding a null appears with a ``None`` value, which is 3.2's "written, and the
        value is null". A name with no row is simply absent from the dict, which is
        "never attempted"; :func:`jmfts_core.evidence.resolve` reads this dict and keeps the
        two apart.
        """
        rows = self.session.execute(
            select(DocumentEvidence.name, DocumentEvidence.value).where(
                DocumentEvidence.document_id == document_id
            )
        ).all()
        return {name: value for name, value in rows}

    def read(self, document_id: int, name: str) -> Any:
        """One evidence value, or :data:`~jmfts_core.evidence.ABSENT`.

        Answers for a leaf as well as a row: the descent into the row's value is
        :func:`jmfts_core.evidence.resolve`'s, driven by the registry's store path, so a
        caller asking for ``extraction.characters`` does not have to know it is inside
        ``extraction``.
        """
        row = self._row_of(name)
        found = self.session.execute(
            select(DocumentEvidence.value).where(
                DocumentEvidence.document_id == document_id,
                DocumentEvidence.name == row,
            )
        ).first()
        if found is None:
            return ev.ABSENT
        return ev.resolve({row: found[0]}, name)

    def read_many(self, document_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        """Evidence for several nodes at once, as ``{document_id: {name: value}}``.

        One query for the whole set rather than one per node. Every node asked about gets an
        entry, empty if it has no evidence, so a caller does not have to tell "no rows" from
        "not asked".
        """
        ids = list(document_ids)
        found: dict[int, dict[str, Any]] = {doc_id: {} for doc_id in ids}
        if not ids:
            return found
        rows = self.session.execute(
            select(
                DocumentEvidence.document_id, DocumentEvidence.name, DocumentEvidence.value
            ).where(DocumentEvidence.document_id.in_(ids))
        ).all()
        for doc_id, name, value in rows:
            found[doc_id][name] = value
        return found

    # =========================================================================
    # Write
    # =========================================================================

    def write(self, document_id: int, name: str, value: Any) -> None:
        """Write one whole evidence row, replacing whatever it held.

        WHOLE ROWS ONLY, AND THAT IS NOT AN OMISSION. An atom produces a block; nothing in
        Part 2 declares producing a single leaf inside somebody else's block, so a
        ``write("matched.format", ...)`` would be a writer with no atom behind it. Leaves are
        for READING — guards and fan-out bounds — which is why :meth:`read` resolves them and
        this does not.

        ``value`` may be ``None``: 3.2 makes a produced null a result, and the row records
        that the atom ran. :func:`jmfts_core.evidence.check` refuses a null for a name that
        declares no null result, so "the reader returned nothing" cannot arrive as "the
        handler forgot".
        """
        row = self._row_of(name)
        if row != name:
            raise ValueError(
                f"evidence {name!r} is a leaf inside the {row!r} row; write the whole block. "
                "An atom produces a block (SPRINT_JOBS.md Part 2) and leaves are read, "
                "not written"
            )
        ev.check(name, value)
        statement = (
            insert(DocumentEvidence)
            .values(document_id=document_id, name=row, value=value, state=STATE_WRITTEN)
            .on_conflict_do_update(
                index_elements=[DocumentEvidence.document_id, DocumentEvidence.name],
                # `state` back to 'written' on a rewrite: a name that was stale or failed and
                # has now been produced again is neither. Part 9's re-run is what does this.
                set_={"value": value, "state": STATE_WRITTEN},
            )
        )
        self.session.execute(statement)

    def write_all(self, document_id: int, values: Mapping[str, Any]) -> None:
        """Several whole rows on one node. Each one goes through :meth:`write`'s checks.

        What a handler that produces more than one block calls, and what
        :meth:`~jmfts_core.repositories.document.DocumentRepository.create` calls for a node
        born carrying evidence. Not a bulk statement — the checks are per name and the win
        would be one round trip on a handful of rows.
        """
        for name, value in values.items():
            self.write(document_id, name, value)

    def append(self, document_id: int, name: str, entries: Sequence[Any]) -> None:
        """Append items to a list-valued evidence row, in one statement.

        NO READ-MODIFY-WRITE, and ``attempts`` is why. Spec 5.6 makes it a durable
        append-only log, the queue writes it from more than one place, and reading the list
        to add to it loses records under exactly the concurrency the log exists to explain.
        ``value = value || entries`` is atomic against the row.

        Creates the row if it is not there, so the first attempt does not need a different
        call than the second.
        """
        entry = ev.get(name)
        if entry.type != ev.TYPE_LIST:
            raise ValueError(
                f"evidence {name!r} declares type {entry.type!r}; append is for a list"
            )
        # A nullable list has a null RESULT (3.2), and appending to one would have to decide
        # whether "produced nothing" becomes "produced these" or stays null. That is a
        # semantic call per name, so this refuses rather than picking one. `attempts` — the
        # only append-only name — is not nullable.
        if entry.nullable:
            raise ValueError(
                f"evidence {name!r} is nullable, and appending to a produced null would "
                "have to decide whether it stays null (SPRINT_JOBS.md 3.2); write it whole"
            )
        row = self._row_of(name)
        if row != name:
            raise ValueError(f"evidence {name!r} is a leaf inside the {row!r} row")
        if not entries:
            return
        statement = insert(DocumentEvidence).values(
            document_id=document_id, name=row, value=list(entries), state=STATE_WRITTEN
        )
        statement = statement.on_conflict_do_update(
            index_elements=[DocumentEvidence.document_id, DocumentEvidence.name],
            set_={
                # COALESCE cannot fire for a non-nullable name, and it is here so that a row
                # that somehow holds SQL NULL still takes the append instead of `NULL || x`
                # quietly evaluating to NULL and losing it.
                "value": func.coalesce(DocumentEvidence.value, cast("[]", JSONB)).op("||")(
                    statement.excluded.value
                ),
                "state": STATE_WRITTEN,
            },
        )
        self.session.execute(statement)

    def delete(self, document_id: int, name: str) -> None:
        """Remove one evidence row entirely.

        Back to 3.2's "never attempted", which is a different fact from a null value and
        from ``state = 'stale'``. Used when a re-run establishes that a name does not apply
        at all — not to stale one, which Part 9 does by setting the state.
        """
        row = self._row_of(name)
        self.session.execute(
            delete(DocumentEvidence).where(
                DocumentEvidence.document_id == document_id,
                DocumentEvidence.name == row,
            )
        )

    def stale(self, document_ids: Iterable[int], names: Iterable[str]) -> int:
        """Mark the named evidence on the named nodes stale. Returns the row count.

        13.1's second fact, as one statement: "binding a rule set at a subtree marks the
        evidence its rules produce as stale" is one ``UPDATE`` against this table, and a
        JSONB rewrite of every node in the subtree without it. Phase 6 is what calls this;
        it is here now because the table is what makes it expressible — the column had to
        DELETE a block to stale it, which loses the distinction every time.

        Pass the subtree as an ID LIST, already resolved. 13.1 measured why: with the
        subtree predicate and this table in one statement, the planner stops using
        ``idx_documents_path`` and sequentially scans ``documents``, which costs more than
        the join does.
        """
        ids = list(document_ids)
        wanted = [self._row_of(name) for name in names]
        if not ids or not wanted:
            return 0
        result = self.session.execute(
            update(DocumentEvidence)
            .where(
                DocumentEvidence.document_id.in_(ids),
                DocumentEvidence.name.in_(wanted),
            )
            .values(state="stale")
        )
        return int(result.rowcount or 0)


def evidence_of(session: Session, document_id: int) -> dict[str, Any]:
    """Every evidence row on one node. The shorthand a handler opens with.

    A module-level function rather than a method because the call it replaces was
    ``doc.structured_content or {}`` — an attribute access — and a handler that wants the
    whole picture before it decides anything should not have to name a repository to get it.
    """
    return EvidenceRepository(session).read_all(document_id)


def evidence_value(session: Session, document_id: int, name: str) -> Optional[Any]:
    """One evidence value, with :data:`~jmfts_core.evidence.ABSENT` flattened to ``None``.

    For the callers that genuinely cannot tell the two apart and do not need to — a message
    that prints what probe counted, a bound that treats "no measurement" and "measured
    nothing" the same way. A caller that DOES need the distinction calls
    :meth:`EvidenceRepository.read` and compares against
    :data:`~jmfts_core.evidence.ABSENT`; 3.2 is why that is not the default.
    """
    found = EvidenceRepository(session).read(document_id, name)
    return None if found is ev.ABSENT else found
