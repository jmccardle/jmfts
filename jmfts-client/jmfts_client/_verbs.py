"""Generated verb table — DO NOT EDIT.

Every method here is one ``@expose``'d JMFTS operation, rendered from the route FastAPI
built for it. Regenerate with ``python -m scripts.generate_client`` in the jmfts
repository; ``tests/test_client_codegen.py`` fails if this file falls behind the surface.

The request logic lives in ``jmfts_client.transport``, not here, so regenerating this file
cannot lose behaviour.
"""

from __future__ import annotations

from typing import Any, Optional

import datetime

from jmfts_client.contracts.access import GrantCreate
from jmfts_client.contracts.access import GrantResponse
from jmfts_client.contracts.access import PrincipalCreate
from jmfts_client.contracts.access import PrincipalResponse
from jmfts_client.contracts.access import TokenCreate
from jmfts_client.contracts.access import TokenMintResponse
from jmfts_client.contracts.access import TokenResponse
from jmfts_client.contracts.conversation import ConversationIngestRequest
from jmfts_client.contracts.conversation import ConversationIngestResponse
from jmfts_client.contracts.document import ChunkRequest
from jmfts_client.contracts.document import ChunkResponse
from jmfts_client.contracts.document import DocumentCreate
from jmfts_client.contracts.document import DocumentResponse
from jmfts_client.contracts.document import DocumentTokensResponse
from jmfts_client.contracts.document import DocumentUpdate
from jmfts_client.contracts.document import FactExtractionRequest
from jmfts_client.contracts.document import FactExtractionResponse
from jmfts_client.contracts.document import LinkCreate
from jmfts_client.contracts.document import LinkResponse
from jmfts_client.contracts.document import PortfolioRaptorRequest
from jmfts_client.contracts.document import RaptorRequest
from jmfts_client.contracts.document import RaptorResponse
from jmfts_client.contracts.document import SegmentRequest
from jmfts_client.contracts.document import SegmentResponse
from jmfts_client.contracts.document import StructuralSplitRequest
from jmfts_client.contracts.document import StructuralSplitResponse
from jmfts_client.contracts.document import SubtreeResponse
from jmfts_client.contracts.explain import AnalyzeIngestResponse
from jmfts_client.contracts.explain import ExplainIngestRequest
from jmfts_client.contracts.explain import ExplainIngestResponse
from jmfts_client.contracts.graph import CentralityResponse
from jmfts_client.contracts.graph import CommunityResponse
from jmfts_client.contracts.graph import GraphDiffResponse
from jmfts_client.contracts.graph import GraphStatsResponse
from jmfts_client.contracts.graph import LintRequest
from jmfts_client.contracts.graph import LintResponse
from jmfts_client.contracts.graph import NeighborsResponse
from jmfts_client.contracts.graph import SpineResponse
from jmfts_client.contracts.graph import SubtreeAuthorityResponse
from jmfts_client.contracts.index import IndexCreate
from jmfts_client.contracts.index import IndexResponse
from jmfts_client.contracts.ingest import IngestRequest
from jmfts_client.contracts.ingest import IngestResponse
from jmfts_client.contracts.ingest import PipelineInfo
from jmfts_client.contracts.search import AutoSearchRequest
from jmfts_client.contracts.search import AutoSearchResponse
from jmfts_client.contracts.search import HybridSearchRequest
from jmfts_client.contracts.search import SearchRequest
from jmfts_client.contracts.search import SearchResponse
from jmfts_client.contracts.search import SynthesizeRequest
from jmfts_client.contracts.search import SynthesizeResponse
from jmfts_client.contracts.search_context import SearchContextCreate
from jmfts_client.contracts.search_context import SearchContextResponse
from jmfts_client.contracts.search_context import SearchContextUpdate
from jmfts_client.contracts.template import TemplateCreate
from jmfts_client.contracts.template import TemplateRenderRequest
from jmfts_client.contracts.template import TemplateRenderResponse
from jmfts_client.contracts.template import TemplateResponse
from jmfts_client.contracts.template import TemplateSearchRequest
from jmfts_client.contracts.template import TemplateUpdate
from jmfts_client.contracts.triple import PathResponse
from jmfts_client.contracts.triple import PredicateCreate
from jmfts_client.contracts.triple import PredicateResponse
from jmfts_client.contracts.triple import TripleCreate
from jmfts_client.contracts.triple import TripleDetailResponse
from jmfts_client.contracts.triple import TripleInvalidateRequest
from jmfts_client.contracts.triple import TripleResponse
from jmfts_client.contracts.triple import TripleSupersedRequest
from jmfts_client.contracts.upload import FileUploadResponse
from jmfts_client.contracts.upload import IngestFrontierResponse
from jmfts_client.contracts.upload import UploadedFile
from jmfts_client.contracts.usetype_presentation import UsetypePresentationCreate
from jmfts_client.contracts.usetype_presentation import UsetypePresentationResponse
from jmfts_client.contracts.usetype_presentation import UsetypePresentationUpdate
from jmfts_client.contracts.view import BackReferenceResponse
from jmfts_client.contracts.view import BreadcrumbResponse
from jmfts_client.contracts.view import ViewChildStub
from jmfts_client.contracts.view import ViewResponse

from jmfts_client.transport import _VerbTransport


