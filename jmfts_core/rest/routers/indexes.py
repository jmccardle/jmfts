"""Index Management API Router — route-less stub.

All `/indexes/*` routes are now generated from the `@expose` registry over
`jmfts_core/services/index_service.py::IndexService`. The hand-written routes and the
local `index_to_response` converter were deleted; the ORM→response mapping is single-
sourced as `IndexResponse.from_index` in `jmfts-client/jmfts_client/contracts/index.py`.

This module is kept (route-less) so historical imports resolve; it is no longer included
by `api/main.py`. See `docs/API_UNIFICATION_CONTRACT_NOTES.md`.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/indexes", tags=["indexes"])
