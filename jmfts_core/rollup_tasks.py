"""Rollup: PELT segmentation and ``effective_content``. ``INGEST_SPEC.md`` 5.4 and 11.4.

Structuring turns bytes into a tree from the top down. Rollup runs the other way — it is
what the settling walk asks for when a node's whole subtree has finished — and it does two
things:

**``structure:semantic``** segments a node's children. Spec 3.5's third rung is "PELT over
``ruptures``", and this is it, moved out of Part 4's structuring table for a reason 5.4
already states: its input is the child sequence *and their embeddings*, which do not exist
until the rung above has finished and its nodes have settled. A task list that named the
tree in advance would be stale before it ran.

**``summarize``** gives a node that holds no text of its own a text embedding, from the
text of its children.

**``summarize:tree``** takes that summary and gives it a NODE, under the derived-tree root
(``derived_roots.py``, ``SPRINT_0_5_0.md`` Block C step 11), with ``summarizes`` edges down
to the members it covers. Same summary, same vector, different tree: ``summarize``'s output
is a field on a structural node and so is a property of the as-written tree, and 3.1 needs
a PARALLEL tree whose leaves resolve to the source leaves through edges. It owns nothing —
the members keep their parent and their access — which is the property Part 1 is about and
which step 12 restored by deleting ``raptor_summarize``'s reparent.

**PELT is not RAPTOR, and the difference is the whole design.** RAPTOR clusters: a cluster
is a SET, so a node built from one holds material from wherever in the document it happened
to be. PELT segments: the children are a SEQUENCE in the order the author wrote them, and a
segment is a contiguous run of siblings. So a structural node here is a SPAN of the
document and the tree reads the way the document reads. Ingestion is the only moment at
which that order is still available for free; nothing downstream can recover an order
discarded here. RAPTOR over an ingested tree remains wanted and is a different operation.

**Represent before interpreting.** A node's embedding comes from concatenating its
children's text in document order while that fits the embedding window, and from an LLM
summary only when it does not. Concatenated length grows quickly walking upward, so
summarization becomes unavoidable a few levels up — the rule is only that it does not
happen before then. A span that was concatenated is a stronger record of what the document
said than any paraphrase of it, and the deciding token count is recorded either way so a
reader can tell the two apart without inferring it from the text.

**Nothing here writes ``content``.** A structural node's prose lives in its leaves. Putting
it on the container as well would enter the same text into the full-text and vector indexes
twice and answer one query with both. The summary lives in
the ``effective_content`` evidence row, which the full-text index does not read, and
the node gets a document vector and no token embeddings — MaxSim over text that is not this
node's own content would be the same double-count by another route.

That rule is about the STRUCTURAL tree, and ``summarize:tree`` obeys it rather than
escaping it: the node it creates in the derived tree carries ``content`` only when the
summary is an LLM paraphrase, which is text that exists nowhere else, and carries none when
the summary was a concatenation of prose the leaves already hold. Both carry the vector,
because the vector is what the summary IS for retrieval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from jmfts_core.atoms import (
    COST_CPU,
    COST_LLM,
    COST_MODEL,
    EV_EFFECTIVE_CONTENT,
    EV_EMBEDDING,
    EV_STRUCTURE,
    EV_TEXT,
    KEY_POSITION,
    ChildKey,
    Fanout,
)
from jmfts_core.config import Settings, get_settings
from jmfts_client.contracts.attempt import param_fingerprint
from jmfts_core.derived_roots import (
    DERIVED_ROOT_USETYPE,
    SUMMARY_TREE_KIND,
    get_or_create_derived_root,
    widening_descendants,
)
from jmfts_core.embedder import get_embedder
from jmfts_core.embedding import get_embedding_service
from jmfts_core.entity_roots import ENTITIES_ROOT_USETYPE
from jmfts_core.ingest_options import resolve_options
from jmfts_core.ingest_tasks import (
    OPTIONS_KEY,
    TASK_STRUCTURE_SEMANTIC,
    TASK_SUMMARIZE,
    TASK_SUMMARIZE_LLM,
    TASK_SUMMARIZE_TREE,
    TaskOutcome,
    register_task_handler,
)
from jmfts_core.llm_client import complete_sync
from jmfts_core.models.document import (
    SETTLED_IN_FLIGHT,
    SUMMARIZES_LINK_TYPE,
    USETYPE_SEGMENT,
    USETYPE_SUMMARY,
    Document,
    DocumentLink,
)
from jmfts_core.models.task_queue import WRITE_SELF, WRITE_SUBTREE, TaskQueue
from jmfts_core.evidence import ABSENT
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import (
    EvidenceRepository,
    evidence_of,
    evidence_value,
)
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.segmentation import Segment, enforce_segment_bounds, pelt_segment
from jmfts_core.settling import TaskSpec, enqueue_batch

logger = logging.getLogger(__name__)

#: Spec 3.5's rung name for a boundary PELT found, written into the nodes it creates.
RUNG_SEMANTIC = "semantic"

#: What produced those boundaries. Named beside the rung for the same reason the structure
#: rungs name theirs: the rung says how good the evidence is, the source says where it came
#: from, and only the second one tells a person why a tree came out the shape it did.
SOURCE_PELT = "pelt_changepoints"

# The usetype of a node PELT created is `USETYPE_SEGMENT`, and it is DEFINED on the model
# with every other ingest usetype — Part 4's rule table names node kinds and cannot import
# this module. See `jmfts_core.models.document`.

#: How ``effective_content`` was produced. ``concatenated`` is the preferred outcome and
#: the one that involves no interpretation at all.
METHOD_CONCATENATED = "concatenated"
METHOD_LLM_SUMMARY = "llm_summary"

#: The prefix the embedding service expects for stored text, matching what
#: ``DocumentRepository.embed_document`` uses. Spelled here because this module embeds
#: text that is not any node's ``content`` and so cannot go through that method.
EMBED_PREFIX = "search_document: "

#: The usetypes of a tree root that is NOT an ingest tree. A node whose tree is rooted in
#: one of these is not material somebody uploaded; it is a container a derivation minted,
#: keyed by access, and holding nodes that have nothing to do with one another beyond being
#: readable by the same principals. See :func:`in_ingest_tree`.
#:
#: BOTH, not just the derived root, because the hazard is the shape and not the tree kind:
#: ``entity_roots.get_or_create_entities_root`` mints the same thing one release earlier —
#: ``parent_id=None``, contentless, children keyed only by access — and a summary of the
#: entities a corpus mentions is the same unasked-for LLM call as a summary of its summaries.
NON_INGEST_ROOT_USETYPES: frozenset[str] = frozenset({DERIVED_ROOT_USETYPE, ENTITIES_ROOT_USETYPE})

SUMMARIZE_SYSTEM_PROMPT = (
    "You summarize one contiguous span of a document. The passages you are given are "
    "consecutive and in the order the author wrote them, so preserve that order and the "
    "narrative it carries. Write a single factual summary of what this span says. Do not "
    "add opinions, do not add knowledge from outside the passages, and do not describe "
    "the passages as passages."
)


# ---------------------------------------------------------------------------
# The planner — spec 5.4 step 4
# ---------------------------------------------------------------------------


class IngestRollupPlanner:
    """What becomes eligible for a node once its whole subtree has settled.

    Called by :func:`~jmfts_core.settling.settle_node` under the node's row lock, at the
    exact instant the rollup's inputs are complete. It returns at most ONE task, and the
    walk brings it back after that task finishes — which is what makes 11.4's two
    recursion directions the same mechanism instead of two:

    * *downward* — segmentation creates containers, each carrying its own ``summarize``;
      a container that is itself too wide segments again on its own walk;
    * *upward* — the containers settle, the walk returns here, and this node now has a
      different number of children. If that is still too many it segments THOSE, one level
      up, over nodes that did not exist when the first pass ran.

    **NOTHING AT ALL FOR A NODE OUTSIDE AN INGEST TREE**, checked first and before any
    rung. :func:`in_ingest_tree` is the predicate and ``SPRINT_0_5_0.md`` Block C finding 2
    is why: the walk visits every ancestor of a completed task's scope node, a derived root
    is an ancestor like any other, and a root's children are unrelated to each other by
    construction. Without this the first node filed under a shared root would buy an LLM
    summarize of that root's whole contents — which is why, until it was checked, a derived
    root could not be a parent for anything a rollup did not create.

    **THREE RUNGS, IN ORDER, ONE PER VISIT.** ``structure:semantic`` while the node is too
    wide, then ``summarize`` for its ``effective_content``, then ``summarize:tree`` to hang
    that summary in the derived tree (``SPRINT_0_5_0.md`` Block C step 11). The third is
    gated on the EVIDENCE rather than on the attempt, unlike the two above it: a
    ``summarize`` that reported ``skipped`` is attempted and wrote no summary, so a queue
    row asking to hang one could only ever report ``skipped`` in turn — and a plan that
    claims work where there is none is the thing 11.2 refuses for ``EXPLAIN``.

    **6.1's diff is applied here rather than by wrapping**
    :class:`~jmfts_core.settling.AttemptDiffPlanner`, because the choice between the
    tasks depends on the diff: a node whose segmentation already ran and produced nothing
    must fall through to ``summarize`` rather than be offered the same segmentation again.
    The termination argument is otherwise identical — a completed task appends an attempt
    carrying its ``(task, param_fingerprint)`` pair, and the pair is not offered twice.

    **The params carry the measurement that triggered the task**, which is 11.4's stated
    tension with that diff and its resolution. PELT's real input is the child set, and a
    node segmented once and then given more children would be over the limit again with an
    identical fingerprint. ``child_count`` in the params moves when the input moves, and
    stays put when it does not.
    """

    def __call__(self, session: Session, node: Document) -> Sequence[TaskSpec]:
        if not in_ingest_tree(session, node):
            # NOT AN INGEST TREE, SO THERE IS NO ROLLUP TO PLAN. `SPRINT_0_5_0.md` Block C
            # finding 2. The walk reaches every ancestor of a completed task's scope node,
            # and a derived root is an ancestor like any other: its children are whatever
            # derivations have filed under that access key, so offering it `summarize`
            # enqueues an LLM call over a container of unrelated derived nodes that nobody
            # asked for. Declining here is what lets a derived root be a parent at all.
            return ()

        children = child_ids(session, node.id)
        if not children:
            # A leaf has nothing to roll up: its own text is its own content, and its
            # `embed` task — which is what brought the walk here — has already written its
            # vectors. Returning nothing is what lets the leaf settle and the walk continue
            # up to the node that DOES have children to roll up.
            return ()

        options = rollup_options(session, node)
        attempted = TaskQueueRepository(session).attempted_fingerprints(node.id)

        if len(children) > options["max_children"]:
            spec = _segment_spec(children, options)
            if (TASK_STRUCTURE_SEMANTIC, param_fingerprint(spec.params)) not in attempted:
                return (spec,)

        spec = _summarize_spec(children, options)
        if (TASK_SUMMARIZE, param_fingerprint(spec.params)) not in attempted:
            return (spec,)

        # THE THIRD RUNG, AND IT IS GATED ON THE EVIDENCE RATHER THAN ON THE ATTEMPT.
        # `summarize` above is offered until its fingerprint has been tried; this one is
        # offered only once the row that fingerprint was for actually EXISTS, because a
        # `summarize` that reported `skipped` (no children with text, or no LLM for a node
        # too wide to concatenate) is attempted and has written nothing to hang in a tree.
        # Reading the evidence is the same question `run_summarize_tree` will ask, asked
        # before a task is queued to ask it — a queue row per node whose only outcome could
        # be `skipped` is a plan that says work exists where there is none.
        if evidence_value(session, node.id, "effective_content") is None:
            return ()
        spec = _summarize_tree_spec(children)
        if (TASK_SUMMARIZE_TREE, param_fingerprint(spec.params)) not in attempted:
            return (spec,)
        return ()


def _segment_spec(children: Sequence[int], options: dict) -> TaskSpec:
    """``subtree``, not ``children``, and spec 5.3 is why.

    Segmentation reparents nodes that may have descendants of their own — the upward
    direction moves whole containers — and moving a populated node rewrites every
    descendant's ``path``. 5.3 reserves that for ``subtree``, which is the only mode that
    reserves a region, and the claim gate enforces it in SQL.
    """
    return TaskSpec(
        task_type=TASK_STRUCTURE_SEMANTIC,
        write_mode=WRITE_SUBTREE,
        params={
            "child_count": len(children),
            "penalty": options["penalty"],
            "min_segment": options["min_segment"],
        },
    )


def _summarize_spec(children: Sequence[int], options: dict) -> TaskSpec:
    """``self``: it reads descendants and writes only this node's own row (spec 5.3)."""
    return TaskSpec(
        task_type=TASK_SUMMARIZE,
        write_mode=WRITE_SELF,
        params={"child_count": len(children), "llm_model": options["llm_model"]},
    )


