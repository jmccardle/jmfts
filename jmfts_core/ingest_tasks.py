"""Queued ingest tasks: the handler registry, and ``probe``. ``INGEST_SPEC.md`` Part 4.

Two things live here and they are deliberately separate.

**The registry** maps a ``task_queue.task_type`` to the function that runs it. The worker
(``jmfts_core/ingest_worker.py``) knows nothing about any particular task; it claims a
row, looks the name up here, and records whatever comes back. A name with no handler is a
hard failure, not a no-op — see :func:`get_task_handler`.

**The Part 4 conditions** are :func:`plan_after_probe`. The spec's table is the whole
scheduling decision for file ingestion:

    ``probe`` is cheap, needs no model, and always runs. Everything downstream is decided
    by what it wrote into ``matched.patterns``.

So the conditions are DECLARED in one place — :data:`TASK_ROWS`, a row per task naming the
patterns it needs and the tasks it comes after — and evaluated by one loop over the
patterns probe measured. Spec 11.2 is the reason it is a table rather than a branch
ladder: an ``EXPLAIN`` for ingestion can only answer "what would this format with these
options do?" if every enqueue decision is a pure function of ``(format, patterns,
options)``, and a table makes that structural instead of a property somebody has to keep
re-establishing. The third of those inputs lives in ``jmfts_core.ingest_options``: a row
names WHICH parameters its task takes, and the task's defaults, the format's profile and
the caller's overrides resolve to what they are — so what a queue row carries is decided by
the same declaration the schedule is. The result is recorded in probe's own attempt detail — every downstream
task appears there either as enqueued, as skipped with a reason, or as not-applicable with
the pattern that was false. A task whose condition holds but whose *implementation* has
not shipped is reported as deferred and **is not enqueued**: enqueuing it would put a row
in the queue that no worker can run, and stubbing a handler that returns a plausible empty
result is exactly the anti-pattern the project's Fail Early rule forbids. When the handler
lands, registering it is the only change needed — the condition is already written.

**A row names its SCOPE, since ``SPRINT_JOBS.md`` Phase 3, and that is the one structural
change to the table.** Every row used to be scoped implicitly to the uploaded file node,
and a task scoped to anything else — a worksheet, a chunk — had nowhere here to be
declared, so it was written as a literal :class:`~jmfts_core.settling.TaskSpec` inside the
handler that creates its nodes. Part 0 measures what that cost: three of those tasks had
parameters no caller could set, their conditions were invisible to ``EXPLAIN``, and the
schedule was stated in two places that were free to disagree.

Three things follow, and the second is the one to have in mind while reading below:

* **A batch is one NODE's tasks.** ``plan_after_probe`` answers for one scope and defaults
  to the file node, which is the caller ``run_probe`` has always been. :func:`plan_frontier`
  is what asks for another — it is called by the five handlers that write children, at the
  moment they write them, because the settling walk travels upward and never descends
  (6.4). The enqueue call survives; the hand-written list of what to enqueue does not.
* **A scope is not an ordering.** ``after`` orders two tasks on one node and resolves to a
  real ``dependencies`` id in the same batch (5.5). A scope says the node does not exist
  yet, which no dependency array can express.
* **Every scope is decided in ONE evaluation.** A child scope's eligibility is read off its
  producing row's, so they cannot be decided apart — and two evaluators of one table are
  free to disagree, which is the failure ``EXPLAIN`` is built to avoid.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol, Union

from sqlalchemy.orm import Session

from jmfts_client.contracts.attempt import AttemptRecord, param_fingerprint
from jmfts_core.atoms import (
    ATOMS,
    COST_CLASSES,
    COST_CPU,
    EV_BLOB,
    EV_FILE,
    EV_MATCHED,
    ChildKey,
    Fanout,
    declare,
)
from jmfts_core.evidence import TYPE_BOOL, TYPE_DICT, TYPE_FLOAT, TYPE_INT, TYPE_LIST, TYPE_STR
from jmfts_core.evidence import written_by as evidence_written_by
from jmfts_core.ingest_options import TASK_PARAM_DEFAULTS, resolve_options
from jmfts_core.models.document import (
    Document,
    USETYPE_CHUNK,
    USETYPE_CELL,
    USETYPE_RECORD,
    USETYPE_SHEET,
    USETYPE_PROFILE,
    USETYPE_TABLE,
)
from jmfts_core.models.task_queue import WRITE_CHILDREN, WRITE_SELF, WRITE_SUBTREE, TaskQueue
from jmfts_core.probe import PROBERS_AVAILABLE, detect_format, probe_patterns
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository, evidence_value
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.settling import TaskSpec, enqueue_batch

#: Task names from spec Part 4, spelled once. Not an enum and not a CHECK constraint —
#: ``task_type`` stays an open string so an application can queue its own work — but the
#: tasks this spec names are referenced from several modules and must agree.
TASK_PROBE = "probe"

#: ``SPRINT_JOBS.md`` 15.4 S8 — turn a locator into stored bytes. Not rows of
#: :data:`TASK_ROWS` and they cannot be: that table is evaluated FROM what ``probe``
#: measured, and these run before there are bytes to measure. The request enqueues one,
#: exactly as an upload enqueues ``probe``, and the handler enqueues ``probe`` when the
#: bytes land. See :mod:`jmfts_core.fetch_tasks`.
TASK_FETCH_URL = "fetch:url"
TASK_FETCH_ARXIV = "fetch:arxiv"
TASK_FETCH_PATH = "fetch:path"
TASK_EXTRACT_TEXT = "extract:text"
TASK_OCR = "ocr"
TASK_STRUCTURE_DECLARED = "structure:declared"
TASK_STRUCTURE_INFERRED = "structure:inferred"
TASK_EXTRACT_TABLES = "extract:tables"
TASK_EXTRACT_IMAGES = "extract:images"

#: ``INGEST_SPEC.md`` Part 8, not Part 4 — a workbook's declared rung, which is its sheet
#: list and nothing else (8.1). It is a task of its own rather than a second reading inside
#: ``structure:declared`` because it reads different EVIDENCE: the two structure rungs
#: consume the text ``extract:text`` produced, and a workbook has no text layer and cannot
#: get one without first choosing a rendering for its cells — which is 8.4's decision and
#: is precisely what 8.1 excludes from the declared rung.
#:
#: 8.2 names ``structure:declared`` as what ``profile:sheet`` depends on, so this is a
#: DIVERGENCE from the spec's task name, taken deliberately and recorded in
#: :mod:`jmfts_core.sheet_tasks`. The rung the nodes carry is still ``declared``.
TASK_STRUCTURE_SHEETS = "structure:sheets"

#: ``SPRINT_JOBS.md`` 15.4 S7 — a transcript's declared rung, which is its turns. A fourth
#: structural row, and a peer of the two prose rungs rather than a step below them: exactly
#: one of the three is ever eligible for a document, because a conversation states its own
#: leaf boundaries and prose does not.
#:
#: The rung the nodes carry is ``declared``. The file says where each message begins; there
#: is no heuristic anywhere in it, which is what 3.5 means by the word.
TASK_STRUCTURE_CONVERSATION = "structure:conversation"

#: ``INGEST_SPEC.md`` 8.2's two per-sheet tasks. ROWS OF :data:`TASK_ROWS` SINCE PHASE 3,
#: scoped to the children ``structure:sheets`` produced rather than to the file node.
#:
#: This comment used to say they could not be rows, and the reasoning was sound about the
#: table it was written against: that table was evaluated once per uploaded FILE from
#: probe's patterns, and these are scoped to a SHEET node, which does not exist until
#: ``structure:sheets`` has created it. What changed is not the fact but the table —
#: ``SPRINT_JOBS.md`` Part 0 named this as the shoehorning and 4.1 named the missing piece:
#: "a ``TaskRow`` has no way to name a scope other than the file node". It has one now, so
#: 8.2's "depends on ``structure:declared``" is written down as the scope it always was —
#: the node a task is scoped to has to exist, which is a stronger statement than a queue
#: dependency and is enforced by a different mechanism.
#:
#: One workbook of forty sheets is still forty batches of two, still enqueued by
#: ``structure:sheets`` as it writes each node (6.4 — the settling walk travels upward, so a
#: freshly created child is unreachable from below and its work must be enqueued where it is
#: created). What moved is the DECLARATION: the batch comes from :func:`plan_frontier`
#: reading these rows, not from a literal tuple inside :mod:`jmfts_core.sheet_tasks`. One
#: malformed sheet still fails its own two tasks and leaves the other thirty-nine settled
#: (8.1).
TASK_PROFILE_SHEET = "profile:sheet"
TASK_EXTRACT_SHEET = "extract:sheet"

#: ``INGEST_SPEC.md`` 8.6's workbook-level task — "the first workbook-level task after the
#: per-sheet fan-out, which is why it needs all sheets profiled first". Scoped to the FILE
#: node, one per workbook, and enqueued by ``structure:sheets`` with a dependency on every
#: ``profile:sheet`` it queued: those ids are known at that moment and nowhere else, because
#: the sheet nodes they are scoped to were created by the same loop.
#:
#: **STILL NOT A ROW OF** :data:`TASK_ROWS`, **and Phase 3 is what makes that a decision
#: rather than a limitation.** The two per-sheet tasks above became rows the day a row could
#: name a scope, and this one could name one too — :data:`ROOT_SCOPE`, the file node. What it
#: cannot express is its dependency. A row's ordering is ``after``, resolved against the
#: other rows of the SAME batch, so the strongest thing this row could say is ``after
#: structure:sheets`` — and ``structure:sheets`` completes at the instant it has ENQUEUED the
#: profiles, not when they have run. The task would be claimable over a workbook with no
#: profile on any sheet, and 8.6's whole precondition is that they are all in. The real
#: dependency is a set of task ids, which exists only inside the loop that created them, so
#: :func:`jmfts_core.sheet_tasks._enqueue_propose_links` is what enqueues it.
TASK_PROPOSE_LINKS = "propose:links"

#: The review task 8.6 says ``propose:links`` emits per candidate pair, and Part 7 defines.
#: Named here with every other task name; it has no handler and, unlike the deferred tasks
#: above, it is not enqueued at all. See :data:`DEFERRED_REASON` for why.
TASK_REVIEW_LINK = "review:link"

#: ``OFFICE_SPEC.md`` Part 5, not ``INGEST_SPEC.md`` Part 4 — the first task in this table
#: that belongs to the office/citation plan rather than to the original file pipeline. It
#: puts a page and a rectangle on every chunk under a file node
#: (:mod:`jmfts_core.citation_tasks`).
#:
#: The same string appears in
#: :data:`~jmfts_core.models.task_queue.ADVISORY_TASK_TYPES`, which cannot import this
#: module (the dependency runs the other way), and ``tests/test_citation_task.py`` pins the
#: two spellings together.
TASK_CITATION = "citation"

#: ``OFFICE_SPEC.md`` Part 5's OTHER anchor, and Part 11 step 9. It puts the part and the
#: XPath of the element a chunk came from on every chunk under an office file node
#: (:func:`jmfts_core.citation_tasks.run_anchor_source`).
#:
#: **TWO ANCHORS, TWO TASKS, BECAUSE THEY FAIL DIFFERENTLY.** Part 5's "Two anchors, not
#: one" is the whole reason this is not another format in :data:`TASK_CITATION`'s reader
#: table: an address into the SOURCE package is exact, permanent and computable from the
#: bytes that were uploaded, while a rectangle on a rendered page is approximate, depends on
#: which LibreOffice paginated the file, and can fail to be computed at all. For a source
#: PDF the two are the same object, which is why ``citation`` alone covers it and why this
#: row's sentinel has no ``pdf`` entry.
#:
#: Advisory, for the reason ``citation`` is and by the same rule — a chunk with no anchor is
#: a chunk, and the absence is recorded on it. The spelling is pinned to
#: :data:`~jmfts_core.models.task_queue.ADVISORY_TASK_TYPES` by ``tests/test_office_anchors.py``.
TASK_ANCHOR_SOURCE = "anchor:source"

#: ``OFFICE_SPEC.md`` Part 6 and Part 1 — the two tasks that drive LibreOffice, and the only
#: two things in this appliance that need tier 3. :mod:`jmfts_core.render_tasks` is the
#: handler module and :mod:`jmfts_core.convert` is the seam.
#:
#: ``render:pdf`` paginates a document into a stored PDF, because an office format has no
#: pages until something does. That is what makes Part 1's simplification true: after it,
#: every renderable document in the system is a PDF, so serving a citation image is
#: ``pymupdf`` over stored bytes and NO node on the query path needs LibreOffice.
#:
#: ``convert:ooxml`` re-expresses a legacy ``.doc``/``.xls``/``.ppt`` as the OOXML package of
#: the same application, so the tier-2 readers can open it. Nothing consumes that package
#: yet, and ``render_tasks``'s module docstring names the one concept that is missing before
#: anything can.
#:
#: ``render:pdf`` also appears in
#: :data:`~jmfts_core.models.task_queue.ADVISORY_TASK_TYPES`, which cannot import this module,
#: and ``tests/test_render_tasks.py`` pins the two spellings together — as
#: ``tests/test_citation_task.py`` does for :data:`TASK_CITATION`.
TASK_RENDER_PDF = "render:pdf"
TASK_CONVERT_OOXML = "convert:ooxml"

#: ``OFFICE_SPEC.md`` Part 5's THIRD anchor and Part 11 step 8: the rectangle a passage
#: occupies on a page of the rendition above, recovered by matching the node's own text
#: against the PDF LibreOffice produced. :func:`jmfts_core.citation_tasks.run_anchor_render`.
#:
#: **NOT A ROW OF** :data:`TASK_ROWS`, and it is the second task in the appliance that is
#: decided by what probe measured and still cannot be one (``propose:links`` is the first).
#: Its real precondition is a conjunction this table cannot state: the chunks exist AND a
#: rendition exists. The first half is what ``after_any`` on the two structure rungs says;
#: the second is ``after render:pdf`` — and stating THAT would hang every office document on
#: an appliance with no LibreOffice. ``render:pdf`` is advisory, so a permanent failure of it
#: leaves the node settling normally, while the claim gate counts COMPLETED dependency rows
#: and would leave this task pending forever — and a pending task is unfinished, so the file
#: node could never settle and would never enter an index. The task is offered by
#: :class:`~jmfts_core.rollup_tasks.IngestRollupPlanner` at the settling boundary instead,
#: where "the subtree is complete and the rendition question has been answered" is a fact
#: rather than a race. ``SPRINT_0_6_0.md`` Block B step 7 moved ``index:bm25`` there for a
#: structurally identical reason: a row could not say what the task actually waits for.
#:
#: Advisory, by the same rule as the two anchors above: a chunk with no rectangle is a chunk,
#: the absence is recorded on it, and the passage still has its exact ``ooxml`` address. The
#: spelling is pinned to :data:`~jmfts_core.models.task_queue.ADVISORY_TASK_TYPES` by
#: ``tests/test_render_anchors.py``.
TASK_ANCHOR_RENDER = "anchor:render"

#: ``INGEST_SPEC.md`` 11.5 — put this file's subtree into every BM25 index whose root is
#: the file node or one of its ancestors. The last row of 11.1's parity table, and the one
#: that ``SPRINT_JOBS.md`` 15.4 S5 could not leave open: ``POST /ingest`` used to index its
#: tree into ``default`` at the end of every run, so moving those usetypes onto the queue
#: without this would have taken them out of full-text search without saying so.
#:
#: **NOT IN** :data:`TASK_ROWS` **SINCE** ``SPRINT_0_6_0.md`` **BLOCK B STEP 7**, which is
#: the rollup task 11.5's prose describes it as all along. It was a row on the file node
#: until then, and the row could not reach a spreadsheet: see the deleted row's epitaph in
#: that table, and :mod:`jmfts_core.index_tasks` for the three causes, the set difference
#: that replaced the subtree walk, what moving it costs, and what about 11.5 is still not
#: built.
TASK_INDEX_BM25 = "index:bm25"

#: ``INGEST_SPEC.md`` 11.4's other missing half — knowledge triples from a document's
#: leaves. Path A ran it as the ``extract_facts`` stage; ``SPRINT_JOBS.md`` 15.4 S6 makes
#: it a task, gated on the ``facts.enabled`` option rather than on anything probe measures,
#: because whether to spend an LLM call per chunk is the caller's choice and not a property
#: of the bytes.
#:
#: 736's docstring said facts "belong to rollup (5.4) and are the settling walk's business".
#: They are a row of Part 4's table instead, and :mod:`jmfts_core.fact_tasks` says why.
TASK_EXTRACT_FACTS = "extract:facts"

#: The two rollup tasks, which are NOT in :data:`TASK_ROWS`. Part 4 lists
#: ``structure:semantic`` under structuring, and 11.4 moves it here for the reason 5.4
#: gives: its input is the child sequence and their embeddings, which do not exist until
#: the rung above has finished and its nodes have settled. Nothing probe measures could
#: decide it. Both are planned by ``jmfts_core.rollup_tasks.IngestRollupPlanner`` at the
#: settling boundary instead.
TASK_STRUCTURE_SEMANTIC = "structure:semantic"
TASK_SUMMARIZE = "summarize"

#: The LLM half of ``summarize``, split out so the two can be routed at different pools.
#:
#: Every ``summarize`` needs the embedding model. Only the ones whose concatenated children
#: overflow the embedding window need an LLM — and which ones those are is decided by
#: ``check_fit`` INSIDE the handler, after the claim, so the queue cannot know it at enqueue
#: time. That left no honest badge for ``summarize``: routed at an LLM pool, embedding-only
#: work occupies a scarce resource; routed at an embedding pool, every embedding worker
#: needs an LLM endpoint and LLM cost stops being separately routable.
#:
#: So ``summarize`` now decides and defers. It does the fit check, and either stores the
#: concatenation itself or enqueues one of these, which carries its own badge. The shape is
#: the one ``structure:semantic`` already uses — a task whose product is more tasks.
TASK_SUMMARIZE_LLM = "summarize:llm"

#: ``SPRINT_0_5_0.md`` Block C step 11 — hang the summary ``summarize`` already produced
#: under the derived-tree root as a NODE, with ``summarizes`` links down to the members it
#: covers. :func:`jmfts_core.rollup_tasks.run_summarize_tree` is the handler.
#:
#: **The difference from** :data:`TASK_SUMMARIZE` **is where the summary lives, not what it
#: says.** ``summarize`` writes ``effective_content@self``: the summary is a FIELD on a
#: structural node, which makes it a property of the as-written tree. This reads that field
#: and gives it a node of its own in a PARALLEL tree (3.1), so the summary tree can be
#: retrieved from, projected down onto the source leaves it covers, and compared against
#: another derived tree — none of which a field on somebody else's node can do. It calls no
#: model and no LLM: the vector it copies is the one ``summarize`` computed over exactly
#: this text, so recomputing it would spend a forward pass to get the same numbers.
#:
#: **NOT A ROW OF** :data:`TASK_ROWS`, and for :data:`TASK_SUMMARIZE`'s reason rather than
#: :data:`TASK_VALIDATE_SHAPE`'s. The three rollup types are enqueued by
#: :class:`~jmfts_core.rollup_tasks.IngestRollupPlanner` at the instant the settling walk
#: finds a node's whole subtree complete, because their eligibility is a fact about the
#: children a node HAS — which is not knowable when ``probe`` finishes and is not stable
#: while the tree below is still being built. A row in that table would be evaluated once,
#: from ``(format, patterns, options)``, and would have to name a scope for a node that does
#: not exist yet.
TASK_SUMMARIZE_TREE = "summarize:tree"

#: Give one node the vectors that make it retrievable. A ROW OF :data:`TASK_ROWS` SINCE
#: PHASE 3, and the widest-scoped one: it applies to the leaves of five different rules.
#:
#: It used to be excluded from the table for the same reason the two per-sheet tasks were —
#: it is scoped to a node that does not exist at probe time — with the extra observation
#: that nothing probe measures decides it, because EVERY leaf gets one. Both halves survive
#: as the row: its scope is a set of producers rather than the file node, and its condition
#: is empty, which is what "every leaf gets one" looks like written down. The structure
#: rungs still enqueue it per chunk as they write the chunk (6.4); what they no longer carry
#: is the spec that says what to enqueue.
#:
#: It used to not exist. ``DocumentRepository.create`` took ``auto_embed=True`` and ran the
#: model inline, so the transformer forward pass for every chunk in a document happened
#: inside the one ``structure:declared`` task that created them — which is why that task
#: measured at 54% of ingest wall clock while claiming to be a text-splitting task
#: (:mod:`jmfts_core.task_routing`). Three things follow from splitting it out:
#:
#: * the chunks of one file embed CONCURRENTLY, across the fleet, instead of in a loop;
#: * the expensive work is one task type, so routing it is one badge on one row rather
#:   than a badge on a handler that also parses text;
#: * a worker can run everything EXCEPT this — see ``--runner-url`` in
#:   :mod:`jmfts_core.worker` — which is what lets a fixed set of GPUs be shared between
#:   embedding and summarization instead of pinned to whichever pool ingests.
#:
#: The ordering that used to be free now has to be real: rollup reads its children's
#: vectors, and it stopped being true that a chunk has one the moment it exists. The
#: settling walk already carries that (5.4) and no dependency array does — a chunk is
#: created ``in_flight`` holding this task, so its parent cannot settle and the rollup
#: planner is not called until every chunk under it has finished. See
#: :func:`jmfts_core.structure_tasks._TreeWriter._write_chunks`.
TASK_EMBED = "embed"

#: ``SPRINT_0_5_0.md`` Block A step 2 — run one bound SHACL shape against the data graph its
#: binding's scope resolves to, and write the violation report onto the node the request
#: minted for it. :mod:`jmfts_core.validate_tasks` is the handler and
#: :meth:`~jmfts_core.services.ontology_service.OntologyService.validate_binding` is what
#: enqueues one.
#:
#: **NOT A ROW OF** :data:`TASK_ROWS`, **and it cannot be one.** That table is evaluated from
#: ``(format, patterns, options)`` the moment ``probe`` finishes, and nothing probe measures
#: decides whether a validation run was ASKED FOR — a shape binding is a statement about a
#: scope of documents made long after any of them were ingested, and the scope is usually not
#: one file's subtree at all. A row with no condition would enqueue a validation run for
#: every uploaded file; a row with a condition would need a pattern nothing measures. The
#: three ``fetch:*`` types are already outside the table for the mirror-image reason (they
#: run BEFORE probe has an output), and the shape here is the same one: **a task the REQUEST
#: enqueues, not one the planner does.** ``scripts/generate_reference`` reports both under
#: "enqueued by something other than the table", which is where this belongs.
TASK_VALIDATE_SHAPE = "validate:shape"

#: ``SPRINT_0_5_0.md`` Block B steps 6, 7 and 8 — run one bound shape's ``sh:rule`` set over
#: the ASSERTED triples its binding's scope resolves to, and replace everything that rule had
#: derived with what it concludes now. :mod:`jmfts_core.derive_tasks` is the handler,
#: :mod:`jmfts_core.shacl_rules` the machinery, and
#: :meth:`~jmfts_core.services.ontology_service.OntologyService.derive_binding` is what
#: enqueues one.
#:
#: **NOT A ROW OF** :data:`TASK_ROWS`, for :data:`TASK_VALIDATE_SHAPE`'s reason exactly: the
#: table is evaluated from ``(format, patterns, options)`` the moment ``probe`` finishes, and
#: nothing probe measures says whether somebody bound a shape that carries rules. A row with
#: no condition would run a derivation for every uploaded file.
#:
#: **The one task type in this appliance that writes rows in ``triples`` with**
#: ``derived_by`` **set.** ``extract:facts`` writes triples too and writes them ASSERTED —
#: an extractor works from a source document, which is what a NULL ``derived_by`` means
#: (``models/triple.py``). The two are different layers of the same table and the column is
#: the only thing that says which.
TASK_DERIVE_RULE = "derive:rule"

#: The write mode ``probe`` declares (spec 5.3). ``self``, not ``children``: probe writes
#: this node's own evidence and creates no nodes. Enqueuing follow-on work
#: is not a write to the tree, and declaring ``children`` would reserve a region probe
#: never touches — which would block a real structuring task for no reason.
PROBE_WRITE_MODE = WRITE_SELF

#: Where a node's ingest options are kept between the upload that chose them and the
#: ``probe`` that reads them. ``probe`` runs in a worker, long after the request that
#: accepted the file has returned, so the options cannot be an argument — they have to be
#: on the node. An evidence row since Phase 2b, which is what puts it out of a metadata
#: ``PATCH``'s reach — the column the gate used to protect it in is the caller's now.
OPTIONS_KEY = "options"

#: The ``source`` evidence row — where a document is to be fetched from, written by
#: the request before any bytes exist (``SPRINT_JOBS.md`` 15.4 S8). Spelled here beside
#: :data:`OPTIONS_KEY` because the service writes it and ``fetch_tasks`` reads it.
SOURCE_KEY = "source"


@dataclass(frozen=True)
class TaskOutcome:
    """What a handler did. Everything here lands in the attempt record (spec 3.4).

    ``status`` is ``'completed'`` or ``'skipped'``. A handler that *failed* raises; it
    does not return a status saying so, because the worker has to classify the exception
    to decide about a retry and a returned string carries no exception to classify.
    """

    detail: dict = field(default_factory=dict)
    produced: Optional[dict] = None
    rung: Optional[str] = None
    status: str = "completed"


class TaskHandler(Protocol):
    """Runs one claimed task inside the worker's transaction.

    The session is the worker's own, one per task, never a request-scoped one. The
    handler writes whatever the task writes and returns; the worker commits, or rolls
    back and records the failure.
    """

    def __call__(self, session: Session, task: TaskQueue) -> TaskOutcome: ...


TASK_HANDLERS: dict[str, TaskHandler] = {}


class UnknownTaskError(LookupError):
    """No handler is registered for a queued task type.

    Deliberately not survivable. The alternatives are worse in both directions: silently
    completing the row publishes a node whose work never ran, and silently leaving it
    pending stalls the ingestion with nothing saying why. The worker fails the task with
    a PERMANENT classification, which puts the reason in the node's attempt log and stops
    the retry budget being spent on a name that will not appear by itself.
    """


def register_task_handler(
    task_type: str,
    *,
    consumes: tuple[str, ...],
    produces: tuple[str, ...],
    write_mode: str,
    cost_class: str,
    fanout: Optional[Fanout] = None,
    child_key: Optional[ChildKey] = None,
) -> Callable[[TaskHandler], TaskHandler]:
    """Register the handler for ``task_type`` and its atom. Duplicate registration is an error.

    THE DECLARATION IS REQUIRED, and it is required here rather than in a second decorator
    so that :data:`TASK_HANDLERS` and :data:`~jmfts_core.atoms.ATOMS` hold the same task
    types by construction. Two registries with two decorators would need a test to say they
    agree, and that test would fail on the day somebody adds a handler — which is the day
    the declaration is easiest to write and hardest to remember.

    ``SPRINT_JOBS.md`` Part 2 for what the fields mean, and 2.2 for why every evidence name
    carries a locus. Nothing in the ingest path reads the declaration yet: Phase 1 is the
    audit, and ``tests/test_atom_declarations.py`` is what reads it.
    """

    def decorate(handler: TaskHandler) -> TaskHandler:
        existing = TASK_HANDLERS.get(task_type)
        # Re-registering the SAME function is how a module re-imported under pytest or
        # uvicorn --reload behaves, and it is harmless. Two different functions under one
        # name is a real collision, and whichever import happened to run last would win.
        if existing is not None and existing is not handler:
            raise ValueError(
                f"task type {task_type!r} already has a handler ({existing!r}); "
                "two handlers under one name would be resolved by import order"
            )
        declare(
            task_type,
            consumes=consumes,
            produces=produces,
            write_mode=write_mode,
            cost_class=cost_class,
            fanout=fanout,
            child_key=child_key,
        )
        TASK_HANDLERS[task_type] = handler
        return handler

    return decorate


def has_task_handler(task_type: str) -> bool:
    return task_type in TASK_HANDLERS


def get_task_handler(task_type: str) -> TaskHandler:
    handler = TASK_HANDLERS.get(task_type)
    if handler is None:
        raise UnknownTaskError(
            f"no handler is registered for task type {task_type!r}; "
            f"known types are {sorted(TASK_HANDLERS)}"
        )
    return handler


# ---------------------------------------------------------------------------
# Part 4 — the enqueue conditions
# ---------------------------------------------------------------------------

#: The pattern names a guard may read. Written as constants rather than as literals in the
#: rows because half of this table already was — the four sentinels below are constants,
#: and line-for-line a row could hold one of each and give no sign they were the same kind
#: of thing.
HAS_TEXT_LAYER = "has_text_layer"
HAS_HEADINGS = "has_headings"
HAS_HEADING_STYLES = "has_heading_styles"
HAS_OUTLINE = "has_outline"
HAS_SLIDES = "has_slides"
HAS_SHEETS = "has_sheets"
HAS_IMAGES = "has_images"
IS_SCANNED = "is_scanned"
IS_CONVERSATION = "is_conversation"
IS_DAMAGED = "is_damaged"
IS_LEGACY_BINARY = "is_legacy_binary"
CHAR_COUNT = "char_count"
PAGES_WITH_TABLES = "pages_with_tables"

#: Every pattern a guard may read, and what it holds. ``SPRINT_JOBS.md`` 4.4.
#:
#: A CLOSED list, and what closes it is that a guard is the one reader for which an unknown
#: name and a false one are indistinguishable. ``matched.patterns`` is an OPEN namespace by
#: design and that design is right for storage: ``jmfts_core.evidence`` states the rule —
#: everything probe emits is a flag unless ``PATTERN_TYPES`` names it — and
#: ``evidence.pattern_type`` therefore answers ``bool`` for a name it has never heard of.
#: Read a guard through that rule and ``has_hedings`` is a well-typed boolean comparison
#: that plans cleanly, stands down on every document forever, and reports a not-applicable
#: sentence naming a pattern nobody measures. So the guard vocabulary is declared here, as
#: a subset of that namespace, and ``evidence``'s rule is left alone.
#:
#: The TYPE is declared rather than derived for the same reason. ``pages_with_tables`` is
#: the case that proves the default wrong: it holds a list of page numbers,
#: ``pattern_type`` calls it a flag, and no test catches the difference because probe never
#: emits it. Truthiness survived that; ``>`` would not.
#:
#: This list is not probe's vocabulary and is not meant to converge on it. Probe measures
#: 31 patterns and a guard reading none of them is not an error. The direction that IS
#: audited is the other one, in ``tests/test_rule_guards.py``: a name here that the
#: installed probe does not emit has to be in :data:`PATTERNS_NOT_PROBED`, saying who
#: writes it instead.
GUARDABLE_PATTERNS: dict[str, str] = {
    HAS_TEXT_LAYER: TYPE_BOOL,
    HAS_HEADINGS: TYPE_BOOL,
    HAS_HEADING_STYLES: TYPE_BOOL,
    HAS_OUTLINE: TYPE_BOOL,
    HAS_SLIDES: TYPE_BOOL,
    HAS_SHEETS: TYPE_BOOL,
    HAS_IMAGES: TYPE_BOOL,
    IS_SCANNED: TYPE_BOOL,
    IS_CONVERSATION: TYPE_BOOL,
    IS_DAMAGED: TYPE_BOOL,
    # `probe._probe_ole2` measures it from the OLE2 directory, and it is the one pattern that
    # separates a 1997 `.doc` from an encrypted OOXML package — the two carry identical magic
    # bytes and go opposite ways. `convert:ooxml` is what it decides.
    IS_LEGACY_BINARY: TYPE_BOOL,
    CHAR_COUNT: TYPE_INT,
    PAGES_WITH_TABLES: TYPE_LIST,
}

#: The guardable patterns probe does not emit, and who does. Two entries, and they are two
#: different facts that a single "unmeasured" would flatten.
#:
#: Neither is an error and neither is a gap to close here. A row guarded on an unmeasured
#: name stands down with that as its stated reason, which is 4.4's ``requires`` policy
#: working: the precondition cannot be confirmed, so the work is not done.
PATTERNS_NOT_PROBED: dict[str, str] = {
    HAS_HEADING_STYLES: (
        "planned, not built: OFFICE_SPEC.md Part 2 specifies it and no prober emits it, so "
        "`structure:declared` stands down on every .docx and `structure:inferred` runs "
        "instead — which is the correct rung for a file whose headings are not known to be "
        "declared"
    ),
    PAGES_WITH_TABLES: (
        "written by `extract:text`, not by probe (`structure_tasks._extract_pdf` puts it in "
        "the extraction RECORD for exactly this reason). Nothing re-plans a node after "
        "extraction, so `extract:tables` is unreachable in the ingest path today and its "
        "not-applicable sentence says the honest thing: nobody has looked yet"
    ),
}


#: The comparisons a guard term may use. Six, and no ``and``/``or``/``not``: ``requires`` is
#: already a conjunction and ``forbids`` already a disjunction of exclusions, and dropping
#: the connectives is what keeps one not-applicable sentence per TERM rather than one per
#: expression. ``SPRINT_JOBS.md`` 4.4 point 3.
OP_EQ = "="
OP_NE = "!="
OP_LT = "<"
OP_LE = "<="
OP_GT = ">"
OP_GE = ">="

#: The operators that need an ordered type on both sides. Checked at import, because
#: ``has_text_layer > 3`` is a row that would raise inside planning on a real document.
ORDERING_OPS: tuple[str, ...] = (OP_LT, OP_LE, OP_GT, OP_GE)

GUARD_OPS: dict[str, Callable[[object, object], bool]] = {
    OP_EQ: operator.eq,
    OP_NE: operator.ne,
    OP_LT: operator.lt,
    OP_LE: operator.le,
    OP_GT: operator.gt,
    OP_GE: operator.ge,
}

#: How an operator reads inside a not-applicable SENTENCE. The symbol is what ``EXPLAIN``
#: reports in a row's ``requires``/``forbids`` list; this is what the reason says, because
#: "does not run when it is = True" is not a sentence and the reason is read by a person
#: trying to find out why their document has no children.
OP_PHRASES: dict[str, str] = {
    OP_EQ: "is",
    OP_NE: "is not",
    OP_LT: "is less than",
    OP_LE: "is at most",
    OP_GT: "is more than",
    OP_GE: "is at least",
}

#: A Python value's :data:`GUARDABLE_PATTERNS` type. Exact types, not ``isinstance`` — a
#: ``bool`` is an ``int`` to ``isinstance`` and is not one here, which is the whole point of
#: refusing ``has_text_layer > 3``.
_TYPE_OF: dict[type, str] = {
    bool: TYPE_BOOL,
    int: TYPE_INT,
    float: TYPE_FLOAT,
    str: TYPE_STR,
    list: TYPE_LIST,
    dict: TYPE_DICT,
}


@dataclass(frozen=True)
class Option:
    """A reference to one resolved option, for either side of a guard term.

    ``SPRINT_JOBS.md`` 4.4 point 2, and the thing that makes 14.2's promise true. A
    threshold written as a literal in this table moves only by a code change; written as an
    option reference it moves through the three-layer stack ``resolve_options`` already has,
    and ``EXPLAIN`` reports both the name and the value it resolved to.
    """

    group: str
    name: str

    def __str__(self) -> str:
        return f"options.{self.group}.{self.name}"


@dataclass(frozen=True)
class Term:
    """One condition in a row's ``requires`` or ``forbids``.

    ``op`` of ``None`` is TRUTHINESS, and it is not sugar for ``= True``. Every guard in
    this table read a name for truthiness before operators existed and they all still do;
    rewriting them as ``= True`` would be wrong for ``pages_with_tables``, which holds a
    list that is never equal to ``True``, and for any count, where the meaning is "not
    zero".

    ``left`` is a pattern name, a sentinel from :data:`SENTINEL_PATTERNS`, or an
    :class:`Option`. The last is what ``enabled_by`` became.
    """

    left: object
    op: Optional[str] = None
    right: object = None

    @property
    def reads_option(self) -> bool:
        """Whether this term asks about the REQUEST rather than about the file.

        What orders the two in :func:`_blocking_reason`, and the reason is inherited from
        the ``_disabled_reason`` this replaced: a caller who turned fact extraction off is
        owed that answer, not "the structure rung it comes after did not run" — which is
        also true and is not why.
        """
        return isinstance(self.left, Option)


def option(group: str, name: str) -> Option:
    """An option reference for a guard term. See :class:`Option`."""
    return Option(group, name)


def term(left: object, op: Optional[str] = None, right: object = None) -> Term:
    """One guard condition. See :class:`Term`.

    A lowercase helper for the same reason ``children_of`` is one: a row reads as the
    sentence Part 4 writes, and the dataclass is what the code downstream matches on.
    """
    return Term(left, op, right)


def _as_term(entry: Union[str, Term]) -> Term:
    """A row's guard entry as a :class:`Term`. A bare name is a truthiness term."""
    return entry if isinstance(entry, Term) else Term(entry)


