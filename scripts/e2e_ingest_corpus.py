#!/usr/bin/env python3
"""End-to-end measurement of the queued ingest path over a real directory of files.

``INGEST_SPEC.md`` Part 5 describes a pipeline whose parts are each tested in isolation.
This script runs the whole of it once, against a live appliance, over files nobody
chose to make convenient, and reports what came out. It answers four questions and
keeps them apart:

1. **Did every file finish?**  Counts of file nodes by lifecycle state, and every task
   that failed with its error type and message.
2. **What did the tree become?**  Nodes by usetype and depth, chunks per file,
   segments the rollup planner created.
3. **Is it retrievable?**  Document vectors present, token embeddings present, BM25
   index entries present, and the same query run through each search method.
4. **What did it cost?**  Wall clock per task type, and the frontier over time.

Nothing here is a fixture. The corpus is whatever directory is named on the command
line, the appliance is whatever ``--api`` points at, and the report says what happened
rather than asserting that it matched an expectation.

The phases are separate subcommands because the middle one takes hours:

    e2e_ingest_corpus.py upload  --state run.json DIR [DIR ...]
    e2e_ingest_corpus.py wait    --state run.json
    e2e_ingest_corpus.py index   --state run.json
    e2e_ingest_corpus.py report  --state run.json --out report.md
    e2e_ingest_corpus.py search  --state run.json
    e2e_ingest_corpus.py all     --state run.json DIR [DIR ...]

``upload`` writes the state file; every later phase reads it, so a run can be resumed
after an interruption and a report can be regenerated without re-ingesting anything.

The report reads the database directly. That is deliberate: the queue's timings, the
per-task error classification and the embedding coverage have no HTTP surface, and
inventing endpoints for a measurement script would put a measurement's shape into the
appliance's API. Everything a caller CAN do over HTTP — upload, poll, index, search —
is done over HTTP here, because that is the path being measured.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import psycopg2
import psycopg2.extras

# The usetype given to the node that holds one ingested directory, and to the node that
# holds them all. Free-text in the schema; named here so the report can find them again.
USETYPE_COLLECTION = "collection"
USETYPE_FOLDER = "folder"

DEFAULT_API = "http://127.0.0.1:8100"
DEFAULT_POLL_SECONDS = 30.0

#: Queries the `search` phase runs. Deliberately about the corpus this was written
#: against (planning / retrieval research) and overridable with --queries.
DEFAULT_QUERIES = [
    "hierarchical task network planning",
    "multi-hop question answering benchmark",
    "reinforcement learning from human feedback",
    "PDDL domain description language",
    "retrieval augmented generation",
]

SEARCH_METHODS = ["vector", "fulltext", "bm25"]


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


class Api:
    """The appliance over HTTP. One client, one token, no retries.

    No retry policy on purpose: a failed request during a measurement is a fact about
    the appliance, and a client that quietly repeated it would report a success rate
    that includes attempts nobody counted.
    """

    def __init__(self, base_url: str, token: Optional[str], timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def get(self, path: str, **kwargs) -> Any:
        response = self.client.get(path, **kwargs)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, **kwargs) -> Any:
        response = self.client.post(path, **kwargs)
        response.raise_for_status()
        return response.json()


def db_dsn(explicit: Optional[str]) -> str:
    """The read-only connection the report uses, from the same env the appliance reads.

    Defaults match ``jmfts_core.config``'s own defaults so a developer who has the app
    configured has this configured too; ``--dsn`` overrides all of it.
    """
    if explicit:
        return explicit
    return (
        f"host={os.environ.get('JMFTS_DB_HOST', '127.0.0.1')} "
        f"port={os.environ.get('JMFTS_DB_PORT', '5432')} "
        f"dbname={os.environ.get('JMFTS_DB_NAME', 'jmfts')} "
        f"user={os.environ.get('JMFTS_DB_USER', 'jmfts')} "
        f"password={os.environ.get('JMFTS_DB_PASSWORD', 'jmfts')}"
    )


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


@dataclass
class RunState:
    """Everything a later phase needs to find the run the earlier phase made."""

    api: str = DEFAULT_API
    collection_id: Optional[int] = None
    collection_title: str = ""
    index_name: str = ""
    folders: dict = field(default_factory=dict)
    files: list = field(default_factory=list)
    frontier_samples: list = field(default_factory=list)
    upload_started_at: str = ""
    upload_finished_at: str = ""
    drain_finished_at: str = ""

    @classmethod
    def load(cls, path: Path) -> "RunState":
        return cls(**json.loads(path.read_text()))

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.__dict__, indent=2))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# phase: upload
# ---------------------------------------------------------------------------


def phase_upload(args: argparse.Namespace) -> RunState:
    api = Api(args.api, args.token)
    state = RunState(api=args.api, collection_title=args.collection, index_name=args.index)

    collection = api.post(
        "/documents",
        json={
            "title": args.collection,
            "usetype": USETYPE_COLLECTION,
            "auto_embed": False,
        },
    )
    state.collection_id = collection["id"]
    print(f"collection {state.collection_id}: {args.collection}")

    options = json.loads(args.options) if args.options else None
    state.upload_started_at = now_iso()

    for directory in args.directories:
        path = Path(directory).expanduser().resolve()
        if not path.is_dir():
            raise SystemExit(f"not a directory: {path}")
        folder = api.post(
            "/documents",
            json={
                "title": path.name,
                "usetype": USETYPE_FOLDER,
                "parent_id": state.collection_id,
                "auto_embed": False,
            },
        )
        state.folders[str(path)] = folder["id"]
        entries = sorted(p for p in path.iterdir() if p.is_file())
        if args.limit:
            entries = entries[: args.limit]
        print(f"folder {folder['id']}: {path} ({len(entries)} files)")

        for entry in entries:
            record: dict = {"path": str(entry), "name": entry.name, "folder_id": folder["id"]}
            try:
                files = {"file": (entry.name, entry.read_bytes())}
                data = {"options": json.dumps(options)} if options else None
                response = api.client.post(
                    "/ingest/file",
                    params={"parent_id": folder["id"]},
                    files=files,
                    data=data,
                )
                response.raise_for_status()
                body = response.json()
                record.update(
                    document_id=body["document_id"],
                    detected_mime=body.get("detected_mime"),
                    detected_by=body.get("detected_by"),
                    byte_size=body["byte_size"],
                    was_existing=body.get("was_existing", False),
                )
            except httpx.HTTPStatusError as error:
                # An upload that the API refuses never becomes a node, so it can only be
                # counted here. Recorded and the run continues: one rejected file is a
                # finding, not a reason to stop measuring the other 271.
                record["upload_error"] = f"HTTP {error.response.status_code}: " + (
                    error.response.text[:300]
                )
                print(f"  REFUSED {entry.name}: {record['upload_error']}")
            state.files.append(record)

    state.upload_finished_at = now_iso()
    accepted = sum(1 for f in state.files if "document_id" in f)
    print(f"uploaded {accepted}/{len(state.files)} files")
    state.save(args.state)
    api.close()
    return state


# ---------------------------------------------------------------------------
# phase: wait
# ---------------------------------------------------------------------------


def phase_wait(args: argparse.Namespace) -> RunState:
    """Poll the collection's frontier until the queue goes quiet.

    The stop condition is ``tasks_unfinished == 0`` and NOT ``nodes_in_flight == 0``.
    They are different facts and the frontier contract says so: a node whose task failed
    permanently stays ``failed``, its ancestors can never settle on top of it, and every
    ancestor is then ``in_flight`` forever. Waiting for zero in-flight nodes would hang
    on exactly the run that has something to report. What is left in flight when the
    queue is quiet is printed, because it is a finding rather than progress.
    """
    state = RunState.load(args.state)
    api = Api(state.api, args.token)
    started = time.monotonic()

    while True:
        frontier = api.get(f"/ingest/file/{state.collection_id}/frontier")
        sample = {"at": now_iso(), "elapsed_s": round(time.monotonic() - started, 1), **frontier}
        state.frontier_samples.append(sample)
        state.save(args.state)
        print(
            f"[{sample['elapsed_s']:>8.1f}s] settled={frontier['nodes_settled']:<6} "
            f"in_flight={frontier['nodes_in_flight']:<5} failed={frontier['nodes_failed']:<4} "
            f"total={frontier['nodes_total']:<6} tasks_unfinished={frontier['tasks_unfinished']}"
        )
        if frontier["tasks_unfinished"] == 0:
            if frontier["nodes_in_flight"]:
                print(
                    f"the queue is quiet with {frontier['nodes_in_flight']} nodes still "
                    f"in flight and {frontier['nodes_failed']} failed: the frontier is "
                    "blocked on something other than the queue"
                )
            break
        if args.max_seconds and (time.monotonic() - started) > args.max_seconds:
            print("stopped waiting: --max-seconds reached; the queue is still working")
            break
        time.sleep(args.poll)

    state.drain_finished_at = now_iso()
    state.save(args.state)
    api.close()
    return state


# ---------------------------------------------------------------------------
# phase: index
# ---------------------------------------------------------------------------


def phase_index(args: argparse.Namespace) -> dict:
    """Give the tree a BM25 index.

    This is the step spec 11.5 has not built. Path B enqueues no indexing task, so a
    queued ingestion produces a tree that vector search finds and BM25 does not. Until
    membership follows the tree, a caller has to say so explicitly — which is what this
    does, and what its presence here records.
    """
    state = RunState.load(args.state)
    api = Api(state.api, args.token)

    existing = {index["name"] for index in api.get("/indexes")}
    if state.index_name not in existing:
        api.post("/indexes", json={"name": state.index_name, "description": "e2e ingest run"})
    api.post(
        f"/indexes/{state.index_name}/roots",
        params={"root_document_id": state.collection_id},
    )
    started = time.monotonic()
    result = api.post(f"/indexes/{state.index_name}/refresh")
    result["refresh_seconds"] = round(time.monotonic() - started, 1)
    print(json.dumps(result, indent=2))
    api.close()
    return result


# ---------------------------------------------------------------------------
# phase: search
# ---------------------------------------------------------------------------


def phase_search(args: argparse.Namespace) -> list:
    state = RunState.load(args.state)
    api = Api(state.api, args.token)
    queries = DEFAULT_QUERIES
    if args.queries:
        queries = [line for line in Path(args.queries).read_text().splitlines() if line.strip()]

    results = []
    for query in queries:
        row: dict = {"query": query, "methods": {}}
        for method in SEARCH_METHODS:
            body = {"query": query, "limit": args.top_k}
            # `/search/bm25` takes its index as a QUERY parameter — `SearchRequest` has no
            # index field — while `/search/hybrid` takes one in the body. Sending it the
            # wrong way is silently ignored and searches the `default` index instead, so
            # the two calls below are shaped differently on purpose.
            params = {"index_name": state.index_name} if method == "bm25" else None
            try:
                started = time.monotonic()
                response = api.post(f"/search/{method}", json=body, params=params)
                elapsed = round((time.monotonic() - started) * 1000)
                hits = response.get("results", response) if isinstance(response, dict) else response
                row["methods"][method] = {
                    "ms": elapsed,
                    "hits": [_hit(h) for h in hits[: args.top_k]],
                }
            except httpx.HTTPStatusError as error:
                row["methods"][method] = {"error": f"HTTP {error.response.status_code}"}

        started = time.monotonic()
        hybrid = api.post(
            "/search/hybrid",
            json={
                "query": query,
                "limit": args.top_k,
                "methods": SEARCH_METHODS,
                "index_name": state.index_name,
            },
        )
        hits = hybrid.get("results", hybrid) if isinstance(hybrid, dict) else hybrid
        row["methods"]["hybrid_rrf"] = {
            "ms": round((time.monotonic() - started) * 1000),
            "hits": [_hit(h) for h in hits[: args.top_k]],
        }
        results.append(row)
        print(f"\n=== {query}")
        for method, payload in row["methods"].items():
            if "error" in payload:
                print(f"  {method:<12} {payload['error']}")
                continue
            print(f"  {method:<12} {payload['ms']:>5}ms")
            for hit in payload["hits"]:
                print(f"      {hit['score']:.4f}  [{hit['usetype']}] {hit['title'][:80]}")

    api.close()
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
    return results


def _hit(hit: dict) -> dict:
    """One `SearchResultItem`: `{document, score, method}`, the node nested under `document`."""
    document = hit.get("document") or {}
    return {
        "document_id": document.get("id"),
        "title": (document.get("title") or "")[:120],
        "usetype": document.get("usetype"),
        "score": float(hit.get("score", 0.0)),
    }


# ---------------------------------------------------------------------------
# phase: report
# ---------------------------------------------------------------------------


def phase_report(args: argparse.Namespace) -> str:
    state = RunState.load(args.state)
    conn = psycopg2.connect(db_dsn(args.dsn))
    conn.set_session(readonly=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    root = state.collection_id

    def q(sql: str, params: dict) -> list:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    # Every node in the run: the collection itself and everything whose path contains it.
    scope = "(d.id = %(root)s OR d.path @> %(rootjson)s::jsonb)"
    params = {"root": root, "rootjson": json.dumps([root])}

    lines: list[str] = []
    add = lines.append

    add(f"# End-to-end ingest run — {state.collection_title}")
    add("")
    add(f"- collection node: `{root}`")
    add(f"- uploaded: {state.upload_started_at} → {state.upload_finished_at}")
    add(f"- queue drained: {state.drain_finished_at or '(not drained)'}")
    if state.frontier_samples:
        add(f"- wall clock in the queue: {state.frontier_samples[-1]['elapsed_s'] / 60:.1f} min")
    add("")

    # -- 1. did every file finish? ------------------------------------------
    add("## 1. Did every file finish?")
    add("")
    refused = [f for f in state.files if "upload_error" in f]
    add(f"Files offered: **{len(state.files)}**, refused at upload: **{len(refused)}**")
    for record in refused[:20]:
        add(f"  - `{record['name']}` — {record['upload_error']}")
    add("")

    add("File nodes by lifecycle state:")
    add("")
    add(
        _table(
            q(
                f"""
        SELECT d.settled, COUNT(*) AS files
        FROM documents d WHERE {scope} AND d.usetype = 'file'
        GROUP BY d.settled ORDER BY files DESC
    """,
                params,
            )
        )
    )
    add("")

    failed_files = q(
        f"""
        SELECT d.id, d.title, d.settled
        FROM documents d WHERE {scope} AND d.usetype = 'file' AND d.settled <> 'settled'
        ORDER BY d.id LIMIT 50
    """,
        params,
    )
    if failed_files:
        add("File nodes that did not settle:")
        add("")
        add(_table(failed_files))
        add("")

    add("Tasks by type and status:")
    add("")
    add(
        _table(
            q(
                f"""
        SELECT t.task_type, t.status, COUNT(*) AS n
        FROM task_queue t JOIN documents d ON d.id = t.scope_document_id
        WHERE {scope} GROUP BY t.task_type, t.status ORDER BY t.task_type, t.status
    """,
                params,
            )
        )
    )
    add("")

    failures = q(
        f"""
        SELECT t.task_type, t.error_type, COUNT(*) AS n,
               MIN(LEFT(t.error, 200)) AS example, MIN(d.title) AS example_document
        FROM task_queue t JOIN documents d ON d.id = t.scope_document_id
        WHERE {scope} AND t.status = 'failed'
        GROUP BY t.task_type, t.error_type ORDER BY n DESC LIMIT 40
    """,
        params,
    )
    if failures:
        add("Failed tasks, grouped by the error they raised:")
        add("")
        add(_table(failures))
        add("")
    else:
        add("No task failed.")
        add("")

    skipped = q(
        f"""
        SELECT a->>'task' AS task, a->'detail'->>'reason' AS reason, COUNT(*) AS n
        FROM documents d
        JOIN document_evidence ev ON ev.document_id = d.id AND ev.name = 'attempts',
        LATERAL jsonb_array_elements(COALESCE(ev.value, '[]'::jsonb)) a
        WHERE {scope} AND a->>'status' = 'skipped'
        GROUP BY 1, 2 ORDER BY n DESC LIMIT 30
    """,
        params,
    )
    if skipped:
        add("Work recorded as skipped, with the reason the handler gave:")
        add("")
        add(_table(skipped))
        add("")

    # -- 2. what did the tree become? ---------------------------------------
    add("## 2. What did the tree become?")
    add("")
    add(
        _table(
            q(
                f"""
        SELECT COALESCE(d.usetype, '(none)') AS usetype, COUNT(*) AS nodes,
               COUNT(*) FILTER (WHERE d.settled = 'settled') AS settled,
               COUNT(*) FILTER (WHERE d.content IS NOT NULL) AS with_content,
               ROUND(AVG(jsonb_array_length(COALESCE(d.path, '[]'::jsonb)))::numeric, 2) AS avg_depth
        FROM documents d WHERE {scope}
        GROUP BY 1 ORDER BY nodes DESC
    """,
                params,
            )
        )
    )
    add("")

    add("Children per file node (the tree each document became):")
    add("")
    add(
        _table(
            q(
                f"""
        WITH counts AS (
            SELECT d.id, (SELECT COUNT(*) FROM documents c
                          WHERE c.path @> to_jsonb(ARRAY[d.id])) AS descendants
            FROM documents d WHERE {scope} AND d.usetype = 'file'
        )
        SELECT COUNT(*) AS file_nodes, MIN(descendants) AS min, MAX(descendants) AS max,
               ROUND(AVG(descendants)::numeric, 1) AS mean,
               PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY descendants) AS median
        FROM counts
    """,
                params,
            )
        )
    )
    add("")

    add("`effective_content` — how each node above the leaves got its vector:")
    add("")
    add(
        _table(
            q(
                f"""
        SELECT d.usetype,
               ev.value->>'method' AS method,
               COUNT(*) AS n
        FROM documents d
        JOIN document_evidence ev
          ON ev.document_id = d.id AND ev.name = 'effective_content'
        WHERE {scope}
        GROUP BY 1, 2 ORDER BY n DESC
    """,
                params,
            )
        )
    )
    add("")

    # -- 3. is it retrievable? ----------------------------------------------
    add("## 3. Is it retrievable?")
    add("")
    add("Document vectors, by usetype:")
    add("")
    add(
        _table(
            q(
                f"""
        SELECT COALESCE(d.usetype, '(none)') AS usetype, COUNT(*) AS nodes,
               COUNT(*) FILTER (WHERE d.embed IS NOT NULL) AS embedded,
               COUNT(*) FILTER (WHERE d.embed IS NULL AND d.content IS NOT NULL)
                   AS content_but_no_vector
        FROM documents d WHERE {scope} AND d.settled = 'settled'
        GROUP BY 1 ORDER BY nodes DESC
    """,
                params,
            )
        )
    )
    add("")

    add(
        _table(
            q(
                f"""
        SELECT COUNT(DISTINCT te.document_id) AS nodes_with_token_embeddings,
               COUNT(*) AS token_vectors
        FROM token_embeddings te JOIN documents d ON d.id = te.document_id
        WHERE {scope}
    """,
                params,
            )
        )
    )
    add("")

    index_rows = q(
        """
        SELECT si.name, si.total_docs, ROUND(si.avg_doc_length::numeric, 1) AS avg_doc_length,
               (SELECT COUNT(*) FROM search_index_entries e WHERE e.index_id = si.id) AS entries,
               (SELECT COUNT(*) FROM search_term_stats s WHERE s.index_id = si.id) AS terms
        FROM search_indexes si WHERE si.name = %(name)s
    """,
        {"name": state.index_name},
    )
    add("BM25 index:")
    add("")
    add(_table(index_rows) if index_rows else "_no index of that name exists_")
    add("")

    # -- 4. what did it cost? -----------------------------------------------
    add("## 4. What did it cost?")
    add("")
    add(
        _table(
            q(
                f"""
        SELECT t.task_type, COUNT(*) AS runs,
               ROUND(SUM(EXTRACT(EPOCH FROM (t.completed_at - t.started_at)))::numeric, 1)
                   AS total_s,
               ROUND(AVG(EXTRACT(EPOCH FROM (t.completed_at - t.started_at)))::numeric, 2)
                   AS mean_s,
               ROUND(PERCENTILE_DISC(0.5) WITHIN GROUP (
                   ORDER BY EXTRACT(EPOCH FROM (t.completed_at - t.started_at)))::numeric, 2)
                   AS median_s,
               ROUND(MAX(EXTRACT(EPOCH FROM (t.completed_at - t.started_at)))::numeric, 1)
                   AS max_s
        FROM task_queue t JOIN documents d ON d.id = t.scope_document_id
        WHERE {scope} AND t.completed_at IS NOT NULL AND t.started_at IS NOT NULL
        GROUP BY t.task_type ORDER BY total_s DESC NULLS LAST
    """,
                params,
            )
        )
    )
    add("")

    if len(state.frontier_samples) > 1:
        first, last = state.frontier_samples[0], state.frontier_samples[-1]
        span = max(last["elapsed_s"] - first["elapsed_s"], 1.0)
        made = last["nodes_total"] - first["nodes_total"]
        add(
            f"Nodes created during the drain: **{made}** over {span / 60:.1f} min "
            f"({made / (span / 60):.1f} nodes/min)."
        )
        add("")

    conn.close()
    report = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(report)
        print(f"wrote {args.out}")
    else:
        print(report)
    return report


def _table(rows: list) -> str:
    """A markdown table, or a sentence saying there were no rows."""
    if not rows:
        return "_(no rows)_"
    headers = list(rows[0].keys())
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join("" if row[h] is None else str(row[h]) for h in headers) + " |")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default=os.environ.get("JMFTS_API_URL", DEFAULT_API))
    parser.add_argument("--token", default=os.environ.get("JMFTS_API_TOKEN"))
    parser.add_argument("--state", type=Path, default=Path("e2e_run.json"))
    parser.add_argument("--dsn", default=None, help="read-only DSN for the report phase")
    sub = parser.add_subparsers(dest="phase", required=True)

    up = sub.add_parser("upload")
    up.add_argument("directories", nargs="+")
    up.add_argument("--collection", default="e2e ingest run")
    up.add_argument("--index", default="e2e")
    up.add_argument("--options", default=None, help="ingest options as JSON")
    up.add_argument("--limit", type=int, default=0, help="at most N files per directory")

    wait = sub.add_parser("wait")
    wait.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS)
    wait.add_argument("--max-seconds", type=float, default=0.0)

    sub.add_parser("index")

    rep = sub.add_parser("report")
    rep.add_argument("--out", default=None)

    search = sub.add_parser("search")
    search.add_argument("--queries", default=None, help="file with one query per line")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--out", default=None)

    every = sub.add_parser("all")
    every.add_argument("directories", nargs="+")
    every.add_argument("--collection", default="e2e ingest run")
    every.add_argument("--index", default="e2e")
    every.add_argument("--options", default=None)
    every.add_argument("--limit", type=int, default=0)
    every.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS)
    every.add_argument("--max-seconds", type=float, default=0.0)
    every.add_argument("--queries", default=None)
    every.add_argument("--top-k", type=int, default=5)
    every.add_argument("--out", default=None)

    args = parser.parse_args(argv)

    if args.phase == "upload":
        phase_upload(args)
    elif args.phase == "wait":
        phase_wait(args)
    elif args.phase == "index":
        phase_index(args)
    elif args.phase == "report":
        phase_report(args)
    elif args.phase == "search":
        phase_search(args)
    elif args.phase == "all":
        phase_upload(args)
        phase_wait(args)
        phase_index(args)
        phase_search(args)
        phase_report(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