def _summarize_tree_spec(children: Sequence[int]) -> TaskSpec:
    """``self``, and it is the second row in the appliance where that names somewhere else.

    ``extract:facts`` is the first (``ingest_tasks.TASK_ROWS``): a task that writes into a
    shared region outside this file's subtree, declaring the narrowest reservation it can,
    because 5.3's three modes describe a region within ONE subtree and there is no mode for
    "a node under a root keyed by access". Reserving ``subtree`` would not cover the write
    either — the derived root is not below this node — it would only block every embed under
    this file while a task that touches none of them ran.

    ``child_count`` alone, and no options key: this task takes no parameters. What decides
    whether it needs to run again is the member set it links to, which is exactly what
    ``child_count`` measures (11.4's tension with 6.1's diff, resolved the same way
    :func:`_summarize_spec` resolves it). The summary TEXT changing is already covered —
    that is a new ``summarize`` attempt with its own fingerprint, and this one comes after.
    """
    return TaskSpec(
        task_type=TASK_SUMMARIZE_TREE,
        write_mode=WRITE_SELF,
        params={"child_count": len(children)},
    )


def in_ingest_tree(session: Session, node: Document) -> bool:
    """Whether ``node`` belongs to a tree an ingest built. ``SPRINT_0_5_0.md`` Block C, 2.

    **THE TREE'S ROOT DECIDES, AND ``produced_by`` CANNOT.** Finding 3 reads what the two
    rollup writers stamp — ``structure:semantic`` on a PELT container, ``summarize:tree`` on
    a derived node — which separates derived NODES from ingest ones. It does not reach the
    node the finding is about. The hazardous node is the shared ROOT: a summarize offered
    there covers every derived node filed under one access key, and that root is minted by
    ``DocumentRepository.create`` with no stamp at all, so its ``produced_by`` is NULL and
    indistinguishable from an uploaded file node's. The one thing it does carry is its
    ``usetype`` (:data:`NON_INGEST_ROOT_USETYPES`), which is also what holds it out of
    retrieval, so the same fact answers both questions.

    Reading the ROOT rather than the node is what makes the answer hold for a node filed
    under that root later — Block A's report node, a keyword tree's nodes — none of which
    the planner can recognise from a stamp it has never seen.

    The test is NEGATIVE on purpose: everything is an ingest tree unless its root says
    otherwise. A positive test — "``produced_by`` names a structure rung" — would decline to
    roll up a tree somebody built by hand through ``POST /documents``, which the planner
    rolls up today and which nothing in this finding asks to change.
    """
    path = node.path or []
    if not path:
        # Its own root. A derived root IS parentless, so this is the case that matters.
        return node.usetype not in NON_INGEST_ROOT_USETYPES
    root = session.get(Document, path[0])
    if root is None:
        # `path` names an ancestor that is not there. That is a broken tree, not a derived
        # one, and inventing an answer for it would hide the breakage — the planner's
        # ordinary checks run and the walk reports what it finds.
        return True
    return root.usetype not in NON_INGEST_ROOT_USETYPES