def render_term(t: Term, fmt: str) -> str:
    """A guard term as the sentence ``EXPLAIN`` reports, resolved for ``fmt``.

    A truthiness term on a pattern renders as the BARE resolved pattern name, which is what
    ``ExplainedTask.requires`` has always held. Operators were added to the table without
    changing what a row that has none says about itself.
    """
    left = t.left if not isinstance(t.left, str) else _resolve_pattern(t.left, fmt)
    if t.op is None:
        return str(left)
    right = t.right if isinstance(t.right, Option) else repr(t.right)
    return f"{left} {t.op} {right}"


#: Which pattern, per format, means "this file declares its own structure" (spec 3.5's
#: `declared` rung: PDF/EPUB outline, DOCX heading styles, PPTX slide list, XLSX sheets,
#: Markdown ATX, HTML h1-h6). The PDF and text probers exist today; every other entry
#: names a pattern probe does not yet report — and the condition is then simply false,
#: recorded as such, rather than assumed either way.
#:
#: There is no ``markdown`` key, and 11.3 records the measurement behind that: nothing in
#: the bytes distinguishes authored markdown from a ``.txt`` file that opens with a ``#``,
#: so ``detect_format`` reports both as ``text`` and one entry covers them. The ``html``
#: entry is kept even though HTML also arrives as ``text`` today — it is a true statement
#: about the format, and the pattern that actually keeps HTML out of the prose path is
#: ``has_markup`` on the ``extract:text`` row below.
DECLARED_STRUCTURE_PATTERN: dict[str, str] = {
    "pdf": HAS_OUTLINE,
    "epub": HAS_OUTLINE,
    "docx": HAS_HEADING_STYLES,
    "pptx": HAS_SLIDES,
    "xlsx": HAS_SHEETS,
    "text": HAS_HEADINGS,
    "html": HAS_HEADINGS,
}

#: Stands in a row's ``requires``/``forbids`` for "whatever pattern THIS format uses to
#: declare its own structure", resolved through the dict above. Written as a sentinel
#: rather than as one row per format because the rule is one rule — the declared rung runs
#: when the file declares something — and thirteen near-identical rows would let the copies
#: drift apart. A format with no entry at all resolves to nothing, and that asymmetry is
#: the point: as a requirement it can never be satisfied, as a prohibition it is always
#: satisfied, so an unknown format gets the inferred rung rather than neither rung.
DECLARED_STRUCTURE = "@declared_structure"

#: Which pattern, per format, means "this file has a per-page geometry a rectangle can be
#: expressed in". The condition ``OFFICE_SPEC.md`` Part 5's ``citation`` row is predicated
#: on, and the same shape as :data:`DECLARED_STRUCTURE_PATTERN` for the same reason: it is a
#: fact about the FORMAT, and a row that named a bare pattern could not express it.
#:
#: PDF is the only entry, and the absence of the others is the schedule. Part 11 orders
#: citation-for-PDF (step 2) before any office format is read, because for a source PDF the
#: source anchor and the rendition anchor are the same object — the geometry is already
#: computed during extraction — while ``docx``, ``pptx`` and ``xlsx`` have no pages at all
#: until LibreOffice paginates them (step 7's ``render:pdf``). An office format therefore
#: reaches this row only once it can carry a rendition, and until then the row is reported
#: as impossible for it rather than as a condition that happened to be false.
PAGE_GEOMETRY_PATTERN: dict[str, str] = {
    "pdf": HAS_TEXT_LAYER,
}

