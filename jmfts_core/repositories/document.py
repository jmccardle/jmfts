"""Document Repository - CRUD and tree navigation

WHERE INGESTION STARTS, and therefore what this module does NOT do to a parent.

An ingestion is rooted at the node that has work queued for it, and nowhere higher. A
settled folder that gains an uploaded file has not itself acquired any work: the folder's
own content, embedding and index entries are exactly as correct after the upload as they
were before it. So ``create`` and ``reparent`` leave the parent's ``settled`` alone, and
``TaskQueueRepository.enqueue`` un-settles only the node it queues the task for. Nothing
walks upward writing ``in_flight``.

The property this gives up is spec 2.1's ``settled`` read as a RECURSIVE predicate over
the whole tree — "a node is settled when its own work is done AND every child is settled".
It is still enforced where it decides anything: :func:`jmfts_core.settling.settle_node`
counts unsettled children under a row lock before it settles a node, so a parent can never
*become* settled over unfinished children. What changes is that a parent already settled
does not *stop* being settled while a descendant is reworked.

The consequence, stated plainly because it is not free. Reworking an existing settled
node (spec 6.3's correction) drops that node out of the settled-only view for the duration,
and its ancestors no longer advertise the fact — so :meth:`DocumentRepository.get_subtree`,
whose in-flight guard keys on the root's own column, returns a tree with a hole and no
error. New content does not have this problem, because a node that was never published
cannot go missing from a view. Corrections of published content do. That guard should key
on the returned set rather than on the root, and does not yet.

What is bought is that a failure anywhere in a large tree stays local: an unreadable PDF
fails on its own node, and the library it was filed in remains retrievable. Under the
walk-to-the-root behaviour this replaces, one bad file took an entire collection out of
the partial retrieval indexes of Part 2.2 and left it there.
"""

import hashlib
from datetime import datetime
from typing import Optional, Any, Sequence
from sqlalchemy import and_, select, func, nullslast, text
from sqlalchemy.orm import Session, joinedload

from jmfts_core.access import (
    readable_filter,
    readable_id_subset,
    require_add_child,
    require_write,
)
from jmfts_client.contracts.attempt import TERMINAL_STATUSES, AttemptRecord
from jmfts_core.models.document import (
    Document,
    DocumentLink,
    SETTLED_SETTLED,
    SETTLED_STATES,
    USETYPE_FILE,
)
from jmfts_core.models.document_blob import DocumentBlob
from jmfts_core.models.document_evidence import DocumentEvidence
from jmfts_core.models.principal import AccessGrant
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.models.token_embedding import TokenEmbedding
from jmfts_core.embedder import get_embedder
from jmfts_core.embedding import EmbeddingResult
from jmfts_core.token_selection import importance_from_salience

# THERE IS NO `INGEST_OWNED_KEYS` ANY MORE, AND PHASE 2b IS WHY. `structured_content` used
# to be two things stitched together — twenty-nine names the ingest pipeline owned, and
# whatever a caller put beside them — so `update()` needed a gate that told a `PATCH` which
# keys it could not name, and every key the gate did not know about was deleted by the next
# metadata edit. `SPRINT_JOBS.md` 13.3 removed the gate by removing the overlap: the blocks
# are rows in `document_evidence` now (migration 015), the column is wholly the caller's,
# and there is nothing left for a gate to protect. `options` was the last hard case — probe
# reads it minutes after the request that set it returned — and it is a row like the rest.


def compute_content_hash(content: Optional[str]) -> Optional[str]:
    """SHA-256 hex digest of UTF-8 content. Returns None for falsy input."""
    if not content:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class InFlightSubtreeError(RuntimeError):
    """`get_subtree` was asked for a subtree whose ROOT is not settled, without the
    caller opting in to in-flight nodes.

    Deliberately NOT a LookupError. `DocumentService.get_subtree` already maps
    LookupError to 404 for both "no such document" and "you may not read it"
    (existence-hiding), and an in-flight root is neither: the document exists, the
    caller may read it, it just is not finished. Reusing 404 would make a real
    mid-ingestion tree indistinguishable from a missing one.

    The failure this prevents: a caller asks for the subtree of an in-flight root, gets
    the settled-only default, and receives a partial tree with no error at all. A silent
    wrong answer is worse than a raise the caller has to think about.
    """

    def __init__(self, root_id: int, state: str):
        self.root_id = root_id
        self.state = state
        super().__init__(
            f"Document {root_id} is '{state}', not settled: its subtree may still be "
            f"changing and a settled-only walk would return an incomplete tree. "
            f"Pass include_in_flight=True to walk it anyway."
        )


class PopulatedMoveError(RuntimeError):
    """A ``children``-mode task tried to move a node that has children of its own.

    Spec 5.3. Moving a populated node rewrites all its descendants' ``path`` values and
    invalidates every rollup above it, which is outside what a ``children``-mode task
    reserved. Deliberately NOT a ValueError: the caller passed a perfectly good document
    id and the refusal is about the *state of the tree*, so a handler that means "bad
    argument" should not catch it. Deliberately not degraded to a partial move either —
    there is no correct partial version of this.
    """

    def __init__(self, document_id: int, child_count: int):
        self.document_id = document_id
        self.child_count = child_count
        super().__init__(
            f"Document {document_id} has {child_count} child(ren) and cannot be moved by "
            "a children-mode task: the move would rewrite every descendant's path and "
            "invalidate the rollups above it. Use a subtree-mode correction instead."
        )


def _sibling_order() -> tuple[Any, ...]:
    """The CR-1 ordering contract for sibling/child listings.

    `position` is sparse (NULL for unordered subtrees), so NULLS LAST makes those
    fall back to the legacy `created_at` order; `id` is the final total-order
    tiebreak (two sub-millisecond inserts can tie on both position and created_at).
    """
    return (
        nullslast(Document.position.asc()),
        Document.created_at.asc(),
        Document.id.asc(),
    )


