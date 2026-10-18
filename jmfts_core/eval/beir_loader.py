"""Read, download, ingest and wipe a BEIR dataset.

Ported from ``benchmarks/beir/loader.py`` on the pre-0.1.0 history. What changed, and why:

- **Ingest goes through ``DocumentRepository.create(auto_embed=True)``.** The old loader
  bulk-inserted unembedded rows, then ran ``embed_batch_with_tokens`` and wrote
  ``TokenEmbedding`` tiers with its own copy of the tiering loop. That copy had drifted
  from the repository's (it kept the top 50% of tokens where ``embed_document`` keeps
  35%), so the old maxsim numbers measured a write path no deployment ran. Now the
  harness measures exactly what ``POST /documents`` writes, and it inherits the runner
  (``JMFTS_RUNNER_URL``) for free. The cost is one forward pass per document instead of
  one per 32; see the report this file was restored with.
- **Over-window documents are a decision, not an accident.** The token path now refuses
  text longer than ``embedding_token_window`` (``TextTooLongError``) instead of embedding a
  prefix. The old loader predates that refusal, so its numbers were computed over
  silently truncated documents. Here every document is measured with the tokenizer
  BEFORE anything is written, and ``policy`` says what happens: ``"error"`` (the default)
  stops the run naming how many and which; ``"skip"`` leaves them out, counts them, and
  records the count and the ids on the corpus root and in the result file. Nothing is
  ever truncated.
- **The wipe is the repository delete**, ``DocumentService.delete_document`` and
  ``IndexService.delete_index``, not hand-written ``DELETE`` statements that bypassed the
  blob/rendition unlink and the settle walk.
- **Download is stdlib** (``urllib`` + ``zipfile``). The ``beir`` package was only ever used
  for its download helper.
- **A shared database is refused** for writes unless asked for. The old CLI refused any
  database not named ``jmfts_beir``; a name is a convention on one machine, so the check
  is now what the name stood for — this database holds nothing but BEIR corpora.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import tempfile
import time
import urllib.request
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from jmfts_core.eval.beir_config import BEIR_URL_TEMPLATE, dataset_info
from jmfts_core.models.document import Document

#: The prefix ``DocumentRepository.embed_document`` embeds documents under. The fit check
#: must measure the same string the embedder will be handed, prefix included.
DOCUMENT_PREFIX = "search_document: "

#: The old loader's floor, kept: a document whose stripped text is shorter than this is
#: left out (and counted). ``DocumentRepository.create`` would store it but not embed it
#: (``len(content) > 10``), so it would be retrievable by BM25 and invisible to vector
#: search — a corpus that differs per method.
MIN_CONTENT_CHARS = 10

OVER_WINDOW_POLICIES = ("error", "skip")


def root_usetype(dataset: str) -> str:
    return f"beir:{dataset}:root"


def doc_usetype(dataset: str) -> str:
    return f"beir:{dataset}:doc"


def index_name_for(dataset: str) -> str:
    return f"beir_{dataset}"


# =============================================================================
# Reading
# =============================================================================


@dataclass
class BEIRFiles:
    """One dataset as read from disk."""

    name: str
    split: str
    corpus: dict[str, dict[str, str]]  # doc_id -> {"title", "text"}
    queries: dict[str, str]  # query_id -> text, ALL queries in queries.jsonl
    qrels: dict[str, dict[str, int]]  # query_id -> {doc_id: grade}, for `split`
    sha256: dict[str, str] = field(default_factory=dict)  # relative path -> digest

    @property
    def evaluated_queries(self) -> dict[str, str]:
        """Queries that have judgments in this split, in qrels order."""
        return {qid: self.queries[qid] for qid in self.qrels if qid in self.queries}


class MalformedDataset(ValueError):
    """A BEIR file is not in the shape BEIR publishes."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_beir_dataset(path: Path, name: str, split: str) -> BEIRFiles:
    """Read ``corpus.jsonl``, ``queries.jsonl`` and ``qrels/<split>.tsv`` under ``path``.

    Malformed input raises :class:`MalformedDataset` naming the file and line. The old
    reader skipped a qrels line with fewer than three fields; a dropped judgment changes
    every metric for its query, so it is an error here.
    """
    files = {
        "corpus.jsonl": path / "corpus.jsonl",
        "queries.jsonl": path / "queries.jsonl",
        f"qrels/{split}.tsv": path / "qrels" / f"{split}.tsv",
    }
    missing = [rel for rel, p in files.items() if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"{path} is not a BEIR dataset directory: missing {missing}")

    corpus: dict[str, dict[str, str]] = {}
    with open(files["corpus.jsonl"], encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
                corpus[str(doc["_id"])] = {
                    "title": doc.get("title") or "",
                    "text": doc.get("text") or "",
                }
            except (json.JSONDecodeError, KeyError) as exc:
                raise MalformedDataset(f"corpus.jsonl line {lineno}: {exc!r}") from exc

    queries: dict[str, str] = {}
    with open(files["queries.jsonl"], encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                q = json.loads(line)
                queries[str(q["_id"])] = q["text"]
            except (json.JSONDecodeError, KeyError) as exc:
                raise MalformedDataset(f"queries.jsonl line {lineno}: {exc!r}") from exc

    qrels: dict[str, dict[str, int]] = defaultdict(dict)
    qrel_path = files[f"qrels/{split}.tsv"]
    with open(qrel_path, encoding="utf-8") as fh:
        header = fh.readline().strip().split("\t")
        if header[:3] != ["query-id", "corpus-id", "score"]:
            raise MalformedDataset(
                f"{qrel_path}: expected header 'query-id<TAB>corpus-id<TAB>score', got {header}"
            )
        for lineno, line in enumerate(fh, 2):
            if not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                raise MalformedDataset(f"{qrel_path} line {lineno}: {len(parts)} fields, want 3")
            try:
                qrels[parts[0]][parts[1]] = int(parts[2])
            except ValueError as exc:
                raise MalformedDataset(f"{qrel_path} line {lineno}: {exc}") from exc

    return BEIRFiles(
        name=name,
        split=split,
        corpus=corpus,
        queries=queries,
        qrels=dict(qrels),
        sha256={rel: _sha256(p) for rel, p in files.items()},
    )


# =============================================================================
# Download
# =============================================================================


def _safe_extract(archive: zipfile.ZipFile, dest: Path) -> None:
    """Extract every member under ``dest``, refusing any that would land outside it."""
    root = dest.resolve()
    for member in archive.infolist():
        target = (dest / member.filename).resolve()
        if target != root and root not in target.parents:
            raise MalformedDataset(f"zip member {member.filename!r} escapes {dest}")
    archive.extractall(dest)


def ensure_dataset(
    data_dir: Path,
    name: str,
    *,
    url_template: str = BEIR_URL_TEMPLATE,
    log: Callable[[str], None] = print,
) -> Path:
    """The dataset's directory under ``data_dir``, downloading and unpacking it if absent.

    Atomic in the way that matters: the zip is fetched to a temporary file and unpacked
    into a temporary directory under ``data_dir``, and only a complete ``<name>/`` is
    renamed into place. An interrupted download leaves no half-dataset that a later run
    would read as a whole one.
    """
    beir_name = dataset_info(name)["beir_name"]
    target = data_dir / beir_name
    if (target / "corpus.jsonl").is_file():
        return target
    if target.exists():
        raise FileExistsError(
            f"{target} exists but has no corpus.jsonl. Remove it, or put the dataset there."
        )

    data_dir.mkdir(parents=True, exist_ok=True)
    url = url_template.format(name=beir_name)
    log(f"  Downloading {url}")
    with tempfile.TemporaryDirectory(dir=data_dir, prefix=f".{beir_name}-") as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / f"{beir_name}.zip"
        with urllib.request.urlopen(url, timeout=60) as response, open(zip_path, "wb") as out:
            shutil.copyfileobj(response, out, length=1 << 20)
        log(f"    {zip_path.stat().st_size / 1e6:.1f} MB")
        unpack = tmp_path / "unpacked"
        with zipfile.ZipFile(zip_path) as archive:
            _safe_extract(archive, unpack)
        extracted = unpack / beir_name
        if not (extracted / "corpus.jsonl").is_file():
            raise MalformedDataset(f"{url} did not unpack to {beir_name}/corpus.jsonl")
        extracted.rename(target)
    return target


def load_files(data_dir: Path, name: str, *, log: Callable[[str], None] = print) -> BEIRFiles:
    """Download if needed, then read. The one call the CLI makes per dataset."""
    info = dataset_info(name)
    path = ensure_dataset(data_dir, name, log=log)
    files = read_beir_dataset(path, name, info["split"])
    log(
        f"  {name}: {len(files.corpus)} docs, {len(files.queries)} queries, "
        f"{len(files.qrels)} with {info['split']} judgments "
        f"({len(files.evaluated_queries)} evaluable)"
    )
    return files


# =============================================================================
# Planning: what will be ingested, measured before anything is written
# =============================================================================


def doc_content(doc: dict[str, str]) -> str:
    """The text a BEIR document is stored and embedded as: title, blank line, body."""
    title, text = doc.get("title", ""), doc.get("text", "")
    return f"{title}\n\n{text}" if title and text else (title or text)


class OverWindowError(ValueError):
    """Documents exceed the token window and the policy is ``error``."""

    def __init__(self, dataset: str, over: list[tuple[str, int]], limit: int, total: int):
        self.dataset = dataset
        self.over = over
        self.limit = limit
        worst = sorted(over, key=lambda pair: -pair[1])[:5]
        shown = ", ".join(f"{doc_id} ({tokens} tokens)" for doc_id, tokens in worst)
        super().__init__(
            f"{dataset}: {len(over)} of {total} documents exceed the {limit}-token "
            f"token/maxsim window (longest: {shown}). Nothing was written. Re-run with "
            "--over-window skip to leave them out of the corpus; the count and the ids are "
            "recorded in the result file. They are never truncated."
        )


@dataclass
class IngestPlan:
    to_ingest: list[tuple[str, str, str]]  # (beir_id, title, content)
    skipped_empty: list[str]
    skipped_over_window: list[str]
    token_window: int


def plan_ingest(
    files: BEIRFiles,
    *,
    over_window: str,
    check_fit: Callable[[str], object],
    log: Callable[[str], None] = print,
) -> IngestPlan:
    """Sort every document into ingest / empty / over-window, using the tokenizer only.

    ``check_fit`` is ``embedder.check_fit`` bound to the token path; it returns a
    ``FitResult``. Run over the whole corpus before the first write, so that a policy of
    ``error`` fails in a minute rather than an hour into an ingest.
    """
    if over_window not in OVER_WINDOW_POLICIES:
        raise ValueError(f"over_window must be one of {OVER_WINDOW_POLICIES}, got {over_window!r}")
    to_ingest: list[tuple[str, str, str]] = []
    empty: list[str] = []
    over: list[tuple[str, int]] = []
    limit = 0
    t0 = time.time()
    for beir_id, doc in files.corpus.items():
        content = doc_content(doc)
        if len(content.strip()) < MIN_CONTENT_CHARS:
            empty.append(beir_id)
            continue
        fit = check_fit(content)
        limit = fit.limit
        if fit.truncated:
            over.append((beir_id, fit.token_count))
            continue
        to_ingest.append((beir_id, doc.get("title", ""), content))
    log(
        f"  Measured {len(files.corpus)} documents in {time.time() - t0:.1f}s: "
        f"{len(to_ingest)} fit, {len(over)} over the window, {len(empty)} empty"
    )
    if over and over_window == "error":
        raise OverWindowError(files.name, over, limit, len(files.corpus))
    return IngestPlan(
        to_ingest=to_ingest,
        skipped_empty=empty,
        skipped_over_window=[doc_id for doc_id, _ in over],
        token_window=limit,
    )


# =============================================================================
# The database: guard, wipe, ingest, reload
# =============================================================================


class SharedDatabaseRefused(RuntimeError):
    """The configured database holds documents that are not BEIR corpora."""


def refuse_shared_database(session: Session, *, allow_shared_db: bool) -> None:
    """Refuse to write into a database that holds anything but ``beir:*`` documents.

    An ingest writes thousands of documents and a wipe deletes a subtree; both belong in a
    database kept for the experiment. A JMFTS token means the shared knowledgebase, and
    one mistyped ``JMFTS_DB_NAME`` would otherwise put a benchmark corpus into it.
    """
    if allow_shared_db:
        return
    foreign = session.execute(
        select(func.count())
        .select_from(Document)
        .where(or_(Document.usetype.is_(None), ~Document.usetype.like("beir:%")))
    ).scalar_one()
    if foreign:
        raise SharedDatabaseRefused(
            f"This database holds {foreign} documents that are not BEIR corpora. Point "
            "JMFTS_DB_* at a database kept for evaluation (jmfts-init-db creates one), or "
            "pass --allow-shared-db to write the corpus beside them anyway."
        )


def wipe_dataset(session: Session, dataset: str, *, log: Callable[[str], None] = print) -> int:
    """Delete every corpus root for ``dataset`` and its BM25 index. Returns roots deleted.

    Through the services the REST API uses, so the blob/rendition unlink and the settle walk
    run exactly as they do for ``DELETE /documents/{id}``.
    """
    from jmfts_core.services.document_service import DocumentService
    from jmfts_core.services.index_service import IndexService
    from jmfts_core.repositories.search import SearchRepository

    roots = (
        session.execute(select(Document.id).where(Document.usetype == root_usetype(dataset)))
        .scalars()
        .all()
    )
    if SearchRepository(session).get_index(index_name_for(dataset)) is not None:
        IndexService(session).delete_index(index_name_for(dataset))
        log(f"  Deleted BM25 index {index_name_for(dataset)}")
    for root_id in roots:
        t0 = time.time()
        DocumentService(session).delete_document(root_id)
        log(f"  Deleted earlier {dataset} corpus (root {root_id}) in {time.time() - t0:.1f}s")
    return len(roots)


@dataclass
class IngestedDataset:
    """A dataset as it stands in the database, ready to query."""

    root_id: int
    id_map: dict[str, int]  # beir_id -> document id
    index_name: str
    record: dict  # what the ingest recorded on the root; see ingest_dataset


def _token_top_percent() -> float:
    """The share of tokens ``embed_document`` stores, read from its signature.

    Read rather than restated, so the result file reports what the write path actually
    did even after somebody changes the default.
    """
    from jmfts_core.repositories.document import DocumentRepository

    sig = inspect.signature(DocumentRepository.embed_document)
    return float(sig.parameters["token_top_percent"].default)


def ingest_dataset(
    session: Session,
    files: BEIRFiles,
    plan: IngestPlan,
    *,
    over_window: str,
    provenance: dict,
    embedding_model: str,
    commit_every: int = 200,
    log: Callable[[str], None] = print,
) -> IngestedDataset:
    """Write the planned documents under a fresh root, embed them, and build the BM25 index.

    The root carries no content, so no retrieval method can return it, and every search is
    scoped to its subtree. What the ingest measured is recorded on the root's
    ``structured_content["beir_ingest"]``, which is how an ``--eval-only`` run later knows
    how many documents were left out and under which code.
    """
    from jmfts_core.repositories.document import DocumentRepository
    from jmfts_core.repositories.search import SearchRepository

    dataset = files.name
    doc_repo = DocumentRepository(session)
    root = doc_repo.create(
        title=f"BEIR: {dataset}",
        content=None,
        usetype=root_usetype(dataset),
        auto_embed=False,
    )
    session.flush()
    root_id = root.id
    log(f"  Corpus root: id={root_id}; embedding {len(plan.to_ingest)} documents")

    id_map: dict[str, int] = {}
    t0 = time.time()
    total = len(plan.to_ingest)
    for i, (beir_id, title, content) in enumerate(plan.to_ingest, 1):
        doc = doc_repo.create(
            title=title[:200] if title else f"doc-{beir_id}",
            content=content,
            parent_id=root_id,
            usetype=doc_usetype(dataset),
            structured_content={"beir_id": beir_id},
            auto_embed=True,
            embed_tokens=True,
        )
        id_map[beir_id] = doc.id
        if i % commit_every == 0 or i == total:
            session.commit()
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0.0
            eta = (total - i) / rate if rate > 0 else 0.0
            log(f"    {i}/{total} ({rate:.1f} docs/s, ETA {eta:.0f}s)")
    embed_seconds = time.time() - t0

    search_repo = SearchRepository(session)
    index_name = index_name_for(dataset)
    t1 = time.time()
    search_repo.create_index(index_name, f"BEIR {dataset} corpus")
    if not search_repo.add_root_to_index(index_name, root_id):
        raise RuntimeError(f"index {index_name} vanished between create and add_root")
    session.flush()
    refreshed = search_repo.refresh_index(index_name)
    if "error" in refreshed:
        raise RuntimeError(f"refresh_index({index_name}) failed: {refreshed['error']}")
    log(f"  BM25 index {index_name}: {refreshed} ({time.time() - t1:.1f}s)")

    relevant = {d for qrel in files.qrels.values() for d, grade in qrel.items() if grade > 0}
    record = {
        "dataset": dataset,
        "split": files.split,
        "corpus_docs": len(files.corpus),
        "ingested_docs": len(id_map),
        "over_window_policy": over_window,
        "over_window_skipped": len(plan.skipped_over_window),
        "over_window_skipped_relevant": len(set(plan.skipped_over_window) & relevant),
        "over_window_ids": plan.skipped_over_window,
        "empty_skipped": len(plan.skipped_empty),
        "empty_ids": plan.skipped_empty,
        "token_window": plan.token_window,
        "token_top_percent": _token_top_percent(),
        "embedding_model": embedding_model,
        "embed_seconds": round(embed_seconds, 1),
        "data_sha256": files.sha256,
        "code": provenance,
    }
    root = doc_repo.get(root_id)
    root.structured_content = {"beir_ingest": record}
    session.commit()
    return IngestedDataset(root_id=root_id, id_map=id_map, index_name=index_name, record=record)


class NotIngested(LookupError):
    """``--eval-only`` asked for a dataset this database does not hold (exactly once)."""


def load_ingested(session: Session, dataset: str) -> IngestedDataset:
    """Find an earlier ingest of ``dataset`` for ``--eval-only``. Exactly one root, or raise."""
    from jmfts_core.repositories.search import SearchRepository

    roots = (
        session.execute(select(Document).where(Document.usetype == root_usetype(dataset)))
        .scalars()
        .all()
    )
    if not roots:
        raise NotIngested(f"{dataset} is not ingested in this database; run without --eval-only")
    if len(roots) > 1:
        raise NotIngested(
            f"{dataset} is ingested {len(roots)} times (roots {[r.id for r in roots]}); a "
            "run without --eval-only wipes them all and ingests once"
        )
    root = roots[0]
    record = (root.structured_content or {}).get("beir_ingest")
    if record is None:
        raise NotIngested(
            f"{dataset} root {root.id} carries no beir_ingest record, so how many documents "
            "it left out is unknown; re-ingest it"
        )
    if SearchRepository(session).get_index(index_name_for(dataset)) is None:
        raise NotIngested(f"{dataset} has no BM25 index {index_name_for(dataset)}; re-ingest it")
    rows = session.execute(
        select(Document.id, Document.structured_content["beir_id"].astext).where(
            Document.usetype == doc_usetype(dataset),
            Document.path.op("@>")(func.jsonb_build_array(root.id)),
        )
    ).all()
    id_map = {beir_id: doc_id for doc_id, beir_id in rows}
    if len(id_map) != record["ingested_docs"]:
        raise NotIngested(
            f"{dataset}: the root records {record['ingested_docs']} ingested documents and "
            f"{len(id_map)} are present; re-ingest it"
        )
    return IngestedDataset(
        root_id=root.id, id_map=id_map, index_name=index_name_for(dataset), record=record
    )


def check_files_match(record: dict, files: BEIRFiles) -> None:
    """Refuse ``--eval-only`` against data files other than the ones that were ingested."""
    if record.get("data_sha256") != files.sha256:
        raise NotIngested(
            f"{files.name}: the files on disk differ from the ones this corpus was ingested "
            "from (sha256 mismatch); re-ingest, or point --data-dir at the original files"
        )
