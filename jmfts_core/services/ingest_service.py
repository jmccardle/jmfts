"""IngestService — the general-ingest operations, transport-neutral.

Logic lifted verbatim from ``api/routers/ingest.py`` so the behaviour is identical; the only
structural change is that the domain→HTTP status mapping is now declared per-op in
``@expose(errors=...)`` and keyed by EXCEPTION TYPE rather than by inline ``HTTPException``:

- ``ValueError``  → 400 — bad input: unknown pipeline ``usetype``, empty ``content``, an
  ingest option that names an unknown group or key or carries a value of the wrong type
  (``jmfts_core.ingest_options``), or a ``ValueError`` raised by ``execute_pipeline``.
  Detail strings preserved verbatim. ``upload_file`` adds two of its own: a
  ``private=True`` upload that also names a ``parent_id``, and one made by a caller with
  no principal to grant to (see the method).
- ``LookupError`` → 404 — a supplied ``parent_id`` does not resolve to a document. Detail
  string ``"Parent document <id> not found"`` preserved verbatim.

The service takes a ``Session`` and returns typed contracts — no FastAPI here. ``ingest_content``
is a coroutine (it awaits ``execute_pipeline``), so the adapter emits an ``async def`` endpoint
that runs it on the event loop; ``list_registered_pipelines`` is a plain ``def`` FastAPI runs in
a threadpool.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import Json
from sqlalchemy import select
from sqlalchemy.orm import Session

from jmfts_core.access import require_add_child
from jmfts_client.contracts.attempt import AttemptRecord
from jmfts_client.contracts.explain import (
    AlreadyStored,
    AnalyzeIngestResponse,
    ExplainIngestRequest,
    ExplainIngestResponse,
    ProbeFailure,
)
from jmfts_core.explain_wire import analyzed_file_from_detection, explain_response_from_plan
from jmfts_client.contracts.ingest import (
    IngestRequest,
    IngestResponse,
    IngestStageResult,
    PipelineInfo,
    PipelineStageInfo,
)
from jmfts_client.contracts.upload import FileUploadResponse, IngestFrontierResponse, UploadedFile
from jmfts_core.ingest_options import resolve_options
from jmfts_core.ingest_tasks import (
    OPTIONS_KEY,
    PATTERNS_PROBED,
    PROBE_WRITE_MODE,
    TASK_PROBE,
    explain_plan,
)
from jmfts_core.models.document import (
    SETTLED_FAILED,
    SETTLED_IN_FLIGHT,
    SETTLED_SETTLED,
    USETYPE_FILE,
    Document,
    DocumentLink,
)
from jmfts_core.models.principal import AccessGrant
from jmfts_core.pipeline import execute_pipeline, get_pipeline, list_pipelines
from jmfts_core.principal_context import get_current_principal
from jmfts_core.probe import detect_format, probe_patterns
from jmfts_core.registry import expose, register_service
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.task_errors import classify_exception

logger = logging.getLogger(__name__)

#: Fallback for the blob row's mime column when neither the bytes nor the client identify
#: the content. It is a statement, not a guess: "an uncharacterised stream of bytes" is
#: what application/octet-stream means, and the node's `file` block keeps detected_mime
#: and declared_mime as the nulls they really are.
_UNKNOWN_MIME = "application/octet-stream"

#: The `DocumentLink` type that places an already-stored file node under a parent when an
#: upload deduplicates. "contains" reads in the direction the edge is written — source
#: parent CONTAINS target file — so the pair (source, target) is not ambiguous the way a
#: symmetric name like "related" would be, and `UNIQUE (source_id, target_id, link_type)`
#: on `document_links` is what makes re-uploading the same bytes under the same parent
#: idempotent at the schema level rather than only in this function.
LINK_CONTAINS = "contains"


@register_service
class IngestService:
    """General-ingest operations over a single database session."""

    def __init__(self, session: Session):
        self.session = session

    @expose(
        "GET",
        "/ingest/pipelines",
        response_model=list[PipelineInfo],
        tags=["ingest"],
        summary="List all registered pipeline definitions",
    )
    def list_registered_pipelines(self) -> list[PipelineInfo]:
        """List all registered pipeline definitions."""
        return [
            PipelineInfo(
                name=p.name,
                description=p.description,
                stages=[
                    PipelineStageInfo(name=name, enabled=cfg.enabled, params=cfg.params)
                    for name, cfg in p.default_stages.items()
                ],
            )
            for p in list_pipelines()
        ]

    @expose(
        "POST",
        "/ingest",
        response_model=IngestResponse,
        errors={ValueError: 400, LookupError: 404},
        tags=["ingest"],
        summary="Ingest content through a named pipeline",
    )
    async def ingest_content(
        self,
        request: IngestRequest,
    ) -> IngestResponse:
        """Ingest content through a named pipeline.

        The ``usetype`` selects which pipeline to run.  Use
        ``GET /ingest/pipelines`` to see available pipelines and their
        default stage configurations.

        Optionally override stage settings via ``pipeline_config``::

            {
                "summarize": false,
                "chunk": {"strategy": "paragraph", "max_tokens": 300}
            }
        """
        db = self.session

        # Validate pipeline exists
        if get_pipeline(request.usetype) is None:
            available = [p.name for p in list_pipelines()]
            raise ValueError(
                f"Unknown pipeline usetype: {request.usetype!r}. Available: {available}"
            )

        # Validate content
        if not request.content or not request.content.strip():
            raise ValueError("Content must not be empty")

        # Validate parent if provided
        if request.parent_id is not None:
            repo = DocumentRepository(db)
            if not repo.get(request.parent_id):
                raise LookupError(f"Parent document {request.parent_id} not found")

        result = await execute_pipeline(
            session=db,
            content=request.content,
            usetype=request.usetype,
            title=request.title,
            parent_id=request.parent_id,
            pipeline_config=request.pipeline_config,
            llm_model=request.llm_model,
        )

        db.commit()

        return IngestResponse(
            source_document_id=result.source_document_id,
            title=result.title,
            usetype=request.usetype,
            message_count=result.message_count,
            segment_count=result.segment_count,
            summary_count=result.summary_count,
            triple_count=result.triple_count,
            tree_depth=result.tree_depth,
            stages=[
                IngestStageResult(
                    stage=s.stage,
                    status=s.status,
                    detail=s.detail,
                    error=s.error,
                )
                for s in result.stages
            ],
            was_existing=result.was_existing,
            existing_document_id=result.existing_document_id,
        )

    @expose(
        "POST",
        "/ingest/file",
        response_model=FileUploadResponse,
        errors={ValueError: 400, LookupError: 404},
        status_code=201,
        tags=["ingest"],
        summary="Upload a file, create its in-flight file node, and enqueue `probe`",
    )
    def upload_file(
        self,
        file: UploadedFile,
        parent_id: Optional[int] = None,
        options: Optional[Json[dict]] = None,
        private: bool = False,
    ) -> FileUploadResponse:
        """Upload a file, create its `file` node, store the bytes, and enqueue `probe`.

        ``INGEST_SPEC.md`` Part 3.1: the file node is created immediately from the
        uploaded bytes, BEFORE any parsing, with ``settled = 'in_flight'``, and it is the
        ROOT of the tree ingestion will build. There is no separate container node — one
        place holds both the bytes and the result.

        The order below follows that literally. Format detection reads a magic-number
        prefix and is not parsing, so it runs first and the blob row gets a mime type
        worth serving. Then the node and the bytes land and are flushed. Nothing here
        opens the document: that is `probe`'s job, and `probe` is a queue row.

        Spec 5.7 governs when this returns: *"once the file node exists with
        ``settled = 'in_flight'``, the blob is stored, and ``probe`` is enqueued. It does
        not wait for extraction."* So `probe` is a queue row here, not a function call —
        the in-process worker (5.8) runs it — and the attempt log this returns holds
        exactly one `pending` entry. The client polls the node, or
        ``GET /ingest/file/{id}/frontier`` for the 2.4 progress counts.

        ALREADY-STORED BYTES ARE NOT STORED TWICE. Before anything is created, the upload
        looks for a `file` node whose blob carries this sha256 AND WHICH THIS CALLER MAY
        READ (:meth:`DocumentRepository.find_readable_file_by_blob_hash`). On a hit with no
        ``parent_id`` — "here are some bytes, analyse them" — the existing node is returned
        with ``was_existing=True`` and this request writes nothing at all. On a hit WITH a
        ``parent_id`` — "put this file in that folder" — the existing node is attached to
        the parent by a ``contains`` link instead of being copied; see
        :meth:`_place_existing_file` for what a link is, what it is not, and why the node is
        not reparented. Two principals in isolated access zones each get their own node,
        because neither one's lookup can see the other's: that is the requirement, not a
        gap in it. A hit whose recorded ``options`` differ from this request's resolved set
        is a 400 rather than a node that quietly ignores them — see
        :meth:`_place_existing_file` again, and spec 6.1.

        This is the FIRST asynchronous pipeline in JMFTS. ``POST /ingest`` and every
        registered text pipeline keep their synchronous behaviour unchanged: they run to
        completion inside the request and return a finished tree.

        ``options`` are this request's overrides — ``{"structure": {"max_tokens": 60}}`` —
        over the task defaults and any profile ``jmfts_core.ingest_options`` registers for
        the format detected from the bytes. They are resolved BEFORE anything is created,
        so a misspelled option is
        a 400 on an upload that never happened rather than a failed task on a node that
        exists, and the RESOLVED set is written onto the node: ``probe`` runs in a worker
        and can only read what the upload left behind.

        ``private`` is OPT-IN AND OFF BY DEFAULT, and that default is the design, not an
        oversight. JMFTS assumes access is controlled outside the application — an
        isolated container, a backend only its own frontend can reach — so a token means
        access to the SHARED knowledgebase and an upload lands readable by every
        principal. "Private until shared" would be the opposite assumption and would
        create bugs of ignorance in every deployment that holds the stated one.

        What the flag exists for is the gap the default leaves. A file uploaded with no
        ``parent_id`` — "here are some bytes, analyse them" — has no ancestor, therefore
        no access-control root above it, therefore nothing governs it; and a later grant
        cannot retroactively cover it, because ACR membership is strictly the tree path
        (``R = D.id OR R ∈ D.path``). ``private=True`` closes that by making the new node
        its own ACR with the uploader as its only grantee, at ``write`` — the uploader
        owns what they uploaded and must be able to correct it. The other way to get the
        same result already exists and is unchanged: upload with a ``parent_id`` inside a
        subtree that is already governed.

        ``private`` NARROWS DEDUPLICATION, and it has to. The ordinary lookup matches any
        file node the caller can READ, and an ungoverned node is readable by everyone — so
        a private upload of bytes that are already here as a SHARED node would silently
        resolve to that shared node and the caller would not be private at all. Under
        ``private=True`` the lookup is restricted to nodes the caller holds a GRANT on
        (:meth:`DocumentRepository.find_own_granted_file_by_blob_hash`): a second private
        upload of the same bytes by the same principal still resolves to their own node,
        and a shared node is never handed to a caller who asked for isolation.

        On the wire ``private`` is a QUERY parameter, not a form field: FastAPI publishes
        scalars as query parameters even on a multipart operation, so a ``bool`` lands
        beside ``parent_id`` and only the structured ``options`` below becomes a form
        field. That is inference, not a choice made here, and it is asserted in the tests
        rather than assumed.

        The annotation is ``Json[dict]`` rather than ``dict`` because this operation is
        multipart: the bytes make the body a form, so a structured parameter can only
        arrive as a form field, and a form field is a string. Pydantic's own ``Json`` says
        exactly that — "a dict, delivered as JSON text" — so the parsing is the
        framework's rather than a convention invented here, and an in-process caller
        passing a real dict is unaffected because annotations are not enforced at runtime.
        """
        db = self.session

        data = file.data
        if not data:
            raise ValueError("Uploaded file is empty")
        filename = (file.filename or "").strip()
        if not filename:
            # Not defaulted to "upload.bin" or similar: the filename carries the
            # extension, which is the fallback evidence for how to open unrecognised
            # bytes, and inventing one would put a fabricated extension in the record.
            raise ValueError("Upload is missing a filename")

        principal = get_current_principal()
        if private:
            # Both refusals are raised BEFORE the parent is looked up and before anything
            # is written: a request that cannot be honoured as asked is answered on its own
            # terms, not after a round trip that might turn it into a 404 instead.
            if parent_id is not None:
                # REFUSED, not silently one or the other. Grants are ADDITIVE — a deeper
                # ACR can only widen access, never restrict what an ancestor granted — so
                # under a governed parent this would NOT make the file private: everyone
                # granted above would still read it, and the flag would be a lie the
                # caller had no way to detect. Under an UNGOVERNED parent it would restrict
                # one child of an otherwise open folder as a side effect of an upload.
                # Marking an ACR inside an existing tree is a real and different operation
                # and it has its own verb, which says what it is doing.
                raise ValueError(
                    "private=True cannot be combined with parent_id: grants are additive, "
                    "so a new access-control root inside a governed subtree widens access "
                    "rather than restricting it. Upload without a parent to get a private "
                    "root, place the file under a subtree that is already governed, or mark "
                    "an existing document with POST /access/documents/{document_id}/grants."
                )
            if principal is None or principal.id is None:
                # The owner bearer and unbound in-process callers have no principals row,
                # so there is nobody to grant to. Ignoring the flag would return a 201 for
                # a world-readable node to a caller who asked for the opposite — the
                # swallowed failure, exactly. The node is not created either: a restriction
                # that cannot be applied is not a detail to fix afterwards.
                raise ValueError(
                    "private=True requires a bound non-owner principal to grant access to; "
                    "this request is authenticated as the owner (or has no principal at "
                    "all), which has no principals row to be the grantee."
                )

        if parent_id is not None:
            if not DocumentRepository(db).get(parent_id):
                raise LookupError(f"Parent document {parent_id} not found")

        digest = hashlib.sha256(data).hexdigest()
        declared_mime = (file.content_type or "").strip() or None
        detection = detect_format(data, filename=filename, declared_mime=declared_mime)

        # Resolved against the DETECTED format, before a node, a blob or a queue row
        # exists. Two things follow from the position. A bad option raises here, and the
        # request has written nothing to roll back — the caller gets a 400 naming the
        # option instead of a 201 and a node that fails an hour later. And the format the
        # options are checked against is the one the bytes are: `probe` re-derives it from
        # the same function over the same bytes, so what was validated is what will run.
        resolved_options = resolve_options(detection.format, options)

        repo = DocumentRepository(db)

        # --- deduplication: have we already got these exact bytes? -------------------
        # Positioned AFTER option resolution and detection — both are pure functions that
        # write nothing — so a misspelled option is a 400 whether or not somebody uploaded
        # these bytes first. The same request otherwise gets a different answer depending
        # on what is already in the database, which is not a property worth having in a
        # validation error. It is positioned BEFORE `repo.create`, which is the point:
        # nothing has been written yet, so a hit costs no document, no blob, no large
        # object and no queue row.
        #
        # A private upload asks the NARROWER question — "have I already got these bytes",
        # not "has anybody" — because readable includes every ungoverned node and those are
        # readable by all. See `find_own_granted_file_by_blob_hash`.
        if private:
            existing = repo.find_own_granted_file_by_blob_hash(digest, principal.id)
        else:
            existing = repo.find_readable_file_by_blob_hash(digest)
        if existing is not None:
            return self._place_existing_file(existing, parent_id, resolved_options)

        node = repo.create(
            title=filename,
            content=None,
            parent_id=parent_id,
            usetype=USETYPE_FILE,
            structured_content={},
            # No text has been extracted yet — that is `extract:text`, phasing step 5 —
            # so there is nothing to embed. auto_embed would be a no-op on empty content
            # anyway; saying so explicitly keeps the intent readable.
            auto_embed=False,
            embed_tokens=False,
            settled=SETTLED_IN_FLIGHT,
        )
        # `documents.content_hash` is the deduplication column, and for a file node the
        # content IS the bytes. DocumentRepository.create derives it from `content`,
        # which is None here, so it is set directly rather than left null on a row whose
        # identity is perfectly well defined.
        node.content_hash = digest

        if private:
            # ONE ROW IS THE WHOLE MECHANISM. Being an access-control root is DEFINED as
            # having at least one grant (`jmfts_core/access.py`) — there is no flag on
            # `documents` — so this row is what makes the node governed, and it must be
            # written in the same transaction as the node: a node that exists ungoverned
            # for even a moment is a node the dedup lookup will hand to somebody else.
            #
            # `AccessService.grant` is NOT the path, deliberately. It calls require_owner(),
            # which denies every bound non-owner — and a non-owner principal restricting
            # their own upload is precisely the case this flag serves. That gate is right
            # where it is (grant administration is the human's), so the row is written here
            # rather than the gate being weakened for everyone.
            #
            # `write`, not `read`: the uploader owns what they uploaded. A read-only grant
            # on your own file means every correction, re-ingest or deletion needs the
            # appliance owner.
            db.add(AccessGrant(document_id=node.id, principal_id=principal.id, level="write"))

        blob = BlobRepository(db).store(
            node.id,
            data,
            # Detected before declared: the bytes are evidence and the header is a claim.
            # Both are preserved on the `file` block below, so choosing here decides only
            # what the stored object is served back as, not what is on the record.
            mime_type=detection.detected_mime or declared_mime or _UNKNOWN_MIME,
            content_hash=digest,
        )

        # Spec 3.3's `file` block: what we received. It never changes after upload.
        # `blob_ref` holds the large-object OID (Part 9 is explicit about that) in the
        # scheme-prefixed form 3.3's example shows, so the reference says what kind of
        # reference it is instead of being a bare integer in a JSON blob.
        blob_ref = f"lob:{blob.lob_oid}"
        file_block = {
            "filename": filename,
            "byte_size": blob.byte_size,
            "content_hash": f"sha256:{digest}",
            "blob_ref": blob_ref,
            "declared_mime": declared_mime,
            "detected_mime": detection.detected_mime,
            "detected_by": detection.detected_by,
            "uploaded_at": _utc_now_iso(),
        }
        sc = dict(node.structured_content or {})
        sc["file"] = file_block
        # The RESOLVED options, not the overrides, and always written even when empty. A
        # node that says `{"structure": {...}}` in full answers "what was this ingested
        # with?" without anyone having to know which version of the profile was in effect
        # at the time; a node that says `{}` states that its format has nothing to tune,
        # which is a different and equally real answer from an absent key.
        sc[OPTIONS_KEY] = resolved_options
        node.structured_content = sc
        db.flush()

        # --- enqueue probe -----------------------------------------------------
        # probe takes no tunable parameters — it reads the bytes and reports what it
        # finds. The constant fingerprint an empty params dict produces is exactly the
        # right statement for spec 6.1's re-run diff: re-probing the same bytes cannot
        # produce a different answer, so no parameter change ever warrants a re-run.
        #
        # `enqueue` writes the `pending` attempt record 5.7 describes, so the response
        # below reads the log rather than synthesising an entry the node does not carry.
        TaskQueueRepository(db).enqueue(TASK_PROBE, node.id, PROBE_WRITE_MODE, params={})

        # The node stays `in_flight`. Probe is the FIRST task, not the last: nothing about
        # this tree is finished and it must not become retrievable yet (spec Part 2).
        db.commit()

        return FileUploadResponse(
            document_id=node.id,
            filename=filename,
            usetype=USETYPE_FILE,
            settled=node.settled,
            byte_size=blob.byte_size,
            content_hash=file_block["content_hash"],
            blob_ref=blob_ref,
            declared_mime=declared_mime,
            detected_mime=detection.detected_mime,
            detected_by=detection.detected_by,
            attempts=[
                AttemptRecord.model_validate(entry)
                for entry in (node.structured_content or {}).get("attempts") or []
            ],
            was_existing=False,
            linked_into_parent=False,
        )

    def _place_existing_file(
        self, node: Document, parent_id: Optional[int], resolved_options: dict
    ) -> FileUploadResponse:
        """The dedupe answer: describe the file node that already holds these bytes, and,
        if the upload named a parent, attach it there with a link.

        WHAT DOES NOT HAPPEN. No document is created, no blob is stored, no large object is
        written and nothing is enqueued. The bytes have already been probed, extracted and
        structured (or are in flight, or failed — whichever, that work is this node's and
        re-running it is spec 6.1's re-ingest, not an upload).

        A LINK IS A GRAPH EDGE, AND THE TREE EDGE IS `parent_id`. The file node keeps the
        parent it was uploaded with. So a file uploaded under folder A and then "uploaded"
        under folder B is reachable from B by `get_links(B)` and NOT by
        `get_subtree(B)` — the subtree walk is `path`/`parent_id` containment and this edge
        is in neither. That is the honest cost of answering "place this in a tree" without
        duplicating 40 MB of bytes, and any caller that renders a folder from the subtree
        alone will not show the file. It is written here because the alternative is
        discovering it from an empty folder listing months later.

        REJECTED: ADOPTING AN ORPHAN BY REPARENTING IT. When the match has
        `parent_id IS NULL` — someone uploaded it to analyse it, spec 3.1 — it is tempting
        to just set its parent and get a real tree edge for free. It is refused. The node
        is a record somebody else created for their own purpose; moving it as a side effect
        of a third party uploading the same bytes changes what THEIR document is under,
        invalidates rollups above the new parent, and does it silently in a request that
        said nothing about moving anything. Adoption is a curator action with its own verb
        (`reparent`, which gates on write access to both ends). This is not it.

        DIFFERENT OPTIONS ON THE SAME BYTES ARE REFUSED, NOT IGNORED. Spec 6.1 is explicit
        that the same bytes resolve to the same node and that a parameter change is handled
        by diffing `(task, fingerprint)` against `attempts` and enqueuing only the
        difference — "no re-parse, no duplicate tree". That diff is the NEXT step and does
        not exist yet, so an upload that asks for `max_tokens: 30` against a node ingested
        at 400 can be answered in exactly two honest ways: do the work, or say it was not
        done. Returning the node as though the options had been applied is the third one,
        and it is the kind of quiet wrong answer this codebase does not ship.
        """
        db = self.session
        recorded_options = (node.structured_content or {}).get(OPTIONS_KEY) or {}
        if recorded_options != resolved_options:
            raise ValueError(
                f"Document {node.id} already holds these exact bytes, ingested with "
                f"{recorded_options!r}; this upload asks for {resolved_options!r}. Uploading "
                "does not re-run work that has already been done — re-running tasks whose "
                "parameters changed is INGEST_SPEC.md 6.1 re-ingest, which is not built yet."
            )
        # Required, not defaulted. Spec 3.3 makes the `file` block the record of what was
        # received, and it is what this response IS; a `file` node carrying stored bytes
        # and no block is a corrupt record, and answering an upload out of the fields that
        # happen to be there would publish that corruption as a normal 201.
        file_block = (node.structured_content or {}).get("file")
        if not file_block:
            raise RuntimeError(
                f"Document {node.id} is a `file` node holding the uploaded bytes but has no "
                "`file` block in structured_content (INGEST_SPEC.md 3.3); it cannot be "
                "reported as the result of an upload"
            )

        linked = False
        if parent_id is not None and node.parent_id != parent_id:
            # The create path reaches `require_add_child` through `repo.create`; this path
            # does not create anything, so the same gate is applied explicitly. Without it,
            # a principal with read-only access to a folder could hang documents off it.
            parent = DocumentRepository(db).get(parent_id)
            if parent is None:  # validated by the caller; re-read for the gate
                raise LookupError(f"Parent document {parent_id} not found")
            require_add_child(db, parent)

            already = db.execute(
                select(DocumentLink).where(
                    DocumentLink.source_id == parent_id,
                    DocumentLink.target_id == node.id,
                    DocumentLink.link_type == LINK_CONTAINS,
                )
            ).scalar_one_or_none()
            if already is None:
                DocumentRepository(db).create_link(
                    source_id=parent_id, target_id=node.id, link_type=LINK_CONTAINS
                )
                db.commit()
            linked = True
        # `node.parent_id == parent_id` deliberately falls through with `linked` False: the
        # file is already a child of that parent by parentage, which is a stronger
        # statement than the link would be, and adding a graph edge that duplicates a tree
        # edge puts a second "contains" in every link listing for no new information.

        return FileUploadResponse(
            document_id=node.id,
            filename=file_block["filename"],
            usetype=USETYPE_FILE,
            settled=node.settled,
            byte_size=file_block["byte_size"],
            content_hash=file_block["content_hash"],
            blob_ref=file_block["blob_ref"],
            declared_mime=file_block.get("declared_mime"),
            detected_mime=file_block.get("detected_mime"),
            detected_by=file_block.get("detected_by"),
            # The EXISTING node's log, not an empty one. It is the answer to "what has been
            # done with these bytes", and this request added nothing to it.
            attempts=[
                AttemptRecord.model_validate(entry)
                for entry in (node.structured_content or {}).get("attempts") or []
            ],
            was_existing=True,
            linked_into_parent=linked,
        )

    @expose(
        "POST",
        "/ingest/explain",
        response_model=ExplainIngestResponse,
        errors={ValueError: 400},
        tags=["ingest"],
        summary="What a format with these options would do, without uploading anything",
    )
    def explain_ingest(self, request: ExplainIngestRequest) -> ExplainIngestResponse:
        """Explain the ingest plan for a format. ``INGEST_SPEC.md`` 11.2.

        READ-ONLY IN THE STRONGEST SENSE: this touches no document, opens no transaction
        work, writes nothing and enqueues nothing. It does not even look at the session.
        The answer comes from :func:`~jmfts_core.ingest_tasks.explain_plan`, which reads
        Part 4's table — the SAME declaration ``probe`` schedules from — so the plan cannot
        drift from the run without the run changing too.

        ``POST`` rather than ``GET`` because the question has structure: ``options`` and
        ``patterns`` are nested objects, and a query string carrying JSON would be a body
        in a worse place. It stays free of side effects regardless of the verb.

        TWO ANSWER MODES, and the response says which. Give ``patterns`` — a hypothesis, or
        a real node's ``matched.patterns`` pasted back — and every task is decided. Omit
        them and the answer is decided anyway IF the format has no prober, because then
        ``probe`` measures nothing and the empty pattern set is a fact about this appliance
        rather than a guess about the file. Otherwise the undecidable rows come back
        ``conditional``, naming the patterns that decide them and what they would become.
        Nothing is assumed in either direction: a plan resting on a fabricated input is a
        wrong answer wearing a confident shape.

        This is ``EXPLAIN``. ``ANALYZE`` — the mode that takes real bytes, runs ``probe``
        and stops — is NOT built. Its input is a file, so it belongs beside the upload, and
        11.2 keeps them separate on purpose.

        An option that does not exist is a 400 naming it, from the same
        :func:`~jmfts_core.ingest_options.resolve_options` the upload calls. Explaining a
        plan under options the run would reject would be the wrong answer this endpoint
        exists to prevent.
        """
        return explain_response_from_plan(
            explain_plan(request.format, request.options, request.patterns)
        )

    @expose(
        "POST",
        "/ingest/analyze",
        response_model=AnalyzeIngestResponse,
        errors={ValueError: 400},
        tags=["ingest"],
        summary="What THESE bytes would do: run probe, report the plan, store nothing",
    )
    def analyze_ingest(
        self,
        file: UploadedFile,
        options: Optional[Json[dict]] = None,
        private: bool = False,
    ) -> AnalyzeIngestResponse:
        """Analyze a file without ingesting it. ``INGEST_SPEC.md`` 11.2's second mode.

        ``EXPLAIN`` is given a format and has to reason about a file it cannot see. This
        is given the file. It runs the two pure functions ``probe`` runs —
        :func:`~jmfts_core.probe.detect_format` over the bytes, then
        :func:`~jmfts_core.probe.probe_patterns` — and hands what they measured to the
        SAME :func:`~jmfts_core.ingest_tasks.explain_plan` the explain endpoint calls. The
        third input of ``(format, patterns, options)`` stops being a hypothesis, so every
        task comes back decided and ``patterns_source`` is ``probed``.

        **It stops there, and 11.2 says so: "``ANALYZE`` runs ``probe`` and stops."** No
        document is created, no blob is stored, no large object is written, no queue row is
        enqueued, no attempt is recorded. The bytes are read into memory, measured, and
        dropped. That is affordable for exactly the reason Part 4 gives for probe running
        first — it is cheap and calls no model — and it is why this is safe to run over a
        whole corpus to see what the appliance would make of it.

        THE ONE THING IT READS FROM THE DATABASE is whether these bytes are already stored
        in a file node this caller may read, through the same lookup ``upload_file`` uses.
        Not a convenience: an upload of already-stored bytes resolves to the existing node
        and runs no plan at all, so an answer that described the pipeline without saying so
        would be forecasting a run that is not going to happen. ``private`` is accepted for
        the same reason and does nothing else here — it selects the NARROWER lookup, so the
        dedup answer matches the upload the caller is actually planning to make.

        A PROBE THAT WOULD FAIL IS REPORTED, NOT PLANNED AROUND. A PDF PyMuPDF refuses to
        open, or a missing extraction library, raises inside ``probe_patterns`` — and it
        would raise identically in the worker. So the exception is caught here, classified
        by the same :func:`~jmfts_core.task_errors.classify_exception` the worker applies,
        and returned as ``probe_failed`` with ``plan`` null. That is the forecast rather
        than a suppression of it: Part 4's conditions are evaluated over patterns that were
        never measured, so there is no downstream schedule to report, and emitting one
        built on an empty pattern set would say "nothing is applicable to this file" when
        the truth is "we could not look".

        ``options`` are validated against the DETECTED format, before anything else, so a
        misspelled option is a 400 here exactly as it is on the upload — which is the point
        of analysing first.
        """
        data = file.data
        if not data:
            # The same two refusals `upload_file` makes, for the same reasons and in the
            # same order. An analysis that answered a question the upload would reject
            # would not be a forecast of anything.
            raise ValueError("Uploaded file is empty")
        filename = (file.filename or "").strip()
        if not filename:
            raise ValueError("Upload is missing a filename")

        declared_mime = (file.content_type or "").strip() or None
        detection = detect_format(data, filename=filename, declared_mime=declared_mime)
        digest = hashlib.sha256(data).hexdigest()

        analyzed = analyzed_file_from_detection(
            detection,
            filename=filename,
            byte_size=len(data),
            content_hash=f"sha256:{digest}",
        )

        # Before the probe, so that a bad option is a 400 whatever the bytes turn out to
        # be — the same position, and the same reason, as in `upload_file`.
        resolve_options(detection.format, options)

        try:
            patterns, probe_detail = probe_patterns(data, detection)
        except Exception as exc:  # noqa: BLE001 — classified and returned, never swallowed
            logger.info("analyze: probe would fail on %s: %s", filename, exc)
            return AnalyzeIngestResponse(
                file=analyzed,
                format=detection.format,
                patterns={},
                probe_detail={},
                probe_failed=ProbeFailure(
                    error=f"{type(exc).__name__}: {exc}",
                    error_type=classify_exception(exc).value,
                ),
                already_stored=self._already_stored(digest, private),
            )

        plan = explain_plan(detection.format, options, patterns, patterns_source=PATTERNS_PROBED)
        return AnalyzeIngestResponse(
            file=analyzed,
            format=detection.format,
            patterns=patterns,
            probe_detail=probe_detail,
            plan=explain_response_from_plan(plan),
            already_stored=self._already_stored(digest, private),
        )

    def _already_stored(self, digest: str, private: bool) -> Optional[AlreadyStored]:
        """The file node holding these exact bytes that this caller may read, if any.

        Mirrors `upload_file`'s dedup lookup exactly, ``private`` included, because the
        whole value of the answer is that it is the lookup the upload will do. A private
        upload asks "have *I* already got these bytes", and asking the wider question here
        would report a shared node the private upload is never going to be given.

        A caller with no principal cannot ask the narrow question — there is no grantee to
        key it on — and gets no dedup answer rather than the wide one, which would be the
        wrong lookup reported as the right one.
        """
        repo = DocumentRepository(self.session)
        if private:
            principal = get_current_principal()
            if principal is None:
                return None
            existing = repo.find_own_granted_file_by_blob_hash(digest, principal.id)
        else:
            existing = repo.find_readable_file_by_blob_hash(digest)
        if existing is None:
            return None
        return AlreadyStored(
            document_id=existing.id,
            settled=existing.settled,
            options=(existing.structured_content or {}).get(OPTIONS_KEY),
        )

    @expose(
        "GET",
        "/ingest/file/{document_id}/frontier",
        response_model=IngestFrontierResponse,
        errors={LookupError: 404},
        tags=["ingest"],
        summary="Progress for an in-flight ingestion: nodes settled, in flight, failed",
    )
    def file_frontier(self, document_id: int) -> IngestFrontierResponse:
        """Report the ingestion frontier under a node. ``INGEST_SPEC.md`` 2.4.

        Counts, never a percentage. The task set below the current frontier does not
        exist until the frontier reaches it (5.5), so the denominator of a percentage is
        unknown while the run happens and any estimate of it moves backward as work is
        discovered.

        The node must exist — an unknown id is a 404 rather than a row of zeros, because
        "no such document" and "a document with nothing under it" are different answers
        and a caller polling for progress would read the second as "finished".
        """
        db = self.session
        node = DocumentRepository(db).get(document_id)
        if node is None:
            raise LookupError(f"Document {document_id} not found")

        counts = DocumentRepository(db).settle_frontier(document_id)
        return IngestFrontierResponse(
            document_id=document_id,
            settled=node.settled,
            nodes_settled=counts[SETTLED_SETTLED],
            nodes_in_flight=counts[SETTLED_IN_FLIGHT],
            nodes_failed=counts[SETTLED_FAILED],
            nodes_total=counts["total"],
            tasks_unfinished=TaskQueueRepository(db).unfinished_task_count_under(document_id),
        )


def _utc_now_iso() -> str:
    """Timezone-aware UTC, ISO-8601 — the format spec 3.3's `uploaded_at`/`probed_at` show."""
    return datetime.now(timezone.utc).isoformat()