class DocumentRepository:
    """Repository for document CRUD operations and tree navigation"""

    def __init__(self, session: Session):
        self.session = session

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    def create(
        self,
        title: Optional[str] = None,
        content: Optional[str] = None,
        parent_id: Optional[int] = None,
        usetype: Optional[str] = None,
        structured_content: Optional[dict] = None,
        evidence: Optional[dict] = None,
        auto_embed: bool = True,
        embed_tokens: bool = True,
        sequential: Optional[bool] = None,
        event_time: Optional[datetime] = None,
        settled: str = SETTLED_SETTLED,
        produced_by: Optional[str] = None,
    ) -> Document:
        """
        Create a new document.

        Args:
            title: Document title
            content: Document content
            parent_id: Parent document ID (for tree structure)
            usetype: Document type classification
            structured_content: Arbitrary JSON metadata, owned by the caller. Evidence
                does not go here — see `evidence` below and `SPRINT_JOBS.md` 13.3.
            evidence: `{registry name: value}` for a node born already knowing something,
                written to `document_evidence` after the row exists. Two parameters rather
                than one because they are two stores and one of them is checked: a name
                that is not in `jmfts_core.evidence.REGISTRY` raises here, and a value of
                the wrong type raises here. A dict literal in the column checked neither.
            auto_embed: Whether to generate embeddings automatically
            embed_tokens: Whether to generate token-level (maxsim) embeddings as
                well as the document vector.  Late interaction operates on leaves,
                and the token path is capped at settings.embedding_token_window,
                so container documents that hold a whole source text must pass
                False: they still get a full-length document vector (8192-token
                window), they just do not get token vectors they cannot fit.
                Leaving this True on over-window content now raises TextTooLongError
                rather than silently embedding a prefix.
            sequential: Whether this document takes an explicit sibling `position`
                (CR-1). When True, position is auto-assigned as MAX(position)+1 over
                the sibling group (birth order). When False, position stays NULL and
                the document sorts by created_at. When None (default), it inherits:
                True iff the parent itself carries a position — so ordering
                propagates down an ordered subtree but stays off by default, and the
                top of an ordered region opts in explicitly. Ill-formed for roots.
            event_time: Domain time — when the thing this document records actually
                happened, as opposed to when the row was created. Set it for imported
                content (transcript turns, backfills, benchmark corpora) where every
                row shares one ingest `created_at` and ordering by the system clock
                would just be reading ingest order. Leave None for content authored in
                place; readers use COALESCE(event_time, created_at).
            settled: Ingest lifecycle state — one of 'in_flight', 'settled', 'failed'.
                Defaults to 'settled', which is what a synchronous caller wants: it
                creates the row, it is done with it, and the row is immediately
                retrievable. Pass 'in_flight' when the node is the start of work rather
                than the end of it (an uploaded file whose tree has not been built yet,
                a chunk whose children are still being written) — it is then excluded
                from vector, full-text and MaxSim retrieval until something settles it.
            produced_by: Which rule wrote this node (`SPRINT_JOBS.md` 4.2), which today is
                the task type of the atom that did it. Default None, meaning ASSERTED — a
                person, an importer, or an upload created it. That default is correct for
                every caller outside the ingest handlers and is not a missing value: the
                stamp is what makes a rule's frontier scope resolvable, and a node no rule
                produced belongs to no rule's scope.

        Returns:
            Created document

        Raises:
            TextTooLongError: If auto_embed is on and the content exceeds the window
                of the requested embedding path.  Chunk it first.
            ValueError: If the parent does not exist, sequential ordering is requested
                for a root document (parent_id is None), or `settled` is not one of the
                three lifecycle states.
        """
        # Checked here as well as by the DB CHECK constraint: a typo'd state should read
        # as a bad argument at the call site, not as an IntegrityError at flush time
        # (which may be an arbitrary distance away, and takes the whole transaction).
        if settled not in SETTLED_STATES:
            raise ValueError(f"settled must be one of {SETTLED_STATES}, got {settled!r}")
        # Build path from parent
        path = []
        parent: Optional[Document] = None
        if parent_id:
            parent = self.get(parent_id)
            if not parent:
                raise ValueError(f"Parent document {parent_id} does not exist")
            # Subtree RBAC: adding a child needs write on the parent's governing ACR
            # (no-op for owner/unbound callers; a hidden parent reads as "not found").
            require_add_child(self.session, parent)
            path = (parent.path or []) + [parent_id]

        # Resolve sibling ordering (CR-1). Default: inherit ordered-ness from the
        # parent (propagates down, off by default). Explicit True/False wins.
        if sequential is None:
            sequential = parent is not None and parent.position is not None
        if sequential and parent_id is None:
            raise ValueError(
                "sequential ordering is not defined for root documents "
                "(parent_id is None): roots have no reading-order relationship"
            )
        position = self._next_sibling_position(parent_id) if sequential else None

        doc = Document(
            title=title,
            content=content,
            parent_id=parent_id,
            usetype=usetype,
            produced_by=produced_by,
            structured_content=structured_content or {},
            path=path,
            position=position,
            event_time=event_time,
            content_hash=compute_content_hash(content),
            settled=settled,
        )

        self.session.add(doc)
        self.session.flush()  # Get the ID

        # AFTER the flush, because an evidence row needs a document id to point at. The
        # names and types are checked in EvidenceRepository.write, so a handler that
        # misspells a block finds out here rather than at the read that comes up empty.
        if evidence:
            EvidenceRepository(self.session).write_all(doc.id, evidence)

        # NOTHING HAPPENS TO THE PARENT HERE. A settled node that gains a child keeps its
        # own `settled`; see the module note on where ingestion starts.

        # Auto-embed if requested and there's content
        if auto_embed and content and len(content) > 10:
            self.embed_document(doc.id, with_tokens=embed_tokens)

        return doc

    def _next_sibling_position(self, parent_id: Optional[int]) -> int:
        """Next explicit position within a sibling group: MAX(position)+1, from 0.

        Deliberately not locked or unique: two concurrent auto-numbered inserts
        under the same parent can read the same MAX and tie. That is acceptable —
        the `created_at, id` tail of the ordering contract resolves ties. A unique
        constraint would instead reject the second insert, which is wrong.
        """
        stmt = select(func.max(Document.position))
        if parent_id is None:
            stmt = stmt.where(Document.parent_id.is_(None))
        else:
            stmt = stmt.where(Document.parent_id == parent_id)
        current_max = self.session.execute(stmt).scalar()
        return 0 if current_max is None else current_max + 1

    def get(self, document_id: int) -> Optional[Document]:
        """Get a document by ID"""
        return self.session.get(Document, document_id)

    def get_with_embeddings(self, document_id: int) -> Optional[Document]:
        """Get a document with its token embeddings loaded"""
        stmt = (
            select(Document)
            .options(joinedload(Document.token_embeddings))
            .where(Document.id == document_id)
        )
        return self.session.execute(stmt).unique().scalar_one_or_none()

    def update(
        self,
        document_id: int,
        title: Optional[str] = None,
        content: Optional[str] = None,
        usetype: Optional[str] = None,
        structured_content: Optional[dict] = None,
        re_embed: bool = True,
    ) -> Optional[Document]:
        """Update a document.

        ``structured_content`` REPLACES the column, and after Phase 2b that is the whole
        story. It used to merge, because the column was two things stitched together and a
        plain assignment deleted the ingest pipeline's half — a ``PATCH`` with
        ``{"tag": "q3"}`` on a node under ingestion took the ``file`` block, the ``matched``
        block and the entire append-only attempt log with it. Evidence is in
        ``document_evidence`` now (13.3), nothing shares this column, and a whole-object
        assignment is what a caller asking to replace their metadata meant.

        **Writing ``content`` clears ``produced_by``** — ``SPRINT_JOBS.md`` 9.4, and it is
        the recommendation that section makes rather than an addition to it. Idempotence
        cannot tell "my output changed" from "somebody edited my output": both read as a
        mismatch when a rule re-derives its children, so a re-run would silently overwrite
        the edit. ``NULL`` already means asserted, so clearing the stamp takes the node out
        of the rule's match set entirely — the re-run neither keeps it nor deletes it, and
        the rule creates a sibling instead. The person's edit survives, the machine's output
        is regenerated, and both are visible. What nothing yet does is reconcile the two;
        9.4 records that as a review task rather than something the walk should decide.

        Only ``content``, and not ``title``, ``usetype`` or ``structured_content``. What a
        rule PRODUCED is the text of the node — a chunk's prose, a record's rendering — so
        that is what a person can have overwritten. Retitling a produced chunk or tagging it
        is metadata about a node the rule still owns, and clearing the stamp for that would
        exclude it from every future pass over its own tree.
        """
        doc = self.get(document_id)
        if not doc:
            return None
        require_write(self.session, doc, "modify")  # subtree RBAC write gate

        if title is not None:
            doc.title = title
        if content is not None:
            doc.content = content
            doc.produced_by = None
        if usetype is not None:
            doc.usetype = usetype
        if structured_content is not None:
            doc.structured_content = structured_content

        # Re-embed if content changed
        if re_embed and content is not None and len(content) > 10:
            self.embed_document(doc.id, with_tokens=True)

        return doc

    def delete(self, document_id: int) -> bool:
        """Delete a document and its children (cascade)"""
        doc = self.get(document_id)
        if not doc:
            return False
        # Subtree RBAC: write on the doc's ACR. The additive model guarantees write on a
        # node implies write on every descendant, so the cascade never removes a child
        # the caller could not have deleted directly.
        require_write(self.session, doc, "delete")
        # Uploaded bytes live in a Postgres LARGE OBJECT, which is not stored in any
        # table: `ON DELETE CASCADE` removes the document_blobs row and leaves the object
        # behind, unreferenced and unreachable. INGEST_SPEC.md Part 9 names this leak
        # explicitly. Unlink the whole subtree's objects FIRST — after the cascade there
        # is nothing left that knows their OIDs.
        BlobRepository(self.session).unlink_subtree(document_id)
        self.session.delete(doc)
        return True

    # =========================================================================
    # Ingest attempt log (INGEST_SPEC.md 3.4)
    # =========================================================================

    def attempt_log(self, document: Document) -> list:
        """The stored attempt log, as a list. Spec 3.4, `SPRINT_JOBS.md` evidence `attempts`.

        A read of the `attempts` evidence row, which is where the log has lived since Phase
        2b moved it out of `structured_content`. One place, so no caller has to know that.
        """
        found = EvidenceRepository(self.session).read(document.id, "attempts")
        return list(found) if isinstance(found, list) else []

    def attempt_counts(self, document: Document) -> dict[str, int]:
        """How many attempts the stored log already holds, per task name.

        The `attempt` field of a new record is this count plus one. Callers that write
        several records for the same task in a single run keep incrementing their own
        copy — re-reading here between records would return the same number twice, because
        the records they have written are in the log and the ones they have not are not.
        """
        counts: dict[str, int] = {}
        for entry in self.attempt_log(document):
            if isinstance(entry, dict) and isinstance(entry.get("task"), str):
                counts[entry["task"]] = counts.get(entry["task"], 0) + 1
        return counts

    def append_attempts(self, document: Document, records: Sequence[AttemptRecord]) -> None:
        """Append attempt records to the `attempts` evidence row. Append-only.

        Spec 3.4: a re-ingest ADDS to the log, it never rewrites it, because the log is
        what makes "this file was ingested before a vision model existed" a recoverable
        fact.

        ONE STATEMENT, AND IT NO LONGER READS FIRST. While the log was a key in
        `structured_content` this had to read the list, copy it, append and reassign — and
        the reassignment was load-bearing, because SQLAlchemy's JSONB change tracking only
        sees a whole-attribute assignment. That whole discipline is gone: `value || records`
        against one row is atomic, so two writers appending to one log cannot lose an entry
        between the read and the write. 13.1's first fact, on the name it mattered most for.
        """
        if not records:
            return
        EvidenceRepository(self.session).append(
            document.id, "attempts", [r.to_jsonb() for r in records]
        )
        self.session.flush()

    def upsert_attempt(self, document: Document, record: AttemptRecord) -> None:
        """Write one attempt, replacing the LIVE entry for the same queue row if there is one.

        The log is append-only across attempts (spec 6.2) — but an attempt is not born
        terminal. Spec 5.7 has the queue write a ``pending`` entry the moment a task is
        enqueued, so ``POST /ingest/file`` can return "the current attempt log, which at
        that moment is one pending entry", and the worker writes the OUTCOME of that same
        attempt when it finishes. Appending both would make one attempt look like two, and
        a client polling the node would watch its history grow with every status change.

        So: a record carrying a ``task_id`` replaces the last entry with that id **if and
        only if that entry is still non-terminal**. A terminal entry is never rewritten,
        which is what keeps a retry's failure visible after the retry succeeds — a retry
        reuses the queue row, and therefore its id, so replacing by id alone would erase
        the very history the log exists for.

        A record with no ``task_id`` (a stage of the synchronous pipeline, or a skip that
        never had a queue row) always appends; there is nothing to match it against.

        THIS IS THE ONE READ-MODIFY-WRITE LEFT ON THE LOG, and it is not one this migration
        could remove. :meth:`append_attempts` became a single ``value || records`` because
        appending needs nothing from the stored list; replacing an entry needs to find it
        first, and "the last non-terminal entry with this task_id" is not a predicate a
        JSONB update expresses. It races only with another write to the SAME row — Phase 2b
        removed the race with a write to a DIFFERENT name, which is the one 13.1 measured —
        and the queue serialises a task's own writes.
        """
        repo = EvidenceRepository(self.session)
        entries = self.attempt_log(document)

        target: Optional[int] = None
        if record.task_id is not None:
            for index in range(len(entries) - 1, -1, -1):
                entry = entries[index]
                if isinstance(entry, dict) and entry.get("task_id") == record.task_id:
                    if entry.get("status") not in TERMINAL_STATUSES:
                        target = index
                    break

        if target is None:
            # Nothing to replace, so this is an ordinary append and takes the atomic path.
            repo.append(document.id, "attempts", [record.to_jsonb()])
        else:
            entries[target] = record.to_jsonb()
            repo.write(document.id, "attempts", entries)
        self.session.flush()

    def find_by_hash_and_parent(
        self, content_hash: str, parent_id: Optional[int]
    ) -> Optional[Document]:
        """Idempotency lookup: existing document with the same content under the same parent.

        Returns the most recently created match if any. Used by ``create_document``
        to short-circuit re-ingestion of identical content.

        ONLY NODES WHOSE `content_hash` IS THE HASH OF THEIR `content`. `content_hash` used
        to be derived from `content` and nothing else, so equal hashes implied equal
        ingested content and this lookup could match on the hash alone. A `file` node
        breaks that: its content IS the uploaded bytes, so `POST /ingest/file` writes their
        sha256 here — and the sha256 of a text file's bytes is the sha256 of the same
        text's UTF-8. Without a predicate excluding file nodes, ingesting `notes.md` as
        text after uploading it as a file resolves to the file node, returns
        `was_existing=true`, and the markdown is never ingested and never retrievable.

        The predicate is on `usetype`, and it used to be `content IS NOT NULL`. That guard
        worked only while a file node's `content` stayed NULL, which stopped being true at
        commit `e7b1ffb`: `extract:text` writes the extracted markdown into `content` on
        the file node itself. A file node then has non-NULL `content` whose hash is NOT its
        `content_hash`, so it passed the old guard and the short-circuit came back —
        silently, and only for formats that reach extraction, which is why `.md` uploads
        never showed it. Naming the usetype says what is actually being excluded and does
        not depend on which columns a later task fills in.

        `is_distinct_from` rather than `!=`: `usetype` is nullable, and `NULL != 'file'` is
        NULL, which would drop every untyped document out of the lookup entirely.
        """
        if not content_hash:
            return None
        stmt = select(Document).where(
            Document.content_hash == content_hash,
            Document.usetype.is_distinct_from(USETYPE_FILE),
        )
        if parent_id is None:
            stmt = stmt.where(Document.parent_id.is_(None))
        else:
            stmt = stmt.where(Document.parent_id == parent_id)
        stmt = stmt.order_by(Document.created_at.desc()).limit(1)
        return self.session.execute(stmt).scalar_one_or_none()

    def find_readable_file_by_blob_hash(self, content_hash: str) -> Optional[Document]:
        """The newest `file` node holding exactly these bytes THAT THE CALLER MAY READ.

        The upload deduplication lookup (`IngestService.upload_file`): a second upload of
        bytes that are already here should resolve to the node that already holds them
        instead of writing a second copy of a 40 MB PDF and re-running its whole pipeline.

        Keyed on `document_blobs.content_hash`, not `documents.content_hash`. Both columns
        carry the same hex for a file node, but only the blob column is indexed
        (`idx_document_blobs_hash`, migration 009), and the blob row is what asserts the
        bytes are actually stored — a node whose upload half-landed (no blob row, see
        :meth:`BlobRepository.find_blobless_documents`) must NOT be handed to a second
        uploader as if its bytes were on disk. The join is the existence check.

        THE RBAC PREDICATE IS THE CORRECTNESS CONDITION, NOT AN OPTIMISATION. Returning a
        node the caller cannot read would hand them a document id they can do nothing with
        and, worse, would tell them that somebody else holds these bytes. So a principal
        who cannot read the existing node gets no match here and their upload proceeds
        normally — two nodes with identical bytes in two isolated access zones, which is
        the honest outcome: nothing can deduplicate across a boundary that exists precisely
        to stop one side learning what the other has. Because the predicate is part of the
        query rather than a check on the result, "no such bytes" and "bytes you may not
        see" are the same `None` and cannot be told apart.

        Owner and unbound (in-process) callers bypass, as everywhere else — `readable_filter`
        returns None and the query is unfiltered.
        """
        if not content_hash:
            return None
        stmt = (
            select(Document)
            .join(DocumentBlob, DocumentBlob.document_id == Document.id)
            .where(
                DocumentBlob.content_hash == content_hash,
                Document.usetype == USETYPE_FILE,
            )
        )
        pred = readable_filter(self.session)
        if pred is not None:
            stmt = stmt.where(pred)
        # created_at ties are real — two uploads in one transaction share a clock reading —
        # so `id` is the tiebreak that makes "the most recent match" a single row.
        stmt = stmt.order_by(Document.created_at.desc(), Document.id.desc()).limit(1)
        return self.session.execute(stmt).scalars().first()

    def find_readable_file_by_source(self, kind: str, locator: str) -> Optional[Document]:
        """The newest `file` node fetched from this locator THAT THE CALLER MAY READ.

        `SPRINT_JOBS.md` 15.4 S8's deduplication, and it asks a different question from
        :meth:`find_readable_file_by_blob_hash` because it has to. That one compares the
        bytes; here the bytes are precisely what has not been fetched yet, so the only
        thing available to compare is the locator — which is also what a caller means when
        they send the same URL twice.

        NO BLOB JOIN, unlike the hash lookup, and the difference is deliberate. A source
        node exists before its bytes do, so requiring a blob row would mean two concurrent
        requests for one URL both create a node and both fetch it. Matching a node whose
        fetch is still in flight is the right answer: the work is queued, and the second
        caller wants the same document.

        The RBAC predicate is the same correctness condition it is on the hash lookup, and
        it is part of the query for the same reason — "no such source" and "a source you
        may not see" must be one `None`.
        """
        if not kind or not locator:
            return None
        # A join, since Phase 2b: `source` is an evidence row, not a column key. The
        # predicate is on `document_evidence.value` and `idx_document_evidence_name` is
        # what answers it — 13.1 measured this shape of read at ten times the JSONB path
        # over a full corpus, with the gap widening as the corpus grows.
        stmt = (
            select(Document)
            .join(
                DocumentEvidence,
                and_(
                    DocumentEvidence.document_id == Document.id,
                    DocumentEvidence.name == "source",
                ),
            )
            .where(
                Document.usetype == USETYPE_FILE,
                DocumentEvidence.value["kind"].astext == kind,
                DocumentEvidence.value["locator"].astext == locator,
            )
        )
        pred = readable_filter(self.session)
        if pred is not None:
            stmt = stmt.where(pred)
        stmt = stmt.order_by(Document.created_at.desc(), Document.id.desc()).limit(1)
        return self.session.execute(stmt).scalars().first()

    def find_own_granted_file_by_blob_hash(
        self, content_hash: str, principal_id: int
    ) -> Optional[Document]:
        """The newest `file` node holding exactly these bytes THAT THIS PRINCIPAL HOLDS A
        GRANT ON — the deduplication lookup for a PRIVATE upload.

        READABLE IS THE WRONG TEST HERE, AND THAT IS THE WHOLE POINT. JMFTS defaults to
        shared: a document under no access-control root is readable by every principal
        (`jmfts_core/access.py`). So :meth:`find_readable_file_by_blob_hash` matches the
        SHARED node holding these bytes, and answering a caller who asked for isolation
        with somebody else's world-readable node would hand them the exact opposite of
        what they requested, with a 201 that looks like success.

        Holding a grant on the node is the narrower condition that means "this node is
        already mine": it is what a private upload creates, so the same principal
        uploading the same bytes privately twice still resolves to one node, while a
        shared node — on which nobody holds a grant — can never be returned. Two
        principals uploading one document privately therefore get one node each. That is
        the cost of isolation, not a defect in it: nothing can deduplicate across a
        boundary drawn to stop one side learning what the other has.

        The grant is checked on the node ITSELF rather than max-over-path, because a
        private upload is rootless (`private` with a `parent_id` is refused — see
        :meth:`IngestService.upload_file`), so there is no path to walk.
        """
        if not content_hash:
            return None
        stmt = (
            select(Document)
            .join(DocumentBlob, DocumentBlob.document_id == Document.id)
            .where(
                DocumentBlob.content_hash == content_hash,
                Document.usetype == USETYPE_FILE,
                Document.id.in_(
                    select(AccessGrant.document_id).where(AccessGrant.principal_id == principal_id)
                ),
            )
            .order_by(Document.created_at.desc(), Document.id.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def find(
        self,
        parent_id: Optional[int] = None,
        usetype: Optional[str] = None,
        title_prefix: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Document]:
        """Find documents matching criteria"""
        stmt = select(Document)

        if parent_id is not None:
            stmt = stmt.where(Document.parent_id == parent_id)
        if usetype:
            stmt = stmt.where(Document.usetype == usetype)
        if title_prefix:
            stmt = stmt.where(Document.title.ilike(f"{title_prefix}%"))

        stmt = stmt.order_by(Document.created_at.desc()).limit(limit).offset(offset)

        return list(self.session.execute(stmt).scalars().all())

    # =========================================================================
    # Tree Navigation
    # =========================================================================

    def get_children(
        self,
        parent_id: int,
        title: Optional[str] = None,
        title_prefix: Optional[str] = None,
        usetype: Optional[str] = None,
        depth: int = 1,
        limit: int = 100,
    ) -> list[Document]:
        """Get children of a document with optional filtering.

        Args:
            parent_id: Parent document ID
            title: Exact title match
            title_prefix: Title prefix match (case-insensitive)
            usetype: Filter by usetype
            depth: 1 = immediate children, -1 = all descendants (subtree)
            limit: Max results
        """
        if depth == -1:
            # Use subtree query for all descendants
            stmt = select(Document).where(Document.path.op("@>")(func.jsonb_build_array(parent_id)))
        else:
            stmt = select(Document).where(Document.parent_id == parent_id)

        if title is not None:
            stmt = stmt.where(Document.title == title)
        if title_prefix is not None:
            stmt = stmt.where(Document.title.ilike(f"{title_prefix}%"))
        if usetype is not None:
            stmt = stmt.where(Document.usetype == usetype)

        # CR-1 sibling ordering. For depth=1 (immediate children) this is the true
        # sibling order. For depth=-1 the result is a *flat* descendant set, not a
        # depth-first tree walk (tree-traversal order is deferred to CR-1b); the
        # clause still gives a deterministic, position-aware ordering.
        stmt = stmt.order_by(*_sibling_order()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def get_ancestors(self, document_id: int) -> list[Document]:
        """Get all ancestors of a document (from root to parent)"""
        doc = self.get(document_id)
        if not doc or not doc.path:
            return []

        stmt = select(Document).where(Document.id.in_(doc.path))
        docs = {d.id: d for d in self.session.execute(stmt).scalars().all()}

        # Return in order (root first)
        return [docs[id] for id in doc.path if id in docs]

    def get_siblings(self, document_id: int, include_self: bool = False) -> list[Document]:
        """Get siblings of a document"""
        doc = self.get(document_id)
        if not doc:
            return []

        stmt = select(Document).where(Document.parent_id == doc.parent_id)
        if not include_self:
            stmt = stmt.where(Document.id != document_id)
        stmt = stmt.order_by(*_sibling_order())  # CR-1 sibling ordering

        return list(self.session.execute(stmt).scalars().all())

    def get_subtree(
        self,
        root_id: int,
        max_depth: Optional[int] = None,
        include_in_flight: bool = False,
    ) -> list[Document]:
        """
        Get all documents in a subtree using the path array.

        Args:
            root_id: Root document ID
            max_depth: Maximum depth to traverse (None for unlimited)
            include_in_flight: Whether nodes that are not settled ('in_flight' or
                'failed') are returned. Default False — a settled-only view, which is
                what every reader outside the ingest machinery wants: a tree that is
                still being built is not an answer. Ingestion passes True, because it
                is the code responsible for the unfinished nodes and must see what it
                just wrote.

        Returns:
            List of documents in the subtree (including root)

        Raises:
            InFlightSubtreeError: If the ROOT itself is not settled and the caller did
                not pass include_in_flight. Returning the settled-only view of an
                unsettled root is a silent partial answer — the tree is incomplete and
                the caller has no way to tell. This is the failure the parameter exists
                to prevent, so it is an error rather than a shorter list.
        """
        root = self.get(root_id)
        if not root:
            return []

        if not include_in_flight and root.settled != SETTLED_SETTLED:
            raise InFlightSubtreeError(root_id, root.settled)

        # Use GIN index on path: find all docs where path contains root_id
        # This means root_id is an ancestor of those docs
        stmt = select(Document).where(Document.path.op("@>")(func.jsonb_build_array(root_id)))

        if not include_in_flight:
            stmt = stmt.where(Document.settled == SETTLED_SETTLED)

        if max_depth is not None:
            root_depth = len(root.path) if root.path else 0
            stmt = stmt.where(func.jsonb_array_length(Document.path) <= root_depth + max_depth)

        descendants = list(self.session.execute(stmt).scalars().all())

        # Include root
        return [root] + descendants

    def settle_frontier(self, root_id: int) -> dict[str, int]:
        """Count the subtree under ``root_id`` (root included) by lifecycle state.

        This is the progress report for an ingestion, and it is deliberately a COUNT and
        not a percentage. Tasks below the current frontier do not exist yet — a node's
        children are only created when the node is processed — so the denominator of a
        percentage is unknown while the run is happening, and any estimate of it moves
        backward as work is discovered. A frontier that reads "31 settled, 4 in flight,
        0 failed" is honest at every instant; "78% complete" that later becomes 61% is
        not.

        Returns:
            All three keys, always, with zeros where nothing is in that state, plus
            ``total``. Callers can subtract without probing for missing keys.
            An unknown ``root_id`` returns all zeros — a subtree with no rows in it
            genuinely has nothing in any state; this is not a swallowed lookup failure,
            and callers who need to distinguish "missing" from "empty" have ``get()``.
        """
        # One grouped scan over the subtree rather than three counts. `path @>` covers
        # the descendants; the root is not on its own path, so it is unioned in by id.
        stmt = (
            select(Document.settled, func.count())
            .where(
                (Document.id == root_id) | (Document.path.op("@>")(func.jsonb_build_array(root_id)))
            )
            .group_by(Document.settled)
        )
        counts = {state: 0 for state in SETTLED_STATES}
        for state, n in self.session.execute(stmt).all():
            counts[state] = n
        counts["total"] = sum(counts[state] for state in SETTLED_STATES)
        return counts

    def get_root_documents(self) -> list[Document]:
        """Get all root documents (no parent)"""
        stmt = (
            select(Document)
            .where(Document.parent_id.is_(None))
            .order_by(Document.created_at.desc())
        )
        return list(self.session.execute(stmt).scalars().all())

    # =========================================================================
    # Re-parenting
    # =========================================================================

    def reparent(
        self, document_id: int, new_parent_id: int, *, childless_only: bool = False
    ) -> Document:
        """Move a document under a new parent, updating path for it and all descendants.

        Args:
            document_id: Document to move.
            new_parent_id: New parent document ID.
            childless_only: Spec 5.3's guard for a ``children``-mode task. When True the
                move is refused unless the node has no children of its own. Off by
                default so the ordinary administrative move (and every existing caller)
                is unchanged; the queue passes True for every task that declared
                ``write_mode = 'children'``.

        Returns:
            The re-parented document.

        Raises:
            ValueError: If either document does not exist, or the move would
                create a cycle (moving a node onto itself or under one of its
                own descendants).
            PopulatedMoveError: If ``childless_only`` and the node has children.
        """
        doc = self.get(document_id)
        if not doc:
            raise ValueError(f"Document {document_id} does not exist")

        if childless_only:
            self._refuse_if_populated(document_id)

        new_parent = self.get(new_parent_id)
        if not new_parent:
            raise ValueError(f"New parent {new_parent_id} does not exist")

        # Subtree RBAC: a move detaches from the source subtree and attaches to the
        # destination, so it needs write on BOTH — the node itself and the new parent.
        require_write(self.session, doc, "reparent")
        require_add_child(self.session, new_parent)

        # Cycle guard: the new parent must not be the node itself, nor any of its
        # descendants. A descendant is exactly a node whose path contains this id
        # (path is the ancestor chain), so this is an O(1) check, no traversal.
        if new_parent_id == document_id or document_id in (new_parent.path or []):
            raise ValueError(
                f"Cannot reparent {document_id} under {new_parent_id}: "
                "the target is the node itself or one of its descendants (cycle)"
            )

        old_path = list(doc.path or [])
        new_path = (new_parent.path or []) + [new_parent_id]

        # Reset sibling ordering (CR-1). The old `position` is meaningless in the
        # new group. Match create()'s inheritance rule: if the new parent is
        # itself ordered, append to the tail of the new sibling group; otherwise
        # the group is unordered and position becomes NULL. Computed BEFORE the
        # parent_id changes (autoflush is off) so the node isn't counted as its
        # own new sibling.
        if new_parent.position is not None:
            doc.position = self._next_sibling_position(new_parent_id)
        else:
            doc.position = None

        doc.parent_id = new_parent_id
        doc.path = new_path

        # Update descendants: replace the old path prefix with the new one
        old_prefix = old_path + [document_id]
        new_prefix = new_path + [document_id]
        descendants = self.get_children(document_id, depth=-1, limit=100000)
        for desc in descendants:
            desc_path = list(desc.path or [])
            if desc_path[: len(old_prefix)] == old_prefix:
                desc.path = new_prefix + desc_path[len(old_prefix) :]

        # The new parent's own `settled` is not touched, for the same reason create()
        # leaves it alone: gaining a child is not work for the parent.
        return doc

    def _refuse_if_populated(self, document_id: int) -> None:
        """Spec 5.3: a ``children``-mode task may only move nodes that have no children.

        Moving a populated node rewrites every descendant's ``path`` (the loop at the
        end of :meth:`reparent`) and invalidates every rollup above it. A ``children``
        task reserves only this node and its childless children, so a path rewrite
        underneath it reaches straight into a region some other task may hold. Refusing
        is the correct outcome — the alternative is silent corruption — and a correction
        that genuinely needs to move a populated node routes through a ``subtree`` task
        (Part 6), which reserves the region first.

        THE LOCK IS THE POINT, not the count. Under READ COMMITTED a concurrent
        transaction can insert a child between this check and the write, and neither
        statement would see the other. ``SELECT ... FOR UPDATE`` on the moving node's own
        row closes that: inserting a child takes ``FOR KEY SHARE`` on the referenced
        parent row for the foreign key, which conflicts with ``FOR UPDATE``. So the
        inserter either committed before this lock — in which case the count below sees
        its child and the move is refused — or it blocks until this transaction commits
        and attaches to the node at its new location, with a path computed from the new
        parent. There is no interleaving in which a populated node is moved. The lock is
        held for the rest of the caller's transaction, which is what makes "the check and
        the move are in one transaction" true rather than merely intended.
        """
        self.session.execute(
            select(Document.id).where(Document.id == document_id).with_for_update()
        ).scalar_one_or_none()

        # Counted by parent_id, the authoritative edge, rather than by the `path` GIN
        # index: a node with any descendant has at least one direct child, so the two
        # are equivalent on a consistent tree, and parent_id is the column a concurrent
        # INSERT actually writes.
        children = self.session.execute(
            select(func.count()).select_from(Document).where(Document.parent_id == document_id)
        ).scalar_one()
        if children:
            raise PopulatedMoveError(document_id, children)

    # =========================================================================
    # Embedding Operations
    # =========================================================================

    def embed_document(
        self,
        document_id: int,
        with_tokens: bool = True,
        token_top_percent: float = 0.35,  # Store top 35% for tiered filtering
        write_importance: bool = False,
    ) -> Optional[EmbeddingResult]:
        """
        Generate and store embeddings for a document.

        Args:
            document_id: Document to embed
            with_tokens: Whether to also generate token-level embeddings
            token_top_percent: Percentage of tokens to store (default 35% for tiered benchmarking)
            write_importance: Opt-in — derive ``structured_content['importance']`` from the
                token salience just computed (``importance_from_salience``) instead of an LLM
                rating. Off by default so the benchmark/retrieval path is untouched; requires
                ``with_tokens`` (no token salience without it). See the read side,
                ``SearchRepository._importance_factor``.

        Returns:
            EmbeddingResult if successful
        """
        doc = self.get(document_id)
        if not doc or not doc.content:
            return None

        # `get_embedder`, not `get_embedding_service`: this is the ingest WRITE path, and a
        # worker configured with JMFTS_RUNNER_URL produces its vectors on somebody else's
        # GPU. The two objects are interchangeable at every line below, which is why there
        # is no branch here. See jmfts_core/embedder.py.
        service = get_embedder()

        if with_tokens:
            # Get top 35% of tokens for tiered storage
            result = service.embed_with_tokens(
                doc.content, top_percent=token_top_percent, prefix="search_document: "
            )

            # Store document embedding
            doc.embed = result.document_embedding.tolist()

            # Opt-in salience-derived importance. Reassign a new dict rather than mutate
            # in place — SQLAlchemy's JSONB change tracking does not see in-place mutation
            # of the existing dict, so an in-place write would silently never persist.
            if write_importance:
                sc = dict(doc.structured_content or {})
                sc["importance"] = importance_from_salience(
                    [t.importance_score for t in result.token_embeddings]
                )
                doc.structured_content = sc

            # Delete existing token embeddings with raw SQL to guarantee
            # execution before ORM inserts (bypasses unit-of-work ordering)
            self.session.execute(
                text("DELETE FROM token_embeddings WHERE document_id = :doc_id"),
                {"doc_id": document_id},
            )
            # Expire the relationship so ORM doesn't try to cascade stale objects
            self.session.expire(doc, ["token_embeddings"])

            # Sort by importance and assign tiers (5, 10, 15, 20, 25, 30, 35, 40, 45, 50)
            # Each tier represents 5% of ORIGINAL tokens (before filtering)
            # So with 50% storage, we have 10 tiers worth of tokens
            sorted_tokens = sorted(
                result.token_embeddings, key=lambda t: t.importance_score, reverse=True
            )
            n_tokens = len(sorted_tokens)
            # Each tier covers 1/10 of stored tokens (which is ~5% of original)
            tier_size = max(1, n_tokens // 10)

            for i, tok in enumerate(sorted_tokens):
                # Assign tier based on position in ranking
                # tier 5 = top 1/10, tier 10 = next 1/10, etc.
                tier_idx = min(i // tier_size, 9)  # 0-9 mapping
                tier = (tier_idx + 1) * 5  # 5, 10, 15, 20, 25, 30, 35, 40, 45, 50

                token_emb = TokenEmbedding(
                    document_id=document_id,
                    token_idx=tok.token_idx,
                    token_text=tok.token_text,
                    importance_score=tok.importance_score,
                    tier=tier,
                )

                # Store 256-dim embedding (only dimension we use now)
                truncated = service.truncate_embedding(tok.embedding, 256)
                token_emb.embed_256 = truncated.tolist()

                self.session.add(token_emb)

            return result
        else:
            # Just document embedding
            embedding = service.embed_text(doc.content, prefix="search_document: ")
            doc.embed = embedding.tolist()
            return EmbeddingResult(
                document_embedding=embedding,
                token_embeddings=[],
            )

    # =========================================================================
    # Links
    # =========================================================================

    def create_link(
        self,
        source_id: int,
        target_id: int,
        link_type: str,
        score: float = 1.0,
        metadata: Optional[dict] = None,
    ) -> DocumentLink:
        """Create a link between two documents"""
        link = DocumentLink(
            source_id=source_id,
            target_id=target_id,
            link_type=link_type,
            score=score,
            link_metadata=metadata or {},
        )
        self.session.add(link)
        self.session.flush()  # Get the ID and created_at
        return link

    def get_links(
        self,
        document_id: int,
        direction: str = "both",
        link_type: Optional[str] = None,
    ) -> list[DocumentLink]:
        """
        Get links for a document.

        Args:
            document_id: Document ID
            direction: "outgoing", "incoming", or "both"
            link_type: Filter by link type
        """
        links = []

        if direction in ("outgoing", "both"):
            stmt = select(DocumentLink).where(DocumentLink.source_id == document_id)
            if link_type:
                stmt = stmt.where(DocumentLink.link_type == link_type)
            links.extend(self.session.execute(stmt).scalars().all())

        if direction in ("incoming", "both"):
            stmt = select(DocumentLink).where(DocumentLink.target_id == document_id)
            if link_type:
                stmt = stmt.where(DocumentLink.link_type == link_type)
            links.extend(self.session.execute(stmt).scalars().all())

        # Subtree RBAC: hide any edge whose OTHER endpoint the current principal cannot
        # read (the endpoint that isn't document_id). No-op for owner/unbound callers.
        other_ids = {
            (lk.target_id if lk.source_id == document_id else lk.source_id) for lk in links
        }
        readable = readable_id_subset(self.session, other_ids)
        if len(readable) != len(other_ids):
            links = [
                lk
                for lk in links
                if (lk.target_id if lk.source_id == document_id else lk.source_id) in readable
            ]

        return links

    def delete_link(self, link_id: int, *, incident_to: Optional[int] = None) -> Optional[str]:
        """Delete a link by id; return the deleted link's ``link_type``.

        The link graph is otherwise append-only — this is the retract leg. Deletion is
        unconditional (RAPTOR ``bridge`` edges included); the caller owns that policy.
        ``incident_to`` scopes the delete to a link touching that document (source or
        target), so ``DELETE /documents/{id}/links/{link_id}`` can't reach an unrelated
        edge. Returns ``None`` if no such link exists (or it isn't incident to the given
        document), which the service maps to 404.
        """
        link = self.session.get(DocumentLink, link_id)
        if link is None:
            return None
        if incident_to is not None and incident_to not in (link.source_id, link.target_id):
            return None
        link_type = link.link_type
        self.session.delete(link)
        self.session.flush()
        return link_type