#: Stands in a row's ``requires``/``forbids`` for "whatever pattern says THIS format carries
#: a page geometry", resolved through the dict above.
PAGE_GEOMETRY = "@page_geometry"

#: Which pattern, per format, means "this file's own package can address the region a chunk
#: came from". ``OFFICE_SPEC.md`` Part 5's SOURCE anchor, which is a different fact from the
#: dict above and therefore a different sentinel.
#:
#: THE TWO DICTS ARE DISJOINT AND THAT IS THE DESIGN, not an oversight to be tidied up by
#: merging them. A ``.docx`` has no pages until Word or LibreOffice paginates it, so it can
#: never satisfy :data:`PAGE_GEOMETRY_PATTERN`; a PDF has no OOXML part to name an element
#: in, so it can never satisfy this one. Each format reaches an anchor by the one route its
#: bytes support, and a format that reached both would be one whose two anchors could
#: disagree — which is the state Part 5 keeps them in separate rows to prevent.
#:
#: ``has_text_layer`` for both entries, and it is the right condition rather than a
#: convenient one: ``probe`` measures it for ``docx`` from ``word/document.xml`` and for
#: ``pptx`` from the slides, so it is exactly "there is authored text in this package", and
#: a package with none has no block for an address to point at.
SOURCE_ADDRESS_PATTERN: dict[str, str] = {
    "docx": HAS_TEXT_LAYER,
    "pptx": HAS_TEXT_LAYER,
}

#: Stands in a row's ``requires``/``forbids`` for "whatever pattern says THIS format's own
#: package can address a region", resolved through the dict above.
SOURCE_ADDRESS = "@source_address"

#: Which pattern, per format, means "this file's own container names a list of sheets".
#: ``INGEST_SPEC.md`` 8.1's condition, and a third per-format dict rather than a reuse of
#: :data:`DECLARED_STRUCTURE_PATTERN` because the two answer different questions. That one
#: says which pattern makes a format's DECLARED rung applicable; this one says which
#: pattern means the declared structure is readable from the container without any text
#: having been extracted first. ``xlsx`` is in both, with the same pattern, and the two
#: entries are not a copy: the day a format declares sheets AND has a text layer, the rows
#: reading these two dicts want different answers about it.
SHEET_LIST_PATTERN: dict[str, str] = {
    "xlsx": HAS_SHEETS,
}

#: Which pattern, per format, means "this file is a transcript of a conversation".
#: ``text`` is the only entry and the absence of the others is the statement: a PDF of a
#: chat log is a PDF, and nothing in this appliance reads turn boundaries out of one.
#:
#: A fourth per-format dict rather than a bare pattern name, for the reason the three above
#: give: as a REQUIREMENT it can never be satisfied by a format with no entry, and as a
#: PROHIBITION it is always satisfied — so `structure:inferred` forbidding it holds back a
#: JSONL transcript and leaves every PDF alone, with no row per format.
CONVERSATION_PATTERN: dict[str, str] = {
    "text": IS_CONVERSATION,
}

#: Which pattern, per format, means "there is a document in here for LibreOffice to
#: paginate". ``OFFICE_SPEC.md`` Part 6's ``render:pdf`` row is predicated on it, and it is a
#: fifth per-format dict for the reason the four above are: the condition is a fact about the
#: FORMAT, and a row naming a bare pattern could not express it.
#:
#: **THERE IS NO ``pdf`` ENTRY AND THAT IS THE WHOLE RULE.** A PDF already has pages, so a
#: rendition of one would be a byte-for-byte second copy of the upload — the row is reported
#: impossible for it, which is what the asymmetry in :data:`DECLARED_STRUCTURE`'s docstring
#: buys: as a requirement an absent entry can never be satisfied. ``repositories.rendition
#: .pdf_for`` is the other half of the same decision, serving a PDF from its own blob.
#:
#: Each entry names a pattern that says the file has CONTENT, not merely that it is of that
#: format, and each is the pattern probe actually emits for it: a ``.pptx`` with no slides
#: and a workbook with no sheets paginate into nothing, and spending a LibreOffice run to
#: find that out is a run nobody needed. ``ole2`` names ``is_legacy_binary`` for a stronger
#: version of the same point — an MSI or a ``Thumbs.db`` carries the same magic bytes as a
#: ``.doc`` and is not a document at all.
RENDITION_SOURCE_PATTERN: dict[str, str] = {
    "docx": HAS_TEXT_LAYER,
    "pptx": HAS_TEXT_LAYER,
    "xlsx": HAS_SHEETS,
    "ole2": IS_LEGACY_BINARY,
}

#: Stands in a row's ``requires``/``forbids`` for "whatever pattern says THIS format holds a
#: document that has to be paginated before it has pages", resolved through the dict above.
RENDITION_SOURCE = "@rendition_source"

#: Stands in a row's ``requires``/``forbids`` for "whatever pattern says THIS format names
#: a sheet list", resolved through the dict above.
SHEET_LIST = "@sheet_list"

#: Stands in a row's ``requires``/``forbids`` for "whatever pattern says THIS format is a
#: transcript", resolved through the dict above.
CONVERSATION = "@conversation"

#: Every sentinel, and the per-format dict that resolves it. A registry rather than a chain
#: of ``if name == ...`` so that :func:`_resolve_pattern` and the ``EXPLAIN`` path cannot
#: come to know different numbers of sentinels — an unresolved sentinel silently read as a
#: literal pattern name would make a row impossible-for-every-format with no reason anybody
#: could read.
SENTINEL_PATTERNS: dict[str, dict[str, str]] = {
    DECLARED_STRUCTURE: DECLARED_STRUCTURE_PATTERN,
    PAGE_GEOMETRY: PAGE_GEOMETRY_PATTERN,
    SOURCE_ADDRESS: SOURCE_ADDRESS_PATTERN,
    SHEET_LIST: SHEET_LIST_PATTERN,
    CONVERSATION: CONVERSATION_PATTERN,
    RENDITION_SOURCE: RENDITION_SOURCE_PATTERN,
}

#: Why a task whose Part 4 condition HOLDS is nevertheless not enqueued: its handler
#: belongs to a later phasing step. Kept as data rather than as a comment because it is
#: written into the node's attempt log, where it is the answer to "why did nothing happen
#: to this file after probe".
DEFERRED_REASON: dict[str, str] = {
    TASK_EXTRACT_TABLES: (
        "tables are extracted inline into the markdown by `extract:text`; a task that "
        "gives each table its own node is INGEST_SPEC.md phasing step 5's remainder and "
        "has no handler registered"
    ),
    TASK_EXTRACT_IMAGES: "image handling is INGEST_SPEC.md phasing step 7; no handler registered",
    # THIS ONE IS NOT LIKE THE OTHERS, and the difference is worth reading before treating
    # it as another missing handler. A review task is not claimed by a worker at all
    # (Part 7.3) and must not hold a node in flight (Part 7.4), and the queue as it stands
    # gives neither: `claim_next` lets an un-badged worker take any row with a NULL badge, so
    # a queued `review:link` would be claimed within seconds and failed PERMANENT by
    # `UnknownTaskError`; and `settle_node` calls `structuring_complete`, which counts every
    # unfinished task, so one that a person answers next month would park the workbook until
    # they did.
    #
    # RE-CHECKED 2026-09-14 against a queue three sprints older than the argument, because
    # an excuse that has quietly expired is worse than the thing it excuses. Both halves
    # still hold, and the two things that came close are not it. `ADVISORY_TASK_TYPES`
    # (`models/task_queue.py`) exempts a type from the `settled` write, but only on a
    # FAILURE — a pending row still blocks — and its own stated rule is about what a settled
    # node MEANS, which is a different question from a task nobody has answered yet.
    # `requeue_expired_claims` takes a lease duration, but it is the heartbeat clock for a
    # LIVE worker; `recover_claims`' docstring names 7.3's review lease as the general case
    # it deliberately does not implement, "held for weeks", with no duration to give.
    #
    # So `propose:links` writes each candidate's Part 7.6 payload onto the workbook node and
    # enqueues nothing. The proposals exist, in the shape the review surface will read them
    # in; what is missing is the claim lease and the `GET /tasks` surface 7.3 asks for, and
    # half-building it as a queue row a worker eats is worse than not building it.
    TASK_REVIEW_LINK: (
        "a review task is claimed by a person or an agent and never by a worker "
        "(INGEST_SPEC.md 7.3), and it must not block settling (7.4); the queue has neither "
        "the claim lease nor the exclusion today, so `propose:links` writes each "
        "candidate's 7.6 payload onto the workbook node instead of queueing a row that a "
        "worker would claim and fail"
    ),
}
# There is no `TASK_EXTRACT_SHEET` entry, and its absence is the record of a decision.
# It carried one — "8.4 has four representations and 8.8 leaves every threshold that
# decision reads unset" — for as long as the task had no handler. `run_extract_sheet` now
# runs ONE of those four, `records`, on a rule that is not a threshold: 8.3's `header_row`
# is a measured boolean, and a sheet whose first row names every column has record keys
# whatever the uncalibrated numbers turn out to be. The other three shapes are still
# unbuilt and a sheet that needs one gets no records with the reason on the node
# (`jmfts_core.sheet_records.NO_HEADER_REASON`), which is a per-sheet fact and not a
# per-task-type one.


@dataclass(frozen=True)
class SkippedTask:
    """A task the spec says to record as never-attempted, with the reason 3.4 demands."""

    task_type: str
    reason: str


# ---------------------------------------------------------------------------
# Scope — SPRINT_JOBS.md 4.1
# ---------------------------------------------------------------------------

#: A row scoped to the node the plan is rooted at: the uploaded file node, which is the one
#: ``probe`` ran on. ``SPRINT_JOBS.md`` 4.1's first form, "an explicit node id, permitted
#: only at the root of a binding".
SCOPE_ROOT = "root"

#: A row scoped to children another row produced. 4.1's second form, and the one that
#: removes the shoehorning Part 0 measured: a ``TaskRow`` used to have no way to name a
#: scope other than the file node, so every task scoped to a sheet or a chunk was written
#: into a handler as a literal ``TaskSpec`` instead of declared as a row.
SCOPE_CHILDREN = "children"


@dataclass(frozen=True)
class Scope:
    """Which nodes a row applies to. ``SPRINT_JOBS.md`` 4.1, with one departure recorded.

    **No queries.** A scope is resolved from two columns of the node in front of the
    planner — ``produced_by`` and ``usetype`` — and never from a search over the tree. The
    parent rule's fan-out IS the child rule's multiplicity, so cardinality composes without
    anything having to read the tree.

    **4.1 SAYS "THE CHILDREN PRODUCED BY ANOTHER NAMED RULE", SINGULAR, AND NAMES NO
    USETYPE. Both halves are widened here, each for a reason the codebase already argues
    somewhere else.**

    * *Several producing rules.* ``embed`` applies to the leaves of five different rules.
      Written 4.1's way that is five rows all called ``embed``, differing in one field —
      which is what :data:`DECLARED_STRUCTURE` refused to do for thirteen formats, in those
      words: "thirteen near-identical rows would let the copies drift apart". It is also
      the disjunction :attr:`TaskRow.after_any` already exists to express.
    * *A usetype.* ``produced_by`` alone cannot separate the two kinds of node one rung
      writes. ``structure:declared`` writes ``section`` containers AND ``chunk`` leaves, and
      only the chunks carry text to embed. The model's own comment draws the line this
      relies on — ``usetype`` describes what a node *is*, ``produced_by`` what made it — so
      a scope that needs both is naming two facts about one node rather than running a
      query about it.

    Both are read off the child node with no I/O, so 4.1's actual constraint holds.
    """

    kind: str
    #: Rules whose children this row applies to. Any one of them suffices.
    produced_by: tuple[str, ...] = ()
    #: Kinds of child this row applies to. Any one of them suffices.
    usetypes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind == SCOPE_ROOT:
            if self.produced_by or self.usetypes:
                raise ValueError(
                    "the root scope is one node and names no producer and no usetype; "
                    f"got produced_by={self.produced_by!r}, usetypes={self.usetypes!r}"
                )
            return
        if self.kind != SCOPE_CHILDREN:
            raise ValueError(f"unknown scope kind {self.kind!r}; expected one of {SCOPE_KINDS}")
        # Both halves are required, and neither defaults. A child scope with no producer
        # would match every node in the tree; one with no usetype would match both kinds a
        # rung writes, which is the mistake the class docstring exists to prevent.
        if not self.produced_by:
            raise ValueError("a children scope must name at least one producing rule")
        if not self.usetypes:
            raise ValueError("a children scope must name at least one child usetype")

    def matches(self, produced_by: Optional[str], usetype: Optional[str]) -> bool:
        """Whether a child stamped ``produced_by`` and typed ``usetype`` is in this scope.

        ``None`` matches nothing, in both columns. A node with no stamp was asserted, not
        produced (4.2), and a rule scoped to a rule's output has no business rescheduling
        work over a node a person wrote.
        """
        if self.kind != SCOPE_CHILDREN:
            return False
        return produced_by in self.produced_by and usetype in self.usetypes

    def __str__(self) -> str:
        if self.kind == SCOPE_ROOT:
            return "@root"
        return f"@children_of({'|'.join(self.produced_by)}):{'|'.join(self.usetypes)}"


SCOPE_KINDS: tuple[str, ...] = (SCOPE_ROOT, SCOPE_CHILDREN)

#: The file node ``probe`` ran on. Every row of Part 4's original table is implicitly here,
#: and naming it is what makes the implicitness go away.
ROOT_SCOPE = Scope(SCOPE_ROOT)


def children_of(*produced_by: str, usetypes: tuple[str, ...]) -> Scope:
    """4.1's second form: the children any of ``produced_by`` wrote, of these usetypes."""
    return Scope(SCOPE_CHILDREN, produced_by=produced_by, usetypes=usetypes)


@dataclass(frozen=True)
class TaskRow:
    """One row of Part 4's table, as data the planner reads rather than code it runs.

    A row's condition is deliberately restricted to three things — named patterns that
    must be true, named patterns that must be false, and rows it comes after. That
    restriction is the whole point of the shape: spec 11.2 wants an ``EXPLAIN`` that can
    answer "what would a ``.pptx`` with these options do?" from a format and a set of
    options, without bytes and without running anything, and that only holds if every
    enqueue decision is a pure function of ``(format, patterns, options)``. A predicate
    written as a lambda would be pure too, but it would not be *inspectable*: the reason a
    row did not fire could then only be discovered by running it, and the hand-written
    ``not_applicable`` string it needed would be one more thing to keep in step with the
    condition. Naming the patterns lets both the decision and its explanation come from
    the same declaration.
    """

    task: str
    #: Spec 5.3's declared write mode. Optional only because a row that is always recorded
    #: as skipped never reaches the queue and so has no region to declare; ``__post_init__``
    #: refuses a row that could be enqueued without one, rather than defaulting to a mode
    #: that would reserve a region the task never writes.
    write_mode: Optional[str] = None
    #: Which nodes this row applies to (:class:`Scope`, ``SPRINT_JOBS.md`` 4.1). Defaults to
    #: the file node, which is where every row of Part 4's table sat implicitly before
    #: Phase 3 — so the default is the old behaviour written down rather than assumed.
    #:
    #: A row scoped to children is NOT evaluated when ``probe`` runs. There is no sheet node
    #: yet, so there is no ``profile:sheet`` instance to enqueue; the row is what 6.2 calls
    #: LATENT, and the rule that creates the node asks for it at the moment it does
    #: (:func:`plan_frontier`). ``after`` and ``after_any`` order rows WITHIN one scope; the
    #: scope itself is what orders a child-scoped row after the rule that produced its node,
    #: which is a stronger statement than a queue dependency — the node does not exist.
    scope: Scope = ROOT_SCOPE
    #: Rows — earlier in :data:`TASK_ROWS` — that must ALL be eligible before this one is.
    #: Resolved to real queue dependencies by ``enqueue_batch`` (spec 5.5).
    after: tuple[str, ...] = ()
    #: Rows — earlier in :data:`TASK_ROWS` — of which AT LEAST ONE must be eligible. This
    #: row is then ordered after every one of them that is.
    #:
    #: A second field rather than a looser reading of ``after``, because the two express
    #: different dependencies and a row needs to be able to state either. ``after`` is a
    #: conjunction: ``structure:declared`` needs ``extract:text`` and there is no
    #: alternative to it. ``after_any`` is a disjunction over rows that are ALTERNATIVES to
    #: each other — the two structure rungs, of which exactly one is ever eligible for a
    #: document (they read the same sentinel with opposite signs). ``citation`` comes after
    #: whichever of them wrote the chunks, and expressing that as ``after`` would name two
    #: rows that can never both be eligible, so the row would never fire and the table would
    #: say the opposite of what it meant.
    #:
    #: The disjunction is resolved to real queue dependencies exactly like ``after``: the
    #: names that came out eligible become this task's ``dependencies``, so the queue still
    #: gates the claim on those rows being ``completed`` rather than merely enqueued.
    after_any: tuple[str, ...] = ()
    #: Every term must HOLD. A bare name is the truthiness term ``term(name)``, and
    #: ``__post_init__`` normalises it, so a read of this field always sees
    #: :class:`Term`. A name in :data:`SENTINEL_PATTERNS` resolves per format first, and a
    #: format with no entry makes the row impossible rather than false.
    #:
    #: A row may name a pattern its predecessor already requires — ``has_text_layer`` on
    #: the rows that come after ``extract:text`` — and that repetition is deliberate. It
    #: decides nothing (the dependency check reaches it first, and says so more precisely),
    #: but it keeps a row's condition readable as Part 4 writes it, without following
    #: ``after`` up the table.
    #:
    #: AN UNMEASURED NAME BLOCKS THE ROW, and that policy is this field's, not the term's.
    #: See ``forbids`` for the other half, and 4.4 for why the two must stay apart.
    requires: tuple[Union[str, Term], ...] = ()
    #: No term may HOLD. The opposite unknown policy: an unmeasured name is not evidence of
    #: a blocker, so it does not block.
    #:
    #: A CALLER'S THRESHOLD BELONGS HERE, not in ``requires``, and the reason is a
    #: measurement rather than a preference. Probe's counts are per-format —
    #: ``char_count`` is emitted for ``text`` alone — so ``requires`` on one would silently
    #: stop the row for every format that does not measure it. Stated as an exclusion the
    #: same threshold reads as what a caller means by it: do not spend this work on a
    #: document we MEASURED as too small, and spend it when nobody measured.
    forbids: tuple[Union[str, Term], ...] = ()
    #: Which group of the format's resolved options this row's queue row carries. The row
    #: names WHICH parameters its task takes; ``jmfts_core.ingest_options`` says what they
    #: are for the format being planned. A row with no key takes no parameters at all,
    #: which is a different fact from taking them and leaving them at their defaults — the
    #: empty params dict is what tells 6.1's re-run diff that nothing about this task can
    #: be tuned into needing a second run.
    params_key: Optional[str] = None
    #: If set, a row whose condition HOLDS is recorded as skipped rather than enqueued —
    #: Part 4's ``ocr``. Not the same fact as a deferred task (:data:`DEFERRED_REASON`),
    #: which is a condition that holds and an implementation that has not shipped.
    skip_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.write_mode is None and self.skip_reason is None:
            raise ValueError(
                f"task row {self.task!r} can be enqueued but declares no write mode; "
                "spec 5.3 requires one before a task may claim a region"
            )
        # A bare name becomes a truthiness term HERE, once, so that nothing downstream has
        # to hold both shapes. Every reader of these two fields — the evaluator, EXPLAIN,
        # the import-time audit — sees `Term` and only `Term`.
        object.__setattr__(self, "requires", tuple(_as_term(t) for t in self.requires))
        object.__setattr__(self, "forbids", tuple(_as_term(t) for t in self.forbids))


