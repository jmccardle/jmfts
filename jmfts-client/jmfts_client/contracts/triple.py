"""Triple & predicate contracts — request/response models for the triples surface.

Moved out of ``api/schemas.py`` so the service layer (``jmfts_core.services``) can
depend on them without importing FastAPI. ``api/schemas.py`` re-exports these names,
so existing ``from jmfts_core.rest.schemas import TripleCreate`` imports keep working.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jmfts_client.contracts.document import DocumentResponse


class PredicateCreate(BaseModel):
    """Request to create a predicate"""

    model_config = ConfigDict(extra="forbid")

    name: str
    #: Renamed from ``domain`` in 0.3.0. ``rdfs:domain`` means "the class a subject must
    #: belong to"; this has always meant "the group this predicate belongs to", and the
    #: two readings collide once the tree speaks RDF.
    namespace: Optional[str] = None
    description: Optional[str] = None
    #: The IRI a published vocabulary knows this predicate by. Unique across predicates;
    #: null means the predicate is local to this appliance.
    iri: Optional[str] = None


class PredicateResponse(BaseModel):
    """Predicate response"""

    id: int
    name: str
    namespace: Optional[str]
    iri: Optional[str] = None
    description: Optional[str]
    created_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


def _exactly_one_object(object_id: Optional[int], object_literal: Optional[str], object_datatype):
    """The shape rule both write requests share: one object, and a datatype only on a literal."""
    if (object_id is None) == (object_literal is None):
        raise ValueError(
            "a triple's object is exactly one of object_id and object_literal; "
            f"got object_id={object_id!r}, object_literal={object_literal!r}"
        )
    if object_datatype is not None and object_literal is None:
        raise ValueError("object_datatype types a literal; give object_literal, not object_id")


class TripleCreate(BaseModel):
    """Request to create a triple.

    The object is EXACTLY ONE of a document node (``object_id``) or a literal value
    (``object_literal``, optionally typed by ``object_datatype``). Neither, or both, is a
    422. ``object_datatype`` without ``object_literal`` is a 422 too: a datatype belongs to
    a literal, and the schema's ``ck_triples_object_exactly_one`` refuses it on a node.
    """

    model_config = ConfigDict(extra="forbid")

    subject_id: int
    predicate_id: int
    object_id: Optional[int] = Field(
        default=None,
        description="The object as a document node. Exactly one of object_id and "
        "object_literal must be given.",
    )
    object_literal: Optional[str] = Field(
        default=None,
        description="The object as a literal value, in its lexical form. Exactly one of "
        "object_id and object_literal must be given.",
    )
    object_datatype: Optional[str] = Field(
        default=None,
        description="The absolute IRI typing object_literal, e.g. "
        "http://www.w3.org/2001/XMLSchema#integer. Omitted means xsd:string. Only valid "
        "beside object_literal; an xsd datatype this appliance does not know, or a literal "
        "that is not a valid value of it, is refused rather than stored.",
    )
    source_document_id: Optional[int] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    fact_type: str = "atemporal"

    @model_validator(mode="after")
    def _one_object(self):
        _exactly_one_object(self.object_id, self.object_literal, self.object_datatype)
        return self


class TripleResponse(BaseModel):
    """Triple response"""

    id: int
    subject_id: int
    predicate_id: int
    #: Null when the object is a literal — see ``object_literal``.
    object_id: Optional[int]
    #: The literal object's lexical form, and the ``xsd:`` IRI typing it. Exactly one of
    #: ``object_id`` and ``object_literal`` is set on any triple. A null
    #: ``object_datatype`` beside a literal means ``xsd:string``, not "unknown".
    object_literal: Optional[str] = None
    object_datatype: Optional[str] = None
    #: Which rule produced this row. Null means asserted.
    derived_by: Optional[str] = None
    source_document_id: Optional[int]
    created_at: Optional[datetime]
    valid_from: Optional[datetime]
    valid_until: Optional[datetime]
    recorded_at: Optional[datetime]
    fact_type: Optional[str]
    invalidated_at: Optional[datetime]
    invalidated_by: Optional[int]
    invalidation_reason: Optional[str]

    model_config = ConfigDict(from_attributes=True)


class TripleDetailResponse(BaseModel):
    """Triple response with expanded relationships"""

    id: int
    subject: DocumentResponse
    predicate: PredicateResponse
    #: Null when the object is a literal; ``object_literal`` carries it instead.
    object: Optional[DocumentResponse]
    object_literal: Optional[str] = None
    object_datatype: Optional[str] = None
    derived_by: Optional[str] = None
    source_document_id: Optional[int]
    created_at: Optional[datetime]
    valid_from: Optional[datetime]
    valid_until: Optional[datetime]
    recorded_at: Optional[datetime]
    fact_type: Optional[str]
    invalidated_at: Optional[datetime]
    invalidated_by: Optional[int]
    invalidation_reason: Optional[str]

    model_config = ConfigDict(from_attributes=True)


class TripleInvalidateRequest(BaseModel):
    """Request to invalidate a triple"""

    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = None
    superseding_triple_id: Optional[int] = None


class TripleSupersedRequest(BaseModel):
    """Request to create a new triple that supersedes an existing one.

    The successor's object follows :class:`TripleCreate`: exactly one of ``object_id`` and
    ``object_literal``, and ``object_datatype`` only beside a literal.
    """

    model_config = ConfigDict(extra="forbid")

    subject_id: int
    predicate_id: int
    object_id: Optional[int] = Field(
        default=None,
        description="The successor's object as a document node. Exactly one of object_id "
        "and object_literal must be given.",
    )
    object_literal: Optional[str] = Field(
        default=None,
        description="The successor's object as a literal value, in its lexical form. "
        "Exactly one of object_id and object_literal must be given.",
    )
    object_datatype: Optional[str] = Field(
        default=None,
        description="The absolute IRI typing object_literal; omitted means xsd:string. "
        "Only valid beside object_literal.",
    )
    source_document_id: Optional[int] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    fact_type: str = "atemporal"
    reason: Optional[str] = None

    @model_validator(mode="after")
    def _one_object(self):
        _exactly_one_object(self.object_id, self.object_literal, self.object_datatype)
        return self


class PathStep(BaseModel):
    """One traversed edge in a graph path — always between two entities.

    ``object_id`` stays non-null here while ``TripleResponse.object_id`` is nullable, and
    the difference is the point. A triple's object may be a literal; a path STEP's cannot,
    because a literal is a value with no far side to walk to, so a literal fact is a leaf
    and never a step. ``find_path`` skips those edges (``SPRINT_0_3_0.md`` 13.1), and this
    type is where that invariant is stated to callers: a step's ``object_id`` is an entity
    id you can ask about, unconditionally. Widening it to ``Optional[int]`` would make
    every consumer branch on a case the walk cannot produce, and would let a regression
    that reintroduced literal hops serve nulls quietly instead of failing at the boundary.
    """

    triple_id: int
    subject_id: int
    predicate_name: str
    object_id: int


class PathResponse(BaseModel):
    """Path between two entities"""

    paths: list[list[PathStep]]
    total_paths: int
