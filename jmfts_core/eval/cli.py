"""``jmfts-eval`` — reproduce the README's retrieval numbers, or measure new arms.

    jmfts-eval beir --list
    jmfts-eval beir --dataset scifact nfcorpus --data-dir DIR \\
        --methods vector bm25 hybrid rrf 'vector→crossenc@100' 'vector→maxsim@200'
    jmfts-eval beir --suite full --data-dir DIR --over-window skip
    jmfts-eval beir --dataset scifact --data-dir DIR --eval-only --methods 'hybrid->maxsim@100'

Ported from ``benchmarks/beir/run.py`` on the pre-0.1.0 history. A subcommand per benchmark
so that MultiHop-RAG (deferred) has somewhere to go.

**Where it writes.** The database is whatever ``JMFTS_DB_*`` names, and a database that holds
anything but BEIR corpora is refused for writes (``--allow-shared-db`` to override). The
results are one JSON file per dataset, ``<output-dir>/<dataset>_<code>_<host>.json``, where
``<code>`` is the short commit (``-dirty`` when the tree has uncommitted changes to tracked
files) or ``v<version>`` for an installed wheel. An existing file is not overwritten unless
``--overwrite`` says so. Nothing is written into the source tree.

**Who it runs as.** No principal is bound, which every in-process caller reads as the owner:
no access filtering. The result file says so, because a number measured through a
principal's read gate would be a different number.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from jmfts_core.eval.beir_config import (
    BEIR_DATASETS,
    BEIR_SUITES,
    DATA_DIR_ENV,
    DataDirNotConfigured,
    resolve_data_dir,
)
from jmfts_core.eval.beir_evaluate import MethodConfig, MethodOutcome, parse_method
from jmfts_core.eval.beir_loader import (
    OVER_WINDOW_POLICIES,
    MalformedDataset,
    NotIngested,
    OverWindowError,
    SharedDatabaseRefused,
)

#: Refusals the harness raises on purpose. Each message says what to do, so the CLI prints
#: it and exits 1; anything else is a defect and keeps its traceback.
_REFUSALS = (MalformedDataset, NotIngested, OverWindowError, SharedDatabaseRefused, FileExistsError)

#: Bumped when the result file's shape changes in a way a reader would need to know.
RESULT_SCHEMA = "jmfts-eval/beir/1"

DEFAULT_METHODS = ["vector", "bm25", "hybrid"]


# =============================================================================
# Provenance
# =============================================================================


def code_identity() -> dict:
    """Which code is running: the commit when ``jmfts_core`` is a tracked git checkout,
    else the package version alone.

    "Tracked" is checked with ``git ls-files`` on this package's own ``__init__.py`` rather
    than by asking whether the directory is inside some work tree: a wheel installed into a
    ``.venv`` that happens to sit inside a clone is inside a work tree, and reporting that
    clone's HEAD would name code that is not the code running.
    """
    import jmfts_core

    pkg_dir = Path(jmfts_core.__file__).resolve().parent
    identity: dict = {"version": jmfts_core.__version__, "commit": None, "dirty": None}

    def git(*args: str) -> Optional[str]:
        try:
            done = subprocess.run(
                ["git", "-C", str(pkg_dir), *args], capture_output=True, text=True, timeout=30
            )
        except FileNotFoundError:  # no git on PATH: an installed wheel, by the rule above
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    if git("ls-files", "--error-unmatch", "__init__.py") is None:
        return identity
    sha = git("rev-parse", "HEAD")
    if sha is None:
        raise RuntimeError(f"{pkg_dir} is tracked by git but `git rev-parse HEAD` failed")
    status = git("status", "--porcelain", "--untracked-files=no")
    if status is None:
        raise RuntimeError(f"{pkg_dir} is tracked by git but `git status` failed")
    identity["commit"] = sha
    identity["dirty"] = bool(status)
    return identity


def code_tag(identity: dict) -> str:
    """The ``<code>`` part of the default file name."""
    if identity["commit"]:
        return identity["commit"][:7] + ("-dirty" if identity["dirty"] else "")
    return f"v{identity['version']}"


def host_tag() -> str:
    return re.sub(r"[^A-Za-z0-9.-]", "_", socket.gethostname()) or "unknown-host"


def principal_record() -> dict:
    from jmfts_core.principal_context import get_current_principal

    principal = get_current_principal()
    if principal is None:
        return {"bound": False, "effective": "owner", "access_filtering": False}
    return {"bound": True, "name": principal.name, "is_owner": principal.is_owner}


def model_record() -> dict:
    """The models a run used, as configured in this process (and as the runner reports)."""
    from jmfts_core.config import get_settings
    from jmfts_core.embedder import RemoteEmbedder, get_embedder

    settings = get_settings()
    embedder = get_embedder()
    record = {
        # Queries are always embedded in this process (vector_search_text), whatever the
        # write path does — so the configured model is the query model.
        "embedding_model": settings.embedding_model,
        "embedding_revision": settings.embedding_revision or None,
        "embedding_device": settings.embedding_device,
        "document_embedder": "runner" if isinstance(embedder, RemoteEmbedder) else "local",
        "reranker_model_configured": settings.reranker_model,
        "embedding_token_window": settings.embedding_token_window,
    }
    if isinstance(embedder, RemoteEmbedder):
        info = embedder.info()
        record["runner_model"] = info.model
        record["runner_device"] = info.device
    return record


# =============================================================================
# Arguments
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jmfts-eval",
        description="Measure JMFTS retrieval against public benchmarks.",
    )
    sub = parser.add_subparsers(dest="benchmark", required=True)
    beir = sub.add_parser(
        "beir",
        help="BEIR datasets: ingest, query, nDCG@10 / MAP / recall / MRR",
        description=(
            "Ingest BEIR datasets into the database JMFTS_DB_* names and evaluate retrieval "
            "methods against their qrels. One result JSON per dataset."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "methods: vector | bm25 | fulltext | maxsim | hybrid | rrf | hybrid_vNNbNN, each\n"
            "optionally followed by →maxsim@DEPTH[:tTIER] or →crossenc@DEPTH ('->' works too).\n"
            "e.g. 'vector→crossenc@100'  'hybrid->maxsim@200:t15'  hybrid_v70b30"
        ),
    )
    which = beir.add_mutually_exclusive_group(required=True)
    which.add_argument("--dataset", "-d", nargs="+", metavar="NAME", help="dataset(s) to run")
    which.add_argument("--suite", choices=sorted(BEIR_SUITES), help="a named set of datasets")
    which.add_argument("--list", action="store_true", help="list the datasets and exit")
    beir.add_argument(
        "--methods",
        nargs="+",
        default=DEFAULT_METHODS,
        metavar="METHOD",
        help=f"methods to evaluate (default: {' '.join(DEFAULT_METHODS)})",
    )
    beir.add_argument(
        "--data-dir",
        help=f"where datasets are read from and downloaded to (default: ${DATA_DIR_ENV}; "
        "one of the two is required)",
    )
    beir.add_argument(
        "--limit",
        type=int,
        default=100,
        help="results kept per query (default 100; below 100, Recall@100 is not reported)",
    )
    beir.add_argument(
        "--over-window",
        choices=OVER_WINDOW_POLICIES,
        default="error",
        help="documents longer than the token window: 'error' stops before anything is "
        "written (default); 'skip' leaves them out and records how many. Never truncated.",
    )
    beir.add_argument(
        "--reranker-model",
        help="cross-encoder for crossenc arms (default: JMFTS_RERANKER_MODEL)",
    )
    mode = beir.add_mutually_exclusive_group()
    mode.add_argument(
        "--eval-only",
        action="store_true",
        help="query an earlier ingest instead of wiping and re-ingesting",
    )
    mode.add_argument("--ingest-only", action="store_true", help="ingest and index, no queries")
    beir.add_argument(
        "--output-dir",
        default="eval-results",
        help="directory for result files (default: ./eval-results)",
    )
    beir.add_argument("--output", help="result file path; only with a single dataset")
    beir.add_argument("--overwrite", action="store_true", help="replace an existing result file")
    beir.add_argument(
        "--allow-shared-db",
        action="store_true",
        help="write into a database that holds documents other than BEIR corpora",
    )
    return parser


def _list_datasets() -> None:
    for name, info in BEIR_DATASETS.items():
        suites = [s for s, members in sorted(BEIR_SUITES.items()) if name in members]
        tags = f" [{', '.join(suites)}]" if suites else ""
        print(f"  {name:<12} {info['corpus_size']:>8} docs  {info['description']}{tags}")


# =============================================================================
# Run
# =============================================================================


def _result_path(args, dataset: str, tag: str) -> Path:
    if args.output:
        return Path(args.output)
    return Path(args.output_dir) / f"{dataset}_{tag}_{host_tag()}.json"


def run_beir(args, parser: argparse.ArgumentParser) -> int:
    datasets = args.dataset or BEIR_SUITES[args.suite]
    for name in datasets:
        if name not in BEIR_DATASETS:
            parser.error(f"unknown dataset {name!r}; --list shows them")
    if args.output and len(datasets) > 1:
        parser.error("--output names one file; with several datasets use --output-dir")
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    try:
        configs: list[MethodConfig] = [parse_method(m) for m in args.methods]
    except ValueError as exc:
        parser.error(str(exc))
    try:
        data_dir = resolve_data_dir(args.data_dir)
    except DataDirNotConfigured as exc:
        parser.error(str(exc))

    identity = code_identity()
    tag = code_tag(identity)
    outputs = {name: _result_path(args, name, tag) for name in datasets}
    if not args.ingest_only and not args.overwrite:
        existing = [str(p) for p in outputs.values() if p.exists()]
        if existing:
            parser.error(f"result file(s) exist: {existing}; pass --overwrite or --output")

    # Everything that can fail cheaply fails before the first download or ingest.
    from jmfts_core.config import get_settings
    from jmfts_core.database import get_session
    from jmfts_core.embedder import get_embedder
    from jmfts_core.eval import beir_loader as loader
    from jmfts_core.eval.beir_evaluate import (
        evaluate_method,
        format_dataset_table,
        format_summary_table,
    )

    settings = get_settings()
    reranker = None
    if not args.ingest_only and any(c.rerank_method == "crossenc" for c in configs):
        from jmfts_core.reranker import RerankerService, get_reranker_service

        reranker = (
            RerankerService(model=args.reranker_model)
            if args.reranker_model
            else get_reranker_service()
        )
    models = model_record()
    embedder = get_embedder()

    print("jmfts-eval beir")
    print(f"  code:      {tag} (version {identity['version']})")
    print(f"  database:  {settings.db_name}")
    print(f"  datasets:  {', '.join(datasets)}")
    if not args.ingest_only:
        print(f"  methods:   {', '.join(c.name for c in configs)}")
    print(f"  embedding: {models['embedding_model']} ({models['document_embedder']} documents)")
    if reranker is not None:
        print(f"  reranker:  {reranker.model_name}")

    per_dataset: dict[str, dict[str, MethodOutcome]] = {}
    for name in datasets:
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        t0 = time.time()
        print(f"\n== {name}")
        files = loader.load_files(data_dir, name)
        with get_session() as session:
            if args.eval_only:
                data = loader.load_ingested(session, name)
                loader.check_files_match(data.record, files)
                print(f"  Reusing corpus root {data.root_id} ({len(data.id_map)} documents)")
            else:
                loader.refuse_shared_database(session, allow_shared_db=args.allow_shared_db)
                plan = loader.plan_ingest(
                    files,
                    over_window=args.over_window,
                    check_fit=lambda text: embedder.check_fit(
                        text, with_tokens=True, prefix=loader.DOCUMENT_PREFIX
                    ),
                )
                loader.wipe_dataset(session, name)
                data = loader.ingest_dataset(
                    session,
                    files,
                    plan,
                    over_window=args.over_window,
                    provenance=identity,
                    embedding_model=embedder.model_name,
                )
            skipped = data.record["over_window_skipped"]
            if skipped:
                print(
                    f"  OVER WINDOW: {skipped} documents left out of the corpus "
                    f"({data.record['over_window_skipped_relevant']} of them judged relevant)"
                )
            if args.ingest_only:
                print(f"  {name} ingested in {time.time() - t0:.1f}s")
                continue

            outcomes: dict[str, MethodOutcome] = {}
            for config in configs:
                print(f"  -- {config.name}")
                t1 = time.time()
                outcomes[config.name] = evaluate_method(
                    session, config, files, data, depth=args.limit, reranker=reranker
                )
                print(
                    f"     nDCG@10={outcomes[config.name].metrics.ndcg_at_10:.4f} "
                    f"({time.time() - t1:.1f}s)"
                )

        per_dataset[name] = outcomes
        print()
        print(format_dataset_table(name, outcomes))
        result = {
            "schema": RESULT_SCHEMA,
            "dataset": name,
            "started_at": started,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "code": identity,
            "host": socket.gethostname(),
            "python": sys.version.split()[0],
            "database": settings.db_name,
            "principal": principal_record(),
            "models": models,
            "retrieval_depth": args.limit,
            "corpus": {
                "docs": len(files.corpus),
                "queries": len(files.queries),
                "queries_evaluated": len(files.evaluated_queries),
                "data_sha256": files.sha256,
            },
            "over_window": {
                "policy": data.record["over_window_policy"],
                "skipped": skipped,
                "skipped_relevant": data.record["over_window_skipped_relevant"],
                "ids": data.record["over_window_ids"],
            },
            "ingest": {k: v for k, v in data.record.items() if not k.startswith("over_window")},
            "methods": {c.name: outcomes[c.name].as_dict(c) for c in configs},
        }
        out = outputs[name]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n")
        print(f"\n  wrote {out}")

    if len(per_dataset) > 1:
        print()
        print(format_summary_table(per_dataset))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.benchmark == "beir":
        if args.list:
            _list_datasets()
            return 0
        try:
            return run_beir(args, parser)
        except _REFUSALS as exc:
            print(f"jmfts-eval: {exc}", file=sys.stderr)
            return 1
    raise AssertionError(f"unhandled benchmark {args.benchmark!r}")


if __name__ == "__main__":
    sys.exit(main())
