"""API Request/Response Schemas"""

from typing import Optional
from pydantic import BaseModel

# Transport-neutral contracts now live in jmfts_core.contracts (so the service
# layer can use them without importing FastAPI). Re-exported here so existing
# `from api.schemas import DocumentResponse` / search models keep working.
# See jmfts_core/contracts/__init__.py and tests/test_api_parity.py.
from jmfts_core.contracts import (  # noqa: F401
    AutoSearchRequest,
    AutoSearchResponse,
    BackReferenceItem,
    BackReferenceResponse,
    BreadcrumbResponse,
    CentralityResponse,
    CentralityScoreItem,
    ChunkItem,
    ChunkRequest,
    ChunkResponse,
    CommunityItem,
    CommunityMember,
    CommunityResponse,
    ConversationIngestRequest,
    ConversationIngestResponse,
    ConversationMessage,
    ConversationStageResult,
    DocumentCreate,
    DocumentResponse,
    DocumentTokensResponse,
    DocumentUpdate,
    FactExtractionRequest,
    FactExtractionResponse,
    GraphDiffResponse,
    GraphStatsResponse,
    HybridSearchRequest,
    IndexCreate,
    IndexResponse,
    IngestRequest,
    IngestResponse,
    IngestStageResult,
    LinkCreate,
    LinkResponse,
    LintFinding,
    LintRequest,
    LintResponse,
    PathResponse,
    PathStep,
    PipelineInfo,
    PipelineStageInfo,
    PortfolioRaptorRequest,
    PredicateCreate,
    PredicateResponse,
    RaptorLayerItem,
    RaptorRequest,
    RaptorResponse,
    RoutingMetadata,
    SearchContextCreate,
    SearchContextResponse,
    SearchContextUpdate,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    SegmentExtractionItem,
    SegmentItem,
    SegmentRequest,
    SegmentResponse,
    SourceReference,
    SpineBranchAlternative,
    SpineBranchPoint,
    SpineItem,
    SpineResponse,
    StructuralSplitRequest,
    StructuralSplitResponse,
    StructuralSplitSectionItem,
    SubtreeAuthorityItem,
    SubtreeAuthorityResponse,
    SubtreeResponse,
    SynthesizeRequest,
    SynthesizeResponse,
    TemplateCreate,
    TemplateRenderRequest,
    TemplateRenderResponse,
    TemplateResponse,
    TemplateSearchRequest,
    TemplateUpdate,
    TemplateVariable,
    TokenEmbeddingResponse,
    TopDescendantItem,
    TripleCreate,
    TripleDetailResponse,
    TripleInvalidateRequest,
    TripleResponse,
    TripleSupersedRequest,
    UsetypePresentationCreate,
    UsetypePresentationResponse,
    UsetypePresentationUpdate,
    ViewAncestor,
    ViewChildStub,
    ViewLinkRef,
    ViewPresentation,
    ViewResponse,
    ViewTripleRef,
)

# ============================================================================
# Document Schemas
# ============================================================================


# DocumentCreate, DocumentUpdate, DocumentResponse are defined in
# jmfts_core.contracts.document and re-exported above.


# ============================================================================
# Search Schemas
# ============================================================================


# SearchRequest, HybridSearchRequest, SearchResultItem, SearchResponse,
# AutoSearchRequest, RoutingMetadata, AutoSearchResponse are defined in
# jmfts_core.contracts.search and re-exported above.


# ============================================================================
# Tree Schemas
# ============================================================================


# SubtreeResponse is defined in jmfts_core.contracts.document and re-exported above.


# ============================================================================
# Link Schemas
# ============================================================================


# LinkCreate, LinkResponse are defined in jmfts_core.contracts.document and
# re-exported above.


# ============================================================================
# Index Schemas
# ============================================================================


# IndexCreate, IndexResponse are defined in jmfts_core.contracts.index and
# re-exported above.


# ============================================================================
# Health/Status
# ============================================================================


class LlmHealthStatus(BaseModel):
    """LLM service reachability check"""

    url: str
    model: str
    reachable: bool
    openai_compatible: bool
    detail: Optional[str] = None


