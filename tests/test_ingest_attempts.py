"""The durable attempt log — ``INGEST_SPEC.md`` Part 3.3 / 3.4.

The CONTRACT and the fingerprint, with no database. ``AttemptRecord`` and
``param_fingerprint`` live in ``jmfts-client`` and are what every task writes through, so
they outlived both ingest pipelines.

A Tier-2 half was here — it asserted that ``execute_pipeline`` really wrote the log to the
row rather than into a mock — and ``SPRINT_JOBS.md`` 15.4 S9 deleted it with that function.
The queue writes its own log, one entry per TASK, through
``TaskQueueRepository.complete``/``fail``; it is asserted in ``tests/test_ingest_worker.py``
and ``tests/test_file_upload.py``.
"""

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from pydantic import ValidationError
from sqlalchemy import text as sa_text

from jmfts_client.contracts.attempt import AttemptRecord, param_fingerprint

# ---------------------------------------------------------------------------
# DB availability check (same pattern as test_regression_ingestion.py)
# ---------------------------------------------------------------------------

try:
    from jmfts_core.database import get_engine
    from jmfts_core.embedding import EmbeddingResult, TokenEmbeddingResult

    _engine = get_engine()
    with _engine.connect() as _conn:
        _conn.execute(sa_text("SELECT 1"))
    _DB_AVAILABLE = True
except Exception:
    _DB_AVAILABLE = False

requires_db = pytest.mark.skipif(not _DB_AVAILABLE, reason="Database not available")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _utc(offset_seconds: int = 0) -> datetime:
    return datetime(2026, 8, 15, 14, 21, 3, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)


def _record(**overrides) -> AttemptRecord:
    """A minimal valid record; override one field per test to isolate the rule."""
    fields = dict(
        task="chunk",
        status="completed",
        scope_document_id=1000,
        params={},
        param_fingerprint=param_fingerprint({}),
        started_at=_utc(),
        finished_at=_utc(3),
    )
    fields.update(overrides)
    return AttemptRecord(**fields)


# ============================================================================
# Tier 1 — param_fingerprint
# ============================================================================


class TestParamFingerprint:
    def test_insensitive_to_dict_ordering(self):
        a = {"min_section_chars": 200, "exclude_patterns": ["^Page \\d+$"], "enabled": True}
        b = {"enabled": True, "exclude_patterns": ["^Page \\d+$"], "min_section_chars": 200}
        assert param_fingerprint(a) == param_fingerprint(b)

    def test_insensitive_to_nested_dict_ordering(self):
        a = {"chunk": {"strategy": "paragraph", "max_tokens": 200}}
        b = {"chunk": {"max_tokens": 200, "strategy": "paragraph"}}
        assert param_fingerprint(a) == param_fingerprint(b)

    def test_changes_when_a_value_changes(self):
        base = {"strategy": "paragraph", "max_tokens": 200}
        assert param_fingerprint(base) != param_fingerprint({**base, "max_tokens": 500})

    def test_changes_when_a_key_is_added(self):
        base = {"strategy": "paragraph"}
        assert param_fingerprint(base) != param_fingerprint({**base, "min_chunk_length": 20})

    def test_list_order_is_significant(self):
        # Lists are ordered data (exclusion patterns are applied in order), so unlike dict
        # keys their order MUST change the fingerprint.
        assert param_fingerprint({"p": [1, 2]}) != param_fingerprint({"p": [2, 1]})

    def test_empty_params_are_hashable(self):
        fp = param_fingerprint({})
        assert fp.startswith("sha256:")
        assert len(fp) == len("sha256:") + 64

    def test_distinguishes_none_from_missing(self):
        # `llm_model: None` means "not overridden"; an absent key means the stage has no
        # such parameter at all. Spec 6.1 diffs on this value, so they cannot collide.
        assert param_fingerprint({"llm_model": None}) != param_fingerprint({})

    def test_rejects_values_json_cannot_represent(self):
        with pytest.raises(TypeError):
            param_fingerprint({"when": datetime(2026, 8, 15, tzinfo=timezone.utc)})

    def test_stable_across_processes(self):
        """Deterministic under different PYTHONHASHSEEDs — spec 6.1 compares across runs.

        Two subprocesses with different hash seeds must agree with this process. This is
        the property that rules out any implementation resting on ``hash()`` or on dict
        iteration order.
        """
        expected = param_fingerprint({"b": 1, "a": {"z": [1, 2], "y": "x"}, "c": None})
        code = (
            "from jmfts_client.contracts.attempt import param_fingerprint;"
            "print(param_fingerprint({'a': {'y': 'x', 'z': [1, 2]}, 'c': None, 'b': 1}))"
        )
        for seed in ("1", "12345"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            out = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                check=True,
                env=env,
                cwd=REPO_ROOT,
            )
            assert out.stdout.strip() == expected


