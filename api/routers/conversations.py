"""Conversation Ingestion Router — #59 (CONVERTED — route-less stub)

``POST /conversations/ingest`` is now generated from the ``@expose`` registry over
``jmfts_core/services/conversation_service.py::ConversationService.ingest_conversation``.
The hand-written route was deleted; this module is kept only so historical imports resolve.
It is no longer included by ``api/main.py`` — all ``/conversations/*`` routes come from
``build_exposed_router()``.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/conversations", tags=["conversations"])
