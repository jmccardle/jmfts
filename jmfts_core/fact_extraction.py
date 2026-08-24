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

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from jmfts_core.config import Settings, get_settings
from jmfts_core.entity_roots import (
    ENTITY_USETYPE,
    entity_root_ids,
    get_or_create_entities_root,
)
from jmfts_core.graph_analysis import RBAC_COREF_LINK_TYPE
from jmfts_core.llm_client import complete
from jmfts_core.models.document import Document, DocumentLink
from jmfts_core.models.triple import FactType
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository

logger = logging.getLogger(__name__)

#: Link type recording that a document mentions an entity. This edge is the SCOPING
#: MECHANISM: until ``SPRINT_0_3_0.md`` 7.5 the entity was a CHILD of the document that
#: mentioned it, so "which entities came out of this subtree" was a path query — and an
#: entity mentioned by two documents could only be a child of one of them. As a link it is
#: many-to-many and records every mention, including the ones that resolved to a node that
#: already existed, which the parent relationship could not represent at all.
MENTIONS_LINK_TYPE = "mentions"

#: How many entity nodes a title lookup will consider. Two separate bounds because they
#: answer differently sized questions: the same-root lookup wants the best match among this
#: access's entities, the cross-root scan wants at most one match per OTHER root, and there
#: are as many roots as there are distinct accesses in the corpus.
_SAME_ROOT_CANDIDATE_LIMIT = 50
_CROSS_ROOT_CANDIDATE_LIMIT = 200


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


# `ResolvedTriple` used to sit here — a dataclass for "a triple after entity/predicate
# resolution, ready for DB insertion". Nothing ever constructed one: the pipeline calls
# `TripleRepository.upsert_triple()` with those same fields as arguments, so the dataclass
# described a row shape the repository already owns.


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
    base_url, model = settings.require_llm("Fact extraction", llm_model)

    # Truncate to fit context budget (rough: 4 chars/token, leave room for prompt)
    char_budget = 60_000 * 4  # ~60K tokens for input
    if len(text) > char_budget:
        text = text[:char_budget]

    result = await complete(
        settings=settings,
        base_url=base_url,
        model=model,
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": f"Extract factual triples from this text:\n\n{text}"},
        ],
        max_tokens=settings.extraction_max_tokens,
        temperature=settings.extraction_temperature,
    )

    content = result.text

    # Strip markdown fences if the LLM wraps the JSON
    if content.startswith("```"):
        lines = content.split("\n")
        # Remove first and last fence lines
        lines = [line for line in lines if not line.strip().startswith("```")]
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


