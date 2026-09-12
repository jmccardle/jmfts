#!/usr/bin/env python3
"""Does a typed link walk at depth 3 want an index on ``document_links.link_type``?
``SPRINT_0_5_0.md`` Block D step 18.

The prediction the step states, in its own words: ``document_links`` is indexed on
``source_id`` and ``target_id`` and **not** on ``link_type`` (``sql/schema.sql:509``-``:510``),
a typed walk filters ``link_type IN (...)`` *after* ``source_id IN (frontier)``, so the
source index should carry the query and the type filter should be a scan of a small set.
This script exists to falsify that, not to confirm it. The result decides whether a
``link_type`` index is worth a second migration; guessing would add an index nothing
measured, which is the error ``SPRINT_0_4_0.md`` Part 3 recorded for structured values in
BM25.

**The graph is generated, and it has to be.** The largest link graph on this machine is
5 edges over 36,823 documents (``jmfts-dev_pgdata``, read from a copy on 2026-09-06);
``jmfts-chunk_chunk_pgdata`` holds 860 documents and 0 edges. Neither can distinguish an
index scan from a sequential one, so the fixture is synthetic and its shape is declared
rather than assumed:

``uniform``
    Sources and targets drawn uniformly at random. Out-degree is Poisson about the mean.
    This is the graph the ``bridge`` writer (``summarization.py:353``) produces — pairwise
    similarity edges inside one RAPTOR layer, with no hub.
``powerlaw``
    Sources and targets drawn from independent Zipf rankings with exponent ``--alpha``.
    A few nodes carry most of the edges. This is the graph ``mentions``
    (``fact_extraction.py``) produces — an entity document is linked from every document
    that mentions it, and mention frequency is not uniform.

The two do not give the same answer and claiming they would is the error this script is
written to avoid: a uniform walk's frontier grows as ``k**depth`` and stays small at
sensible ``k``; a power-law walk reaches a hub at depth 1 and its depth-3 frontier is a
large fraction of the graph, at which point the type filter is being applied to most of
the table and the question changes.

``--type-assign`` decides whether ``link_type`` correlates with ``source_id``, and it
matters more than the degree distribution:

``by-source`` (default)
    Every out-edge of a node carries the same type. This is what all four shipped writers
    do — an entity's edges are all ``mentions``, a parent's are all ``contains``, a summary's
    are all ``summarizes``. Under it, ``link_type`` is nearly a function of ``source_id``,
    so the type filter applied after the source lookup either keeps everything or keeps
    nothing.
``random``
    Type drawn independently per edge. No shipped writer does this. It is the control that
    separates "the filter is cheap" from "the filter is cheap *because* it is correlated".

**The walk is the shipped one.** ``jmfts_core.graph_analysis.compute_neighbors`` is called
directly — the same function ``GraphService.get_neighbors`` calls at
``services/graph_service.py:262`` — and the statements it issues are captured with a
SQLAlchemy ``before_cursor_execute`` hook and then re-run under
``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` verbatim, same text and same parameters. No SQL
in this file resembles the walk's SQL; there is none.

Two things about the shipped walk decide the answer as much as the index does, and both are
reported rather than assumed:

* ``limit`` (200 at the REST default) caps ``reached``, and ``next_frontier`` is built from
  the same loop, so **the frontier can never exceed the limit**. At the default the depth-3
  query is ``source_id IN (<= 200 ids)`` whatever the graph is. ``--limits`` sweeps this so
  the bounded and unbounded regimes are separated instead of averaged.
* ``compute_neighbors`` issues *two* statements per depth under ``direction="both"``, plus
  one hydration query on ``documents`` at the end (``graph_analysis.py:878``). The hydration
  query is reported too, because it can dominate and it is not what the step is about.

Usage — the DSN must point at an EMPTY database on a throwaway Postgres; the script loads
``jmfts_core/sql/schema.sql`` into it and generates its own rows:

    docker run -d --name walk-pg -e POSTGRES_PASSWORD=jmfts -e POSTGRES_USER=jmfts \\
        -e POSTGRES_DB=jmfts -p 127.0.0.1:5457:5432 pgvector/pgvector:pg16
    ./scripts/measure_typed_walk.py --dsn postgresql://jmfts:jmfts@127.0.0.1:5457/jmfts

``sql/schema.sql`` is the authority for this table and is what gets loaded — NOT
``Base.metadata.create_all``, which would omit ``idx_links_source`` / ``idx_links_target``
entirely (``models/document.py:278``-``:284`` says why the mapper does not declare them) and
so would measure a table that does not exist anywhere.

The counterfactual runs on the same fixture, in the same process, three times over:
no extra index, then ``(link_type)``, then ``(link_type, source_id)``. An index the planner
declines is the answer "no second migration" with evidence, and it is a better result than a
timing.

Note on what the schema already contains: the UNIQUE constraint at ``schema.sql:290`` builds
``btree (source_id, target_id, link_type)``. The planner therefore ALREADY has an index whose
leading column is ``source_id`` and which carries ``link_type``, and any recommendation has to
be made against that, not against a table with only ``idx_links_source``.

**The answer this produced on 2026-09-06, and the conditions under which it flips, are
written down in ``docs/MEASURE_TYPED_WALK.md``.** Short version: do not build the index —
across 48 configurations it was faster in 3, slower in 8 and indistinguishable in 37, the
largest gain was 34 ms and the largest loss 2.0 s, and 87-89% of an unbounded walk's wall
time is not in the database at all.
"""

