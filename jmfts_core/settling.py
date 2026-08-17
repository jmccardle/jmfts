"""The settling walk. ``INGEST_SPEC.md`` Part 5.4.

The rollup structure cannot be planned in advance. A task list that names the tree as it
currently stands goes stale the moment a child is added, removed, or moved — so there
are no sentinel tasks here and no precomputed cross-level dependency arrays. Each task,
when it finishes, walks one step up and **re-reads the tree**. The ``dependencies`` array
on ``task_queue`` still orders tasks *within* one node, where the set is known when they
are enqueued (5.5); it is only the cross-level rollup that is late-bound.

The spec's seven steps, and where each lives:

1. mark the task complete and append its attempt record —
   :meth:`jmfts_core.repositories.task_queue.TaskQueueRepository.complete`, in the
   worker's transaction, committed before the walk starts;
2. read ``parent_id`` fresh and take ``SELECT ... FOR UPDATE`` on that row —
   :func:`settle_node`, whose ``SELECT`` carries ``populate_existing`` so a stale copy
   in the session's identity map cannot answer for the locked row;
3. ask whether that node has any unfinished task and whether every *current* child is
   settled — :func:`settle_node`;
4. if work is eligible that was not before, enqueue it; the node is then not settled,
   because it has a pending task — :func:`settle_node`, same transaction as step 5;
5. otherwise, if both conditions hold, mark it settled — :func:`settle_node`;
6. commit, releasing the row — :func:`settle_walk`, which owns one session per level;
7. if it settled, repeat for its parent **in a new transaction** — :func:`settle_walk`.

Why each piece, restated because the code cannot be read correctly without it:

* **The row lock serialises step 3.** Two siblings finishing at the same instant cannot
  both see themselves as last, and cannot both miss. Count-and-check without the lock
  gives a duplicate rollup or none, intermittently.
* **Reading ``parent_id`` fresh** means a node re-parented between finishing and
  checking signals its NEW parent.
* **Reading the current child set** means a child added late is simply not settled yet,
  and the check fails until it is.
* **Committing between levels** prevents deadlock. Holding the parent while acquiring
  the grandparent deadlocks against a downward task that took the grandparent first and
  wants the parent. One row lock at a time.

**Steps 4 and 5 in one transaction is what makes the rollup fire exactly once**: the
second sibling to arrive finds the first sibling's freshly-enqueued rollup already
pending on the node and therefore neither enqueues nor settles.

ONE DEPARTURE FROM THE LITERAL NUMBERING. The spec's step 2 starts at the finished
node's *parent*. Taken literally, a leaf whose only task just finished is evaluated by
nobody: its parent's check fails because the leaf is not settled, and no child of the
leaf will ever walk up through it, because it has none. The walk here therefore starts
at the finished task's own scope node and evaluates each node under a lock on *that*
node. Every subsequent iteration is the spec's steps 2-7 exactly; the sibling race is
still serialised, because two siblings finishing together both lock and evaluate the
same parent row on their second iteration.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterable, Literal, Optional, Protocol, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jmfts_core.contracts.attempt import param_fingerprint
from jmfts_core.database import get_session
from jmfts_core.models.document import (
    Document,
    SETTLED_FAILED,
    SETTLED_SETTLED,
    USETYPE_FILE,
)
from jmfts_core.repositories.task_queue import TaskQueueRepository

#: Why a node did not settle. ``None`` means it did.
BlockedBy = Literal["tasks", "children", "enqueued", "failed"]


@dataclass(frozen=True)
class TaskSpec:
    """A task a planner wants enqueued, before it has an id.

    ``after`` names other specs *in the same batch* that must complete first. That is
    spec 5.5's within-node ordering — ``extract_facts`` after ``summarize`` — and it is
    resolved to real ``dependencies`` ids at enqueue time, because the batch is the one
    place where the set is genuinely known in advance.
    """

    task_type: str
    write_mode: str
    params: dict = field(default_factory=dict)
    priority: int = 0
    service_badge: Optional[str] = None
    after: tuple[str, ...] = ()
    max_retries: int = 3


@dataclass(frozen=True)
class SettleStep:
    """What one level of the walk decided."""

    node_id: int
    parent_id: Optional[int]
    settled: bool
    enqueued_task_ids: tuple[int, ...] = ()
    blocked_by: Optional[BlockedBy] = None


class RollupPlanner(Protocol):
    """Decides what work becomes eligible for a node once its subtree is finished.

    Called by :func:`settle_node` **only** when the node has no unfinished task and
    every current child is settled — that is, at the exact instant the rollup's inputs
    are complete. Anything it returns is enqueued and the node stays unsettled.
    """

    def __call__(self, session: Session, node: Document) -> Sequence[TaskSpec]: ...


def _no_rollup(session: Session, node: Document) -> Sequence[TaskSpec]:
    return ()


#: The policy "this walk enqueues no rollup work". A named, explicit choice rather than
#: a default: a walk that quietly enqueued nothing because nobody passed a planner would
#: settle a whole tree without ever summarising it, and look like it worked.
NO_ROLLUP: RollupPlanner = _no_rollup


class AttemptDiffPlanner:
    """Enqueue the declared rollup tasks the node's attempt log does not already record.

    This is spec 6.1's diff — ``(task_name, param_fingerprint)`` against
    ``structured_content['attempts']`` — applied at the rollup boundary, and it is what
    makes the walk terminate.

    The termination argument, in full, because it is the property the whole design turns
    on. The planner runs only when the node has no unfinished task. Each spec it returns
    is enqueued and, when it finishes, appends an attempt carrying that spec's
    ``(task, fingerprint)`` pair. So on the next walk over the same node the diff is
    empty, the planner returns nothing, and the node settles. A rollup that creates a
    summary node as a child leaves an in-flight child under the node it summarises; the
    child is a leaf and settles on its own walk, that walk reaches the parent, and the
    second pass enqueues nothing because the log already holds the pair. Without the diff
    this loop is infinite: every walk over a finished node would re-enqueue the same
    rollup.
    """

    def __init__(self, desired: Sequence[TaskSpec]):
        self.desired = tuple(desired)

    def __call__(self, session: Session, node: Document) -> Sequence[TaskSpec]:
        repo = TaskQueueRepository(session)
        already_attempted = repo.attempted_fingerprints(node.id)
        return tuple(
            spec
            for spec in self.desired
            if (spec.task_type, param_fingerprint(spec.params)) not in already_attempted
        )


class SettleWalkError(RuntimeError):
    """The walk found the tree in a shape it cannot be in."""


def settle_node(session: Session, node_id: int, planner: RollupPlanner) -> SettleStep:
    """Spec 5.4 steps 2-5 for one node, in the caller's transaction.

    Takes ``SELECT ... FOR UPDATE`` on the node's row and holds it until the caller
    commits. **Exactly one document row is locked**, which is what keeps the walk out of
    a deadlock with a downward task that already holds an ancestor: this transaction can
    wait on that one, but it holds nothing that task can be waiting on.

    Returns the decision, including the node's freshly-read ``parent_id`` so the caller
    knows where to go next.
    """
    node = (
        session.execute(
            select(Document)
            .where(Document.id == node_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .one_or_none()
    )
    if node is None:
        raise SettleWalkError(
            f"document {node_id} does not exist; the settle walk was handed a node that "
            "was deleted while its task ran"
        )

    parent_id = node.parent_id

    # A node whose task failed permanently (spec 2.1) is terminal. Settling it would
    # publish a node whose ingestion is known to have died, and would let every ancestor
    # settle on top of a hole. It stays 'failed' until a correction (Part 6) re-enqueues
    # work, which is what un-sets it.
    if node.settled == SETTLED_FAILED:
        return SettleStep(node_id, parent_id, settled=False, blocked_by="failed")

    tasks = TaskQueueRepository(session)
    if not tasks.structuring_complete(node_id):
        return SettleStep(node_id, parent_id, settled=False, blocked_by="tasks")

    # The CURRENT child set, read now and not cached anywhere. A child added a
    # microsecond ago is in this count and is not settled, so the check fails — which is
    # precisely the behaviour that makes a late addition safe.
    unsettled_children = session.execute(
        select(func.count())
        .select_from(Document)
        .where(Document.parent_id == node_id)
        .where(Document.settled != SETTLED_SETTLED)
    ).scalar_one()
    if unsettled_children:
        return SettleStep(node_id, parent_id, settled=False, blocked_by="children")

    # Step 4 and step 5, in this transaction, under this row's lock. That is the whole
    # exactly-once mechanism: a second walker cannot get between the "is anything
    # pending?" test above and the enqueue below.
    specs = planner(session, node)
    if specs:
        enqueued = enqueue_batch(tasks, node_id, specs)
        return SettleStep(
            node_id, parent_id, settled=False, enqueued_task_ids=enqueued, blocked_by="enqueued"
        )

    _record_if_yielded_nothing(session, node)

    node.settled = SETTLED_SETTLED
    session.flush()
    return SettleStep(node_id, parent_id, settled=True)


def _record_if_yielded_nothing(session: Session, node: Document) -> None:
    """Note, on a ``file`` node about to settle, that nothing came out of it.

    A file node reaches this point with no content and no children when every extraction
    the probe found a path for produced nothing: a scan with no OCR yet, a truncated
    upload, a ZIP wearing a ``.pdf`` name, a zero-byte file. That is a correct outcome —
    the bytes were processed safely and they do not contain a document we can read. It is
    not an error, and it does not fail the node.

    It is, however, invisible otherwise. ``settled`` means retrievable, and a file node
    with an empty ``content`` contributes nothing to the full-text and vector indexes, so
    the only trace of the upload is a title. This writes ``structured_content['yield']``
    so the state is auditable — "which of my uploads produced no document?" is one query,
    not a full-corpus scan — and carries the probe's own patterns forward as the evidence
    for why, rather than restating a guess.

    Scoped to ``file`` because that is the node whose whole purpose is to hold what came
    out of a byte stream. A structural node legitimately holds only children, and a chunk
    node cannot reach here empty.
    """
    if node.usetype != USETYPE_FILE:
        return
    if (node.content or "").strip():
        return
    children = session.execute(
        select(func.count()).select_from(Document).where(Document.parent_id == node.id)
    ).scalar_one()
    if children:
        return

    structured = dict(node.structured_content or {})
    structured["yield"] = {
        "documents": 0,
        "reason": "no content and no children when the node settled",
        "matched": (structured.get("matched") or {}).get("patterns"),
    }
    node.structured_content = structured


def enqueue_batch(
    tasks: TaskQueueRepository, node_id: int, specs: Sequence[TaskSpec]
) -> tuple[int, ...]:
    """Enqueue one node's batch, resolving ``after`` names to ``dependencies`` ids.

    Public because the rollup walk is not the only place a batch of within-node tasks is
    planned: a task handler that decides what runs next (spec Part 4 — ``probe`` reading
    its own ``matched.patterns``) needs the same ``after``-to-``dependencies`` resolution,
    and duplicating it would let the two drift.
    """
    by_name: dict[str, int] = {}
    created: list[int] = []
    for spec in specs:
        missing = [name for name in spec.after if name not in by_name]
        if missing:
            raise SettleWalkError(
                f"task {spec.task_type!r} declares after={spec.after!r} but "
                f"{missing!r} is not earlier in the same batch; within-node ordering "
                "can only reference tasks whose ids this batch already knows"
            )
        task = tasks.enqueue(
            spec.task_type,
            node_id,
            spec.write_mode,
            params=spec.params,
            dependencies=[by_name[name] for name in spec.after] or None,
            priority=spec.priority,
            service_badge=spec.service_badge,
            max_retries=spec.max_retries,
        )
        by_name[spec.task_type] = task.id
        created.append(task.id)
    return tuple(created)


SessionFactory = Callable[[], AbstractContextManager[Session]]


def settle_walk(
    node_id: int,
    planner: RollupPlanner,
    *,
    session_factory: SessionFactory = get_session,
    max_levels: int = 256,
) -> list[SettleStep]:
    """Walk up from ``node_id``, one node and one transaction at a time (spec 5.4).

    Stops at the first node that does not settle — everything above it is waiting on
    that node anyway — and at the root.

    ``session_factory`` must yield a session that **commits on clean exit**, and a fresh
    one per call: the whole point of step 7's "in a new transaction" is that the previous
    level's row lock is already released when the next one is taken. Passing a factory
    that reuses one transaction (``unit_of_work``, or the test suite's savepoint session)
    turns the walk back into the deadlock-prone form the spec rejects.
    """
    steps: list[SettleStep] = []
    seen: set[int] = set()
    current: Optional[int] = node_id

    while current is not None:
        if current in seen:
            # Only reachable if `path`/`parent_id` describe a cycle, which reparent's
            # guard forbids. Raising beats looping until max_levels and pretending.
            raise SettleWalkError(
                f"settle walk revisited document {current}: the ancestor chain is cyclic"
            )
        seen.add(current)
        if len(steps) >= max_levels:
            raise SettleWalkError(
                f"settle walk exceeded {max_levels} levels from {node_id}; the tree is "
                "deeper than anything this system creates and the chain is suspect"
            )

        with session_factory() as session:
            step = settle_node(session, current, planner)
        steps.append(step)

        if not step.settled:
            break
        current = step.parent_id

    return steps


def borrow_session(session: Session) -> SessionFactory:
    """A walk factory that runs every level on ``session``, committing between levels.

    For the caller that has a session already and no way to open a second one — a service
    method running inside a request, which must walk up AFTER its own commit (5.4 step 7
    applies to a deletion exactly as it does to a completed task, and a walk that ran
    before the delete committed would read the row it is walking away from).

    This is not the reuse :func:`settle_walk`'s docstring warns about. That warning is
    about a factory which keeps ONE transaction open across levels — ``unit_of_work``, or
    a test's savepoint session used without committing — because holding level N's row
    lock while taking level N+1's is the deadlock 5.4 rejects. Here the exit commits, so
    each level is its own transaction and each row lock is released before the next is
    taken. What is given up, knowingly, is isolation from the caller's own uncommitted
    work: anything the caller has pending is committed by the first level. Callers commit
    first for that reason.
    """

    @contextmanager
    def _factory():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    return _factory


def settle_after_task(
    scope_document_id: int,
    planner: RollupPlanner,
    *,
    session_factory: SessionFactory = get_session,
) -> list[SettleStep]:
    """Spec 5.4 steps 2-7, run after a task's completion has been committed.

    A thin name over :func:`settle_walk` so worker code reads as the spec does: finish
    the task, then settle from where it ran. Step 1 (mark complete, append the attempt)
    belongs to the worker's own transaction and must already have committed — the walk
    asks the database whether the node still has unfinished work, and an uncommitted
    completion still counts as unfinished.
    """
    return settle_walk(scope_document_id, planner, session_factory=session_factory)


def unsettled_children(session: Session, node_id: int) -> Iterable[Document]:
    """The children keeping ``node_id`` from settling. Diagnostic, not part of the walk."""
    return (
        session.execute(
            select(Document)
            .where(Document.parent_id == node_id)
            .where(Document.settled != SETTLED_SETTLED)
            .order_by(Document.id)
        )
        .scalars()
        .all()
    )
