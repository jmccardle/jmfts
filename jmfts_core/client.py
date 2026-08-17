"""``LocalJmftsClient`` — the embedded, in-process transport for the ``@expose`` surface.

The REST adapter (``api/wiring.py``) turns every ``@expose``'d service method into an
HTTP route. This is the *other* generated transport: the same registry, turned into plain
Python methods on one object, so a co-located caller (Tau) invokes the verb surface with
no HTTP, no serialization, and no double-commit tax — while sharing the process's GPU
embedding singleton and connection pool.

There is deliberately **no hand-written method per verb** here, exactly as there is no
hand-written route in ``wiring.py``. Both transports are generated from
``jmfts_core.registry.REGISTRY``, so they cannot drift from each other or from the service
definitions. ``tests/test_local_client.py`` asserts client↔registry coverage the same way
``tests/test_api_parity.py`` asserts route↔registry bijection.

Sessions
--------
Each standalone verb call runs in its own ``unit_of_work()`` — one pooled connection, one
transaction, committed on success and rolled back on error — mirroring one HTTP request.
The services already return Pydantic responses (detached from the session), so results
outlive the transaction safely.

Composition (the embedded win)
------------------------------
``unit_of_work()`` yields a *bound* client whose verbs all run against one shared session
and therefore one transaction — several writes commit or roll back atomically, which the
REST transport cannot offer without a batch endpoint::

    client = LocalJmftsClient()
    with client.unit_of_work() as tx:
        subj = tx.create_document(DocumentCreate(title="Ada", content="Ada Lovelace"))
        obj = tx.create_document(DocumentCreate(title="England", content="England"))
        tx.create_triple(TripleCreate(subject_id=subj.id, predicate_id=p, object_id=obj.id))
    # all three persist together, or none do

Async verbs (ingest, embed, fact extraction) are generated as ``async def`` methods, so a
coroutine service method stays awaitable through the facade; sync verbs stay sync. A
remote (HTTP) sibling — ``RemoteJmftsClient`` over the generated REST routes — is a
deferred follow-up; the transaction boundary is where the two would honestly diverge.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from typing import Callable, Generator, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# Importing the services package triggers ``@register_service`` on every service, which is
# what populates REGISTRY. Without this the generated method table below would be empty.
import jmfts_core.services  # noqa: F401  (imported for its registration side effect)
from jmfts_core.registry import REGISTRY, ExposeSpec
from jmfts_core.unit_of_work import unit_of_work as _uow


def _make_verb(spec: ExposeSpec) -> Callable:
    """Build one client method that runs ``spec``'s service op.

    In *bound* mode (inside :meth:`LocalJmftsClient.unit_of_work`) the op runs against the
    shared session, so its self-commit is only a savepoint of the enclosing transaction. In
    *standalone* mode it opens its own ``unit_of_work`` — one connection, one transaction.
    """
    func = spec.func
    service_cls = spec.service_cls

    if inspect.iscoroutinefunction(func):

        async def verb(self, *args, **kwargs):
            if self._session is not None:
                return await func(service_cls(self._session), *args, **kwargs)
            with _uow(self._engine) as session:
                return await func(service_cls(session), *args, **kwargs)

    else:

        def verb(self, *args, **kwargs):
            if self._session is not None:
                return func(service_cls(self._session), *args, **kwargs)
            with _uow(self._engine) as session:
                return func(service_cls(session), *args, **kwargs)

    verb.__name__ = func.__name__
    verb.__qualname__ = f"LocalJmftsClient.{func.__name__}"
    verb.__doc__ = func.__doc__
    verb.__wrapped__ = func
    # Present the service method's own signature (minus ``self``) for help()/IDE hints.
    sig = inspect.signature(func)
    verb.__signature__ = sig.replace(
        parameters=[p for name, p in sig.parameters.items() if name != "self"]
    )
    return verb


class LocalJmftsClient:
    """In-process facade exposing every ``@expose``'d verb as a plain method.

    Verb methods (``create_document``, ``hybrid_search``, ``create_triple``,
    ``delete_link``, ``get_neighbors``, …) are generated from the registry at import time;
    see module docstring. Construct once and reuse; it is cheap (it owns no state beyond an
    optional engine override).
    """

    def __init__(self, engine: Optional[Engine] = None):
        """Args:
        engine: connection source for every verb. Defaults to the process engine
            (``get_engine()`` inside ``unit_of_work``); pass an explicit engine to run
            against a different pool/database (tests, or a second appliance).
        """
        self._engine = engine
        # Non-None only for a *bound* client yielded by ``unit_of_work`` — then verbs run on
        # this shared session instead of opening their own transaction.
        self._session: Optional[Session] = None

    @contextmanager
    def unit_of_work(self) -> Generator["LocalJmftsClient", None, None]:
        """Yield a bound client whose verbs share one atomic transaction.

        Every verb called on the yielded client runs against a single session; the whole
        block commits on clean exit and rolls back on any exception. Nesting joins the
        existing unit rather than opening a second transaction.
        """
        if self._session is not None:
            # Already inside a unit — compose into the same transaction.
            yield self
            return
        with _uow(self._engine) as session:
            bound = LocalJmftsClient(self._engine)
            bound._session = session
            yield bound


def _install_verbs(cls: type) -> None:
    """Attach one generated method per registered ``@expose`` op. Idempotent."""
    for spec in REGISTRY:
        setattr(cls, spec.func.__name__, _make_verb(spec))


_install_verbs(LocalJmftsClient)
