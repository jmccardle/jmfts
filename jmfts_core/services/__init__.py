"""Service layer — transport-neutral JMFTS operations.

A service holds a ``Session`` and exposes the domain verbs. Methods decorated with
``@expose`` (see ``jmfts_core.registry``) are served over REST by a generated adapter
*and* callable directly in-process by Tau. Services raise domain exceptions
(``ValueError`` etc.); mapping those to HTTP status codes is the REST adapter's job,
declared in each ``@expose(errors=...)``.
"""

from jmfts_core.services.access_service import AccessService
from jmfts_core.services.conversation_service import ConversationService
from jmfts_core.services.document_service import DocumentService
from jmfts_core.services.graph_service import GraphService
from jmfts_core.services.index_service import IndexService
from jmfts_core.services.ingest_service import IngestService
from jmfts_core.services.search_context_service import SearchContextService
from jmfts_core.services.search_service import SearchService
from jmfts_core.services.template_service import TemplateService
from jmfts_core.services.triple_service import TripleService
from jmfts_core.services.usetype_presentation_service import UsetypePresentationService
from jmfts_core.services.view_service import ViewService

__all__ = [
    "AccessService",
    "ConversationService",
    "DocumentService",
    "GraphService",
    "IndexService",
    "IngestService",
    "SearchContextService",
    "SearchService",
    "TemplateService",
    "TripleService",
    "UsetypePresentationService",
    "ViewService",
]
