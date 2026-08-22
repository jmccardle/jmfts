"""IndexService — search-index management operations, transport-neutral.

Logic lifted verbatim from ``api/routers/indexes.py`` so the behaviour is identical; the
only single-sourcing change is that ``SearchIndex -> IndexResponse`` now goes through the
one ``IndexResponse.from_index`` converter (was the router-local ``index_to_response``).

Domain → HTTP mapping is declared per-op in ``@expose(errors=...)`` and keyed by EXCEPTION
TYPE, so the three hand-written statuses the router raised are reproduced without a
call-site check:

- ``LookupError``          → 404 (index not found), detail ``"Index '<name>' not found"``.
- ``IndexConflictError``   → 409 (index name already exists), detail
  ``"Index '<name>' already exists"``.
- ``ValueError``           → 400 (document not found / has no content), detail
  ``"Document <id> not found or has no content"``.

The refresh route's 404 keeps its ORIGINAL detail: the repo's ``refresh_index`` returns
``{"error": ...}`` on a missing index, and that error string is raised verbatim as the
``LookupError`` detail — byte-identical to the old ``HTTPException(404, result["error"])``.

All detail strings are preserved verbatim.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from jmfts_client.contracts.index import IndexCreate, IndexResponse
from jmfts_core.registry import expose, register_service
from jmfts_core.repositories.search import SearchRepository


class IndexConflictError(Exception):
    """An index with the requested name already exists (→ HTTP 409)."""


@register_service
class IndexService:
    """Search-index management over a single database session."""

    def __init__(self, session: Session):
        self.session = session

    # -- index CRUD ---------------------------------------------------------------

    @expose(
        "GET",
        "/indexes",
        response_model=list[IndexResponse],
        tags=["indexes"],
        summary="List all search indexes",
    )
    def list_indexes(self) -> list[IndexResponse]:
        """List all search indexes"""
        repo = SearchRepository(self.session)
        indexes = repo.list_indexes()
        return [IndexResponse.from_index(idx) for idx in indexes]

    @expose(
        "POST",
        "/indexes",
        response_model=IndexResponse,
        errors={IndexConflictError: 409},
        tags=["indexes"],
        summary="Create a new search index",
    )
    def create_index(self, request: IndexCreate) -> IndexResponse:
        """Create a new search index"""
        repo = SearchRepository(self.session)

        # Check if exists
        existing = repo.get_index(request.name)
        if existing:
            raise IndexConflictError(f"Index '{request.name}' already exists")

        index = repo.create_index(
            name=request.name,
            description=request.description,
            config=request.config,
        )
        response = IndexResponse.from_index(index)
        # Commit before responding: get_db's teardown commit runs after the
        # response is sent, so a client acting on the result would race it.
        self.session.commit()
        return response

    @expose(
        "GET",
        "/indexes/{index_name}",
        response_model=IndexResponse,
        errors={LookupError: 404},
        tags=["indexes"],
        summary="Get a search index by name",
    )
    def get_index(self, index_name: str) -> IndexResponse:
        """Get a search index by name"""
        repo = SearchRepository(self.session)
        index = repo.get_index(index_name)
        if not index:
            raise LookupError(f"Index '{index_name}' not found")
        return IndexResponse.from_index(index)

    @expose(
        "DELETE",
        "/indexes/{index_name}",
        errors={LookupError: 404},
        tags=["indexes"],
        summary="Delete a search index",
    )
    def delete_index(self, index_name: str) -> dict:
        """Delete a search index"""
        repo = SearchRepository(self.session)
        index = repo.get_index(index_name)
        if not index:
            raise LookupError(f"Index '{index_name}' not found")

        # Cascade delete handled by FK constraints
        self.session.delete(index)
        self.session.commit()
        return {"deleted": index_name}

    # -- root management (subtrees) -----------------------------------------------

    @expose(
        "GET",
        "/indexes/{index_name}/roots",
        errors={LookupError: 404},
        tags=["indexes"],
        summary="Get root document IDs for an index",
    )
    def get_index_roots(self, index_name: str) -> dict:
        """Get root document IDs for an index"""
        repo = SearchRepository(self.session)
        index = repo.get_index(index_name)
        if not index:
            raise LookupError(f"Index '{index_name}' not found")

        root_ids = repo.get_index_roots(index_name)
        return {"index": index_name, "roots": root_ids}

    @expose(
        "POST",
        "/indexes/{index_name}/roots",
        errors={LookupError: 404},
        tags=["indexes"],
        summary="Add a document subtree to an index",
    )
    def add_root_to_index(self, index_name: str, *, root_document_id: int) -> dict:
        """Add a document subtree to an index"""
        repo = SearchRepository(self.session)

        if not repo.add_root_to_index(index_name, root_document_id):
            raise LookupError(f"Index '{index_name}' not found")

        self.session.commit()
        return {"index": index_name, "added_root": root_document_id}

    @expose(
        "DELETE",
        "/indexes/{index_name}/roots/{root_document_id}",
        errors={LookupError: 404},
        tags=["indexes"],
        summary="Remove a document subtree from an index",
    )
    def remove_root_from_index(self, index_name: str, root_document_id: int) -> dict:
        """Remove a document subtree from an index"""
        repo = SearchRepository(self.session)

        if not repo.remove_root_from_index(index_name, root_document_id):
            raise LookupError(f"Index '{index_name}' not found")

        self.session.commit()
        return {"index": index_name, "removed_root": root_document_id}

    # -- index operations ---------------------------------------------------------

    @expose(
        "POST",
        "/indexes/{index_name}/refresh",
        errors={LookupError: 404},
        tags=["indexes"],
        summary="Rebuild an index from its member subtrees",
    )
    def refresh_index(self, index_name: str) -> dict:
        """
        Rebuild an index from its member subtrees.

        This clears existing BM25 data and re-indexes all documents
        in the subtrees associated with this index.
        """
        repo = SearchRepository(self.session)
        result = repo.refresh_index(index_name)

        if "error" in result:
            raise LookupError(result["error"])

        self.session.commit()
        return result

    @expose(
        "POST",
        "/indexes/{index_name}/index-document/{document_id}",
        errors={LookupError: 404, ValueError: 400},
        tags=["indexes"],
        summary="Index a single document into the index",
    )
    def index_single_document(self, index_name: str, document_id: int) -> dict:
        """Index a single document into the index"""
        repo = SearchRepository(self.session)

        if not repo.get_index(index_name):
            raise LookupError(f"Index '{index_name}' not found")

        success = repo.index_document(document_id, index_name)
        if not success:
            raise ValueError(f"Document {document_id} not found or has no content")

        self.session.commit()
        return {"index": index_name, "indexed_document": document_id}