import argparse
import io
import json
import random
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jmfts_core.graph_analysis import compute_neighbors  # noqa: E402

#: The largest real document corpus measured on this machine: `jmfts-dev_pgdata`, read from
#: a copy on 2026-09-06 (36,823 documents, 5 links). The node count is taken from it so the
#: fixture is at least the size of something that exists; the EDGE count cannot be, because
#: 5 edges measure nothing.
DEFAULT_NODES = 36823

#: Average out-degree 10 and 100 over `DEFAULT_NODES`. Two points, not one, because the
#: question is whether the answer scales — a single edge count cannot say.
DEFAULT_EDGES = "368230,3682300"

#: Shipped `link_type` values and a share for each. The shares are a MODELLING CHOICE and
#: are labelled as one: no corpus on this machine has enough edges to measure them. What the
#: sweep needs from them is one common type and one rare one, so that the "filter keeps most
#: of the frontier's edges" and "filter keeps almost none" cases are both exercised.
#: Names are the real ones: `ingest_service.py:918` (contains), `summarization.py:467`
#: (summarizes), `:353` (bridge), `fact_extraction.py` (mentions), `graph_analysis.py:905`
#: (same_as).
LINK_TYPE_SHARES = [
    ("mentions", 0.55),
    ("contains", 0.30),
    ("summarizes", 0.10),
    ("bridge", 0.04),
    ("same_as", 0.01),
]
COMMON_TYPE = "mentions"
RARE_TYPE = "same_as"

#: `services/graph_service.py:248` — the REST default, and the reason the frontier is bounded.
REST_DEFAULT_LIMIT = 200
#: Stands in for "no cap". Larger than any fixture here, so `node_cap` never binds.
UNBOUNDED_LIMIT = 10_000_000

#: The counterfactual, in order. `None` is the shipped index set.
INDEX_PHASES = [
    ("shipped", None),
    ("link_type", "CREATE INDEX ix_walk_link_type ON document_links (link_type)"),
    (
        "link_type_source",
        "CREATE INDEX ix_walk_link_type ON document_links (link_type, source_id)",
    ),
]
COUNTERFACTUAL_INDEX = "ix_walk_link_type"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def load_schema(engine, schema_path: Path, reset: bool) -> None:
    """Load `sql/schema.sql` verbatim. Refuses a database that already has rows unless
    `reset` says the rows are a previous run of this script and may be discarded."""
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT to_regclass('public.document_links') IS NOT NULL")
        ).scalar()
        if exists:
            n = conn.execute(text("SELECT count(*) FROM document_links")).scalar()
            if n and not reset:
                raise SystemExit(
                    f"--dsn points at a database with {n} rows in document_links. "
                    "This script generates its own fixture and will not run against "
                    "populated data. Pass --reset if those rows are a previous run of it."
                )
            return
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(schema_path.read_text())
        raw.commit()
    finally:
        raw.close()


