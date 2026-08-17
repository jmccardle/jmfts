"""Triples & Predicates API Router — fully converted to the @expose service layer.

Every route that used to live here (predicate CRUD, triple CRUD, ``GET /triples/query``,
``GET /triples/path``, and the invalidate/supersede operations) is now generated from the
``@expose``-decorated methods on ``jmfts_core.services.triple_service.TripleService`` via
``api/wiring.build_exposed_router()`` and mounted in ``api/main.py``. There is no
hand-written route or local ``_doc_to_response`` converter left here — all document
serialisation flows through the single ``DocumentResponse.from_document`` converter, which
is what added ``position``/``event_time`` to ``TripleDetailResponse`` subjects/objects.

``tests/test_api_parity.py`` guards that the generated routes and the registry stay in
bijection. This module is intentionally kept (empty of routes) so that historical imports
resolve; it is no longer included by ``api/main.py``.
"""
