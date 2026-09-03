"""What this appliance means by a transcript: messages in, messages out, and readable text.

WAS the conversation ingestion orchestrator (#59) — parse, create documents, embed, RAPTOR,
fact extraction, all inside one request. ``SPRINT_JOBS.md`` 15.4 S7 moved every one of
those onto the ingest queue, where they are the tasks every other format already ran, so
what is left here is the vocabulary rather than the pipeline:

* :class:`ParsedMessage` — one turn.
* :func:`parse_adjutant_jsonl` and :func:`parse_message_array` — the two spellings a
  caller may send, into that.
* :func:`messages_to_jsonl` — back out, as the bytes the queue ingests.
* :func:`conversation_markdown` — the readable form ``extract:text`` writes onto the file
  node.

``probe`` recognises a transcript with :data:`~jmfts_core.probe.CONVERSATION_PATTERN`'s
``is_conversation``, ``jmfts_core.structure_tasks`` reads it, and
``jmfts_core.conversation_tasks`` writes its turns.

"""

import json
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ParsedMessage:
    role: str
    content: str
    timestamp: Optional[str] = None
    turn_index: int = 0


# `StageResult`, `StageClock` and `IngestResult` WERE HERE, and `SPRINT_JOBS.md` 15.4 S9
# deleted them with the synchronous pipeline that was their only remaining caller.
#
# Each has a successor and none of the three is a loss:
#
# * `StageResult` — one stage's outcome, its detail and its wall-clock span. The queue's
#   `AttemptRecord` (INGEST_SPEC.md 3.4) is the same record, per TASK, and durable: it is
#   written onto the node instead of returned in an HTTP body and forgotten.
# * `StageClock` — stamped those spans. `TaskQueueRepository.claim`/`complete` take them
#   from the server clock, which is the one every worker in a fleet agrees on.
# * `IngestResult` — the six counts a run reported as it went.
#   `jmfts_core.ingest_summary.summarize_tree` reads them back off the finished tree,
#   which is the only place they can come from once the work is spread across tasks.


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_adjutant_jsonl(raw: str) -> list[ParsedMessage]:
    """Parse adjutant JSONL session data into messages.

    Adjutant format: one JSON object per line with prompt/response pairs.
    ``{"prompt": "...", "response": "...", "timestamp": "..."}``

    Also accepts pre-structured message lines:
    ``{"role": "user", "content": "...", "timestamp": "..."}``
    """
    messages: list[ParsedMessage] = []
    turn_index = 0
    for line_num, line in enumerate(raw.strip().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed JSONL line %d", line_num)
            continue

        ts = obj.get("timestamp")

        # Adjutant prompt/response pair format
        if "prompt" in obj:
            messages.append(
                ParsedMessage(
                    role=obj.get("role", "user"),
                    content=obj["prompt"],
                    timestamp=ts,
                    turn_index=turn_index,
                )
            )
            turn_index += 1
            if "response" in obj:
                messages.append(
                    ParsedMessage(
                        role="assistant",
                        content=obj["response"],
                        timestamp=ts,
                        turn_index=turn_index,
                    )
                )
                turn_index += 1

        # Pre-structured message format
        elif "role" in obj and "content" in obj:
            messages.append(
                ParsedMessage(
                    role=obj["role"],
                    content=obj["content"],
                    timestamp=ts,
                    turn_index=turn_index,
                )
            )
            turn_index += 1
        else:
            logger.warning("Skipping unrecognised JSONL line %d", line_num)

    return messages


def parse_message_array(messages: list[dict]) -> list[ParsedMessage]:
    """Parse simple message array format [{role, content, timestamp?}]."""
    return [
        ParsedMessage(
            role=msg.get("role", "unknown"),
            content=msg.get("content", ""),
            timestamp=msg.get("timestamp"),
            turn_index=i,
        )
        for i, msg in enumerate(messages)
    ]


def messages_to_jsonl(messages: list[ParsedMessage]) -> str:
    """The pre-structured JSONL :func:`parse_adjutant_jsonl` reads back unchanged.

    ``SPRINT_JOBS.md`` 15.4 S7. The queue starts from stored bytes, and
    ``POST /conversations/ingest`` accepts a JSON ARRAY as well as a JSONL string — so the
    array has to become bytes before it can be ingested, and these are the bytes.

    **A re-encoding, not the caller's own text.** A caller who sent ``messages`` gets a
    blob that is a faithful record of the messages this appliance parsed, in the one shape
    ``probe`` recognises and one line per turn. A caller who sent ``jsonl`` gets their
    bytes stored verbatim; only the array path is re-encoded, and it has to be, because
    there is no single line-oriented spelling of a JSON array.

    ``turn_index`` is deliberately NOT written. It is the position of the line in the file,
    and storing it would create a second record of that fact that a later edit could
    contradict. ``timestamp`` is written only when there is one — a null would claim the
    message carried a time that was empty rather than none.
    """
    lines = []
    for msg in messages:
        record: dict = {"role": msg.role, "content": msg.content}
        if msg.timestamp is not None:
            record["timestamp"] = msg.timestamp
        lines.append(json.dumps(record, ensure_ascii=False))
    return "\n".join(lines) + "\n"


def conversation_markdown(messages: list[ParsedMessage]) -> str:
    """The whole conversation as readable text — what ``extract:text`` writes.

    ``[role]: content``, one block per turn, blank line between. The same concatenation the
    deprecated ``ingest_conversation`` put on its root node, so what a search hit on the
    file node shows is unchanged by the migration.

    Named beside the parser rather than in ``structure_tasks`` because it is the inverse of
    the parser: the two together define what this appliance means by a transcript, and a
    format's readable form belongs with the code that reads it.
    """
    return "\n\n".join(f"[{m.role}]: {m.content}" for m in messages)


# `SegmentationOutcome`, `segment_conversation` and `ingest_conversation` WERE HERE, and
# `SPRINT_JOBS.md` 15.4 S7 deleted all three with the pipeline that called them.
#
# What each of them did, and what does it now:
#
# * `segment_conversation` ran PELT over the turns' embeddings and inserted topic
#   containers. It was opt-in and off by default. `jmfts_core.rollup_tasks`'
#   `structure:semantic` does the same thing for every format, at the settling boundary,
#   where it can also segment the containers a previous pass created (11.4).
# * `ingest_conversation` orchestrated parse -> chunk -> segment -> RAPTOR -> facts inside
#   one request. Those are five tasks now, and `jmfts_core/conversation_tasks.py` holds the
#   only part of it that is genuinely about transcripts: one chunk per turn, and an
#   over-window turn split into pieces that fit.
# * `SegmentationOutcome` existed to keep "PELT ran and found one topic" apart from "PELT
#   never ran", which 3.4 insists on. `structure:semantic` carries that distinction itself.
#
# What is left in this module is the vocabulary: what a message is, how the two accepted
# transcript spellings are parsed into messages, and what a conversation looks like as
# readable text.
