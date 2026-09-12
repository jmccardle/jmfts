#!/usr/bin/env python3
"""What the MaxSim ANN setting costs and buys, measured through the real `maxsim_search`.

**THIS FILE SWEPT `ivfflat.probes` UNTIL MIGRATION 022 AND NOW SWEEPS `hnsw.ef_search`.**
`docs/ANN_INDEX_HEALTH.md` 5.3's table is the earlier run and its `ran` line names this
path; that table is history, and 5.8 is the decision that retired it. The name is kept
precisely so that citation still resolves. Everything below the setting — the provisioning,
the two controls, the two latency columns — is unchanged, because none of it was about
which index was underneath.

`4.3` and `docs/SPRINT_0_4_0.md` Block A both recorded the same candidate and the same gap:
nothing in `jmfts_core/` set `ivfflat.probes`, so every MaxSim ANN query ran at pgvector's
default of one probe over `lists = 1024` — and the entry condition for making that a step
was a failing test through `maxsim_search`, which `scripts/maxsim_recall.py` is not, because
it measures throwaway tables shaped like the appliance's rather than the appliance's own.

This is the other half of `tests/test_maxsim_recall.py`. The test asserts; this reports the
table the assertion was chosen against. Both stand on `tests/maxsim_corpus.py`, so the two
cannot describe different corpora.

    JMFTS_EMBEDDING_DEVICE=cpu python -m scripts.maxsim_probes \\
        --dsn postgresql://jmfts:jmfts@localhost:5444/postgres --chunks 600

**The appliance's schema, not a copy of it.** The target database is dropped, recreated and
loaded from `jmfts_core/sql/schema.sql`, so `idx_token_embed_256_hnsw` is built exactly as
`jmfts-init-db` builds it: ON AN EMPTY TABLE, before a single row exists. That used to be
the whole subject. IVFFlat fixed its centroids at build time and pgvector said so at that
moment ("ivfflat index created with little data ... This will cause low recall"); the
`--reindex` arm is what that warning was worth in recall, and no prior measurement could see
it, because `scripts/maxsim_recall.py` and `docs/STRESS_CORPUS.md` 6.2 both built the index
AFTER loading their rows, which is the favourable order and not the shipped one.

**`--reindex` IS NOW A CONTROL RATHER THAN AN ARM, and that is the point of keeping it.**
An HNSW graph is built by insertion, so rebuilding it on the loaded rows should produce the
same index; 5.7 measured that on a synthetic corpus and read 0.987 against 0.998 at
`ef_search = 40`, closing entirely at 100. Running this flag on the appliance's own tables
and real vectors is how that claim gets checked here rather than assumed. A large gap
between the two states would mean 5.8 decided on a property HNSW does not have.

**Two controls, because one is not enough.** That discipline is `scripts/maxsim_recall.py`'s
and it is kept here:

`exact`
    Ground truth is the same `maxsim_search` call with every index path denied, on the same
    rows, in the same transaction, with the same query embedding. A shortfall is therefore
    ANN-against-exact and never a judgement about what the right answer was.
`open`
    The same query asked as the OWNER, for whom `readable_sql` returns None and no ACL
    fragment is built at all. Without it a gated recall of 0.5 cannot be told apart from the
    index's own loss, and the read gate would take credit for it. `4dadb62` is the reason
    the column exists: the ungated scan there was already losing about half its true
    neighbours before any filter existed.

**Latency is per SEARCH, not per ANN page, and it is reported twice.** `maxsim_search`
issues one ANN query per content token of the query (`repositories/search.py:1068`), so
what a caller pays for `ef_search = 400` is that candidate budget times however many tokens
the query has; a per-page number would understate it by that factor. But the same call also embeds
the query with the model, and on CPU that dominates everything and would hide the thing
being measured. So `ann ms` is the summed server time of exactly the statements
`maxsim_search` issued against `token_embeddings` — the part `ef_search` moves — and
`call ms` is the whole method, model included, which is what a caller waits for.

**One setting is swept and one is not.** `hnsw.ef_search` is swept because nothing in
`jmfts_core/` sets it, so every value here except pgvector's default is a hypothetical.
`hnsw.iterative_scan` is left alone because `maxsim_search` sets it on every call (migration
022), so it IS the shipped configuration and overriding it would report a number no caller
can get.
"""

