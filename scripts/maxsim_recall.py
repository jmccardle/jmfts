#!/usr/bin/env python3
"""Does the MaxSim ANN query lose documents to IVFFlat at scale? SPRINT_0_4_0.md Block A.

**SUPERSEDED 2026-09-11 AS A DESCRIPTION OF THE APPLIANCE, AND KEPT AS A MEASUREMENT.**
It answered yes, `ANN_INDEX_HEALTH.md` 5.8 acted on it, and migration
`022_token_embed_256_hnsw.sql` made `embed_256` an HNSW index — so the DDL quoted below is
no longer what `schema.sql` builds and the two predictions below are no longer predictions
about anything shipped. Nothing here is edited to match, because this file builds its OWN
throwaway IVFFlat tables and measures those: the numbers it produced are still true of what
it measured, and rewriting the framing would leave the citations in Part 5 pointing at a
document that no longer says what they quote. `scripts/maxsim_probes.py` is the instrument
that still describes the appliance.

``scripts/filtered_recall.py`` asked this of HNSW and ``documents.embed``. The answer there
turned out not to reach the appliance, because ``vector_search`` orders by a similarity
label and so never reads ``idx_documents_embed`` at all
(``tests/test_filtered_recall.py``). MaxSim is the other path and it does NOT have that
defect: ``repositories/search.py:582`` orders by the bare distance operator, and
``:559``-``:561`` ANDs ``readable_sql`` into the same statement. So if any shipped path
truncates under the read gate today, it is this one.

The index is different and the arithmetic is different. ``sql/schema.sql:488`` builds

    CREATE INDEX idx_token_embed_256_ivf ON token_embeddings
        USING ivfflat (embed_256 halfvec_cosine_ops) WITH (lists = 1024);

IVFFlat partitions the vectors into ``lists`` cells and, at the default
``ivfflat.probes = 1``, scans exactly ONE of them. So a scan touches about ``rows / 1024``
tuples whatever the query is, the ``WHERE`` clause is applied to those, and
``search.py:513`` asks for ``LIMIT 100`` per query token. Two predictions follow, and this
script exists to check them rather than to assert them:

* ``lists`` is a hardcoded 1024, not a function of table size. Below about 102,400 token
  rows one probe cannot hold 100 tuples even with no filter at all, so a SMALL corpus is
  the exposed one — the opposite of the HNSW case, where the planner protects a small
  table by choosing the exact plan.
* The read gate then subtracts again, and it is applied after the probe rather than
  inside it.

**A diagnosis, not a benchmark.** No model is loaded and no recall-against-a-corpus number
is produced. Ground truth is computed here, on the same rows, by denying the planner every
index and letting it sort exactly; every reported shortfall is ANN-against-exact on
identical data, so nothing depends on a judgment about what the right answer was.

It runs against its own throwaway tables, not the appliance schema, for the reason
``filtered_recall.py`` gives. What it does carry over is the part that decides the plan:
the join to a documents-shaped table, ``readable_sql``'s literal
``(id IN (...) OR path @> ANY(ARRAY[...]))`` shape against a GIN index, and the IVFFlat
index verbatim from ``schema.sql``. The four predicates it drops (usetype, parent path,
as_of, and a tighter tier) can only make the filter narrower.

    ./scripts/maxsim_recall.py --dsn postgresql://jmfts:jmfts@localhost:5434/postgres
    ./scripts/maxsim_recall.py --sizes 500000 --probes 1,10,64 --lists 1024

Two geometries, swept rather than chosen, because they bracket the answer:

``disjoint``
    Every readable token sits in the opposite half-space from the query and every
    unreadable one beside it. The exact answer is known in advance and the probe is
    guaranteed to land on the unreadable cell. This is the constructed worst case and it
    is what ``filtered_recall.py`` used; a failure here proves the mechanism exists.
``mixed``
    ``--clusters`` tight Gaussian-ish clusters at random centres, with readability assigned
    per document and independent of which cluster a token lands in. This is the ordinary
    case, where any shortfall is the ``rows / lists`` arithmetic alone.

The clusters in ``mixed`` are not decoration. A first version drew every component
uniformly, which is isotropic, and IVFFlat's k-means has nothing to partition: at
``probes = 1`` the probed cell came back EMPTY on 10 of 10 queries and the script was
reporting a degeneracy of its own fixture. Token embeddings are not uniform noise, so
neither is this.

Every configuration is asked ``--queries`` times rather than once, and each query vector
is DRAWN FROM THE CORPUS DISTRIBUTION rather than being a fixed basis vector. Both parts
are corrections to a first version of this script that reported one draw: which IVFFlat
cell a single query lands in is close to arbitrary, so one query answered 0 of 100 and 100
of 100 on the same configuration under two seeds. ``maxsim_search`` issues one of these
per query token (``search.py:569``), so a distribution is also the more faithful shape.
"""

