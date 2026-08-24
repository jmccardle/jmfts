"""RdfService — the triple store, said out loud in somebody else's vocabulary.

``docs/SPRINT_0_3_0.md`` 5.3. One operation: export the live graph as Turtle.

**Why this is a route and not only a library function.** 5.3's argument for building the
serialiser early is that it is how the extractor's output gets REVIEWED — "a wall of
integer foreign keys is unreadable; twenty lines of Turtle is not". A reviewer is a person
at a terminal, and a person at a terminal has ``curl``, not a Python REPL with a session
open.

The path is ``/rdf/turtle`` and not ``/triples/turtle`` for a mechanical reason worth
recording: ``TripleService`` mounts ``GET /triples/{triple_id}``, and FastAPI matches in
registration order, so a second service adding ``/triples/turtle`` afterwards would be
shadowed by it and answer 422 on ``turtle`` not being an integer. ``TripleService`` keeps
``/query`` and ``/path`` ahead of ``/{triple_id}`` inside one class body for that reason;
across two classes the ordering is the import order of ``services/__init__.py``, which is
too long a piece of string to hang a route on.

The response is JSON with the Turtle inside it rather than a ``text/turtle`` response
body. That is the honest shape here: the counts in ``TurtleExportResponse`` are not
decoration — an export is bounded, and the document itself has no way to say whether the
graph ended or the page ran out.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from jmfts_client.contracts.rdf import TurtleExportResponse
from jmfts_core.registry import expose, register_service
from jmfts_core.rdf import RdfStackNotInstalled
from jmfts_core.rdf.names import DEFAULT_BASE_IRI


@register_service
class RdfService:
    """Turtle out — the triple store serialised in RDF."""

    def __init__(self, session: Session):
        self.session = session

    @expose(
        "GET",
        "/rdf/turtle",
        response_model=TurtleExportResponse,
        errors={RdfStackNotInstalled: 501, ValueError: 422},
        tags=["rdf"],
        summary="Export the live triples as a Turtle document",
    )
    def export_turtle(
        self,
        *,
        entity_id: Optional[int] = None,
        predicate_id: Optional[int] = None,
        predicate: Optional[str] = None,
        direction: str = "both",
        coreferent: bool = False,
        provenance: str = "any",
        limit: int = 200,
        offset: int = 0,
        base_iri: str = DEFAULT_BASE_IRI,
        include_labels: bool = True,
    ) -> TurtleExportResponse:
        """Export the live triples as a Turtle document.

        The filters mean what they mean on ``GET /triples/query``, with two additions and
        one subtraction.

        ``coreferent=true`` with an ``entity_id`` unions the facts recorded under every
        alias in the entity's ``same_as`` cluster and writes the cluster out as
        ``owl:sameAs``. The cluster is reported, never collapsed — rewriting aliases to a
        canonical id would read more tidily and would destroy the only evidence that two
        nodes were ever separate.

        ``provenance`` splits the asserted layer from a derived one (4.2): ``asserted`` is
        ``derived_by IS NULL``, ``derived`` its complement, ``any`` both. An unrecognised
        value is a 422, not a silent widening.

        The subtraction is ``include_invalidated``, which this operation does not have.
        Turtle asserts its triples and the 5.1 subset cannot say "retracted", so an export
        is always of the LIVE graph; the history is reachable through ``GET
        /triples/query``, whose JSON has a field to put the answer in.

        Answers 501 where this install has no ``rdflib``: holding triples without being
        able to speak Turtle is a supported deployment (``jmfts_core/rdf/__init__.py``),
        so it is "this server does not implement that", not a fault in the request.
        """
        from jmfts_core.rdf.serialize import triples_to_turtle

        export = triples_to_turtle(
            self.session,
            entity_id=entity_id,
            predicate_id=predicate_id,
            predicate_name=predicate,
            direction=direction,
            coreferent=coreferent,
            provenance=provenance,
            limit=limit,
            offset=offset,
            base_iri=base_iri,
            include_labels=include_labels,
        )
        return TurtleExportResponse(
            turtle=export.turtle,
            triple_count=export.triple_count,
            entity_ids=export.entity_ids,
            same_as_count=export.same_as_count,
        )
