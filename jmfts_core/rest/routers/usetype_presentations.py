"""Usetype Presentations API Router — route-less stub.

All `/usetype-presentations/*` routes are now generated from the `@expose` registry over
`jmfts_core/services/usetype_presentation_service.py::UsetypePresentationService`. The
hand-written routes were deleted; the ORM→response mapping is single-sourced as
`UsetypePresentationResponse.from_presentation` in
`jmfts-client/jmfts_client/contracts/usetype_presentation.py`.

This module is kept (route-less) so historical imports resolve; it is no longer included
by `api/main.py`. See `docs/API_UNIFICATION_CONTRACT_NOTES.md`.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/usetype-presentations", tags=["usetype-presentations"])