import argparse
import json
import random
import sys
import time

#: `token_embeddings.embed_256` (`sql/schema.sql:106`).
DIM = 256
#: `search.py:513`. How many token rows one query token asks the index for.
K_PER_TOKEN = 100
#: `schema.sql:489`. Hardcoded there, so it does not track table size.
DEFAULT_LISTS = 1024
#: 1 is pgvector's default and is what the appliance runs under — nothing in `jmfts_core/`
#: sets `ivfflat.probes`.
DEFAULT_PROBES = "1,10,64"
DEFAULT_SIZES = "50000,500000"
#: How much of the corpus the principal may read. The HNSW probe swept this because the
#: planner prices the index against a sort using it. IVFFlat has no such escape at these
#: sizes — the scan is a fixed fraction of the table — so this sweeps what survives the
#: gate, not what the planner chooses.
DEFAULT_READABLE_PCT = "1,5,20,50"
GEOMETRIES = ("disjoint", "mixed")

#: Two roots, ids 1 and 2. Both are access-control roots; only 2 is granted to the caller.
#: Reproduces `access.py:114`-`:128` literally, including the `OR NOT` arm that admits an
#: ungoverned document — there are none here, which keeps the fragment from being a no-op
#: for the wrong reason.
HIDDEN_ROOT = 1
OPEN_ROOT = 2


def _acl_sql(alias="d"):
    """`readable_sql(alias=...)` for a caller granted `OPEN_ROOT` and nothing else."""

    def within(root_ids):
        ids = ",".join(str(r) for r in root_ids)
        arr = ",".join(f"jsonb_build_array({r})" for r in root_ids)
        return f"({alias}.id IN ({ids}) OR {alias}.path @> ANY(ARRAY[{arr}]))"

    return f"({within([OPEN_ROOT])} OR NOT {within([HIDDEN_ROOT, OPEN_ROOT])})"


#: Component ranges the generated rows use. Kept as constants because the query vectors
#: are drawn from the same ranges — a query from outside the corpus distribution lands
#: between cells and measures the geometry of the probe rather than the index.
NOISE = 0.35
NEAR_FIRST = 1.0
FAR_FIRST = -3.0
#: `mixed` only: how many centres the tokens are drawn around, and how tight. 256 centres
#: over 1024 lists means several lists per cluster, which is the regime IVFFlat is for.
DEFAULT_CLUSTERS = 256
DEFAULT_CLUSTER_NOISE = 0.10


def _literal(values):
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


def _queries(cur, rng, geometry, count, cluster_noise):
    """`count` query vectors, drawn the way a token of the corpus is drawn.

    In ``disjoint`` that is the NEAR cluster, which is the unreadable one: a query matching
    content the caller may not read is exactly the case the read gate is for. In ``mixed``
    a centre is read back from the table that generated the rows and perturbed, so the
    query is near the corpus without being a member of it — a query drawn from a different
    distribution than the rows lands between cells and would measure this script's geometry
    rather than the index. Cosine ignores magnitude, so nothing here is normalized, and
    neither are the rows.
    """
    if geometry == "disjoint":
        return [
            _literal([NEAR_FIRST] + [rng.uniform(-NOISE, NOISE) for _ in range(DIM - 1)])
            for _ in range(count)
        ]
    # `::real[]`, because psycopg2 has no adapter for pgvector's own type and would hand
    # back the literal as a string.
    cur.execute("SELECT embed::real[] FROM maxsim_probe_centers ORDER BY id")
    centres = [row[0] for row in cur.fetchall()]
    return [
        _literal([v + rng.uniform(-cluster_noise, cluster_noise) for v in rng.choice(centres)])
        for _ in range(count)
    ]