# ============================================================================
# Tier 1 — AttemptRecord invariants
# ============================================================================


class TestAttemptRecordStatus:
    def test_skipped_without_a_reason_is_rejected(self):
        with pytest.raises(ValidationError, match="detail.reason"):
            _record(status="skipped", detail={})

    def test_skipped_with_a_blank_reason_is_rejected(self):
        with pytest.raises(ValidationError, match="detail.reason"):
            _record(status="skipped", detail={"reason": "   "})

    def test_skipped_with_a_reason_is_accepted(self):
        rec = _record(status="skipped", detail={"reason": "no vision model configured"})
        assert rec.status == "skipped"

    def test_completed_finding_nothing_needs_no_reason(self):
        """Spec 3.4: a heuristic that ran and matched nothing is `completed`, not skipped.

        The record must not force a reason onto it — that is precisely the fact the two
        statuses exist to keep apart.
        """
        rec = _record(status="completed", detail={"looked_for": "outline", "sections": 0})
        assert rec.status == "completed"
        assert "reason" not in rec.detail

    def test_unknown_status_is_rejected(self):
        with pytest.raises(ValidationError):
            _record(status="done")

    def test_all_five_statuses_are_accepted(self):
        assert _record(status="completed").status == "completed"
        assert _record(status="failed", error="boom").status == "failed"
        assert _record(status="skipped", detail={"reason": "disabled"}).status == "skipped"
        assert _record(status="running", finished_at=None).status == "running"
        assert _record(status="pending", started_at=None, finished_at=None).status == "pending"