def _like_escape(value: str) -> str:
    """Escape LIKE metacharacters so an entity named ``50%`` is not a wildcard."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _entity_candidates(
    session: Session, name: str, parent_ids: list[int], limit: int
) -> list[Document]:
    """Entity nodes directly under ``parent_ids`` whose title could plausibly be ``name``.

    Two patterns, one query: a prefix match (the cheap, index-friendly one) OR a contains
    match (which catches "Paris" inside "Paris, France"). Both are only a candidate SIEVE —
    :func:`_string_similarity` decides. Entities are created as direct children of their
    entities root, so ``parent_id`` membership is the exact scope and needs no path query.

    NO ACCESS FILTER, deliberately, and this is the load-bearing part of
    ``SPRINT_0_3_0.md`` 7.5. Scoping is done by WHICH ROOTS are passed in, not by who is
    asking: the same-root lookup is already confined to one access, and the cross-root scan
    must see every copy regardless of the current principal, because fact extraction runs
    in the worker (which binds no principal, ``ingest_worker.py:58``) and a copy created
    last must still find the copies created first.
    """
    if not parent_ids:
        return []
    prefix = name[:3] if len(name) >= 3 else name
    stmt = (
        select(Document)
        .where(
            Document.usetype == ENTITY_USETYPE,
            Document.parent_id.in_(parent_ids),
            or_(
                Document.title.ilike(f"{_like_escape(prefix)}%", escape="\\"),
                Document.title.ilike(f"%{_like_escape(name)}%", escape="\\"),
            ),
        )
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())


def _best_match(name: str, candidates: list[Document], threshold: float) -> Optional[Document]:
    """The candidate most similar to ``name``, or None if none clears ``threshold``."""
    best: Optional[Document] = None
    best_score = 0.0
    for doc in candidates:
        if not doc.title:
            continue
        score = _string_similarity(name, doc.title)
        if score > best_score:
            best_score = score
            best = doc
    return best if best is not None and best_score >= threshold else None


def _link_rbac_coref(
    session: Session, entity_doc: Document, name: str, threshold: float, root_id: int
) -> int:
    """Join a freshly created entity node to the copies of it under every OTHER root.

    One real-world thing gets one entity node per distinct access it is mentioned in, and
    those copies are joined by ``rbac_coref``: same referent, different viewers. At most
    one edge per other root — a root holds one node per referent, so a second match under
    the same root would be a duplicate rather than another copy.

    **Creation order does not matter, and that is load-bearing.** Copy *k* links to copies
    1..*k*−1 when it is created, and the walk runs ``direction="both"``, so every pair gets
    exactly one edge when its later member appears and a public copy created last is still
    reachable from every restricted one.

    Returns the number of edges written.
    """
    others = [rid for rid in entity_root_ids(session) if rid != root_id]
    candidates = _entity_candidates(session, name, others, _CROSS_ROOT_CANDIDATE_LIMIT)
    by_root: dict[int, list[Document]] = {}
    for doc in candidates:
        by_root.setdefault(doc.parent_id, []).append(doc)

    written = 0
    for twins in by_root.values():
        twin = _best_match(name, twins, threshold)
        if twin is None:
            continue
        _upsert_link(session, entity_doc.id, twin.id, RBAC_COREF_LINK_TYPE)
        written += 1
    if written:
        logger.debug(
            "Entity '%s' (doc %d) joined to %d copy/copies under other entities roots",
            name,
            entity_doc.id,
            written,
        )
    return written


def _upsert_link(session: Session, source_id: int, target_id: int, link_type: str) -> None:
    """Assert a ``DocumentLink``, idempotently.

    ``document_links`` is UNIQUE on (source, target, type), so a plain ``create_link``
    raises the second time the same mention is extracted — which, inside an extraction
    batch, takes the whole batch with it. Same reasoning as
    ``TripleRepository.get_or_create_predicate``.
    """
    session.execute(
        pg_insert(DocumentLink.__table__)
        .values(source_id=source_id, target_id=target_id, link_type=link_type)
        .on_conflict_do_nothing()
    )


def resolve_entity(
    name: str,
    session: Session,
    threshold: float,
    _cache: dict[tuple[int, str], int] | None = None,
    *,
    source_document_id: int,
) -> tuple[int, bool]:
    """Resolve an entity name to the entity node for the ACCESS of its source document.

    Lookup is keyed by access, not by tree position (``SPRINT_0_3_0.md`` 7.5). The name is
    matched only against entity nodes under the entities root whose grants are the
    effective access of ``source_document_id``; copies under other roots are found in a
    separate pass and joined with ``rbac_coref`` rather than resolved to.

    **That split is the fix for 13.9.** Resolution used to run over every entity node in
    the store with no access filter, so a restricted document's two entities resolved to
    existing PUBLIC nodes, the triple was written between them, and ``query_triples`` —
    which scopes a fact by the readability of its endpoints — handed that fact to everyone.
    Nothing was created, so the placement rule that was the whole of the protection never
    ran. A restricted document's resolution now cannot land on a public node because it
    does not look there.

    The mention is recorded as a ``mentions`` link on every call, including the ones that
    resolved to an existing node.

    Args:
        name: Entity name to resolve.
        session: DB session.
        threshold: Minimum string similarity to match an existing entity.
        _cache: Optional ``(entities_root_id, normalized_name) -> id`` cache for this run.
            The root is IN the key: one extraction run walks a whole subtree, a deeper ACR
            can give part of that subtree a different access, and a name-only cache would
            hand a restricted document the copy it resolved for a public one — which is
            13.9 again, one run wide.
        source_document_id: The document this mention was extracted from. Required: an
            entity has to be keyed by the access of the document that mentioned it, and
            there is no key without one.

    Returns:
        (document_id, created) — True if a new entity document was created.
    """
    root_id = get_or_create_entities_root(session, source_document_id)
    key = (root_id, name.strip().lower())

    if _cache is not None and key in _cache:
        entity_id = _cache[key]
        _upsert_link(session, source_document_id, entity_id, MENTIONS_LINK_TYPE)
        return entity_id, False

    repo = DocumentRepository(session)

    match = _best_match(
        name,
        _entity_candidates(session, name, [root_id], _SAME_ROOT_CANDIDATE_LIMIT),
        threshold,
    )
    if match is not None:
        logger.debug("Entity '%s' resolved to doc %d ('%s')", name, match.id, match.title)
        if _cache is not None:
            _cache[key] = match.id
        _upsert_link(session, source_document_id, match.id, MENTIONS_LINK_TYPE)
        return match.id, False

    entity_doc = repo.create(
        title=name,
        content=name,
        usetype=ENTITY_USETYPE,
        auto_embed=True,
        parent_id=root_id,
    )
    logger.info("Created entity document '%s' -> doc %d (root %d)", name, entity_doc.id, root_id)
    _link_rbac_coref(session, entity_doc, name, threshold, root_id)
    _upsert_link(session, source_document_id, entity_doc.id, MENTIONS_LINK_TYPE)
    if _cache is not None:
        _cache[key] = entity_doc.id
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
    entity_cache: dict[tuple[int, str], int] | None = None,
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
        entity_cache: Shared (entities root, name) -> id cache across documents.
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

            # Resolve entities against the entities root for THIS document's access, and
            # record each mention as a link (SPRINT_0_3_0.md 7.5).
            subject_id, _ = resolve_entity(
                raw.subject,
                session,
                entity_threshold,
                entity_cache,
                source_document_id=document_id,
            )
            object_id, _ = resolve_entity(
                raw.object,
                session,
                entity_threshold,
                entity_cache,
                source_document_id=document_id,
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
    entity_cache: dict[tuple[int, str], int] = {}
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