class HealthResponse(BaseModel):
    """Health check response"""

    status: str
    version: str
    database: str
    embedding_model: str
    llm: Optional[LlmHealthStatus] = None


# ============================================================================
# Token Embedding Schemas
# ============================================================================


# TokenEmbeddingResponse, DocumentTokensResponse are defined in
# jmfts_core.contracts.document and re-exported above.


# ============================================================================
# Triple/Predicate Schemas
# ============================================================================

# PredicateCreate, PredicateResponse, TripleCreate, TripleResponse,
# TripleDetailResponse, TripleInvalidateRequest, TripleSupersedRequest, PathStep,
# PathResponse are defined in jmfts_core.contracts.triple and re-exported above.


# ============================================================================
# Search Context Schemas
# ============================================================================

# ============================================================================
# Segmentation Schemas
# ============================================================================


# StructuralSplitRequest, StructuralSplitSectionItem, StructuralSplitResponse,
# SegmentRequest, SegmentItem, SegmentResponse, ChunkRequest, ChunkItem, ChunkResponse
# are defined in jmfts_core.contracts.document and re-exported above.


# ============================================================================
# Search Context Schemas
# ============================================================================


# ============================================================================
# Synthesis Schemas
# ============================================================================

# SynthesizeRequest, SourceReference, SynthesizeResponse are defined in
# jmfts_core.contracts.search and re-exported above.


# ============================================================================
# Template Schemas
# ============================================================================

# TemplateVariable, TemplateCreate, TemplateUpdate, TemplateResponse,
# TemplateRenderRequest, TemplateRenderResponse, TemplateSearchRequest are defined in
# jmfts_core.contracts.template and re-exported above.


# ============================================================================
# RAPTOR Summarization Schemas
# ============================================================================


# RaptorRequest, PortfolioRaptorRequest, RaptorLayerItem, RaptorResponse are defined in
# jmfts_core.contracts.document and re-exported above.


# SearchContextCreate, SearchContextUpdate, SearchContextResponse are defined in
# jmfts_core.contracts.search_context and re-exported above.


# ============================================================================
# Fact Extraction Schemas (#58)
# ============================================================================


# ============================================================================
# Conversation Ingestion Schemas (#59)
# ============================================================================


# ConversationMessage, ConversationIngestRequest, ConversationStageResult,
# ConversationIngestResponse are defined in jmfts_core.contracts.conversation and
# re-exported above.


# FactExtractionRequest is defined in jmfts_core.contracts.document and re-exported above.


# SegmentExtractionItem, FactExtractionResponse are defined in
# jmfts_core.contracts.document and re-exported above.


# ============================================================================
# General Ingest Pipeline Schemas (#61)
# ============================================================================


# IngestRequest, IngestStageResult, IngestResponse, PipelineStageInfo, PipelineInfo are
# defined in jmfts_core.contracts.ingest and re-exported above.


# ============================================================================
# Graph Analytics + Lint Schemas
# ============================================================================


# CentralityScoreItem, CentralityResponse, TopDescendantItem,
# SubtreeAuthorityItem, SubtreeAuthorityResponse, SpineBranchAlternative,
# SpineBranchPoint, SpineItem, SpineResponse, CommunityMember, CommunityItem,
# CommunityResponse, GraphDiffResponse, GraphStatsResponse, LintRequest,
# LintFinding, LintResponse are defined in jmfts_core.contracts.graph and
# re-exported above.


# ============================================================================
# Usetype Presentation Schemas (Phase 2)
# ============================================================================
# UsetypePresentationCreate, UsetypePresentationUpdate, UsetypePresentationResponse
# are defined in jmfts_core.contracts.usetype_presentation and re-exported above.


# ============================================================================
# View Schemas (Phase 2)
# ============================================================================
# ViewPresentation, ViewAncestor, ViewChildStub, ViewLinkRef, ViewTripleRef,
# ViewResponse, BreadcrumbResponse, BackReferenceItem, BackReferenceResponse are
# defined in jmfts_core.contracts.view and re-exported above.
