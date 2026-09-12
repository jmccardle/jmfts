"""Which clock a relative date in a source is resolved against.

``SPRINT_0_4_0.md`` Block B step 7 and its open question 4.2. The extraction prompt asks
the model for ISO 8601 dates in ``valid_from``/``valid_until`` and used to inject no
"recorded on" line, so a source saying "last Tuesday" produced *a* date — computed from
whatever the model believed today to be — and that date entered ``triples`` looking exactly
like one the source had stated. Silently wrong output, which is why the fix sits in the
defect block. ``ROADMAP.md`` known gap 3.

4.2 is decided: ``Document.event_time`` where the caller set one, ingest time
(``created_at``) otherwise. The case it serves is a backfilled transcript, where
"yesterday" means yesterday relative to the transcript and not relative to the import.

These assert on the prompt TEXT, which needs no LLM and no database — a real ``Document``
is constructed in memory, so the two clocks can be set to dates months apart and the test
can say which one reached the wire. The end-to-end case uses ``tests/llm_stub.py`` rather
than a mock so that the assertion is about what JMFTS actually sent, not about a call
record: the prompt is only a fix if it survives the trip through ``llm_client``.

Kept out of ``test_fact_extraction.py`` because that file's document doubles are
``MagicMock``s, and a MagicMock answers ``event_time`` with a truthy mock — it cannot
distinguish "the caller set a domain clock" from "nobody set anything", which is the entire
question here.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from unittest.mock import MagicMock

import pytest

from jmfts_core.fact_extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    build_extraction_prompt,
    extract_facts_from_document,
    extraction_anchor,
)
from jmfts_core.models.document import Document
from tests.llm_stub import llm_stub


def _run(coro):
    """Run an async coroutine synchronously (same shape as test_fact_extraction.py)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


#: The two dates are eight months apart and in different years on purpose: an assertion
#: that only checked "some ISO date is present" would pass against either one, and the
#: whole of 4.2 is which of the two it is.
INGESTED_AT = datetime(2026, 9, 4, 11, 30, tzinfo=timezone.utc)
HAPPENED_AT = datetime(2025, 12, 19, 9, 0, tzinfo=timezone.utc)


def _doc(**kwargs) -> Document:
    """An unpersisted Document carrying only the columns these tests read.

    Never flushed: ``created_at``'s default fires at INSERT, so setting it explicitly here
    is the only way to hold a known ingest clock without a database.
    """
    values = {"id": 7, "title": "a turn", "content": "It shipped yesterday."}
    values.update(kwargs)
    return Document(**values)


class TestExtractionAnchor:
    """Which of the two columns answers, and what happens when neither does."""

    def test_event_time_wins_when_the_caller_set_one(self):
        doc = _doc(created_at=INGESTED_AT, event_time=HAPPENED_AT)
        assert extraction_anchor(doc) == HAPPENED_AT

    def test_created_at_answers_when_event_time_is_null(self):
        doc = _doc(created_at=INGESTED_AT, event_time=None)
        assert extraction_anchor(doc) == INGESTED_AT

    def test_no_clock_at_all_raises(self):
        """Fail early rather than substituting ``now()``.

        A persisted row always has ``created_at``, so this is an unflushed in-memory
        Document. Guessing the current date for one would be step 7's defect again, one
        layer further down and with no source text to contradict it.
        """
        doc = _doc(created_at=None, event_time=None)
        with pytest.raises(ValueError, match="event_time"):
            extraction_anchor(doc)


class TestBuildExtractionPrompt:
    """The date the model is told to resolve against."""

    def test_prompt_carries_the_event_time_date(self):
        prompt = build_extraction_prompt(_doc(created_at=INGESTED_AT, event_time=HAPPENED_AT))
        assert "recorded on 2025-12-19" in prompt
        assert "2026-09-04" not in prompt

    def test_prompt_carries_created_at_when_there_is_no_event_time(self):
        prompt = build_extraction_prompt(_doc(created_at=INGESTED_AT, event_time=None))
        assert "recorded on 2026-09-04" in prompt
        assert "2025-12-19" not in prompt

    def test_anchor_is_additive(self):
        """The base prompt survives intact; the anchor is a clause, not a rewrite."""
        prompt = build_extraction_prompt(_doc(created_at=INGESTED_AT))
        assert EXTRACTION_SYSTEM_PROMPT in prompt
        assert "valid_from" in prompt

    def test_the_model_is_told_not_to_use_the_current_date(self):
        """The injected date is useless if the model may still fall back to today.

        Both halves are load-bearing: resolve against the recorded date, and leave the
        field null when that is not possible. A guessed date is worse than an absent one
        because only the guess reaches ``triples``.
        """
        prompt = build_extraction_prompt(_doc(created_at=INGESTED_AT))
        assert "Do not resolve any of them against the current date" in prompt
        assert "leave the field null rather than guessing" in prompt

    def test_date_only_no_time_of_day(self):
        """Day-grained expressions get a day-grained anchor; an hour would overstate it."""
        prompt = build_extraction_prompt(_doc(created_at=INGESTED_AT))
        assert "11:30" not in prompt
        assert "2026-09-04T" not in prompt

    def test_the_anchor_moves_with_the_document(self):
        """Two documents, two prompts — the anchor is not computed once per process."""
        first = build_extraction_prompt(_doc(created_at=INGESTED_AT))
        second = build_extraction_prompt(_doc(created_at=INGESTED_AT - timedelta(days=400)))
        assert first != second


class TestAnchorReachesTheWire:
    """The prompt as ``llm_client`` actually sends it.

    ``llm_stub`` answers with an empty JSON array, so extraction stops after the call and
    no entity resolution, predicate registry or database work is reached: the request body
    is the whole assertion.
    """

    def _system_message(self, doc: Document) -> str:
        session = MagicMock()
        session.get.return_value = doc
        with llm_stub(content="[]") as stub:
            result = _run(
                extract_facts_from_document(
                    document_id=doc.id, session=session, settings=stub.settings()
                )
            )
            assert result.errors == []
            assert len(stub.requests) == 1
            return stub.requests[0].role("system")

    def test_event_time_reaches_the_system_message(self):
        system = self._system_message(_doc(created_at=INGESTED_AT, event_time=HAPPENED_AT))
        assert "recorded on 2025-12-19" in system
        assert "2026-09-04" not in system

    def test_created_at_reaches_the_system_message(self):
        system = self._system_message(_doc(created_at=INGESTED_AT, event_time=None))
        assert "recorded on 2026-09-04" in system
        assert "2025-12-19" not in system
