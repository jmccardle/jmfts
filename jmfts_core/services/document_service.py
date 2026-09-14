"""DocumentService — document CRUD, tree navigation, and the ingest-pipeline
operations (split / chunk / segment / RAPTOR / fact-extraction), transport-neutral.

Logic extracted verbatim from ``api/routers/documents.py`` so the behaviour is
identical; the only intentional change is that document serialisation now goes through
the single ``DocumentResponse.from_document`` converter (deleting ``documents.py``'s
local ``doc_to_response``), keeping the ONE ORM→response mapping the unification
established.

Domain → HTTP mapping is declared per-op in ``@expose(errors=...)`` and keyed by
EXCEPTION TYPE, so the hand-written statuses the router raised are reproduced without a
call-site check:

- ``LookupError``            → 404 (document / content not found)
- ``ValueError``             → 400 (bad parent, no content, unknown strategy,
                                    too-few embedded children, link source mismatch;
                                    ``TextTooLongError`` is a ``ValueError`` subclass,
                                    so ``create`` maps it to 400 with ``str(exc)`` too)
- ``EmbedTextTooLongError``  → 400 (the ``POST /{id}/embed`` case, which alone carries a
                                    STRUCTURED detail dict, preserved verbatim via the
                                    adapter's ``http_detail`` passthrough)

Reads do not commit; writes call ``self.session.commit()`` before returning (matching
the old routes' ``db.commit()`` and the ``get_db`` teardown-commit backstop contract).
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from sqlalchemy.orm import Session

from jmfts_core.access import can_read, filter_readable, require_edge_write
from jmfts_core.chunking import ChunkStrategy, chunk_text
from jmfts_client.contracts.anchor import (
    ANCHOR_ROW,
    ANCHOR_UNRESOLVED_ROW,
    CellsAnchor,
    PdfAnchor,
    SpanAnchor,
    UnknownAnchorKind,
    parse_anchor,
)
from jmfts_client.contracts.binary import BinaryPayload
from jmfts_client.contracts.document import (
    CellNoteResponse,
    CellRowResponse,
    ChunkItem,
    ChunkRequest,
    ChunkResponse,
    DocumentCellsResponse,
    DocumentEvidenceResponse,
    DocumentCreate,
    DocumentResponse,
    DocumentTokensResponse,
    DocumentUpdate,
    FactExtractionRequest,
    FactExtractionResponse,
    LinkCreate,
    LinkResponse,
    PortfolioRaptorRequest,
    RaptorLayerItem,
    RaptorRequest,
    RaptorResponse,
    SegmentExtractionItem,
    SegmentItem,
    SegmentRequest,
    SegmentResponse,
    StructuralSplitRequest,
    StructuralSplitResponse,
    StructuralSplitSectionItem,
    SubtreeResponse,
    TokenEmbeddingResponse,
)
from jmfts_core.embedding import TextTooLongError, get_embedding_service
from jmfts_core.fact_extraction import extract_facts
from jmfts_core.models.document import Document, USETYPE_FILE, USETYPE_SHEET
from jmfts_core.office.cells import (
    CELLS_READ_MAX,
    BadCellRef,
    CellRange,
    TooManyCells,
    cell_ref,
    parse_ref,
    read_region,
    render_region,
    used_range,
)
from jmfts_core.registry import expose, register_service
from jmfts_core.rendering import (
    DPI_DEFAULT,
    PDF_MEDIA_TYPE,
    PNG_MEDIA_TYPE,
    RenderTooLarge,
    UnreadablePdf,
    parse_bbox,
    render_page,
)
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.repositories.search import SearchRepository
from jmfts_core.repositories.document import (
    DocumentRepository,
    InFlightSubtreeError,
    compute_content_hash,
)
from jmfts_core.segmentation import enforce_segment_bounds, pelt_segment
from jmfts_core.settling import NO_ROLLUP, borrow_session, settle_walk
from jmfts_core.structural_splitting import split_on_headings
from jmfts_core.summarization import portfolio_raptor_summarize, raptor_summarize


class EmbedTextTooLongError(Exception):
    """Document content exceeds the embedding window (→ HTTP 400).

    Unlike every other domain error in this service (whose HTTP detail is ``str(exc)``),
    ``POST /{id}/embed`` returned a STRUCTURED detail dict. That dict is carried on
    ``http_detail`` and the REST adapter emits it verbatim, so the wire error is
    byte-identical to the hand-written route.
    """

    def __init__(self, detail: dict):
        self.http_detail = detail
        super().__init__(detail.get("message", "text too long"))


class NotASheetNode(Exception):
    """``GET /{id}/cells`` was pointed at a document that is not a worksheet (→ HTTP 409).

    409 and not 404: the document exists and the caller may read it. It is simply not a
    thing that has cells. Answering with an empty table instead would be the failure
    ``OFFICE_SPEC.md`` Part 5 rejects in the citation case for the same reason — the caller
    cannot tell "this sheet is blank" from "you asked the wrong node".
    """


class SheetSourceUnavailable(Exception):
    """The node is a sheet, and its cells still cannot be served (→ HTTP 409).

    Every instance names which of the four it is: the node carries no ``sheet.name``, it has
    no file node above it, that file node's bytes are gone, or its used range has not been
    measured (and no ``ref`` was given to stand in for one). All four are states of the tree
    rather than faults in the request, which is what makes them 409 rather than 400.
    """


class BlobUnavailable(Exception):
    """There are no stored bytes to serve for this document (→ HTTP 409).

    ``GET /{id}/blob`` when the node never held an upload — it is not a ``file`` node — or
    held one whose large object has since been unlinked; ``/image`` and ``/region`` when
    nothing above the node holds one either. 409 for ``SheetSourceUnavailable``'s reason:
    the document exists, the caller may read it, and what is missing is a state of the tree.

    **Named, and never an empty 200.** ``BlobRepository.find_blobless_documents`` exists
    because a file node whose bytes never landed is a real state, and a zero-byte body would
    be indistinguishable from an empty file that really was uploaded.
    """


class NotAPdfSource(Exception):
    """``/image`` or ``/region`` was pointed at something that is not a PDF (→ HTTP 409).

    The message names what the evidence says the bytes ARE, so the answer is "this is a
    .docx" rather than "no". Office formats reach the same two verbs through a rendition,
    which is ``OFFICE_SPEC.md`` Part 11 step 7 and ``docs/SPRINT_0_6_0.md`` Block G — not
    this step, and a renderer that quietly extracted markdown instead would be the failure
    step 29 is written to avoid, one route earlier.
    """


class RegionNotAddressable(Exception):
    """``anchor=true``, and this node's own anchor is not a rectangle on a page (→ HTTP 409).

    Four ways in, and the message says which: there is no ``source_anchor`` row; there is a
    ``source_anchor.unresolved`` row and it carries the reason; the anchor addresses cells,
    which are served as cells; or it addresses a character span, which has no geometry at
    all. 409 and not 404 for ``NotASheetNode``'s reason — the document exists and the caller
    may read it, it is simply not a thing that has a picture.
    """


# THE SECOND SPELLING OF `USETYPE_SHEET` IS GONE. This module carried its own
# `USETYPE_SHEET = "sheet"` with a comment saying why: importing it from `sheet_tasks`
# would pull in `jmfts_core.ingest_tasks`, whose module scope REGISTERS every task handler
# as a side effect, and a read verb on the query path must not change what the worker will
# dispatch merely by being imported. That reasoning was right about `sheet_tasks` and is not
# a reason to copy the string — `jmfts_core.models.document` is where the ingest usetypes
# live now (`SPRINT_JOBS.md` Phase 3 needed them below the handler modules), this module
# already imports that one, and it registers nothing. The test that asserted the two
# spellings agreed is what the copy cost; the import is the guard now.

#: The evidence row an anchor lives in, and the ``kind`` ``OFFICE_SPEC.md`` Part 5 gives a
#: worksheet region: ``{"kind": "cells", "sheet": "Q3 Pipeline", "ref": "B4:H120"}``.
#:
#: THE THIRD SPELLING OF `"source_anchor"` IS GONE TOO, and it went the way `USETYPE_SHEET`
#: above did. This was a literal, for the reason that paragraph gives — the writer is
#: :data:`jmfts_core.citation_tasks.ANCHOR_NAME` and importing it would register every task
#: handler. `jmfts_client.contracts.anchor` (Block F step 17) is now the client-side reader
#: of the same row, it is a contract package that imports neither the server nor a
#: framework, and this module already imports contracts. So the import is the guard here as
#: well; the name is kept because `tests/test_document_cells.py` pins it against the writer's
#: spelling, which is the check that matters and which the alias does not weaken.
ANCHOR_NAME = ANCHOR_ROW
ANCHOR_KIND_CELLS = "cells"

#: What ``ref_source`` reports. Three, because "the caller named this rectangle", "the node
#: was already addressed at this rectangle" and "this is everything the sheet holds" are
#: three different claims about the same string, and a consumer caching a region needs to
#: know which one it has.
CELLS_REF_REQUEST = "request"
CELLS_REF_ANCHOR = "anchor"
CELLS_REF_USED_RANGE = "used_range"


def _cells_bounds(
    document_id: int,
    evidence: dict,
    sheet: dict,
    ref: Optional[str],
) -> tuple[CellRange, str]:
    """Which rectangle ``GET /{id}/cells`` serves, and which of the three said so.

    The order is the spec's: what the caller named, then what the node is addressed at, then
    what the sheet was measured to hold. Separated from the verb because it is the part with
    a decision in it and it needs none of a session, a blob or a reader to be tested.
    """
    if ref is not None:
        return parse_ref(ref), CELLS_REF_REQUEST

    anchor = evidence.get(ANCHOR_NAME)
    if anchor is not None:
        if not isinstance(anchor, dict):
            raise SheetSourceUnavailable(
                f"Document {document_id} carries an `anchor` that is not an object; Part 5 "
                "makes an anchor a record with a `kind`, and there is no address to read "
                "out of anything else"
            )
        kind = anchor.get("kind")
        if kind != ANCHOR_KIND_CELLS:
            # Not ignored in favour of the used range: an anchor of another kind on a sheet
            # node means something wrote an address for a region that is not a region of
            # cells, and quietly serving a different rectangle would hide it.
            raise SheetSourceUnavailable(
                f"Document {document_id} carries an anchor of kind {kind!r}; Part 5 gives a "
                f"worksheet region the kind {ANCHOR_KIND_CELLS!r}. Name a region with `ref`"
            )
        named = anchor.get("sheet")
        if named is not None and named != sheet.get("name"):
            raise SheetSourceUnavailable(
                f"Document {document_id} is sheet {sheet.get('name')!r} and its anchor "
                f"addresses sheet {named!r}; an anchor records where THIS node's region came "
                "from, so the two naming different sheets is a tree that was rewritten "
                "underneath the anchor"
            )
        anchored = anchor.get("ref")
        if not anchored:
            raise SheetSourceUnavailable(
                f"Document {document_id} carries a {ANCHOR_KIND_CELLS!r} anchor with no "
                "`ref`, which is the half of it that names the region"
            )
        return parse_ref(anchored), CELLS_REF_ANCHOR

    measurements = sheet.get("measurements") or {}
    rows, cols = measurements.get("rows"), measurements.get("cols")
    if rows is None or cols is None:
        raise SheetSourceUnavailable(
            f"Document {document_id} carries no `sheet.measurements`, so `profile:sheet` has "
            "not run and this sheet's used range has not been measured. Name a region with "
            "`ref` to read one without it"
        )
    bounds = used_range(int(rows), int(cols))
    if bounds is None:
        raise SheetSourceUnavailable(
            f"Sheet {sheet.get('name')!r} was measured to hold no cells ({rows} row(s) by "
            f"{cols} column(s)), so it has no used range to serve. Name a region with `ref` "
            "to read one anyway"
        )
    return bounds, CELLS_REF_USED_RANGE


@register_service
class DocumentService:
    """Document CRUD, tree navigation, and pipeline ops over one database session."""

    def __init__(self, session: Session):
        self.session = session

    # -- CRUD ---------------------------------------------------------------------

    @expose(
        "POST",
        "/documents",
        response_model=DocumentResponse,
        errors={ValueError: 400},
        tags=["documents"],
        summary="Create a new document",
    )
    def create_document(self, request: DocumentCreate, *, dedup: bool = False) -> DocumentResponse:
        """Create a new document

        With ``dedup=true`` this is an idempotent create: if a document with identical
        content already exists under the same parent it is returned unchanged instead of
        inserting a duplicate (the replay-safe "assert this content" path Tau's ingest
        needs). Best-effort only — there is no DB uniqueness backstop, so two *concurrent*
        dedup creates can still both insert; it removes serial re-ingestion dupes, not a
        race. Content-hash idempotency is scoped to (content, parent), matching
        ``find_by_hash_and_parent``; two legitimately-identical sibling messages under
        different parents are unaffected.

        ``request.auto_index_bm25`` (default true) writes the document to the ``default``
        BM25 index inline. This operation enqueues nothing — ``index:bm25`` is a task of
        the queued ingest path — so without it a document created here reached the vector
        and full-text legs of retrieval and not the BM25 one. See the field's comment on
        ``DocumentCreate``.
        """
        repo = DocumentRepository(self.session)
        if dedup:
            existing = repo.find_by_hash_and_parent(
                compute_content_hash(request.content), request.parent_id
            )
            if existing is not None:
                # No write occurred; return the pre-existing document as-is.
                return DocumentResponse.from_document(existing)
        # Bad parent_id / sequential-ordering-on-a-root raise ValueError; over-window
        # content raises TextTooLongError (a ValueError subclass). Both were HTTP 400
        # with str(e) in the router, so @expose(errors={ValueError: 400}) reproduces
        # both — no call-site catch needed.
        doc = repo.create(
            title=request.title,
            content=request.content,
            parent_id=request.parent_id,
            usetype=request.usetype,
            structured_content=request.structured_content,
            auto_embed=request.auto_embed,
            embed_tokens=request.embed_tokens,
            sequential=request.sequential,
            event_time=request.event_time,
        )
        if request.auto_index_bm25 and doc.content:
            # Not best-effort. `index_document` returns False for a document the index
            # holds out by usetype or that tokenises to nothing, which are both correct
            # outcomes; anything it RAISES is a broken index and belongs at the call site,
            # not swallowed into a document that reports success and cannot be found.
            SearchRepository(self.session).index_document(doc.id, index_name="default")
        response = DocumentResponse.from_document(doc)
        # Commit before responding: get_db's teardown commit runs after the
        # response is sent, so a client acting on the returned id would race it.
        self.session.commit()
        return response

    @expose(
        "GET",
        "/documents",
        response_model=list[DocumentResponse],
        tags=["documents"],
        summary="List documents with optional filters",
    )
    def list_documents(
        self,
        *,
        parent_id: Optional[int] = None,
        usetype: Optional[str] = None,
        title_prefix: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DocumentResponse]:
        """List documents with optional filters"""
        repo = DocumentRepository(self.session)
        docs = repo.find(
            parent_id=parent_id,
            usetype=usetype,
            title_prefix=title_prefix,
            limit=limit,
            offset=offset,
        )
        return [DocumentResponse.from_document(d) for d in filter_readable(self.session, docs)]

    @expose(
        "GET",
        "/documents/roots",
        response_model=list[DocumentResponse],
        tags=["documents"],
        summary="Get all root documents (no parent)",
    )
    def get_root_documents(self) -> list[DocumentResponse]:
        """Get all root documents (no parent)"""
        repo = DocumentRepository(self.session)
        docs = repo.get_root_documents()
        return [DocumentResponse.from_document(d) for d in filter_readable(self.session, docs)]

    @expose(
        "GET",
        "/documents/{document_id}",
        response_model=DocumentResponse,
        errors={LookupError: 404},
        tags=["documents"],
        summary="Get a document by ID",
    )
    def get_document(
        self,
        document_id: int,
        *,
        include_embed: bool = False,
    ) -> DocumentResponse:
        """Get a document by ID"""
        repo = DocumentRepository(self.session)
        doc = repo.get(document_id)
        # Subtree RBAC: an unreadable document is indistinguishable from a missing one
        # (existence-hiding). Owner/unbound callers and ungoverned docs pass through.
        if not doc or not can_read(self.session, doc):
            raise LookupError(f"Document {document_id} not found")
        return DocumentResponse.from_document(doc, include_embed=include_embed)

    @expose(
        "PATCH",
        "/documents/{document_id}",
        response_model=DocumentResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["documents"],
        summary="Update a document's fields and/or move it under a new parent",
    )
    def update_document(self, document_id: int, request: DocumentUpdate) -> DocumentResponse:
        """Update a document's fields, and/or reparent it under a new parent.

        Passing ``parent_id`` moves the document — and its whole subtree — under that
        node: the first-class subtree-move verb (``reparent``), previously reachable only
        inside segmentation/RAPTOR. It rewrites ``path`` for the node and every descendant,
        resets ``position`` in the new sibling group, and rejects a move that would create a
        cycle (onto itself or under one of its own descendants) with a 400. It requires write
        access on both the moved node and the new parent (subtree RBAC). ``parent_id=None``
        leaves the parent unchanged — there is no move-to-root through this verb.

        Field edits and the move compose in one call: fields are applied first, then the move.
        """
        repo = DocumentRepository(self.session)
        doc = repo.update(
            document_id,
            title=request.title,
            content=request.content,
            usetype=request.usetype,
            structured_content=request.structured_content,
            re_embed=request.re_embed,
        )
        if not doc:
            raise LookupError(f"Document {document_id} not found")
        # Optional subtree move. reparent() is the only field here that rewrites path/
        # position and guards cycles; its ValueError (missing new parent / cycle) -> 400.
        # Skipped when parent_id is omitted or already the current parent (idempotent no-op).
        if request.parent_id is not None and request.parent_id != doc.parent_id:
            doc = repo.reparent(document_id, request.parent_id)
        response = DocumentResponse.from_document(doc)
        self.session.commit()
        return response

    @expose(
        "DELETE",
        "/documents/{document_id}",
        errors={LookupError: 404},
        tags=["documents"],
        summary="Delete a document and its children",
    )
    def delete_document(self, document_id: int) -> dict:
        """Delete a document and its children, then settle what the deletion released.

        The walk is not housekeeping. Removing an in-flight node removes the REASON its
        ancestors are un-settled, and nothing else will ever notice: ``settle_after_task``
        runs only when a task reaches a terminal state (5.4 step 1), and the deleted
        node's ``task_queue`` rows cascade away with it. Without this, deleting an upload
        before its ``probe`` runs leaves the parent — an ordinary settled document that
        may have been in the corpus for months — pinned at ``in_flight`` forever, and
        therefore out of the partial indexes of Part 2.2: gone from vector and full-text
        search, with no task, no error, and no recovery path. ``GET
        /ingest/file/{id}/frontier`` would report the stall and nothing would act on it.

        AFTER the commit, and in its own transactions, per 5.4 step 7 — the walk asks the
        database what is left under each ancestor, and an uncommitted delete is not yet
        gone. ``borrow_session`` gives it this session with a commit between levels, which
        is what releases each row lock before the next is taken.

        ``NO_ROLLUP`` for the same reason the worker uses it: rollup summarisation is not
        a queued task yet, and a planner nobody wrote cannot be invented here.
        """
        repo = DocumentRepository(self.session)
        # Read the parent BEFORE the delete: after it, the row that knew is gone.
        doomed = repo.get(document_id)
        parent_id = doomed.parent_id if doomed is not None else None
        if not repo.delete(document_id):
            raise LookupError(f"Document {document_id} not found")
        self.session.commit()
        if parent_id is not None:
            settle_walk(parent_id, NO_ROLLUP, session_factory=borrow_session(self.session))
        return {"deleted": document_id}

    # -- Tree navigation ----------------------------------------------------------

    @expose(
        "GET",
        "/documents/{document_id}/children",
        response_model=list[DocumentResponse],
        tags=["documents"],
        summary="Get children of a document with optional filtering",
    )
    def get_children(
        self,
        document_id: int,
        *,
        title: Optional[str] = None,
        title_prefix: Optional[str] = None,
        usetype: Optional[str] = None,
        depth: int = 1,
        limit: int = 100,
    ) -> list[DocumentResponse]:
        """Get children of a document with optional filtering"""
        repo = DocumentRepository(self.session)
        children = repo.get_children(
            document_id,
            title=title,
            title_prefix=title_prefix,
            usetype=usetype,
            depth=depth,
            limit=limit,
        )
        return [DocumentResponse.from_document(d) for d in filter_readable(self.session, children)]

    @expose(
        "GET",
        "/documents/{document_id}/ancestors",
        response_model=list[DocumentResponse],
        tags=["documents"],
        summary="Get all ancestors (path to root)",
    )
    def get_ancestors(self, document_id: int) -> list[DocumentResponse]:
        """Get all ancestors (path to root)"""
        repo = DocumentRepository(self.session)
        ancestors = repo.get_ancestors(document_id)
        return [DocumentResponse.from_document(d) for d in filter_readable(self.session, ancestors)]

    @expose(
        "GET",
        "/documents/{document_id}/siblings",
        response_model=list[DocumentResponse],
        tags=["documents"],
        summary="Get siblings of a document",
    )
    def get_siblings(
        self,
        document_id: int,
        *,
        include_self: bool = False,
    ) -> list[DocumentResponse]:
        """Get siblings of a document"""
        repo = DocumentRepository(self.session)
        siblings = repo.get_siblings(document_id, include_self=include_self)
        return [DocumentResponse.from_document(d) for d in filter_readable(self.session, siblings)]

    @expose(
        "GET",
        "/documents/{document_id}/subtree",
        response_model=SubtreeResponse,
        # 409, not 404: an in-flight root exists and is readable, it is simply not
        # finished. Collapsing it into the 404 that already covers "missing" and
        # "hidden from you" would make a tree that is mid-ingestion indistinguishable
        # from one that was never there.
        errors={LookupError: 404, InFlightSubtreeError: 409},
        tags=["documents"],
        summary="Get all documents in a subtree",
    )
    def get_subtree(
        self,
        document_id: int,
        *,
        max_depth: Optional[int] = None,
        include_in_flight: bool = False,
    ) -> SubtreeResponse:
        """Get all documents in a subtree.

        ``include_in_flight`` opts in to nodes that are not settled. Off by default: an
        unfinished subtree is not an answer, and an in-flight ROOT is a 409 rather than
        a quietly truncated tree.
        """
        repo = DocumentRepository(self.session)
        docs = repo.get_subtree(
            document_id, max_depth=max_depth, include_in_flight=include_in_flight
        )
        if not docs:
            raise LookupError(f"Document {document_id} not found")

        root = docs[0]
        # Subtree RBAC: an unreadable root is indistinguishable from missing (404); the
        # returned descendants are filtered to the ones the principal may read. Under the
        # additive model a readable root usually implies a readable subtree, but a nested
        # ACR the principal lacks can still carve out descendants, so filter regardless.
        if not can_read(self.session, root):
            raise LookupError(f"Document {document_id} not found")
        descendants = filter_readable(self.session, docs[1:] if len(docs) > 1 else [])

        return SubtreeResponse(
            root=DocumentResponse.from_document(root),
            descendants=[DocumentResponse.from_document(d) for d in descendants],
            total=len(descendants) + 1,
        )

    # -- Embedding ----------------------------------------------------------------

    @expose(
        "POST",
        "/documents/{document_id}/embed",
        errors={EmbedTextTooLongError: 400, LookupError: 404},
        tags=["documents"],
        summary="Generate embeddings for a document",
    )
    def embed_document(
        self,
        document_id: int,
        *,
        with_tokens: bool = True,
        write_importance: bool = False,
    ) -> dict:
        """Generate embeddings for a document.

        Refuses over-window content rather than embedding a prefix and reporting
        success. `token_count` is the number of *selected* token embeddings, so it was
        never a truncation signal — a caller had no way to learn that the tail of its
        document had been discarded (docs/archive/KNOWN-DEFECTS.md, D1).

        ``write_importance`` (opt-in) derives ``structured_content['importance']`` from
        the token salience computed here, instead of an LLM rating — the write side of
        the recency/importance rerank. Requires ``with_tokens``.
        """
        repo = DocumentRepository(self.session)
        try:
            result = repo.embed_document(
                document_id, with_tokens=with_tokens, write_importance=write_importance
            )
        except TextTooLongError as e:
            raise EmbedTextTooLongError(
                {
                    "error": "text_too_long",
                    "message": str(e),
                    "token_count": e.token_count,
                    "limit": e.limit,
                    "chars_total": e.chars_total,
                    "path": e.path,
                    "remedy": (
                        "Chunk the document first (POST /documents/{id}/chunk), or pass "
                        "with_tokens=false for a document vector only."
                    ),
                }
            )
        if not result:
            raise LookupError(f"Document {document_id} not found or has no content")
        response = {
            "document_id": document_id,
            "embedded": True,
            "with_tokens": with_tokens,
            "token_count": len(result.token_embeddings),
        }
        if write_importance:
            # doc is already in the session identity map from repo.embed_document; this
            # is a lookup, not a round-trip. Report what was written so the caller can see it.
            doc = repo.get(document_id)
            response["importance"] = (doc.structured_content or {}).get("importance")
        self.session.commit()
        return response

    @expose(
        "GET",
        "/documents/{document_id}/tokens",
        response_model=DocumentTokensResponse,
        errors={LookupError: 404},
        tags=["documents"],
        summary="Get token embeddings for a document (for inspection)",
    )
    def get_document_tokens(self, document_id: int) -> DocumentTokensResponse:
        """Get token embeddings for a document (for inspection)"""
        repo = DocumentRepository(self.session)
        doc = repo.get_with_embeddings(document_id)
        # Subtree RBAC: unreadable == not found (existence-hiding).
        if not doc or not can_read(self.session, doc):
            raise LookupError(f"Document {document_id} not found")

        # `embed_384` / `embed_512` no longer exist on TokenEmbedding -- settings.token_embed_dims
        # was narrowed to [256] ("Only 256, dropped 384") and the columns went with it. Reading
        # them here raised AttributeError, so this endpoint 500'd for any document that actually
        # had token embeddings: it worked only on documents with nothing to show.
        tokens = [
            TokenEmbeddingResponse(
                token_idx=tok.token_idx,
                token_text=tok.token_text or "",
                importance_score=float(tok.importance_score),
                has_embed_256=tok.embed_256 is not None,
            )
            for tok in sorted(doc.token_embeddings, key=lambda t: -t.importance_score)
        ]

        return DocumentTokensResponse(
            document_id=doc.id,
            title=doc.title,
            token_count=len(tokens),
            tokens=tokens,
        )

    @expose(
        "GET",
        "/documents/{document_id}/evidence",
        response_model=DocumentEvidenceResponse,
        errors={LookupError: 404},
        tags=["documents"],
        summary="Everything the ingest pipeline knows about a document",
    )
    def get_document_evidence(self, document_id: int) -> DocumentEvidenceResponse:
        """Every evidence row on one node, keyed by registry name.

        ``SPRINT_JOBS.md`` 13.3, and this route is the whole of what that decision gave
        back. Evidence used to be twenty-nine keys inside
        ``DocumentResponse.structured_content``; Phase 2b moved it to ``document_evidence``
        and no response stitches it back, so a client reading ``matched.patterns`` out of
        that column now reads nothing. This is where it went.

        A ROUTE AND NOT A FIELD, deliberately. A field on ``DocumentResponse`` would join
        this table on every document read and every search hit, which is the cost 13.3
        rejected option 1 for. Asking is cheap and it is one query.

        A name present with a ``null`` value and a name missing altogether are DIFFERENT
        answers (3.2): the first says an atom ran and produced nothing, the second says
        nothing has run.

        Access is the subtree RBAC every read here uses — an unreadable document is
        indistinguishable from a missing one, because evidence names the format, the reader
        and the byte count of a document the caller may not read.
        """
        doc = DocumentRepository(self.session).get(document_id)
        if not doc or not can_read(self.session, doc):
            raise LookupError(f"Document {document_id} not found")
        return DocumentEvidenceResponse(
            document_id=doc.id,
            evidence=EvidenceRepository(self.session).read_all(doc.id),
        )

    # -- Spreadsheet regions (OFFICE_SPEC.md Part 7) -------------------------------

    @expose(
        "GET",
        "/documents/{document_id}/cells",
        response_model=DocumentCellsResponse,
        errors={
            LookupError: 404,
            # Two states of the tree, not two faults in the request. See the classes.
            NotASheetNode: 409,
            SheetSourceUnavailable: 409,
            BadCellRef: 400,
            # 413, and it is the RESPONSE that would be too large. The status is the one
            # word HTTP has for "what you named is bigger than I will serve", and telling it
            # apart from the 400 a malformed `ref` gets is worth more to a caller than the
            # literal reading of the request half of RFC 9110's definition.
            TooManyCells: 413,
            ValueError: 400,
            # `OfficeStackNotInstalled: 501` was declared here and is now
            # `registry.DEFAULT_ERRORS`, which maps it for every operation. This route was
            # the only place in the tree that mapped either optional-stack error, so
            # `POST /search/*` answered a bare 500 for the same fact about the deployment
            # — `SPRINT_0_3_0.md` 13.10. The 501-not-503 reasoning moved with it.
        },
        tags=["documents"],
        summary="Read a region of a spreadsheet from the sheet node's source workbook",
    )
    def get_document_cells(
        self,
        document_id: int,
        *,
        ref: Optional[str] = None,
    ) -> DocumentCellsResponse:
        """Read a region of a spreadsheet: its values, its formulas, and it as a table.

        ``OFFICE_SPEC.md`` Part 7. Served from the SOURCE blob with ``openpyxl``; no
        rendition is involved, because a page image of a spreadsheet answers a different
        question and is rarely the one asked.

        ``ref`` is an A1-style rectangle (``B4:H120``) or a single cell (``B4``). Omitting it
        asks for the node's own region, and there are two of those:

        1. **The node's ``cells`` anchor**, when it carries one — Part 5's
           ``{"kind": "cells", "sheet": ..., "ref": "B4:H120"}``, which is the address the
           node's region was produced from.
        2. **The sheet's used range**, otherwise: ``A1`` to the last row and column
           ``profile:sheet`` MEASURED a value in. Note that **nothing in this tree writes a
           ``cells`` anchor yet** — Part 5 specifies where one lives and every writer of one
           is still unbuilt — so case 2 is what every request takes today. It is a default
           and not a placeholder: the used range is a measured fact about the sheet, it is
           the region a caller asking for "this sheet" means, and ``ref_source`` says which
           of the three the answer came from so nothing downstream has to assume.

        Refusals, none of which is a smaller or emptier region:

        * a ``ref`` that is not a rectangle, or is outside the format's own limits — 400;
        * a ``ref`` naming more than ``CELLS_READ_MAX`` cells — 413, naming the limit;
        * a document that is not a sheet node — 409, saying so;
        * a sheet whose used range has not been measured, or was measured as empty — 409,
          naming ``ref`` as the way to read one anyway.

        Access is the subtree RBAC every read here uses, applied TWICE: to the sheet node,
        and to the file node above it whose bytes are what actually gets served.
        """
        repo = DocumentRepository(self.session)
        node = repo.get(document_id)
        # Subtree RBAC: an unreadable document is indistinguishable from a missing one.
        if not node or not can_read(self.session, node):
            raise LookupError(f"Document {document_id} not found")
        if node.usetype != USETYPE_SHEET:
            raise NotASheetNode(
                f"Document {document_id} has usetype {node.usetype!r} and not "
                f"{USETYPE_SHEET!r}, so it is not a worksheet and has no cells to read"
            )

        # ONE READ FOR THE WHOLE NODE. 13.3 named this method as a server-side reader
        # that moves to the evidence API: `sheet.name`, `sheet.measurements` and the anchor
        # were three keys in one column and are three rows now, and asking for them
        # separately would be three queries where the column was one attribute access.
        evidence = EvidenceRepository(self.session).read_all(document_id)
        sheet = evidence.get("sheet") or {}
        name = sheet.get("name")
        if not name:
            raise SheetSourceUnavailable(
                f"Document {document_id} carries usetype {USETYPE_SHEET!r} with no "
                "`sheet.name`, which is the only thing that says WHICH sheet of the "
                "workbook it is"
            )

        bounds, ref_source = _cells_bounds(document_id, evidence, sheet, ref)

        if node.parent_id is None:
            raise SheetSourceUnavailable(
                f"Sheet node {document_id} has no parent, so there is no file node holding "
                "the workbook its cells would come from"
            )
        parent = repo.get(node.parent_id)
        # The bytes belong to the file node, so reading them is a read OF the file node.
        # Same 404 as above and for the same reason — a principal who may not read the
        # workbook must not learn from this verb that it is there.
        if parent is None or not can_read(self.session, parent):
            raise LookupError(f"Document {document_id} not found")
        data = BlobRepository(self.session).read_bytes(node.parent_id)
        if data is None:
            raise SheetSourceUnavailable(
                f"Document {node.parent_id} has no stored blob, so sheet {name!r} has no "
                "bytes left to read a region out of"
            )

        region = read_region(data, name, bounds=bounds, max_cells=CELLS_READ_MAX)

        return DocumentCellsResponse(
            document_id=document_id,
            sheet=name,
            ref=bounds.ref,
            ref_source=ref_source,
            columns=list(bounds.column_letters),
            rows=[CellRowResponse(row=row.index, values=list(row.values)) for row in region.rows],
            cells={
                cell_ref(row.index, column): CellNoteResponse(
                    formula=note.formula,
                    formula_shared=note.formula_shared,
                    text_forced=note.text_forced,
                )
                for row in region.rows
                for column, note in sorted(row.notes.items())
            },
            markdown=render_region(region),
            row_count=len(region.rows),
            # The AREA, which is what the limit is applied to — see the contract.
            cell_count=bounds.cells,
        )

    # -- Structural splitting -----------------------------------------------------

    @expose(
        "POST",
        "/documents/{document_id}/split",
        response_model=StructuralSplitResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["documents"],
        summary="Split a document on markdown heading boundaries, creating child documents",
    )
    def split_document(
        self,
        document_id: int,
        request: StructuralSplitRequest = StructuralSplitRequest(),
    ) -> StructuralSplitResponse:
        """Split a document on markdown heading boundaries, creating child documents.

        Step 1 of the tree-building pipeline: structural split → chunk → PELT group → summarize.

        Documents without headings produce a single child containing the full content.
        """
        repo = DocumentRepository(self.session)
        parent = repo.get(document_id)
        if not parent:
            raise LookupError(f"Document {document_id} not found")
        if not parent.content or not parent.content.strip():
            raise ValueError(f"Document {document_id} has no content to split")

        sections = split_on_headings(parent.content)
        if not sections:
            raise ValueError(f"Document {document_id} has no content to split")

        had_headings = any(s.level > 0 for s in sections)
        result_items: list[StructuralSplitSectionItem] = []

        for section in sections:
            title = section.title if section.title else parent.title
            child = repo.create(
                title=title,
                content=section.content,
                parent_id=document_id,
                usetype=request.usetype,
                structured_content={
                    "heading_level": section.level,
                    "split_from": document_id,
                },
                # `source_line` is registered evidence and the ingest chunker writes the
                # same fact, so it goes to the same place. A caller-driven split that left
                # it in the column would put one name in two stores, which is the second
                # code path SPRINT_JOBS.md Part 14 forbids.
                evidence={"source_line": section.source_line},
                auto_embed=request.auto_embed,
            )
            result_items.append(
                StructuralSplitSectionItem(
                    document_id=child.id,
                    title=title or "",
                    level=section.level,
                    content_length=len(section.content),
                    source_line=section.source_line,
                )
            )

        self.session.commit()
        return StructuralSplitResponse(
            parent_id=document_id,
            total_sections=len(result_items),
            had_headings=had_headings,
            sections=result_items,
        )

    # -- Chunking -----------------------------------------------------------------

    @expose(
        "POST",
        "/documents/{document_id}/chunk",
        response_model=ChunkResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["documents"],
        summary="Split a document's content into sentence/paragraph/token-count chunks",
    )
    def chunk_document(
        self,
        document_id: int,
        request: ChunkRequest = ChunkRequest(),
    ) -> ChunkResponse:
        """Split a document's content into sentence/paragraph/token-count chunks.

        The source document becomes the container (parent) and each chunk
        is created as a child document.  Step 2 of the tree-building pipeline:
        structural split → **chunk** → PELT group → summarize.
        """
        repo = DocumentRepository(self.session)
        parent = repo.get(document_id)
        if not parent:
            raise LookupError(f"Document {document_id} not found")
        if not parent.content or not parent.content.strip():
            raise ValueError(f"Document {document_id} has no content to chunk")

        try:
            strategy = ChunkStrategy(request.strategy)
        except ValueError:
            raise ValueError(
                f"Unknown strategy '{request.strategy}'. Use: sentence, paragraph, token_count"
            )

        chunks = chunk_text(
            parent.content,
            strategy=strategy,
            max_tokens=request.max_tokens,
            overlap=request.overlap,
            min_chunk_length=request.min_chunk_length,
            max_chars=request.max_chars,
            # Only when the children will actually be embedded. The predicate is a
            # measurement against the token/maxsim window, and a caller who asked for
            # `auto_embed=False` is not chunking for that window — holding their chunks
            # to it would split text for a constraint their request does not have.
            fits=get_embedding_service().fits_token_window if request.auto_embed else None,
        )

        if request.container_usetype is not None:
            parent.usetype = request.container_usetype

        result_items: list[ChunkItem] = []
        for chunk in chunks:
            child = repo.create(
                title=f"{parent.title or 'Untitled'} — chunk {chunk.index}",
                content=chunk.text,
                parent_id=document_id,
                usetype=request.child_usetype,
                # `chunk_index` is registered evidence for the same reason `source_line` is
                # above: the ingest chunker writes the same fact under the same name.
                evidence={"chunk_index": chunk.index},
                structured_content={
                    "char_start": chunk.char_start,
                    "char_end": chunk.char_end,
                    "strategy": request.strategy,
                    "chunked_from": document_id,
                },
                auto_embed=request.auto_embed,
            )
            result_items.append(
                ChunkItem(
                    document_id=child.id,
                    index=chunk.index,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    length=len(chunk.text),
                )
            )

        self.session.commit()
        return ChunkResponse(
            container_id=document_id,
            strategy=request.strategy,
            num_chunks=len(result_items),
            chunks=result_items,
        )

    # -- Segmentation -------------------------------------------------------------

    @expose(
        "POST",
        "/documents/{document_id}/segment",
        response_model=SegmentResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["documents"],
        summary="Detect topic boundaries among a document's children (PELT)",
    )
    def segment_document(
        self,
        document_id: int,
        request: SegmentRequest = SegmentRequest(),
    ) -> SegmentResponse:
        """Detect topic boundaries among a document's children using PELT
        change-point detection on the embedding sequence.

        Requires that the parent document has children with pre-computed embeddings.

        When ``constructive=true``, interim container documents are created for each
        segment and the chunk documents are re-parented under them.  The
        ``min_segment`` / ``max_segment`` parameters control post-PELT merging and
        splitting so every container holds a reasonable number of children.
        """
        repo = DocumentRepository(self.session)
        parent = repo.get(document_id)
        if not parent:
            raise LookupError(f"Document {document_id} not found")

        # Fetch immediate children ordered by creation time (sequential order)
        children = repo.get_children(document_id, depth=1, limit=10000)
        if not children:
            raise ValueError(f"Document {document_id} has no children to segment")

        # Collect embeddings, skipping children without them
        embedded_children = [(c.id, c.embed) for c in children if c.embed is not None]
        if len(embedded_children) < 2:
            raise ValueError(
                f"Need at least 2 embedded children for segmentation, "
                f"found {len(embedded_children)}"
            )

        child_ids = [cid for cid, _ in embedded_children]
        embeddings = np.array([list(emb) for _, emb in embedded_children])

        segments = pelt_segment(
            embeddings,
            child_ids,
            penalty=request.penalty,
            min_size=request.min_size,
        )

        # In constructive mode, enforce bounds and materialise the tree
        if request.constructive:
            segments = enforce_segment_bounds(
                segments,
                min_segment=request.min_segment,
                max_segment=request.max_segment,
            )

            result_items: list[SegmentItem] = []
            for idx, seg in enumerate(segments, start=1):
                container = repo.create(
                    title=f"Segment {idx}",
                    content=None,
                    parent_id=document_id,
                    usetype="segment",
                    structured_content={
                        "segment_index": idx,
                        "source_parent_id": document_id,
                    },
                    auto_embed=False,
                )
                for cid in seg.child_ids:
                    repo.reparent(cid, container.id)

                result_items.append(
                    SegmentItem(
                        start=seg.start,
                        end=seg.end,
                        child_ids=seg.child_ids,
                        size=seg.end - seg.start,
                        container_id=container.id,
                    )
                )

            self.session.commit()
            return SegmentResponse(
                document_id=document_id,
                total_children=len(embedded_children),
                num_segments=len(result_items),
                constructive=True,
                segments=result_items,
            )

        # Diagnostic mode (default) — return boundaries only
        return SegmentResponse(
            document_id=document_id,
            total_children=len(embedded_children),
            num_segments=len(segments),
            segments=[
                SegmentItem(
                    start=s.start,
                    end=s.end,
                    child_ids=s.child_ids,
                    size=s.end - s.start,
                )
                for s in segments
            ],
        )

    # -- RAPTOR hierarchical summarization ----------------------------------------

    @expose(
        "POST",
        "/documents/{document_id}/raptor",
        response_model=RaptorResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["documents"],
        summary="Run RAPTOR hierarchical summarization on a document's children",
    )
    async def raptor_summarize_document(
        self,
        document_id: int,
        request: RaptorRequest = RaptorRequest(),
    ) -> RaptorResponse:
        """Run RAPTOR hierarchical summarization on a document's children.

        Requires that the parent document has children with pre-computed embeddings
        (e.g., output of split → chunk → segment pipeline).

        Uses Leiden community detection with dual-adaptive k/gamma to cluster children,
        then calls the LLM to summarize each cluster. Recurses on summaries until
        convergence or max_depth is reached. Bridge links are created for cross-cluster
        context via DocumentLink (link_type="bridge").
        """
        repo = DocumentRepository(self.session)
        parent = repo.get(document_id)
        if not parent:
            raise LookupError(f"Document {document_id} not found")

        children = repo.get_children(document_id, depth=1, limit=10000)
        embedded = [c for c in children if c.embed is not None]
        if len(embedded) < 2:
            raise ValueError(
                f"Need at least 2 embedded children for RAPTOR, "
                f"found {len(embedded)} (total children: {len(children)})"
            )

        result = await raptor_summarize(
            document_id=document_id,
            session=self.session,
            max_depth=request.max_depth,
            min_cluster_size=request.min_cluster_size,
            llm_model=request.llm_model,
            max_summary_tokens=request.max_summary_tokens,
            usetype_filter=request.usetype_filter,
        )

        self.session.commit()

        return RaptorResponse(
            root_id=result.root_id,
            layers=[
                RaptorLayerItem(
                    layer=lr.layer,
                    clusters=lr.clusters,
                    summary_ids=lr.summary_ids,
                    bridge_links_created=lr.bridge_links_created,
                )
                for lr in result.layers
            ],
            total_summaries=result.total_summaries,
            total_bridge_links=result.total_bridge_links,
        )

    @expose(
        "POST",
        "/documents/{document_id}/raptor/portfolio",
        response_model=RaptorResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["documents"],
        summary="Run cross-document RAPTOR over report-level summaries under a portfolio root",
    )
    async def raptor_portfolio_summarize(
        self,
        document_id: int,
        request: PortfolioRaptorRequest = PortfolioRaptorRequest(),
    ) -> RaptorResponse:
        """Run cross-document RAPTOR over report-level summaries under a portfolio root.

        Collects usetype='summary' children from each report document under the
        portfolio, then clusters them into portfolio-level themes via Leiden +
        LLM summarization. Requires that per-document RAPTOR has already been run
        on each report.
        """
        repo = DocumentRepository(self.session)
        parent = repo.get(document_id)
        if not parent:
            raise LookupError(f"Document {document_id} not found")

        # Check that there are report children with summaries
        reports = repo.get_children(document_id, depth=1, limit=10000)
        if not reports:
            raise ValueError(f"Portfolio {document_id} has no child report documents")

        # Count available summaries across reports
        summary_count = 0
        for report in reports:
            summaries = repo.get_children(report.id, usetype="summary", depth=1, limit=10000)
            summary_count += sum(1 for s in summaries if s.embed is not None)

        if summary_count < 2:
            raise ValueError(
                f"Need at least 2 embedded report-level summaries for portfolio RAPTOR, "
                f"found {summary_count} across {len(reports)} reports"
            )

        result = await portfolio_raptor_summarize(
            portfolio_id=document_id,
            session=self.session,
            max_depth=request.max_depth,
            min_cluster_size=request.min_cluster_size,
            llm_model=request.llm_model,
            max_summary_tokens=request.max_summary_tokens,
        )

        self.session.commit()

        return RaptorResponse(
            root_id=result.root_id,
            layers=[
                RaptorLayerItem(
                    layer=lr.layer,
                    clusters=lr.clusters,
                    summary_ids=lr.summary_ids,
                    bridge_links_created=lr.bridge_links_created,
                )
                for lr in result.layers
            ],
            total_summaries=result.total_summaries,
            total_bridge_links=result.total_bridge_links,
        )

    # -- Fact extraction (#58) ----------------------------------------------------

    @expose(
        "POST",
        "/documents/{document_id}/extract-facts",
        response_model=FactExtractionResponse,
        errors={LookupError: 404},
        tags=["documents"],
        summary="Extract knowledge triples from a document's segments via LLM",
    )
    async def extract_facts_endpoint(
        self,
        document_id: int,
        request: FactExtractionRequest = FactExtractionRequest(),
    ) -> FactExtractionResponse:
        """Extract knowledge triples from a document's segments via LLM.

        Processes all child segments/chunks (and optionally RAPTOR summaries),
        extracts structured (subject, predicate, object) triples, resolves entities
        against existing documents, and creates Triple records with temporal metadata.

        Requires a document with children that have text content (e.g., output of
        the split → chunk → segment pipeline).
        """
        repo = DocumentRepository(self.session)
        parent = repo.get(document_id)
        if not parent:
            raise LookupError(f"Document {document_id} not found")

        result = await extract_facts(
            document_id=document_id,
            session=self.session,
            llm_model=request.llm_model,
            max_facts=request.max_facts,
            confidence_threshold=request.confidence_threshold,
            include_summaries=request.include_summaries,
        )

        self.session.commit()

        return FactExtractionResponse(
            root_document_id=result.root_document_id,
            documents_processed=result.documents_processed,
            total_triples_created=result.total_triples_created,
            total_skipped=result.total_skipped,
            entities_created=result.entities_created,
            entities_resolved=result.entities_resolved,
            predicates_created=result.predicates_created,
            extractions=[
                SegmentExtractionItem(
                    source_document_id=ext.source_document_id,
                    triples_created=len(ext.created_triple_ids),
                    skipped=ext.skipped_count,
                    triple_ids=ext.created_triple_ids,
                    errors=ext.errors,
                )
                for ext in result.extractions
            ],
            errors=result.errors,
        )

    # -- Links --------------------------------------------------------------------

    @expose(
        "POST",
        "/documents/{document_id}/links",
        response_model=LinkResponse,
        errors={ValueError: 400, LookupError: 404},
        tags=["documents"],
        summary="Create a link from this document to another",
    )
    def create_link(self, document_id: int, request: LinkCreate) -> LinkResponse:
        """Create a link from this document to another.

        Subtree RBAC: WRITE on the source, READ on the target — ``SPRINT_0_6_0.md`` Block A
        step 1, answering Part 4 question 4.1. Until that step this method performed no
        access check of any kind: its whole validation was the ``source_id`` mismatch
        below, while ``get_links`` directly underneath it called ``can_read``. Reads were
        gated and writes were not, in adjacent methods of one class.

        The gate is also inside ``repo.create_link``, which is where every OTHER writer of
        an edge goes through it. Both, because this one wants the 404 spelled against the
        document in the URL before ``source_id`` is even compared — an unreadable
        ``document_id`` must look missing here exactly as it does in ``get_links``, not
        like a 400 about a field the caller got right.
        """
        repo = DocumentRepository(self.session)
        source = repo.get(document_id)
        if source is None or not can_read(self.session, source):
            raise LookupError(f"Document {document_id} not found")
        if request.source_id != document_id:
            raise ValueError("source_id must match document_id in URL")
        require_edge_write(self.session, request.source_id, request.target_id)

        link = repo.create_link(
            source_id=request.source_id,
            target_id=request.target_id,
            link_type=request.link_type,
            score=request.score,
            metadata=request.metadata,
        )
        response = LinkResponse(
            id=link.id,
            source_id=link.source_id,
            target_id=link.target_id,
            link_type=link.link_type,
            score=link.score,
            metadata=link.link_metadata or {},
            created_at=link.created_at,
        )
        self.session.commit()
        return response

    @expose(
        "GET",
        "/documents/{document_id}/links",
        response_model=list[LinkResponse],
        errors={LookupError: 404},
        tags=["documents"],
        summary="Get links for a document",
    )
    def get_links(
        self,
        document_id: int,
        *,
        direction: str = "both",
        link_type: Optional[str] = None,
    ) -> list[LinkResponse]:
        """Get links for a document"""
        repo = DocumentRepository(self.session)
        # Subtree RBAC: an unreadable incident document is indistinguishable from missing;
        # repo.get_links additionally hides edges pointing to unreadable other endpoints.
        doc = repo.get(document_id)
        if not doc or not can_read(self.session, doc):
            raise LookupError(f"Document {document_id} not found")
        links = repo.get_links(document_id, direction=direction, link_type=link_type)
        return [
            LinkResponse(
                id=link.id,
                source_id=link.source_id,
                target_id=link.target_id,
                link_type=link.link_type,
                score=link.score,
                metadata=link.link_metadata or {},
                created_at=link.created_at,
            )
            for link in links
        ]

    @expose(
        "DELETE",
        "/documents/{document_id}/links/{link_id}",
        errors={LookupError: 404},
        tags=["documents"],
        summary="Delete a link incident to this document",
    )
    def delete_link(self, document_id: int, link_id: int) -> dict:
        """Delete a link incident to this document.

        The retract leg for the append-only link graph. Deletes any link type (RAPTOR
        ``bridge`` edges included) as long as it touches ``document_id``; the deleted
        ``link_type`` is echoed back so a caller can tell what it removed.

        Subtree RBAC, ``SPRINT_0_6_0.md`` Block A step 1: an unreadable ``document_id`` is
        indistinguishable from missing here (the same 404 ``get_links`` gives), and which
        END of the link must be writable is ``require_edge_delete``'s decision, applied
        inside ``repo.delete_link``. Before this step neither check existed — the method
        raised ``LookupError`` only when nothing matched.
        """
        repo = DocumentRepository(self.session)
        doc = repo.get(document_id)
        if doc is None or not can_read(self.session, doc):
            raise LookupError(f"Document {document_id} not found")
        link_type = repo.delete_link(link_id, incident_to=document_id)
        if link_type is None:
            raise LookupError(f"Link {link_id} not found on document {document_id}")
        self.session.commit()
        return {"deleted": True, "id": link_id, "link_type": link_type}

    # -- Bytes out: the file, a page, a rectangle (OFFICE_SPEC.md Part 7) ----------
    #
    # `OFFICE_SPEC.md` Part 11 step 3, which that document scheduled BEFORE any office
    # format is read — "steps 1 to 3 deliver the headline feature end to end, a search
    # result that can show you the page and the rectangle it came from, using formats JMFTS
    # already ingests, with no new dependency of any tier" — and which had no code until
    # now. Steps 1 and 2 shipped in 0.4.0 (`ADVISORY_TASK_TYPES`, and `citation_tasks.py`
    # writing the anchors these routes read back). `docs/SPRINT_0_6_0.md` Block F steps 20,
    # 21 and 22.
    #
    # These are the FIRST callers of `ExposeSpec.media_type` (`registry.py:95`) and
    # `wiring.py::_binary_response` outside that mechanism's own tests. Three consequences
    # worth reading before adding a fourth:
    #
    #   * the method returns a `BinaryPayload`, never bare `bytes` — the adapter raises on
    #     anything else, because FastAPI's encoder would render bytes as base64 in quotes;
    #   * the media type on the WIRE is the payload's, and the one on the spec is what
    #     OpenAPI declares. `/blob` serves whatever was uploaded, so it can only declare
    #     `application/octet-stream`;
    #   * `download=True` is `/blob`'s alone. A rendered page is derived from a document
    #     rather than being one, and naming it invites somebody to treat it as the source
    #     (`jmfts_client/contracts/binary.py`, the `filename` field).
    #
    # ACCESS IS THE SUBTREE RBAC EVERY READ HERE USES, and it is applied to the node the
    # caller named AND to the file node whose bytes are what actually gets served — the
    # same two checks `get_document_cells` makes, for the same reason: a principal who may
    # not read the workbook must not learn from a rendering verb that it is there. An
    # unreadable document is indistinguishable from a missing one.

    def _readable(self, document_id: int) -> Document:
        """The node, or a 404 that does not say whether it exists."""
        node = DocumentRepository(self.session).get(document_id)
        if not node or not can_read(self.session, node):
            raise LookupError(f"Document {document_id} not found")
        return node

    def _pdf_bytes(self, node: Document) -> bytes:
        """The PDF ``node``'s region lives in: its own bytes, or its nearest file node's.

        A chunk is what carries an anchor and a file node is what carries the bytes, so a
        verb that takes an anchor has to walk from one to the other. ``Document.path`` is
        ancestor ids root-first and excludes self, so the nearest file node above is the
        LAST of them that is one — nearest and not first, because a file ingested into a
        subtree under another file (a conversation attachment, a fetched PDF under a page)
        would otherwise resolve to the outer one's bytes.
        """
        repo = DocumentRepository(self.session)
        if node.usetype == USETYPE_FILE:
            file_node = node
        else:
            above = [a for a in repo.get_ancestors(node.id) if a.usetype == USETYPE_FILE]
            if not above:
                raise BlobUnavailable(
                    f"Document {node.id} has usetype {node.usetype!r} and no {USETYPE_FILE!r} "
                    "node above it, so there are no stored bytes anywhere on its path to "
                    "render a page out of"
                )
            file_node = above[-1]
        # The bytes belong to the file node, so reading them is a read OF the file node.
        if not can_read(self.session, file_node):
            raise LookupError(f"Document {node.id} not found")

        blobs = BlobRepository(self.session)
        if blobs.get(file_node.id) is None:
            raise BlobUnavailable(
                f"Document {file_node.id} holds no stored bytes; `find_blobless_documents` "
                "exists because a file node whose bytes never landed, or whose large object "
                "was unlinked, is a real state of this tree"
            )

        # `detected_mime` and NOT the blob row's served `mime_type`, and the difference is
        # the one `ingest_service.py:773` states: the bytes are evidence and the header is a
        # claim. A real PDF always detects by its magic bytes (`probe.py:59`), so absent or
        # different here is evidence that these are not one — and believing a declaration
        # instead would hand `pymupdf` a `.docx` and turn a clear refusal into a parse error.
        found = EvidenceRepository(self.session).read(file_node.id, "file") or {}
        detected = found.get("detected_mime")
        if detected != PDF_MEDIA_TYPE:
            raise NotAPdfSource(
                f"Document {file_node.id} is {detected or 'bytes nothing recognised'} "
                f"(declared {found.get('declared_mime') or 'nothing'}), not "
                f"{PDF_MEDIA_TYPE}. Rendering an office format goes through a rendition, "
                "which is OFFICE_SPEC.md Part 11 step 7 and has no code here yet"
            )

        # `read_bytes` raises `BlobLeakError` when the row is there and the large object is
        # not. That is left UNMAPPED and reaches a 500 on purpose: the row asserts the bytes
        # exist, so its being wrong is a corruption report and not a state to describe.
        return blobs.read_bytes(file_node.id)

    def _pdf_anchor(self, document_id: int) -> PdfAnchor:
        """This node's own ``source_anchor``, as a page and a rectangle — or why it is not one.

        ``OFFICE_SPEC.md`` Part 5. The four refusals are four different facts and the caller
        can act on each: wait for `citation`, read the recorded reason, ask ``/cells``, or
        highlight the text it is already showing.
        """
        evidence = EvidenceRepository(self.session).read_all(document_id)
        row = evidence.get(ANCHOR_NAME)
        if row is None:
            # TWO ROWS, NEVER ONE WITH A NULL (`evidence.py:453`: the unresolved row is
            # "present exactly when `anchor` is not"). "Nothing has placed this passage" and
            # "this passage could not be placed, because X" are different answers, and a
            # verb that gave one message for both would be hiding the second.
            unresolved = evidence.get(ANCHOR_UNRESOLVED_ROW)
            if isinstance(unresolved, dict):
                raise RegionNotAddressable(
                    f"Document {document_id} has no rectangle: "
                    f"{unresolved.get('reason')} (code {unresolved.get('code')!r})"
                )
            raise RegionNotAddressable(
                f"Document {document_id} carries no {ANCHOR_NAME!r} evidence row, so it is "
                "not addressed at any region of its source. `citation` is what writes one "
                "and it runs over a PDF text layer; name a `page` and a `bbox` to render a "
                "rectangle this node does not claim"
            )
        try:
            anchor = parse_anchor(row)
        except UnknownAnchorKind as exc:
            # The contract raises rather than returning None, and this keeps that: an
            # anchor nobody can interpret is not a highlight that is merely missing.
            raise RegionNotAddressable(f"Document {document_id}: {exc}") from exc

        if isinstance(anchor, CellsAnchor):
            # A SPREADSHEET REGION IS SERVED AS CELLS, NOT AS AN IMAGE. Part 7 gives it its
            # own verb and `docs/SPRINT_0_6_0.md` step 22 says why: for a sheet, JSON cells
            # are the better answer than a picture of cells — they are selectable,
            # summable and diffable, and `/cells` serves formulas beside the values, which
            # no raster can. 409 is this file's house status for "wrong kind of node"
            # (`NotASheetNode`), and the message carries the route that does answer, because
            # an error that names the fix costs the caller one call instead of a search.
            raise RegionNotAddressable(
                f"Document {document_id} is addressed at cells {anchor.ref!r} of sheet "
                f"{anchor.sheet!r}, and a spreadsheet region is served as cells rather than "
                f"as a picture of cells: GET /documents/{document_id}/cells"
            )
        if isinstance(anchor, SpanAnchor):
            # Same treatment, different absence: a character range in the extracted markdown
            # has NO geometry to crop to. There is nothing to draw it on until something
            # paginates the document, which for an office format is a rendition (Part 6).
            # The feature for this node is highlighting the text a renderer is already
            # showing, which is IC-9's other surface and not a picture.
            raise RegionNotAddressable(
                f"Document {document_id} is addressed at characters "
                f"{anchor.char_start}-{anchor.char_end} of its extracted markdown, which is "
                "a range in text and has no rectangle on any page. Nothing has paginated "
                "this document, so there is no picture of that span to crop"
            )
        return anchor

    @expose(
        "GET",
        "/documents/{document_id}/blob",
        # The weaker claim, and deliberately so: this route serves whatever was uploaded, so
        # its real content type is a property of the row and travels on the payload.
        media_type="application/octet-stream",
        errors={LookupError: 404, BlobUnavailable: 409},
        tags=["documents"],
        summary="The bytes this document was ingested from, exactly as they were received",
    )
    def get_document_blob(self, document_id: int) -> BinaryPayload:
        """The original uploaded bytes, for re-hosting and for download.

        ``INGEST_SPEC.md`` Part 9 stores them as a Postgres large object and
        ``jmfts_core/repositories/blob.py`` is the only door to one; until now its three
        callers were all task handlers and nothing under ``jmfts_core/rest/`` could reach a
        blob at all. This is the read verb for them.

        **Served as an attachment, with the uploader's own filename.** The reason this route
        exists is that somebody wants the file back — to re-host it, to open it in the
        application that wrote it, or to check it against what they sent — and all three
        want a file on disk rather than a tab full of bytes. The filename comes from the
        ``file`` evidence block, which is the record of what was received and never changes
        after upload (spec 3.3).

        **``attachment`` is also the only safe disposition here**, and that is the half that
        is not a preference. This route serves uploaded bytes under the uploader's own
        content type, from the appliance's own origin — an ingested ``.html`` or ``.svg``
        served ``inline`` would execute in it. A browser does not render an attachment, so
        the one header that makes the route useful is the one that makes it safe, and a
        future ``?inline=true`` would have to answer this before it could exist.

        The content type is the blob row's ``mime_type``. That is the value
        ``ingest_service.py:776`` resolved AT UPLOAD from the detection and the declaration,
        with a comment saying it "decides only what the stored object is served back as" —
        which is this. ``file.detected_mime`` is the stronger evidence and is what
        ``/image`` tests against, but it is nullable (``probe.py:271``: both it and
        ``detected_by`` are None when nothing recognised the bytes) and a payload's media
        type is not, so serving from it would mean a second resolution rule here, decided
        with less than the uploader had.

        A document with no blob is a named 409 and never an empty 200 — see
        :class:`BlobUnavailable`.
        """
        node = self._readable(document_id)
        blobs = BlobRepository(self.session)
        row = blobs.get(document_id)
        if row is None:
            raise BlobUnavailable(
                f"Document {document_id} has usetype {node.usetype!r} and holds no stored "
                f"bytes. Only a {USETYPE_FILE!r} node carries an upload, and one whose large "
                "object has been unlinked carries none either"
            )
        found = EvidenceRepository(self.session).read(document_id, "file") or {}
        return BinaryPayload(
            content=blobs.read_bytes(document_id),
            media_type=row.mime_type,
            filename=found.get("filename"),
            download=True,
        )

    @expose(
        "GET",
        "/documents/{document_id}/image",
        media_type=PNG_MEDIA_TYPE,
        errors={
            LookupError: 404,
            BlobUnavailable: 409,
            NotAPdfSource: 409,
            RegionNotAddressable: 409,
            UnreadablePdf: 409,
            # It is the RESPONSE that would be too large — `TooManyCells`' reasoning above.
            RenderTooLarge: 413,
            # `rendering.BadRenderRequest` is a ValueError, so this one entry covers it and
            # the parameter-combination refusals below without listing two names for 400.
            ValueError: 400,
        },
        tags=["documents"],
        summary="One page of the PDF this node came from, rendered as a PNG",
    )
    def get_document_image(
        self,
        document_id: int,
        *,
        page: Optional[int] = None,
        dpi: int = DPI_DEFAULT,
    ) -> BinaryPayload:
        """One page as a PNG. ``OFFICE_SPEC.md`` Part 7.

        ``page`` is **0-BASED**, matching ``PdfAnchor.page``, ``page_offsets`` and
        ``pages_with_tables``. Nothing here adds one: a viewer that labels pages for a human
        does that, because the record is not what a human reads, and an endpoint that
        silently offset it would put every stored anchor one page out.

        Omitting ``page`` asks for the page this node is ADDRESSED at, and there are two
        readings of that:

        1. **A file node is the whole document**, so its first page is page 0. That is the
           only reading of "a picture of this document" and it hides nothing.
        2. **Anything else names its page through its own anchor** — a chunk, a section. A
           node with no anchor gets a 409 that says so rather than page 0, because a picture
           of the wrong page is the failure Part 5 calls worse than no picture at all.

        ``dpi`` defaults to 150 and is BOUNDED — see :mod:`jmfts_core.rendering` for the two
        limits and why there are two. Both refuse rather than clamp, and the refusal names
        the largest density that would fit.

        **No ``highlight`` parameter, and Part 7's table lists one.** The overlay is IC-9 in
        ``docs/SPRINT_0_6_0.md`` — one client component that takes a ``source_anchor`` and a
        rendered surface and draws the box, shared by ``pdf-page``, ``image`` and
        ``sheet-region``. Burning the rectangle into the pixels here would make the page
        uncacheable per-anchor, make the box unselectable and unadjustable, and give the
        tree a second place where a rectangle is drawn — which is exactly how the three
        surfaces end up disagreeing about which corner the origin is in.
        """
        node = self._readable(document_id)
        if page is None:
            page = 0 if node.usetype == USETYPE_FILE else self._pdf_anchor(document_id).page
        return BinaryPayload(
            content=render_page(self._pdf_bytes(node), page=page, dpi=dpi),
            media_type=PNG_MEDIA_TYPE,
        )

    @expose(
        "GET",
        "/documents/{document_id}/region",
        media_type=PNG_MEDIA_TYPE,
        errors={
            LookupError: 404,
            BlobUnavailable: 409,
            NotAPdfSource: 409,
            RegionNotAddressable: 409,
            UnreadablePdf: 409,
            RenderTooLarge: 413,
            ValueError: 400,
        },
        tags=["documents"],
        summary="A rectangle of a page, from this node's own anchor or from explicit bounds",
    )
    def get_document_region(
        self,
        document_id: int,
        *,
        anchor: bool = False,
        page: Optional[int] = None,
        bbox: Optional[str] = None,
        dpi: int = DPI_DEFAULT,
    ) -> BinaryPayload:
        """The picture of the words that matched. ``OFFICE_SPEC.md`` Part 7.

        This is the verb the user story is about: a search result comes back, and the caller
        wants to see the passage where it sits on the page rather than take the text on
        trust. Two ways to name the rectangle, and they are mutually exclusive:

        * ``anchor=true`` — the node's OWN ``source_anchor`` evidence row, which is what
          ``citation`` wrote when it placed this chunk. No coordinates to carry, and it is
          the form a client has after a search hit.
        * ``page`` and ``bbox=x0,y0,x1,y1`` — an explicit rectangle in PDF points, in
          ``pymupdf``'s ``Rect`` order, which is the order a ``source_anchor`` bbox is
          already in. For a caller that has adjusted a box, or is cropping something no node
          claims.

        Naming both is a 400 and not a precedence rule. There is no way to tell which was
        meant, and serving the wrong rectangle confidently is the failure Part 5 says is
        worse than serving none — the reader cannot tell.

        A rectangle that hangs over the edge of the page is intersected with it; one that
        misses the page entirely is a 400 naming the media box, because there is no picture
        of it. An anchor with ``continues`` covers only the page the passage BEGINS on, which
        is what ``anchor_for_span`` wrote and deliberately so; the overlay is what says
        "continues on p. 4".

        **The rectangle in points is not returned beside the image**, and Part 7's sketch has
        it doing so. A ``BinaryPayload`` is bytes and their type, so a second value would
        need a header or a JSON envelope around a PNG, and neither is a shape this tree has.
        It is not lost: it is the ``source_anchor`` row that ``GET /documents/{id}/evidence``
        already serves and that IC-4 puts directly on a search hit, so a caller doing its own
        rendering has the coordinates before it calls this at all — which is one call earlier
        than reading them off the response would be.
        """
        node = self._readable(document_id)
        named = page is not None or bbox is not None
        if anchor and named:
            raise ValueError(
                "`anchor=true` asks for this node's own rectangle and `page`/`bbox` names "
                "another. Pass one or the other: there is no way to tell which was meant, "
                "and a confident picture of the wrong rectangle is worse than a refusal"
            )
        if anchor:
            found = self._pdf_anchor(document_id)
            page, box = found.page, found.bbox
        elif page is not None and bbox is not None:
            box = parse_bbox(bbox)
        else:
            raise ValueError(
                "name a rectangle with `page` and `bbox=x0,y0,x1,y1` in PDF points, or ask "
                "for this node's own with `anchor=true`. A `page` with no `bbox` is a whole "
                "page, and GET /documents/{document_id}/image is the verb that serves one"
            )
        return BinaryPayload(
            content=render_page(self._pdf_bytes(node), page=page, dpi=dpi, clip=box),
            media_type=PNG_MEDIA_TYPE,
        )
