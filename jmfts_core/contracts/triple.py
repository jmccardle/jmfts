"""Triple & predicate contracts — request/response models for the triples surface.

Moved out of ``api/schemas.py`` so the service layer (``jmfts_core.services``) can
depend on them without importing FastAPI. ``api/schemas.py`` re-exports these names,
so existing ``from jmfts_core.rest.schemas import TripleCreate`` imports keep working.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from jmfts_core.contracts.document import DocumentResponse


class PredicateCreate(BaseModel):
    """Request to create a predicate"""

    name: str
    domain: Optional[str] = None
    description: Optional[str] = None


class PredicateResponse(BaseModel):
    """Predicate response"""

    id: int
    name: str
    domain: Optional[str]
    description: Optional[str]
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


class TripleCreate(BaseModel):
    """Request to create a triple"""

    subject_id: int
    predicate_id: int
    object_id: int
    source_document_id: Optional[int] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    fact_type: str = "atemporal"


class TripleResponse(BaseModel):
    """Triple response"""

    id: int
    subject_id: int
    predicate_id: int
    object_id: int
    source_document_id: Optional[int]
    created_at: Optional[datetime]
    valid_from: Optional[datetime]
    valid_until: Optional[datetime]
    recorded_at: Optional[datetime]
    fact_type: Optional[str]
    invalidated_at: Optional[datetime]
    invalidated_by: Optional[int]
    invalidation_reason: Optional[str]

    class Config:
        from_attributes = True


class TripleDetailResponse(BaseModel):
    """Triple response with expanded relationships"""

    id: int
    subject: DocumentResponse
    predicate: PredicateResponse
    object: DocumentResponse
    source_document_id: Optional[int]
    created_at: Optional[datetime]
    valid_from: Optional[datetime]
    valid_until: Optional[datetime]
    recorded_at: Optional[datetime]
    fact_type: Optional[str]
    invalidated_at: Optional[datetime]
    invalidated_by: Optional[int]
    invalidation_reason: Optional[str]

    class Config:
        from_attributes = True


class TripleInvalidateRequest(BaseModel):
    """Request to invalidate a triple"""

    reason: Optional[str] = None
    superseding_triple_id: Optional[int] = None


class TripleSupersedRequest(BaseModel):
    """Request to create a new triple that supersedes an existing one"""

    subject_id: int
    predicate_id: int
    object_id: int
    source_document_id: Optional[int] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    fact_type: str = "atemporal"
    reason: Optional[str] = None


class PathStep(BaseModel):
    """A single step in a graph path"""

    triple_id: int
    subject_id: int
    predicate_name: str
    object_id: int


class PathResponse(BaseModel):
    """Path between two entities"""

    paths: list[list[PathStep]]
    total_paths: int