def child_ids(session: Session, parent_id: int) -> list[int]:
    """This node's children, in the sibling-ordering contract's order.

    ``position ASC NULLS LAST, created_at ASC, id ASC`` — document order, which is the
    input PELT segments and the order the concatenation is built in. Read directly rather
    than through ``get_children`` because that method takes a row limit, and a truncated
    child list here would segment part of a node and call it the whole thing.
    """
    return list(
        session.execute(
            select(Document.id)
            .where(Document.parent_id == parent_id)
            .order_by(
                Document.position.asc().nullslast(),
                Document.created_at.asc(),
                Document.id.asc(),
            )
        )
        .scalars()
        .all()
    )


def rollup_options(session: Session, node: Document) -> dict:
    """The ``rollup`` options this node is rolled up under.

    Read from the nearest ancestor that recorded any — the upload writes them on the file
    node, and the segments created underneath it inherit what that upload asked for rather
    than whatever the profile defaults have become since. A node with no such ancestor
    resolves to the task defaults, which is the documented meaning of "no overrides".

    The format is taken from the same ancestor's ``matched`` block, so a format profile
    that deviates applies to its rollup as well as to its structuring.
    """
    owner = _options_owner(session, node)
    owned = evidence_of(session, owner.id) if owner is not None else {}
    stored = owned.get(OPTIONS_KEY)
    fmt = (owned.get("matched") or {}).get("format", "")
    return resolve_options(fmt, stored)["rollup"]


def _options_owner(session: Session, node: Document) -> Optional[Document]:
    """``node`` itself if it records options, else the nearest ancestor that does."""
    evidence = EvidenceRepository(session)
    if evidence.read(node.id, OPTIONS_KEY) is not ABSENT:
        return node
    for ancestor_id in reversed(node.path or []):
        ancestor = session.get(Document, ancestor_id)
        if ancestor is not None and evidence.read(ancestor.id, OPTIONS_KEY):
            return ancestor
    return None


# ---------------------------------------------------------------------------
# structure:semantic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _NotSegmented:
    """Why a node stayed wide. 11.4's rule 3: that is a correct state, and it is recorded.

    Wide and unsummarized hides nothing — the node is retrievable and so are its children.
    Quietly making progress is the one outcome that would be a defect, so every path that
    declines to segment names itself here.
    """

    reason: str
    detail: dict


