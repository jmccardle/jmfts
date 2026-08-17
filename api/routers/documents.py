"""Document API Router — fully converted to the @expose service layer.

Every route that used to live here (documents CRUD, tree navigation, embedding,
structural split / chunk / segment, RAPTOR + portfolio RAPTOR, fact extraction, and
links — 20 endpoints) is now generated from the ``@expose``-decorated methods on
``jmfts_core.services.document_service.DocumentService`` via
``api/wiring.build_exposed_router()`` and mounted in ``api/main.py``. There is no
hand-written route or local ``doc_to_response`` converter left here — all document
serialisation flows through the single ``DocumentResponse.from_document`` converter.

``tests/test_api_parity.py`` guards that the generated routes and the registry stay in
bijection. This module is intentionally kept (empty of routes) so that historical
imports resolve and the parity test's source scan has a file to read; it is no longer
included by ``api/main.py``.
"""
