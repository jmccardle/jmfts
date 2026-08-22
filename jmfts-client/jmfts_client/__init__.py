"""JMFTS client — the wire contracts, and an HTTP client generated from them.

This distribution is the thin half of JMFTS. It carries ``httpx`` and ``pydantic`` and
nothing else, so a consumer that only needs to CALL an appliance does not install
sqlalchemy, pgvector, transformers or pymupdf to do it.

The server distribution (``jmfts``) depends on this one rather than the other way round.
That direction looks inverted and is deliberate: ``jmfts_client.contracts`` is the single
definition of every request and response shape, and both the in-process
``LocalJmftsClient`` and this ``RemoteJmftsClient`` must validate against the SAME classes.
Shipping a copy to each side would make them equal by value and unequal by ``isinstance``
— a bug that appears only when one process holds both, which is exactly what
``JMFTS_RUNNER_URL`` (one appliance embedding against another over HTTP) does.

Imports here are LAZY, through :pep:`562`. The server imports ``jmfts_client.contracts.*``
in dozens of modules, and an eager ``__init__`` would make every one of those build the
94-method verb table and import ``httpx`` on the way to one Pydantic class. Laziness also
keeps the import graph shallow enough that a contract can never close a cycle back through
the client.
"""

from typing import TYPE_CHECKING

__version__ = "0.2.0"

if TYPE_CHECKING:  # import-time cost avoided at runtime, type checkers still see the names
    from jmfts_client.errors import (
        JmftsBadRequest,
        JmftsConflict,
        JmftsError,
        JmftsForbidden,
        JmftsNotFound,
        JmftsResponseError,
        JmftsServerError,
        JmftsTransportError,
        JmftsUnauthorized,
        JmftsUnprocessable,
    )
    from jmfts_client.remote import RemoteJmftsClient
    from jmfts_client.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT

#: Public name -> the module that defines it.
_LAZY: dict[str, str] = {
    "DEFAULT_BASE_URL": "jmfts_client.transport",
    "DEFAULT_TIMEOUT": "jmfts_client.transport",
    "JmftsBadRequest": "jmfts_client.errors",
    "JmftsConflict": "jmfts_client.errors",
    "JmftsError": "jmfts_client.errors",
    "JmftsForbidden": "jmfts_client.errors",
    "JmftsNotFound": "jmfts_client.errors",
    "JmftsResponseError": "jmfts_client.errors",
    "JmftsServerError": "jmfts_client.errors",
    "JmftsTransportError": "jmfts_client.errors",
    "JmftsUnauthorized": "jmfts_client.errors",
    "JmftsUnprocessable": "jmfts_client.errors",
    "RemoteJmftsClient": "jmfts_client.remote",
}


def __getattr__(name: str):
    """Resolve a public name on first use. Anything unlisted is a real AttributeError."""
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value  # resolved once; later lookups skip this hook
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "JmftsBadRequest",
    "JmftsConflict",
    "JmftsError",
    "JmftsForbidden",
    "JmftsNotFound",
    "JmftsResponseError",
    "JmftsServerError",
    "JmftsTransportError",
    "JmftsUnauthorized",
    "JmftsUnprocessable",
    "RemoteJmftsClient",
    "__version__",
]
