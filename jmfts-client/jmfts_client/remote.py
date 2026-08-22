"""``RemoteJmftsClient`` — the HTTP transport for the ``@expose`` verb surface.

This is the sibling ``jmfts_core/client.py`` names as deferred. Same one definition, third
generated view::

    REGISTRY ──> REST routes ──> OpenAPI document
       │              └────────> RemoteJmftsClient  (this package, generated)
       └────────────────────────> LocalJmftsClient   (jmfts_core, generated at import)

Where it deliberately differs from ``LocalJmftsClient``
------------------------------------------------------
``LocalJmftsClient.unit_of_work()`` yields a bound client whose verbs share one
transaction, so several writes commit or roll back together. **This client offers no such
method, and that is the honest choice.** Each HTTP request is its own transaction on the
server; a ``unit_of_work`` here could group the calls but could not roll back the earlier
ones when a later one failed. Offering the name without the guarantee would be worse than
not offering it, so a caller that needs atomicity across several writes must either run
in-process with ``LocalJmftsClient`` or ask for a server-side batch operation.
"""

from __future__ import annotations

from jmfts_client._verbs import _GeneratedVerbs


class RemoteJmftsClient(_GeneratedVerbs):
    """Call a JMFTS appliance over HTTP.

    ::

        from jmfts_client import RemoteJmftsClient
        from jmfts_client.contracts import DocumentCreate

        with RemoteJmftsClient("http://localhost:8100", token="...") as jmfts:
            doc = jmfts.create_document(DocumentCreate(title="Ada", content="Ada Lovelace"))
            hits = jmfts.hybrid_search(HybridSearchRequest(query="Ada"))

    Every verb is generated from the server's route table, so this class gains an
    operation the moment the appliance does. It carries no hand-written method: see
    ``jmfts_client/_verbs.py`` for the table and ``jmfts_client/transport.py`` for the one
    request path they all share.
    """
