"""Prompt Template Library Router — fully converted to the @expose service layer.

Every route that used to live here (GET /templates, GET /templates/{id},
POST /templates, PUT /templates/{id}, POST /templates/{id}/render and
POST /templates/search) is now generated from the ``@expose``-decorated methods on
``jmfts_core.services.template_service.TemplateService`` via
``api/wiring.build_exposed_router()`` and mounted in ``api/main.py``.

There is no hand-written route left here, and no local ``_doc_to_doc_response`` /
``_doc_to_template`` converter: document serialisation flows through the single
``DocumentResponse.from_document`` converter (which added ``position``/``event_time`` to
template-search results), and the template-response mapping lives once as
``TemplateResponse.from_document`` in ``jmfts-client/jmfts_client/contracts/template.py``.

``tests/test_api_parity.py`` guards that the generated routes and the registry stay in
bijection. This module is intentionally kept (empty of routes) so that historical
imports resolve; it is no longer included by ``api/main.py``.
"""
