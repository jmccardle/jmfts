"""Graph analytics API Router — fully converted to the @expose service layer.

Every route that used to live here (GET /graph/{centrality,subtree-authority,spines,
communities,diff,stats} and POST /graph/lint) is now generated from the
``@expose``-decorated methods on ``jmfts_core.services.graph_service.GraphService`` via
``api/wiring.build_exposed_router()`` and mounted in ``api/main.py``. There is no
hand-written route left here; the request/response models moved to
``jmfts_core.contracts.graph`` (re-exported from ``api.schemas`` for back-compat).

``tests/test_api_parity.py`` guards that the generated routes and the registry stay in
bijection. This module is intentionally kept (empty of routes) so that historical
imports resolve; it is no longer included by ``api/main.py``.
"""
