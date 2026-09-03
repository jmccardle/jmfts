"""IngestService — the general-ingest operations, transport-neutral.

Every entry point runs on the ingest queue (``SPRINT_JOBS.md`` Part 15). A request stores
its content as a file node, or names a locator for a ``fetch:*`` task to store, and then
drains THAT DOCUMENT'S tasks before returning the finished tree — the same tasks, handlers
and settling walk the background worker runs, with the request acting as the worker.

The domain→HTTP status mapping is declared per-op in ``@expose(errors=...)`` and keyed by
EXCEPTION TYPE rather than by inline ``HTTPException``:

- ``ValueError``  → 400 — bad input: an unknown ``usetype``, empty ``content``, an ingest
  option that names an unknown group or key or carries a value of the wrong type
  (``jmfts_core.ingest_options``), a ``pipeline_config`` (the deleted pipeline's stage
  vocabulary), or an unknown source kind. ``upload_file`` adds two of its own: a
  ``private=True`` upload that also names a ``parent_id``, and one made by a caller with
  no principal to grant to (see the method).
- ``LookupError`` → 404 — a supplied ``parent_id`` does not resolve to a document. Detail
  string ``"Parent document <id> not found"`` preserved verbatim.

The service takes a ``Session`` and returns typed contracts — no FastAPI here.
``ingest_content`` is a coroutine so that it can ``await asyncio.to_thread(...)``: the
drain must not hold the event loop, and an in-process caller needs that as much as an HTTP
one. The other operations are plain ``def`` and FastAPI runs them in a threadpool.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from uuid import uuid4
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
from jmfts_client.contracts.ingest import IngestRequest, IngestResponse, PipelineInfo
from jmfts_client.contracts.upload import FileUploadResponse, IngestFrontierResponse, UploadedFile
from jmfts_core.config import get_settings
from jmfts_core.fetch_tasks import SOURCE_KIND_TASKS
from jmfts_core.ingest_options import (
    INGEST_USETYPES,
    SOURCE_CONTENT,
    Usetype,
    get_usetype,
    resolve_options,
    resolve_usetype_options,
)
from jmfts_core.ingest_summary import summarize_tree
from jmfts_core.ingest_worker import IngestWorker, borrowed_session
from jmfts_core.ingest_tasks import (
    OPTIONS_KEY,
    PATTERNS_PROBED,
    PROBE_WRITE_MODE,
    SOURCE_KEY,
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
from jmfts_core.principal_context import get_current_principal
from jmfts_core.probe import detect_format, probe_patterns
from jmfts_core.registry import expose, register_service
from jmfts_core.rollup_tasks import IngestRollupPlanner
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository, evidence_value
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
        summary="List the entry points POST /ingest accepts",
    )
    def list_registered_pipelines(self) -> list[PipelineInfo]:
        """The entry points ``POST /ingest``'s ``usetype`` accepts, and what each one does.

        Answers from ``jmfts_core.ingest_options.INGEST_USETYPES`` — the one list of entry
        points (``SPRINT_JOBS.md`` 15.4 S1). ``options`` is the resolved set a request
        under that usetype runs with, resolved against the empty format because a usetype
        names no format: ``probe`` measures one from the bytes, and a format profile that
        deviates would apply on top of what is reported here.

        A ``stages`` list was here until ``SPRINT_JOBS.md`` 15.4 S9. It carried the
        deprecated pipeline's per-stage defaults, and there are no stages — there are
        tasks, and which of them a document runs is decided from what ``probe`` measured.
        ``POST /ingest/explain`` is the operation that answers that, and it answers it for
        a format and a set of options rather than for a name.
        """
        return [
            PipelineInfo(
                name=name,
                description=usetype.description,
                source=usetype.source,
                options=resolve_usetype_options(name, ""),
            )
            for name, usetype in INGEST_USETYPES.items()
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
        """Ingest content through a named entry point, and return the finished tree.

        The ``usetype`` selects the entry point. ``GET /ingest/pipelines`` lists them, and
        says for each one where its content comes from and what options it resolves to.

        **Synchronous, and staying that way** (``SPRINT_JOBS.md`` 15.2 decision 1). The
        content becomes a file node with stored bytes — fetched first, when the usetype
        names a locator rather than a document — and this request then drains THAT
        DOCUMENT'S tasks before returning. Same tasks, same handlers, same settling walk as
        the background worker, with the request acting as the worker. The wire does not
        move; the work does.

        Override the ingest options with ``options``::

            {"structure": {"chunk_strategy": "paragraph", "max_tokens": 300}}

        ``pipeline_config`` was the deprecated pipeline's stage vocabulary and is REFUSED.
        There is no translation between the two — path A's ``summarize`` stage was RAPTOR
        clustering and the queue's is what gives a container its vectors — so accepting one
        and doing something else would be the swallowed failure this codebase does not
        ship. The 400 names the field to use instead.

        ``llm_model`` still works and sets ``rollup.llm_model`` and ``facts.llm_model``.
        """
        db = self.session

        # The entry point exists. Asked of INGEST_USETYPES rather than of the deprecated
        # pipeline registry, because that table is what `GET /ingest/pipelines` answers
        # from and the two lists a caller is told about must be the same list.
        usetype = get_usetype(request.usetype)
        if usetype is None:
            available = list(INGEST_USETYPES)
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

        # OFF THE EVENT LOOP, and this is not an optimisation. Task handlers are
        # synchronous by contract, and the ones that call an LLM reach
        # `llm_client.complete_sync`, which owns an `asyncio.run` for the duration of one
        # call and REFUSES to run in a thread that already has a loop. It is also a drain
        # that can last minutes, and holding the loop for that would stall every other
        # request in the process.
        #
        # The method stays `async def` so that `await asyncio.to_thread(...)` is what puts
        # it there. A plain `def` would give the same threadpool from FastAPI and would
        # NOT give it to an in-process caller, of which the test suite is one.
        return await asyncio.to_thread(self._ingest_through_the_queue, request, usetype)

    def _ingest_through_the_queue(self, request: IngestRequest, usetype: Usetype) -> IngestResponse:
        """One content string, stored, queued, drained here, and reported. 15.4 S5.

        Four steps, and only the third is new machinery:

        1. :meth:`store_text_as_file` writes the bytes, the ``file`` block and the ``probe``
           row — the same function ``POST /ingest/file`` uses, so the node is
           indistinguishable from an upload of the same text. A usetype whose ``source`` is
           a locator goes through :meth:`store_source_as_file` instead, and the bytes
           arrive one task later.
        2. If those bytes were already here, nothing is drained. The tree is finished
           already, by whoever uploaded it first, and re-running it is 6.1's re-ingest.
        3. :meth:`~jmfts_core.ingest_worker.IngestWorker.drain_document` runs this
           document's tasks to quiet, on this request's session.
        4. :func:`~jmfts_core.ingest_summary.summarize_tree` reads the counts back.

        **A plain ``def``, run in a thread.** Nothing on this path awaits anything — the
        queue's handlers are synchronous by contract and the drain is a loop — so the
        caller hands it to ``asyncio.to_thread``. See there for why a loop in this thread
        would break the handlers that call an LLM.

        WHAT MOVES, that a caller can see. The root node's ``usetype`` is ``file``, not
        ``raw`` / ``markdown`` / ``transcript`` — it is a file node holding stored bytes,
        which is what it now really is; the response's ``usetype`` field still reports what
        was asked for. Deduplication is by sha256 across everything the caller can read
        rather than by content hash under the same parent, so re-sending the same text
        under a different parent links rather than copies. And the same bytes sent with
        DIFFERENT options is a 400 rather than a silent hit, which is
        :meth:`_place_existing_file`'s rule and spec 6.1's.
        """
        if request.pipeline_config is not None:
            raise ValueError(
                f"usetype {request.usetype!r} runs on the ingest queue, which takes "
                "`options` (ingest option groups, e.g. {'structure': {'max_tokens': 300}}) "
                "rather than `pipeline_config` (stage names). The two are not "
                "translatable: the queue has no `summarize` stage to switch off, its "
                "`summarize` task is what gives a container node its vectors."
            )

        overrides = dict(request.options or {})
        if request.llm_model:
            # The one field of the old vocabulary that maps cleanly, because it names the
            # same thing in both: which model answers. Its own description says
            # "summarization and extraction", so it sets BOTH groups — they are separate
            # options because summarizing and extracting are separate tasks a deployment
            # may route differently, and this field predates that split. Merged rather than
            # assigned, so a caller who set `options.rollup` keeps the rest of that group.
            for group in ("rollup", "facts"):
                merged = dict(overrides.get(group) or {})
                merged["llm_model"] = request.llm_model
                overrides[group] = merged

        resolved = resolve_usetype_options(request.usetype, "", overrides)

        if usetype.source == SOURCE_CONTENT:
            word_count = len(request.content.split())
            # Path A's default title, preserved exactly: the response carries a `title` and
            # a caller who did not send one got this string. It is also the file node's
            # filename, which needs no extension — see `store_text_as_file`.
            title = request.title or f"{request.usetype.capitalize()} ({word_count} words)"
            stored = self.store_text_as_file(
                request.content,
                filename=title,
                parent_id=request.parent_id,
                options=resolved,
            )
        else:
            # `content` is a locator for these — the usetype table says which kind, and the
            # kind selects the fetch task. The document does not exist yet and this request
            # will wait for it, exactly as it waited for path A's inline fetch; what is
            # different is that a flaky server is now a retry with a backoff and an entry
            # in the attempt log rather than a 400 with no record (15.2 decision 4).
            stored = self.store_source_as_file(
                usetype.source,
                request.content,
                title=request.title,
                parent_id=request.parent_id,
                options=resolved,
            )

        if not stored.was_existing:
            worker = IngestWorker(
                # Unique per request. `requeue_stale_claims` is scoped to a worker's own id
                # and runs at ITS startup, so a shared id would let a restarting appliance
                # fail a task an in-flight request is running. A request that dies holding
                # a task is recovered by the lease reaper instead, which is what that
                # mechanism is for.
                worker_id=f"inline-ingest-{uuid4().hex[:12]}",
                session_factory=lambda: borrowed_session(self.session),
                planner=IngestRollupPlanner(),
            )
            worker.drain_document(
                stored.document_id,
                timeout_seconds=get_settings().ingest_sync_timeout_seconds,
            )

        # The drain committed several times through the borrowed session, so this session's
        # identity map holds rows from before the tasks ran.
        self.session.expire_all()
        summary = summarize_tree(self.session, stored.document_id)
        if summary is None:
            raise RuntimeError(
                f"document {stored.document_id} was ingested and then could not be read "
                "back; the tree it describes is gone"
            )

        return IngestResponse(
            source_document_id=stored.document_id,
            title=summary.title,
            usetype=request.usetype,
            message_count=summary.message_count,
            segment_count=summary.segment_count,
            summary_count=summary.summary_count,
            triple_count=summary.triple_count,
            tree_depth=summary.tree_depth,
            stages=summary.stages,
            was_existing=stored.was_existing,
            # Set only on a hit, matching path A: on a fresh ingest there is no OTHER
            # document to name, and `source_document_id` already carries the new one.
            existing_document_id=stored.document_id if stored.was_existing else None,
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

        return self._store_file(
            data,
            filename=filename,
            declared_mime=(file.content_type or "").strip() or None,
            parent_id=parent_id,
            options=options,
            private=private,
            principal_id=principal.id if private else None,
        )

    def store_text_as_file(
        self,
        content: str,
        *,
        filename: str,
        parent_id: Optional[int] = None,
        options: Optional[dict] = None,
    ) -> FileUploadResponse:
        """A content STRING becomes a file node holding its bytes. ``SPRINT_JOBS.md`` 15.4 S2.

        The one thing path A had that path B did not: an entry point that takes text rather
        than an upload. ``POST /ingest`` sends a JSON string; the queue starts from stored
        bytes and a ``probe`` row. This is the join, and it is deliberately a re-encoding
        rather than a second ingest path — the string is UTF-8 encoded, stored as a blob
        exactly as an upload would be, and probed by the same task. Nothing downstream can
        tell the two apart, which is the property that makes it safe for S5 to move callers.

        **The filename carries no extension and does not need one.** ``detect_format``
        reads the extension only for bytes nothing else recognised, and text that came from
        a ``str`` is UTF-8 by construction — ``_sniff_text`` reports ``text`` from the bytes.
        A caller who sends a markdown document gets ``format=text`` with ``has_headings``
        measured, which is exactly what uploading the same file as ``.md`` gets, and Part
        4's table picks the declared rung from the measurement either way.

        The one input this cannot characterise is a string containing ``U+0000``: JSON
        permits it, ``_sniff_text`` rejects it as the binary marker it is, and with no
        extension to fall back on the format comes out ``unknown``. That is reported rather
        than repaired — the plan says which rows were not applicable and why — because a
        NUL-bearing string is not text and pretending otherwise is what would hide it.

        ``private`` is not a parameter. Path A has no privacy flag, so a text ingest lands
        the way it always has: readable, or governed by the parent it was given.
        """
        return self._store_file(
            content.encode("utf-8"),
            filename=filename,
            # A claim, and a true one — this really is what the caller sent. Detection
            # still reads the bytes first, so the claim decides nothing except what the
            # stored object is served back as when the bytes are unrecognisable.
            declared_mime="text/plain; charset=utf-8",
            parent_id=parent_id,
            options=options,
            private=False,
            principal_id=None,
        )

    def store_source_as_file(
        self,
        kind: str,
        locator: str,
        *,
        title: Optional[str] = None,
        parent_id: Optional[int] = None,
        options: Optional[dict] = None,
    ) -> FileUploadResponse:
        """A LOCATOR becomes a file node with no bytes yet, and a ``fetch:*`` task.

        ``SPRINT_JOBS.md`` 15.4 S8. The mirror of :meth:`store_text_as_file`: that one has
        the document and stores it, this one has only a name for it and queues the work of
        getting it. Everything after the fetch is identical — the same ``probe``, the same
        Part 4 batch, the same rungs — because ``fetch:*`` writes the same ``blob`` and
        ``file`` block an upload writes and then enqueues ``probe`` itself.

        **Deduplicated on the LOCATOR, not on the bytes.** An upload can compare sha256
        because it holds the content; here the content is what has not been fetched yet, so
        the only question this can ask is "have I already been asked for this URL", and
        that is also the question a caller means by re-sending it. A hit returns the
        existing node and enqueues nothing — the tree it grew is that node's, and rebuilding
        it because the page may have changed is spec 6.1's re-ingest, which is not built.

        **``settled`` is ``in_flight`` and there is no blob**, which is a state no upload
        can produce. It is honest: the node exists because somebody asked for this document,
        the bytes are on their way, and nothing may treat it as retrievable until they
        arrive. A fetch that permanently fails leaves the node ``failed`` with the reason,
        which is exactly what 2.1's ``failed`` is for and is more than path A left behind.

        ``private`` is not a parameter, for the reason :meth:`store_text_as_file` gives.
        """
        db = self.session
        task_type = SOURCE_KIND_TASKS.get(kind)
        if task_type is None:
            raise ValueError(
                f"unknown source kind {kind!r}; the kinds this appliance fetches are "
                f"{sorted(SOURCE_KIND_TASKS)}"
            )
        locator = (locator or "").strip()
        if not locator:
            raise ValueError(f"a {kind} source needs a locator; none was given")

        if parent_id is not None and not DocumentRepository(db).get(parent_id):
            raise LookupError(f"Parent document {parent_id} not found")

        # Resolved against the EMPTY format, unlike an upload's — probe has not run and
        # there are no bytes to detect one from. A format profile that deviates therefore
        # cannot apply to a fetched document until 6.1 can re-resolve after the fetch;
        # `INGEST_PROFILES` is empty today, so nothing is lost yet and this is where it
        # would be noticed.
        resolved_options = resolve_options("", options)

        repo = DocumentRepository(db)
        existing = repo.find_readable_file_by_source(kind, locator)
        if existing is not None:
            return self._describe_source_node(existing, already_fetched=True)

        node = repo.create(
            title=title or locator,
            content=None,
            parent_id=parent_id,
            usetype=USETYPE_FILE,
            structured_content={},
            auto_embed=False,
            embed_tokens=False,
            settled=SETTLED_IN_FLIGHT,
        )
        EvidenceRepository(db).write_all(
            node.id,
            {
                SOURCE_KEY: {"kind": kind, "locator": locator, "requested_at": _utc_now_iso()},
                OPTIONS_KEY: resolved_options,
            },
        )
        db.flush()

        # No params, for the reason `probe` has none: the locator is on the node, and
        # re-fetching the same locator is not a different attempt because a parameter
        # changed. A re-fetch is 6.1's, and it is a different question.
        TaskQueueRepository(db).enqueue(task_type, node.id, PROBE_WRITE_MODE, params={})
        db.commit()

        return self._describe_source_node(node, already_fetched=False)

    def _describe_source_node(self, node: Document, *, already_fetched: bool) -> FileUploadResponse:
        """A ``FileUploadResponse`` for a node whose bytes may not have arrived yet.

        The `file` block is absent until ``fetch:*`` writes it, so the byte fields describe
        an empty file rather than a wrong one. A caller polls the node, or
        ``GET /ingest/file/{id}/frontier``, the same way they would after an upload.
        """
        evidence = EvidenceRepository(self.session).read_all(node.id)
        block = evidence.get("file") or {}
        return FileUploadResponse(
            document_id=node.id,
            filename=block.get("filename") or node.title,
            usetype=USETYPE_FILE,
            settled=node.settled,
            byte_size=block.get("byte_size", 0),
            content_hash=block.get("content_hash", ""),
            blob_ref=block.get("blob_ref", ""),
            declared_mime=block.get("declared_mime"),
            detected_mime=block.get("detected_mime"),
            detected_by=block.get("detected_by"),
            attempts=[
                AttemptRecord.model_validate(entry) for entry in evidence.get("attempts") or []
            ],
            was_existing=already_fetched,
            linked_into_parent=False,
        )

    def _store_file(
        self,
        data: bytes,
        *,
        filename: str,
        declared_mime: Optional[str],
        parent_id: Optional[int],
        options: Optional[dict],
        private: bool,
        principal_id: Optional[int],
    ) -> FileUploadResponse:
        """Detect, deduplicate, store, and enqueue ``probe``. The shared body of an ingest.

        Split out of :meth:`upload_file` by ``SPRINT_JOBS.md`` 15.4 S2 so that
        :meth:`store_text_as_file` reaches the SAME code rather than a parallel copy of it.
        Everything above this point is per-entry-point validation; everything below is what
        happens to bytes, and there is one of it.

        ``principal_id`` is required when ``private`` and refused otherwise: the caller has
        already established that a grantee exists, and passing the principal rather than
        re-reading the contextvar keeps that check and its use in one place.
        """
        db = self.session
        if private and principal_id is None:
            raise ValueError(
                "a private file needs a principal to grant to; the caller must establish "
                "one before storing bytes"
            )

        digest = hashlib.sha256(data).hexdigest()
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
            existing = repo.find_own_granted_file_by_blob_hash(digest, principal_id)
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
            db.add(AccessGrant(document_id=node.id, principal_id=principal_id, level="write"))

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
        # The RESOLVED options, not the overrides, and always written even when empty. A
        # node that says `{"structure": {...}}` in full answers "what was this ingested
        # with?" without anyone having to know which version of the profile was in effect
        # at the time; a node that says `{}` states that its format has nothing to tune,
        # which is a different and equally real answer from an absent row.
        EvidenceRepository(db).write_all(
            node.id, {"file": file_block, OPTIONS_KEY: resolved_options}
        )
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
                for entry in DocumentRepository(db).attempt_log(node)
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
        evidence = EvidenceRepository(db)
        recorded_options = evidence.read(node.id, OPTIONS_KEY) or {}
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
        file_block = evidence.read(node.id, "file")
        if not isinstance(file_block, dict) or not file_block:
            raise RuntimeError(
                f"Document {node.id} is a `file` node holding the uploaded bytes but has no "
                "`file` evidence (INGEST_SPEC.md 3.3); it cannot be reported as the result "
                "of an upload"
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
                for entry in DocumentRepository(db).attempt_log(node)
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
            options=evidence_value(self.session, existing.id, OPTIONS_KEY),
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