class TestAttemptRecordTiming:
    def test_naive_started_at_is_rejected(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            _record(started_at=datetime(2026, 8, 15, 14, 21, 3))

    def test_non_utc_offset_is_normalised(self):
        eastern = timezone(timedelta(hours=-4))
        rec = _record(
            started_at=datetime(2026, 8, 15, 10, 21, 3, tzinfo=eastern),
            finished_at=_utc(3),
        )
        assert rec.started_at == _utc()
        assert rec.started_at.utcoffset() == timedelta(0)

    def test_terminal_status_requires_finished_at(self):
        with pytest.raises(ValidationError, match="finished_at"):
            _record(status="completed", finished_at=None)

    def test_non_terminal_status_requires_started_at(self):
        with pytest.raises(ValidationError, match="started_at"):
            _record(status="running", started_at=None, finished_at=None)

    def test_pending_carries_no_timestamps(self):
        with pytest.raises(ValidationError, match="pending"):
            _record(status="pending", started_at=_utc(), finished_at=_utc(1))

    def test_finished_before_started_is_rejected(self):
        with pytest.raises(ValidationError, match="precedes"):
            _record(started_at=_utc(5), finished_at=_utc(1))


class TestAttemptRecordSerialisation:
    def test_to_jsonb_is_json_native(self):
        entry = _record(
            rung="declared",
            produced={"node_count": 2, "child_ids": [7, 8]},
            detail={"sections": 2},
        ).to_jsonb()
        assert datetime.fromisoformat(entry["started_at"]) == _utc()
        assert entry["produced"] == {"node_count": 2, "child_ids": [7, 8]}
        # The not-yet-built machinery is explicitly null, not absent and not invented.
        assert entry["task_id"] is None
        assert entry["write_mode"] is None
        assert entry["superseded_by"] is None
        assert entry["error_type"] is None

    def test_every_spec_3_4_field_is_present(self):
        entry = _record().to_jsonb()
        assert set(entry) == {
            "task",
            "task_id",
            "status",
            "attempt",
            "rung",
            "scope_document_id",
            "write_mode",
            "params",
            "param_fingerprint",
            "started_at",
            "finished_at",
            "detail",
            "produced",
            "superseded_by",
            "error",
            "error_type",
        }


# ============================================================================
# Tier 2 — the pipeline actually writes it
# ============================================================================


class MockEmbeddingService:
    """Deterministic mock: hashes text to produce a reproducible embedding."""

    def __init__(self, dim=768):
        self.dim = dim

    def embed_text(self, text, normalize=True, prefix=""):
        rng = np.random.default_rng(abs(hash(text)) % (2**31))
        vec = rng.standard_normal(self.dim).astype(np.float32)
        vec /= np.linalg.norm(vec)
        return vec

    def embed_with_tokens(self, text, top_percent=0.35, token_selector=None, prefix=""):
        doc_emb = self.embed_text(text)
        words = (text.split() or ["empty"])[:3]
        token_embs = []
        for i, w in enumerate(words):
            rng = np.random.default_rng(abs(hash(f"{text}_{i}")) % (2**31))
            tok_emb = rng.standard_normal(self.dim).astype(np.float32)
            tok_emb /= np.linalg.norm(tok_emb)
            token_embs.append(
                TokenEmbeddingResult(
                    token_idx=i,
                    token_text=w,
                    importance_score=1.0 - i * 0.2,
                    embedding=tok_emb,
                )
            )
        return EmbeddingResult(document_embedding=doc_emb, token_embeddings=token_embs)

    def truncate_embedding(self, embedding, target_dim, normalize=True):
        trunc = embedding[:target_dim].copy()
        if normalize:
            n = np.linalg.norm(trunc)
            if n > 0:
                trunc /= n
        return trunc


# THE LOCAL `db_session` FIXTURE WAS HERE, and it leaked. It was a plain session with a
# nested SAVEPOINT, so `session.commit()` committed for real — which nothing in this file
# used to do, because it called `execute_pipeline` and that only flushes. SPRINT_JOBS.md
# 15.4 S5 routed these tests through `IngestService`, which commits the file node before
# the queue can see it, and the committed rows then outlived the test and were claimed by
# whatever ran next.
#
# `tests/conftest.py`'s `db_session` is the one that contains a commit: it binds the
# session to a connection-level transaction with `join_transaction_mode="create_savepoint"`,
# so an endpoint's commit releases a savepoint inside a transaction the fixture rolls back.


@pytest.fixture
def mock_embedding():
    if not _DB_AVAILABLE:
        pytest.skip("Database not available")
    svc = MockEmbeddingService()
    with patch("jmfts_core.repositories.document.get_embedder", return_value=svc):
        yield svc


# TestAttemptsArePersisted WAS HERE. SPRINT_JOBS.md 15.4 S7 moved `conversation` onto the
# ingest queue, and with it the last entry point `execute_pipeline` served that takes a
# content STRING — the three `wiki:` usetypes left take a URL, an arXiv id or a local path,
# so there is no way to drive `_record_attempts` from a test without a network or a
# fixture file, and S8 deletes all three anyway.
#
# What the class asserted was that the attempt log is really written to the row rather than
# into a mock: one entry per stage that ran, timestamps that are real and UTC, `produced`
# carrying the child ids, `params`/`param_fingerprint` tracking the resolved config, and a
# skipped stage carrying its reason. The QUEUE's attempt log is a different mechanism — one
# entry per TASK, written by `TaskQueueRepository.complete`/`fail` — and every one of those
# properties is asserted about it in `tests/test_ingest_worker.py` and
# `tests/test_file_upload.py`.
#
# The Tier 1 half of this file is unaffected: `AttemptRecord` and `param_fingerprint` are
# contracts in `jmfts-client` and outlive both pipelines.