class _GeneratedVerbs(_VerbTransport):
    """Every exposed JMFTS operation, as a method. Mixed into ``RemoteJmftsClient``."""

    def create_principal(
        self,
        request: PrincipalCreate,
    ) -> PrincipalResponse:
        """Create a non-owner principal

        ``POST /access/principals`` — AccessService.create_principal

        Raises on 409 (server: PrincipalConflictError).
        """
        return self._call(
            "POST",
            "/access/principals",
            body=request,
            response=PrincipalResponse,
        )

    def delete_principal(
        self,
        principal_id: int,
    ) -> Any:
        """Delete a principal (cascades its tokens and grants)

        ``DELETE /access/principals/{principal_id}`` — AccessService.delete_principal

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "DELETE",
            "/access/principals/{principal_id}",
            path={"principal_id": principal_id},
            response=None,
        )

    def grant(
        self,
        document_id: int,
        request: GrantCreate,
    ) -> GrantResponse:
        """Grant a principal read/write on a document (makes it an ACR)

        ``POST /access/documents/{document_id}/grants`` — AccessService.grant

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/access/documents/{document_id}/grants",
            path={"document_id": document_id},
            body=request,
            response=GrantResponse,
        )

    def list_grants(
        self,
        document_id: int,
    ) -> list[GrantResponse]:
        """List the grants on a document (its ACR grants)

        ``GET /access/documents/{document_id}/grants`` — AccessService.list_grants
        """
        return self._call(
            "GET",
            "/access/documents/{document_id}/grants",
            path={"document_id": document_id},
            response=list[GrantResponse],
        )

    def list_principals(
        self,
    ) -> list[PrincipalResponse]:
        """List principals

        ``GET /access/principals`` — AccessService.list_principals
        """
        return self._call(
            "GET",
            "/access/principals",
            response=list[PrincipalResponse],
        )

    def list_tokens(
        self,
        principal_id: int,
    ) -> list[TokenResponse]:
        """List a principal's tokens (metadata only)

        ``GET /access/principals/{principal_id}/tokens`` — AccessService.list_tokens

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/access/principals/{principal_id}/tokens",
            path={"principal_id": principal_id},
            response=list[TokenResponse],
        )

    def mint_token(
        self,
        principal_id: int,
        request: TokenCreate,
    ) -> TokenMintResponse:
        """Mint a bearer token for a principal (raw token returned once)

        ``POST /access/principals/{principal_id}/tokens`` — AccessService.mint_token

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/access/principals/{principal_id}/tokens",
            path={"principal_id": principal_id},
            body=request,
            response=TokenMintResponse,
        )

    def revoke_grant(
        self,
        document_id: int,
        principal_id: int,
    ) -> Any:
        """Revoke a principal's grant (un-marks the ACR when the last one goes)

        ``DELETE /access/documents/{document_id}/grants/{principal_id}`` — AccessService.revoke_grant

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "DELETE",
            "/access/documents/{document_id}/grants/{principal_id}",
            path={"document_id": document_id, "principal_id": principal_id},
            response=None,
        )

    def revoke_token(
        self,
        token_id: int,
    ) -> Any:
        """Revoke a token

        ``DELETE /access/tokens/{token_id}`` — AccessService.revoke_token

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "DELETE",
            "/access/tokens/{token_id}",
            path={"token_id": token_id},
            response=None,
        )

    def ingest_conversation(
        self,
        request: ConversationIngestRequest,
    ) -> ConversationIngestResponse:
        """Ingest a conversation through the full pipeline

        ``POST /conversations/ingest`` — ConversationService.ingest_conversation

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/conversations/ingest",
            body=request,
            response=ConversationIngestResponse,
        )

    def chunk_document(
        self,
        document_id: int,
        *,
        request: Optional[ChunkRequest] = None,
    ) -> ChunkResponse:
        """Split a document's content into sentence/paragraph/token-count chunks

        ``POST /documents/{document_id}/chunk`` — DocumentService.chunk_document

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/documents/{document_id}/chunk",
            path={"document_id": document_id},
            body=request,
            response=ChunkResponse,
        )

    def create_document(
        self,
        request: DocumentCreate,
        *,
        dedup: bool = False,
    ) -> DocumentResponse:
        """Create a new document

        ``POST /documents`` — DocumentService.create_document

        Raises on 400 (server: ValueError).
        """
        return self._call(
            "POST",
            "/documents",
            query={"dedup": dedup},
            body=request,
            response=DocumentResponse,
        )

    def create_link(
        self,
        document_id: int,
        request: LinkCreate,
    ) -> LinkResponse:
        """Create a link from this document to another

        ``POST /documents/{document_id}/links`` — DocumentService.create_link

        Raises on 400 (server: ValueError).
        """
        return self._call(
            "POST",
            "/documents/{document_id}/links",
            path={"document_id": document_id},
            body=request,
            response=LinkResponse,
        )

    def delete_document(
        self,
        document_id: int,
    ) -> Any:
        """Delete a document and its children

        ``DELETE /documents/{document_id}`` — DocumentService.delete_document

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "DELETE",
            "/documents/{document_id}",
            path={"document_id": document_id},
            response=None,
        )

    def delete_link(
        self,
        document_id: int,
        link_id: int,
    ) -> Any:
        """Delete a link incident to this document

        ``DELETE /documents/{document_id}/links/{link_id}`` — DocumentService.delete_link

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "DELETE",
            "/documents/{document_id}/links/{link_id}",
            path={"document_id": document_id, "link_id": link_id},
            response=None,
        )

    def embed_document(
        self,
        document_id: int,
        *,
        with_tokens: bool = True,
        write_importance: bool = False,
    ) -> Any:
        """Generate embeddings for a document

        ``POST /documents/{document_id}/embed`` — DocumentService.embed_document

        Raises on 400 (server: EmbedTextTooLongError).
        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "POST",
            "/documents/{document_id}/embed",
            path={"document_id": document_id},
            query={"with_tokens": with_tokens, "write_importance": write_importance},
            response=None,
        )

    def extract_facts_endpoint(
        self,
        document_id: int,
        *,
        request: Optional[FactExtractionRequest] = None,
    ) -> FactExtractionResponse:
        """Extract knowledge triples from a document's segments via LLM

        ``POST /documents/{document_id}/extract-facts`` — DocumentService.extract_facts_endpoint

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/documents/{document_id}/extract-facts",
            path={"document_id": document_id},
            body=request,
            response=FactExtractionResponse,
        )

    def get_ancestors(
        self,
        document_id: int,
    ) -> list[DocumentResponse]:
        """Get all ancestors (path to root)

        ``GET /documents/{document_id}/ancestors`` — DocumentService.get_ancestors
        """
        return self._call(
            "GET",
            "/documents/{document_id}/ancestors",
            path={"document_id": document_id},
            response=list[DocumentResponse],
        )

    def get_children(
        self,
        document_id: int,
        *,
        title: Optional[str] = None,
        title_prefix: Optional[str] = None,
        usetype: Optional[str] = None,
        depth: int = 1,
        limit: int = 100,
    ) -> list[DocumentResponse]:
        """Get children of a document with optional filtering

        ``GET /documents/{document_id}/children`` — DocumentService.get_children
        """
        return self._call(
            "GET",
            "/documents/{document_id}/children",
            path={"document_id": document_id},
            query={
                "title": title,
                "title_prefix": title_prefix,
                "usetype": usetype,
                "depth": depth,
                "limit": limit,
            },
            response=list[DocumentResponse],
        )

    def get_document(
        self,
        document_id: int,
        *,
        include_embed: bool = False,
    ) -> DocumentResponse:
        """Get a document by ID

        ``GET /documents/{document_id}`` — DocumentService.get_document

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/documents/{document_id}",
            path={"document_id": document_id},
            query={"include_embed": include_embed},
            response=DocumentResponse,
        )

    def get_document_tokens(
        self,
        document_id: int,
    ) -> DocumentTokensResponse:
        """Get token embeddings for a document (for inspection)

        ``GET /documents/{document_id}/tokens`` — DocumentService.get_document_tokens

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/documents/{document_id}/tokens",
            path={"document_id": document_id},
            response=DocumentTokensResponse,
        )

    def get_links(
        self,
        document_id: int,
        *,
        direction: str = "both",
        link_type: Optional[str] = None,
    ) -> list[LinkResponse]:
        """Get links for a document

        ``GET /documents/{document_id}/links`` — DocumentService.get_links

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/documents/{document_id}/links",
            path={"document_id": document_id},
            query={"direction": direction, "link_type": link_type},
            response=list[LinkResponse],
        )

    def get_root_documents(
        self,
    ) -> list[DocumentResponse]:
        """Get all root documents (no parent)

        ``GET /documents/roots`` — DocumentService.get_root_documents
        """
        return self._call(
            "GET",
            "/documents/roots",
            response=list[DocumentResponse],
        )

    def get_siblings(
        self,
        document_id: int,
        *,
        include_self: bool = False,
    ) -> list[DocumentResponse]:
        """Get siblings of a document

        ``GET /documents/{document_id}/siblings`` — DocumentService.get_siblings
        """
        return self._call(
            "GET",
            "/documents/{document_id}/siblings",
            path={"document_id": document_id},
            query={"include_self": include_self},
            response=list[DocumentResponse],
        )

    def get_subtree(
        self,
        document_id: int,
        *,
        max_depth: Optional[int] = None,
        include_in_flight: bool = False,
    ) -> SubtreeResponse:
        """Get all documents in a subtree

        ``GET /documents/{document_id}/subtree`` — DocumentService.get_subtree

        Raises on 404 (server: LookupError).
        Raises on 409 (server: InFlightSubtreeError).
        """
        return self._call(
            "GET",
            "/documents/{document_id}/subtree",
            path={"document_id": document_id},
            query={"max_depth": max_depth, "include_in_flight": include_in_flight},
            response=SubtreeResponse,
        )

    def list_documents(
        self,
        *,
        parent_id: Optional[int] = None,
        usetype: Optional[str] = None,
        title_prefix: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DocumentResponse]:
        """List documents with optional filters

        ``GET /documents`` — DocumentService.list_documents
        """
        return self._call(
            "GET",
            "/documents",
            query={
                "parent_id": parent_id,
                "usetype": usetype,
                "title_prefix": title_prefix,
                "limit": limit,
                "offset": offset,
            },
            response=list[DocumentResponse],
        )

    def raptor_portfolio_summarize(
        self,
        document_id: int,
        *,
        request: Optional[PortfolioRaptorRequest] = None,
    ) -> RaptorResponse:
        """Run cross-document RAPTOR over report-level summaries under a portfolio root

        ``POST /documents/{document_id}/raptor/portfolio`` — DocumentService.raptor_portfolio_summarize

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/documents/{document_id}/raptor/portfolio",
            path={"document_id": document_id},
            body=request,
            response=RaptorResponse,
        )

    def raptor_summarize_document(
        self,
        document_id: int,
        *,
        request: Optional[RaptorRequest] = None,
    ) -> RaptorResponse:
        """Run RAPTOR hierarchical summarization on a document's children

        ``POST /documents/{document_id}/raptor`` — DocumentService.raptor_summarize_document

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/documents/{document_id}/raptor",
            path={"document_id": document_id},
            body=request,
            response=RaptorResponse,
        )

    def segment_document(
        self,
        document_id: int,
        *,
        request: Optional[SegmentRequest] = None,
    ) -> SegmentResponse:
        """Detect topic boundaries among a document's children (PELT)

        ``POST /documents/{document_id}/segment`` — DocumentService.segment_document

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/documents/{document_id}/segment",
            path={"document_id": document_id},
            body=request,
            response=SegmentResponse,
        )

    def split_document(
        self,
        document_id: int,
        *,
        request: Optional[StructuralSplitRequest] = None,
    ) -> StructuralSplitResponse:
        """Split a document on markdown heading boundaries, creating child documents

        ``POST /documents/{document_id}/split`` — DocumentService.split_document

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/documents/{document_id}/split",
            path={"document_id": document_id},
            body=request,
            response=StructuralSplitResponse,
        )

    def update_document(
        self,
        document_id: int,
        request: DocumentUpdate,
    ) -> DocumentResponse:
        """Update a document's fields and/or move it under a new parent

        ``PATCH /documents/{document_id}`` — DocumentService.update_document

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "PATCH",
            "/documents/{document_id}",
            path={"document_id": document_id},
            body=request,
            response=DocumentResponse,
        )

    def get_centrality(
        self,
        *,
        metric: str = "pagerank",
        scope: str = "links",
        parent_id: Optional[int] = None,
        exclude_usetypes: Optional[str] = None,
        top: int = 20,
    ) -> CentralityResponse:
        """Flat top-N centrality scores

        ``GET /graph/centrality`` — GraphService.get_centrality
        """
        return self._call(
            "GET",
            "/graph/centrality",
            query={
                "metric": metric,
                "scope": scope,
                "parent_id": parent_id,
                "exclude_usetypes": exclude_usetypes,
                "top": top,
            },
            response=CentralityResponse,
        )

    def get_communities(
        self,
        *,
        scope: str = "links",
        parent_id: Optional[int] = None,
        exclude_usetypes: Optional[str] = None,
        resolution: float = 1.0,
        min_size: int = 2,
    ) -> CommunityResponse:
        """Leiden communities over the chosen edge graph

        ``GET /graph/communities`` — GraphService.get_communities
        """
        return self._call(
            "GET",
            "/graph/communities",
            query={
                "scope": scope,
                "parent_id": parent_id,
                "exclude_usetypes": exclude_usetypes,
                "resolution": resolution,
                "min_size": min_size,
            },
            response=CommunityResponse,
        )

    def get_diff(
        self,
        *,
        since: Optional[datetime.datetime] = None,
        until: Optional[datetime.datetime] = None,
    ) -> GraphDiffResponse:
        """Counts of new/changed/superseded entities in the time window

        ``GET /graph/diff`` — GraphService.get_diff
        """
        return self._call(
            "GET",
            "/graph/diff",
            query={"since": since, "until": until},
            response=GraphDiffResponse,
        )

    def get_neighbors(
        self,
        root_id: int,
        *,
        max_depth: int = 2,
        direction: str = "both",
        link_types: Optional[str] = None,
        limit: int = 200,
    ) -> NeighborsResponse:
        """Link-graph neighbors of a root document (bounded BFS)

        ``GET /graph/neighbors`` — GraphService.get_neighbors

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/graph/neighbors",
            query={
                "root_id": root_id,
                "max_depth": max_depth,
                "direction": direction,
                "link_types": link_types,
                "limit": limit,
            },
            response=NeighborsResponse,
        )

    def get_spines(
        self,
        root_id: int,
        *,
        metric: str = "pagerank",
        scope: str = "links",
        branching_threshold: float = 0.85,
        max_paths: int = 5,
        max_depth: Optional[int] = None,
        exclude_usetypes: Optional[str] = None,
    ) -> SpineResponse:
        """Top reading paths through the subtree of root_id

        ``GET /graph/spines`` — GraphService.get_spines

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/graph/spines",
            query={
                "root_id": root_id,
                "metric": metric,
                "scope": scope,
                "branching_threshold": branching_threshold,
                "max_paths": max_paths,
                "max_depth": max_depth,
                "exclude_usetypes": exclude_usetypes,
            },
            response=SpineResponse,
        )

    def get_stats(
        self,
    ) -> GraphStatsResponse:
        """Corpus-wide aggregate counts

        ``GET /graph/stats`` — GraphService.get_stats
        """
        return self._call(
            "GET",
            "/graph/stats",
            response=GraphStatsResponse,
        )

    def get_subtree_authority(
        self,
        *,
        metric: str = "pagerank",
        scope: str = "links",
        decay: float = 0.7,
        min_subtree_size: int = 3,
        parent_id: Optional[int] = None,
        exclude_usetypes: Optional[str] = None,
        top: int = 20,
    ) -> SubtreeAuthorityResponse:
        """Hierarchical roll-up: descendant centrality flows up the tree with decay

        ``GET /graph/subtree-authority`` — GraphService.get_subtree_authority
        """
        return self._call(
            "GET",
            "/graph/subtree-authority",
            query={
                "metric": metric,
                "scope": scope,
                "decay": decay,
                "min_subtree_size": min_subtree_size,
                "parent_id": parent_id,
                "exclude_usetypes": exclude_usetypes,
                "top": top,
            },
            response=SubtreeAuthorityResponse,
        )

    def post_lint(
        self,
        request: LintRequest,
    ) -> LintResponse:
        """Run orphan + contradiction + stale + coverage audits in one transaction

        ``POST /graph/lint`` — GraphService.post_lint
        """
        return self._call(
            "POST",
            "/graph/lint",
            body=request,
            response=LintResponse,
        )

    def add_root_to_index(
        self,
        index_name: str,
        root_document_id: int,
    ) -> Any:
        """Add a document subtree to an index

        ``POST /indexes/{index_name}/roots`` — IndexService.add_root_to_index

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "POST",
            "/indexes/{index_name}/roots",
            path={"index_name": index_name},
            query={"root_document_id": root_document_id},
            response=None,
        )

    def create_index(
        self,
        request: IndexCreate,
    ) -> IndexResponse:
        """Create a new search index

        ``POST /indexes`` — IndexService.create_index

        Raises on 409 (server: IndexConflictError).
        """
        return self._call(
            "POST",
            "/indexes",
            body=request,
            response=IndexResponse,
        )

    def delete_index(
        self,
        index_name: str,
    ) -> Any:
        """Delete a search index

        ``DELETE /indexes/{index_name}`` — IndexService.delete_index

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "DELETE",
            "/indexes/{index_name}",
            path={"index_name": index_name},
            response=None,
        )

    def get_index(
        self,
        index_name: str,
    ) -> IndexResponse:
        """Get a search index by name

        ``GET /indexes/{index_name}`` — IndexService.get_index

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/indexes/{index_name}",
            path={"index_name": index_name},
            response=IndexResponse,
        )

    def get_index_roots(
        self,
        index_name: str,
    ) -> Any:
        """Get root document IDs for an index

        ``GET /indexes/{index_name}/roots`` — IndexService.get_index_roots

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "GET",
            "/indexes/{index_name}/roots",
            path={"index_name": index_name},
            response=None,
        )

    def index_single_document(
        self,
        index_name: str,
        document_id: int,
    ) -> Any:
        """Index a single document into the index

        ``POST /indexes/{index_name}/index-document/{document_id}`` — IndexService.index_single_document

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "POST",
            "/indexes/{index_name}/index-document/{document_id}",
            path={"index_name": index_name, "document_id": document_id},
            response=None,
        )

    def list_indexes(
        self,
    ) -> list[IndexResponse]:
        """List all search indexes

        ``GET /indexes`` — IndexService.list_indexes
        """
        return self._call(
            "GET",
            "/indexes",
            response=list[IndexResponse],
        )

    def refresh_index(
        self,
        index_name: str,
    ) -> Any:
        """Rebuild an index from its member subtrees

        ``POST /indexes/{index_name}/refresh`` — IndexService.refresh_index

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "POST",
            "/indexes/{index_name}/refresh",
            path={"index_name": index_name},
            response=None,
        )

    def remove_root_from_index(
        self,
        index_name: str,
        root_document_id: int,
    ) -> Any:
        """Remove a document subtree from an index

        ``DELETE /indexes/{index_name}/roots/{root_document_id}`` — IndexService.remove_root_from_index

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "DELETE",
            "/indexes/{index_name}/roots/{root_document_id}",
            path={"index_name": index_name, "root_document_id": root_document_id},
            response=None,
        )

    def analyze_ingest(
        self,
        file: UploadedFile,
        *,
        options: Optional[dict] = None,
        private: bool = False,
    ) -> AnalyzeIngestResponse:
        """What THESE bytes would do: run probe, report the plan, store nothing

        ``POST /ingest/analyze`` — IngestService.analyze_ingest

        Raises on 400 (server: ValueError).
        """
        return self._call(
            "POST",
            "/ingest/analyze",
            query={"private": private},
            form_json={"options": options},
            files={"file": file},
            response=AnalyzeIngestResponse,
        )

    def explain_ingest(
        self,
        request: ExplainIngestRequest,
    ) -> ExplainIngestResponse:
        """What a format with these options would do, without uploading anything

        ``POST /ingest/explain`` — IngestService.explain_ingest

        Raises on 400 (server: ValueError).
        """
        return self._call(
            "POST",
            "/ingest/explain",
            body=request,
            response=ExplainIngestResponse,
        )

    def file_frontier(
        self,
        document_id: int,
    ) -> IngestFrontierResponse:
        """Progress for an in-flight ingestion: nodes settled, in flight, failed

        ``GET /ingest/file/{document_id}/frontier`` — IngestService.file_frontier

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/ingest/file/{document_id}/frontier",
            path={"document_id": document_id},
            response=IngestFrontierResponse,
        )

    def ingest_content(
        self,
        request: IngestRequest,
    ) -> IngestResponse:
        """Ingest content through a named pipeline

        ``POST /ingest`` — IngestService.ingest_content

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/ingest",
            body=request,
            response=IngestResponse,
        )

    def list_registered_pipelines(
        self,
    ) -> list[PipelineInfo]:
        """List all registered pipeline definitions

        ``GET /ingest/pipelines`` — IngestService.list_registered_pipelines
        """
        return self._call(
            "GET",
            "/ingest/pipelines",
            response=list[PipelineInfo],
        )

    def upload_file(
        self,
        file: UploadedFile,
        *,
        options: Optional[dict] = None,
        parent_id: Optional[int] = None,
        private: bool = False,
    ) -> FileUploadResponse:
        """Upload a file, create its in-flight file node, and enqueue `probe`

        ``POST /ingest/file`` — IngestService.upload_file

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/ingest/file",
            query={"parent_id": parent_id, "private": private},
            form_json={"options": options},
            files={"file": file},
            response=FileUploadResponse,
        )

    def create_context(
        self,
        request: SearchContextCreate,
    ) -> SearchContextResponse:
        """Create a new search context preset

        ``POST /search-contexts/`` — SearchContextService.create_context

        Raises on 409 (server: SearchContextConflictError).
        """
        return self._call(
            "POST",
            "/search-contexts/",
            body=request,
            response=SearchContextResponse,
        )

    def delete_context(
        self,
        name: str,
    ) -> Any:
        """Delete a search context preset

        ``DELETE /search-contexts/{name}`` — SearchContextService.delete_context

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "DELETE",
            "/search-contexts/{name}",
            path={"name": name},
            response=None,
        )

    def get_context(
        self,
        name: str,
    ) -> SearchContextResponse:
        """Get a search context preset by name

        ``GET /search-contexts/{name}`` — SearchContextService.get_context

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/search-contexts/{name}",
            path={"name": name},
            response=SearchContextResponse,
        )

    def list_contexts(
        self,
    ) -> list[SearchContextResponse]:
        """List all search context presets

        ``GET /search-contexts/`` — SearchContextService.list_contexts
        """
        return self._call(
            "GET",
            "/search-contexts/",
            response=list[SearchContextResponse],
        )

    def update_context(
        self,
        name: str,
        request: SearchContextUpdate,
    ) -> SearchContextResponse:
        """Update a search context preset

        ``PUT /search-contexts/{name}`` — SearchContextService.update_context

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "PUT",
            "/search-contexts/{name}",
            path={"name": name},
            body=request,
            response=SearchContextResponse,
        )

    def auto_search(
        self,
        request: AutoSearchRequest,
        *,
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> AutoSearchResponse:
        """Automatic search — analyzes the query and picks the best method

        ``POST /search/auto`` — SearchService.auto_search

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/search/auto",
            query={"context": context, "rerank": rerank, "rerank_method": rerank_method},
            body=request,
            response=AutoSearchResponse,
        )

    def bm25_search(
        self,
        request: SearchRequest,
        *,
        index_name: str = "default",
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> SearchResponse:
        """BM25 lexical search

        ``POST /search/bm25`` — SearchService.bm25_search

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/search/bm25",
            query={
                "index_name": index_name,
                "context": context,
                "rerank": rerank,
                "rerank_method": rerank_method,
            },
            body=request,
            response=SearchResponse,
        )

    def fulltext_search(
        self,
        request: SearchRequest,
        *,
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> SearchResponse:
        """PostgreSQL full-text search

        ``POST /search/fulltext`` — SearchService.fulltext_search

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/search/fulltext",
            query={"context": context, "rerank": rerank, "rerank_method": rerank_method},
            body=request,
            response=SearchResponse,
        )

    def hybrid_search(
        self,
        request: HybridSearchRequest,
        *,
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> SearchResponse:
        """Hybrid search with Reciprocal Rank Fusion

        ``POST /search/hybrid`` — SearchService.hybrid_search

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/search/hybrid",
            query={"context": context, "rerank": rerank, "rerank_method": rerank_method},
            body=request,
            response=SearchResponse,
        )

    def maxsim_search(
        self,
        request: SearchRequest,
        *,
        embed_dim: int = 256,
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> SearchResponse:
        """ColBERT-style MaxSim late interaction search

        ``POST /search/maxsim`` — SearchService.maxsim_search

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/search/maxsim",
            query={
                "embed_dim": embed_dim,
                "context": context,
                "rerank": rerank,
                "rerank_method": rerank_method,
            },
            body=request,
            response=SearchResponse,
        )

    def quick_search(
        self,
        q: str,
        *,
        limit: int = 10,
        method: str = "hybrid",
        usetype: Optional[str] = None,
        parent_id: Optional[int] = None,
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> SearchResponse:
        """Quick search endpoint (GET)

        ``GET /search/`` — SearchService.quick_search

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/search/",
            query={
                "q": q,
                "limit": limit,
                "method": method,
                "usetype": usetype,
                "parent_id": parent_id,
                "context": context,
                "rerank": rerank,
                "rerank_method": rerank_method,
            },
            response=SearchResponse,
        )

    def synthesize_search(
        self,
        request: SynthesizeRequest,
        *,
        context: Optional[str] = None,
    ) -> SynthesizeResponse:
        """Search then synthesize an answer via LLM

        ``POST /search/synthesize`` — SearchService.synthesize_search

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/search/synthesize",
            query={"context": context},
            body=request,
            response=SynthesizeResponse,
        )

    def vector_search(
        self,
        request: SearchRequest,
        *,
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> SearchResponse:
        """Semantic vector search

        ``POST /search/vector`` — SearchService.vector_search

        Raises on 400 (server: ValueError).
        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/search/vector",
            query={"context": context, "rerank": rerank, "rerank_method": rerank_method},
            body=request,
            response=SearchResponse,
        )

    def create_template(
        self,
        request: TemplateCreate,
    ) -> TemplateResponse:
        """Create a new prompt template

        ``POST /templates`` — TemplateService.create_template

        Raises on 422 (server: InvalidCategoryError).
        """
        return self._call(
            "POST",
            "/templates",
            body=request,
            response=TemplateResponse,
        )

    def get_template(
        self,
        template_id: int,
    ) -> TemplateResponse:
        """Get a single template by ID

        ``GET /templates/{template_id}`` — TemplateService.get_template

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/templates/{template_id}",
            path={"template_id": template_id},
            response=TemplateResponse,
        )

    def list_templates(
        self,
        *,
        category: Optional[str] = None,
    ) -> list[TemplateResponse]:
        """List all templates, optionally filtered by category

        ``GET /templates`` — TemplateService.list_templates

        Raises on 422 (server: InvalidCategoryError).
        """
        return self._call(
            "GET",
            "/templates",
            query={"category": category},
            response=list[TemplateResponse],
        )

    def render_template(
        self,
        template_id: int,
        request: TemplateRenderRequest,
    ) -> TemplateRenderResponse:
        """Render a template by substituting {{variable}} placeholders

        ``POST /templates/{template_id}/render`` — TemplateService.render_template

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/templates/{template_id}/render",
            path={"template_id": template_id},
            body=request,
            response=TemplateRenderResponse,
        )

    def search_templates(
        self,
        request: TemplateSearchRequest,
    ) -> SearchResponse:
        """Semantic search for templates by description

        ``POST /templates/search`` — TemplateService.search_templates

        Raises on 422 (server: InvalidCategoryError).
        """
        return self._call(
            "POST",
            "/templates/search",
            body=request,
            response=SearchResponse,
        )

    def update_template(
        self,
        template_id: int,
        request: TemplateUpdate,
    ) -> TemplateResponse:
        """Update a template's content or metadata

        ``PUT /templates/{template_id}`` — TemplateService.update_template

        Raises on 404 (server: LookupError).
        Raises on 422 (server: InvalidCategoryError).
        """
        return self._call(
            "PUT",
            "/templates/{template_id}",
            path={"template_id": template_id},
            body=request,
            response=TemplateResponse,
        )

    def create_predicate(
        self,
        request: PredicateCreate,
    ) -> PredicateResponse:
        """Create a new predicate

        ``POST /triples/predicates`` — TripleService.create_predicate

        Raises on 409 (server: PredicateConflictError).
        """
        return self._call(
            "POST",
            "/triples/predicates",
            body=request,
            response=PredicateResponse,
        )

    def create_triple(
        self,
        request: TripleCreate,
        *,
        dedup: bool = False,
    ) -> TripleResponse:
        """Create a new triple

        ``POST /triples`` — TripleService.create_triple

        Raises on 422 (server: InvalidFactTypeError).
        """
        return self._call(
            "POST",
            "/triples",
            query={"dedup": dedup},
            body=request,
            response=TripleResponse,
        )

    def delete_predicate(
        self,
        predicate_id: int,
    ) -> Any:
        """Delete a predicate and all its triples

        ``DELETE /triples/predicates/{predicate_id}`` — TripleService.delete_predicate

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "DELETE",
            "/triples/predicates/{predicate_id}",
            path={"predicate_id": predicate_id},
            response=None,
        )

    def delete_triple(
        self,
        triple_id: int,
    ) -> Any:
        """Delete a triple

        ``DELETE /triples/{triple_id}`` — TripleService.delete_triple

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "DELETE",
            "/triples/{triple_id}",
            path={"triple_id": triple_id},
            response=None,
        )

    def find_path(
        self,
        from_id: int,
        to_id: int,
        *,
        max_depth: int = 5,
    ) -> PathResponse:
        """Find paths between two entities via triples

        ``GET /triples/path`` — TripleService.find_path
        """
        return self._call(
            "GET",
            "/triples/path",
            query={"from_id": from_id, "to_id": to_id, "max_depth": max_depth},
            response=PathResponse,
        )

    def get_predicate(
        self,
        predicate_id: int,
    ) -> PredicateResponse:
        """Get a predicate by ID

        ``GET /triples/predicates/{predicate_id}`` — TripleService.get_predicate

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/triples/predicates/{predicate_id}",
            path={"predicate_id": predicate_id},
            response=PredicateResponse,
        )

    def get_triple(
        self,
        triple_id: int,
    ) -> TripleResponse:
        """Get a triple by ID

        ``GET /triples/{triple_id}`` — TripleService.get_triple

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/triples/{triple_id}",
            path={"triple_id": triple_id},
            response=TripleResponse,
        )

    def invalidate_triple(
        self,
        triple_id: int,
        request: TripleInvalidateRequest,
    ) -> TripleResponse:
        """Invalidate a triple (soft-delete for contradicted facts)

        ``POST /triples/{triple_id}/invalidate`` — TripleService.invalidate_triple

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "POST",
            "/triples/{triple_id}/invalidate",
            path={"triple_id": triple_id},
            body=request,
            response=TripleResponse,
        )

    def list_predicates(
        self,
        *,
        domain: Optional[str] = None,
        with_triples_only: bool = True,
    ) -> list[PredicateResponse]:
        """List predicates

        ``GET /triples/predicates`` — TripleService.list_predicates
        """
        return self._call(
            "GET",
            "/triples/predicates",
            query={"domain": domain, "with_triples_only": with_triples_only},
            response=list[PredicateResponse],
        )

    def query_triples(
        self,
        *,
        entity_id: Optional[int] = None,
        predicate_id: Optional[int] = None,
        predicate: Optional[str] = None,
        direction: str = "both",
        limit: int = 50,
        offset: int = 0,
        valid_only: bool = False,
        valid_at: Optional[datetime.datetime] = None,
        fact_type: Optional[str] = None,
        include_invalidated: bool = True,
        coreferent: bool = False,
    ) -> list[TripleDetailResponse]:
        """Query triples with optional filters including temporal filtering

        ``GET /triples/query`` — TripleService.query_triples

        Raises on 422 (server: InvalidFactTypeError).
        """
        return self._call(
            "GET",
            "/triples/query",
            query={
                "entity_id": entity_id,
                "predicate_id": predicate_id,
                "predicate": predicate,
                "direction": direction,
                "limit": limit,
                "offset": offset,
                "valid_only": valid_only,
                "valid_at": valid_at,
                "fact_type": fact_type,
                "include_invalidated": include_invalidated,
                "coreferent": coreferent,
            },
            response=list[TripleDetailResponse],
        )

    def supersede_triple(
        self,
        triple_id: int,
        request: TripleSupersedRequest,
    ) -> TripleResponse:
        """Create a new triple that supersedes (invalidates) an existing one

        ``POST /triples/{triple_id}/supersede`` — TripleService.supersede_triple

        Raises on 404 (server: LookupError).
        Raises on 409 (server: TripleAlreadyInvalidatedError).
        Raises on 422 (server: InvalidFactTypeError).
        """
        return self._call(
            "POST",
            "/triples/{triple_id}/supersede",
            path={"triple_id": triple_id},
            body=request,
            response=TripleResponse,
        )

    def upsert_triple(
        self,
        request: TripleCreate,
    ) -> TripleResponse:
        """Assert a fact idempotently (insert-or-return the existing triple)

        ``PUT /triples`` — TripleService.upsert_triple

        Raises on 422 (server: InvalidFactTypeError).
        """
        return self._call(
            "PUT",
            "/triples",
            body=request,
            response=TripleResponse,
        )

    def create_presentation(
        self,
        request: UsetypePresentationCreate,
    ) -> UsetypePresentationResponse:
        """Create a presentation rule for a usetype (use '*' for the catch-all).

        ``POST /usetype-presentations/`` — UsetypePresentationService.create_presentation

        Raises on 409 (server: UsetypePresentationConflictError).
        Raises on 422 (server: ValueError).
        """
        return self._call(
            "POST",
            "/usetype-presentations/",
            body=request,
            response=UsetypePresentationResponse,
        )

    def delete_presentation(
        self,
        usetype: str,
    ) -> Any:
        """Delete a presentation rule.

        ``DELETE /usetype-presentations/{usetype:path}`` — UsetypePresentationService.delete_presentation

        Raises on 404 (server: LookupError).

        The route declares no response model, so the parsed JSON is returned.
        """
        return self._call(
            "DELETE",
            "/usetype-presentations/{usetype:path}",
            path={"usetype": usetype},
            response=None,
        )

    def get_presentation(
        self,
        usetype: str,
    ) -> UsetypePresentationResponse:
        """Look up the presentation rule for a usetype.

        ``GET /usetype-presentations/{usetype:path}`` — UsetypePresentationService.get_presentation

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/usetype-presentations/{usetype:path}",
            path={"usetype": usetype},
            response=UsetypePresentationResponse,
        )

    def list_presentations(
        self,
    ) -> list[UsetypePresentationResponse]:
        """List all usetype presentation rules.

        ``GET /usetype-presentations/`` — UsetypePresentationService.list_presentations
        """
        return self._call(
            "GET",
            "/usetype-presentations/",
            response=list[UsetypePresentationResponse],
        )

    def update_presentation(
        self,
        usetype: str,
        request: UsetypePresentationUpdate,
    ) -> UsetypePresentationResponse:
        """Update fields on an existing presentation rule. Unspecified fields are kept.

        ``PUT /usetype-presentations/{usetype:path}`` — UsetypePresentationService.update_presentation

        Raises on 404 (server: LookupError).
        Raises on 422 (server: ValueError).
        """
        return self._call(
            "PUT",
            "/usetype-presentations/{usetype:path}",
            path={"usetype": usetype},
            body=request,
            response=UsetypePresentationResponse,
        )

    def expand_children(
        self,
        document_id: int,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> list[ViewChildStub]:
        """Lazy-load additional children

        ``GET /view/{document_id}/expand-children`` — ViewService.expand_children

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/view/{document_id}/expand-children",
            path={"document_id": document_id},
            query={"offset": offset, "limit": limit},
            response=list[ViewChildStub],
        )

    def get_back_references(
        self,
        document_id: int,
        *,
        limit: int = 50,
    ) -> BackReferenceResponse:
        """Documents whose links or triples target this document

        ``GET /view/back-references/{document_id}`` — ViewService.get_back_references

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/view/back-references/{document_id}",
            path={"document_id": document_id},
            query={"limit": limit},
            response=BackReferenceResponse,
        )

    def get_breadcrumbs(
        self,
        document_id: int,
    ) -> BreadcrumbResponse:
        """Ancestor chain for a document, root-first

        ``GET /view/breadcrumbs/{document_id}`` — ViewService.get_breadcrumbs

        Raises on 404 (server: LookupError).
        """
        return self._call(
            "GET",
            "/view/breadcrumbs/{document_id}",
            path={"document_id": document_id},
            response=BreadcrumbResponse,
        )

    def view_document(
        self,
        document_id: int,
        *,
        include: Optional[str] = None,
        limit_children: int = 20,
        link_direction: str = "both",
    ) -> ViewResponse:
        """Render-ready view of a document for direct human/agent reading

        ``GET /view/{document_id}`` — ViewService.view_document

        Raises on 404 (server: LookupError).
        Raises on 422 (server: InvalidIncludeError).
        """
        return self._call(
            "GET",
            "/view/{document_id}",
            path={"document_id": document_id},
            query={
                "include": include,
                "limit_children": limit_children,
                "link_direction": link_direction,
            },
            response=ViewResponse,
        )