#: Part 4's table. Order is significant twice over: ``after`` may only name a row above,
#: and the eligible list comes out in this order, which is the order the batch is enqueued
#: in.
TASK_ROWS: tuple[TaskRow, ...] = (
    # `self`, for the same reason probe is (see PROBE_WRITE_MODE): extraction writes this
    # node's own `content` and `extraction` block and creates nothing. The nodes are made
    # by the structure task that follows it, which is the one that declares `children`.
    #
    # `has_markup` USED TO BE a `forbids` here, and is not one any more. The reasoning it
    # carried was sound and is still true: HTML arrives as `text` (11.3's measurement), and
    # the text extractor is a decoder, so decoding HTML yields HTML — which would chunk with
    # its tags intact and settle looking exactly like a success. What was missing was not
    # the prohibition but the reader. An appliance with no HTML reader had only one way to
    # refuse, and refusing produced a document with no content and no children while the
    # upload returned 200 and every task reported "completed".
    #
    # `structure_tasks.MARKUP_EXTRACTOR` is that reader now, so the pattern SELECTS a
    # reader instead of blocking the row, and the hazard the prohibition guarded against is
    # answered by conversion rather than by refusal. The pattern is still measured, still
    # reported, and still the thing that decides — it just decides which reader rather than
    # whether. This row therefore has no `forbids` at all: every file with a text layer
    # gets an extractor, and a format with no reader still fails loudly inside the handler.
    #
    # `is_damaged` is the appliance's OWN guard and takes a literal, not an option
    # reference: a caller may not ask for text to be extracted from a file probe measured
    # as unreadable. It is PDF-only (`probe._probe_pdf` derives it from a zero page count
    # over non-empty bytes), so for every other format the name is absent and `forbids`'
    # unknown policy leaves the row alone — which is the correct reading of "nobody
    # measured whether this file is damaged".
    TaskRow(
        TASK_EXTRACT_TEXT,
        write_mode=WRITE_SELF,
        requires=(HAS_TEXT_LAYER,),
        forbids=(term(IS_DAMAGED, OP_EQ, True),),
    ),
    # Part 4 marks ocr "out of scope for v1, recorded as skipped", so it is the one task
    # the spec itself says to log as never-attempted rather than to enqueue.
    TaskRow(
        TASK_OCR,
        requires=(IS_SCANNED,),
        skip_reason=(
            "OCR is out of scope for v1 (INGEST_SPEC.md Part 4); the document has page "
            "images and no usable text layer, so its text is not recoverable by this "
            "appliance"
        ),
    ),
    # The two structure rungs are ONE decision read both ways, which is why they sit next
    # to each other reading the same sentinel with opposite signs. Part 4 predicates the
    # inferred rung on "coverage gap remains" after the declared rung, and a document that
    # declares no structure at all is the total case of that: the gap is the whole
    # document and it is already known here, from the one pattern the sentinel names.
    # Waiting for structure:declared to say so would mean enqueuing a task whose only
    # possible finding is that it has nothing to read. The PARTIAL case — an outline that
    # names eight chapters and nothing below — is not decidable from the patterns and is
    # not handled by this pass; the declared handler measures the remaining gap and
    # records it.
    TaskRow(
        TASK_STRUCTURE_DECLARED,
        write_mode=WRITE_CHILDREN,
        after=(TASK_EXTRACT_TEXT,),
        requires=(HAS_TEXT_LAYER, DECLARED_STRUCTURE),
        forbids=(CONVERSATION,),
        params_key="structure",
    ),
    TaskRow(
        TASK_STRUCTURE_INFERRED,
        write_mode=WRITE_CHILDREN,
        after=(TASK_EXTRACT_TEXT,),
        requires=(HAS_TEXT_LAYER,),
        forbids=(DECLARED_STRUCTURE, CONVERSATION),
        params_key="structure",
    ),
    # SPRINT_JOBS.md 15.4 S7. The third alternative to the two rungs above, and the reason
    # both of them now forbid `@conversation`: a transcript's leaves are its turns, which
    # the file states, so splitting it on ATX headings or packing it into sentences would
    # throw away a boundary the document gave us and invent ones it did not.
    #
    # `after` `extract:text` like the two rungs, though it reads the BLOB rather than the
    # node's text — because `extract:text`'s conversation reader writes the readable
    # concatenation the file node is searched by, and a rung that raced it could write
    # children before the parent had any content at all. The two parse the same bytes with
    # the same parser and take different halves of the answer: the text, and the turns.
    #
    # No `params_key`. The turn boundaries are what the file says; there is nothing here
    # for 6.1 to re-run differently, which is the same claim `structure:sheets` makes.
    TaskRow(
        TASK_STRUCTURE_CONVERSATION,
        write_mode=WRITE_CHILDREN,
        after=(TASK_EXTRACT_TEXT,),
        requires=(HAS_TEXT_LAYER, CONVERSATION),
    ),
    # `INGEST_SPEC.md` 8.1. The third structural row, and the only one with no `after`:
    # its evidence is the workbook part, which is in the uploaded bytes, so it waits for
    # nothing. That is also why it is not the two rows above with an extra splitter —
    # those consume `extract:text`'s output, and a workbook has none.
    #
    # No `params_key`. 8.1's rung takes no parameters and that is a claim, not an
    # omission: the sheet list is what the file says, so there is nothing here for 6.1 to
    # re-run differently. The knobs Part 8 does have — which representation a sheet
    # becomes, and the thresholds that choose it — belong to `profile:sheet` (8.4), and
    # giving this row a params group would put them one rung too high, where a re-run
    # would rebuild every sheet node to change a decision about one sheet's cells.
    TaskRow(
        TASK_STRUCTURE_SHEETS,
        write_mode=WRITE_CHILDREN,
        requires=(SHEET_LIST,),
    ),
    # INGEST_SPEC.md 8.2's two per-sheet tasks, and the first two rows in this table that
    # are not scoped to the file node. `SPRINT_JOBS.md` Part 0 measured what their absence
    # cost, in four consequences that all close here: the sheet knobs become settable, the
    # `param_fingerprint` claim `sheet_tasks` makes about them becomes true, EXPLAIN stops
    # stopping at the sheet list, and `ingest_options`' validation reaches `max_rows`.
    #
    # `requires=(SHEET_LIST,)` is the same deliberate repetition the rows below
    # `extract:text` carry: the scope reaches it first and says so more precisely — there is
    # no sheet node unless `structure:sheets` ran — and naming the pattern keeps the row
    # readable as Part 8 writes it, without following the scope back up the table.
    #
    # `children`, where 8.2's table says `self`, and this is a deliberate divergence
    # inherited unchanged from `PROFILE_SHEET_SPEC`. A write mode is a concurrency
    # declaration the claim query acts on (5.3): a `self` task conflicts only with another
    # `self` on the same node. 8.5 makes the profile a CHILD of the sheet node, so this task
    # writes children, and declaring otherwise would tell the queue it is safe to run a
    # children-writer beside it — today only `extract:sheet`, which is ordered after this
    # one, so the lie would not yet cost anything. Which is exactly why it would still be
    # there when it did.
    TaskRow(
        TASK_PROFILE_SHEET,
        write_mode=WRITE_CHILDREN,
        scope=children_of(TASK_STRUCTURE_SHEETS, usetypes=(USETYPE_SHEET,)),
        requires=(SHEET_LIST,),
        params_key="sheet_profile",
    ),
    # 8.2's second per-sheet task: the cells themselves, one node per row. It runs 8.4's
    # `records` shape only, and the rule it branches on is not one of 8.8's thresholds —
    # see `jmfts_core.sheet_records.SHAPE_BASIS`.
    #
    # `after` and not a second scope: both rows are scoped to the SAME node, so this is
    # ordinary within-node ordering (5.5) and `enqueue_batch` resolves it to a real
    # `dependencies` id. It reads the header verdict and the column labels out of what
    # `profile:sheet` measured rather than measuring them a second time.
    TaskRow(
        TASK_EXTRACT_SHEET,
        write_mode=WRITE_CHILDREN,
        scope=children_of(TASK_STRUCTURE_SHEETS, usetypes=(USETYPE_SHEET,)),
        after=(TASK_PROFILE_SHEET,),
        requires=(SHEET_LIST,),
        params_key="sheet_records",
    ),
    # `pages_with_tables` is a LIST of page numbers, and this row reads it for TRUTHINESS
    # exactly as it read the `has_tables` boolean it replaced: an empty list blocks the
    # row, a non-empty one satisfies it. The list is carried rather than a flag because
    # the task this row schedules wants to be one task per page that has tables, and a
    # boolean threw away an answer the scan had already produced.
    #
    # PROBE DOES NOT REPORT THIS PATTERN. `extract:text` does, because the pass that
    # renders tables into the markdown is the only one that must find them anyway (see
    # `jmfts_core.probe._probe_pdf` for the scan that removed, and
    # `pdf_to_markdown`'s `pages_with_tables` for where it went). So this row is NOT
    # decidable at probe time, and at probe time `plan_after_probe` says exactly that —
    # "patterns.pages_with_tables was not measured" — rather than claiming the document
    # has no tables. It is the same shape as the lower structure rungs, which Part 4
    # predicates on a measurement the rung above produces: the row declares what the task
    # needs, and the task that learns the answer is the one that can act on it.
    #
    # The row stays here rather than moving out of the table with `structure:semantic`,
    # because this is where a task's requirement is DECLARED, and deleting the row would
    # leave `extract:tables` with its condition written nowhere. Passing a pattern set
    # that has the key — a caller's hypothesis to `explain_plan`, or a node's own
    # `extraction` record read back — decides the row normally.
    TaskRow(
        TASK_EXTRACT_TABLES,
        write_mode=WRITE_CHILDREN,
        after=(TASK_EXTRACT_TEXT,),
        requires=(HAS_TEXT_LAYER, PAGES_WITH_TABLES),
    ),
    TaskRow(TASK_EXTRACT_IMAGES, write_mode=WRITE_CHILDREN, requires=(HAS_IMAGES,)),
    # OFFICE_SPEC.md Part 5. Last in the table because it is the only row that runs after
    # the tree exists rather than in order to build it, and because `after_any` may only
    # name rows above.
    #
    # `subtree`, NOT the `children` Part 5's table writes, and this is the one place where
    # meeting the code contradicted the spec. `children` is "this node, plus nodes that have
    # no children of their own" — the node's OWN children. The chunks citation annotates are
    # its children only when a region had no title; under a titled section they are
    # grandchildren, and the same document routinely has both. `subtree` is the mode that
    # actually describes what this task writes, and declaring the narrower one would have
    # let citation run concurrently with the `embed` task on a chunk it is writing to.
    #
    # The cost is real and is worth stating: `subtree` reserves the whole file, so citation
    # cannot be claimed while any chunk below is embedding, and no chunk can embed while it
    # runs. It waits for the embeds to drain, then runs once over an in-memory block map
    # with no model and no network. That is the right side of the trade — the alternative is
    # a reservation that does not cover the writes it is for.
    TaskRow(
        TASK_CITATION,
        write_mode=WRITE_SUBTREE,
        after_any=(TASK_STRUCTURE_DECLARED, TASK_STRUCTURE_INFERRED),
        requires=(PAGE_GEOMETRY,),
    ),
    # OFFICE_SPEC.md Part 5's source anchor, Part 11 step 9. The row above and this one are
    # the same shape over disjoint formats — see `SOURCE_ADDRESS_PATTERN` for why the two
    # sentinels may not be merged — and `subtree` for the same reason, word for word: the
    # chunks it annotates are grandchildren under a titled section and children under an
    # untitled one, and the same deck routinely has both.
    #
    # NOT ordered against `citation`. Neither row can be eligible for a format the other is,
    # so an `after` naming it would order this row behind something that is never there.
    TaskRow(
        TASK_ANCHOR_SOURCE,
        write_mode=WRITE_SUBTREE,
        after_any=(TASK_STRUCTURE_DECLARED, TASK_STRUCTURE_INFERRED),
        requires=(SOURCE_ADDRESS,),
    ),
    # INGEST_SPEC.md 11.5's `index:bm25` WAS A ROW HERE AND IS NOT ONE ANY MORE
    # (`SPRINT_0_6_0.md` Block B step 7). It read:
    #
    #     TaskRow(TASK_INDEX_BM25, write_mode=WRITE_SELF,
    #             after_any=(TASK_STRUCTURE_DECLARED, TASK_STRUCTURE_INFERRED,
    #                        TASK_STRUCTURE_CONVERSATION),
    #             requires=(HAS_TEXT_LAYER,))
    #
    # and its comment claimed `after_any` for `citation`'s reason — "exactly one of the two
    # rungs is ever eligible for a document". **That sentence is true of the prose formats
    # it was written about and false for a workbook**, whose rung is `structure:sheets`; and
    # `requires=(HAS_TEXT_LAYER,)` is a PDF property no workbook can satisfy. Either
    # exclusion alone kept the task from ever being created, and `STRESS_CORPUS.md` 4.4
    # measured the result on a real corpus: zero `index:bm25` tasks anywhere in a
    # 21-workbook subtree, an `xlsx` index holding nothing, 4,601 `record` nodes with text.
    #
    # THE THIRD CAUSE IS WHY THE ROW IS GONE RATHER THAN WIDENED. A workbook's leaves are
    # not written by its structure rung and every other format's are: `structure:sheets`
    # writes the `sheet` containers and `extract:sheet` writes the rows, one rung further
    # down and scoped to a different node. `_check_task_rows` rule 2 refuses to order two
    # rows on different nodes and rule 1 refuses a second row under the same name, so this
    # table cannot state the dependency at all — 4.4c repaired the two named exclusions and
    # measured `index:bm25 added 0 documents ... it saw a subtree of 2`.
    # `docs/MEASURE_BM25_BOUNDARY.md` §5.2 moved it to the settling boundary as a set
    # difference; `jmfts_core.index_tasks` is where that argument now lives, and
    # `IngestRollupPlanner` is what offers it.
    #
    # INGEST_SPEC.md 11.4, SPRINT_JOBS.md 15.4 S6. `after_any` on the three rungs, like
    # `citation` above: it reads the leaves' `content`, which a completed rung has written.
    #
    # `self`, and this is the one row where that is a statement about somewhere else. It
    # writes no node in this file's subtree — the triples are rows in `triples`, and the
    # entity nodes `resolve_entity` creates live under the entities root, which is a region
    # no write mode of this file's can reserve. 5.3's modes describe a reservation over ONE
    # subtree, and a task that writes into a shared region outside it is a gap in that
    # model rather than something `subtree` would close.
    #
    # THE TWO GUARDS THAT ARE THE CALLER'S, and the only row that has either. Both ask what
    # the request wanted rather than what the bytes are, which is the distinction 4.4 draws:
    # whether to spend an LLM call per chunk is a choice, and no property of a document
    # decides it. Neither is a way to turn an arbitrary row off — the structure rungs are
    # not optional, and giving one of them a caller's switch would let an upload be accepted
    # and nothing built from it.
    #
    # `options.facts.enabled` was the `enabled_by` field until Phase 4, and it is a term now
    # because there was never a second thing that field could express. It is evaluated
    # AHEAD of the dependency gate — see `Term.reads_option` — so a caller who turned fact
    # extraction off is told that, and not "the structure rung it comes after did not run".
    #
    # `char_count` is a `forbids` and NOT a `requires`, and the measurement is the argument.
    # Probe emits `char_count` for `text` alone; required, it would stop fact extraction on
    # every PDF, .docx and .pptx, which is 4.4's `.docx` regression in a second costume.
    # Excluded, it says the thing a caller means by a minimum: do not spend the call on a
    # document measured as too small, and do spend it where nobody measured a size.
    #
    # The threshold is an option reference rather than a literal because that is what makes
    # 14.2's promise true — a number in this table moves only by a code change, and this one
    # moves through `resolve_options`' three layers.
    TaskRow(
        TASK_EXTRACT_FACTS,
        write_mode=WRITE_SELF,
        after_any=(
            TASK_STRUCTURE_DECLARED,
            TASK_STRUCTURE_INFERRED,
            TASK_STRUCTURE_CONVERSATION,
        ),
        requires=(term(option("facts", "enabled")), HAS_TEXT_LAYER),
        forbids=(term(CHAR_COUNT, OP_LT, option("facts", "min_characters")),),
        params_key="facts",
    ),
    # The vectors, on every leaf five different rules write. LAST in the table because a
    # child scope may only name producers above it, and this one names five.
    #
    # ONE ROW AND NOT FIVE. `Scope`'s docstring argues it: five rows called `embed`,
    # differing in one field, is the drift `DECLARED_STRUCTURE` refused for thirteen
    # formats. The scope's two halves are both disjunctions, and the cross-product
    # over-accepts pairs that never occur — `profile:sheet` writes no chunk, the rungs write
    # no record — which costs nothing, because a row only ever fires for a child that
    # actually exists.
    #
    # THE USETYPES ARE THE POINT OF THE SECOND HALF. `structure:declared` writes `section`
    # containers as well as `chunk` leaves, and a section holds no text of its own: its
    # `effective_content` comes from the rollup, over children that have already embedded.
    # An `embed` on a section would raise, because `run_embed` refuses empty content — which
    # is the correct behaviour of that handler and the wrong behaviour of this schedule.
    #
    # `table` is the fifth, and it is the one leaf here whose text routinely does NOT fit the
    # token/maxsim window: 8.4's `small_table` is tested against the DOCUMENT window, and
    # 52.0% of open-web sheets fit 8192 and not 512. The row still names one `with_tokens`,
    # because the group is the caller's request; `sheet_tasks._write_table` sets it false on
    # the queue row for a table the token path cannot take, from the measurement that decided
    # the shape. See that function for why the decision belongs to the node and not to the
    # row.
    #
    # No condition beyond the scope. "Every leaf gets one" is what an empty `requires` says.
    TaskRow(
        TASK_EMBED,
        write_mode=WRITE_SELF,
        scope=children_of(
            TASK_STRUCTURE_DECLARED,
            TASK_STRUCTURE_INFERRED,
            TASK_STRUCTURE_CONVERSATION,
            TASK_PROFILE_SHEET,
            TASK_EXTRACT_SHEET,
            usetypes=(
                USETYPE_CHUNK,
                USETYPE_RECORD,
                USETYPE_PROFILE,
                USETYPE_CELL,
                USETYPE_TABLE,
            ),
        ),
        params_key="embed",
    ),
    # OFFICE_SPEC.md Part 6 and Part 1 — the two tier-3 rows (SPRINT_0_6_0.md Block G step
    # 32). Both are scoped to the file node, both read only the uploaded bytes, and NEITHER
    # is ordered against anything in this table: they consume `matched` and `blob`, which
    # probe and the upload have already written when this table is evaluated.
    #
    # LAST, and the placement is the only thing about them that is a choice rather than a
    # fact. Order here is enqueue order within one batch and nothing else — with no `after`
    # and no child scope, a row placed anywhere plans identically — so they go at the end,
    # where an append cannot collide with a lane editing a row in the middle of the table.
    #
    # `render:pdf` is ADVISORY (models/task_queue.ADVISORY_TASK_TYPES). That is what makes
    # an unconditional row safe on an appliance with no LibreOffice: the task fails
    # PERMANENTLY with a message naming JMFTS_CONVERT_URL and the image, and the document
    # settles anyway with its text, its chunks and its vectors. Part 6: "Rendering degrades;
    # retrieval does not."
    #
    # UNCONDITIONAL, with no option to switch it off, and that is Block G answering
    # OFFICE_SPEC.md Part 12 question 1 rather than carrying it. That question proposed an
    # option because a rendition "roughly doubles blob storage for office documents"; the
    # measurement says the denominator was wrong — original bytes are 144 MB of a 2988 MB
    # reference database, 4.8%, so the upper bound is +4.8% — and a rendition is DERIVED,
    # which makes the policy cache eviction rather than retention. An option here would also
    # have been the wrong mechanism for a different reason: a row's condition is a pure
    # function of (format, patterns, options) so that EXPLAIN can answer without bytes, and
    # "is there a converter configured" is a fact about the DEPLOYMENT, which does not belong
    # in either.
    TaskRow(TASK_RENDER_PDF, write_mode=WRITE_SELF, requires=(RENDITION_SOURCE,)),
    # NOT advisory, and the asymmetry with the row above is the rule applied twice. A .docx
    # with no rendition is a complete searchable document missing a picture; a .doc with no
    # conversion has no reader that can open it, so it has no text and no children — which is
    # `extract:text`'s case, and `settled='failed'` is the true thing to say about that node.
    #
    # A bare pattern rather than a sentinel: `is_legacy_binary` is measured for `ole2` and
    # for nothing else, so the condition is already per-format without a dict to say so.
    TaskRow(TASK_CONVERT_OOXML, write_mode=WRITE_SELF, requires=(IS_LEGACY_BINARY,)),
)


