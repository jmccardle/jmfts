"""Getting the bytes, when what arrived was a locator. ``SPRINT_JOBS.md`` 15.4 S8.

Path A's three ``wiki:`` pipelines took a URL, an arXiv id or a local path as their
``content``, fetched it INSIDE the request, converted it to markdown, and ingested that. A
slow server was a slow request; a flaky one was a 400 with no record; a large PDF was both.

Here the request creates the node and enqueues one of these, and the fetch is a task. 15.2
decision 4 is the whole argument: a flaky network becomes a TRANSIENT retry in machinery
that already exists, with a backoff and an attempt log, instead of a request that hangs and
reports nothing.

**They store what they fetched, not what they converted.** Path A ran ``html_to_markdown``
and ``pdf_to_markdown`` eagerly and stored neither original. These write the fetched bytes
to the blob and stop, and then ``probe`` measures them like any upload:

* a fetched HTML page is ``text`` with ``has_markup``, so ``extract:text``'s
  ``MARKUP_EXTRACTOR`` converts it — the same reader an uploaded ``.html`` gets;
* a fetched PDF is ``pdf``, so it gets the real PDF pipeline: an outline-derived rung,
  page geometry, and ``citation``, none of which survived being flattened to markdown
  first.

So the conversion did not move, it stopped being duplicated: one reader per format,
selected from what was measured. That is 11.3's claim about markdown being the intermediate
format, applied to the entry points that were bypassing it.

**Three task types, one body.** ``fetch:url``, ``fetch:arxiv`` and ``fetch:path`` differ
only in how they turn a locator into bytes, and they are separate types rather than one
``fetch`` with a ``kind`` parameter for two reasons: a deployment can badge them
separately (arXiv is rate-limited and a path is local), and ``EXPLAIN`` names the task a
document will run rather than a task plus a parameter.

**Not in ``TASK_ROWS``.** Part 4's table is evaluated FROM probe's output, and these run
before probe exists to have an output — they are what produces the bytes probe reads. They
are enqueued by the request, exactly as ``probe`` is for an upload, and each one enqueues
``probe`` when it lands.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from sqlalchemy.orm import Session

from jmfts_core.atoms import COST_CPU, EV_BLOB, EV_FILE, EV_SOURCE
from jmfts_core.ingest_tasks import (
    PROBE_WRITE_MODE,
    SOURCE_KEY,
    TASK_FETCH_ARXIV,
    TASK_FETCH_PATH,
    TASK_FETCH_URL,
    TASK_PROBE,
    TaskOutcome,
    register_task_handler,
)
from jmfts_core.models.task_queue import WRITE_SELF, TaskQueue
from jmfts_core.probe import detect_format
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.structure_tasks import _scope_node

logger = logging.getLogger(__name__)

#: What the `source` block's `kind` says, per task type. The one mapping between the two,
#: so a node's own record and the task that will act on it cannot disagree.
SOURCE_KIND_TASKS: dict[str, str] = {
    "url": TASK_FETCH_URL,
    "arxiv": TASK_FETCH_ARXIV,
    "path": TASK_FETCH_PATH,
}

#: The mime a fetched PDF is stored under when the fetcher already knows. Detection still
#: runs over the bytes and wins; this is only what the blob row is served back as.
_PDF_MIME = "application/pdf"


class FetchError(Exception):
    """A locator that could not be turned into bytes.

    A plain ``Exception`` rather than a ``ValueError``, deliberately.
    ``jmfts_core.task_errors.classify_exception`` calls ``ValueError`` PERMANENT — right
    for a malformed option, wrong for a server that was down for ten seconds — so the two
    fetchers wrap their own errors in this and let the classifier apply its conservative
    default, which is a retry with backoff. A locator that is malformed rather than
    unreachable is refused by the REQUEST, before a node exists.
    """


def _fetch_url_bytes(locator: str) -> tuple[bytes, str, str]:
    """``(bytes, filename, declared mime)`` for a URL."""
    from jmfts_core.url_fetch import UrlFetchError, fetch_url

    try:
        text, ctype = fetch_url(locator)
    except UrlFetchError as exc:
        raise FetchError(f"could not fetch {locator}: {exc}") from exc
    # `fetch_url` decodes to `str`, so re-encoding is what the blob gets. It is UTF-8
    # whatever the page declared, which is a normalisation and is stated as one: the
    # `file` block's declared mime carries what the server said.
    name = locator.rstrip("/").rsplit("/", 1)[-1] or "index"
    return text.encode("utf-8"), name, ctype


def _fetch_arxiv_bytes(locator: str) -> tuple[bytes, str, str]:
    """``(bytes, filename, declared mime)`` for an arXiv id. The PDF, not the metadata.

    The metadata this used to fold into the root node's evidence is NOT
    written here. A node's blocks are its evidence, and "what arXiv's Atom feed said about
    this paper" is neither what the bytes are nor what a task measured — it is a third-party
    record about the document, which is `GRAPH`/`triples` territory. It is dropped rather
    than moved, and that is a loss worth naming: title, authors and abstract came back with
    the PDF and now nothing keeps them.
    """
    from jmfts_core.arxiv_fetch import ArxivFetchError, fetch_arxiv_pdf, normalize_arxiv_id

    arxiv_id = normalize_arxiv_id(locator)
    try:
        data = fetch_arxiv_pdf(arxiv_id)
    except ArxivFetchError as exc:
        raise FetchError(f"could not fetch arXiv {arxiv_id}: {exc}") from exc
    return data, f"{arxiv_id}.pdf", _PDF_MIME


def _fetch_path_bytes(locator: str) -> tuple[bytes, str, str]:
    """``(bytes, filename, declared mime)`` for a file on this host.

    NOT a fetch over anything, and it keeps the name because it is the same operation from
    the caller's side: a locator this appliance resolves into bytes. It reads whatever is
    there — the format is probe's to decide, so this is no longer PDF-only the way
    ``wiki:pdf`` was.
    """
    path = Path(locator)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FetchError(f"could not read {locator}: {exc}") from exc
    return data, path.name, ""


_FETCHERS = {
    TASK_FETCH_URL: _fetch_url_bytes,
    TASK_FETCH_ARXIV: _fetch_arxiv_bytes,
    TASK_FETCH_PATH: _fetch_path_bytes,
}


def _run_fetch(session: Session, task: TaskQueue) -> TaskOutcome:
    """Turn this node's ``source`` locator into stored bytes, then enqueue ``probe``."""
    doc = _scope_node(session, task, task.task_type)
    source = EvidenceRepository(session).read(doc.id, SOURCE_KEY)
    if not isinstance(source, dict) or not source.get("locator"):
        raise ValueError(
            f"{task.task_type} is scoped to document {doc.id}, which carries no `source` "
            "block naming what to fetch"
        )

    t0 = time.monotonic()
    data, filename, declared_mime = _FETCHERS[task.task_type](source["locator"])
    if not data:
        raise FetchError(f"{source['locator']} returned no bytes")

    digest = hashlib.sha256(data).hexdigest()
    detection = detect_format(data, filename=filename, declared_mime=declared_mime or None)
    blob = BlobRepository(session).store(
        doc.id,
        data,
        mime_type=detection.detected_mime or declared_mime or "application/octet-stream",
        content_hash=digest,
    )

    # The same `file` block an upload writes, and written here for the same reason: it is
    # the record of what was received. `uploaded_at` is when the bytes arrived, which for a
    # fetch is now rather than when the request was made.
    EvidenceRepository(session).write(
        doc.id,
        "file",
        {
            "filename": filename,
            "byte_size": blob.byte_size,
            "content_hash": f"sha256:{digest}",
            "blob_ref": f"lob:{blob.lob_oid}",
            "declared_mime": declared_mime or None,
            "detected_mime": detection.detected_mime,
            "detected_by": detection.detected_by,
            "uploaded_at": _utc_now_iso(),
        },
    )
    doc.content_hash = digest
    session.flush()

    # `probe` is enqueued HERE and not by the request, because until now there were no
    # bytes to probe. This is the same hand-off `probe` itself makes to Part 4's batch: a
    # task whose product is more tasks.
    TaskQueueRepository(session).enqueue(TASK_PROBE, doc.id, PROBE_WRITE_MODE, params={})

    return TaskOutcome(
        detail={
            "locator": source["locator"],
            "bytes": blob.byte_size,
            "content_hash": f"sha256:{digest}",
            "detected_format": detection.format,
            "detected_mime": detection.detected_mime,
            "declared_mime": declared_mime or None,
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
        }
    )


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# One function, three names. The atoms are identical because the work is: a locator in,
# bytes and a `file` block out, `probe` enqueued. What differs is which fetcher runs and —
# the reason they are separate types at all — which pool a deployment may route them to.
for _task_type in (TASK_FETCH_URL, TASK_FETCH_ARXIV, TASK_FETCH_PATH):
    register_task_handler(
        _task_type,
        consumes=(f"{EV_SOURCE}@self",),
        produces=(f"{EV_BLOB}@self", f"{EV_FILE}@self"),
        write_mode=WRITE_SELF,
        # `cpu`, and the cost class has no better answer. These are network- and
        # disk-bound and load no model, and COST_CLASSES has three members none of which
        # is "waits on I/O". A pool sized on this being cpu-bound would be wrong about
        # throughput; a badge is what a deployment has to say it with.
        cost_class=COST_CPU,
    )(_run_fetch)
