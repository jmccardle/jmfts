"""Fact Extraction Pipeline (#58).

LLM-powered extraction of structured knowledge triples from document segments.
Single-pass extraction: one LLM call per segment -> structured JSON output.

Pipeline: segment text -> LLM extraction -> entity resolution -> predicate registry -> triple creation.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from jmfts_core.config import Settings, get_settings
from jmfts_core.llm_utils import extract_llm_text
from jmfts_core.models.document import Document
from jmfts_core.models.triple import FactType
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes for extraction results
# ---------------------------------------------------------------------------


@dataclass
class RawTriple:
    """A triple as returned by the LLM before entity resolution."""

    subject: str
    predicate: str
    object: str
    confidence: float = 1.0
    fact_type: str = "atemporal"
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None


@dataclass
class ExtractionResult:
    """Result of extracting facts from a single document."""

    source_document_id: int
    raw_triples: list[RawTriple] = field(default_factory=list)
    created_triple_ids: list[int] = field(default_factory=list)
    skipped_count: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class PipelineResult:
    """Result of running the full extraction pipeline on a document tree."""

    root_document_id: int
    documents_processed: int = 0
    total_triples_created: int = 0
    total_skipped: int = 0
    entities_created: int = 0
    entities_resolved: int = 0
    predicates_created: int = 0
    predicates_reused: int = 0
    extractions: list[ExtractionResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM extraction prompt
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = """\
You are a precise knowledge extraction engine. First, identify the key entities and \
relationships in the text as a bulleted outline. Then, convert those observations into \
the required JSON format. Output only the JSON array — no explanation or outline in the \
final output.

Rules:
- Extract 1-5 of the most important factual statements as triples.
- Each triple has: subject (entity name), predicate (relationship verb/phrase), \
object (entity name or value).
- Normalize predicate names to lowercase snake_case (e.g., "is_capital_of", "authored", "works_at").
- Classify each fact:
  - "atemporal" — universally true (e.g., "Paris is the capital of France")
  - "static" — true for a long period (e.g., "John works at Acme Corp")
  - "dynamic" — frequently changing (e.g., "The stock price is $150")
- If temporal context is present, include valid_from and/or valid_until as ISO 8601 dates.
- Assign a confidence score between 0.0 and 1.0 for each triple.
- Return ONLY a JSON array. No markdown, no explanation.

Output format:
[
  {
    "subject": "Entity Name",
    "predicate": "predicate_name",
    "object": "Entity or Value",
    "confidence": 0.95,
    "fact_type": "static",
    "valid_from": "2026-03-01T00:00:00Z",
    "valid_until": null
  }
]

If no factual triples can be extracted, return an empty array: []"""


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


async def _llm_extract(text: str, settings: Settings, llm_model: str | None = None) -> list[dict]:
    """Call the LLM to extract triples from a text passage.

    Returns parsed JSON list of triple dicts, or empty list on failure.
    """
    model = llm_model or settings.effective_llm_model
    base_url = settings.effective_llm_url.rstrip("/")

    # Truncate to fit context budget (rough: 4 chars/token, leave room for prompt)
    char_budget = 60_000 * 4  # ~60K tokens for input
    if len(text) > char_budget:
        text = text[:char_budget]

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": f"Extract factual triples from this text:\n\n{text}"},
        ],
        "temperature": settings.extraction_temperature,
        "max_tokens": settings.extraction_max_tokens,
    }

    async with httpx.AsyncClient(timeout=settings.effective_llm_timeout) as client:
        resp = await client.post(f"{base_url}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()

    content = extract_llm_text(data["choices"][0])

    # Strip markdown fences if the LLM wraps the JSON
    if content.startswith("```"):
        lines = content.split("\n")
        # Remove first and last fence lines
        lines = [l for l in lines if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()

    try:
        result = json.loads(content)
        if not isinstance(result, list):
            logger.warning("LLM returned non-list JSON: %s", type(result))
            return []
        return result
    except json.JSONDecodeError as e:
        logger.error("Failed to parse LLM extraction output: %s\nContent: %s", e, content[:500])
        return []


def _parse_raw_triples(raw_dicts: list[dict], max_facts: int) -> list[RawTriple]:
    """Parse LLM output dicts into RawTriple objects, with validation."""
    triples = []
    for d in raw_dicts[:max_facts]:
        if not isinstance(d, dict):
            continue
        subject = str(d.get("subject", "")).strip()
        predicate = str(d.get("predicate", "")).strip()
        obj = str(d.get("object", "")).strip()

        if not subject or not predicate or not obj:
            continue

        triples.append(
            RawTriple(
                subject=subject,
                predicate=predicate,
                object=obj,
                confidence=float(d.get("confidence", 1.0)),
                fact_type=d.get("fact_type", "atemporal"),
                valid_from=d.get("valid_from"),
                valid_until=d.get("valid_until"),
            )
        )
    return triples


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------


def _string_similarity(a: str, b: str) -> float:
    """Compute string similarity using SequenceMatcher (0.0 - 1.0)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def resolve_entity(
    name: str,
    session: Session,
    threshold: float,
    _cache: dict[str, int] | None = None,
    parent_id: int | None = None,
) -> tuple[int, bool]:
    """Resolve an entity name to an existing Document or create a new entity document.

    Args:
        name: Entity name to resolve.
        session: DB session.
        threshold: Minimum string similarity to match an existing entity.
        _cache: Optional name->id cache for this extraction run.
        parent_id: Source document ID to attach newly created entity nodes to.
            This keeps entity nodes structurally part of the document tree so
            that path-based scope filters include/exclude them correctly.

    Returns:
        (document_id, created) — True if a new entity document was created.
    """
    # Check in-run cache first
    if _cache is not None:
        normalized = name.strip().lower()
        if normalized in _cache:
            return _cache[normalized], False

    repo = DocumentRepository(session)

    # Search existing entity documents by title similarity
    # First try exact match (case-insensitive)
    candidates = repo.find(usetype="entity", title_prefix=name[:3] if len(name) >= 3 else name)

    # Also search without usetype filter for broader matching
    if not candidates:
        stmt = select(Document).where(Document.title.ilike(f"%{name}%")).limit(50)
        candidates = list(session.execute(stmt).scalars().all())

    best_match = None
    best_score = 0.0
    for doc in candidates:
        if not doc.title:
            continue
        score = _string_similarity(name, doc.title)
        if score > best_score:
            best_score = score
            best_match = doc

    if best_match and best_score >= threshold:
        logger.debug(
            "Entity '%s' resolved to doc %d ('%s', score=%.2f)",
            name,
            best_match.id,
            best_match.title,
            best_score,
        )
        if _cache is not None:
            _cache[name.strip().lower()] = best_match.id
        return best_match.id, False

    # Create new entity document, attached to the source document so that
    # path-based scope filters handle it correctly (Bug 1b fix — Option A).
    entity_doc = repo.create(
        title=name,
        content=name,
        usetype="entity",
        auto_embed=True,
        parent_id=parent_id,
    )
    logger.info("Created entity document '%s' -> doc %d", name, entity_doc.id)
    if _cache is not None:
        _cache[name.strip().lower()] = entity_doc.id
    return entity_doc.id, True