def _check_task_rows() -> None:
    """The table is internally consistent. Checked at import, because it decides schedules.

    Five invariants, and each one is a schedule that would come out wrong rather than a
    tidiness rule. The fifth is :func:`_check_term`, which carries its own reasons:

    1. **A task name appears once.** Outcomes are reported per task —
       :attr:`DownstreamPlan.not_applicable`, the attempt detail, ``EXPLAIN``'s rows — so
       two rows under one name would silently report one of them.
    2. **``after`` and ``after_any`` name rows ABOVE, in the SAME scope.** Ordering within a
       node is resolved by ``enqueue_batch``, which can only point at an id it already has
       (5.5). A name from another scope is a node that does not exist.
    3. **A child scope's producers are rows ABOVE.** The scope is what orders the row after
       the rule that creates its node, so a forward reference would be a plan whose
       eligibility depends on a row not yet decided.
    4. **A ``params_key`` names a declared group.** A row naming a group nobody declared
       would be planned with no parameters at all, and the handler would then supply its
       own numbers while the attempt log recorded the empty dict as what was asked for.
    """
    seen: dict[str, Scope] = {}
    groups = set(TASK_PARAM_DEFAULTS)
    for row in TASK_ROWS:
        if row.task in seen:
            raise ValueError(
                f"task {row.task!r} has two rows in TASK_ROWS; every outcome this table "
                "reports is keyed by task name, so one of the two would be invisible"
            )
        for name in row.after + row.after_any:
            if name not in seen:
                raise ValueError(
                    f"row {row.task!r} is ordered after {name!r}, which is not above it in "
                    "TASK_ROWS; within-node ordering can only name a task whose id the "
                    "batch already has"
                )
            if seen[name] != row.scope:
                raise ValueError(
                    f"row {row.task!r} at scope {row.scope} is ordered after {name!r} at "
                    f"scope {seen[name]}; `after` orders two tasks on ONE node, and these "
                    "are on different ones"
                )
        for producer in row.scope.produced_by:
            if producer not in seen:
                raise ValueError(
                    f"row {row.task!r} is scoped to the children of {producer!r}, which is "
                    "not above it in TASK_ROWS; a scope decides eligibility from the "
                    "producing row's, which has to be decided first"
                )
        if row.params_key is not None and row.params_key not in groups:
            raise ValueError(
                f"row {row.task!r} names option group {row.params_key!r}, which is not in "
                f"TASK_PARAM_DEFAULTS; the declared groups are {sorted(groups)}"
            )
        for t in row.requires + row.forbids:
            _check_term(row, t)
        seen[row.task] = row.scope


def _declared_type(row: TaskRow, t: Term) -> str:
    """What a term's left side holds, and the check that it is a name at all.

    A sentinel's targets must agree on ONE type, because a comparison is checked against
    the type here and a sentinel resolves per format — a sentinel whose ``docx`` entry were
    a flag and whose ``pdf`` entry were a count would type-check against whichever the
    audit happened to look at, and fail on documents of the other format.
    """
    if isinstance(t.left, Option):
        group = TASK_PARAM_DEFAULTS.get(t.left.group)
        if group is None or t.left.name not in group:
            raise ValueError(
                f"row {row.task!r} guards on {t.left}, which is not a declared option; "
                f"a guard's option reference is resolved by `resolve_options` and a name "
                f"nothing declares would resolve to nothing"
            )
        return _TYPE_OF.get(type(group[t.left.name]), TYPE_STR)

    targets = SENTINEL_PATTERNS.get(t.left)
    names = sorted(set(targets.values())) if targets is not None else [t.left]
    unknown = [n for n in names if n not in GUARDABLE_PATTERNS]
    if unknown:
        raise ValueError(
            f"row {row.task!r} guards on {unknown}, which GUARDABLE_PATTERNS does not "
            f"name; `matched.patterns` is an open namespace, so an unlisted name would "
            f"read as a pattern nobody measures and stand the row down forever"
        )
    types = sorted({GUARDABLE_PATTERNS[n] for n in names})
    if len(types) > 1:
        raise ValueError(
            f"row {row.task!r} guards on sentinel {t.left!r}, whose patterns are typed "
            f"{types}; a sentinel resolves per format and a comparison is checked once, so "
            f"its patterns have to agree on one type"
        )
    return types[0]


def _check_term(row: TaskRow, t: Term) -> None:
    """One guard term is a name this table may read, compared with something comparable.

    ``SPRINT_JOBS.md`` Part 10's check 6, arriving in Phase 4 because the audit that holds
    it already exists. Both halves are schedules that would come out wrong rather than
    tidiness rules: an unlisted name is a row that never fires and says so about a pattern
    nobody measures, and a mistyped comparison is a ``TypeError`` raised inside planning on
    the first real document of that format.
    """
    left_type = _declared_type(row, t)
    if t.op is None:
        return
    if t.op not in GUARD_OPS:
        raise ValueError(
            f"row {row.task!r} guards with operator {t.op!r}; the operators are "
            f"{sorted(GUARD_OPS)}"
        )
    if isinstance(t.right, Option):
        right_type = _declared_type(row, Term(t.right))
    else:
        right_type = _TYPE_OF.get(type(t.right))
    if right_type != left_type:
        raise ValueError(
            f"row {row.task!r} compares {t.left} ({left_type}) with {t.right!r} "
            f"({right_type}); a guard's two sides have to be the same kind of thing"
        )
    if t.op in ORDERING_OPS and left_type not in (TYPE_INT, TYPE_FLOAT):
        raise ValueError(
            f"row {row.task!r} orders {t.left} with {t.op!r}, and it holds a {left_type}; "
            f"{list(ORDERING_OPS)} are for counts"
        )


_check_task_rows()


def _resolve_pattern(name: str, fmt: str) -> Optional[str]:
    """The pattern a row's condition actually tests, for this format.

    Plain names pass through unchanged, so ``None`` can only ever mean "the sentinel has
    no pattern for this format" — which is what lets the caller phrase that case as the
    different thing it is, rather than as a pattern that happened to be false.
    """
    per_format = SENTINEL_PATTERNS.get(name)
    if per_format is None:
        return name
    return per_format.get(fmt)


def _dependency_reason(task: str, dependency: str) -> str:
    """Why a row whose predecessor did not come out eligible is not applicable either.

    Spelled once because :func:`explain_plan` reports the same fact for the same rows and
    a second wording would read as a second, different finding.
    """
    return f"{dependency} is not eligible, and {task} depends on it"


def _alternative_dependency_reason(task: str, alternatives: tuple[str, ...]) -> str:
    """Why a row whose ``after_any`` set came out empty is not applicable either.

    A different sentence from :func:`_dependency_reason` because it is a different fact:
    not "this one predecessor did not fire" but "none of the alternatives did", and naming
    only one of them would read as though the others had been ignored.
    """
    return f"none of {list(alternatives)} is eligible, and {task} runs after whichever does"


def _no_declared_structure_reason(fmt: str) -> str:
    """Why a row requiring :data:`DECLARED_STRUCTURE` can never fire for this format.

    Not "a pattern that was false" — there is no pattern. :data:`DECLARED_STRUCTURE_PATTERN`
    has no entry for the format, so no bytes of it could ever satisfy the requirement, which
    is why :func:`explain_plan` reports the row as ``impossible`` and reuses this exact
    sentence for the reason.
    """
    return f"format {fmt!r} declares no structure pattern this spec knows about"


def _no_page_geometry_reason(fmt: str) -> str:
    """Why a row requiring :data:`PAGE_GEOMETRY` can never fire for this format.

    The same fact as above about a different sentinel: :data:`PAGE_GEOMETRY_PATTERN` has no
    entry, so no bytes of this format can report a page a rectangle could sit on.
    """
    return f"format {fmt!r} carries no page geometry a citation rectangle could address"


def _no_source_address_reason(fmt: str) -> str:
    """Why a row requiring :data:`SOURCE_ADDRESS` can never fire for this format.

    :data:`SOURCE_ADDRESS_PATTERN` has no entry, so nothing in this format's bytes names a
    part and an element an anchor could point into. For ``pdf`` that is not a gap: its
    source anchor is a rectangle and ``citation`` writes it, because for a source PDF Part
    5's two anchors are the same object.
    """
    return f"format {fmt!r} names no package element a source anchor could address"


def _no_conversation_reason(fmt: str) -> str:
    """Why a row requiring :data:`CONVERSATION` can never fire for this format.

    A fourth sentinel with the same shape: :data:`CONVERSATION_PATTERN` has no entry, so
    no bytes of this format could be read as a sequence of turns by this appliance.
    """
    return f"format {fmt!r} carries no conversation transcript this spec knows how to read"


def _no_sheet_list_reason(fmt: str) -> str:
    """Why a row requiring :data:`SHEET_LIST` can never fire for this format.

    The same fact as above about a third sentinel: :data:`SHEET_LIST_PATTERN` has no entry,
    so no bytes of this format could name the worksheets ``INGEST_SPEC.md`` 8.1 builds its
    declared rung out of.
    """
    return f"format {fmt!r} names no worksheet list a declared rung could read"


def _no_rendition_source_reason(fmt: str) -> str:
    """Why a row requiring :data:`RENDITION_SOURCE` can never fire for this format.

    A fifth sentinel with the same shape and one reader it is worth writing for: ``pdf``.
    The sentence has to say that a PDF gets no rendition BECAUSE it is already one, rather
    than leaving an operator to conclude that renditions are broken for the format the
    feature is mostly used on.
    """
    if fmt == "pdf":
        return (
            "format 'pdf' already has pages, so a rendition of it would be a second copy of "
            "the upload; `pdf_for` serves a PDF from its own stored bytes"
        )
    return f"format {fmt!r} holds no document this appliance knows how to paginate"


#: sentinel -> the sentence explaining why a row requiring it is impossible for a format.
#: Beside :data:`SENTINEL_PATTERNS` and keyed the same way, so a sentinel cannot be added to
#: one without the other: a requirement that no format can satisfy and no sentence to say
#: why is a row that vanishes from every plan with an empty reason.
SENTINEL_REASONS: dict[str, Callable[[str], str]] = {
    DECLARED_STRUCTURE: _no_declared_structure_reason,
    PAGE_GEOMETRY: _no_page_geometry_reason,
    SOURCE_ADDRESS: _no_source_address_reason,
    SHEET_LIST: _no_sheet_list_reason,
    CONVERSATION: _no_conversation_reason,
    RENDITION_SOURCE: _no_rendition_source_reason,
}


def _unsatisfiable_sentinel(row: TaskRow, fmt: str) -> Optional[str]:
    """The sentinel in ``row.requires`` that this format has no pattern for, if any.

    Only ``requires`` can make a row impossible: a sentinel in ``forbids`` with no entry
    prohibits nothing and is always satisfied (see :data:`DECLARED_STRUCTURE`).
    """
    for t in row.requires:
        if isinstance(t.left, str) and _resolve_pattern(t.left, fmt) is None:
            return t.left
    return None


#: The left side is a sentinel this format has no pattern for. Only ``requires`` can be
#: made impossible by it.
_IMPOSSIBLE = "impossible"
#: Nobody measured the left side. The two fields read this state in opposite directions,
#: which is 4.4's table and the reason they are two fields.
_ABSENT = "absent"
_PRESENT = "present"


def _left_side(t: Term, fmt: str, patterns: dict, resolved: dict) -> tuple[str, str, object]:
    """A term's left side as ``(state, label, value)``.

    The label is what the not-applicable sentence names, and it carries the namespace —
    ``patterns.has_outline``, ``options.facts.enabled`` — because a reader of a plan has no
    other way to tell a measurement from a request.
    """
    if isinstance(t.left, Option):
        return _PRESENT, str(t.left), resolved[t.left.group][t.left.name]
    pattern = _resolve_pattern(t.left, fmt)
    if pattern is None:
        return _IMPOSSIBLE, t.left, None
    if pattern not in patterns:
        return _ABSENT, f"patterns.{pattern}", None
    return _PRESENT, f"patterns.{pattern}", patterns[pattern]


def _right_side(t: Term, resolved: dict) -> tuple[object, str]:
    """A term's right side as ``(value, label)``.

    An option reference reports the NAME and the value it resolved to, because a plan that
    said only ``5000`` would not tell a caller which knob to turn, and one that said only
    the knob would not tell them what it is currently set to.
    """
    if isinstance(t.right, Option):
        value = resolved[t.right.group][t.right.name]
        return value, f"{t.right} ({value!r})"
    return t.right, repr(t.right)


def _holds(t: Term, value: object, resolved: dict) -> Optional[bool]:
    """Whether the term holds for ``value``, or ``None`` if it cannot be evaluated.

    ``None`` is a THIRD answer and not a false one. It means the two sides are not the same
    kind of thing — a caller's hypothetical ``matched.patterns`` reaching ``EXPLAIN`` with
    ``char_count`` set to a string, say — and the callers block the row on it in BOTH
    fields. That is deliberately not the ``forbids`` unknown policy: absent means nobody
    looked, which is not evidence of a blocker, while a value that cannot be compared means
    somebody looked and wrote something unusable, and reading that as "no blocker" would be
    the swallowed error the two policies exist to prevent.
    """
    if t.op is None:
        return bool(value)
    right, _label = _right_side(t, resolved)
    if _TYPE_OF.get(type(value)) != _TYPE_OF.get(type(right)):
        return None
    return GUARD_OPS[t.op](value, right)


def _uncomparable_reason(row: TaskRow, label: str, value: object, right_label: str) -> str:
    """Why a row cannot be decided: its guard's two sides are different kinds of thing."""
    return (
        f"{label} is {value!r}, which cannot be compared with {right_label}, so "
        f"{row.task}'s condition has no answer"
    )


def _requires_reason(
    t: Term, row: TaskRow, fmt: str, patterns: dict, resolved: dict
) -> Optional[str]:
    """Why a ``requires`` term does not hold, or ``None`` if it does."""
    state, label, value = _left_side(t, fmt, patterns, resolved)
    if state is _IMPOSSIBLE:
        return SENTINEL_REASONS[t.left](fmt)
    # ABSENT AND FALSE ARE DIFFERENT ANSWERS, and collapsing them into one sentence was
    # survivable only while every pattern a row named was one probe always reported.
    # `pages_with_tables` is not: `extract:text` writes it, not probe (see
    # PATTERNS_NOT_PROBED), so at probe time the key is missing, and "no tables were found
    # in this document" would be a confident wrong answer to "nobody has looked yet". 11.2
    # draws the same line for `probe_failed`: a plan built on a pattern set that could not
    # be measured must say so rather than read as a measurement that came back empty.
    if state is _ABSENT:
        return f"{label} was not measured"
    held = _holds(t, value, resolved)
    if held is None:
        return _uncomparable_reason(row, label, value, _right_side(t, resolved)[1])
    if held:
        return None
    if t.op is None:
        if t.reads_option:
            return f"{label} is false, and {row.task} runs only when it is true"
        return f"{label} is false"
    return (
        f"{label} is {value!r}, and {row.task} runs only when it "
        f"{OP_PHRASES[t.op]} {_right_side(t, resolved)[1]}"
    )


def _forbids_reason(
    t: Term, row: TaskRow, fmt: str, patterns: dict, resolved: dict
) -> Optional[str]:
    """Why a ``forbids`` term blocks the row, or ``None`` if it does not."""
    state, label, value = _left_side(t, fmt, patterns, resolved)
    # A sentinel with no pattern for this format prohibits nothing: there is no declared
    # structure to be in the way. An unmeasured name prohibits nothing either, and that is
    # the other half of 4.4's table — no evidence of a blocker is not a blocker.
    if state is not _PRESENT:
        return None
    held = _holds(t, value, resolved)
    if held is None:
        return _uncomparable_reason(row, label, value, _right_side(t, resolved)[1])
    if not held:
        return None
    if t.op is None:
        return f"{label} is true, and {row.task} runs only when it is false"
    return (
        f"{label} is {value!r}, and {row.task} does not run when it "
        f"{OP_PHRASES[t.op]} {_right_side(t, resolved)[1]}"
    )


def _request_reason(row: TaskRow, fmt: str, patterns: dict, resolved: dict) -> Optional[str]:
    """Why a term about the REQUEST blocks the row, or ``None``.

    Checked before everything else, and ahead of the dependency gate, because these are the
    conditions that are true of the request rather than of the file. A caller who turned
    fact extraction off is owed that answer, not "the structure rung it comes after did not
    run" — which is also true and is not why. That ordering was ``_disabled_reason``'s
    before ``enabled_by`` became an ordinary term, and it survives the fold because the
    ordering was never about the field: it is about which kind of question a term asks.
    """
    for t in row.requires:
        if t.reads_option:
            reason = _requires_reason(t, row, fmt, patterns, resolved)
            if reason is not None:
                return reason
    for t in row.forbids:
        if t.reads_option:
            reason = _forbids_reason(t, row, fmt, patterns, resolved)
            if reason is not None:
                return reason
    return None


def _no_scope_reason(row: TaskRow) -> str:
    """Why a child-scoped row does not fire: nothing will create the node it runs on.

    A different sentence from :func:`_alternative_dependency_reason` because it is a
    different fact. That one says a task has no predecessor to be ordered after; this says
    the task has no NODE — 4.1's second form is not a queue dependency, it is the statement
    that the rule's scope resolves to nothing.
    """
    rules = list(row.scope.produced_by)
    return (
        f"none of {rules} is eligible, so no node of usetype "
        f"{list(row.scope.usetypes)} is created for {row.task} to run on"
    )


def _blocking_reason(
    row: TaskRow,
    fmt: str,
    patterns: dict,
    eligible: set[str],
    eligible_anywhere: set[str],
    resolved: dict,
) -> Optional[str]:
    """Why ``row``'s condition does not hold, or ``None`` if it does.

    ``eligible`` is the set of task names already eligible IN THIS ROW'S SCOPE, which is
    what ``after`` and ``after_any`` are answered from — an ordering is between two tasks on
    one node. ``eligible_anywhere`` is every eligible row, which is what a child scope is
    answered from, because the producing rule runs somewhere else by construction.

    Scope is checked before dependencies and dependencies before patterns, and both orders
    are deliberate. A row whose node will never exist has one honest reason and it is not
    "a pattern was false"; a row that comes after a row that is not eligible likewise —
    ``structure:declared`` on a file with no text layer is not applicable because there will
    be no text, not because of anything about its outline.
    """
    requested = _request_reason(row, fmt, patterns, resolved)
    if requested is not None:
        return requested

    if row.scope.kind == SCOPE_CHILDREN and not any(
        name in eligible_anywhere for name in row.scope.produced_by
    ):
        return _no_scope_reason(row)

    for dependency in row.after:
        if dependency not in eligible:
            return _dependency_reason(row.task, dependency)

    if row.after_any and not any(name in eligible for name in row.after_any):
        return _alternative_dependency_reason(row.task, row.after_any)

    # The terms about the FILE. The ones about the request were answered at the top, so
    # both loops skip them here rather than reporting them a second time in a worse place.
    for t in row.requires:
        if t.reads_option:
            continue
        reason = _requires_reason(t, row, fmt, patterns, resolved)
        if reason is not None:
            return reason

    for t in row.forbids:
        if t.reads_option:
            continue
        reason = _forbids_reason(t, row, fmt, patterns, resolved)
        if reason is not None:
            return reason

    return None


@dataclass(frozen=True)
class DownstreamPlan:
    """What Part 4's table decides for ONE SCOPE, given a node's ``matched.patterns``.

    Three lists rather than one, because they are three different facts and 3.4 insists
    they stay apart: work to do, work deliberately not done, and work whose condition was
    false.

    A plan describes its own scope and nothing else. The rows of the sheet tier are decided
    by the same evaluation that decides the file's — they have to be, or a child scope's
    eligibility could not be read off its producer's — but they belong to a different node,
    so reporting them in the file node's plan would put work in a batch that cannot hold it.
    ``EXPLAIN`` is where every scope appears at once, because a forecast is about the format
    rather than about one node.
    """

    #: Conditions that hold. Ordered; ``after`` names refer to earlier entries.
    eligible: tuple[TaskSpec, ...] = ()
    #: Conditions that hold but which the spec says to record as skipped rather than run.
    skipped: tuple[SkippedTask, ...] = ()
    #: task -> the condition that was false.
    not_applicable: dict[str, str] = field(default_factory=dict)
    #: Which nodes this plan is for. ``SPRINT_JOBS.md`` 4.1.
    scope: Scope = ROOT_SCOPE