import argparse
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

#: `hnsw.ef_search` values swept by default. 40 is pgvector's default and therefore the
#: appliance's; the rest bracket it. 10 is below the `LIMIT` `maxsim_search` issues
#: (`K_PER_TOKEN`, 100), and whether a budget smaller than the LIMIT changes anything is a
#: question about pgvector this sweep answers rather than assumes — hence the row.
DEFAULT_EF_SEARCH = "10,40,100,200,400,800"
DEFAULT_CHUNKS = 600
DEFAULT_QUERIES = 24
#: What a caller asks `maxsim_search` for. Recall is over the documents it hands back.
LIMIT = 10


def _psql(dsn: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["psql", "-d", dsn, "-v", "ON_ERROR_STOP=1", "-Xq", *args],
        capture_output=True,
        text=True,
    )


def _provision(dsn: str, dbname: str) -> str:
    """Drop, recreate and load `schema.sql` — `jmfts-init-db`'s own DDL, in its own order."""
    for statement in (f'DROP DATABASE IF EXISTS "{dbname}"', f'CREATE DATABASE "{dbname}"'):
        done = _psql(dsn, "-c", statement)
        if done.returncode:
            raise SystemExit(f"{statement}: {done.stderr.strip()}")
    target = dsn.rsplit("/", 1)[0] + "/" + dbname
    done = _psql(target, "-f", str(_REPO_ROOT / "jmfts_core" / "sql" / "schema.sql"))
    if done.returncode:
        raise SystemExit(f"schema.sql: {done.stderr.strip()}")
    # Kept after migration 022, and expected to print nothing now. This is where pgvector's
    # "ivfflat index created with little data ... This will cause low recall" used to
    # surface — the server volunteering the defect at the moment schema.sql caused it. HNSW
    # emits no such NOTICE because it has nothing to warn about, so silence here is the
    # finding rather than the check having been removed.
    for line in done.stderr.splitlines():
        if "little data" in line or "low recall" in line:
            print(f"# schema.sql said, at CREATE INDEX time: {line.strip()}")
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


def _median(values):
    return statistics.median(values) if values else float("nan")


class _AnnTimer:
    """How long the ANN statements took, and how long their pages were, per search.

    `maxsim_search` is one method that does two very different things — it embeds the query
    with the model, then issues one ANN statement per content token — and on CPU the first
    costs more than the second by an order of magnitude. Timing the method alone would
    therefore report the model and call it the index. This wraps `session.execute` and
    charges only the statements that name `token_embeddings` in an ANN `ORDER BY`, which is
    exactly the set `ef_search` moves.
    """

    MARKER = "ORDER BY te.embed_256"

    def __init__(self, repo):
        self.repo = repo
        self.searches: list[float] = []
        self.pages: list[tuple[int, int]] = []
        self._accrued = 0.0
        self._original_execute = None
        self._original_note = None

    def reset(self):
        self._accrued = 0.0

    def close_search(self):
        self.searches.append(self._accrued)

    def __enter__(self):
        session = self.repo.session
        self._original_execute = session.execute
        self._original_note = self.repo._note_ann_page

        def execute(statement, *args, **kwargs):
            if self.MARKER not in str(statement):
                return self._original_execute(statement, *args, **kwargs)
            started = time.monotonic()
            try:
                return self._original_execute(statement, *args, **kwargs)
            finally:
                self._accrued += (time.monotonic() - started) * 1000.0

        def note(returned, requested):
            self.pages.append((returned, requested))
            return self._original_note(returned, requested)

        session.execute = execute
        self.repo._note_ann_page = note
        return self

    def __exit__(self, *exc):
        self.repo.session.execute = self._original_execute
        self.repo._note_ann_page = self._original_note
        return False


def _ann_timer(repo):
    return _AnnTimer(repo)


