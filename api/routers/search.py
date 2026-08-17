"""Search API Router — fully converted to the @expose service layer.

Every route that used to live here (POST /search/{vector,fulltext,bm25,maxsim,
synthesize,auto} and GET /search/) is now generated from the ``@expose``-decorated
methods on ``jmfts_core.services.search_service.SearchService`` via
``api/wiring.build_exposed_router()`` and mounted in ``api/main.py``. There is no
hand-written route or local ``doc_to_response`` converter left here — all document
serialisation flows through the single ``DocumentResponse.from_document`` converter,
which is what added ``position``/``event_time`` to these results.

``tests/test_api_parity.py`` guards that the generated routes and the registry stay in
bijection. This module is intentionally kept (empty of routes) so that historical
imports resolve and the parity test's source scan has a file to read; it is no longer
included by ``api/main.py``.
"""