@dataclass(frozen=True)
class _Evaluated:
    """Every row of the table decided, across every scope. One evaluation, many plans.

    Not public, and the reason is 11.2's: a caller who wanted "the file node's batch" and
    got every scope's rows would enqueue work onto a node that cannot hold it. What reads
    this is :func:`plan_after_probe`, which filters to one scope, and
    :func:`_explain_concrete`, which does not filter because a forecast is about the format.
    """

    resolved: dict
    #: Eligible rows, in table order, with the spec each would be enqueued as.
    eligible: tuple[tuple[TaskRow, TaskSpec], ...]
    skipped: tuple[tuple[TaskRow, SkippedTask], ...]
    #: task -> the condition that was false, over every scope.
    not_applicable: dict[str, str]


def _evaluate(fmt: str, patterns: dict, options: Optional[dict]) -> _Evaluated:
    """One pass over :data:`TASK_ROWS`, deciding every row at every scope.

    Scopes are decided together and cannot be decided apart: a row scoped to the children of
    ``structure:sheets`` is eligible only if that row is, so the two evaluations would have
    to agree — and two evaluators of one table are free to disagree, which is the failure
    :func:`_explain_concrete` already refuses to risk for ``EXPLAIN`` against the run.

    Ordering within a scope is tracked per scope, because ``after`` orders two tasks on ONE
    node. Ordering across scopes is the scope itself, and the table's order is what makes it
    resolvable in a single pass: a child scope may only name producers above it
    (:func:`_check_task_rows`).
    """
    resolved = resolve_options(fmt, options)
    eligible: list[tuple[TaskRow, TaskSpec]] = []
    skipped: list[tuple[TaskRow, SkippedTask]] = []
    not_applicable: dict[str, str] = {}
    # Names of rows that came out eligible, per scope and overall. A skipped row is in
    # NEITHER: nothing may be ordered after work that will never be queued.
    eligible_in_scope: dict[Scope, set[str]] = {}
    eligible_anywhere: set[str] = set()

    for row in TASK_ROWS:
        in_scope = eligible_in_scope.setdefault(row.scope, set())
        reason = _blocking_reason(row, fmt, patterns, in_scope, eligible_anywhere, resolved)
        if reason is not None:
            not_applicable[row.task] = reason
            continue
        if row.skip_reason is not None:
            skipped.append((row, SkippedTask(row.task, row.skip_reason)))
            continue
        eligible.append(
            (
                row,
                TaskSpec(
                    task_type=row.task,
                    write_mode=row.write_mode,
                    # Copied, not shared: `resolved` is about to be read again by the next
                    # row naming the same group, and this dict is going onto a queue row.
                    params=dict(resolved[row.params_key]) if row.params_key else {},
                    # `after_any` collapses to the alternatives that actually came out
                    # eligible. In table order, because `enqueue_batch` resolves a name to
                    # the id of a spec EARLIER in the same batch and a set is unordered.
                    after=row.after + tuple(n for n in row.after_any if n in in_scope),
                ),
            )
        )
        in_scope.add(row.task)
        eligible_anywhere.add(row.task)

    return _Evaluated(
        resolved=resolved,
        eligible=tuple(eligible),
        skipped=tuple(skipped),
        not_applicable=not_applicable,
    )


def plan_after_probe(
    fmt: str,
    patterns: dict,
    options: Optional[dict] = None,
    *,
    scope: Scope = ROOT_SCOPE,
) -> DownstreamPlan:
    """Evaluate spec Part 4's enqueue conditions from what ``probe`` measured.

    ``scope`` defaults to the file node, which is where every row of Part 4's table sat
    before a row could name a scope — so the default is the caller ``run_probe`` has always
    been, spelled out. Naming another scope asks the same question of another node's batch:
    :func:`plan_frontier` is what does that, and it is the only caller that passes one.

    Only the rows Part 4 predicates on ``probe``'s output are decidable here. The lower
    structure rungs (``structure:semantic`` / ``flat``) are predicated on "coverage gap
    remains", which is a measurement the rung above produces. ``describe:images`` and
    ``embed:images`` follow ``extract:images`` for the same reason. ``summarize`` belongs to
    rollup (5.4) and is the settling walk's business.

    One pass over :data:`TASK_ROWS`, in order, with no branch per task: a row is either
    eligible, recorded as skipped, or recorded with the condition that was false. Adding a
    task is then a row, not a branch and a hand-typed string — which is what makes Part 4's
    tasks and Part 8's two per worksheet a table rather than ninety more lines, and what
    keeps 11.2's ``EXPLAIN`` honest: the plan is derived from the same declaration the run
    is. Part 8's two ARE in the table now; the sentence above used to promise that and the
    code did not do it.

    ``options`` are OVERRIDES, not a finished set: they are resolved here against the
    format's profile and the tasks' own defaults, so the params that reach a queue row are
    always complete and always validated. Every group resolves for every format — the
    defaults belong to the task that reads them — so a row that names a ``params_key``
    always has parameters, and there is no case here to handle. Omitting the argument means
    those defaults, which is the documented meaning of "no overrides" rather than a
    stand-in for something missing. Resolving is idempotent on a complete set, which is what
    lets ``run_probe`` hand back the options the upload recorded on the node and get the
    same plan the upload was promised. A bad override raises rather than being ignored (see
    :func:`~jmfts_core.ingest_options.resolve_options`); by the time ``probe`` runs, the
    upload has already validated them, so a raise here means the node's stored options were
    tampered with rather than that a caller mistyped.
    """
    decided = _evaluate(fmt, patterns, options)
    return DownstreamPlan(
        eligible=tuple(spec for row, spec in decided.eligible if row.scope == scope),
        skipped=tuple(entry for row, entry in decided.skipped if row.scope == scope),
        not_applicable={
            row.task: decided.not_applicable[row.task]
            for row in TASK_ROWS
            if row.scope == scope and row.task in decided.not_applicable
        },
        scope=scope,
    )


# ---------------------------------------------------------------------------
# The frontier — SPRINT_JOBS.md 6.2 and 6.4
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Frontier:
    """The batch a newly created child gets, planned once for a whole fan-out.

    ``SPRINT_JOBS.md`` 6.4, which corrects a claim an earlier draft of that document made
    twice — that the planner replaces the handler-side enqueue entirely. It cannot.
    ``settle_walk`` advances to ``step.parent_id`` and never descends, so a node created
    settled and given no work is never visited, never gets an embedding, and nothing ever
    notices. A rule that creates a child must therefore enqueue that child's work at the
    moment it creates it.

    **What Phase 3 removed is the literal list, not the call.** The five handlers that
    carried a hand-written ``TaskSpec`` tuple now ask :func:`plan_frontier` for one, and the
    rows it reads are declared in :data:`TASK_ROWS` beside every other scheduling decision.

    Planned ONCE per fan-out and not per child: the answer is a function of
    ``(format, patterns, options, scope)`` and every child of one run shares all four. A
    forty-sheet workbook is one evaluation and forty enqueues.
    """

    #: The rule that will stamp the children this frontier is for.
    produced_by: str
    #: The usetype those children carry.
    usetype: str
    #: What to enqueue on each child, in dependency order.
    specs: tuple[TaskSpec, ...] = ()
    #: task -> why it is planned and not queued (no handler registered, or ordered after
    #: one). Carried so the producing handler can put it in its attempt detail: 3.4 wants a
    #: rung that stopped where the spec says to stop to be distinguishable from one that
    #: never ran.
    deferred: dict[str, str] = field(default_factory=dict)
    #: task -> the condition that was false, for rows in this scope.
    not_applicable: dict[str, str] = field(default_factory=dict)

    @property
    def in_flight(self) -> bool:
        """Whether a child gets work, and therefore whether it is born ``in_flight``.

        A node created ``settled`` with work queued on it is a false claim its ancestors
        roll up over; a node created ``in_flight`` with no work parks the tree forever
        because nothing will ever settle it. Neither is a constant — it is whether this
        frontier is empty — and reading it off the plan is what keeps the two in step when
        a handler is added or deferred.
        """
        return bool(self.specs)


def plan_frontier(session: Session, node: Document, *, produced_by: str, usetype: str) -> Frontier:
    """The rows scoped to the children ``produced_by`` is about to write under ``node``.

    ``node`` is the node the fan-out is happening under — the handler's own scope node — and
    it is not necessarily where the plan's inputs live. ``matched`` and ``options`` are
    written on the file node by ``probe`` and by the upload, and a sheet, a section or a
    segment is somewhere below it, so the inputs are read from the nearest ancestor that has
    them (:func:`_plan_inputs`).

    A node whose ingest recorded no ``matched`` block gets an empty frontier and no
    exception. That is not a fallback: it means this subtree was not built by the file
    pipeline — a caller's own tree, an importer's — and Part 4's conditions are predicates
    over patterns that were never measured. Scheduling from patterns nobody took is the
    confident wrong answer 11.2 refuses for ``EXPLAIN``, and it is no better here.
    """
    fmt, patterns, options = _plan_inputs(session, node)
    if fmt is None:
        return Frontier(produced_by=produced_by, usetype=usetype)

    # `Scope.matches` and not scope equality. The scope a ROW declares may name several
    # producers and several usetypes; what is asked for here is exactly one of each,
    # because a fan-out writes one kind of child at a time. Comparing the two as objects
    # would find nothing — `embed`'s row names five rules and three usetypes, and no caller
    # is ever writing all of them at once.
    decided = _evaluate(fmt, patterns, options)
    eligible = tuple(
        spec for row, spec in decided.eligible if row.scope.matches(produced_by, usetype)
    )
    runnable, deferred = _split_by_handler(eligible)
    return Frontier(
        produced_by=produced_by,
        usetype=usetype,
        specs=tuple(runnable),
        deferred=deferred,
        not_applicable={
            row.task: decided.not_applicable[row.task]
            for row in TASK_ROWS
            if row.scope.matches(produced_by, usetype) and row.task in decided.not_applicable
        },
    )


def _plan_inputs(session: Session, node: Document) -> tuple[Optional[str], dict, Optional[dict]]:
    """``(format, patterns, options)`` for the ingest ``node`` belongs to, or ``(None, …)``.

    The nearest node at or above ``node`` carrying a ``matched`` row, which is the file node
    for everything the file pipeline builds. Nearest-ancestor rather than ``path[0]``,
    because an upload may name a ``parent_id`` and the root of the path is then the caller's
    own folder node rather than the file.

    The options come from the SAME node as the patterns, deliberately. They were resolved
    and validated before any of this tree existed, and freezing them there is what makes a
    fan-out schedule its children under what the upload asked for rather than under whatever
    the profile defaults have become since.
    """
    evidence = EvidenceRepository(session)
    for candidate_id in [node.id] + list(reversed(node.path or [])):
        matched = evidence.read(candidate_id, "matched")
        if isinstance(matched, dict) and matched.get("format"):
            return (
                matched["format"],
                matched.get("patterns") or {},
                evidence_value(session, candidate_id, OPTIONS_KEY),
            )
    return None, {}, None


def collection_option_layers(session: Session, parent_id: Optional[int]) -> tuple[dict, ...]:
    """The option layers a document uploaded under ``parent_id`` inherits, ROOT-FIRST.

    ``SPRINT_JOBS.md`` 5.1. **A collection is not a scope a table has to express. It is a
    subtree, and a setting on a node reaches the documents beneath it.** This is the walk
    that makes that true, and it is ``rollup_tasks._options_owner`` started one level
    higher: that one walks up from a segment to the file node and stops on its first read,
    because an upload always writes a complete ``options`` row there. This one starts at the
    PARENT and collects every layer above it, because a collection's declaration has to
    reach documents that do not exist yet.

    **EVERY LAYER, NOT THE NEAREST.** ``_options_owner`` returns one owner and uses its set
    wholesale, which is correct while there is only ever one. Under a tree a caller puts
    ``structure.max_tokens`` on ``/papers`` and ``facts.enabled`` on ``/papers/2026``, and
    nearest-wins gives them only the second — so a collection could not refine its parent
    without restating it. 5.1 point 1.

    **IT RUNS AT UPLOAD, NOT AT PLAN TIME, and that is what keeps 11.2's purity rule
    intact.** :func:`plan_after_probe` never learns that ancestors exist: the resolved set
    lands in the node's ``options`` row exactly as it did before, and a guard still reads
    only patterns and options. Resolving at plan time would put a tree read inside the
    planner and make ``EXPLAIN`` and the run two functions.

    A parent id that names nothing RAISES. The upload validates the parent before it gets
    here and every other caller can, so a missing node is a caller that did not — and
    silently inheriting nothing would ingest a document under defaults the collection it
    named was supposed to set.
    """
    if parent_id is None:
        return ()
    parent = session.get(Document, parent_id)
    if parent is None:
        raise LookupError(f"Parent document {parent_id} not found")
    evidence = EvidenceRepository(session)
    layers: list[dict] = []
    # `path` is the ancestor ids ROOT-FIRST and the parent itself is the last layer, which
    # is the order `resolve_options` applies them in: nearest last, so it wins.
    for node_id in list(parent.path or []) + [parent.id]:
        stored = evidence.read(node_id, OPTIONS_KEY)
        if isinstance(stored, dict) and stored:
            layers.append(stored)
    return tuple(layers)


def enqueue_frontier(
    tasks: TaskQueueRepository, child: Document, frontier: Frontier
) -> tuple[int, ...]:
    """Enqueue ``frontier`` onto one freshly created child, checking it is the right child.

    The check is the ratchet Part 14 asks for — "state a rule and audit the rule against
    what actually ran". A frontier is planned before the children exist and then used N
    times, so the pair it was planned for and the pair the node was stamped with are two
    statements that have to agree, made a few lines apart. If they drift, the child gets
    another kind of node's batch, and nothing downstream could tell: the tasks would run,
    fail on evidence they cannot find, and read as a broken document.
    """
    if child.produced_by != frontier.produced_by or child.usetype != frontier.usetype:
        raise ValueError(
            f"document {child.id} is stamped produced_by={child.produced_by!r} "
            f"usetype={child.usetype!r}, and the frontier planned for it is "
            f"{frontier.produced_by!r}/{frontier.usetype!r}; a node cannot be given "
            "another kind of node's batch"
        )
    return enqueue_batch(tasks, child.id, frontier.specs)


def _split_by_handler(specs: tuple[TaskSpec, ...]) -> tuple[list[TaskSpec], dict[str, str]]:
    """Partition eligible tasks into runnable now, and deferred with a stated reason.

    A spec whose ``after`` names a deferred task is deferred too: enqueuing it with an
    unresolvable dependency would either block forever (if the dependency is never
    created) or run out of order.
    """
    runnable: list[TaskSpec] = []
    deferred: dict[str, str] = {}
    for spec in specs:
        if not has_task_handler(spec.task_type):
            deferred[spec.task_type] = DEFERRED_REASON.get(
                spec.task_type, "no handler is registered for this task type"
            )
            continue
        blocking = [name for name in spec.after if name in deferred]
        if blocking:
            deferred[spec.task_type] = (
                f"depends on {blocking!r}, which is deferred; a task cannot be ordered "
                "after work that was never queued"
            )
            continue
        runnable.append(spec)
    return runnable, deferred


# ---------------------------------------------------------------------------
# EXPLAIN — spec 11.2
# ---------------------------------------------------------------------------

#: A task will be queued, and a worker can run it.
OUTCOME_ENQUEUED = "enqueued"
#: Its condition holds and the spec says to record it as never-attempted (``ocr``).
OUTCOME_SKIPPED = "skipped"
#: Its condition holds — or could — and no handler is registered, so it is NOT enqueued.
OUTCOME_DEFERRED = "deferred"
#: Its condition is false for the patterns that are known.
OUTCOME_NOT_APPLICABLE = "not_applicable"
#: Its condition can never hold for this format, whatever the bytes turn out to be.
OUTCOME_IMPOSSIBLE = "impossible"
#: Undecidable without patterns; ``if_condition_holds`` says what it would become.
OUTCOME_CONDITIONAL = "conditional"

#: The caller handed us patterns — a hypothesis, or a real node's ``matched.patterns``.
PATTERNS_SUPPLIED = "supplied"
#: ``probe`` was RUN, over real bytes the caller sent, and these are what it measured.
#: ``ANALYZE``'s source, and the only one of the four that is evidence about a FILE rather
#: than about a format: ``supplied`` is a hypothesis, ``no_prober`` and ``unknown`` are
#: facts about this appliance.
PATTERNS_PROBED = "probed"
#: The format has no prober, so ``probe_patterns`` returns ``{}``. Known, not assumed.
PATTERNS_NO_PROBER = "no_prober"
#: The format has a prober and nobody said what it would find.
PATTERNS_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Cost — SPRINT_JOBS.md Part 7
#
# One traversal of the plan with a choice of accumulator: counting tasks, child nodes,
# embeddings and LLM calls are the same fold. What makes it a fold at all rather than a
# query is 4.1's scope — "the parent rule's fan-out IS the child rule's multiplicity, so
# cardinality composes without anything having to read the tree" — and `_check_task_rows`
# invariant 3, which puts a producing row above every row scoped to its children. So one
# forward pass over TASK_ROWS in table order resolves every multiplicity.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Count:
    """An interval with an OPEN TOP. ``SPRINT_JOBS.md`` 7.1 and 7.2.

    7.1 says every accumulator returns ``[low, high]`` rather than a floor, because what
    blows a budget is the ceiling. 7.2 says an unmeasurable count is reported and never
    guessed — and ``EXPLAIN`` is given no bytes, so most of the measurements a fan-out
    bound divides have not been taken when the fold runs. ``high is None`` is that state:
    not "unbounded" and not "large", but *this appliance has not measured the thing that
    would bound it*, with :class:`Unmeasured` saying which thing and who would write it.

    A separate type from :data:`~jmfts_core.atoms.Interval` for exactly that reason. A
    bound returns a closed interval because it is handed the evidence; a FOLD over bounds
    some of which could not run returns this.
    """

    low: int
    #: ``None`` when nothing in this answer bounds it. Never a sentinel integer: a large
    #: number would be indistinguishable from a measured large number.
    high: Optional[int]

    def __post_init__(self) -> None:
        if self.low < 0:
            raise ValueError(f"a count cannot be negative; got low={self.low}")
        if self.high is not None and self.high < self.low:
            raise ValueError(f"count [{self.low}, {self.high}] has its top below its floor")

    def __add__(self, other: "Count") -> "Count":
        high = None if self.high is None or other.high is None else self.high + other.high
        return Count(self.low + other.low, high)

    def __mul__(self, other: "Count") -> "Count":
        """Instances × per-instance. ZERO ANNIHILATES AN OPEN TOP, and it has to.

        A rung that cannot run writes nothing, and nothing is a closed answer however open
        the per-instance bound is. Without this a not-applicable ``extract:sheet`` would
        put a ``?`` on the node total of every PDF in the appliance.
        """
        if self.high == 0 or other.high == 0:
            return Count(0, 0)
        high = None if self.high is None or other.high is None else self.high * other.high
        return Count(self.low * other.low, high)

    def __str__(self) -> str:
        if self.low == self.high:
            return str(self.low)
        return f"[{self.low}, {'?' if self.high is None else self.high}]"


#: Nothing runs and nothing is written.
ZERO = Count(0, 0)
#: Exactly once. The root scope's multiplicity, and an enqueued row's run factor.
ONE = Count(1, 1)


@dataclass(frozen=True)
class Unmeasured:
    """A measurement the fold needed and could not take. 7.1's ``?`` line.

    7.1's worked example prints ``? rows — produced by profile:sheet`` beneath the totals,
    and says the ``?`` line "naming what would collapse the interval is the part that
    carries the value". This is that line: the evidence name, the atom whose bound wanted
    it, and who writes it — the last read out of :mod:`jmfts_core.evidence` rather than
    from a table here, so an atom that starts producing it moves this sentence the same
    day.
    """

    #: A registry name, from the bound's own ``reads``.
    evidence: str
    #: The atom whose :class:`~jmfts_core.atoms.Fanout` declared it.
    needed_by: str
    #: :func:`jmfts_core.evidence.written_by`'s sentence.
    written_by: str


