"""Client-side exceptions, one per HTTP status the server actually maps to.

The server's ``@expose`` specs map DOMAIN exception types to statuses — ``ValueError``
to 400, ``DocumentNotFoundError`` to 404, and so on. A thin client cannot import those
types: they live in ``jmfts_core``, which is the whole server stack. So the mapping is
inverted here, status back to a client exception, and the generated verb docstrings name
the domain exceptions that produce each status.

That inversion loses information, and it loses it in one specific way: two domain
exceptions mapped to the same status arrive here as the same class. ``detail`` carries
the server's message, which is what distinguishes them. Do not add a second layer that
guesses the domain type back out of that string.
"""

from __future__ import annotations

from typing import Any


class JmftsError(Exception):
    """Base for every error this client raises."""


class JmftsTransportError(JmftsError):
    """The request never produced an HTTP response — DNS, connect, timeout, TLS."""


class JmftsResponseError(JmftsError):
    """The server answered with a status this client treats as a failure."""

    def __init__(self, status_code: int, detail: Any, url: str) -> None:
        self.status_code = status_code
        #: The server's ``detail`` field, verbatim. Usually ``str``; the embed route
        #: answers with a dict, so this is deliberately not narrowed to text.
        self.detail = detail
        self.url = url
        super().__init__(f"HTTP {status_code} from {url}: {detail}")


class JmftsBadRequest(JmftsResponseError):
    """400 — the server refused the request as invalid."""


class JmftsUnauthorized(JmftsResponseError):
    """401 — no token, or a token the server does not accept."""


class JmftsForbidden(JmftsResponseError):
    """403 — authenticated, but not permitted on this document or subtree."""


class JmftsNotFound(JmftsResponseError):
    """404 — the addressed object does not exist."""


class JmftsConflict(JmftsResponseError):
    """409 — the request collided with existing state (duplicate, lock, race)."""


class JmftsUnprocessable(JmftsResponseError):
    """422 — the body parsed but failed validation."""


class JmftsServerError(JmftsResponseError):
    """5xx — the server failed. Not a client mistake; safe to report upward as-is.

    **501 is the exception to "the server failed", and it can arrive from any
    operation.** A JMFTS install may be built without the model stack or without the
    office readers — both are supported deployments, not broken ones — and an operation
    that needs one it does not have answers 501 with a message naming the missing extra.
    That is stable: the same call will answer 501 again, so retrying is wasted and the
    fix is on the server. No per-verb docstring mentions it, because it is true of every
    verb rather than of any one of them.
    """


#: Status → exception. Anything unlisted becomes :class:`JmftsResponseError` itself,
#: which is not a fallback that hides the code: the status stays on the exception.
STATUS_EXCEPTIONS: dict[int, type[JmftsResponseError]] = {
    400: JmftsBadRequest,
    401: JmftsUnauthorized,
    403: JmftsForbidden,
    404: JmftsNotFound,
    409: JmftsConflict,
    422: JmftsUnprocessable,
}


def exception_for(status_code: int, detail: Any, url: str) -> JmftsResponseError:
    """Build the exception that represents ``status_code``."""
    cls: type[JmftsResponseError]
    if status_code >= 500:
        cls = JmftsServerError
    else:
        cls = STATUS_EXCEPTIONS.get(status_code, JmftsResponseError)
    return cls(status_code, detail, url)
