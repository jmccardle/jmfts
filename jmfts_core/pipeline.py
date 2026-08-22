"""General ingest pipeline with configurable registry — #61

Maps usetype names to pipeline definitions with default stage configs.
Each pipeline runs: parse -> chunk -> embed -> summarize -> extract_facts.

Parse/chunk logic is content-type-specific; summarize and extract_facts are
shared across all pipelines.  The conversation pipeline delegates to the
existing ``conversation_ingest`` module.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from jmfts_core.chunking import ChunkStrategy, chunk_text
from jmfts_client.contracts.attempt import AttemptRecord, param_fingerprint
from jmfts_core.conversation_ingest import (
    IngestResult,
    ParsedMessage,
    StageClock,
    StageResult,
    ingest_conversation,
    parse_adjutant_jsonl,
)
from jmfts_core.embedding import get_embedding_service
from jmfts_core.fact_extraction import extract_facts
from jmfts_core.repositories.document import DocumentRepository, compute_content_hash
from jmfts_core.repositories.search import SearchRepository
from jmfts_core.structural_splitting import split_on_headings
from jmfts_core.summarization import raptor_summarize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage & pipeline config
# ---------------------------------------------------------------------------


@dataclass
class StageConfig:
    """Configuration for a single pipeline stage."""

    enabled: bool = True
    params: dict = field(default_factory=dict)


@dataclass
class PipelineDefinition:
    """A named pipeline with default stage configurations."""

    name: str
    description: str
    default_stages: dict[str, StageConfig]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_registry: dict[str, PipelineDefinition] = {}


def register_pipeline(defn: PipelineDefinition) -> None:
    """Register a pipeline definition under its name."""
    _registry[defn.name] = defn


def get_pipeline(name: str) -> Optional[PipelineDefinition]:
    """Look up a pipeline by name.  Returns ``None`` if not found."""
    return _registry.get(name)


def list_pipelines() -> list[PipelineDefinition]:
    """Return all registered pipeline definitions."""
    return list(_registry.values())


# ---------------------------------------------------------------------------
# Built-in pipeline definitions
# ---------------------------------------------------------------------------

register_pipeline(
    PipelineDefinition(
        name="conversation",
        description=(
            "Conversation ingestion: parse JSONL/messages, chunk by turn, "
            "embed, optionally RAPTOR + fact extraction"
        ),
        default_stages={
            "parse": StageConfig(),
            "chunk": StageConfig(),
            # Opt-in PELT topic segmentation (off by default → default tree shape unchanged).
            "segment": StageConfig(enabled=False, params={"min_segment": 3, "max_segment": 10}),
            "summarize": StageConfig(params={"max_depth": 5, "min_cluster_size": 2}),
            "extract_facts": StageConfig(),
        },
    )
)

register_pipeline(
    PipelineDefinition(
        name="markdown",
        description=(
            "Markdown ingestion: structural split on headings, chunk sections, "
            "embed, optionally RAPTOR + fact extraction"
        ),
        default_stages={
            "parse": StageConfig(),
            "chunk": StageConfig(
                params={"strategy": "paragraph", "max_tokens": 200, "min_chunk_length": 20}
            ),
            "summarize": StageConfig(params={"max_depth": 5, "min_cluster_size": 2}),
            "extract_facts": StageConfig(),
        },
    )
)

register_pipeline(
    PipelineDefinition(
        name="raw",
        description=(
            "Raw text ingestion: sentence chunk, embed, " "optionally RAPTOR + fact extraction"
        ),
        default_stages={
            "parse": StageConfig(),
            "chunk": StageConfig(
                params={"strategy": "sentence", "max_tokens": 200, "min_chunk_length": 20}
            ),
            "summarize": StageConfig(params={"max_depth": 5, "min_cluster_size": 2}),
            "extract_facts": StageConfig(),
        },
    )
)

register_pipeline(
    PipelineDefinition(
        name="transcript",
        description=(
            "Voice transcript ingestion: sentence chunk with transcript metadata, "
            "embed, optionally RAPTOR + fact extraction"
        ),
        default_stages={
            "parse": StageConfig(),
            "chunk": StageConfig(
                params={"strategy": "sentence", "max_tokens": 200, "min_chunk_length": 20}
            ),
            "summarize": StageConfig(params={"max_depth": 5, "min_cluster_size": 2}),
            "extract_facts": StageConfig(),
        },
    )
)


# Wiki source-fetch pipelines (Phase 3). Content is the source identifier
# (URL string, arxiv id, PDF path), not raw text — the handler fetches and
# converts before delegating to the markdown pipeline's stages.
register_pipeline(
    PipelineDefinition(
        name="wiki:url",
        description="Fetch a URL, convert HTML→markdown, then run markdown pipeline.",
        default_stages={
            "parse": StageConfig(),
            "chunk": StageConfig(
                params={"strategy": "paragraph", "max_tokens": 200, "min_chunk_length": 20}
            ),
            "summarize": StageConfig(enabled=False, params={"max_depth": 5, "min_cluster_size": 2}),
            "extract_facts": StageConfig(enabled=False),
        },
    )
)

register_pipeline(
    PipelineDefinition(
        name="wiki:arxiv",
        description="Fetch an arXiv paper (metadata + PDF), convert to markdown.",
        default_stages={
            "parse": StageConfig(),
            "chunk": StageConfig(
                params={"strategy": "paragraph", "max_tokens": 200, "min_chunk_length": 20}
            ),
            "summarize": StageConfig(enabled=False, params={"max_depth": 5, "min_cluster_size": 2}),
            "extract_facts": StageConfig(enabled=False),
        },
    )
)

register_pipeline(
    PipelineDefinition(
        name="wiki:pdf",
        description="Convert a local PDF (file path) to markdown.",
        default_stages={
            "parse": StageConfig(),
            "chunk": StageConfig(
                params={"strategy": "paragraph", "max_tokens": 200, "min_chunk_length": 20}
            ),
            "summarize": StageConfig(enabled=False, params={"max_depth": 5, "min_cluster_size": 2}),
            "extract_facts": StageConfig(enabled=False),
        },
    )
)


# ---------------------------------------------------------------------------
# Stage resolution (merge defaults + overrides)
# ---------------------------------------------------------------------------


def _resolve_stages(
    pipeline: PipelineDefinition,
    overrides: Optional[dict[str, Any]],
) -> dict[str, StageConfig]:
    """Merge pipeline defaults with caller-supplied overrides."""
    stages: dict[str, StageConfig] = {}
    for name, default_cfg in pipeline.default_stages.items():
        stages[name] = StageConfig(
            enabled=default_cfg.enabled,
            params=dict(default_cfg.params),
        )

    if not overrides:
        return stages

    for stage_name, override in overrides.items():
        if stage_name not in stages:
            continue
        if isinstance(override, bool):
            stages[stage_name].enabled = override
        elif isinstance(override, dict):
            if "enabled" in override:
                stages[stage_name].enabled = override["enabled"]
            # Accept params nested under "params" key or as flat keys
            if "params" in override:
                stages[stage_name].params.update(override["params"])
            flat = {k: v for k, v in override.items() if k not in ("enabled", "params")}
            if flat:
                stages[stage_name].params.update(flat)

    return stages


# ---------------------------------------------------------------------------
# Shared later stages (summarize, extract_facts)
# ---------------------------------------------------------------------------


async def _run_summarize(
    session: Session,
    root_id: int,
    stages: dict[str, StageConfig],
    *,
    llm_model: Optional[str] = None,
) -> tuple[int, int, StageResult]:
    """RAPTOR summarization.  Returns (summary_count, tree_depth, stage)."""
    cfg = stages.get("summarize")
    if cfg is None or not cfg.enabled:
        return 0, 1, StageResult(stage="summarize", status="skipped", detail={"reason": "disabled"})

    repo = DocumentRepository(session)
    try:
        # No lifecycle filter, deliberately: get_children carries no `settled` predicate
        # (it walks by parent_id, not by the partial path index) and this stage must
        # summarize the children the earlier stages just created, which are exactly the
        # ones that are not settled yet. Filtering here would make RAPTOR skip the tree
        # it was invoked to summarize.
        children = repo.get_children(root_id, depth=1, limit=10000)
        embedded = [c for c in children if c.embed is not None]

        if len(embedded) < 2:
            return (
                0,
                1,
                StageResult(
                    stage="summarize",
                    status="skipped",
                    detail={"reason": f"Only {len(embedded)} embedded children (need >= 2)"},
                ),
            )

        t0 = time.monotonic()
        raptor_result = await raptor_summarize(
            document_id=root_id,
            session=session,
            max_depth=cfg.params.get("max_depth", 5),
            min_cluster_size=cfg.params.get("min_cluster_size", 2),
            llm_model=llm_model,
            max_summary_tokens=cfg.params.get("max_summary_tokens"),
        )
        elapsed_ms = (time.monotonic() - t0) * 1000
        session.flush()

        summary_count = raptor_result.total_summaries
        tree_depth = max((lr.layer for lr in raptor_result.layers), default=0) + 1

        return (
            summary_count,
            tree_depth,
            StageResult(
                stage="summarize",
                status="completed",
                detail={
                    "total_summaries": summary_count,
                    "total_bridge_links": raptor_result.total_bridge_links,
                    "layers": len(raptor_result.layers),
                    "tree_depth": tree_depth,
                    "elapsed_ms": round(elapsed_ms, 1),
                },
            ),
        )
    except Exception as e:
        logger.error("RAPTOR summarization failed: %s", e, exc_info=True)
        return 0, 1, StageResult(stage="summarize", status="failed", error=str(e))


async def _run_extract_facts(
    session: Session,
    root_id: int,
    stages: dict[str, StageConfig],
    *,
    llm_model: Optional[str] = None,
) -> tuple[int, StageResult]:
    """Fact extraction.  Returns (triple_count, stage)."""
    cfg = stages.get("extract_facts")
    if cfg is None or not cfg.enabled:
        return 0, StageResult(
            stage="extract_facts", status="skipped", detail={"reason": "disabled"}
        )

    try:
        t0 = time.monotonic()
        fact_result = await extract_facts(
            document_id=root_id,
            session=session,
            llm_model=llm_model,
            max_facts=cfg.params.get("max_facts"),
            confidence_threshold=cfg.params.get("confidence_threshold"),
            include_summaries=cfg.params.get("include_summaries", True),
        )
        elapsed_ms = (time.monotonic() - t0) * 1000
        session.flush()

        return fact_result.total_triples_created, StageResult(
            stage="extract_facts",
            status="completed",
            detail={
                "documents_processed": fact_result.documents_processed,
                "triples_created": fact_result.total_triples_created,
                "triples_skipped": fact_result.total_skipped,
                "entities_created": fact_result.entities_created,
                "entities_resolved": fact_result.entities_resolved,
                "predicates_created": fact_result.predicates_created,
                "elapsed_ms": round(elapsed_ms, 1),
            },
        )
    except Exception as e:
        logger.error("Fact extraction failed: %s", e, exc_info=True)
        return 0, StageResult(stage="extract_facts", status="failed", error=str(e))


# ---------------------------------------------------------------------------
# Content-type-specific parse + chunk handlers
# ---------------------------------------------------------------------------


async def _execute_conversation(
    session: Session,
    content: str,
    stages: dict[str, StageConfig],
    *,
    title: Optional[str] = None,
    parent_id: Optional[int] = None,
    llm_model: Optional[str] = None,
    messages: Optional[list[ParsedMessage]] = None,
) -> IngestResult:
    """Conversation pipeline — delegates to the existing orchestrator."""
    if messages is None:
        messages = parse_adjutant_jsonl(content)
    if not messages:
        raise ValueError("No messages to ingest")

    # Filter empty
    messages = [m for m in messages if m.content and m.content.strip()]
    if not messages:
        raise ValueError("All messages are empty")
    for i, msg in enumerate(messages):
        msg.turn_index = i

    segment_cfg = stages.get("segment", StageConfig(enabled=False))
    summarize_cfg = stages.get("summarize", StageConfig(enabled=False))
    facts_cfg = stages.get("extract_facts", StageConfig(enabled=False))

    return await ingest_conversation(
        session=session,
        messages=messages,
        title=title,
        parent_id=parent_id,
        segment=segment_cfg.enabled,
        segment_min=segment_cfg.params.get("min_segment", 3),
        segment_max=segment_cfg.params.get("max_segment", 10),
        segment_penalty=segment_cfg.params.get("penalty"),
        summarize=summarize_cfg.enabled,
        extract_triples=facts_cfg.enabled,
        raptor_max_depth=summarize_cfg.params.get("max_depth", 5),
        raptor_min_cluster_size=summarize_cfg.params.get("min_cluster_size", 2),
        llm_model=llm_model,
        max_summary_tokens=summarize_cfg.params.get("max_summary_tokens"),
        max_facts=facts_cfg.params.get("max_facts"),
        confidence_threshold=facts_cfg.params.get("confidence_threshold"),
        include_summaries=facts_cfg.params.get("include_summaries", True),
    )


async def _execute_markdown(
    session: Session,
    content: str,
    stages: dict[str, StageConfig],
    *,
    title: Optional[str] = None,
    parent_id: Optional[int] = None,
    llm_model: Optional[str] = None,
) -> IngestResult:
    """Markdown pipeline: structural split -> chunk sections -> embed -> summarize -> facts."""
    repo = DocumentRepository(session)
    stage_results: list[StageResult] = []

    # -- Parse: structural split on headings --
    clock = StageClock()
    t0 = time.monotonic()
    sections = split_on_headings(content)
    if not sections:
        raise ValueError("No content to ingest (empty markdown)")
    parse_ms = (time.monotonic() - t0) * 1000

    stage_results.append(
        clock.stamp(
            StageResult(
                stage="parse",
                status="completed",
                detail={
                    "sections": len(sections),
                    "had_headings": any(s.level > 0 for s in sections),
                    "elapsed_ms": round(parse_ms, 1),
                },
            )
        )
    )

    # -- Chunk: create root + children --
    clock = StageClock()
    t0 = time.monotonic()
    doc_title = title or f"Markdown document ({len(sections)} sections)"
    root = repo.create(
        title=doc_title,
        content=content,
        parent_id=parent_id,
        usetype="markdown",
        structured_content={"section_count": len(sections)},
        auto_embed=True,
        # Container: holds the whole source text, which is routinely over the
        # token/maxsim window. It gets a full-length document vector; maxsim runs
        # on the chunks created below. See KNOWN-DEFECTS D1.
        embed_tokens=False,
    )
    root_id = root.id

    chunk_cfg = stages.get("chunk", StageConfig())
    strategy = ChunkStrategy(chunk_cfg.params.get("strategy", "paragraph"))
    max_tokens = chunk_cfg.params.get("max_tokens", 200)
    min_chunk_length = chunk_cfg.params.get("min_chunk_length", 20)

    child_ids: list[int] = []
    for section in sections:
        section_text = section.content
        if not section_text or not section_text.strip():
            continue

        # Chunk each section
        try:
            chunks = chunk_text(
                section_text,
                strategy=strategy,
                max_tokens=max_tokens,
                min_chunk_length=min_chunk_length,
                fits=get_embedding_service().fits_token_window,
            )
        except ValueError:
            # Section too small to chunk — store as single child
            chunks = None

        if chunks and len(chunks) > 1:
            for chunk in chunks:
                section_title = section.title or f"Section (line {section.source_line})"
                child = repo.create(
                    title=f"{section_title} — chunk {chunk.index}",
                    content=chunk.text,
                    parent_id=root_id,
                    usetype="chunk",
                    structured_content={
                        "section_title": section.title,
                        "section_level": section.level,
                        "chunk_index": chunk.index,
                        "source_line": section.source_line,
                    },
                    auto_embed=True,
                )
                child_ids.append(child.id)
        else:
            # Single chunk — store the whole section
            section_title = section.title or f"Section (line {section.source_line})"
            child = repo.create(
                title=section_title,
                content=section_text,
                parent_id=root_id,
                usetype="chunk",
                structured_content={
                    "section_title": section.title,
                    "section_level": section.level,
                    "source_line": section.source_line,
                },
                auto_embed=True,
            )
            child_ids.append(child.id)

    session.flush()
    chunk_ms = (time.monotonic() - t0) * 1000

    stage_results.append(
        clock.stamp(
            StageResult(
                stage="chunk",
                status="completed",
                detail={
                    "chunks_created": len(child_ids),
                    "document_ids": child_ids,
                    "elapsed_ms": round(chunk_ms, 1),
                },
            )
        )
    )

    # -- Summarize --
    # Stamped here rather than inside ``_run_summarize``: the helper has four outcome
    # branches and the call site is the one place that knows when the stage began.
    clock = StageClock()
    summary_count, tree_depth, sum_stage = await _run_summarize(
        session, root_id, stages, llm_model=llm_model
    )
    stage_results.append(clock.stamp(sum_stage))

    # -- Extract facts --
    clock = StageClock()
    triple_count, fact_stage = await _run_extract_facts(
        session, root_id, stages, llm_model=llm_model
    )
    stage_results.append(clock.stamp(fact_stage))

    return IngestResult(
        source_document_id=root_id,
        title=doc_title,
        message_count=0,
        segment_count=len(child_ids),
        summary_count=summary_count,
        triple_count=triple_count,
        tree_depth=tree_depth,
        stages=stage_results,
    )


async def _execute_url(
    session: Session,
    content: str,
    stages: dict[str, StageConfig],
    *,
    title: Optional[str] = None,
    parent_id: Optional[int] = None,
    llm_model: Optional[str] = None,
) -> IngestResult:
    """wiki:url pipeline: ``content`` is the URL. Fetch, html→md, then markdown stages."""
    from jmfts_core.url_fetch import UrlFetchError, fetch_url, html_to_markdown

    url = content.strip()
    try:
        fetched, ctype = fetch_url(url)
    except UrlFetchError as e:
        raise ValueError(f"wiki:url fetch failed: {e}") from e

    md = html_to_markdown(fetched) if "html" in ctype else fetched
    derived_title = title or url

    repo = DocumentRepository(session)
    clock = StageClock()
    chash = compute_content_hash(md)
    if chash:
        existing = repo.find_by_hash_and_parent(chash, parent_id)
        if existing is not None:
            return _existing_result(
                existing,
                chash,
                clock=clock,
                reason="fetched content_hash matched existing document",
            )

    result = await _execute_markdown(
        session,
        md,
        stages,
        title=derived_title,
        parent_id=parent_id,
        llm_model=llm_model,
    )
    # Stamp source URL into the root document's structured_content
    root = repo.get(result.source_document_id)
    if root is not None:
        sc = dict(root.structured_content or {})
        sc.update({"source_url": url, "fetched_content_type": ctype})
        root.structured_content = sc
    return result


async def _execute_arxiv(
    session: Session,
    content: str,
    stages: dict[str, StageConfig],
    *,
    title: Optional[str] = None,
    parent_id: Optional[int] = None,
    llm_model: Optional[str] = None,
) -> IngestResult:
    """wiki:arxiv pipeline: ``content`` is the arXiv ID. Fetch metadata + PDF."""
    from jmfts_core.arxiv_fetch import ArxivFetchError, fetch_arxiv
    from jmfts_core.pdf_extraction import pdf_to_markdown

    try:
        pdf_bytes, metadata = fetch_arxiv(content)
        md, _pdf_meta = pdf_to_markdown(pdf_bytes)
    except ArxivFetchError as e:
        raise ValueError(f"wiki:arxiv fetch failed: {e}") from e
    except Exception as e:
        raise ValueError(f"wiki:arxiv PDF processing failed: {e}") from e

    derived_title = title or metadata.get("title") or f"arXiv {metadata.get('arxiv_id')}"

    repo = DocumentRepository(session)
    clock = StageClock()
    chash = compute_content_hash(md)
    if chash:
        existing = repo.find_by_hash_and_parent(chash, parent_id)
        if existing is not None:
            return _existing_result(
                existing,
                chash,
                clock=clock,
                reason="fetched content_hash matched existing document",
            )

    result = await _execute_markdown(
        session,
        md,
        stages,
        title=derived_title,
        parent_id=parent_id,
        llm_model=llm_model,
    )
    root = repo.get(result.source_document_id)
    if root is not None:
        sc = dict(root.structured_content or {})
        sc.update(metadata)
        root.structured_content = sc
    return result


async def _execute_pdf(
    session: Session,
    content: str,
    stages: dict[str, StageConfig],
    *,
    title: Optional[str] = None,
    parent_id: Optional[int] = None,
    llm_model: Optional[str] = None,
) -> IngestResult:
    """wiki:pdf pipeline: ``content`` is a local file path."""
    from jmfts_core.pdf_extraction import pdf_to_markdown

    path = content.strip()
    try:
        md, metadata = pdf_to_markdown(path)
    except FileNotFoundError as e:
        raise ValueError(f"wiki:pdf file not found: {path}") from e
    except Exception as e:
        raise ValueError(f"wiki:pdf processing failed: {e}") from e

    derived_title = title or metadata.get("title") or path.rsplit("/", 1)[-1]

    repo = DocumentRepository(session)
    clock = StageClock()
    chash = compute_content_hash(md)
    if chash:
        existing = repo.find_by_hash_and_parent(chash, parent_id)
        if existing is not None:
            return _existing_result(
                existing,
                chash,
                clock=clock,
                reason="fetched content_hash matched existing document",
            )

    result = await _execute_markdown(
        session,
        md,
        stages,
        title=derived_title,
        parent_id=parent_id,
        llm_model=llm_model,
    )
    root = repo.get(result.source_document_id)
    if root is not None:
        sc = dict(root.structured_content or {})
        sc.update({"source_path": path, **metadata})
        root.structured_content = sc
    return result


def _existing_result(existing, chash: str, *, clock: StageClock, reason: str) -> IngestResult:
    """Build the short-circuit IngestResult for a content_hash that already exists.

    Shared by the source-fetch pipelines (which hash the *converted* text after fetching)
    and by ``execute_pipeline``'s own pre-dispatch check, so both write the same attempt
    record. ``clock`` is created by the caller before it computes the hash: the hash and
    the lookup ARE the attempt, and they are what the span should cover.
    """
    return IngestResult(
        source_document_id=existing.id,
        title=existing.title or "",
        message_count=0,
        segment_count=0,
        summary_count=0,
        triple_count=0,
        tree_depth=1,
        stages=[
            clock.stamp(
                StageResult(
                    stage="idempotency",
                    status="skipped",
                    detail={
                        "reason": reason,
                        "existing_document_id": existing.id,
                        "content_hash": chash,
                    },
                )
            )
        ],
        was_existing=True,
        existing_document_id=existing.id,
    )


async def _execute_text(
    session: Session,
    content: str,
    stages: dict[str, StageConfig],
    *,
    usetype: str = "raw",
    title: Optional[str] = None,
    parent_id: Optional[int] = None,
    llm_model: Optional[str] = None,
) -> IngestResult:
    """Raw/transcript pipeline: chunk text -> embed -> summarize -> facts."""
    repo = DocumentRepository(session)
    stage_results: list[StageResult] = []

    if not content or not content.strip():
        raise ValueError("No content to ingest (empty text)")

    # -- Parse (minimal — just record input stats) --
    clock = StageClock()
    word_count = len(content.split())
    stage_results.append(
        clock.stamp(
            StageResult(
                stage="parse",
                status="completed",
                detail={"char_count": len(content), "word_count": word_count},
            )
        )
    )

    # -- Chunk: create root + children --
    clock = StageClock()
    t0 = time.monotonic()
    doc_title = title or f"{usetype.capitalize()} ({word_count} words)"
    root = repo.create(
        title=doc_title,
        content=content,
        parent_id=parent_id,
        usetype=usetype,
        structured_content={"word_count": word_count},
        auto_embed=True,
        # Container — document vector only; maxsim runs on its chunks (D1).
        embed_tokens=False,
    )
    root_id = root.id

    chunk_cfg = stages.get("chunk", StageConfig())
    strategy = ChunkStrategy(chunk_cfg.params.get("strategy", "sentence"))
    max_tokens = chunk_cfg.params.get("max_tokens", 200)
    min_chunk_length = chunk_cfg.params.get("min_chunk_length", 20)

    try:
        chunks = chunk_text(
            content,
            strategy=strategy,
            max_tokens=max_tokens,
            min_chunk_length=min_chunk_length,
            fits=get_embedding_service().fits_token_window,
        )
    except ValueError:
        chunks = []

    child_ids: list[int] = []
    for chunk in chunks:
        child = repo.create(
            title=f"{usetype} — chunk {chunk.index}",
            content=chunk.text,
            parent_id=root_id,
            usetype="chunk",
            structured_content={
                "chunk_index": chunk.index,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
            },
            auto_embed=True,
        )
        child_ids.append(child.id)

    session.flush()
    chunk_ms = (time.monotonic() - t0) * 1000

    stage_results.append(
        clock.stamp(
            StageResult(
                stage="chunk",
                status="completed",
                detail={
                    "chunks_created": len(child_ids),
                    "document_ids": child_ids,
                    "elapsed_ms": round(chunk_ms, 1),
                },
            )
        )
    )

    # -- Summarize --
    clock = StageClock()
    summary_count, tree_depth, sum_stage = await _run_summarize(
        session, root_id, stages, llm_model=llm_model
    )
    stage_results.append(clock.stamp(sum_stage))

    # -- Extract facts --
    clock = StageClock()
    triple_count, fact_stage = await _run_extract_facts(
        session, root_id, stages, llm_model=llm_model
    )
    stage_results.append(clock.stamp(fact_stage))

    return IngestResult(
        source_document_id=root_id,
        title=doc_title,
        message_count=0,
        segment_count=len(child_ids),
        summary_count=summary_count,
        triple_count=triple_count,
        tree_depth=tree_depth,
        stages=stage_results,
    )


# ---------------------------------------------------------------------------
# BM25 auto-index
# ---------------------------------------------------------------------------


def _index_subtree_bm25(
    session: Session,
    root_id: int,
    index_name: str = "default",
) -> StageResult:
    """Index a document and its children into the BM25 index.

    Creates the index if it doesn't exist.  Adds the root as an index member
    and indexes each document in the subtree individually (no full-corpus
    refresh).
    """
    clock = StageClock()
    t0 = time.monotonic()
    try:
        repo = SearchRepository(session)
        doc_repo = DocumentRepository(session)

        # Ensure the index exists
        if not repo.get_index(index_name):
            repo.create_index(name=index_name, description="Auto-created default BM25 index")

        # Register the root as an index member
        repo.add_root_to_index(index_name, root_id)

        # Index every document in the subtree.
        # include_in_flight=True: this stage runs INSIDE the ingestion that just built
        # the tree, so it is the code responsible for those nodes and must see them. The
        # settled-only default exists to stop OUTSIDE readers getting a partial tree with
        # no error; here a partial tree would mean silently indexing half of what we just
        # wrote. The walk is over one local subtree, so the sequential scan it accepts is
        # bounded.
        subtree = doc_repo.get_subtree(root_id, include_in_flight=True)
        indexed = 0
        for doc in subtree:
            if doc.content and repo.index_document(doc.id, index_name):
                indexed += 1

        session.flush()
        elapsed_ms = (time.monotonic() - t0) * 1000
        return clock.stamp(
            StageResult(
                stage="bm25_index",
                status="completed",
                detail={
                    "index": index_name,
                    "documents_indexed": indexed,
                    "subtree_size": len(subtree),
                    "elapsed_ms": round(elapsed_ms, 1),
                },
            )
        )
    except Exception as e:
        logger.error("BM25 auto-index failed for root %d: %s", root_id, e, exc_info=True)
        elapsed_ms = (time.monotonic() - t0) * 1000
        return clock.stamp(
            StageResult(
                stage="bm25_index",
                status="failed",
                error=str(e),
                detail={"elapsed_ms": round(elapsed_ms, 1)},
            )
        )


# ---------------------------------------------------------------------------
# Attempt log — INGEST_SPEC Part 3.3 / 3.4
# ---------------------------------------------------------------------------
#
# JMFTS already computed this record and threw it away: a list[StageResult] returned in
# the HTTP body and never written down. Everything below turns that list into the durable
# per-node log the scheduler (Part 5) and the re-run diff (Part 6.1) read. Nothing here
# changes how ingestion runs — it only stops it forgetting.

# Pipelines whose section boundaries come from the document's own markdown outline. All
# four route through ``_execute_markdown``, which splits on ATX headings — spec 3.5 files
# that under the `declared` rung.
_DECLARED_OUTLINE_PIPELINES = frozenset({"markdown", "wiki:url", "wiki:arxiv", "wiki:pdf"})

# Stages whose output depends on which model answered, so the model belongs in the
# fingerprint: re-running a summary against a different LLM is a different attempt.
_LLM_STAGES = frozenset({"summarize", "extract_facts"})

# Where each stage records the ids of the nodes it created. ``produced.child_ids`` is the
# undo record for spec 6.2 — a stage missing from this map creates no nodes, and gets a
# null ``produced`` rather than an empty one.
_PRODUCED_ID_KEYS = {"chunk": "document_ids", "segment": "segment_document_ids"}

# Spec 3.5's rungs, best to worst.
_RUNG_ORDER = ("declared", "inferred", "semantic", "flat")


def _stage_structure(usetype: str, stage: StageResult) -> Optional[tuple[str, str]]:
    """``(rung, source)`` for a stage that decides node boundaries; ``None`` for the rest.

    Spec 3.4 wants the rung recorded so hierarchical summarization can tell a declared
    section boundary from a guessed one. Only boundary-deciding stages get one:
    ``summarize``, ``extract_facts`` and ``bm25_index`` produce no structure, and a
    plausible-looking rung on them would be an invented fact.
    """
    if stage.stage == "parse":
        # split_on_headings reads the document's own ATX outline — but only when the
        # document HAS one. A heading-less file comes back as a single level-0 section,
        # and calling that `declared` would claim evidence the file never carried.
        if usetype in _DECLARED_OUTLINE_PIPELINES and stage.detail.get("had_headings") is True:
            return ("declared", "markdown_headings")
        return None
    if stage.stage == "chunk":
        if usetype == "conversation":
            # Turn boundaries are declared by the source transcript, not guessed by a
            # chunker: one child per message, exactly where the record says.
            return ("declared", "message_boundaries")
        return ("flat", "chunking")
    if stage.stage == "segment":
        return ("semantic", "pelt")  # PELT over ruptures — spec 3.5's `semantic` rung
    return None


def _attempt_params(stage_name: str, cfg: Optional[StageConfig], llm_model: Optional[str]) -> dict:
    """The parameters that affect this stage's output, for the 6.1 diff key.

    Known gap, recorded rather than papered over: JMFTS writes each stage default twice —
    once in the registry literal and once as the ``params.get(key, default)`` fallback at
    the point of use. Only the registry half is visible here, so a parameter that exists
    solely as an inline fallback (``include_summaries``, ``max_facts``,
    ``confidence_threshold``, ``max_summary_tokens``) is absent from the fingerprint until
    those defaults are unified into the registry. Two runs that differ only in such a
    parameter therefore fingerprint identically. Fixing it means moving the defaults, not
    guessing them here — inventing values would make the fingerprint disagree with the
    registry it is supposed to summarise.
    """
    if cfg is None:
        # bm25_index and idempotency are not registry stages: they run unconditionally
        # with no configurable input, so there is genuinely nothing to fingerprint.
        return {}
    params: dict = {"enabled": cfg.enabled, **cfg.params}
    if stage_name in _LLM_STAGES:
        # None means "not overridden, use the configured default" — a real distinction
        # from a caller who pinned that same model by name.
        params["llm_model"] = llm_model
    return params


def _structure_summary(usetype: str, result: IngestResult) -> Optional[dict]:
    """The spec 3.3 ``structure`` block, restricted to what JMFTS actually knows today.

    ``coverage`` and ``gap_regions`` need the per-region accounting that arrives with the
    rung ladder (phasing step 5), and ``max_depth`` needs a structural depth that is not
    RAPTOR's layer count. Those keys are OMITTED rather than written as zero: a
    ``coverage: 0`` reads as "the structuring stage claimed nothing", which is a different
    and false fact. Returns ``None`` when nothing structural completed at all.
    """
    primary: Optional[tuple[str, str]] = None
    node_count = 0
    for stage in result.stages:
        if stage.status != "completed":
            continue
        structure = _stage_structure(usetype, stage)
        if structure is None:
            continue
        if primary is None or _RUNG_ORDER.index(structure[0]) < _RUNG_ORDER.index(primary[0]):
            primary = structure
        id_key = _PRODUCED_ID_KEYS.get(stage.stage)
        if id_key:
            node_count += len(stage.detail.get(id_key) or [])

    if primary is None:
        return None
    block = {"primary_rung": primary[0], "source": primary[1]}
    if node_count:
        block["node_count"] = node_count
    return block


def _record_attempts(
    session: Session,
    result: IngestResult,
    stages: dict[str, StageConfig],
    usetype: str,
    llm_model: Optional[str],
) -> None:
    """Append this run's attempt records to the node's ``structured_content``.

    Append-only, per spec 3.4: a re-ingest adds to the log, it never rewrites it, because
    the log is what makes "this file was ingested before a vision model existed" a
    recoverable fact. ``attempt`` continues the per-(node, task) count already on the row,
    so the second ``chunk`` on a node is attempt 2 and says so.
    """
    repo = DocumentRepository(session)
    node = repo.get(result.source_document_id)
    if node is None:
        # The row is gone — a rolled-back transaction, or a caller-supplied stub. The
        # stages are still on the returned IngestResult, so nothing is being swallowed
        # silently; log it loudly and let the ingest itself stand.
        logger.warning("Attempt log not written: document %d not found", result.source_document_id)
        return

    counts = repo.attempt_counts(node)

    records: list[AttemptRecord] = []
    for stage in result.stages:
        counts[stage.stage] = counts.get(stage.stage, 0) + 1
        structure = _stage_structure(usetype, stage)
        params = _attempt_params(stage.stage, stages.get(stage.stage), llm_model)

        produced = None
        id_key = _PRODUCED_ID_KEYS.get(stage.stage)
        if id_key and stage.status == "completed":
            child_ids = list(stage.detail.get(id_key) or [])
            produced = {"node_count": len(child_ids), "child_ids": child_ids}

        records.append(
            AttemptRecord(
                task=stage.stage,
                status=stage.status,
                attempt=counts[stage.stage],
                rung=structure[0] if structure else None,
                scope_document_id=result.source_document_id,
                params=params,
                param_fingerprint=param_fingerprint(params),
                started_at=stage.started_at,
                finished_at=stage.finished_at,
                detail=stage.detail,
                produced=produced,
                error=stage.error,
                # task_id / write_mode / superseded_by / error_type stay null: the queue
                # (Part 5), the declared write mode (5.3), superseding (6.2) and the
                # error classifier do not exist yet, and a null that means "not built"
                # is honest where a default would not be.
            )
        )

    # The `structure` block first, then the log. Two separate reassignments, each copying
    # the dict as it stands — the JSONB change-tracking rule that makes that necessary is
    # documented once, on DocumentRepository.append_attempts.
    structure_block = _structure_summary(usetype, result)
    if structure_block is not None:
        sc = dict(node.structured_content or {})
        sc["structure"] = structure_block
        node.structured_content = sc
    repo.append_attempts(node, records)
    session.flush()


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------


async def execute_pipeline(
    session: Session,
    content: str,
    usetype: str,
    *,
    title: Optional[str] = None,
    parent_id: Optional[int] = None,
    pipeline_config: Optional[dict[str, Any]] = None,
    llm_model: Optional[str] = None,
    messages: Optional[list[ParsedMessage]] = None,
) -> IngestResult:
    """Execute a registered pipeline on content.

    Args:
        session: Database session.
        content: Raw text content to ingest.
        usetype: Pipeline name (must be registered).
        title: Optional title for the root document.
        parent_id: Optional parent document ID.
        pipeline_config: Stage-level overrides — keys are stage names,
            values are dicts with ``enabled`` and/or param keys.
        llm_model: Override LLM model for summarization / extraction.
        messages: Pre-parsed messages (conversation pipeline only).

    Returns:
        IngestResult with per-stage details.

    Raises:
        ValueError: If the usetype is not registered.
    """
    pipeline = get_pipeline(usetype)
    if pipeline is None:
        available = sorted(_registry.keys())
        raise ValueError(f"Unknown pipeline usetype: {usetype!r}. Available: {available}")

    repo = DocumentRepository(session)

    # Validate parent_id before delegating to content-specific handlers.
    # If the parent document no longer exists (deleted, rolled-back transaction),
    # degrade to root-level creation instead of crashing the whole pipeline.
    if parent_id is not None:
        if repo.get(parent_id) is None:
            logger.warning(
                "Parent document %d does not exist; ingesting as root document",
                parent_id,
            )
            parent_id = None

    # Idempotency short-circuit: if the same content already exists under the
    # same parent, skip re-ingestion and return the existing document. Skipped
    # for pipelines whose ``content`` is a source identifier (URL/path/id),
    # not the actual document content — they hash inside their own handler
    # after fetching.
    _SOURCE_FETCH_PIPELINES = {"wiki:url", "wiki:arxiv", "wiki:pdf"}
    result: Optional[IngestResult] = None
    # Empty on the short-circuit path: no stage was resolved, so no stage had
    # parameters. An empty dict here is the truth, not a placeholder.
    stages: dict[str, StageConfig] = {}

    if usetype != "conversation" and usetype not in _SOURCE_FETCH_PIPELINES:
        # Only dedup on genuine content. Empty/whitespace input must fall through
        # to the handler so it can raise its own specific empty-content error
        # ("empty text" / "empty markdown"), rather than being swallowed by the
        # idempotency short-circuit hashing whitespace and matching a stray row.
        clock = StageClock()
        chash = compute_content_hash(content) if content and content.strip() else None
        if chash:
            existing = repo.find_by_hash_and_parent(chash, parent_id)
            if existing is not None:
                logger.info(
                    "Idempotent re-ingest: content_hash=%s matched existing doc #%d",
                    chash[:12],
                    existing.id,
                )
                # Assigned rather than returned: this path must still reach the attempt
                # log at the tail. A re-ingest that matched is exactly the event spec 6.1
                # needs written down on the existing node — "we were asked again, and
                # here is why nothing ran" — and an early return would drop it.
                result = _existing_result(
                    existing,
                    chash,
                    clock=clock,
                    reason="content_hash matched existing document",
                )

    if result is None:
        stages = _resolve_stages(pipeline, pipeline_config)
        if usetype == "conversation":
            result = await _execute_conversation(
                session,
                content,
                stages,
                title=title,
                parent_id=parent_id,
                llm_model=llm_model,
                messages=messages,
            )
        elif usetype == "markdown":
            result = await _execute_markdown(
                session,
                content,
                stages,
                title=title,
                parent_id=parent_id,
                llm_model=llm_model,
            )
        elif usetype == "wiki:url":
            result = await _execute_url(
                session,
                content,
                stages,
                title=title,
                parent_id=parent_id,
                llm_model=llm_model,
            )
        elif usetype == "wiki:arxiv":
            result = await _execute_arxiv(
                session,
                content,
                stages,
                title=title,
                parent_id=parent_id,
                llm_model=llm_model,
            )
        elif usetype == "wiki:pdf":
            result = await _execute_pdf(
                session,
                content,
                stages,
                title=title,
                parent_id=parent_id,
                llm_model=llm_model,
            )
        else:
            # raw, transcript, or any future text-based pipeline
            result = await _execute_text(
                session,
                content,
                stages,
                usetype=usetype,
                title=title,
                parent_id=parent_id,
                llm_model=llm_model,
            )

        # -- Auto-index into the default BM25 index --
        # Skip when we short-circuited: the existing document is already indexed.
        if not result.was_existing:
            bm25_stage = _index_subtree_bm25(session, result.source_document_id)
            result.stages.append(bm25_stage)

    # -- Persist the attempt log (spec 3.3/3.4) --
    # The single point every path converges on, short-circuit included, and the last
    # thing to happen before the result leaves the pipeline. It only flushes; the
    # caller's commit() carries it, so the log lands in the same transaction as the tree
    # it describes and rolls back with it.
    _record_attempts(session, result, stages, usetype, llm_model)

    return result