@dataclass(frozen=True)
class PlanCost:
    """How much, beside ``EXPLAIN``'s what. One traversal, four accumulators.

    :attr:`not_counted` is not a disclaimer. ``SPRINT_0_6_0.md`` Block D: phase 5 inherits
    a debt it does not pay — ``Fact.locus`` has no term for a write into another tree and
    three shipped tasks write there — and "step 14 should state what it does not report
    rather than quietly report a number that is wrong by three tasks". Every sentence in
    it is DERIVED from the declarations, never a checked-in list (Part 14's rule), so a
    task that stops being in that position stops being named.
    """

    #: Task INSTANCES, not table rows. A forty-sheet workbook's ``profile:sheet`` is forty.
    tasks: Count
    #: Child nodes the plan's rules write, from their declared fan-out bounds.
    nodes: Count
    #: Instances by :data:`~jmfts_core.atoms.COST_CLASSES`. Embeddings are ``model`` and
    #: LLM calls are ``llm``; 8.1's budget is sized on the second.
    by_cost_class: dict[str, Count]
    #: Every open top in this answer, and what would close it.
    unmeasured: tuple[Unmeasured, ...]
    #: What these totals leave out, in sentences, each derived from the declarations.
    not_counted: tuple[str, ...]


#: How many times a row with this outcome runs, per node in its scope. ``conditional`` is
#: the only one that is neither zero nor one: the patterns that decide it were not
#: supplied, so it runs never or once and the fold refuses to pick. ``skipped`` is zero
#: because the spec's one skipped row (``ocr``) is RECORDED as never-attempted rather than
#: queued, and ``deferred`` is zero because nothing runs a task with no handler.
_RUN_FACTOR: dict[str, Count] = {
    OUTCOME_ENQUEUED: ONE,
    OUTCOME_CONDITIONAL: Count(0, 1),
    OUTCOME_SKIPPED: ZERO,
    OUTCOME_DEFERRED: ZERO,
    OUTCOME_NOT_APPLICABLE: ZERO,
    OUTCOME_IMPOSSIBLE: ZERO,
}


@dataclass(frozen=True)
class ExplainedTask:
    """One row of Part 4's table as an ``EXPLAIN`` answer. Transport-neutral, like
    :class:`DownstreamPlan`; ``jmfts_client.contracts.explain`` is the wire form.

    ``requires``/``forbids`` are RESOLVED for the format being explained — the
    :data:`DECLARED_STRUCTURE` sentinel is replaced by the pattern it names, and a sentinel
    with no entry for the format is dropped from the list rather than emitted as a null.
    Dropping it loses nothing: as a requirement its absence is what makes the row
    ``impossible`` and :attr:`reason` says so, and as a prohibition it prohibits nothing.
    """

    task: str
    outcome: str
    #: Set ONLY when :attr:`outcome` is ``conditional`` — what the row becomes if its
    #: condition holds. ``deferred`` is legal here for a row whose own handler exists but
    #: whose predecessor is conditional and unimplemented; no row in the shipped table is
    #: in that position today.
    if_condition_holds: Optional[str] = None
    #: The skip reason, the deferral reason, or the condition that was false — whichever
    #: applies. Taken from the same strings the run records, never re-worded here.
    reason: Optional[str] = None
    write_mode: Optional[str] = None
    after: tuple[str, ...] = ()
    #: Alternatives, of which one suffices — see :attr:`TaskRow.after_any`. Kept apart from
    #: :attr:`after` here for the reason it is kept apart there: a reader who saw both
    #: structure rungs in one ``after`` list would conclude the row can never fire.
    after_any: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    forbids: tuple[str, ...] = ()
    #: The resolved options for the row's ``params_key``; ``{}`` for a row that takes none.
    params: dict = field(default_factory=dict)
    #: Which nodes the row applies to, as ``str(Scope)``: ``@root`` for the file node, or
    #: ``@children_of(rule|rule):usetype|usetype``. Reported because without it a plan
    #: listing ``probe``, ``structure:sheets`` and ``profile:sheet`` reads as three tasks on
    #: one node, and the third is one per sheet.
    scope: str = str(ROOT_SCOPE)
    #: HOW MANY instances of this task the plan holds — :attr:`scope`'s cardinality, times
    #: zero or one for whether :attr:`outcome` lets it run at all. ``@root`` is one node, so
    #: a root-scoped row is :data:`ONE` or :data:`ZERO`; a child-scoped row is however many
    #: nodes the rules above it write. Part 7.
    multiplicity: Count = ONE
    #: Child nodes ALL of those instances write together, from the atom's declared fan-out
    #: bound (2.4). :data:`ZERO` for a task that writes no children, and an open top when
    #: the bound's evidence has not been measured — :attr:`PlanCost.unmeasured` says which.
    writes: Count = ZERO
    #: The usetype :attr:`writes` counts (``Fanout.counts``), because "wrote 5" has to say
    #: five WHAT before it can be held against a ceiling. ``None`` when nothing is written.
    writes_usetype: Optional[str] = None
    #: The bound in the terms 2.4's table uses — "⌊characters / min_chunk_length⌋ chunks".
    #: Reported verbatim rather than re-worded, for the reason :attr:`reason` is.
    writes_basis: Optional[str] = None


@dataclass(frozen=True)
class ExplainedPlan:
    """What a format with these options would do, and on what basis it was decided.

    :attr:`patterns_source` is not decoration. ``EXPLAIN`` has no bytes and therefore no
    patterns, so an answer that did not say where its patterns came from would be
    indistinguishable from one that made them up.
    """

    format: str
    #: Whether ``probe`` can look inside this format at all (``probe.PROBERS_AVAILABLE``).
    prober_available: bool
    #: Whether every task's outcome is decided. False means some rows are ``conditional``.
    patterns_known: bool
    patterns_source: str
    #: Supplied pattern keys no row's condition consults for this format. Reported, never
    #: rejected: pasting a real ``matched.patterns`` block is the obvious use and it
    #: carries measurements (``page_count``, ``outline_depth``) no condition reads. What
    #: the list is FOR is making a typo — ``has_text_lyer`` — visible as a key that decided
    #: nothing, instead of silently planning as though the pattern were false.
    patterns_ignored: tuple[str, ...]
    #: The RESOLVED options, all groups — what the queue rows would actually carry.
    options: dict
    #: ``probe`` first (it always runs), then every row of :data:`TASK_ROWS` in table
    #: order. Every row appears: a task missing from a plan is a wrong answer, not a short
    #: one.
    tasks: tuple[ExplainedTask, ...]
    #: How much, folded over :attr:`tasks`. Part 7. Not optional and not lazily computed:
    #: the fold is a walk over declarations with no I/O in it, and an ``EXPLAIN`` that
    #: answered "which" and made "how many" a second request would be two answers about one
    #: plan that a caller could receive out of step.
    cost: PlanCost


def _resolved_names(terms: tuple[Term, ...], fmt: str) -> tuple[str, ...]:
    """A row's ``requires``/``forbids`` as sentences, with the sentinel resolved.

    Strings, and they stay strings on the wire. A term with no operator renders as the bare
    pattern name it always did, so a row that gained none says exactly what it said before
    Phase 4; a term that has one renders as the comparison. Whether ``EXPLAIN`` wants the
    STRUCTURE instead is a separate question from whether the table has it, and answering it
    here would spend a client-visible break on a shape nobody has asked for yet.
    """
    rendered = []
    for t in terms:
        if isinstance(t.left, str) and _resolve_pattern(t.left, fmt) is None:
            continue
        rendered.append(render_term(t, fmt))
    return tuple(rendered)


#: The prefix a fan-out bound's ``reads`` entry carries when it names one of ``probe``'s
#: measurements. The fold has those and nothing else, so this is the whole of what
#: separates a bound it can run from one it cannot.
_PATTERN_PREFIX = "matched.patterns."


def _consulted_patterns(fmt: str) -> set[str]:
    """Every PATTERN name this table reads for this format — to decide, or to count.

    Not the same thing as :func:`_resolved_names`, and the difference is what
    ``patterns_ignored`` reports. This answers "which measurements decide anything here",
    so a term reading an option contributes nothing to it: ``options.facts.enabled`` is not
    a key a caller's ``matched.patterns`` could have supplied.

    **A FAN-OUT BOUND'S READS COUNT AS CONSULTED, and Part 7 is what made them.** Before
    the cost fold, ``sheet_count`` decided nothing — no row's condition names it — so a
    caller who pasted a real workbook's pattern block got it back under
    ``patterns_ignored``, correctly. It now decides ``structure:sheets``' multiplicity, and
    reporting it as a key that decided nothing while the plan divided by it would be the
    confident wrong answer ``patterns_ignored`` exists to prevent.
    """
    consulted: set[str] = set()
    for row in TASK_ROWS:
        for t in row.requires + row.forbids:
            if not isinstance(t.left, str):
                continue
            pattern = _resolve_pattern(t.left, fmt)
            if pattern is not None:
                consulted.add(pattern)
    for atom in ATOMS.values():
        if atom.fanout is None:
            continue
        for name in atom.fanout.reads:
            if name.startswith(_PATTERN_PREFIX):
                consulted.add(name[len(_PATTERN_PREFIX) :])
    return consulted


def _impossible_after(row: TaskRow, impossible: set[str]) -> bool:
    """Whether ``row``'s ordering or its scope alone makes it impossible.

    ``after`` is a conjunction, so ONE impossible predecessor is enough. ``after_any`` is a
    disjunction, so it takes ALL of them — a row whose alternatives include one that can
    still fire is not impossible, it is merely undecided.

    A child scope is a third case with the disjunction's shape and a stronger meaning: if no
    rule that could produce this row's scope can ever fire for this format, the node the row
    runs on can never exist. ``profile:sheet`` on a PDF is the worked example — not "the
    sheet had no header", but "this format names no worksheet list".
    """
    if any(name in impossible for name in row.after):
        return True
    if row.scope.kind == SCOPE_CHILDREN and all(
        name in impossible for name in row.scope.produced_by
    ):
        return True
    return bool(row.after_any) and all(name in impossible for name in row.after_any)


def _explained_row(row: TaskRow, fmt: str, resolved: dict, outcome: str, **kwargs) -> ExplainedTask:
    """One :class:`ExplainedTask` from a row, with the parts that never vary filled in."""
    return ExplainedTask(
        task=row.task,
        outcome=outcome,
        write_mode=row.write_mode,
        after=row.after,
        after_any=row.after_any,
        requires=_resolved_names(row.requires, fmt),
        forbids=_resolved_names(row.forbids, fmt),
        params=dict(resolved[row.params_key]) if row.params_key else {},
        scope=str(row.scope),
        **kwargs,
    )


def _explain_concrete(
    fmt: str, patterns: dict, options: Optional[dict], resolved: dict
) -> tuple[ExplainedTask, ...]:
    """Every row's outcome, at every scope, when the patterns are known.

    The decision is taken from :func:`_evaluate` and :func:`_split_by_handler` UNCHANGED —
    the same two calls ``run_probe`` and :func:`plan_frontier` make — rather than
    re-derived. A second evaluator of the same table would be free to disagree with the
    first, and the disagreement would surface as an ``EXPLAIN`` that confidently describes a
    run that does something else.

    ``_split_by_handler`` is applied PER SCOPE, and that is not an optimisation. Its rule is
    that a spec ordered after a deferred spec is deferred too, and "ordered after" is a
    relation within one node's batch (5.5). Run over every scope at once it would defer a
    sheet's ``extract:sheet`` because some unrelated file-scoped row had no handler.

    Part 0 recorded that ``EXPLAIN`` "stops at the sheet list" — ``profile:sheet`` and
    ``extract:sheet`` were not rows, so a ``.xlsx`` forecast ended at the rung that creates
    the sheets and said nothing about the two tasks per sheet that follow. They are rows
    now, so the forecast reaches them; :attr:`ExplainedTask.scope` is what says they are one
    batch per sheet rather than two more tasks on the file.
    """
    decided = _evaluate(fmt, patterns, options)
    enqueued: set[str] = set()
    deferred: dict[str, str] = {}
    by_scope: dict[Scope, list[TaskSpec]] = {}
    for row, spec in decided.eligible:
        by_scope.setdefault(row.scope, []).append(spec)
    for specs in by_scope.values():
        runnable, scope_deferred = _split_by_handler(tuple(specs))
        enqueued.update(spec.task_type for spec in runnable)
        deferred.update(scope_deferred)
    skipped = {entry.task_type: entry.reason for _, entry in decided.skipped}

    impossible: set[str] = set()
    tasks: list[ExplainedTask] = []
    for row in TASK_ROWS:
        blocked = decided.not_applicable.get(row.task)
        if blocked is not None:
            # `impossible` REFINES not-applicable rather than competing with it: the run
            # records the same row as not-applicable, and the outcome adds the fact that no
            # bytes of this format could have changed that.
            #
            # The row's OWN unsatisfiable requirement is reported ahead of a blocked
            # dependency, which is the one place this deliberately departs from
            # `_blocking_reason`'s order. Both are true of `structure:declared` on a format
            # with no declared-structure pattern and no text layer; only one of them is why
            # the answer is `impossible` rather than "not this time".
            sentinel = _unsatisfiable_sentinel(row, fmt)
            if sentinel is not None:
                impossible.add(row.task)
                reason = SENTINEL_REASONS[sentinel](fmt)
            elif _impossible_after(row, impossible):
                impossible.add(row.task)
                reason = blocked
            else:
                tasks.append(
                    _explained_row(row, fmt, resolved, OUTCOME_NOT_APPLICABLE, reason=blocked)
                )
                continue
            tasks.append(_explained_row(row, fmt, resolved, OUTCOME_IMPOSSIBLE, reason=reason))
        elif row.task in skipped:
            tasks.append(
                _explained_row(row, fmt, resolved, OUTCOME_SKIPPED, reason=skipped[row.task])
            )
        elif row.task in deferred:
            tasks.append(
                _explained_row(row, fmt, resolved, OUTCOME_DEFERRED, reason=deferred[row.task])
            )
        elif row.task in enqueued:
            tasks.append(_explained_row(row, fmt, resolved, OUTCOME_ENQUEUED))
        else:
            # Unreachable: the three lists of a DownstreamPlan partition TASK_ROWS, and
            # `_split_by_handler` partitions the eligible list. Raised rather than assumed
            # because the alternative is reporting some default outcome for a row the plan
            # placed somewhere this function does not know about.
            raise RuntimeError(
                f"task {row.task!r} appears in no part of the plan for format {fmt!r}; "
                "the evaluation no longer partitions TASK_ROWS"
            )
    return tuple(tasks)


def _explain_conditional(fmt: str, resolved: dict) -> tuple[ExplainedTask, ...]:
    """Every row's outcome, when the format has a prober and nobody said what it found.

    Most rows come out ``conditional`` — the resolved patterns that decide them are on the
    row, and ``if_condition_holds`` says what they would become. Three kinds are decided
    anyway, and reporting them as undecided would be a worse answer than no answer: a row
    whose requirement names a pattern this format has no name for can never fire, a row
    after such a row can never fire either, and a row with no registered handler is not
    enqueued whatever the bytes say.
    """
    impossible: set[str] = set()
    deferred: set[str] = set()
    tasks: list[ExplainedTask] = []

    for row in TASK_ROWS:
        # DECIDED even here, because it is decidable: a term about the REQUEST reads the
        # options, and options are one of EXPLAIN's two known inputs in both modes.
        # Reporting a row the caller switched off as `conditional` on patterns nobody has
        # measured would be a worse answer than the one available.
        #
        # The empty pattern set is not a stand-in for measurements this mode does not have.
        # `_request_reason` reads option terms and nothing else, by construction — a term
        # whose left side is a pattern is exactly what this mode CANNOT decide, and it is
        # reported as conditional below.
        requested = _request_reason(row, fmt, {}, resolved)
        if requested is not None:
            tasks.append(
                _explained_row(row, fmt, resolved, OUTCOME_NOT_APPLICABLE, reason=requested)
            )
            continue

        # Same precedence as `_explain_concrete`: the row's own unsatisfiable requirement
        # first, a dependency that can never fire second.
        sentinel = _unsatisfiable_sentinel(row, fmt)
        if sentinel is not None:
            impossible.add(row.task)
            reason = SENTINEL_REASONS[sentinel](fmt)
            tasks.append(_explained_row(row, fmt, resolved, OUTCOME_IMPOSSIBLE, reason=reason))
            continue
        if _impossible_after(row, impossible):
            impossible.add(row.task)
            blocking = next((name for name in row.after if name in impossible), None)
            if blocking is not None:
                reason = _dependency_reason(row.task, blocking)
            elif row.scope.kind == SCOPE_CHILDREN and all(
                name in impossible for name in row.scope.produced_by
            ):
                # The scope is checked ahead of `after_any` here because it is the stronger
                # statement: the node this row runs on can never exist for this format, and
                # naming an ordering instead would describe a task that is merely waiting.
                reason = _no_scope_reason(row)
            else:
                reason = _alternative_dependency_reason(row.task, row.after_any)
            tasks.append(_explained_row(row, fmt, resolved, OUTCOME_IMPOSSIBLE, reason=reason))
            continue

        if row.skip_reason is not None:
            # `ocr` is the one row the spec itself says to record rather than run, so what
            # is unknown about it is only whether the condition fires at all.
            tasks.append(
                _explained_row(
                    row,
                    fmt,
                    resolved,
                    OUTCOME_CONDITIONAL,
                    if_condition_holds=OUTCOME_SKIPPED,
                    reason=row.skip_reason,
                )
            )
            continue

        if not has_task_handler(row.task):
            deferred.add(row.task)
            reason = DEFERRED_REASON.get(row.task, "no handler is registered for this task type")
            tasks.append(_explained_row(row, fmt, resolved, OUTCOME_DEFERRED, reason=reason))
            continue
        # Same conjunction/disjunction split as `_impossible_after`: one deferred `after`
        # defers the row, but an `after_any` alternative that still has a handler keeps it
        # alive.
        blocked_on: Optional[list[str]] = None
        if any(name in deferred for name in row.after):
            blocked_on = [name for name in row.after if name in deferred]
        elif row.scope.kind == SCOPE_CHILDREN and all(
            name in deferred for name in row.scope.produced_by
        ):
            # Every rule that would create this row's node is deferred, so no node of that
            # kind is written and this row is not queued either — the same conclusion the
            # `after` case reaches, from the scope rather than from an ordering.
            blocked_on = list(row.scope.produced_by)
        elif row.after_any and all(name in deferred for name in row.after_any):
            blocked_on = list(row.after_any)
        if blocked_on is not None:
            deferred.add(row.task)
            tasks.append(
                _explained_row(
                    row,
                    fmt,
                    resolved,
                    OUTCOME_DEFERRED,
                    reason=(
                        f"depends on {blocked_on!r}, which is deferred; a task cannot be "
                        "ordered after work that was never queued"
                    ),
                )
            )
            continue

        tasks.append(
            _explained_row(
                row, fmt, resolved, OUTCOME_CONDITIONAL, if_condition_holds=OUTCOME_ENQUEUED
            )
        )
    return tuple(tasks)


# ---------------------------------------------------------------------------
# The fold — Part 7
# ---------------------------------------------------------------------------


def _fold_inputs(patterns: Optional[dict]) -> dict:
    """The evidence a bound may be handed at ``EXPLAIN`` time, by registry name.

    ``probe``'s measurements and NOTHING ELSE, and that is the whole of 7.2. Every other
    name a bound reads — ``extraction.characters``, ``sheet.measurements.rows``,
    ``child_count`` — is written by a task that has not run, so the fold does not have it
    and says so rather than standing in a number for it. The parameters are the bound's
    other argument and those the fold does have: they were resolved before any of this.
    """
    return {} if patterns is None else {f"{_PATTERN_PREFIX}{k}": v for k, v in patterns.items()}


