"""Search Contexts API Router — route-less stub.

All `/search-contexts/*` routes are now generated from the `@expose` registry over
`jmfts_core/services/search_context_service.py::SearchContextService`. The hand-written
routes were deleted; the ORM→response mapping is single-sourced as
`SearchContextResponse.from_context` in `jmfts-client/jmfts_client/contracts/search_context.py`.

This module is kept (route-less) so historical imports resolve; it is no longer included
by `api/main.py`. See `docs/API_UNIFICATION_CONTRACT_NOTES.md`.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/search-contexts", tags=["search-contexts"])
