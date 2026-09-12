"""Index contract — the one IndexResponse, plus the single ORM→response converter.

Before unification the ``SearchIndex -> IndexResponse`` mapping lived as a local
``index_to_response`` helper in ``api/routers/indexes.py``. It is now single-sourced as
``IndexResponse.from_index`` here, mirroring ``DocumentResponse.from_document`` — every
surface (in-process, REST) builds an index response through this one classmethod, so the
shape cannot drift.

Why an explicit converter and not ``model_validate(index)`` with ``from_attributes``: the
old router coalesced the nullable columns (``config``/``capabilities`` → ``{}``,
``total_docs`` → ``0``, ``avg_doc_length`` → ``0.0``) so a freshly-created index with NULL
aggregates serialises to the same zeroed shape it always did. ``from_index`` keeps that
coalescing in one place.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class IndexCreate(BaseModel):
    """Request to create a search index"""

    name: str
    description: Optional[str] = None
    config: Optional[dict] = None


class IndexResponse(BaseModel):
    """Search index response"""

    id: int
    name: str
    description: Optional[str]
    config: dict
    total_docs: int
    avg_doc_length: float
    capabilities: dict
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_index(cls, index) -> "IndexResponse":
        """Convert a ``SearchIndex`` ORM row to a response.

        The single source of truth for this mapping (was ``index_to_response`` in
        ``api/routers/indexes.py``). Nullable aggregate columns are coalesced to their
        zero values, exactly as the old router did.
        """
        return cls(
            id=index.id,
            name=index.name,
            description=index.description,
            config=index.config or {},
            total_docs=index.total_docs or 0,
            avg_doc_length=index.avg_doc_length or 0.0,
            capabilities=index.capabilities or {},
            created_at=index.created_at,
            updated_at=index.updated_at,
        )