# ---------------------------------------------------------------------------
# Predicate resolution
# ---------------------------------------------------------------------------


def resolve_predicate(
    name: str,
    session: Session,
    _cache: dict[str, int] | None = None,
) -> tuple[int, bool]:
    """Resolve a predicate name to an existing Predicate or create a new one.

    Returns:
        (predicate_id, created) — True if a new predicate was created.
    """
    normalized = name.strip().lower().replace(" ", "_")

    if _cache is not None and normalized in _cache:
        return _cache[normalized], False

    repo = TripleRepository(session)
    # Atomic get-or-create: concurrent extraction batches minting the same
    # predicate name must not abort on predicates_name_key (see
    # TripleRepository.get_or_create_predicate).
    pred, created = repo.get_or_create_predicate(name=normalized)
    if created:
        logger.info("Created predicate '%s' -> id %d", normalized, pred.id)
    if _cache is not None:
        _cache[normalized] = pred.id
    return pred.id, created


# ---------------------------------------------------------------------------
# Temporal parsing
# ---------------------------------------------------------------------------


def _parse_temporal(value: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string, returning None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        logger.warning("Could not parse temporal value: %s", value)
        return None


def _parse_fact_type(value: str) -> FactType:
    """Parse a fact_type string into the FactType enum."""
    try:
        return FactType(value.lower())
    except ValueError:
        return FactType.atemporal


# ---------------------------------------------------------------------------
# Single-document extraction
# ---------------------------------------------------------------------------


async def extract_facts_from_document(
    document_id: int,
    session: Session,
    settings: Settings | None = None,
    llm_model: str | None = None,
    max_facts: int | None = None,
    confidence_threshold: float | None = None,
    entity_cache: dict[str, int] | None = None,
    predicate_cache: dict[str, int] | None = None,
) -> ExtractionResult:
    """Extract knowledge triples from a single document via LLM.

    Args:
        document_id: Document to extract from.
        session: SQLAlchemy session.
        settings: Override settings (uses global if None).
        llm_model: Override LLM model name.
        max_facts: Max triples per document.
        confidence_threshold: Minimum confidence to keep a triple.
        entity_cache: Shared name->id cache across documents.
        predicate_cache: Shared name->id cache across documents.

    Returns:
        ExtractionResult with created triple IDs and any errors.
    """
    settings = settings or get_settings()
    max_facts = max_facts if max_facts is not None else settings.extraction_max_facts
    confidence_threshold = (
        confidence_threshold
        if confidence_threshold is not None
        else settings.extraction_confidence_threshold
    )
    entity_threshold = settings.extraction_entity_similarity_threshold

    result = ExtractionResult(source_document_id=document_id)

    doc = session.get(Document, document_id)
    if not doc or not doc.content:
        result.errors.append(f"Document {document_id} not found or has no content")
        return result

    # Call LLM
    raw_dicts = await _llm_extract(doc.content, settings, llm_model)
    raw_triples = _parse_raw_triples(raw_dicts, max_facts)
    result.raw_triples = raw_triples

    if not raw_triples:
        return result

    triple_repo = TripleRepository(session)

    for raw in raw_triples:
        # Filter by confidence
        if raw.confidence < confidence_threshold:
            result.skipped_count += 1
            continue

        try:
            # Flush pending ORM state so the DB path trigger sees a consistent
            # parent path when creating entity child documents (Bug 1b / Bug 2 fix).
            session.flush()

            # Resolve entities, attaching new entity nodes to this document so
            # path-based scope filters handle them correctly.
            subject_id, _ = resolve_entity(
                raw.subject, session, entity_threshold, entity_cache, parent_id=document_id
            )
            object_id, _ = resolve_entity(
                raw.object, session, entity_threshold, entity_cache, parent_id=document_id
            )

            # Resolve predicate
            predicate_id, _ = resolve_predicate(raw.predicate, session, predicate_cache)

            # Parse temporal metadata
            valid_from = _parse_temporal(raw.valid_from)
            valid_until = _parse_temporal(raw.valid_until)
            fact_type = _parse_fact_type(raw.fact_type)

            triple, created = triple_repo.upsert_triple(
                subject_id=subject_id,
                predicate_id=predicate_id,
                object_id=object_id,
                source_document_id=document_id,
                valid_from=valid_from,
                valid_until=valid_until,
                fact_type=fact_type,
            )
            if created:
                result.created_triple_ids.append(triple.id)
            else:
                logger.debug(
                    "Duplicate triple skipped: (%s, %s, %s)",
                    raw.subject,
                    raw.predicate,
                    raw.object,
                )
                result.skipped_count += 1

        except Exception as e:
            msg = f"Error processing triple ({raw.subject}, {raw.predicate}, {raw.object}): {e}"
            logger.error(msg)
            result.errors.append(msg)

    return result


# ---------------------------------------------------------------------------
# Pipeline: extract from a document and its children
# ---------------------------------------------------------------------------


async def extract_facts(
    document_id: int,
    session: Session,
    llm_model: str | None = None,
    max_facts: int | None = None,
    confidence_threshold: float | None = None,
    include_summaries: bool = True,
) -> PipelineResult:
    """Run fact extraction on a document and its children/segments.

    Processes all child documents (segments, chunks) and optionally RAPTOR
    summary documents. Entity and predicate resolution is shared across
    the entire run for cross-segment consistency.

    Args:
        document_id: Root document whose children to process.
        session: SQLAlchemy session.
        llm_model: Override LLM model.
        max_facts: Max triples per segment.
        confidence_threshold: Minimum confidence to keep.
        include_summaries: Whether to also extract from summary documents.

    Returns:
        PipelineResult with aggregate stats.
    """
    settings = get_settings()
    result = PipelineResult(root_document_id=document_id)

    repo = DocumentRepository(session)
    root = repo.get(document_id)
    if not root:
        result.errors.append(f"Document {document_id} not found")
        return result

    # Collect documents to process
    children = repo.get_children(document_id, depth=-1, limit=10000)

    # Filter: process segments, chunks, and optionally summaries
    target_usetypes = {"chunk", "segment", "section", None}
    if include_summaries:
        target_usetypes.add("summary")

    docs_to_process = []
    for child in children:
        if child.usetype in target_usetypes and child.content and len(child.content) > 20:
            docs_to_process.append(child)

    # If the root itself has content and no children, extract from it directly
    if not docs_to_process and root.content and len(root.content) > 20:
        docs_to_process = [root]

    if not docs_to_process:
        result.errors.append("No documents with extractable content found")
        return result

    # Shared caches for cross-document entity/predicate resolution
    entity_cache: dict[str, int] = {}
    predicate_cache: dict[str, int] = {}

    for doc in docs_to_process:
        extraction = await extract_facts_from_document(
            document_id=doc.id,
            session=session,
            settings=settings,
            llm_model=llm_model,
            max_facts=max_facts,
            confidence_threshold=confidence_threshold,
            entity_cache=entity_cache,
            predicate_cache=predicate_cache,
        )
        result.extractions.append(extraction)
        result.documents_processed += 1
        result.total_triples_created += len(extraction.created_triple_ids)
        result.total_skipped += extraction.skipped_count
        result.errors.extend(extraction.errors)

    # Count entity/predicate stats from caches
    # Query how many entity docs were created in this session
    entity_docs = repo.find(usetype="entity", limit=10000)
    result.entities_created = sum(
        1 for eid in entity_cache.values() if any(e.id == eid for e in entity_docs)
    )
    result.entities_resolved = len(entity_cache) - result.entities_created

    # Predicate stats
    for pred_name, pred_id in predicate_cache.items():
        # We can't easily tell created vs reused from cache alone,
        # so count predicates that exist
        pass
    result.predicates_created = len(predicate_cache)  # conservative estimate

    session.flush()
    return result
