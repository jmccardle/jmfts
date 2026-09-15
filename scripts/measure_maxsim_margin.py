#!/usr/bin/env python3
"""How far `tests/test_maxsim_recall.py`'s reading moves when nothing changes.

`docs/SPRINT_0_6_0.md` step 8 and question 4.4. The test asserts that the mean of 24
per-query `recall@10` values clears 0.90; public CI read `21.5/24 = 0.8958` on 0.5.0 and
this machine has never read below `217/240 = 0.9042`. Three accounts are on the table —
the corpus is too small, the index loses a neighbour under a condition CI has, or 0.90 is
the wrong number for a 24-query fixture — and **none of them can be told apart from a
reading.** They need a distribution. This takes one.

    JMFTS_EMBEDDING_DEVICE=cpu python -m scripts.measure_maxsim_margin \\
        --dsn postgresql://jmfts:jmfts@localhost:5496/postgres --trials 60

**What varies between trials, and what cannot.** The fixture's text is generated from two
fixed seeds (`tests/maxsim_corpus.py`, `CORPUS_SEED` and `QUERY_SEED`), so the corpus and
the queries are the same bytes every time; the model is deterministic for a fixed device
and thread count, so the vectors are too. What is left is the **HNSW graph**: pgvector
draws each element's level from the backend's PRNG at insert (`ANN_INDEX_HEALTH.md` 1.4 —
*"the same cell at the same parameters has read 6/20 and 15/20 across sweeps"*), so every
build of the index over the same rows is a different graph. That is the one term this
script resamples, and it resamples it the way the appliance produces it: `TRUNCATE` and
re-`INSERT` the same rows through the live index, never `REINDEX`, because a rebuild is a
different construction (`ANN_INDEX_HEALTH.md` 5.7 priced the two at 0.987 against 0.998).

**Which is why the model runs once.** Embedding 200 chunks costs about a minute of CPU and
produces the same vectors every time; paying it per trial would buy nothing and hold the
sample size down to what a person is willing to wait for. Ground truth is taken once for
the same reason and for a stronger one: it is an exact scan with every index path denied,
so it is a property of the rows and the query alone and no graph can move it. `--verify`
recomputes it at the end and fails if any truth list changed, which is the check that this
shortcut is sound rather than the assumption that it is.

**Two arms, because one is not enough.** `gated` asks as a non-owner whose grant makes
`readable_sql` emit a fragment and `open` asks as the owner, for whom none is built. That
is `tests/test_maxsim_recall.py`'s own control and the reason `4dadb62`'s reading survived:
an ANN index is approximate before any filter, and a gated shortfall cannot be attributed
to the gate unless the ungated arm is measured beside it.

**This measures. It changes nothing and recommends nothing.** In particular it does not
pick a threshold: a number chosen to sit under the lowest reading a sample happened to take
is a number that will be wrong the next time the sample is larger.

`docs/MEASURE_MAXSIM_MARGIN.md` is the run this produced — 79 readings, the variance
decomposition behind "more queries works", and what about public CI could not be
reproduced.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

#: `tests/test_maxsim_recall.py` CHUNKS, QUERIES, LIMIT. Repeated rather than imported at
#: module scope because importing the tests package pulls in `tests/conftest.py`, which
#: provisions a database of its own at import time; `run()` imports the corpus helpers
#: after the environment points at the database this script made.
CHUNKS = 200
QUERIES = 24
LIMIT = 10
#: The threshold under measurement, and the floor the tests actually compare against
#: (`tests/test_maxsim_recall.py`, MINIMUM_RECALL and RECALL_FLOOR).
MINIMUM_RECALL = 0.90
RECALL_FLOOR = 0.8999999999999996 - 1e-15


def _psql(dsn: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["psql", "-d", dsn, "-v", "ON_ERROR_STOP=1", "-Xq", *args],
        capture_output=True,
        text=True,
    )


def _provision(dsn: str, dbname: str) -> str:
    """Drop, recreate and load `schema.sql` — the appliance's own DDL, in its own order.

    ON AN EMPTY TABLE, which is how `jmfts-init-db` builds `idx_token_embed_256_hnsw` and
    therefore the only build order a measurement of the shipped configuration may use.
    """
    for statement in (f'DROP DATABASE IF EXISTS "{dbname}"', f'CREATE DATABASE "{dbname}"'):
        done = _psql(dsn, "-c", statement)
        if done.returncode:
            raise SystemExit(f"{statement}: {done.stderr.strip()}")
    target = dsn.rsplit("/", 1)[0] + "/" + dbname
    done = _psql(target, "-f", str(_REPO_ROOT / "jmfts_core" / "sql" / "schema.sql"))
    if done.returncode:
        raise SystemExit(f"schema.sql: {done.stderr.strip()}")
    return target


def _point_env_at(target: str) -> None:
    """Set `JMFTS_DB_*` from a DSN, BEFORE `jmfts_core` is imported anywhere."""
    rest = target.split("://", 1)[1]
    creds, hostpart = rest.split("@", 1)
    user, _, password = creds.partition(":")
    hostport, _, dbname = hostpart.partition("/")
    host, _, port = hostport.partition(":")
    os.environ.update(
        JMFTS_DB_HOST=host,
        JMFTS_DB_PORT=port or "5432",
        JMFTS_DB_USER=user,
        JMFTS_DB_PASSWORD=password,
        JMFTS_DB_NAME=dbname,
        JMFTS_INGEST_WORKER_ENABLED="0",
    )


def _tenths(recalls: list[float]) -> int:
    """The mean as an exact integer count of documents out of `10 * len(recalls)`.

    Every `recall@10` is a multiple of 1/10, so the mean is a multiple of 1/240 at 24
    queries and the float is a lossy rendering of an integer. Reporting the integer is what
    makes "one document short" a statement rather than an interpretation — and it is the
    arithmetic behind `RECALL_FLOOR`.
    """
    return sum(round(r * 10) for r in recalls)


def run(dsn, dbname, chunks, n_queries, trials, verify, dump):
    target = _provision(dsn, dbname)
    _point_env_at(target)

    from sqlalchemy import text as sa_text

    from jmfts_core.database import get_engine, get_session_factory
    from jmfts_core.principal_context import reset_principal, set_principal
    from jmfts_core.repositories.search import SearchRepository
    from tests.maxsim_corpus import (
        EF_SEARCH,
        TOKEN_INDEX,
        ann_maxsim,
        ann_plan_reaches_the_index,
        exact_maxsim,
        query_texts,
        recall,
        seed_split_corpus,
    )

    session = get_session_factory()(bind=get_engine())
    server = session.execute(sa_text("SELECT current_setting('server_version')")).scalar()
    vector = session.execute(
        sa_text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    ).scalar()
    print(f"# postgres {server}, pgvector {vector}, hnsw.ef_search {EF_SEARCH} (pgvector's)")
    print(f"# seeding {chunks} chunks with the real model — paid once, not per trial")

    fixture = seed_split_corpus(session, chunks)
    corpus = fixture.corpus
    session.commit()
    print(
        f"# {corpus.token_rows} token rows over {len(corpus.document_ids)} documents "
        f"in {corpus.seconds:.0f}s"
    )
    if not ann_plan_reaches_the_index(session, corpus):
        raise SystemExit(
            f"the planner does not read {TOKEN_INDEX} at {corpus.token_rows} rows; "
            "nothing below would be a measurement of the index. Raise --chunks."
        )

    queries = query_texts(n_queries)
    repo = SearchRepository(session)

    # Ground truth: an exact scan with every index path denied, so it is a property of the
    # rows and the query and no graph rebuild below can move it. `--verify` checks that.
    truth: dict[str, dict[str, list[int]]] = {}
    for arm, principal in fixture.arms.items():
        token = set_principal(principal)
        try:
            truth[arm] = {q: exact_maxsim(repo, q, LIMIT) for q in queries}
        finally:
            reset_principal(token)
    session.commit()

    # The rows, kept server-side. A trial is TRUNCATE + re-INSERT of exactly these, which
    # is how the appliance writes them: one row at a time through a live HNSW index. Never
    # REINDEX — that is a different construction (`ANN_INDEX_HEALTH.md` 5.7).
    session.execute(sa_text("CREATE TEMP TABLE saved_tokens AS SELECT * FROM token_embeddings"))
    session.commit()

    print(
        f"\n{'trial':>5s} {'gated':>9s} {'gated/240':>10s} {'open':>9s} {'open/240':>9s} "
        f"{'short':>6s} {'verdict':>8s}"
    )
    rows = []
    for trial in range(trials):
        if trial:
            session.execute(sa_text("TRUNCATE token_embeddings"))
            session.execute(sa_text("INSERT INTO token_embeddings SELECT * FROM saved_tokens"))
            session.execute(sa_text("ANALYZE token_embeddings"))
            session.commit()
        measured = {}
        for arm, principal in fixture.arms.items():
            token = set_principal(principal)
            try:
                measured[arm] = [
                    recall(ann_maxsim(repo, q, LIMIT, ef_search=None), truth[arm][q])
                    for q in queries
                ]
            finally:
                reset_principal(token)
        session.commit()
        gated, opened = measured["gated"], measured["open"]
        gated_mean, open_mean = sum(gated) / len(gated), sum(opened) / len(opened)
        verdict = "PASS" if gated_mean >= RECALL_FLOOR else "FAIL"
        print(
            f"{trial:5d} {gated_mean:9.4f} {_tenths(gated):6d}/{10 * len(gated):<3d} "
            f"{open_mean:9.4f} {_tenths(opened):5d}/{10 * len(opened):<3d} "
            f"{sum(1 for r in gated if r < 1.0):3d}/{len(gated):<2d} {verdict:>8s}",
            # A trial is minutes on a loaded machine and this is the only sign of life;
            # redirected to a file, Python block-buffers and the run looks hung for an hour.
            flush=True,
        )
        rows.append({"trial": trial, "gated": gated, "open": opened})

    if verify:
        again = {}
        for arm, principal in fixture.arms.items():
            token = set_principal(principal)
            try:
                again[arm] = {q: exact_maxsim(repo, q, LIMIT) for q in queries}
            finally:
                reset_principal(token)
        moved = [
            (arm, q)
            for arm in truth
            for q in truth[arm]
            if set(truth[arm][q]) != set(again[arm][q])
        ]
        if moved:
            raise SystemExit(
                f"ground truth moved for {len(moved)} (arm, query) pairs across the run, so "
                "the shortcut this script takes — one exact scan reused by every trial — is "
                f"not sound here. First: {moved[0]}"
            )
        print(f"\n# --verify: ground truth unchanged for all {n_queries * 2} (arm, query) pairs")

    _report(rows, n_queries)
    if dump:
        Path(dump).write_text(json.dumps(rows, indent=1))
        print(f"\n# per-query recalls written to {dump}")
    session.close()
    return 0


def _report(rows, n_queries):
    """The distribution, in the unit the assertion is written in: documents out of 240."""
    gated = [_tenths(r["gated"]) for r in rows]
    opened = [_tenths(r["open"]) for r in rows]
    scale = 10 * n_queries
    floor_tenths = scale * MINIMUM_RECALL
    print(f"\n## {len(rows)} trials, one HNSW graph each, identical rows and identical queries")
    for name, series in (("gated", gated), ("open", opened)):
        print(
            f"{name:>6s}  min {min(series)}/{scale}  max {max(series)}/{scale}  "
            f"mean {statistics.mean(series):.2f}  median {statistics.median(series)}  "
            f"sd {statistics.pstdev(series):.2f}"
        )
    print(f"\n{'gated/' + str(scale):>10s} {'recall':>8s} {'trials':>7s}")
    for value, count in sorted(Counter(gated).items()):
        mark = " <- below 0.90" if value < floor_tenths else ""
        print(f"{value:10d} {value / scale:8.4f} {count:7d}  {'#' * count}{mark}")
    below = sum(1 for v in gated if v < floor_tenths)
    print(
        f"\n# {below}/{len(gated)} trials read below {MINIMUM_RECALL}; the lowest is "
        f"{min(gated)}/{scale} = {min(gated) / scale:.4f}, which is "
        f"{floor_tenths - min(gated):.0f} documents from the threshold"
    )
    # Which queries lose a neighbour, and whether it is the same ones. A fixed set says the
    # corpus has hard queries in it; a moving set says the graph decides per build.
    per_query = [sum(1 for r in rows if r["gated"][i] < 1.0) for i in range(len(rows[0]["gated"]))]
    print(f"\n# per query, trials (of {len(rows)}) in which the gated arm lost a neighbour:")
    print("#   " + " ".join(f"{c:2d}" for c in per_query))
    print(
        f"#   always short: {sum(1 for c in per_query if c == len(rows))}, "
        f"never short: {sum(1 for c in per_query if c == 0)}, "
        f"sometimes: {sum(1 for c in per_query if 0 < c < len(rows))}"
    )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dsn", required=True, help="DSN of a database this may CREATE DATABASE on")
    ap.add_argument("--dbname", default="jmfts_maxsim_margin")
    ap.add_argument("--chunks", type=int, default=CHUNKS)
    ap.add_argument("--queries", type=int, default=QUERIES)
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument(
        "--verify",
        action="store_true",
        help="recompute ground truth at the end and fail if any list moved",
    )
    ap.add_argument("--dump", help="write every trial's per-query recalls to this JSON file")
    args = ap.parse_args()
    return run(
        args.dsn,
        args.dbname,
        args.chunks,
        args.queries,
        args.trials,
        args.verify,
        args.dump,
    )


if __name__ == "__main__":
    sys.exit(main())
