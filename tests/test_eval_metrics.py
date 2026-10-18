"""``jmfts-eval``'s pure parts: metrics, the method grammar, the file reader, the planner.

No database and no model. The metric values are computed by hand in the comments, so a
change to a formula has to change a comment to pass — which is the point: the README's
table is only as good as these six functions.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from jmfts_core.eval import beir_loader as loader
from jmfts_core.eval.beir_config import DATA_DIR_ENV, DataDirNotConfigured, resolve_data_dir
from jmfts_core.eval.beir_evaluate import parse_method
from jmfts_core.eval.cli import code_identity, code_tag, main
from jmfts_core.eval.metrics import (
    MetricAccumulator,
    average_precision,
    dcg_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

# One query. Graded: c is 2, a and e are 1, z was judged and found NOT relevant (0).
QREL = {"a": 1, "c": 2, "e": 1, "z": 0}
RETRIEVED = ["a", "b", "c", "d"]


# =============================================================================
# Metrics
# =============================================================================


def test_dcg_is_linear_gain_over_log2_rank_plus_one():
    # 1/log2(2) + 0 + 2/log2(4) + 0 = 1 + 1 = 2
    assert dcg_at_k([1, 0, 2, 0], 10) == pytest.approx(2.0)
    assert dcg_at_k([1, 0, 2, 0], 1) == pytest.approx(1.0)


def test_ndcg_against_the_ideal_ordering():
    # DCG = 2 (above). Ideal grades [2, 1, 1, 0]: 2/1 + 1/log2(3) + 1/log2(4) = 3.13093
    assert ndcg_at_k(RETRIEVED, QREL, 10) == pytest.approx(0.6387878864795979)
    assert ndcg_at_k(["c", "a", "e"], QREL, 10) == pytest.approx(1.0)


def test_binary_metrics_count_only_positive_grades():
    # Relevant = {a, c, e}; top 10 holds a and c.
    assert precision_at_k(RETRIEVED, QREL, 10) == pytest.approx(0.2)  # 2 / k, not 2 / 4
    assert recall_at_k(RETRIEVED, QREL, 10) == pytest.approx(2 / 3)
    # AP: hits at ranks 1 and 3 -> (1/1 + 2/3) / min(3, 10)
    assert average_precision(RETRIEVED, QREL, 10) == pytest.approx(0.5555555555555555)
    assert reciprocal_rank(RETRIEVED, QREL, 10) == pytest.approx(1.0)


def test_a_judged_zero_is_not_a_hit():
    """The 200c5fe fix: membership in the qrels is not relevance."""
    assert reciprocal_rank(["z", "b", "c"], QREL, 10) == pytest.approx(1 / 3)
    assert precision_at_k(["z"], QREL, 10) == 0.0
    assert recall_at_k(["z"], QREL, 10) == 0.0
    assert ndcg_at_k(["z"], QREL, 10) == 0.0


def test_a_query_with_no_positive_judgment_scores_zero_everywhere():
    qrel = {"z": 0}
    for fn in (ndcg_at_k, recall_at_k, average_precision, reciprocal_rank, precision_at_k):
        assert fn(["z", "a"], qrel, 10) == 0.0


def test_cutoffs_look_only_at_the_first_k():
    assert reciprocal_rank(["b", "d", "a"], QREL, 2) == 0.0
    assert recall_at_k(["b", "d", "a"], QREL, 2) == 0.0


def test_accumulator_means_and_withholds_recall_at_100_below_depth_100():
    acc = MetricAccumulator(depth=100)
    acc.add(["c", "a", "e"], QREL, 10.0)  # perfect: every metric at its max for this query
    acc.add(["b"], QREL, 30.0)  # nothing
    m = acc.result()
    assert m.ndcg_at_10 == pytest.approx(0.5)
    assert m.recall_at_10 == pytest.approx(0.5)
    assert m.recall_at_100 == pytest.approx(0.5)
    assert m.precision_at_10 == pytest.approx((3 / 10) / 2)
    assert m.avg_latency_ms == pytest.approx(20.0)
    assert m.queries_evaluated == 2

    shallow = MetricAccumulator(depth=10)
    shallow.add(["c"], QREL, 1.0)
    assert shallow.result().recall_at_100 is None


def test_accumulator_refuses_to_average_nothing():
    with pytest.raises(ValueError):
        MetricAccumulator(depth=100).result()


# =============================================================================
# Method grammar
# =============================================================================


def test_single_stage_methods():
    assert parse_method("vector").stage1 == "vector"
    assert not parse_method("bm25").rerank
    hybrid = parse_method("hybrid")
    assert (hybrid.stage1, hybrid.hybrid_methods, hybrid.hybrid_weights) == ("hybrid", None, None)


def test_rrf_is_an_explicit_empty_weight_dict_not_none():
    rrf = parse_method("rrf")
    assert rrf.stage1 == "hybrid"
    assert rrf.hybrid_weights == {}  # None would be the tuned default
    assert rrf.hybrid_methods == ("vector", "bm25")


def test_weighted_hybrid():
    m = parse_method("hybrid_v70b30")
    assert m.hybrid_weights == {"vector": 0.7, "bm25": 0.3}


def test_two_stage_crossenc():
    m = parse_method("vector→crossenc@100")
    assert (m.stage1, m.rerank_method, m.rerank_depth, m.rerank_tier) == (
        "vector",
        "crossenc",
        100,
        None,
    )
    assert m.name == "vector→crossenc@100"


def test_two_stage_maxsim_with_tier_and_ascii_arrow():
    m = parse_method("hybrid_v60b40->maxsim@200:t15")
    assert m.name == "hybrid_v60b40→maxsim@200:t15"  # recorded with the canonical arrow
    assert (m.stage1, m.rerank_method, m.rerank_depth, m.rerank_tier) == (
        "hybrid",
        "maxsim",
        200,
        15,
    )
    assert parse_method("vector→maxsim").rerank_depth == 100


@pytest.mark.parametrize(
    "bad",
    [
        "vectr",  # unknown first stage
        "hybrid_v70",  # half a weight spec
        "vector→",  # nothing after the arrow
        "vector→bm25",  # bm25 is not a reranker
        "vector→maxsim@200junk",  # used to parse as maxsim@200
        "vector→crossenc@100:t5",  # tiers are maxsim's
        "vector→maxsim@0",
    ],
)
def test_malformed_methods_are_refused_at_parse_time(bad):
    with pytest.raises(ValueError):
        parse_method(bad)


# =============================================================================
# Files, download, planning
# =============================================================================


def write_dataset(root, name="scifact", *, corpus=None, queries=None, qrels=None, header=True):
    """A BEIR-format directory. Used by the smoke test too."""
    d = root / name
    (d / "qrels").mkdir(parents=True)
    corpus = corpus or {"d1": ("T1", "first text body"), "d2": ("", "second text body")}
    queries = queries or {"q1": "first", "q2": "unjudged"}
    qrels = qrels or [("q1", "d1", 1)]
    (d / "corpus.jsonl").write_text(
        "".join(
            json.dumps({"_id": k, "title": t, "text": x}) + "\n" for k, (t, x) in corpus.items()
        )
    )
    (d / "queries.jsonl").write_text(
        "".join(json.dumps({"_id": k, "text": v}) + "\n" for k, v in queries.items())
    )
    lines = ["query-id\tcorpus-id\tscore\n"] if header else []
    lines += [f"{q}\t{c}\t{s}\n" for q, c, s in qrels]
    (d / "qrels" / "test.tsv").write_text("".join(lines))
    return d


def test_reader_and_evaluable_queries(tmp_path):
    path = write_dataset(tmp_path)
    files = loader.read_beir_dataset(path, "scifact", "test")
    assert files.corpus["d1"] == {"title": "T1", "text": "first text body"}
    assert files.evaluated_queries == {"q1": "first"}  # q2 has no judgment
    assert set(files.sha256) == {"corpus.jsonl", "queries.jsonl", "qrels/test.tsv"}
    assert loader.doc_content(files.corpus["d1"]) == "T1\n\nfirst text body"
    assert loader.doc_content(files.corpus["d2"]) == "second text body"


def test_reader_refuses_a_qrels_file_without_its_header(tmp_path):
    path = write_dataset(tmp_path, header=False)
    with pytest.raises(loader.MalformedDataset):
        loader.read_beir_dataset(path, "scifact", "test")


def test_reader_refuses_a_short_qrels_line(tmp_path):
    path = write_dataset(tmp_path)
    with open(path / "qrels" / "test.tsv", "a") as fh:
        fh.write("q1\td2\n")
    with pytest.raises(loader.MalformedDataset, match="line 3"):
        loader.read_beir_dataset(path, "scifact", "test")


def _zip_of(tmp_path, members: dict[str, str]):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    src = tmp_path / "mirror"
    src.mkdir()
    (src / "scifact.zip").write_bytes(buf.getvalue())
    return f"file://{src}/{{name}}.zip"


def test_download_unpacks_into_place(tmp_path):
    template = _zip_of(
        tmp_path,
        {
            "scifact/corpus.jsonl": json.dumps({"_id": "d1", "title": "", "text": "x" * 20}),
            "scifact/queries.jsonl": json.dumps({"_id": "q1", "text": "x"}),
            "scifact/qrels/test.tsv": "query-id\tcorpus-id\tscore\nq1\td1\t1\n",
        },
    )
    data = tmp_path / "data"
    path = loader.ensure_dataset(data, "scifact", url_template=template, log=lambda _: None)
    assert path == data / "scifact"
    assert (path / "corpus.jsonl").is_file()
    assert [p.name for p in data.iterdir()] == ["scifact"]  # no temp dir left behind


def test_download_refuses_a_zip_that_escapes(tmp_path):
    template = _zip_of(tmp_path, {"../evil.txt": "x", "scifact/corpus.jsonl": "{}"})
    with pytest.raises(loader.MalformedDataset, match="escapes"):
        loader.ensure_dataset(tmp_path / "data", "scifact", url_template=template, log=print)
    assert not (tmp_path / "evil.txt").exists()


def test_data_dir_has_no_default(monkeypatch, tmp_path):
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    with pytest.raises(DataDirNotConfigured):
        resolve_data_dir(None)
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    assert resolve_data_dir(None) == tmp_path
    assert resolve_data_dir("/elsewhere") == Path("/elsewhere")


def _fit_by_words(limit):
    def check_fit(text):
        n = len(text.split())
        return SimpleNamespace(token_count=n, limit=limit, truncated=n > limit)

    return check_fit


def _planning_files(tmp_path):
    corpus = {
        "short": ("", "a b c d e f g h i j k l"),
        "long": ("", " ".join(["w"] * 50)),
        "tiny": ("", "x"),
    }
    path = write_dataset(tmp_path, corpus=corpus, qrels=[("q1", "long", 2), ("q1", "short", 0)])
    return loader.read_beir_dataset(path, "scifact", "test")


def test_over_window_error_writes_nothing_and_names_the_way_out(tmp_path):
    files = _planning_files(tmp_path)
    with pytest.raises(loader.OverWindowError, match="--over-window skip") as info:
        loader.plan_ingest(
            files, over_window="error", check_fit=_fit_by_words(20), log=lambda _: None
        )
    assert info.value.over == [("long", 50)]


def test_over_window_skip_counts_and_never_truncates(tmp_path):
    files = _planning_files(tmp_path)
    plan = loader.plan_ingest(
        files, over_window="skip", check_fit=_fit_by_words(20), log=lambda _: None
    )
    assert [beir_id for beir_id, _, _ in plan.to_ingest] == ["short"]
    assert plan.skipped_over_window == ["long"]
    assert plan.skipped_empty == ["tiny"]
    assert plan.token_window == 20


def test_unknown_over_window_policy_is_refused(tmp_path):
    with pytest.raises(ValueError):
        loader.plan_ingest(
            _planning_files(tmp_path), over_window="truncate", check_fit=_fit_by_words(20)
        )


# =============================================================================
# CLI surface that touches no database
# =============================================================================


def test_list_needs_no_data_dir(capsys):
    assert main(["beir", "--list"]) == 0
    assert "trec-covid" in capsys.readouterr().out


def test_run_without_a_data_dir_is_refused(monkeypatch, capsys):
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    with pytest.raises(SystemExit) as info:
        main(["beir", "--dataset", "scifact"])
    assert info.value.code == 2
    assert DATA_DIR_ENV in capsys.readouterr().err


def test_a_bad_method_is_refused_before_anything_runs(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["beir", "-d", "scifact", "--data-dir", str(tmp_path), "--methods", "vectr"])
    assert "vectr" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_code_identity_names_a_commit_or_a_version():
    identity = code_identity()
    tag = code_tag(identity)
    if identity["commit"]:
        assert len(identity["commit"]) == 40 and tag.startswith(identity["commit"][:7])
    else:
        assert tag == f"v{identity['version']}"