def _server_facts(cur):
    cur.execute("SELECT current_setting('server_version')")
    server = cur.fetchone()[0]
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    row = cur.fetchone()
    # `LOAD` before reading `pg_settings`: pgvector registers its GUCs in `_PG_init`, which
    # `CREATE EXTENSION` alone does not run. Without this the probes setting reports as
    # absent on a server that supports it. `filtered_recall.py` fell into this once.
    cur.execute("LOAD 'vector'")
    cur.execute("SELECT count(*) FROM pg_settings WHERE name = 'ivfflat.probes'")
    return server, (row[0] if row else None), cur.fetchone()[0] == 1


def _assert_distinct(cur, table, column, sample):
    """Refuse to index a table whose generated vectors collapsed to a handful of values.

    An uncorrelated subquery makes every row identical, and every number this script prints
    downstream would then be a fact about one point rather than about a corpus. There is no
    sensible fallback: a degenerate fixture is not a smaller experiment, it is a different
    one, so this raises.
    """
    cur.execute(
        f"SELECT count(*), count(DISTINCT {column}::text) FROM "
        f"(SELECT {column} FROM {table} LIMIT {int(sample)}) s"
    )
    rows, distinct = cur.fetchone()
    if rows and distinct < max(2, rows * 0.9):
        raise RuntimeError(
            f"{table}.{column}: {distinct} distinct values in {rows} sampled rows. The "
            "generating subquery was hoisted out of the row loop — it must range over "
            "generate_series(<outer column>, <outer column> + N) so it stays correlated."
        )


