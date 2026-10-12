"""``structure:conversation`` — a transcript's turns. ``SPRINT_JOBS.md`` 15.4 S7.

The third alternative to the two prose rungs, and a peer of them rather than a step below:
exactly one of the three is ever eligible for a document, because a conversation states
where each message begins and prose does not. The rung the nodes carry is ``declared``, and
that is 3.5's word used literally — there is no heuristic anywhere in here.

**Why a transcript stopped being a usetype.** Path A asked the caller to say
``usetype="conversation"`` and ran a 674-line orchestrator under that name. 15.2 decision 3
made it a probed pattern instead, and what that buys is everything the queue already gives
every other input: an attempt log, retry classification, ``EXPLAIN``, per-node failure
instead of per-request, and a tree that is segmented and summarized by the same rollup as a
PDF's. What it costs is that a caller can no longer force the reading — and that is the
same trade every other format made, for the same reason: what the bytes are is a
measurement, not a claim.

**What the deprecated pipeline did that this does not, and why each is not a loss:**

* *Its own PELT segmentation* (``segment_conversation``, opt-in, off by default). Path B
  segments every node wider than ``rollup.max_children``, in document order, for every
  format. That is the same algorithm at the settling boundary, where it can also see the
  containers a previous pass created (11.4's two recursion directions).
* *RAPTOR summarization.* ``summarize`` gives every container its ``effective_content``,
  and 11.4 gives the reason it segments in document order rather than clustering.
* *Fact extraction.* S6's ``extract:facts``, which the ``conversation`` usetype turns on
  exactly as path A's stage was on by default.

**A turn that overflows the token window becomes a container with chunk children**, which
is what the deprecated pipeline did (KNOWN-DEFECTS D1) and is also what the prose rungs do
with an oversized region. The turn keeps its own ``content`` and gets a document vector;
its pieces carry the token vectors. Nothing here embeds anything — ``embed`` is its own
task, queued per chunk, and a node is ``in_flight`` until it runs.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from jmfts_core.atoms import (
    COST_CPU,
    EV_BLOB,
    EV_MATCHED,
    EV_STRUCTURE,
    EV_TEXT,
    KEY_POSITION,
    ChildKey,
    Fanout,
)
from jmfts_core.conversation_ingest import ParsedMessage, parse_adjutant_jsonl
from jmfts_core.embedding import get_embedding_service
from jmfts_core.ingest_tasks import (
    TASK_STRUCTURE_CONVERSATION,
    Frontier,
    TaskOutcome,
    enqueue_frontier,
    plan_frontier,
    register_task_handler,
)
from jmfts_core.models.document import SETTLED_IN_FLIGHT, SETTLED_SETTLED, USETYPE_CHUNK
from jmfts_core.models.task_queue import WRITE_CHILDREN, TaskQueue
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.structure_tasks import RUNG_DECLARED, _scope_node

logger = logging.getLogger(__name__)


def _conversation_ceiling(evidence: dict, params: dict) -> tuple[int, int]:
    """How many CHUNK nodes this rung may write. ``SPRINT_JOBS.md`` 2.4.

    ``SPRINT_0_6_0.md`` question 4.11, option A (answered 2026-10-12): the two counts probe
    already measures over the whole file, and nothing else. Loose, and true:

    * **Turns.** A turn is a JSON string inside one line, and a line yields at most two —
      a prompt/response pair writes both. NOT at most one, which is what 4.11 said; the
      sum below still holds because of the next point.
    * **Parts.** :func:`_split_oversized` writes one per piece ``chunk_to_fit`` returns,
      and every piece holds at least one non-whitespace character of the turn that no
      other piece holds. ``_split_sentences`` drops a piece that strips to nothing;
      ``_enforce_max_chars`` emits a short text whole and otherwise only non-empty
      ``str.split()`` words, joins of them, or non-empty slices of one; ``_enforce_fit``
      recurses through ``_enforce_max_chars`` and nothing else. ``min_chunk_length`` does
      not enter — this caller leaves it at 1 — and neither does the window: the window
      decides WHETHER a turn splits, and the ``sentence`` strategy emits one part per
      sentence without packing them, so a part is a sentence and not a window's worth.
    * **The sum.** A JSON-decoded string is never longer than its encoding, and every line
      that yields a turn spends at least 13 characters on syntax (``{"prompt":""}``) and
      27 on a pair. So a line writes at most ``2 + its content characters``, which is
      under its own length, and summing over lines gives ``char_count``. ``line_count`` is
      added on top of a ceiling that already holds; it is in the formula because it is
      the turns half, stated in the terms a reader checks it in.

    Floor zero: a file whose lines parse to no turns settles with no children (the
    handler's zero-yield branch), so zero is reachable. Option B in 4.11 is the tighter
    replacement — pack the parts to the window, then bound them by tokens — and it is
    0.7.0's, because it changes what the rung writes rather than what it declares.
    """
    lines = int(evidence["matched.patterns.line_count"])
    characters = int(evidence["matched.patterns.char_count"])
    return (0, lines + characters)


#: Position, as for the prose rungs' chunks. A turn has a `turn_index`, but that IS its
#: position — nothing in a transcript names a turn — and a part has no key of its own.
_CONVERSATION_FANOUT = Fanout(
    bound=_conversation_ceiling,
    reads=("matched.patterns.line_count", "matched.patterns.char_count"),
    basis="one per turn, and at most one per character of an over-window turn",
    # Turns and their parts are both `chunk` nodes, written by this rule under one
    # frontier, so one interval counts both — the same pair 4.1's scope matches on.
    counts=USETYPE_CHUNK,
)


@register_task_handler(
    TASK_STRUCTURE_CONVERSATION,
    # The bytes, not the text. `extract:text`'s conversation reader wrote the readable
    # concatenation onto the node, and reading THAT back to recover turns would mean
    # parsing `[role]: ` markers out of prose — a second, weaker parser for a fact the
    # original JSON states outright. `matched` is a real consumption too: this handler is
    # reached only because `is_conversation` was measured.
    consumes=(f"{EV_MATCHED}@self", f"{EV_BLOB}@self", f"{EV_TEXT}@self"),
    produces=(f"{EV_STRUCTURE}@self", f"{EV_STRUCTURE}@subtree", f"{EV_TEXT}@subtree"),
    write_mode=WRITE_CHILDREN,
    # The tokenizer, not the model: `check_fit` loads no weights. A badge sized on this
    # being `model` would pin a JSON parse to the GPU pool.
    cost_class=COST_CPU,
    fanout=_CONVERSATION_FANOUT,
    child_key=ChildKey(KEY_POSITION),
)
def run_structure_conversation(session: Session, task: TaskQueue) -> TaskOutcome:
    """One chunk node per turn, in the order the transcript states."""
    doc = _scope_node(session, task, TASK_STRUCTURE_CONVERSATION)

    data = BlobRepository(session).read_bytes(doc.id)
    if data is None:
        raise ValueError(
            f"document {doc.id} has no stored blob; structure:conversation was enqueued "
            "for bytes that are no longer there"
        )
    messages = parse_adjutant_jsonl(data.decode("utf-8"))

    # NOT an error, and not a failure either. `probe` decided from the FIRST message line;
    # a file whose remaining lines are all malformed is a file this appliance read
    # correctly and found one turn in, or none. A zero yield settles the node with no
    # children and the detail says how many lines were read — the same shape
    # `extract:text` uses for a PDF whose text layer gives three characters.
    if not messages:
        return TaskOutcome(
            rung=RUNG_DECLARED,
            detail={
                "messages": 0,
                "reason": (
                    "probe read the first line as a conversation message and no line in "
                    "the file parsed as one; the transcript has no turns to write"
                ),
                "bytes_in": len(data),
            },
        )

    repo = DocumentRepository(session)
    tasks = TaskQueueRepository(session)
    service = get_embedding_service()
    # One frontier for the whole transcript. A turn and an over-window turn's parts are the
    # same kind of node written by the same rule, so they share it: 4.1's scope is
    # `(produced_by, usetype)` and both halves are equal for the two.
    frontier = plan_frontier(
        session, doc, produced_by=TASK_STRUCTURE_CONVERSATION, usetype=USETYPE_CHUNK
    )

    child_ids: list[int] = []
    part_count = 0
    for message in messages:
        turn_id = _write_turn(
            repo,
            tasks,
            service,
            parent_id=doc.id,
            message=message,
            conversation_id=doc.id,
            frontier=frontier,
        )
        child_ids.append(turn_id)
        part_count += _split_oversized(
            repo,
            tasks,
            service,
            turn_id=turn_id,
            message=message,
            conversation_id=doc.id,
            frontier=frontier,
        )

    session.flush()

    EvidenceRepository(session).write(
        doc.id,
        "structure",
        {
            "primary_rung": RUNG_DECLARED,
            "source": "conversation_turns",
            "node_count": len(child_ids) + part_count,
        },
    )

    return TaskOutcome(
        rung=RUNG_DECLARED,
        detail={
            "messages": len(messages),
            "participants": sorted({m.role for m in messages}),
            "oversized_turn_parts": part_count,
            "first_timestamp": messages[0].timestamp,
            "last_timestamp": messages[-1].timestamp,
            "bytes_in": len(data),
        },
        produced={"node_count": len(child_ids) + part_count, "child_ids": child_ids},
    )


def _write_turn(
    repo: DocumentRepository,
    tasks: TaskQueueRepository,
    service,
    *,
    parent_id: int,
    message: ParsedMessage,
    conversation_id: int,
    frontier: Frontier,
) -> int:
    """One turn, as a chunk node holding an ``embed``. Returns its id."""
    over_window = service.check_fit(message.content, with_tokens=True).truncated
    node = repo.create(
        title=f"{message.role} — turn {message.turn_index}",
        content=message.content,
        parent_id=parent_id,
        usetype=USETYPE_CHUNK,
        produced_by=TASK_STRUCTURE_CONVERSATION,
        evidence={
            "rung": RUNG_DECLARED,
            "speaker": message.role,
            "turn_index": message.turn_index,
            "timestamp": message.timestamp,
            "conversation_id": conversation_id,
            # Stated on the node rather than left to be inferred from whether it has
            # children: a turn with one part and a turn that fit are different facts, and
            # only this says which.
            "over_token_window": over_window,
        },
        auto_embed=False,
        sequential=True,
        settled=SETTLED_IN_FLIGHT if frontier.in_flight else SETTLED_SETTLED,
    )
    enqueue_frontier(tasks, node, frontier)
    return node.id


def _split_oversized(
    repo: DocumentRepository,
    tasks: TaskQueueRepository,
    service,
    *,
    turn_id: int,
    message: ParsedMessage,
    conversation_id: int,
    frontier: Frontier,
) -> int:
    """Give an over-window turn chunk children that fit. Returns how many were written.

    Most assistant messages exceed the token/maxsim window — the routine case, not the edge
    case. The turn keeps its own text and its document vector; the pieces carry the token
    vectors, so MaxSim has something in range to score against. Before this shape existed
    the message was embedded as a ~2000-character prefix and reported as fully embedded
    (KNOWN-DEFECTS D1).
    """
    if not service.check_fit(message.content, with_tokens=True).truncated:
        return 0
    written = 0
    for index, piece in enumerate(service.chunk_to_fit(message.content)):
        node = repo.create(
            title=f"{message.role} — turn {message.turn_index} (part {index + 1})",
            content=piece,
            parent_id=turn_id,
            usetype=USETYPE_CHUNK,
            produced_by=TASK_STRUCTURE_CONVERSATION,
            evidence={
                "rung": RUNG_DECLARED,
                "speaker": message.role,
                "turn_index": message.turn_index,
                "timestamp": message.timestamp,
                "conversation_id": conversation_id,
                "part_index": index,
            },
            auto_embed=False,
            sequential=True,
            settled=SETTLED_IN_FLIGHT if frontier.in_flight else SETTLED_SETTLED,
        )
        enqueue_frontier(tasks, node, frontier)
        written += 1
    return written