def _zipf_cum_weights(n: int, alpha: float) -> list[float]:
    total = 0.0
    cum = []
    for rank in range(1, n + 1):
        total += rank ** (-alpha)
        cum.append(total)
    return cum


def generate_edges(
    nodes: int, edges: int, dist: str, alpha: float, rng: random.Random
) -> list[tuple[int, int]]:
    """Distinct (source, target) pairs, self-loops excluded, under the named distribution.

    Returns fewer than `edges` pairs when the draw collides; the realized count is reported
    rather than topped up, because topping up biases the distribution toward the tail.
    """
    ids = list(range(1, nodes + 1))
    if dist == "uniform":
        srcs = [rng.randrange(1, nodes + 1) for _ in range(edges)]
        tgts = [rng.randrange(1, nodes + 1) for _ in range(edges)]
    elif dist == "powerlaw":
        cum = _zipf_cum_weights(nodes, alpha)
        src_rank = ids[:]
        tgt_rank = ids[:]
        rng.shuffle(src_rank)
        rng.shuffle(tgt_rank)
        srcs = rng.choices(src_rank, cum_weights=cum, k=edges)
        tgts = rng.choices(tgt_rank, cum_weights=cum, k=edges)
    else:
        raise ValueError(f"unknown distribution {dist!r}")
    seen = set()
    out = []
    for s, t in zip(srcs, tgts):
        if s == t or (s, t) in seen:
            continue
        seen.add((s, t))
        out.append((s, t))
    return out


def assign_types(
    pairs: list[tuple[int, int]], mode: str, rng: random.Random
) -> list[tuple[int, int, str]]:
    names = [n for n, _ in LINK_TYPE_SHARES]
    cum = []
    running = 0.0
    for _, share in LINK_TYPE_SHARES:
        running += share
        cum.append(running)
    if mode == "random":
        picks = rng.choices(names, cum_weights=cum, k=len(pairs))
        return [(s, t, ty) for (s, t), ty in zip(pairs, picks)]
    if mode != "by-source":
        raise ValueError(f"unknown type-assign {mode!r}")
    by_source: dict[int, str] = {}
    out = []
    for s, t in pairs:
        ty = by_source.get(s)
        if ty is None:
            ty = rng.choices(names, cum_weights=cum, k=1)[0]
            by_source[s] = ty
        out.append((s, t, ty))
    return out


def populate(engine, nodes: int, triples: list[tuple[int, int, str]]) -> None:
    """COPY the fixture in, then ANALYZE. Only the columns the walk reads are populated."""
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("TRUNCATE document_links, documents RESTART IDENTITY CASCADE")
        buf = io.StringIO()
        for i in range(1, nodes + 1):
            buf.write(f"{i}\tdoc {i}\tchunk\tsettled\n")
        buf.seek(0)
        cur.copy_expert(
            "COPY documents (id, title, usetype, settled) FROM STDIN WITH (FORMAT text)", buf
        )
        buf = io.StringIO()
        for s, t, ty in triples:
            buf.write(f"{s}\t{t}\t{ty}\n")
        buf.seek(0)
        cur.copy_expert(
            "COPY document_links (source_id, target_id, link_type) FROM STDIN WITH (FORMAT text)",
            buf,
        )
        cur.execute("SELECT setval('documents_id_seq', %s)", (nodes,))
        cur.execute("ANALYZE documents")
        cur.execute("ANALYZE document_links")
        raw.commit()
    finally:
        raw.close()


