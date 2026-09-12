"""File-upload contracts — the framework-neutral upload marker and the 5.7 response.

``UploadedFile`` exists to solve one specific problem. ``tests/test_api_parity.py``
forbids ``jmfts_core/services`` and this contracts package from importing FastAPI or
Starlette, and ``UploadFile`` is a Starlette name — so a service method cannot annotate a
multipart part directly without breaking the core-purity seal. The alternative of
hand-writing the route in ``api/routers/`` would break the *other* seal
(``test_all_domain_routes_are_generated``), and a file upload is a domain operation, not
infra; adding it to ``INFRA_ALLOWLIST`` would be exactly the exception that test refuses
to make.

So the adapter learns the type instead: ``api/wiring.py`` substitutes
``fastapi.UploadFile`` for this annotation when it builds the endpoint signature, and
converts the part back into one of these before calling the service. Core stays
framework-free, the route stays generated, both parity tests pass unmodified, and
``LocalJmftsClient`` keeps working because an in-process caller can just construct one.

A dataclass rather than a ``BaseModel``, for two reasons: FastAPI treats a lone
``BaseModel`` parameter as a JSON body, and Pydantic's ``bytes`` coerces ``str``, so a
caller who passed text would silently get UTF-8 bytes and a wrong content hash.
"""

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field

from jmfts_client.contracts.attempt import AttemptRecord


@dataclass(frozen=True)
class UploadedFile:
    """One uploaded file: its bytes, plus what the client said about them.

    ``filename`` and ``content_type`` are Optional because a multipart part may genuinely
    carry neither. They are NOT defaulted to something plausible here — the service
    rejects a nameless upload with a 400, and a missing content type is recorded as a
    null ``declared_mime`` rather than guessed.
    """

    data: bytes
    filename: Optional[str] = None
    content_type: Optional[str] = None


class FileUploadResponse(BaseModel):
    """What ``POST /ingest/file`` returns. ``INGEST_SPEC.md`` 5.7.

    The spec's response is "the file node id plus the current attempt log". The identity
    fields alongside it are the ``file`` block of 3.3 — they are what the caller needs to
    confirm the bytes that landed are the bytes it sent, without a second round trip.

    5.7 describes this call as returning BEFORE extraction, with one ``pending`` attempt.
    That is true once the queue exists (phasing step 4). Today probe runs inline, so the
    log comes back with probe already terminal. The response shape is the same either
    way; only the status in it differs, and it is the real one.
    """

    document_id: int = Field(description="The file node — root of the tree ingestion builds")
    filename: str
    usetype: str
    settled: str = Field(description="'in_flight' until structuring finishes (spec 3.1)")
    byte_size: int
    content_hash: str = Field(description="sha256:<hex> of the uploaded bytes")
    blob_ref: str = Field(description="lob:<oid> — the Postgres large object holding them")
    declared_mime: Optional[str] = Field(
        description="What the client said; null if it said nothing"
    )
    detected_mime: Optional[str] = Field(description="What the bytes are; null if unrecognised")
    detected_by: Optional[str] = Field(description="magic_bytes | zip_manifest | content_sniff")
    attempts: list[AttemptRecord] = Field(
        description="The node's attempt log as it stands (spec 3.4), newest last"
    )
    was_existing: bool = Field(
        default=False,
        description=(
            "True when these exact bytes were already stored in a `file` node the caller "
            "may read, so `document_id` names a node this request FOUND rather than one it "
            "created: no second document, no second blob, and nothing enqueued. Named to "
            "match `IngestResponse.was_existing`, which reports the same thing for the "
            "text pipeline. It is the flag an HTTP layer would key a 200 on instead of the "
            "declared 201 — `@expose` declares one status per operation today, so the "
            "route still answers 201 and the body is where the distinction lives."
        ),
    )
    governed: bool = Field(
        default=False,
        description=(
            "Whether an access-control root sits at or above this node. FALSE means the "
            "node is UNPROTECTED — readable and writable by anyone holding a token — which "
            "is the documented default, not a fault. It is reported because until now the "
            "two states were indistinguishable on the wire: a subtree meant to be governed "
            "whose grant was never issued looked exactly like one meant to be open. "
            "`GET /access/audit` is the corpus-wide version of the same question."
        ),
    )
    governing_acrs: list[int] = Field(
        default_factory=list,
        description="The access-control roots at or above this node. Empty when `governed` is false.",
    )
    linked_into_parent: bool = Field(
        default=False,
        description=(
            "True when the returned node is placed under `parent_id` by a `contains` "
            "DocumentLink rather than by parentage — the deduplicated form of 'put this "
            "file in that folder'. A link is a GRAPH edge, so the node's `parent_id` is "
            "unchanged and a subtree walk of the parent does NOT reach it. False when this "
            "request created the node (parentage did the job) and false when the existing "
            "node is already a tree child of that parent (the tree edge already says it, "
            "and a duplicate graph edge would only add noise)."
        ),
    )


class IngestFrontierResponse(BaseModel):
    """Progress for an in-flight tree. ``INGEST_SPEC.md`` 2.4.

    A FRONTIER, not a percentage, and the spec is explicit about why: the tasks below the
    current frontier do not exist yet — a node's children are only created when the node
    is processed — so the denominator of a percentage is unknown while the run happens and
    any estimate of it moves backward as work is discovered. "31 settled, 4 in flight, 0
    failed" is honest at every instant; "78% complete" that later reads 61% is not.

    Counts cover the root node and everything under it, by ``path``.
    """

    document_id: int
    settled: str = Field(description="The root node's own lifecycle state")
    nodes_settled: int
    nodes_in_flight: int
    nodes_failed: int
    nodes_total: int
    tasks_unfinished: int = Field(
        description=(
            "Queued tasks under this root that still owe work — pending, claimed, "
            "running, or failed-but-retryable. Zero with nodes_in_flight above zero "
            "means the frontier is waiting on something other than the queue."
        )
    )