def _build(cur, size, readable_pct, geometry, lists, tokens_per_doc, seed, clusters, cluster_noise):
    """Generate `size` token rows over a two-root document tree and index them.

    Rows are generated SERVER-side. 500,000 rows is 128 million random components, and
    shipping those from Python is minutes of round-trips for numbers the server can make
    in place. The seed goes to `setseed` so a run is still reproducible.
    """
    cur.execute("DROP TABLE IF EXISTS maxsim_probe_tokens")
    cur.execute("DROP TABLE IF EXISTS maxsim_probe_docs")
    cur.execute("DROP TABLE IF EXISTS maxsim_probe_centers")
    cur.execute(
        "CREATE TABLE maxsim_probe_docs ("
        "  id integer PRIMARY KEY,"
        "  path jsonb NOT NULL,"
        "  settled text NOT NULL)"
    )
    cur.execute(
        "CREATE TABLE maxsim_probe_tokens ("
        "  id bigserial PRIMARY KEY,"
        "  document_id integer NOT NULL REFERENCES maxsim_probe_docs(id),"
        "  tier integer,"
        f"  embed_{DIM} halfvec({DIM}))"
    )

    n_docs = max(2, -(-size // tokens_per_doc))
    n_leaves = n_docs - 2
    n_readable = max(1, round(n_leaves * readable_pct / 100.0))
    if n_readable >= n_leaves:
        raise ValueError(f"{readable_pct}% of {n_leaves} leaves leaves nothing unreadable")

    # The two roots, then the leaves: ids 3..(2+n_readable) under OPEN, the rest under
    # HIDDEN. `path` holds the ancestor chain exactly as the appliance's trigger writes it
    # (`schema.sql:550`-`:558`), because `readable_sql` matches on `path @>`.
    cur.execute(
        "INSERT INTO maxsim_probe_docs (id, path, settled) VALUES "
        f"({HIDDEN_ROOT}, '[]'::jsonb, 'settled'), ({OPEN_ROOT}, '[]'::jsonb, 'settled')"
    )
    cur.execute(
        "INSERT INTO maxsim_probe_docs (id, path, settled) "
        "SELECT i,"
        f"       CASE WHEN i <= %s THEN jsonb_build_array({OPEN_ROOT})"
        f"            ELSE jsonb_build_array({HIDDEN_ROOT}) END,"
        "       'settled' "
        "FROM generate_series(3, %s) i",
        (2 + n_readable, n_docs),
    )
    cur.execute("CREATE INDEX ON maxsim_probe_docs USING GIN (path)")

    # Geometry. `readable` is a property of the document; where the token sits is either
    # determined by it (disjoint) or independent of it (mixed).
    #
    #   disjoint: readable tokens get a first component of -3, unreadable +1, so cosine
    #             distance to the query is above 1 for every readable row and below 1 for
    #             every unreadable one. The ordering is then a fact about the construction
    #             rather than about IVFFlat's approximation, which is what makes the exact
    #             answer knowable without computing it.
    #   mixed:    every token is drawn around one of `clusters` random centres, chosen
    #             independently of whether its document is readable.
    cur.execute("SELECT setseed(%s)", (((seed % 1000) / 1000.0) - 0.5,))
    started = time.monotonic()
    # EVERY generated vector's subquery ranges over `generate_series(<outer>, <outer> + 255)`
    # rather than `generate_series(1, 256)`, and that is load-bearing. A subquery that does
    # not reference the outer row is uncorrelated, so Postgres hoists it to an InitPlan and
    # evaluates `random()` ONCE for the whole statement — which built 20,000 token rows
    # holding one distinct vector, and 256 centres holding one. Nothing about that fixture
    # is visibly wrong from the outside; it just reports whatever IVFFlat does with a single
    # point. `_assert_distinct` below is the guard, because the next person to edit this
    # will not remember the rule.
    if geometry == "disjoint":
        first = (
            f"CASE WHEN t.document_id <= {2 + n_readable} THEN {FAR_FIRST} ELSE {NEAR_FIRST} END"
        )
        cur.execute(
            f"""
            INSERT INTO maxsim_probe_tokens (document_id, tier, embed_{DIM})
            SELECT t.document_id, 10,
                   (SELECT array_agg(CASE WHEN d = t.i THEN {first}
                                          ELSE (random() - 0.5) * {2 * NOISE} END ORDER BY d)
                      FROM generate_series(t.i, t.i + {DIM - 1}) d
                   )::real[]::vector({DIM})::halfvec({DIM})
            FROM (SELECT 3 + ((i - 1) %% {int(n_leaves)}) AS document_id, i
                    FROM generate_series(1, %s) i) t
            """,
            (size,),
        )
    else:
        cur.execute(
            f"CREATE TABLE maxsim_probe_centers (id integer PRIMARY KEY, embed vector({DIM}))"
        )
        cur.execute(
            f"""
            INSERT INTO maxsim_probe_centers (id, embed)
            SELECT c, (SELECT array_agg((random() - 0.5) * 2.0 ORDER BY d)
                         FROM generate_series(c, c + {DIM - 1}) d)::real[]::vector({DIM})
            FROM generate_series(1, %s) c
            """,
            (clusters,),
        )
        _assert_distinct(cur, "maxsim_probe_centers", "embed", clusters)
        # `vector + vector` is pgvector's own operator, so the offset is applied in the
        # server rather than shipped per row from Python.
        cur.execute(
            f"""
            INSERT INTO maxsim_probe_tokens (document_id, tier, embed_{DIM})
            SELECT t.document_id, 10,
                   (ct.embed + (SELECT array_agg((random() - 0.5) * {2 * cluster_noise}
                                                 ORDER BY d)
                                  FROM generate_series(t.i, t.i + {DIM - 1}) d
                               )::real[]::vector({DIM}))::halfvec({DIM})
            FROM (SELECT 3 + ((i - 1) %% {int(n_leaves)}) AS document_id, i,
                         1 + floor(random() * {int(clusters)})::int AS center_id
                    FROM generate_series(1, %s) i) t
            JOIN maxsim_probe_centers ct ON ct.id = t.center_id
            """,
            (size,),
        )
    generated = time.monotonic() - started
    _assert_distinct(cur, "maxsim_probe_tokens", f"embed_{DIM}", min(size, 1000))

    started = time.monotonic()
    cur.execute(
        f"CREATE INDEX idx_maxsim_probe_ivf ON maxsim_probe_tokens "
        f"USING ivfflat (embed_{DIM} halfvec_cosine_ops) WITH (lists = {int(lists)})"
    )
    cur.execute("ANALYZE maxsim_probe_docs")
    cur.execute("ANALYZE maxsim_probe_tokens")
    indexed = time.monotonic() - started

    cur.execute(
        "SELECT count(*) FROM maxsim_probe_tokens te JOIN maxsim_probe_docs d "
        f"ON te.document_id = d.id WHERE {_acl_sql()}"
    )
    readable_rows = cur.fetchone()[0]
    return n_docs, readable_rows, generated, indexed


def _ann_sql(gated=True):
    """``search.py:577``-``:584``, minus the four predicates this probe does not model.

    ``gated=False`` drops only the ``readable_sql`` fragment and is the CONTROL. Without
    it a short page has two candidate causes and the script cannot choose between them:
    the read gate removing rows the probe found, or the probe not finding ``k`` rows in
    the first place because ``lists`` is large relative to the table. The ungated run
    separates those, so the attribution is measured rather than argued.
    """
    acl = f"\n  AND {_acl_sql()}" if gated else ""
    return f"""
SELECT te.document_id
FROM maxsim_probe_tokens te
JOIN maxsim_probe_docs d ON te.document_id = d.id
WHERE te.embed_{DIM} IS NOT NULL
  AND d.settled = 'settled'
  AND te.tier <= 50{acl}
ORDER BY te.embed_{DIM} <=> CAST(%(q)s AS vector)
LIMIT %(k)s
"""


def _run(cur, query, k, gated=True):
    sql = _ann_sql(gated)
    cur.execute("EXPLAIN (FORMAT JSON) " + sql, {"q": query, "k": k})
    plan = json.dumps(cur.fetchone()[0])
    cur.execute(sql, {"q": query, "k": k})
    rows = [r[0] for r in cur.fetchall()]
    return rows, ("idx_maxsim_probe_ivf" in plan)


def _exact(cur, query, k, gated=True):
    """Ground truth: the same statement with every index path denied, so the planner sorts.

    A sort is exact by construction, so this is the answer the ANN query is measured
    against — computed on the same rows in the same transaction, never assumed.
    """
    for guc in ("enable_indexscan", "enable_bitmapscan", "enable_indexonlyscan"):
        cur.execute(f"SET {guc} = off")
    try:
        rows, _ = _run(cur, query, k, gated=gated)
    finally:
        for guc in ("enable_indexscan", "enable_bitmapscan", "enable_indexonlyscan"):
            cur.execute(f"SET {guc} = on")
    return rows


def _sweep(cur, queries, truths, probe, has_probes):
    """Ask each query vector under one `probes` setting, beside its own ground truth.

    `truths` is computed once per table by the caller: it is a property of the query and
    the rows, and `ivfflat.probes` cannot move it. Recomputing it per setting would be an
    exact scan of the whole table for every column of the report.
    """
    if has_probes:
        cur.execute(f"SET ivfflat.probes = {int(probe)}")
    rows_got, rows_truth, rows_ungated = [], [], []
    recalls, open_recalls, used_any = [], [], False
    for query, (truth, open_truth) in zip(queries, truths):
        truth_docs, open_docs = set(truth), set(open_truth)
        got, used_index = _run(cur, query, K_PER_TOKEN)
        ungated, _ = _run(cur, query, K_PER_TOKEN, gated=False)
        used_any = used_any or used_index
        # Rows, not distinct documents: `LIMIT 100` is a row budget, and a short page is
        # the index stopping. Recall is over documents, because that is what
        # `maxsim_search` aggregates to and hands the caller.
        rows_got.append(len(got))
        rows_truth.append(len(truth))
        rows_ungated.append(len(ungated))
        recalls.append(len(set(got) & truth_docs) / len(truth_docs) if truth_docs else 1.0)
        # IVFFlat at one probe is approximate BEFORE any filter: it ranks the contents of
        # 1/lists of the space. Without this control a gated recall of 0.5 cannot be told
        # apart from the index's own loss, and the gate would take credit for it.
        open_recalls.append(len(set(ungated) & open_docs) / len(open_docs) if open_docs else 1.0)
    return rows_got, rows_truth, rows_ungated, recalls, open_recalls, used_any


def _median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def run(
    dsn,
    sizes,
    pcts,
    probes,
    geometries,
    lists,
    tokens_per_doc,
    seed,
    n_queries,
    clusters,
    cluster_noise,
):
    import psycopg2

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    server, vector_version, has_probes = _server_facts(cur)

    print(f"# postgres {server}, pgvector {vector_version}")
    print(f"# ivfflat.probes: {'settable' if has_probes else 'NOT SETTABLE'}")
    print(f"# k = {K_PER_TOKEN} (search.py:513), lists = {lists} (schema.sql:489)")
    print("# `exact` is the same statement with every index denied, on the same rows.")
    print("# `got` below `exact` is the index losing answers, not the data running out.")
    print("# `ungated` and `open` are the same query with readable_sql removed: `open` is")
    print("# IVFFlat's own recall at that probes setting, so `recall` well under `open` is")
    print("# the read gate and `recall` tracking `open` is the index, not the gate.\n")

    header = (
        f"{'rows':>8s} {'geom':>8s} {'read%':>6s} {'readable':>9s} {'per-list':>8s} "
        f"{'probes':>6s} {'plan':>9s} {'got~':>5s} {'min':>4s} {'max':>4s} "
        f"{'ungated~':>8s} {'exact~':>6s} {'recall':>6s} {'open':>6s}  verdict"
    )
    findings = []
    rng = random.Random(seed)
    for size in sizes:
        for geometry in geometries:
            for pct in pcts:
                n_docs, readable_rows, gen_s, idx_s = _build(
                    cur,
                    size,
                    pct,
                    geometry,
                    lists,
                    tokens_per_doc,
                    rng.randrange(1 << 30),
                    clusters,
                    cluster_noise,
                )
                # After the build, never before: a `mixed` query is a perturbed centre and
                # the centres do not exist until the rows that surround them do.
                queries = _queries(cur, rng, geometry, n_queries, cluster_noise)
                per_list = size / float(lists)
                print(
                    f"# {size} rows over {n_docs} docs, {readable_rows} readable token "
                    f"rows; generated in {gen_s:.1f}s, indexed in {idx_s:.1f}s, "
                    f"{n_queries} queries"
                )
                print(header)
                truths = [
                    (_exact(cur, q, K_PER_TOKEN), _exact(cur, q, K_PER_TOKEN, gated=False))
                    for q in queries
                ]
                for probe in probes:
                    got, truth, ungated, recalls, open_recalls, used_index = _sweep(
                        cur, queries, truths, probe, has_probes
                    )
                    got_med, truth_med = _median(got), _median(truth)
                    ungated_med = _median(ungated)
                    recall = sum(recalls) / len(recalls)
                    open_recall = sum(open_recalls) / len(open_recalls)
                    short = sum(1 for g, t in zip(got, truth) if g < t)
                    # Attribution, and it is the whole point of the two control columns.
                    # The gate is implicated only where the SAME probe did better without
                    # it — a shorter page, or a higher recall. Where gated and ungated
                    # recall agree, what was lost is IVFFlat's own approximation and the
                    # gate is a bystander.
                    gate_short = sum(1 for g, u in zip(got, ungated) if g < u)
                    gate_cost = open_recall - recall
                    blames_gate = bool(gate_short) or gate_cost > 0.02
                    cause = "the read gate" if blames_gate else f"ivfflat at probes={probe}"
                    if not used_index:
                        verdict = "exact plan; no truncation possible"
                    elif short:
                        verdict = f"TRUNCATED on {short}/{n_queries}, {cause}"
                    elif recall < 0.99:
                        verdict = f"full page, recall {recall:.2f}, {cause}"
                    else:
                        verdict = "complete"
                    if verdict != "complete" and used_index:
                        findings.append(
                            (size, geometry, pct, probe, min(got), truth_med, recall, blames_gate)
                        )
                    print(
                        f"{size:8d} {geometry:>8s} {pct:6g} {readable_rows:9d} "
                        f"{per_list:8.1f} {probe:6d} "
                        f"{'ivfflat' if used_index else 'seq+sort':>9s} "
                        f"{got_med:5.1f} {min(got):4d} {max(got):4d} {ungated_med:8.1f} "
                        f"{truth_med:6.1f} {recall:6.2f} {open_recall:6.2f}  {verdict}"
                    )
                print()

    cur.execute("DROP TABLE IF EXISTS maxsim_probe_tokens")
    cur.execute("DROP TABLE IF EXISTS maxsim_probe_docs")
    cur.execute("DROP TABLE IF EXISTS maxsim_probe_centers")
    conn.close()

    if findings:
        default = [f for f in findings if f[3] == 1]
        gated = [f for f in default if f[7]]
        print(f"# {len(findings)} configuration(s) lost answers.")
        if default:
            print(f"#   {len(default)} of them at the APPLIANCE'S OWN SETTING (probes=1).")
            worst = min(default, key=lambda f: f[6])
            print(
                f"#   Worst: {worst[0]} rows, {worst[1]}, {worst[2]}% readable — mean recall "
                f"{worst[6]:.2f}, worst query returned {worst[4]} of a median {worst[5]:.0f}."
            )
            print(
                f"#   {len(gated)} of the {len(default)} are the READ GATE — the same probe "
                "did better with readable_sql removed."
            )
            print(
                f"#   The other {len(default) - len(gated)} are IVFFlat's own approximation "
                "at one probe, which the gate never enters."
            )
        else:
            print("#   NONE at probes=1. The appliance's own setting is unaffected here.")
    else:
        print("# No configuration lost an answer. Read the `plan` column before concluding:")
        print("# a row that says seq+sort was never a test of the index.")
    return 0 if not findings else 1


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--sizes", default=DEFAULT_SIZES, help="token rows")
    ap.add_argument("--readable-pct", default=DEFAULT_READABLE_PCT)
    ap.add_argument("--probes", default=DEFAULT_PROBES)
    ap.add_argument("--lists", type=int, default=DEFAULT_LISTS)
    ap.add_argument(
        "--geometry",
        default=",".join(GEOMETRIES),
        help="disjoint (constructed worst case), mixed (readability independent of position)",
    )
    ap.add_argument("--tokens-per-doc", type=int, default=64)
    ap.add_argument(
        "--queries",
        type=int,
        default=20,
        help="query vectors per configuration; one draw is not a measurement",
    )
    ap.add_argument(
        "--clusters",
        type=int,
        default=DEFAULT_CLUSTERS,
        help="mixed only: centres the tokens are drawn around",
    )
    ap.add_argument("--cluster-noise", type=float, default=DEFAULT_CLUSTER_NOISE)
    ap.add_argument("--seed", type=int, default=20260904)
    args = ap.parse_args()
    geometries = [g.strip() for g in args.geometry.split(",") if g.strip()]
    unknown = set(geometries) - set(GEOMETRIES)
    if unknown:
        ap.error(f"unknown geometry: {', '.join(sorted(unknown))}")
    return run(
        args.dsn,
        [int(s) for s in args.sizes.split(",") if s.strip()],
        [float(p) for p in args.readable_pct.split(",") if p.strip()],
        [int(p) for p in args.probes.split(",") if p.strip()],
        geometries,
        args.lists,
        args.tokens_per_doc,
        args.seed,
        args.queries,
        args.clusters,
        args.cluster_noise,
    )


if __name__ == "__main__":
    sys.exit(main())
