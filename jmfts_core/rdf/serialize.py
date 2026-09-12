"""Turtle out. ``docs/SPRINT_0_3_0.md`` 5.3.

**This exists to be read, not to interoperate.** 5.3 is explicit about it: a wall of
integer foreign keys is unreadable and twenty lines of Turtle is not, so the serialiser
lands before the importer and before the extractor that will need reviewing. Everything
below that looks like a convenience — the ``rdfs:label`` on every node, the bound
prefixes, the ``:`` separator in ``names.py`` — is there because the output has a reader.

Three decisions worth stating, because each of them is a refusal:

**Invalidated facts are never emitted, and there is no flag to ask for them.** A Turtle
document asserts its triples; the subset in 5.1 has no way to say "this one was
retracted", so writing a superseded fact out would be asserting something the store
believes is false. An export is therefore always of the live graph. The bitemporal history
is real and is reachable — through ``GET /triples/query``, which can say
``include_invalidated`` because JSON has a field to put the answer in.

**A ``same_as`` cluster is reported, not collapsed.** ``resolve_coreferent_ids``
(``graph_analysis.py``) hands back an entity together with its aliases; the export unions
their facts and emits ``owl:sameAs`` between them. Rewriting every alias to a canonical id
would be tidier to read and would destroy the only evidence that two nodes were ever
separate — which is the same argument ``SAME_AS_LINK_TYPE`` already makes for the edge
being a link rather than a merge.

**The ``derived_by`` split is a parameter with no default that widens.**
``provenance="asserted"`` is ``derived_by IS NULL`` (4.2). It is passed straight through to
``TripleRepository.query_triples``, which raises on an unrecognised value rather than
reading it as "any" — a filter that silently widens is how an underived layer's guarantee
gets lost.

Subtree access control is the repository's, not this module's: ``query_triples`` already
drops any triple whose subject or object the current principal cannot read. The one thing
this module has to do for itself is apply the same rule to ``same_as`` cluster members,
because those arrive from the graph walk rather than from a triple.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from jmfts_core.access import readable_id_subset
from jmfts_core.rdf import require_rdflib
from jmfts_core.rdf.names import (
    DEFAULT_BASE_IRI,
    DOCUMENT_PREFIX,
    PREDICATE_PREFIX,
    STANDARD_PREFIXES,
    document_iri,
    document_namespace,
    predicate_namespace,
)


class TurtleSerializationError(ValueError):
    """A stored row cannot be written as RDF without saying something untrue.

    A ``ValueError``, so the ``@expose`` layer maps it to a 422 the way every other
    malformed-input error on this surface is mapped. It names the triple, because the
    caller's next move is to look at that row.
    """


@dataclass
class TurtleExport:
    """What one export produced, plus what it was an export OF.

    The counts are not decoration. An export is bounded by ``limit`` and the reader has no
    way to tell a graph that ended from a page that ran out, so ``triple_count`` beside the
    limit the caller passed is the difference between "these are the facts" and "these are
    the first fifty".
    """

    turtle: str
    triple_count: int
    #: Every entity whose facts were asked for, after coreference expansion. The anchor is
    #: first, as ``resolve_coreferent_ids`` returns it. Empty for an unfiltered export.
    entity_ids: list[int] = field(default_factory=list)
    #: How many ``owl:sameAs`` statements were written. Zero unless ``coreferent`` was on
    #: and the anchor actually had aliases.
    same_as_count: int = 0


def _label_map(session: Session, document_ids: set[int]) -> dict[int, str]:
    """``{document id: title}`` for the nodes about to be named in the output."""
    if not document_ids:
        return {}
    from jmfts_core.models.document import Document

    rows = session.execute(
        select(Document.id, Document.title).where(Document.id.in_(document_ids))
    ).all()
    return {doc_id: title for doc_id, title in rows if title}


def triples_to_turtle(
    session: Session,
    *,
    entity_id: Optional[int] = None,
    predicate_id: Optional[int] = None,
    predicate_name: Optional[str] = None,
    direction: str = "both",
    coreferent: bool = False,
    provenance: str = "any",
    limit: int = 200,
    offset: int = 0,
    base_iri: str = DEFAULT_BASE_IRI,
    include_labels: bool = True,
) -> TurtleExport:
    """Serialise the live triples matching these filters as one Turtle document.

    ``entity_id`` / ``predicate_id`` / ``predicate_name`` / ``direction`` / ``limit`` /
    ``offset`` mean what they mean on ``GET /triples/query``; ``coreferent`` and
    ``provenance`` are the two the module docstring explains. ``include_labels`` adds an
    ``rdfs:label`` per document node, which is what makes the output legible and is the
    reason 5.3 puts this step before the extractor.

    Raises:
        RdfStackNotInstalled: this install has no ``rdflib``.
        TurtleSerializationError: a row cannot be written as RDF honestly.
        ValueError: ``provenance`` is not one of any/asserted/derived (from the repository).
    """
    rdflib = require_rdflib()
    from jmfts_core.repositories.triple import TripleRepository

    # ONE row→RDF mapping for the exporter and the validator, which is Block A step 1's
    # leftover: a validator that read a row differently from the exporter would validate a
    # graph nobody can export. It lives in `rdf/shacl.py` because that is the module that
    # needs it per triple; imported HERE rather than at module scope because `rdf/shacl.py`
    # imports `TurtleSerializationError` from this module, and a module-scope import in both
    # directions is a cycle whose resolution would depend on which one Python loaded first.
    from jmfts_core.rdf.shacl import triple_terms

    entity_ids: Optional[list[int]] = None
    if coreferent and entity_id is not None:
        from jmfts_core.graph_analysis import resolve_coreferent_ids

        entity_ids = resolve_coreferent_ids(session, entity_id)
        # The cluster comes from a graph walk, not from a triple, so the repository's
        # subtree filter has not seen it. An alias the caller cannot read must not be
        # named in the output — an owl:sameAs to a hidden node discloses it exists.
        readable = readable_id_subset(session, set(entity_ids))
        entity_ids = [eid for eid in entity_ids if eid in readable]

    repo = TripleRepository(session)
    triples = repo.query_triples(
        entity_id=entity_id,
        entity_ids=entity_ids,
        predicate_id=predicate_id,
        predicate_name=predicate_name,
        direction=direction,
        limit=limit,
        offset=offset,
        # An export is of the live graph; see the module docstring.
        include_invalidated=False,
        provenance=provenance,
    )

    graph = rdflib.Graph()
    for prefix, namespace in STANDARD_PREFIXES.items():
        graph.bind(prefix, rdflib.Namespace(namespace))
    # Two bindings, not one on the base IRI: rdflib will not use a prefix that stops short
    # of where it would split the IRI itself, so `jmfts: <urn:jmfts:>` produces
    # `<urn:jmfts:document:42>` on every line and these produce `doc:42`. See
    # `rdf/names.py`, which measures it.
    graph.bind(DOCUMENT_PREFIX, rdflib.Namespace(document_namespace(base_iri)))
    graph.bind(PREDICATE_PREFIX, rdflib.Namespace(predicate_namespace(base_iri)))

    rdfs_label = rdflib.URIRef(STANDARD_PREFIXES["rdfs"] + "label")
    owl_same_as = rdflib.URIRef(STANDARD_PREFIXES["owl"] + "sameAs")

    named_documents: set[int] = set()
    for triple in triples:
        # Every refusal this loop used to make is still made, one level down and by one
        # implementation: a predicate IRI or an `object_datatype` that is a CURIE or a
        # relative reference raises `TurtleSerializationError` out of `triple_terms`.
        graph.add(triple_terms(rdflib, triple, base_iri))
        named_documents.add(triple.subject_id)
        if triple.object_id is not None:
            named_documents.add(triple.object_id)

    # Facts only. The owl:sameAs statements below and the rdfs:labels after them are the
    # export's own commentary on the facts, and counting them here would make "50 triples
    # against a limit of 50" — the reader's only signal that a page ran out — wrong.
    triple_count = len(graph)

    same_as_count = 0
    if entity_ids and len(entity_ids) > 1:
        anchor = rdflib.URIRef(document_iri(entity_ids[0], base_iri))
        named_documents.add(entity_ids[0])
        for alias in entity_ids[1:]:
            graph.add((anchor, owl_same_as, rdflib.URIRef(document_iri(alias, base_iri))))
            named_documents.add(alias)
            same_as_count += 1

    if include_labels:
        for doc_id, title in _label_map(session, named_documents).items():
            graph.add(
                (
                    rdflib.URIRef(document_iri(doc_id, base_iri)),
                    rdfs_label,
                    rdflib.Literal(title),
                )
            )

    return TurtleExport(
        turtle=graph.serialize(format="turtle"),
        triple_count=triple_count,
        entity_ids=list(entity_ids or ([entity_id] if entity_id is not None else [])),
        same_as_count=same_as_count,
    )
