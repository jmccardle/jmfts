"""Turtle in. ``docs/SPRINT_0_3_0.md`` 5.1 and 5.2.

**The subset is a ceiling, not a starting point.** 5.1 names nine terms — ``sh:NodeShape``,
``sh:targetClass``, ``sh:property``, ``sh:path``, ``sh:minCount``, ``sh:maxCount``,
``sh:datatype``, ``sh:in``, ``sh:class`` — and that is what this reads. Everything else in
the document is stored (``ontologies.source_turtle`` holds the bytes verbatim) and
REPORTED as unsupported. It is not read, not enforced, and not quietly approximated.

The reporting is the whole point of the ceiling. A parser that ignored ``sh:pattern``
would give a caller a stored shape that passes on data the shape forbids — a validator
that is right about eight constraints and silent about the ninth is worse than no
validator, because the silence looks like a pass. So the unread terms come back by name.

**No reasoner, and none of the two OWL predicates 5.4 admits are inferred here.**
``owl:InverseFunctionalProperty`` and ``owl:equivalentProperty`` are structural and are
implementable as SQL when Part 7 needs them; storing an axiom as a triple is not the same
as acting on it, and this module does neither — it reads shapes. The axioms are in
``source_turtle`` waiting for the step that uses them.

**What a "predicate" is, on the way in.** Every ``sh:path`` in a shape, plus anything the
document types as ``rdf:Property``, ``owl:ObjectProperty`` or ``owl:DatatypeProperty``.
Those are the terms a bound shape can constrain an extractor to (8.2), so those are the
terms that need a row in ``predicates`` for a triple to be able to reference one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from jmfts_core.rdf import require_rdflib
from jmfts_core.rdf.names import STANDARD_PREFIXES, iri_problem, local_name

SHACL = STANDARD_PREFIXES["sh"]
OWL = STANDARD_PREFIXES["owl"]
RDF = STANDARD_PREFIXES["rdf"]

#: The nine terms 5.1 admits, as full IRIs. Named here rather than inline so the ceiling is
#: one list a reader can check against the spec, and so :func:`_unsupported_terms` can
#: subtract it from what the document actually used.
SUPPORTED_SHACL_TERMS: frozenset[str] = frozenset(
    SHACL + name
    for name in (
        "NodeShape",
        "targetClass",
        "property",
        "path",
        "minCount",
        "maxCount",
        "datatype",
        "in",
        "class",
    )
)

#: Terms that are recognised but deliberately do not become a constraint. ``sh:name`` and
#: ``sh:description`` are documentation of a property shape, and reading them is not
#: widening the ceiling — neither one can make a shape accept or reject anything.
ANNOTATION_TERMS: frozenset[str] = frozenset((SHACL + "name", SHACL + "description"))

#: What makes a term a predicate this appliance should carry a row for. See the module
#: docstring.
PROPERTY_TYPES: frozenset[str] = frozenset(
    (RDF + "Property", OWL + "ObjectProperty", OWL + "DatatypeProperty")
)


class TurtleParseError(ValueError):
    """The uploaded bytes are not Turtle this appliance can read.

    A ``ValueError``, so ``@expose`` maps it to 422 with every other malformed-input error
    on this surface. It carries rdflib's own message, because "line 7: expected '.'" is
    what the uploader needs and nothing this module could write is better.
    """


@dataclass
class ParsedProperty:
    """One ``sh:property`` node, reduced to the subset."""

    path: str
    min_count: Optional[int] = None
    max_count: Optional[int] = None
    datatype: Optional[str] = None
    class_iri: Optional[str] = None
    allowed_values: Optional[list[str]] = None
    name: Optional[str] = None
    description: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "min_count": self.min_count,
            "max_count": self.max_count,
            "datatype": self.datatype,
            "class_iri": self.class_iri,
            "allowed_values": self.allowed_values,
            "name": self.name,
            "description": self.description,
        }


@dataclass
class ParsedShape:
    """One ``sh:NodeShape``."""

    iri: str
    target_class: Optional[str] = None
    properties: list[ParsedProperty] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "iri": self.iri,
            "target_class": self.target_class,
            "properties": [p.to_dict() for p in self.properties],
        }


@dataclass
class ParsedOntology:
    """Everything the importer needs out of one uploaded document."""

    #: ``{prefix: namespace IRI}`` as the document declared them, so the vocabulary can be
    #: rendered back out in the names its author chose.
    prefixes: dict[str, str] = field(default_factory=dict)
    shapes: list[ParsedShape] = field(default_factory=list)
    #: ``{predicate IRI: local name}`` for every term a bound shape could constrain.
    predicate_terms: dict[str, str] = field(default_factory=dict)
    #: SHACL/OWL terms the document used that the subset does not read. Sorted, so two
    #: uploads of the same bytes report the same list.
    unsupported_terms: list[str] = field(default_factory=list)


def _one(graph, subject, predicate):
    """The single object of ``(subject, predicate, ?)``, or None.

    ``graph.value`` with ``any=False`` raises when a subject carries the same predicate
    twice, which is exactly right here: two ``sh:minCount``s on one property shape is a
    contradiction, and picking one of them silently is how a shape ends up enforcing
    something nobody wrote.
    """
    return graph.value(subject, predicate, any=False)


def _int(value) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _iri(value, what: str) -> Optional[str]:
    """A term that must be an IRI, checked. See ``names.iri_problem``."""
    if value is None:
        return None
    text = str(value)
    problem = iri_problem(text)
    if problem is not None:
        raise TurtleParseError(f"{what} is {text!r}, which cannot be an IRI: {problem}.")
    return text


def _text(value) -> Optional[str]:
    return None if value is None else str(value)


def parse_ontology(turtle: str) -> ParsedOntology:
    """Read one Turtle document down to the subset in 5.1.

    Raises:
        RdfStackNotInstalled: this install has no ``rdflib``.
        TurtleParseError: the bytes are not parseable Turtle, or a shape says something
            self-contradictory (the same constraint twice with different values).
    """
    rdflib = require_rdflib()
    graph = rdflib.Graph()
    try:
        graph.parse(data=turtle, format="turtle")
    except Exception as exc:  # rdflib raises several unrelated types for a bad document
        raise TurtleParseError(f"could not parse the document as Turtle: {exc}") from exc

    sh = rdflib.Namespace(SHACL)
    rdf_type = rdflib.RDF.type

    parsed = ParsedOntology()
    # rdflib binds a table of its own into every fresh Graph, so the document's OWN prefixes
    # are what is left after subtracting an empty graph's. Storing rdflib's defaults instead
    # would make two uploads of unrelated vocabularies report near-identical prefix maps,
    # and the reason `ontologies.prefixes` exists is to render Turtle back out in the names
    # THIS author chose.
    builtin = {(str(p), str(n)) for p, n in rdflib.Graph().namespaces()}
    for prefix, namespace in graph.namespaces():
        if (str(prefix), str(namespace)) not in builtin:
            parsed.prefixes[str(prefix)] = str(namespace)

    try:
        for shape_node in graph.subjects(rdf_type, sh.NodeShape):
            if isinstance(shape_node, rdflib.BNode):
                # A shape with no IRI cannot be BOUND: shape_bindings.shape_iri is how a
                # binding names one (models/ontology.py), and a blank node's id is an
                # artefact of this parse rather than a name the document gave it.
                raise TurtleParseError(
                    "a sh:NodeShape was declared as a blank node. A shape has to be named "
                    "with an IRI to be bound to a scope."
                )
            shape = ParsedShape(
                iri=str(shape_node),
                target_class=_iri(_one(graph, shape_node, sh.targetClass), "sh:targetClass"),
            )
            for prop_node in graph.objects(shape_node, sh.property):
                shape.properties.append(_parse_property(rdflib, graph, sh, prop_node))
            shape.properties.sort(key=lambda p: p.path)
            parsed.shapes.append(shape)
    except TurtleParseError:
        raise
    except Exception as exc:
        # rdflib's UniquenessError (the `any=False` contract: one subject, one value for
        # this constraint) and int() on a non-numeric sh:minCount both land here. Both mean
        # the shape says something no validator could act on, and both are the uploader's
        # to fix.
        raise TurtleParseError(f"a shape in this document is not readable: {exc}") from exc

    parsed.shapes.sort(key=lambda s: s.iri)

    for shape in parsed.shapes:
        for prop in shape.properties:
            parsed.predicate_terms.setdefault(prop.path, local_name(prop.path))
    for property_type in PROPERTY_TYPES:
        for term in graph.subjects(rdf_type, rdflib.URIRef(property_type)):
            if isinstance(term, rdflib.BNode):
                continue
            parsed.predicate_terms.setdefault(str(term), local_name(str(term)))

    parsed.unsupported_terms = _unsupported_terms(graph)
    return parsed


def _parse_property(rdflib, graph, sh, node) -> ParsedProperty:
    """One ``sh:property`` object node."""
    path = _one(graph, node, sh.path)
    if path is None:
        raise TurtleParseError("a sh:property node declares no sh:path, so it constrains nothing.")
    if isinstance(path, rdflib.BNode):
        # SHACL property paths (alternative, inverse, sequence) are blank-node structures.
        # 5.1 admits `sh:path` as a predicate IRI and nothing else; a path expression read
        # as if it were an IRI would silently constrain the wrong property.
        raise TurtleParseError(
            "a sh:path is a property-path expression rather than a predicate IRI. The "
            "subset in SPRINT_0_3_0.md 5.1 reads a plain IRI path only."
        )
    allowed = None
    in_list = _one(graph, node, sh["in"])
    if in_list is not None:
        allowed = [str(item) for item in graph.items(in_list)]
    return ParsedProperty(
        path=_iri(path, "sh:path"),
        min_count=_int(_one(graph, node, sh.minCount)),
        max_count=_int(_one(graph, node, sh.maxCount)),
        datatype=_iri(_one(graph, node, sh.datatype), "sh:datatype"),
        class_iri=_iri(_one(graph, node, sh["class"]), "sh:class"),
        allowed_values=allowed,
        name=_text(_one(graph, node, sh.name)),
        description=_text(_one(graph, node, sh.description)),
    )


def _unsupported_terms(graph) -> list[str]:
    """SHACL and OWL terms the document used that the subset does not read.

    Only the SHACL and OWL namespaces, and only where the term appears as a PREDICATE or as
    the object of ``rdf:type`` — those are the two positions where a term makes a claim
    that a validator would have to honour. A term appearing anywhere else is data.
    """
    used: set[str] = set()
    rdf_type = RDF + "type"
    for _subject, predicate, obj in graph:
        term = str(predicate)
        if term.startswith(SHACL) or term.startswith(OWL):
            used.add(term)
        if term == rdf_type:
            obj_term = str(obj)
            if obj_term.startswith(SHACL) or obj_term.startswith(OWL):
                used.add(obj_term)
    return sorted(used - SUPPORTED_SHACL_TERMS - ANNOTATION_TERMS - PROPERTY_TYPES)