def _segment_ceiling(evidence: dict, params: dict) -> tuple[int, int]:
    """How many containers one PELT pass may create. ``SPRINT_JOBS.md`` 2.4 and 7.1.

    THE CEILING IS LOOSE AND 7.1 SAYS SO. ``min_segment`` bounds how few children a segment
    may hold, so ``⌊N / min_segment⌋`` is the most segments PELT can return — but a segment
    of one child becomes no container (11.4 rule 2), and a run that finds one segment
    covering everything creates nothing at all. The floor is therefore zero and the ceiling
    is reached only by a document that changes subject every ``min_segment`` children.

    That looseness is why 7.1 puts the budget rather than the bound in charge: this
    interval is context for a person reading ``EXPLAIN``, not a number to schedule against.
    """
    minimum = int(params["min_segment"])
    children = int(evidence["child_count"])
    return (0, children // minimum if minimum else children)


# `subtree`, because reparenting moves nodes that have children of their own — see
# `_write_segments`. Position is the child key: a segment container has no title, no
# content and no natural key, and its identity is entirely which span of siblings it holds.
#
# `cpu`. PELT runs over vectors this atom READS; the containers it creates carry their own
# `summarize`, and that is where the model and the LLM enter.
@register_task_handler(
    TASK_STRUCTURE_SEMANTIC,
    consumes=(f"{EV_EMBEDDING}@children", f"{EV_TEXT}@children"),
    produces=(f"{EV_STRUCTURE}@children",),
    write_mode=WRITE_SUBTREE,
    cost_class=COST_CPU,
    fanout=Fanout(
        bound=_segment_ceiling,
        reads=("child_count",),
        basis="⌊children / min_segment⌋ containers",
        counts=USETYPE_SEGMENT,
    ),
    child_key=ChildKey(KEY_POSITION),
)
def run_structure_semantic(session: Session, task: TaskQueue) -> TaskOutcome:
    """Segment this node's children at PELT's changepoints and reparent them.

    Creates one container per segment, in document order, and gives each container its own
    ``summarize``. The containers are created IN FLIGHT and carry that task, which is what
    brings the settling walk to them: the walk only travels upward, so a node created
    settled and given no work would never be visited and would never get an embedding.
    """
    doc = _scope_node(session, task)
    params = dict(task.params or {})
    children = child_ids(session, doc.id)

    refusal = _refuse_to_segment(session, children, params)
    if refusal is not None:
        return TaskOutcome(
            rung=RUNG_SEMANTIC,
            detail={"segmented": False, "reason": refusal.reason, **refusal.detail},
        )

    segments = _segments(session, children, params)
    if len(segments) <= 1:
        # 11.4 rule 1. One segment covering every child is a NON-RESULT: creating a
        # container for it would give the next walk the same children under a new parent,
        # which segments the same way, and the tree extends forever as a linked list with
        # an LLM call per level.
        return TaskOutcome(
            rung=RUNG_SEMANTIC,
            detail={
                "segmented": False,
                "reason": (
                    "PELT found no changepoint: one segment covers every child, and a "
                    "container for it would deepen the tree without narrowing it"
                ),
                "children": len(children),
                "segments": len(segments),
            },
        )

    return _write_segments(session, doc, children, segments, params)


def _refuse_to_segment(
    session: Session, children: Sequence[int], params: dict
) -> Optional[_NotSegmented]:
    """The conditions under which there is nothing to segment, each with its reason."""
    if len(children) < 2:
        return _NotSegmented(
            reason="a node with fewer than two children has no sequence to segment",
            detail={"children": len(children)},
        )

    unembedded = [
        row
        for row in session.execute(
            select(Document.id).where(Document.id.in_(children)).where(Document.embed.is_(None))
        ).scalars()
    ]
    if unembedded:
        # The honest limit of this pass, recorded rather than worked around. Section
        # containers written by the structure rungs settle at creation and are never
        # embedded, so a file whose children are sections cannot be segmented yet.
        # Segmenting the embedded subset would produce a tree over a different set of
        # children from the one the node actually has.
        return _NotSegmented(
            reason=(
                "the child sequence is not fully embedded, and segmenting the embedded "
                "subset would build a tree over a different set of children than this "
                "node has"
            ),
            detail={"children": len(children), "unembedded": len(unembedded)},
        )
    return None


def _segments(session: Session, children: Sequence[int], params: dict) -> list[Segment]:
    """PELT over the child embedding sequence. Every boundary here is a changepoint.

    ``jump=1`` because the default grid of 5 makes a short sequence unsegmentable
    regardless of how sharp the change in it is — with six children there is exactly one
    candidate position, and ``min_size`` rules it out.

    ``enforce_segment_bounds`` is called for its MERGE phase only, and its split phase is
    disabled by handing it a maximum no segment can exceed. That phase divides an oversized
    segment into equal parts, which would put a boundary where the document has none —
    and a run with no changepoint in it that came back as two segments would be 11.4's
    rule 1 defeated by arithmetic. An oversized segment is handled by recursion instead:
    it becomes a container, and its own walk segments it again.
    """
    rows = {
        doc_id: embed
        for doc_id, embed in session.execute(
            select(Document.id, Document.embed).where(Document.id.in_(children))
        ).all()
    }
    embeddings = np.array([np.asarray(rows[doc_id], dtype=np.float32) for doc_id in children])

    segments = pelt_segment(
        embeddings,
        list(children),
        penalty=float(params["penalty"]),
        min_size=int(params["min_segment"]),
        jump=1,
    )
    return enforce_segment_bounds(
        segments,
        min_segment=int(params["min_segment"]),
        max_segment=len(children),
    )


def _write_segments(
    session: Session,
    doc: Document,
    children: Sequence[int],
    segments: Sequence[Segment],
    params: dict,
) -> TaskOutcome:
    """Create a container per multi-child segment, reparent into it, and re-order."""
    repo = DocumentRepository(session)
    tasks = TaskQueueRepository(session)
    planner = IngestRollupPlanner()

    #: The parent's children after the move, in document order: a container where one was
    #: made, the child itself where it was left alone.
    remaining: list[int] = []
    created: list[int] = []

    for index, segment in enumerate(segments):
        if len(segment.child_ids) < 2:
            # 11.4 rule 2. One child under a new node adds a level and no information.
            remaining.extend(segment.child_ids)
            continue

        container = repo.create(
            # No title. The document does not name this span, and naming it would be the
            # interpretation this rung exists to postpone — `summarize` writes what the
            # span says, and the ordering is what says where it is.
            title=None,
            content=None,
            parent_id=doc.id,
            usetype=USETYPE_SEGMENT,
            # 4.2's stamp. No rule is scoped to a segment's children today — a container's
            # first task comes from the rollup planner, which asks the tree rather than the
            # table — so nothing reads this yet. It is written anyway, because a node with
            # no stamp means ASSERTED, and claiming a PELT container was written by a person
            # would be a false statement about provenance rather than a missing one.
            produced_by=TASK_STRUCTURE_SEMANTIC,
            evidence={
                "structure": {
                    "primary_rung": RUNG_SEMANTIC,
                    "source": SOURCE_PELT,
                    "segment_index": index,
                    "segment_count": len(segments),
                    "child_count": len(segment.child_ids),
                }
            },
            auto_embed=False,
            sequential=True,
            settled=SETTLED_IN_FLIGHT,
        )
        for child_id in segment.child_ids:
            # `childless_only=False`: the upward direction moves containers that have
            # children of their own, which is why this task declares `subtree` (5.3).
            repo.reparent(child_id, container.id)
        session.flush()

        # The container's own first task comes from the SAME planner the walk would have
        # asked, rather than a hardcoded `summarize`. A container that is itself too wide
        # must segment before it summarizes: summarizing first would concatenate forty
        # children — an LLM call, at that size — and then be asked to do it again over the
        # five containers that replaced them.
        specs = planner(session, container)
        if not specs:
            raise ValueError(
                f"the rollup planner offered nothing to segment container {container.id}, "
                "which holds children and no attempts; it would never settle"
            )
        enqueue_batch(tasks, container.id, specs)
        created.append(container.id)
        remaining.append(container.id)

    if not created:
        # Every segment held one child — 11.4's maximum-fragmentation shape, absorbed by
        # rule 2 into no change at all.
        return TaskOutcome(
            rung=RUNG_SEMANTIC,
            detail={
                "segmented": False,
                "reason": (
                    "every segment held a single child, and a container per child adds a "
                    "level and no information"
                ),
                "children": len(children),
                "segments": len(segments),
            },
        )

    _reorder(session, remaining)
    session.flush()

    return TaskOutcome(
        rung=RUNG_SEMANTIC,
        detail={
            "segmented": True,
            "children_before": len(children),
            "children_after": len(remaining),
            "segments": len(segments),
            "containers": len(created),
            "sizes": [len(s.child_ids) for s in segments],
            "params": {
                "penalty": params["penalty"],
                "min_segment": params["min_segment"],
            },
        },
        produced={"node_count": len(created), "child_ids": created},
    )


def _reorder(session: Session, node_ids: Sequence[int]) -> None:
    """Renumber a sibling group so it reads in document order.

    Necessary because the two halves of the move number themselves independently: a new
    container is appended to the tail of the sibling group, while a child left in place by
    rule 2 keeps the position it already had. Without this a document whose second segment
    was containerised and whose first was not would come back with the second span first.
    """
    for position, node_id in enumerate(node_ids):
        node = session.get(Document, node_id)
        if node is not None:
            node.position = position


# ---------------------------------------------------------------------------
# summarize — effective_content
# ---------------------------------------------------------------------------


# 2.2's worked example, declared. `effective_content@children` is NOT consumed here — this
# reads the children's `text`, which for a container child is the summary that its own
# `summarize` embedded. Producing `effective_content@self` while consuming `text@children`
# is what gives the rollup no self-edge at all, and it is why the acyclicity check needs no
# exception for the one shape every rollup in the appliance has.
#
# `model`, not `cpu`: `store_effective_content` embeds the concatenation. The tokenizer
# calls above it are the cheap half and they are not what the class has to be sized for.
@register_task_handler(
    TASK_SUMMARIZE,
    consumes=(f"{EV_TEXT}@children",),
    produces=(f"{EV_EFFECTIVE_CONTENT}@self", f"{EV_EMBEDDING}@self"),
    write_mode=WRITE_SELF,
    cost_class=COST_MODEL,
)
def run_summarize(session: Session, task: TaskQueue) -> TaskOutcome:
    """Give this node a text embedding derived from its children. 11.4's ``effective_content``.

    Concatenate while the result fits the embedding window; summarize with an LLM only when
    it does not. The deciding token count goes into the attempt detail either way, because
    "this node was concatenated" and "this node was paraphrased" are different facts about
    how much interpretation stands between a query and the document.

    A node with no LLM configured and text too long to concatenate records ``skipped`` with
    the reason (spec 3.4, 11.4 §4). A missing summary is never silent.
    """
    doc = _scope_node(session, task)
    children = child_ids(session, doc.id)
    if not children:
        return TaskOutcome(
            status="skipped",
            detail={"reason": "the node has no children, so there is nothing to roll up"},
        )

    text = effective_text(session, doc.id, own_content=False)
    if not text.strip():
        return TaskOutcome(
            status="skipped",
            detail={
                "reason": "no child of this node carries any text",
                "children": len(children),
            },
        )

    service = get_embedding_service()
    fit = service.check_fit(text, with_tokens=False, prefix=EMBED_PREFIX)

    if not fit.truncated:
        return store_effective_content(
            session,
            doc,
            embed_text=text,
            method=METHOD_CONCATENATED,
            children=len(children),
            detail={"tokens": fit.token_count, "window": fit.limit, "characters": len(text)},
        )

    # PAST HERE AN LLM IS REQUIRED, and this handler does not call one. It hands the node
    # to `summarize:llm`, which carries its own badge and therefore its own pool — see
    # TASK_SUMMARIZE_LLM for why the split exists at all.
    #
    # The check-and-defer is NOT a scheduling decision that could have been made at enqueue
    # time. `fit.truncated` is a fact about this node's children as they are right now, and
    # the only way to learn it is to concatenate them and tokenise, which is what this task
    # just did. Deferring is the point: the expensive pool is asked for only after the work
    # is known to need it.
    deferred = enqueue_batch(
        TaskQueueRepository(session),
        doc.id,
        [
            TaskSpec(
                task_type=TASK_SUMMARIZE_LLM,
                write_mode=WRITE_SELF,
                params=dict(task.params or {}),
                # Inherit the deferring task's priority so a tree that was pushed to the
                # front of the queue does not fall back to the default when it crosses into
                # the LLM pool. getattr because a handler is also driven directly in tests
                # with a stand-in row that carries only what handlers read.
                priority=getattr(task, "priority", 0) or 0,
            )
        ],
    )
    return TaskOutcome(
        detail={
            "deferred_to": TASK_SUMMARIZE_LLM,
            "deferred_task_ids": list(deferred),
            "reason": (
                "the concatenated children do not fit the embedding window, so this node "
                "needs an LLM summary"
            ),
            "tokens": fit.token_count,
            "window": fit.limit,
            "children": len(children),
        },
    )


# The SAME declaration as `summarize` except the cost class, and 11.1 is what that means:
# the handoff is not a dependency and needs no edge. Two atoms produce
# `effective_content@self` on one node, exactly as two structure rungs produce the chunks
# `citation` reads — a fact about the produces map, which 2.3 says `after_any` was only
# ever spelling out by hand.
#
# `llm`, and the class is the most expensive thing the atom does: this calls the endpoint
# AND embeds what came back. Sizing it as `model` would put an LLM call in the embedding
# pool's budget, which is the split `TASK_SUMMARIZE_LLM` exists to prevent.
@register_task_handler(
    TASK_SUMMARIZE_LLM,
    consumes=(f"{EV_TEXT}@children",),
    produces=(f"{EV_EFFECTIVE_CONTENT}@self", f"{EV_EMBEDDING}@self"),
    write_mode=WRITE_SELF,
    cost_class=COST_LLM,
)
def run_summarize_llm(session: Session, task: TaskQueue) -> TaskOutcome:
    """Summarize this node's children with an LLM, then embed the summary.

    Reached only from :func:`run_summarize`, which has already established that the
    concatenation does not fit. It re-derives the text rather than receiving it on the task
    row: the children may have changed between the two tasks, and a summary of a stale
    concatenation would be a quiet wrong answer where re-deriving is one cheap walk.

    A node whose text is too long to concatenate and that has NO LLM configured records
    ``skipped`` with the reason (spec 3.4, 11.4 §4). A missing summary is never silent.
    """
    doc = _scope_node(session, task)
    children = child_ids(session, doc.id)
    text = effective_text(session, doc.id, own_content=False)
    service = get_embedding_service()
    fit = service.check_fit(text, with_tokens=False, prefix=EMBED_PREFIX)

    if not fit.truncated:
        # The children shrank between the deferral and now. Concatenating is strictly
        # better than paraphrasing — fewer layers of interpretation between a query and the
        # document — so take it rather than calling an LLM to undo a change.
        return store_effective_content(
            session,
            doc,
            embed_text=text,
            method=METHOD_CONCATENATED,
            children=len(children),
            detail={
                "tokens": fit.token_count,
                "window": fit.limit,
                "characters": len(text),
                "note": "the node fit the window by the time the LLM task ran",
            },
        )

    settings = get_settings()
    if not settings.effective_llm_url:
        return TaskOutcome(
            status="skipped",
            detail={
                "reason": (
                    "the concatenated children do not fit the embedding window and no LLM "
                    "is configured to summarize them"
                ),
                "tokens": fit.token_count,
                "window": fit.limit,
                "children": len(children),
            },
        )

    model = (task.params or {}).get("llm_model") or settings.effective_llm_model
    summary = summarize_span(text, settings, model)
    summary_fit = service.check_fit(summary, with_tokens=False, prefix=EMBED_PREFIX)
    if summary_fit.truncated:
        # The model returned something longer than the window it was called to get under.
        # Raising is right: embedding it is impossible and storing it unembedded would
        # leave a node claiming an `effective_content` that nothing can retrieve.
        raise ValueError(
            f"the summary of document {doc.id} is {summary_fit.token_count} tokens, over "
            f"the {summary_fit.limit}-token embedding window; the model did not summarize"
        )

    return store_effective_content(
        session,
        doc,
        embed_text=summary,
        method=METHOD_LLM_SUMMARY,
        children=len(children),
        text=summary,
        detail={
            "tokens": summary_fit.token_count,
            "window": summary_fit.limit,
            "input_tokens": fit.token_count,
            "input_characters": len(text),
            "model": model,
        },
    )


def effective_text(session: Session, node_id: int, *, own_content: bool = True) -> str:
    """The text this node stands for, in document order.

    A node's own ``content`` when it has one; its stored summary when it has one; otherwise
    its children's effective text joined in order. Recursive, and it terminates at the
    leaves, which always have content.

    Computed rather than stored. A concatenation is derivable from the subtree it came
    from, and storing it at every level would duplicate the whole document once per level
    for no fact that could not be recomputed — where a summary is NOT derivable and is
    therefore the one thing that is written down.

    ``own_content=False`` for the node being summarized, because a node that already has
    content is not asking what its children say.
    """
    node = session.get(Document, node_id)
    if node is None:
        return ""
    if own_content and node.content:
        return node.content
    summary = (evidence_value(session, node_id, "effective_content") or {}).get("text")
    if summary:
        return summary
    parts = [effective_text(session, child) for child in child_ids(session, node_id)]
    return "\n\n".join(part for part in parts if part.strip())


def store_effective_content(
    session: Session,
    doc: Document,
    *,
    embed_text: str,
    method: str,
    children: int,
    detail: dict,
    text: Optional[str] = None,
) -> TaskOutcome:
    """Embed ``embed_text`` onto the node and record how the text was arrived at.

    PUBLIC BECAUSE A SUMMARY CAN ARRIVE HOURS AFTER THE TASK THAT ASKED FOR IT. The
    reference batch worker (``jmfts_batch/``) submits a node's text to an external batch
    provider, is marked ``batched``, and applies the returned summary in a later process.
    That path must write ``effective_content`` the same way this module does — same
    embedding, same prefix, same record shape — so the definition lives here and has one
    caller-visible name rather than being copied into the worker.

    The DOCUMENT vector only. Token embeddings would put this text into the MaxSim index
    under a node whose ``content`` it is not, which is the same double-count that keeps
    ``content`` off a container in the first place.

    ``text`` is stored only for a summary. It lives in the ``effective_content`` evidence
    row, which the full-text index does not read (``to_tsvector(title || content)``), so a
    summary cannot skew the BM25 statistics of the corpus it summarizes.
    """
    # `get_embedder`, like the other ingest write: a worker pointed at a runner summarizes
    # locally and embeds the summary remotely. The `check_fit` calls above stay on the
    # local service, which answers them from the tokenizer alone.
    embedder = get_embedder()
    doc.embed = embedder.embed_text(embed_text, prefix=EMBED_PREFIX).tolist()

    record = {"method": method, "source_children": children, **detail}
    if text is not None:
        record["text"] = text
    EvidenceRepository(session).write(doc.id, "effective_content", record)
    session.flush()

    return TaskOutcome(detail={"method": method, "source_children": children, **detail})


def summarize_span(text: str, settings: Settings, model: str) -> str:
    """One LLM call over a contiguous span, in document order.

    Deliberately not ``summarization._llm_summarize``. That one is written for a RAPTOR
    cluster — its prompt says the passages "belong to the same topic cluster", which is
    the wrong instruction for a span whose order carries meaning — and it silently
    truncates its input to a character budget, which would produce a summary of the first
    part of a span and label it a summary of the span.

    The full text is sent. If it is over the model's context the server refuses, the
    worker classifies that refusal and records it, and the answer is to segment further —
    not to send less and call the result a summary.

    A module-level function so a test can replace it without a live model.

    Synchronous, and the only one of JMFTS's four LLM call sites that is: it runs inside
    the ingest worker, which is a plain thread driving synchronous task handlers. τ's
    client is async-only, so this goes through ``llm_client.complete_sync``, which owns an
    event loop for the duration of the call and closes τ's provider pool before tearing it
    down. See that function for why nesting is refused rather than accommodated.

    The bearer is now always sent — ``JMFTS_LLM_API_KEY`` when set, τ's ``"not-needed"``
    sentinel when not. It used to be omitted entirely in the second case. A llama-server
    on the LAN that wants no auth ignores the header either way, and the same worker image
    still serves a local model and a metered API without a code path for each.
    """
    base_url, _ = settings.require_llm("Span summarization", model)

    extra_body = {}
    if settings.summarization_disable_thinking:
        extra_body["chat_template_kwargs"] = {"enable_thinking": False}

    result = complete_sync(
        settings=settings,
        base_url=base_url,
        model=model,
        messages=[
            {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        max_tokens=settings.raptor_max_summary_tokens,
        temperature=settings.summarization_temperature,
        extra_body=extra_body,
    )
    return result.text


# ---------------------------------------------------------------------------
# summarize:tree — the same summary, as a node in a parallel tree
# ---------------------------------------------------------------------------

#: Where a derived summary node records the source node it stands for, in
#: ``structured_content``. It is not a link, and that is the distinction: the ``summarizes``
#: edges point at the MEMBERS this summary covers, while this names the one node in the
#: source tree whose ``effective_content`` it is. Recovering it from the members would mean
#: reading a member's ``parent_id``, which is a fact about the source tree that a derivation
#: must not depend on staying put.
SOURCE_NODE_KEY = "source_node_id"


# `effective_content@self` AND `embedding@self`: this reads the summary row `summarize`
# wrote and the vector `summarize` computed for it, and both come from that one atom. The
# pair derives the within-node edge `summarize -> summarize:tree` (2.2), which is the real
# ordering — `IngestRollupPlanner` will not offer this until the evidence exists.
#
# `produces=()`, for `extract:facts`' and `index:bm25`' reason (`fact_tasks.py:73`): what
# this writes is a NODE UNDER ANOTHER ROOT and its edges, none of which is evidence on a
# node of this tree, and an atom claiming otherwise would enter the audit's derivation under
# a key nobody could read back.
#
# ---------------------------------------------------------------------------------------
# A DEBT, TAKEN DELIBERATELY: `SPRINT_0_5_0.md` OPEN QUESTION 6.1, AND ITS RECORDED DEFAULT.
#
# `Fact.locus` admits `self`, `children`, `subtree` and `ancestor` — every one a position
# RELATIVE TO THE NODE IN FRONT OF THE PLANNER. This handler writes a node under the derived
# root, which is none of them: not this node, not below it, not above it. 6.1 asks whether
# the vocabulary grows a fifth locus; its recorded default is that the handler declares
# `self` and step 11 writes the incompleteness down. That default is taken here, and the
# same debt is written into `docs/INGEST_SPEC.md` 5.3 where a reader meets the locus
# vocabulary rather than only here where they meet one instance of it.
#
# WHAT THE DEBT COSTS, precisely: `EXPLAIN` reports this atom as touching one node, and
# `derive_edges` derives no edge into whatever later reads the derived tree, because the
# fact it produces has no name. A fifth locus added without the `SPRINT_JOBS.md` phase that
# owns `EXPLAIN` would be a vocabulary term nothing reports on, which is the more expensive
# half of the trade — so the cheaper one is taken and stated rather than taken and hidden.
# ---------------------------------------------------------------------------------------
#
# `cpu`, and it is the only rollup atom that is. Nothing here runs a model: the vector is
# COPIED from the node `summarize` embedded, over exactly this text, so recomputing it would
# spend a forward pass to arrive at the same numbers. A badge sized on this being `model`
# would pin a node-and-two-inserts task to the GPU pool.
@register_task_handler(
    TASK_SUMMARIZE_TREE,
    consumes=(f"{EV_EFFECTIVE_CONTENT}@self", f"{EV_EMBEDDING}@self"),
    produces=(),
    write_mode=WRITE_SELF,
    cost_class=COST_CPU,
)
def run_summarize_tree(session: Session, task: TaskQueue) -> TaskOutcome:
    """Hang this node's summary under the derived root as a node, linked down to its members.

    ``SPRINT_0_5_0.md`` Block C step 11. ``summarize`` has already decided what this node's
    text is and embedded it; this gives that summary a node of its own in the summary tree,
    so that 3.1's leaf projection has something to project FROM.

    **It owns nothing.** The members keep their parent, their ``path`` and the access-control
    root they were ingested under; the only record of the relation is a ``summarizes`` edge,
    which is many-to-many and which Part 1 is entirely about. That is the property step 12
    restored for ``raptor_summarize`` and this handler is the first thing built to rely on it.

    **It re-derives rather than accumulates.** A second run over a changed member set finds
    the node it wrote before — by :data:`SOURCE_NODE_KEY`, not by title — rewrites it, and
    replaces its outgoing ``summarizes`` edges wholesale. Two summary nodes for one source
    node would make the projection ambiguous and neither copy wrong.

    **The derived node is flat under the root, and the nesting is in the edges.** A summary
    of a container cannot be created under the summary of that container's PARENT, because
    the walk rolls up from the leaves and the parent's summary does not exist yet; building
    it would mean reparenting the child's summary afterwards, which is the one move this
    whole block exists to stop. So the derived tree's shape is carried by ``summarizes``
    edges — one level down into the SOURCE tree per derived node — and composing them is
    what reaches the leaves.
    """
    doc = _scope_node(session, task)
    members = child_ids(session, doc.id)
    if not members:
        return TaskOutcome(
            status="skipped",
            detail={"reason": "the node has no children, so there is nothing to summarise"},
        )

    record = evidence_value(session, doc.id, "effective_content")
    if not record:
        # `summarize` reported `skipped` — no child carried text, or the node was too wide
        # to concatenate with no LLM configured. There is no summary to hang, and inventing
        # one here would be a second summarizer with none of `summarize`'s fit checks.
        return TaskOutcome(
            status="skipped",
            detail={
                "reason": (
                    "this node has no effective_content, so summarize wrote no summary to "
                    "give a node of its own"
                ),
                "children": len(members),
            },
        )
    if doc.embed is None:
        return TaskOutcome(
            status="skipped",
            detail={
                "reason": (
                    "this node has effective_content but no document vector, so there is "
                    "nothing to copy onto the derived node and no way to retrieve it"
                ),
                "method": record.get("method"),
            },
        )

    # THE ACCESS GATE, AND IT COMES BEFORE THE ROOT IS MINTED. A derived root is keyed by
    # ONE document's effective access; this summary is of everything below that document.
    # Where the two disagree in the widening direction, writing the node would publish a
    # summary of material its readers may not read — SPRINT_0_3_0.md 13.9 by a fourth route,
    # and `derived_roots.widening_descendants` is the check. Refusing is the whole
    # protection: there is no narrower root to fall back to, because the key that would be
    # correct (the intersection of every member's readers) is not any document's key, and
    # minting a root for it is a design this step does not open.
    widened = widening_descendants(session, doc.id)
    if widened:
        return TaskOutcome(
            status="skipped",
            detail={
                "reason": (
                    "this node's access is wider than that of documents below it, so a "
                    "summary node keyed by this node would be readable by principals who "
                    "may not read what it summarises"
                ),
                "restricted_below": widened[:20],
                "restricted_count": len(widened),
            },
        )

    root_id = get_or_create_derived_root(session, doc.id, SUMMARY_TREE_KIND)
    node = _derived_node_for(session, root_id, doc.id)
    created = node is None
    label = doc.title or f"document {doc.id}"
    # CONTENT ONLY WHEN THE SUMMARY IS NEW TEXT, which is the module docstring's rule
    # applied one node over. `store_effective_content` stores `text` for an LLM summary and
    # not for a concatenation, because a concatenation is the children's own prose and
    # writing it here would enter the same text into the full-text index twice under a node
    # whose content it is not. The vector below stands for that text either way.
    content = record.get("text")
    structured = {
        SOURCE_NODE_KEY: doc.id,
        "tree_kind": SUMMARY_TREE_KIND,
        "method": record.get("method"),
        "member_ids": list(members),
        "member_count": len(members),
    }

    if created:
        node = DocumentRepository(session).create(
            title=f"Summary of {label}",
            content=content,
            parent_id=root_id,
            usetype=USETYPE_SUMMARY,
            structured_content=structured,
            # The vector is copied below. `auto_embed=True` would run the model over the
            # summary text a second time — and over NOTHING at all in the concatenated
            # case, where this node has no content of its own.
            auto_embed=False,
            produced_by=TASK_SUMMARIZE_TREE,
        )
    else:
        node.title = f"Summary of {label}"
        node.content = content
        node.structured_content = structured
    node.embed = list(doc.embed)
    session.flush()

    _rewrite_member_links(session, node.id, members)
    session.flush()

    return TaskOutcome(
        detail={
            "derived_root_id": root_id,
            "derived_node_id": node.id,
            "created": created,
            "tree_kind": SUMMARY_TREE_KIND,
            "method": record.get("method"),
            "members": len(members),
            "has_content": content is not None,
        }
    )


def _derived_node_for(session: Session, root_id: int, source_node_id: int) -> Optional[Document]:
    """The summary node already standing for ``source_node_id`` under ``root_id``, if any.

    ``one_or_none``, so two nodes for one source raise rather than resolve to whichever the
    index returned first. That state is not survivable by picking one: the two carry
    different member sets, and a projection that reads either is reading half a tree.
    """
    return session.execute(
        select(Document).where(
            Document.parent_id == root_id,
            Document.structured_content[SOURCE_NODE_KEY].astext == str(source_node_id),
        )
    ).scalar_one_or_none()


def _rewrite_member_links(session: Session, node_id: int, members: Sequence[int]) -> None:
    """Replace this derived node's ``summarizes`` edges with one per member, in order.

    Delete-then-insert, which is Block D step 16's discipline for a derived edge scoped the
    way this handler can scope it: ``DocumentRepository.rederive_links`` deletes everything
    a RULE produced, and this rule produces edges for every node in the store, so a rebuild
    of one node's edges through it would delete every other node's. Scoping the delete to
    this derived node's own outgoing edges is complete for the same reason — nothing else
    writes an edge out of a node this handler created.

    ``derived_by`` IS stamped, and this is the first writer of migration 019's column. The
    edges are a rule's output and a re-run does delete and rebuild them, which is the exact
    fact the column was added to record.

    ``position`` in the metadata because the members are in document order (:func:`child_ids`)
    and the module docstring's point about order applies here too: ingestion is the last
    moment at which it is free, and an edge set is unordered.
    """
    session.execute(
        delete(DocumentLink).where(
            DocumentLink.source_id == node_id,
            DocumentLink.link_type == SUMMARIZES_LINK_TYPE,
        )
    )
    repo = DocumentRepository(session)
    for position, member_id in enumerate(members):
        link = repo.create_link(
            source_id=node_id,
            target_id=member_id,
            link_type=SUMMARIZES_LINK_TYPE,
            metadata={"position": position},
        )
        link.derived_by = TASK_SUMMARIZE_TREE


def _scope_node(session: Session, task: TaskQueue) -> Document:
    doc = session.get(Document, task.scope_document_id)
    if doc is None:
        raise ValueError(
            f"{task.task_type} is scoped to document {task.scope_document_id}, which does "
            "not exist"
        )
    return doc
