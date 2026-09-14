"""The one response shape that is not JSON.

``BinaryPayload`` is what a service method returns when its ``@expose`` declares a
``media_type``. Both clients hand back this same object: ``LocalJmftsClient`` gets it from
the method directly, and ``RemoteJmftsClient`` rebuilds it from the HTTP response. A caller
holding one does not know which transport produced it, which is the property every other
contract in this package already has and which a bare ``bytes`` return would lose — the
media type and the filename are part of the answer, not metadata about it.

**Why the media type is on the payload and not only on the spec.** ``GET /documents/{id}/blob``
serves whatever was uploaded, so its content type is a property of the row
(``file.detected_mime``, written at ``services/ingest_service.py:788``) and not of the route.
The spec's ``media_type`` is what OpenAPI declares — ``application/octet-stream`` for that
route, because the document cannot promise more — and this field is what actually went on
the wire. For ``/image`` the two agree; the shape does not depend on their agreeing.

This module imports neither ``jmfts_core`` nor a web framework, per the rule
``tests/test_client_codegen.py::test_client_package_does_not_import_the_server`` enforces.
Turning one of these into an HTTP response is the adapter's job and lives in
``jmfts_core/rest/wiring.py``.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class BinaryPayload(BaseModel):
    """Bytes, what they are, and what to call them if they are saved."""

    model_config = ConfigDict(frozen=True)

    content: bytes = Field(..., description="The response body, verbatim")
    media_type: str = Field(..., description="What these bytes are, e.g. image/png")

    #: The name a browser should save this under. ``None`` means the route has no opinion,
    #: which is the right answer for a rendered page: it is derived from a document rather
    #: than being one, and naming it invites somebody to treat it as the source.
    filename: Optional[str] = Field(default=None)

    #: ``True`` renders ``Content-Disposition: attachment`` and makes a browser download
    #: rather than display. Only ``/blob`` sets it — that route re-hosts the original upload,
    #: and the reason it exists is that somebody wants the file back.
    download: bool = Field(default=False)

    def __len__(self) -> int:
        """Byte count, so ``len(payload)`` reads the way the caller expects."""
        return len(self.content)