def degree_report(triples: list[tuple[int, int, str]]) -> dict:
    out = Counter(s for s, _, _ in triples)
    inn = Counter(t for _, t, _ in triples)

    def q(counter: Counter) -> dict:
        vals = sorted(counter.values())
        if not vals:
            return {}
        return {
            "nodes_with_any": len(vals),
            "p50": vals[len(vals) // 2],
            "p90": vals[int(len(vals) * 0.90)],
            "p99": vals[int(len(vals) * 0.99)],
            "max": vals[-1],
            "mean": round(statistics.fmean(vals), 2),
        }

    return {
        "edges": len(triples),
        "out_degree": q(out),
        "in_degree": q(inn),
        "by_link_type": dict(Counter(ty for _, _, ty in triples)),
    }


# ---------------------------------------------------------------------------
# Statement capture and EXPLAIN
# ---------------------------------------------------------------------------


class Capture:
    """Records every statement the walk issues, with its parameters and wall time."""

    def __init__(self, engine):
        self.engine = engine
        self.rows: list[dict] = []
        self._t0 = None
        event.listen(engine, "before_cursor_execute", self._before)
        event.listen(engine, "after_cursor_execute", self._after)

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        self._t0 = time.perf_counter()

    def _after(self, conn, cursor, statement, parameters, context, executemany):
        self.rows.append(
            {
                "sql": statement,
                "params": parameters,
                "ms": (time.perf_counter() - self._t0) * 1000.0,
            }
        )

    def reset(self) -> None:
        self.rows = []


def _self_time(node: dict) -> float:
    total = node.get("Actual Total Time", 0.0) * node.get("Actual Loops", 1)
    for child in node.get("Plans", []) or []:
        total -= child.get("Actual Total Time", 0.0) * child.get("Actual Loops", 1)
    return total


def _walk_plan(node: dict, out: list) -> None:
    out.append(node)
    for child in node.get("Plans", []) or []:
        _walk_plan(child, out)


def explain(engine, sql: str, params) -> dict:
    """Re-run one captured statement under EXPLAIN (ANALYZE, BUFFERS). Verbatim."""
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
        plan = cur.fetchone()[0][0]
    finally:
        raw.close()
    root = plan["Plan"]
    nodes = []
    _walk_plan(root, nodes)
    dominant = max(nodes, key=_self_time)
    index_names = sorted({n["Index Name"] for n in nodes if "Index Name" in n})
    return {
        "execution_ms": round(plan["Execution Time"], 3),
        "planning_ms": round(plan["Planning Time"], 3),
        "dominant_node": dominant["Node Type"],
        "dominant_index": dominant.get("Index Name"),
        "dominant_self_ms": round(_self_time(dominant), 3),
        "dominant_rows_removed_by_filter": dominant.get("Rows Removed by Filter"),
        "indexes_used": index_names,
        "seq_scans": [n["Relation Name"] for n in nodes if n["Node Type"] == "Seq Scan"],
        "shared_hit": root.get("Shared Hit Blocks"),
        "shared_read": root.get("Shared Read Blocks"),
        "node_types": sorted({n["Node Type"] for n in nodes}),
    }


# ---------------------------------------------------------------------------
# One measured configuration
# ---------------------------------------------------------------------------


def run_config(
    engine,
    capture: Capture,
    root_id: int,
    link_types,
    limit: int,
    max_depth: int,
    runs: int,
) -> dict:
    """`runs` walks. Run 1 is reported separately as the cold one; the rest are warm."""
    per_run_ms = []
    per_run_stmt_ms = []
    nodes = None
    for _ in range(runs):
        capture.reset()
        with Session(engine) as session:
            t0 = time.perf_counter()
            nodes = compute_neighbors(
                session,
                root_id,
                max_depth=max_depth,
                direction="both",
                link_types=link_types,
                limit=limit,
            )
            per_run_ms.append((time.perf_counter() - t0) * 1000.0)
        per_run_stmt_ms.append([r["ms"] for r in capture.rows])

    # Frontier sizes come from the walk's own return value: `next_frontier` at depth d is
    # exactly the set of nodes emitted at depth d, so the depth-(d+1) query is issued with
    # that many ids. Depth 1 is issued with the root alone.
    by_depth = Counter(n.depth for n in nodes)
    frontiers = [1] + [by_depth[d] for d in range(1, max_depth)]

    # Cross-check the derived frontier against the parameters actually bound, and fail
    # rather than report a number that was inferred when it could be read.
    n_types = len(link_types) if link_types else 0
    link_stmts = [r for r in capture.rows if "document_links" in r["sql"]]
    bound = [len(r["params"]) - n_types for r in link_stmts]
    expected = [f for f in frontiers for _ in (0, 1)][: len(bound)]
    if bound and bound != expected:
        raise SystemExit(
            f"frontier cross-check failed: derived {expected} from the walk's result, "
            f"but the statements bound {bound} ids. The harness is wrong, not the walk."
        )

    explains = []
    for i, r in enumerate(link_stmts):
        depth = i // 2 + 1
        sense = "outgoing" if i % 2 == 0 else "incoming"
        e = explain(engine, r["sql"], r["params"])
        e["depth"] = depth
        e["direction"] = sense
        e["frontier_ids"] = len(r["params"]) - n_types
        explains.append(e)
    hydration = [r for r in capture.rows if "document_links" not in r["sql"]]
    hydration_explain = (
        explain(engine, hydration[-1]["sql"], hydration[-1]["params"]) if hydration else None
    )

    return {
        "reached": len(nodes),
        "frontier_by_depth": {str(d + 1): f for d, f in enumerate(frontiers)},
        "nodes_by_depth": {str(d): by_depth[d] for d in sorted(by_depth)},
        "walk_ms_cold_first": round(per_run_ms[0], 2),
        "walk_ms_warm_min": round(min(per_run_ms[1:] or per_run_ms), 2),
        "walk_ms_warm_median": round(statistics.median(per_run_ms[1:] or per_run_ms), 2),
        "walk_ms_warm_max": round(max(per_run_ms[1:] or per_run_ms), 2),
        "walk_ms_all": [round(v, 2) for v in per_run_ms],
        "statements_per_walk": len(per_run_stmt_ms[0]),
        # How much of the walk was spent inside the driver at all. The remainder is
        # `compute_neighbors` itself — the per-edge Python loop, the visited set, the
        # NeighborNode construction. Reported because an index can only move the first
        # number, and a recommendation that ignores the split is arguing about the
        # smaller half.
        "statement_ms_all": [round(sum(r), 2) for r in per_run_stmt_ms],
        "explains": explains,
        "hydration_explain": hydration_explain,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", required=True, help="empty throwaway Postgres with pgvector")
    ap.add_argument("--nodes", type=int, default=DEFAULT_NODES)
    ap.add_argument("--edges", default=DEFAULT_EDGES)
    ap.add_argument("--dists", default="uniform,powerlaw")
    ap.add_argument("--alpha", type=float, default=1.0, help="Zipf exponent for --dists powerlaw")
    ap.add_argument("--type-assign", default="by-source", choices=["by-source", "random"])
    ap.add_argument("--limits", default=f"{REST_DEFAULT_LIMIT},{UNBOUNDED_LIMIT}")
    ap.add_argument("--filters", default="none,common,rare")
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--runs", type=int, default=6, help="1 cold + N-1 warm")
    ap.add_argument("--seed", type=int, default=20260906)
    ap.add_argument("--json", help="write the full record here")
    ap.add_argument(
        "--reset",
        action="store_true",
        help="discard an existing fixture in --dsn (only ever a previous run of this script)",
    )
    args = ap.parse_args()

    engine = create_engine(args.dsn, future=True)
    schema = Path(__file__).resolve().parent.parent / "jmfts_core" / "sql" / "schema.sql"
    load_schema(engine, schema, args.reset)
    capture = Capture(engine)

    with engine.connect() as conn:
        server = {
            k: conn.execute(text(f"SHOW {k}")).scalar()
            for k in ("shared_buffers", "work_mem", "effective_cache_size", "server_version")
        }

    filters = {
        "none": None,
        "common": [COMMON_TYPE],
        "rare": [RARE_TYPE],
    }

    record = {
        "server": server,
        "nodes": args.nodes,
        "alpha": args.alpha,
        "type_assign": args.type_assign,
        "seed": args.seed,
        "depth": args.depth,
        "runs": args.runs,
        "configs": [],
    }

    for dist in args.dists.split(","):
        for edges in [int(e) for e in args.edges.split(",")]:
            rng = random.Random(args.seed)
            pairs = generate_edges(args.nodes, edges, dist, args.alpha, rng)
            triples = assign_types(pairs, args.type_assign, rng)
            populate(engine, args.nodes, triples)
            degrees = degree_report(triples)
            out_deg = Counter(s for s, _, _ in triples)
            # Roots are chosen PER FILTER, over the edges that filter admits. A root with no
            # edge of the filtered type ends the walk at depth 0 and measures nothing — and
            # under `by-source` that is most roots, because a node's edges are all one type.
            # Measuring the empty walk would report "the type filter is free" for the reason
            # that nothing was walked, which is the class of wrong number this script exists
            # to avoid.
            roots = {}
            for fname, types in filters.items():
                deg = Counter(s for s, _, ty in triples if types is None or ty in types)
                if not deg:
                    raise SystemExit(f"no edges of type {types} in the {dist} fixture")
                vals = sorted(deg.values())
                med = vals[len(vals) // 2]
                roots[fname] = {
                    "hub": deg.most_common(1)[0][0],
                    "median": next(n for n, d in deg.items() if d == med),
                }
            print(
                f"\n### {dist} n={args.nodes} m={degrees['edges']} "
                f"type-assign={args.type_assign}",
                file=sys.stderr,
            )
            print(f"    degrees {json.dumps(degrees)}", file=sys.stderr)

            for phase_name, ddl in INDEX_PHASES:
                with engine.begin() as conn:
                    conn.execute(text(f"DROP INDEX IF EXISTS {COUNTERFACTUAL_INDEX}"))
                    if ddl:
                        conn.execute(text(ddl))
                    conn.execute(text("ANALYZE document_links"))
                for fname in args.filters.split(","):
                    for root_label in ("hub", "median"):
                        root_id = roots[fname][root_label]
                        for limit in [int(x) for x in args.limits.split(",")]:
                            res = run_config(
                                engine,
                                capture,
                                root_id,
                                filters[fname],
                                limit,
                                args.depth,
                                args.runs,
                            )
                            entry = {
                                "dist": dist,
                                "requested_edges": edges,
                                "realized_edges": degrees["edges"],
                                "degrees": degrees,
                                "index_phase": phase_name,
                                "root": root_label,
                                "root_id": root_id,
                                "root_out_degree": out_deg[root_id],
                                "filter": fname,
                                "limit": limit,
                                **res,
                            }
                            record["configs"].append(entry)
                            chose = any(
                                COUNTERFACTUAL_INDEX in e["indexes_used"] for e in res["explains"]
                            )
                            print(
                                f"  {phase_name:16s} root={root_label:6s} "
                                f"filter={fname:6s} limit={limit:<8d} "
                                f"reached={res['reached']:<7d} "
                                f"frontier={list(res['frontier_by_depth'].values())} "
                                f"warm={res['walk_ms_warm_median']:>9.2f}ms "
                                f"chose_new_index={chose}",
                                file=sys.stderr,
                            )

    if args.json:
        Path(args.json).write_text(json.dumps(record, indent=1))
        print(f"\nwrote {args.json}", file=sys.stderr)
    else:
        json.dump(record, sys.stdout, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
