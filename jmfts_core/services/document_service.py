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

from jmfts_core.access import can_read, filter_readable
from jmfts_core.chunking import ChunkStrategy, chunk_text
from jmfts_core.contracts.document import (
    ChunkItem,
    ChunkRequest,
    ChunkResponse,
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
from jmfts_core.registry import expose, register_service
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
        document had been discarded (docs/KNOWN-DEFECTS.md, D1).

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
                    "source_line": section.source_line,
                    "split_from": document_id,
                },
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
                structured_content={
                    "chunk_index": chunk.index,
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
                        "child_count": seg.end - seg.start,
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
        errors={ValueError: 400},
        tags=["documents"],
        summary="Create a link from this document to another",
    )
    def create_link(self, document_id: int, request: LinkCreate) -> LinkResponse:
        """Create a link from this document to another"""
        if request.source_id != document_id:
            raise ValueError("source_id must match document_id in URL")

        repo = DocumentRepository(self.session)
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
        """
        repo = DocumentRepository(self.session)
        link_type = repo.delete_link(link_id, incident_to=document_id)
        if link_type is None:
            raise LookupError(f"Link {link_id} not found on document {document_id}")
        self.session.commit()
        return {"deleted": True, "id": link_id, "link_type": link_type}
