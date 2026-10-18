"""``jmfts-eval beir`` end to end on a ten-document BEIR-format corpus written in the test.

Read → plan (real tokenizer) → ingest through ``DocumentRepository`` → BM25 index → query
with ``vector``, ``bm25`` and ``vector→maxsim`` → metrics. Then the reload ``--eval-only``
uses, the wipe, and the shared-database refusal. Nothing is downloaded and nothing ships:
the corpus is the dict below.

Each query is a paraphrase-free restatement of exactly one document, so every method
should put that document first and nDCG@10 is 1.0. That is a smoke test's claim, not a
quality claim: it says the wiring from BEIR ids to JMFTS ids and back is right, which is
the part a metric cannot check about itself.
"""

from __future__ import annotations

import pytest

from jmfts_core.embedder import get_embedder
from jmfts_core.eval import beir_loader as loader
from jmfts_core.eval.beir_evaluate import evaluate_method, parse_method
from jmfts_core.repositories.document import DocumentRepository
from tests.test_eval_metrics import write_dataset

CORPUS = {
    "c1": ("Volcanoes", "Basaltic lava flows erupt from shield volcanoes in Hawaii."),
    "c2": ("Bread", "Sourdough bread rises slowly because wild yeast ferments the dough."),
    "c3": ("Chess", "The Sicilian Defence is a sharp reply to the king pawn opening in chess."),
    "c4": ("Bees", "Honey bees communicate the location of flowers with a waggle dance."),
    "c5": ("Tides", "Ocean tides are driven by the gravitational pull of the moon."),
    "c6": ("Copper", "Copper wire conducts electricity with very low resistance."),
    "c7": ("Glaciers", "Glaciers carve U-shaped valleys as they grind across bedrock."),
    "c8": ("Violins", "Stradivarius violins are prized for their resonant spruce tops."),
    "c9": ("Penguins", "Emperor penguins huddle together to survive the Antarctic winter."),
    "c10": ("Rust", "Iron rusts when it reacts with oxygen and water to form iron oxide."),
    "c11": ("Empty", "   "),  # under MIN_CONTENT_CHARS: left out and counted
    "c12": ("Long", " ".join(["filibuster"] * 700)),  # over the 512-token window
}
QUERIES = {
    "q1": "waggle dance of honey bees communicating flower locations",
    "q2": "gravitational pull of the moon drives ocean tides",
    "q3": "iron reacts with oxygen and water to form rust",
    "q4": "an unjudged query",
}
QRELS = [("q1", "c4", 1), ("q2", "c5", 2), ("q2", "c6", 0), ("q3", "c10", 1), ("q3", "c12", 1)]


@pytest.fixture
def files(tmp_path):
    write_dataset(tmp_path, "scifact", corpus=CORPUS, queries=QUERIES, qrels=QRELS)
    return loader.load_files(tmp_path, "scifact", log=lambda _: None)


def _check_fit(text):
    return get_embedder().check_fit(text, with_tokens=True, prefix=loader.DOCUMENT_PREFIX)


def _ingest(session, files):
    plan = loader.plan_ingest(files, over_window="skip", check_fit=_check_fit, log=print)
    return loader.ingest_dataset(
        session,
        files,
        plan,
        over_window="skip",
        provenance={"version": "test", "commit": None, "dirty": None},
        embedding_model=get_embedder().model_name,
        commit_every=4,
        log=print,
    )


def test_over_window_error_is_raised_before_any_write(db_session, files):
    with pytest.raises(loader.OverWindowError, match="1 of 12"):
        loader.plan_ingest(files, over_window="error", check_fit=_check_fit, log=print)


def test_ingest_query_and_score_end_to_end(db_session, files):
    loader.refuse_shared_database(db_session, allow_shared_db=False)
    data = _ingest(db_session, files)

    assert set(data.id_map) == {f"c{i}" for i in range(1, 11)}
    record = data.record
    assert record["over_window_skipped"] == 1
    assert record["over_window_ids"] == ["c12"]
    assert record["over_window_skipped_relevant"] == 1  # c12 is judged relevant to q3
    assert record["empty_ids"] == ["c11"]
    assert record["ingested_docs"] == 10
    assert record["token_window"] == 512

    for name in ("vector", "bm25", "vector→maxsim@10"):
        outcome = evaluate_method(
            db_session, parse_method(name), files, data, depth=10, progress_every=0
        )
        m = outcome.metrics
        assert m.queries_evaluated == 3, name  # q4 has no judgment
        assert m.recall_at_100 is None  # depth 10
        # q1 and q2 each have their one relevant document first: nDCG 1.0. q3 finds c10
        # first, but its other relevant document (c12) was skipped as over-window, so its
        # recall is 1/2 and its ideal DCG counts the missing one — the skip costs score, it
        # is not hidden.
        q3_ndcg = 1.0 / (1.0 + 1.0 / 1.584962500721156)
        assert m.ndcg_at_10 == pytest.approx((1.0 + 1.0 + q3_ndcg) / 3), name
        assert m.recall_at_10 == pytest.approx((1.0 + 1.0 + 0.5) / 3), name
        assert m.mrr_at_10 == pytest.approx(1.0), name


def test_eval_only_reload_wipe_and_shared_db_refusal(db_session, files):
    data = _ingest(db_session, files)

    again = loader.load_ingested(db_session, "scifact")
    assert again.root_id == data.root_id
    assert again.id_map == data.id_map
    loader.check_files_match(again.record, files)

    assert loader.wipe_dataset(db_session, "scifact", log=print) == 1
    with pytest.raises(loader.NotIngested):
        loader.load_ingested(db_session, "scifact")

    DocumentRepository(db_session).create(title="not beir", content="x", auto_embed=False)
    db_session.flush()
    with pytest.raises(loader.SharedDatabaseRefused):
        loader.refuse_shared_database(db_session, allow_shared_db=False)
    loader.refuse_shared_database(db_session, allow_shared_db=True)