def _not_counted(tasks: tuple[ExplainedTask, ...]) -> tuple[str, ...]:
    """What these totals leave out. DERIVED, per Part 14's rule against a second list.

    Two statements, each from a declaration rather than from a name written down here.
    There were three:

    1. **Twelve task types are not rows of Part 4's table at all.** ``explain_plan`` reads
       ``TASK_ROWS`` and nothing else (``SPRINT_0_6_0.md`` Block C), so the rollup walk's
       ``summarize``, the settling boundary's ``index:bm25``, a binding's ``derive:rule``
       and the three ``fetch:*`` entry points are outside every number here. Phase 7 is
       where that ends.
    2. **A task that writes into another tree writes nothing this fold can count.**
       ``SPRINT_0_5_0.md`` 6.1: ``Fact.locus`` has no term for it, so those atoms declare
       ``produces=()`` — which is the derivable signature, and ``extract:facts`` is the one
       of the three that is a row.
    3. *Retired 2026-10-12.* **An atom that writes children and declares no fan-out
       bound.** 2.4 requires one, and ``structure:conversation`` was the one that had none,
       so its ``writes`` was open rather than a number short by every turn in the
       transcript. ``SPRINT_0_6_0.md`` step 36 gave it a bound (question 4.11, option A)
       and made :class:`~jmfts_core.atoms.Atom` refuse the position, so no registered atom
       can be in it and there is nothing left here to derive the statement from.
    """
    lines: list[str] = []

    outside = sorted(set(ATOMS) - {row.task for row in TASK_ROWS} - {TASK_PROBE})
    if outside:
        lines.append(
            f"these totals are Part 4's table only; {len(outside)} registered task types are "
            f"scheduled elsewhere — by the settling walk, by a fetch request or by a binding "
            f"— and are in no number here: {', '.join(outside)}"
        )

    running = {t.task for t in tasks if t.multiplicity.high != 0}
    for task in sorted(running):
        atom = ATOMS.get(task)
        if atom is None:
            continue
        if not atom.produces:
            lines.append(
                f"{task} writes rows and nodes outside this tree, and the locus vocabulary "
                "has no term for that (INGEST_SPEC.md 5.3, SPRINT_0_5_0.md 6.1), so it "
                "declares `produces=()` and `nodes` does not count what it writes"
            )
    return tuple(lines)


def _fold_cost(
    tasks: tuple[ExplainedTask, ...], patterns: Optional[dict]
) -> tuple[tuple[ExplainedTask, ...], PlanCost]:
    """Multiplicity and the four totals. ``SPRINT_JOBS.md`` Part 7.

    ONE FORWARD PASS, AND THAT IS A PROPERTY OF THE TABLE RATHER THAN OF THIS FUNCTION.
    ``_check_task_rows`` invariant 3 refuses a row scoped to the children of a rule below
    it, so by the time a child-scoped row is reached every rule that could write its node
    has already contributed. Nothing here reads the tree, and 4.1's "the parent rule's
    fan-out IS the child rule's multiplicity" is why it does not have to.

    **A scope's cardinality is a SUM over producing rules, not a maximum.** Two of
    ``embed``'s five producers are the alternative structure rungs and exactly one of them
    ever fires — but which is already decided by then: the one that cannot fire is
    ``not_applicable``, its multiplicity is :data:`ZERO`, and zero is what it contributes.
    Adding is therefore right in the concrete mode and right in the conditional one, where
    both rungs carry ``[0, 1]`` and the sum is the honest ``[0, 2]``.

    Returns the tasks again because each one gains its own numbers; the totals are folds
    over those, so the per-task answer and the plan's answer cannot disagree.
    """
    rows_by_task = {row.task: row for row in TASK_ROWS}
    evidence = _fold_inputs(patterns)

    #: (producing rule, usetype) -> the nodes of that kind it writes. 4.1's scope is
    #: exactly this pair, so a scope's cardinality is a lookup rather than a query.
    produced: dict[tuple[str, str], Count] = {}
    # THERE WAS AN `undeclared` SET HERE, for rules that write children and declare no
    # bound: what they wrote was not a number and not a usetype, so a scope naming one was
    # forced open. `structure:conversation` was the only member, and `Atom` refuses the
    # position since `SPRINT_0_6_0.md` step 36, so every child-writing rule reaches the
    # `fanout` branch below and contributes to `produced` like any other.
    unmeasured: list[Unmeasured] = []
    seen_unmeasured: set[tuple[str, str]] = set()

    folded: list[ExplainedTask] = []
    for task in tasks:
        row = rows_by_task.get(task.task)
        if row is None or row.scope.kind == SCOPE_ROOT:
            # `probe` has no row and is the file node's; every Part 4 row defaults there.
            scope_count = ONE
        else:
            scope_count = ZERO
            for producer in row.scope.produced_by:
                for usetype in row.scope.usetypes:
                    scope_count = scope_count + produced.get((producer, usetype), ZERO)

        multiplicity = scope_count * _RUN_FACTOR[task.outcome]

        writes, usetype_written, basis = ZERO, None, None
        atom = ATOMS.get(task.task)
        if atom is not None and atom.fanout is not None:
            usetype_written = atom.fanout.counts
            basis = atom.fanout.basis
            missing = [name for name in atom.fanout.reads if name not in evidence]
            if missing:
                # RECORDED ONLY FOR A RUNG THAT CAN RUN. `structure:declared`'s bound
                # divides `extraction.characters` and a workbook has no text layer, so the
                # rung is not-applicable and its bound is not a measurement this plan is
                # missing — it is one this plan does not want. A `?` line for it would send
                # a caller looking for a way to close an interval that is already closed at
                # zero.
                if multiplicity.high != 0:
                    for name in missing:
                        if (name, task.task) in seen_unmeasured:
                            continue
                        seen_unmeasured.add((name, task.task))
                        unmeasured.append(
                            Unmeasured(
                                evidence=name,
                                needed_by=task.task,
                                written_by=evidence_written_by(name),
                            )
                        )
                per_instance = Count(0, None)
            else:
                low, high = atom.fanout.bound(evidence, task.params)
                per_instance = Count(low, high)
            writes = multiplicity * per_instance
            key = (task.task, usetype_written)
            produced[key] = produced.get(key, ZERO) + writes

        folded.append(
            replace(
                task,
                multiplicity=multiplicity,
                writes=writes,
                writes_usetype=usetype_written,
                writes_basis=basis,
            )
        )

    total_tasks, total_nodes = ZERO, ZERO
    by_cost_class = {cost_class: ZERO for cost_class in COST_CLASSES}
    for task in folded:
        total_tasks = total_tasks + task.multiplicity
        total_nodes = total_nodes + task.writes
        atom = ATOMS.get(task.task)
        if atom is not None:
            by_cost_class[atom.cost_class] = by_cost_class[atom.cost_class] + task.multiplicity

    return tuple(folded), PlanCost(
        tasks=total_tasks,
        nodes=total_nodes,
        by_cost_class=by_cost_class,
        unmeasured=tuple(unmeasured),
        not_counted=_not_counted(tuple(folded)),
    )


def explain_plan(
    fmt: str,
    options: Optional[dict] = None,
    patterns: Optional[dict] = None,
    *,
    patterns_source: Optional[str] = None,
) -> ExplainedPlan:
    """``EXPLAIN`` for file ingestion: what this format with these options would do.

    Spec 11.2's first mode — format and options, NO BYTES. This function never opens a
    file. ``ANALYZE``, 11.2's second mode, is ``IngestService.analyze_ingest``: it runs
    :func:`~jmfts_core.probe.detect_format` and :func:`~jmfts_core.probe.probe_patterns`
    over real bytes and then calls THIS, passing what probe measured as ``patterns`` and
    :data:`PATTERNS_PROBED` as ``patterns_source``. One planner answers both modes, which
    is what stops the two from ever describing different runs.

    ``patterns_source`` is therefore an override of the provenance label, not of the plan:
    the rows are decided by ``patterns`` either way, and this only changes what the answer
    says about where those patterns came from. It is refused without ``patterns``, because
    a conditional plan has no provenance to state.

    :func:`plan_after_probe` is already a pure function of ``(format, patterns, options)``,
    and ``EXPLAIN`` has two of those three. It therefore answers in one of two ways, and
    :attr:`ExplainedPlan.patterns_source` states which, because an answer that did not say
    would be indistinguishable from one that invented the third input:

    **Concrete** — the patterns are known and every row is decided. Either the caller
    supplied them (a hypothesis, or a real node's ``matched.patterns`` pasted back), or the
    format has no prober, in which case ``probe_patterns`` returns ``{}`` at runtime and the
    empty pattern set is a measured fact about this appliance rather than an assumption
    about the file. The second is the honest answer to 11.2's own example question, "what
    would a ``.pptx`` do?" — today: probe identifies it, and nothing downstream can become
    eligible, because nothing can report a pattern.

    **Conditional** — the format has a prober and the caller said nothing about what it
    would find. Each row then carries the resolved patterns that decide it and what it
    would become if they held. Rows that are decidable anyway are still decided; see
    :func:`_explain_conditional`.

    Patterns are NEVER fabricated. There is no "assume a text layer" default, because a
    plan built on a guessed input is a wrong answer with a confident shape — and 11.2's
    whole claim is that the plan is a first-class answer.

    ``options`` are overrides, resolved here exactly as an upload resolves them, so a
    misspelled option raises ``ValueError`` naming it (a 400 at the endpoint) rather than
    being explained away as a plan the run would not produce.
    """
    if patterns_source is not None and patterns is None:
        raise ValueError(
            "patterns_source says where `patterns` came from and there are none; labelling "
            "a conditional plan with a provenance would claim an input it does not have"
        )

    resolved = resolve_options(fmt, options)
    prober_available = fmt in PROBERS_AVAILABLE

    if patterns is not None:
        known: Optional[dict] = patterns
        source = patterns_source or PATTERNS_SUPPLIED
    elif not prober_available:
        known, source = {}, PATTERNS_NO_PROBER
    else:
        known, source = None, PATTERNS_UNKNOWN

    if known is not None:
        rows = _explain_concrete(fmt, known, options, resolved)
    else:
        rows = _explain_conditional(fmt, resolved)

    tasks, cost = _fold_cost(
        (
            # probe is not a TASK_ROWS entry because nothing decides it: Part 4 specifies it
            # as depending on nothing and always running, which is exactly why it has no
            # condition to declare. It leads the list for the same reason.
            ExplainedTask(task=TASK_PROBE, outcome=OUTCOME_ENQUEUED, write_mode=PROBE_WRITE_MODE),
        )
        + rows,
        known,
    )

    return ExplainedPlan(
        format=fmt,
        prober_available=prober_available,
        patterns_known=known is not None,
        patterns_source=source,
        patterns_ignored=tuple(sorted(set(patterns or {}) - _consulted_patterns(fmt))),
        options=resolved,
        tasks=tasks,
        cost=cost,
    )


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


# `matched` and nothing else. Probe enqueues the Part 4 batch, and enqueuing is not a
# write to the tree — PROBE_WRITE_MODE says the same thing about the write mode, for the
# same reason. So no fan-out and no child key: the nodes the batch goes on to create are
# created by the atoms in it, and each of those declares its own.
@register_task_handler(
    TASK_PROBE,
    consumes=(f"{EV_FILE}@self", f"{EV_BLOB}@self"),
    produces=(f"{EV_MATCHED}@self",),
    write_mode=PROBE_WRITE_MODE,
    cost_class=COST_CPU,
)
def run_probe(session: Session, task: TaskQueue) -> TaskOutcome:
    """Identify the bytes, measure the content patterns, and schedule what follows.

    Spec 3.1 keeps file type and content pattern in separate fields, and this runs them
    in that order: :func:`~jmfts_core.probe.detect_format` says how to open the bytes,
    :func:`~jmfts_core.probe.probe_patterns` says what is inside. The node's ``matched``
    block gets both (3.3), then Part 4's conditions are evaluated over the patterns.

    Failure is NOT caught here. A corrupt PDF, a missing prober library, a node with no
    stored bytes — each raises, the worker classifies it, and the node's attempt log
    records the failure with its classification. The step-3 version of this code ran
    inline inside the upload request and had to swallow the exception, because raising
    would have rolled back and discarded bytes the client had already sent. The queue
    removes that constraint: the upload has committed long before this runs.
    """
    doc = session.get(Document, task.scope_document_id)
    if doc is None:
        raise ValueError(
            f"probe is scoped to document {task.scope_document_id}, which does not exist"
        )

    evidence = EvidenceRepository(session)
    file_block = evidence.read(doc.id, "file")
    if not isinstance(file_block, dict):
        raise ValueError(
            f"document {doc.id} carries no `file` block; probe reads the uploaded bytes "
            "and the record of what was received, and this node was not created by an "
            "upload (INGEST_SPEC.md 3.3)"
        )

    data = BlobRepository(session).read_bytes(doc.id)
    if data is None:
        raise ValueError(
            f"document {doc.id} has a `file` block but no stored blob; the bytes it "
            "describes are gone and there is nothing to probe"
        )

    declared_mime = file_block.get("declared_mime")
    # Re-derived from the bytes rather than read back off the `file` block. Detection is
    # cheap and deterministic, and deriving it here means this handler works on any node
    # carrying bytes, not only on one this exact version of the uploader wrote.
    detection = detect_format(
        data, filename=file_block.get("filename"), declared_mime=declared_mime
    )

    detail: dict = {
        "format": detection.format,
        "declared_mime": declared_mime,
        "detected_mime": detection.detected_mime,
        "detected_by": detection.detected_by,
    }
    # Spec 3.1: when declared and detected disagree, record both. Both are already on the
    # `file` block; this makes the DISAGREEMENT itself a first-class entry in the log, so
    # nobody has to notice it by comparing fields.
    if detection.mime_agrees is False:
        detail["mime_conflict"] = {
            "declared": declared_mime,
            "detected": detection.detected_mime,
        }

    patterns, pattern_detail = probe_patterns(data, detection)
    detail.update(pattern_detail)

    evidence.write(
        doc.id,
        "matched",
        {
            "format": detection.format,
            "patterns": patterns,
            "probed_at": utc_now_iso(),
        },
    )
    session.flush()

    # The options the UPLOAD was made with, read back off the node rather than resolved
    # afresh. They were resolved and validated before this node existed, and freezing them
    # there is what makes a re-probe reproduce the run it is re-probing instead of picking
    # up whatever the profile defaults have become since. A node with no `options` block
    # was written by something older than this block and recorded no overrides, which
    # resolves to the same profile defaults it already ran with.
    plan = plan_after_probe(
        detection.format, patterns, evidence_value(session, doc.id, OPTIONS_KEY)
    )
    runnable, deferred = _split_by_handler(plan.eligible)

    tasks = TaskQueueRepository(session)
    enqueued_ids = enqueue_batch(tasks, doc.id, runnable) if runnable else ()

    docs = DocumentRepository(session)
    for entry in plan.skipped:
        docs.upsert_attempt(doc, _skipped_record(doc, entry, docs.attempt_counts(doc)))

    detail["enqueued"] = {spec.task_type: task_id for spec, task_id in zip(runnable, enqueued_ids)}
    detail["deferred"] = deferred
    detail["skipped"] = {entry.task_type: entry.reason for entry in plan.skipped}
    detail["not_applicable"] = plan.not_applicable
    return TaskOutcome(detail=detail)


def _skipped_record(doc: Document, entry: SkippedTask, counts: dict[str, int]) -> AttemptRecord:
    """A spec-3.4 ``skipped`` entry for a task that will never be attempted.

    ``skipped`` means the task was NEVER attempted, and 3.4 requires it to carry
    ``detail.reason``. There is no queue row and therefore no ``task_id``: inventing one
    would point the log at a row that does not exist.

    The timestamps are the instant the decision was taken, not a duration of work — the
    record contract requires a terminal status to carry both, and the honest reading of
    them for a skip is "this is when we decided not to". It is the convention the deleted
    ``execute_pipeline`` used for a disabled stage, kept because a reader of an old node's
    log should not have to know which pipeline wrote it.
    """
    params: dict = {}
    decided_at = datetime.now(timezone.utc)
    return AttemptRecord(
        task=entry.task_type,
        status="skipped",
        attempt=counts.get(entry.task_type, 0) + 1,
        scope_document_id=doc.id,
        params=params,
        param_fingerprint=param_fingerprint(params),
        started_at=decided_at,
        finished_at=decided_at,
        detail={"reason": entry.reason},
    )


def utc_now_iso() -> str:
    """Timezone-aware UTC, ISO-8601 — the format spec 3.3's `uploaded_at`/`probed_at` show."""
    return datetime.now(timezone.utc).isoformat()


# Registration of the handlers that live in other modules, at the BOTTOM of this one and
# on purpose. `_split_by_handler` asks `has_task_handler` whether a task can run, so what
# is registered decides what `plan_after_probe` enqueues — and if registration depended on
# some caller having imported the handler module first, the same patterns would schedule
# differently depending on import order. Importing them here means anyone who can see
# `TASK_HANDLERS` sees every handler that has shipped.
#
# The cycle is real and it resolves: `structure_tasks` imports names from this module, and
# by the time this line runs every one of them is defined.
from jmfts_core import embed_tasks  # noqa: E402,F401  (side effect: registration)
from jmfts_core import rollup_tasks  # noqa: E402,F401  (side effect: registration)
from jmfts_core import structure_tasks  # noqa: E402,F401  (side effect: registration)

# AFTER `structure_tasks`, and not by accident: `citation_tasks` reads that module's
# `EXTRACTION_PDF_TEXT_LAYER` and `USETYPE_CHUNK`, which is the contract between the task
# that wrote the text and the task that addresses it.
from jmfts_core import citation_tasks  # noqa: E402,F401  (side effect: registration)

# AFTER `structure_tasks` for the same kind of reason: `sheet_tasks` reads `RUNG_DECLARED`
# from it, because the rung a workbook's sheet list belongs to is the same rung a PDF's
# outline belongs to and the two must be spelled once (INGEST_SPEC.md 3.5, 8.1).
from jmfts_core import sheet_tasks  # noqa: E402,F401  (side effect: registration)

# INGEST_SPEC.md 11.5's handler. Last, and it needs nothing from the modules above it — it
# reads `documents.content` and the search tables and touches no evidence block. It is here
# rather than higher up only so the ordering above stays readable as a chain of real
# dependencies rather than a list somebody has to check.
from jmfts_core import index_tasks  # noqa: E402,F401  (side effect: registration)

# INGEST_SPEC.md 11.4's fact extraction, SPRINT_JOBS.md 15.4 S6. Like `index_tasks` above
# it needs nothing from the modules before it.
from jmfts_core import fact_tasks  # noqa: E402,F401  (side effect: registration)

# SPRINT_JOBS.md 15.4 S7's conversation rung. AFTER `structure_tasks` for the same reason
# `citation_tasks` is: it reads that module's `RUNG_DECLARED`, `USETYPE_CHUNK` and
# `EMBED_CHUNK_SPEC`, because a transcript's turns are chunks of the same kind a prose rung
# writes and the contract between the rung and `embed` must be spelled once.
from jmfts_core import conversation_tasks  # noqa: E402,F401  (side effect: registration)

# SPRINT_JOBS.md 15.4 S8's three fetchers. AFTER `structure_tasks`, whose `_scope_node`
# they share with every other handler.
from jmfts_core import fetch_tasks  # noqa: E402,F401  (side effect: registration)

# SPRINT_0_5_0.md Block A step 2's validator. Last, and it needs nothing from the modules
# above it: it reads a shape binding, the triples its scope resolves to, and the node the
# request minted to hold the report. It touches no evidence block and no ingest rung.
from jmfts_core import validate_tasks  # noqa: E402,F401  (side effect: registration)

# OFFICE_SPEC.md Part 6's two tier-3 handlers. Last, and they need nothing from the modules
# above: both read the uploaded bytes and the `matched` block, and hand them to
# `jmfts_core.convert`, which imports no task module at all — deliberately, so that the seam
# to LibreOffice is importable by a process that registers no handlers.
from jmfts_core import render_tasks  # noqa: E402,F401  (side effect: registration)

# SPRINT_0_5_0.md Block B steps 6-8's rule pass, beside the validator and after it for the
# same reasons. It imports `jmfts_core.shacl_rules`, which is domain code and imports no task
# module — deliberately, because a task module that imported its sibling would make the order
# of the two lines above load-bearing.
from jmfts_core import derive_tasks  # noqa: E402,F401  (side effect: registration)