def run(dsn, dbname, chunks, n_queries, ef_searches, reindex):
    target = _provision(dsn, dbname)
    _point_env_at(target)

    from sqlalchemy import text as sa_text

    from jmfts_core.database import get_engine, get_session_factory
    from jmfts_core.principal_context import reset_principal, set_principal
    from jmfts_core.repositories.search import SearchRepository
    from tests.maxsim_corpus import (
        EF_SEARCH,
        K_PER_TOKEN,
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

    print(f"# postgres {server}, pgvector {vector}")
    print(
        f"# k = {K_PER_TOKEN} per query token (search.py:1018); "
        f"hnsw.ef_search defaults to {EF_SEARCH} and jmfts_core sets none"
    )
    print(f"# seeding {chunks} chunks with the real model — this is the slow part")
    fixture = seed_split_corpus(session, chunks)
    corpus = fixture.corpus
    session.commit()
    print(
        f"# {corpus.token_rows} token rows over {len(corpus.document_ids)} documents in "
        f"{corpus.seconds:.0f}s, against a LIMIT of {K_PER_TOKEN} per query token"
    )
    principals = fixture.arms
    queries = query_texts(n_queries)
    repo = SearchRepository(session)

    states = [("as shipped", False)] + ([("REINDEXed after load", True)] if reindex else [])
    for state, do_reindex in states:
        if do_reindex:
            # The control described in this module's docstring. Under IVFFlat this arm was
            # the whole finding — centroids refitted to the rows, which `schema.sql` never
            # reaches on its own. Under HNSW it should read the same as "as shipped",
            # because a graph is built by insertion either way, and a gap here would say
            # that 5.8 decided on a property the index does not have.
            started = time.monotonic()
            session.execute(sa_text(f"REINDEX INDEX {TOKEN_INDEX}"))
            session.commit()
            print(f"\n# REINDEX {TOKEN_INDEX}: {time.monotonic() - started:.1f}s")

        if not ann_plan_reaches_the_index(session, corpus):
            print(f"\n# {state}: the planner does NOT read {TOKEN_INDEX} at this size.")
            print("# Nothing below would be a measurement of the index. Raise --chunks.")
            continue

        print(f"\n## index state: {state}")
        print(
            f"{'arm':>6s} {'ef':>6s} {'recall@' + str(LIMIT):>9s} {'ann ms':>8s} "
            f"{'call ms':>8s} {'page':>6s} {'short':>11s}"
        )
        for arm, principal in principals.items():
            token = set_principal(principal)
            try:
                truths = {q: exact_maxsim(repo, q, LIMIT) for q in queries}
                for ef in ef_searches:
                    with _ann_timer(repo) as timer:
                        recalls, calls = [], []
                        for query in queries:
                            timer.reset()
                            started = time.monotonic()
                            got = ann_maxsim(repo, query, LIMIT, ef_search=ef)
                            calls.append((time.monotonic() - started) * 1000.0)
                            timer.close_search()
                            recalls.append(recall(got, truths[query]))
                    short = sum(1 for got, want in timer.pages if got < want)
                    print(
                        f"{arm:>6s} {ef:6d} {sum(recalls)/len(recalls):9.4f} "
                        f"{_median(timer.searches):8.1f} {_median(calls):8.1f} "
                        f"{_median([p[0] for p in timer.pages]):6.0f} "
                        f"{short:5d}/{len(timer.pages):<5d}"
                    )
            finally:
                reset_principal(token)

    session.close()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dsn", required=True, help="DSN of a database this may CREATE DATABASE on")
    ap.add_argument("--dbname", default="jmfts_maxsim_probe")
    ap.add_argument("--chunks", type=int, default=DEFAULT_CHUNKS)
    ap.add_argument("--queries", type=int, default=DEFAULT_QUERIES)
    ap.add_argument("--ef-search", default=DEFAULT_EF_SEARCH)
    ap.add_argument(
        "--reindex",
        action="store_true",
        help="also measure the index REBUILT on the loaded rows, which schema.sql never does",
    )
    args = ap.parse_args()
    return run(
        args.dsn,
        args.dbname,
        args.chunks,
        args.queries,
        [int(p) for p in args.ef_search.split(",") if p.strip()],
        args.reindex,
    )


if __name__ == "__main__":
    sys.exit(main())
