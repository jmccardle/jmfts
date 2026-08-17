"""Per-request principal, carried in a contextvar.

The HTTP auth layer (``api/auth.py``) resolves the bearer token to a principal and
binds it here for the duration of the request; repository/query code reads it via
``get_current_principal()`` when building access-control predicates (see
``jmfts_core/access.py``).

In-process callers — ingestion, ``unit_of_work()``, scripts, the benchmark harness —
never bind one, so they observe ``None`` → owner-equivalent (no filtering). That is
what keeps the default/benchmark path byte-identical: enforcement engages ONLY for a
bound, non-owner principal.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CurrentPrincipal:
    """A resolved identity for the current request.

    ``id`` is the ``principals.id`` PK, or ``None`` for the synthetic owner (which
    has no row). ``is_owner`` short-circuits every access check.
    """

    id: Optional[int]
    name: str
    is_owner: bool = False


# The synthetic owner: the shared bearer / ephemeral boot token. It bypasses all
# access control and deliberately has NO database row — owner auth stays a
# constant-time token compare that needs no DB (see api/auth.py).
OWNER = CurrentPrincipal(id=None, name="owner", is_owner=True)

_current: ContextVar[Optional[CurrentPrincipal]] = ContextVar(
    "jmfts_current_principal", default=None
)


def set_principal(principal: Optional[CurrentPrincipal]) -> Token:
    """Bind the principal for this context; returns a token to ``reset_principal``."""
    return _current.set(principal)


def reset_principal(token: Token) -> None:
    """Restore the previous binding (call in a ``finally`` to avoid leakage)."""
    _current.reset(token)


def get_current_principal() -> Optional[CurrentPrincipal]:
    """The principal bound to this context, or ``None`` for unbound in-process callers."""
    return _current.get()
