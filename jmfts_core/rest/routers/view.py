"""Direct-readability /view/{id} API Router — route-less stub.

All `/view/*` routes are now generated from the `@expose` registry over
`jmfts_core/services/view_service.py::ViewService`. The hand-written routes were deleted;
the response models moved to `jmfts-client/jmfts_client/contracts/view.py` (re-exported from
`api/schemas.py`). No document is serialised through `DocumentResponse` on this surface,
so there was no local converter to fold in.

This module is kept (route-less) so historical imports resolve; it is no longer included
by `api/main.py`. See `docs/API_UNIFICATION_CONTRACT_NOTES.md`.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/view", tags=["view"])
