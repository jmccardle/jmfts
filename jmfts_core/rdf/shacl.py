"""The data graph a bound shape is validated against. ``docs/SPRINT_0_5_0.md`` Part 2, Block A
step 1.

A :class:`~jmfts_core.models.ontology.ShapeBinding` names a scope of documents. The data graph
is the live triples whose endpoints are in that scope, **under the caller's access filter**.
Both halves are load-bearing and neither is optional:

**The scope bounds the graph and nothing else does.** A triple is in the graph when every
document it names is in the scope — subject in scope, and object in scope or a literal. The
alternative bound (subject in scope, objects free) puts nodes in the graph that the binding
never named, and then whether a constraint on such a node passes depends on which of its
triples happened to be pulled in with it. That is not a bound; it is a sample. What the tight
bound costs is recorded rather than hidden: every triple it removes is a
:class:`BoundaryCut`, and open question 6.2's answer is built on those.

**The access filter is applied where the scope is resolved, not after the graph is built.**
:func:`resolve_scope_document_ids` runs ``jmfts_core.access.readable_filter`` — the same
predicate ``readable_id_subset`` runs for every triple read (``repositories/triple.py``) — so
the scope a principal gets is already the readable subset, and "both endpoints in scope"
therefore means "both endpoints readable" by construction. A validation run that built its
graph from triples the caller cannot read would be ``SPRINT_0_3_0.md`` 13.9 arrived at by a
fourth route; ``tests/test_shacl_graph.py`` proves the restricted triple is absent.

The one place the filter has to be applied a second time is the boundary cuts, because those
name a document that is deliberately OUTSIDE the scope and so has not been through the scope
query. An unreadable neighbour is dropped from the cut list entirely rather than being
recorded with its identity blanked: "node A lost a triple on predicate p" is itself the
disclosure that A has an edge to something, which is exactly what
``query_triples``'s endpoint filter exists to hide. The consequence is stated here because it
is a real one — a violation caused by a neighbour the caller may not read is reported
unmarked, and looks like a genuine violation. It is the only answer that does not leak.

**No labels.** ``rdf/serialize.py`` adds an ``rdfs:label`` per node because its output has a
human reader. This graph has a validator for a reader, and a shape that constrains
``rdfs:label`` would then pass on a triple the store never asserted. The graph carries the
store's triples and nothing else; the prefixes are bound because binding a namespace adds no
triple and makes ``graph.serialize()`` legible when somebody debugs a report.

**Validation never mutates** (``services/ontology_service.py:11``). Nothing here writes.

**Both graphs a run needs are built here, and that is Block B finding 4.** :func:`build_data_graph`
is the DATA graph and :func:`bound_shape_graph` is the SHAPES graph — the vocabulary with every
shape but the bound one disarmed. The second one lives here because ``validate_tasks`` and
``derive_tasks`` each had their own answer to "only the bound shape runs" and the two answers
did not agree; one function, in the module both already import, is what stops them drifting
again.

This module does NOT validate — it builds the graphs and hands them over. ``pyshacl`` is not
imported here, not even through :func:`~jmfts_core.rdf.require_pyshacl`; Block A step 2 is its
first caller and this is its input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from jmfts_core.access import readable_filter, readable_id_subset
from jmfts_core.models.document import Document
from jmfts_core.models.ontology import SCOPE_TYPES, ShapeBinding
from jmfts_core.models.triple import Triple
from jmfts_core.rdf import require_rdflib
from jmfts_core.rdf.names import (
    DEFAULT_BASE_IRI,
    DOCUMENT_PREFIX,
    PREDICATE_PREFIX,
    STANDARD_PREFIXES,
    document_iri,
    document_namespace,
    iri_problem,
    predicate_iri,
    predicate_namespace,
)
from jmfts_core.rdf.serialize import TurtleSerializationError

#: The ``Settings`` field that carries the bound :class:`ScopeTooLargeError` is raised past,
#: named here so that the module which raises the error and the module which configures it
#: agree on one spelling. **The field exists**: ``Settings.shacl_max_scope_documents``
#: (``jmfts_core/config.py``, env ``JMFTS_SHACL_MAX_SCOPE_DOCUMENTS``), default **512
#: documents**, read at the one place a run's scope is pinned
#: (``services/ontology_service._pin_scope``). Open question 6.3, taken 2026-09-10.
#:
#: **512 is a deliberately conservative default and not a capacity limit.** MEASURED through
#: a real worker over ten rungs (``docs/MEASURE_SHACL_SCOPE.md``): 138,000 documents peaks at
#: 2,039–2,042 MiB, which is the whole memory REQUEST of the cpu worker pod, and 512 is far
#: below any rung anybody ran. A fresh install therefore refuses long before it can be
#: OOM-killed on a run that has no partial progress to resume from, and an operator who wants
#: a larger scope raises the bound knowing what it costs. 6.3 records where to raise it to:
#: 138,000 for a single run, 99,000 for a fleet that runs validations back to back, because a
#: worker does not give the memory back (``VmRSS`` after a drain is 93–95% of the peak).
#:
#: :func:`build_data_graph` still takes the bound as a REQUIRED argument — ``None`` for "no
#: bound", which is a statement the caller makes rather than a default this module invented.
#: ``validate_tasks`` and ``derive_tasks`` pass ``None`` deliberately: the request already
#: applied the operator's bound, and re-applying it in the worker would refuse a run the
#: request accepted.
MAX_SCOPE_DOCUMENTS_SETTING = "shacl_max_scope_documents"

#: The environment variable that sets it, spelled out so the refusal below can name the knob
#: an operator actually types. ``Settings`` takes its prefix from ``env_prefix="JMFTS_"``.
MAX_SCOPE_DOCUMENTS_ENV = "JMFTS_SHACL_MAX_SCOPE_DOCUMENTS"

#: The provenance layers a graph may be built from. Mirrors ``repositories/triple.py``'s
#: ``query_triples`` check (triple.py:258) — same three words, same refusal to read an
#: unrecognised value as "any", because a filter that silently widens is how a derived row
#: reaches an answer that asked for asserted facts.
PROVENANCE_LAYERS = ("any", "asserted", "derived")


class ScopeTooLargeError(ValueError):
    """This scope holds more documents than the configured bound, so no graph was built.

    Open question 6.3. ``pyshacl`` has no streaming mode, so a scope that does not fit in
    memory is a refusal rather than a slow success — and the refusal is cheap: the count runs
    before any triple is fetched and before any RDF term is minted.

    A ``ValueError``, which is the classification this wants twice over: ``task_errors.py``
    maps it to PERMANENT (a scope does not shrink on the third attempt) and the ``@expose``
    layer maps it to 422 (the request named a scope this appliance will not validate whole).

    The bound is expressed in **documents in the scope**, not in RDF nodes, and that is the
    only honest unit available before the build: the node count of a graph is not knowable
    until it has been materialised, which is the thing being refused. The scope size is also
    the number the operator controls.
    """

    def __init__(self, scope_document_count: int, bound: int):
        self.scope_document_count = scope_document_count
        self.bound = bound
        super().__init__(
            f"this scope holds {scope_document_count} readable documents, past the "
            f"configured bound of {bound}. Raise {MAX_SCOPE_DOCUMENTS_SETTING} "
            f"(env {MAX_SCOPE_DOCUMENTS_ENV}) or narrow the binding's scope. The default "
            "bound is deliberately far below anything measured — 138,000 documents was "
            "measured at 2 GiB, a whole worker's memory request — so raising it is a "
            "decision about memory, not a workaround."
        )


class ShapeNotInOntologyError(ValueError):
    """The binding names a shape its vocabulary no longer declares.

    Checked at bind time (``OntologyService.create_binding``) and again at run time, because a
    vocabulary is REPLACED by a second upload under the same name (``models/ontology.py``) and
    the replacement need not declare the shape the binding named.

    **A ``ValueError`` so that it classifies PERMANENT, and it was a ``LookupError`` until
    2026-09-10.** ``task_errors.classify_exception``'s permanent tuple is ``(ValueError,
    TypeError, KeyError, AttributeError, ImportError)`` — ``KeyError`` is a ``LookupError`` but
    a bare ``LookupError`` is none of them, so the two classes this one replaces fell through
    to the RETRYABLE default and a missing shape burned three backoffs before settling failed
    (``SPRINT_0_5_0.md`` Block B finding 6, whose docstring claimed the opposite). The tuple in
    ``task_errors.py`` was deliberately NOT widened to ``LookupError``: that would take
    ``IndexError`` with it, and an index error is as likely to be a transient off-by-one over a
    list that is still filling as it is to be a permanent one.

    **One class for one condition**, per Block B finding 7. ``validate_tasks`` reached it from
    the read side and ``shacl_rules`` from the write side, each with its own class; the shared
    home is here, next to the function that raises it.
    """


@dataclass(frozen=True)
class BoundaryCut:
    """One triple the scope bound removed, recorded so its consequences can be recognised.

    Open question 6.2's default is "report the violation and mark it as scope-derived,
    because a marked artefact is recoverable and a suppressed violation is not". This is what
    the mark is read from.

    ``node_iri`` is the endpoint that IS in the scope — the node whose picture in the graph
    is incomplete. ``outside_iri`` is the endpoint that is not, and it is only ever a document
    the caller may read (see the module docstring). ``predicate_iri`` is the property the cut
    was on, which is what lets a violation carrying an ``sh:resultPath`` be matched precisely
    rather than by focus node alone.
    """

    #: The in-scope endpoint's document IRI.
    node_iri: str
    #: The property the removed triple used.
    predicate_iri: str
    #: The out-of-scope endpoint's document IRI.
    outside_iri: str
    #: ``"outgoing"`` when the in-scope node was the subject, ``"incoming"`` when it was the
    #: object. Both are recorded: the SHACL subset (5.1) has no inverse path today, so only
    #: outgoing cuts can produce a violation, but a cut is a fact about the bound and the
    #: bound is symmetric.
    direction: str


@dataclass
class ScopeBoundGraph:
    """A data graph, what it was a graph OF, and what the bound removed on the way.

    ``graph`` is an ``rdflib.Graph`` and is what Block A step 2 hands to ``pyshacl``. It is
    typed ``Any`` because this module must import cleanly on an install with no ``rdflib``;
    :func:`~jmfts_core.rdf.require_rdflib` is what turns that into a named failure at the
    point of use.
    """

    graph: Any
    #: ``usetype`` / ``subtree`` / ``documents``, as the binding recorded it.
    scope_type: str
    #: Every readable document the scope resolved to, sorted. This is the scope the graph was
    #: built from, not the scope the binding names: a document the caller cannot read never
    #: appears here.
    scope_document_ids: tuple[int, ...]
    #: Data triples in the graph. Equal to ``len(graph)``, and stated separately because the
    #: report node outlives the graph object.
    triple_count: int
    #: Distinct document nodes named by those triples. **This is the number Block A step 5
    #: measures against** — the scope-document count is what the bound is expressed in, this
    #: is what pyshacl actually holds.
    node_count: int
    #: What the bound removed. Empty means the scope was closed under the triples touching
    #: it, and every violation reported against this graph is a violation of the data.
    boundary_cuts: tuple[BoundaryCut, ...] = ()
    #: The base the IRIs were minted under, so a stored report can be read back against the
    #: same names.
    base_iri: str = DEFAULT_BASE_IRI
    #: The provenance layer the triples came from — ``any``, ``asserted`` or ``derived``.
    provenance: str = "any"
    #: ``{node IRI: (predicate IRI, …)}`` over :attr:`boundary_cuts`, built once because the
    #: report lane asks it per violation.
    _cuts_by_node: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        index: dict[str, set[str]] = {}
        for cut in self.boundary_cuts:
            index.setdefault(cut.node_iri, set()).add(cut.predicate_iri)
        self._cuts_by_node = {node: tuple(sorted(preds)) for node, preds in index.items()}

    def cuts_at(self, node_iri: Optional[str]) -> tuple[str, ...]:
        """The properties on which this node lost a triple at the scope boundary."""
        if node_iri is None:
            return ()
        return self._cuts_by_node.get(node_iri, ())

    def is_scope_artefact(self, *node_iris: Optional[str]) -> bool:
        """Whether a violation naming these nodes may be an artefact of the bound.

        **How a caller tells a scope artefact from a real violation.** Pass every node the
        violation names — its ``sh:focusNode`` and its ``sh:value`` — and a true answer means
        at least one of them had a triple removed by the scope bound, so the violation cannot
        be attributed to the data alone. It is deliberately "may be": a focus node that lost
        an unrelated triple answers true, and open question 6.2's default is to MARK rather
        than to suppress, so an over-inclusive mark costs a reader a second look while an
        under-inclusive one destroys the evidence.

        For a sharper answer, compare a violation's ``sh:resultPath`` against
        :meth:`cuts_at` — the cut records the property, so a ``sh:minCount`` violation on a
        path this node lost a triple on is an artefact with near-certainty.
        """
        return any(self.cuts_at(iri) for iri in node_iris)


def triple_terms(rdflib, triple: Triple, base_iri: str) -> tuple[Any, Any, Any]:
    """One stored row as ``(subject, predicate, object)`` RDF terms.

    **The ONE mapping, for the validator and for the exporter.** ``triples_to_turtle``
    (``rdf/serialize.py``) calls this rather than repeating it, which is Block A step 1's
    leftover closed: a validator that read a row differently from the exporter would validate
    a graph nobody can export, and the drift would show up as a violation on a row that
    exports cleanly. ``tests/test_rdf_turtle.py`` pins the two paths to identical terms for
    the same rows. It lives here, in the module that needs it per triple, and
    ``rdf/serialize.py`` imports it at the point of use because this module imports
    :class:`TurtleSerializationError` from that one.

    Raises:
        TurtleSerializationError: a row cannot be written as RDF without saying something
            untrue — a predicate IRI or an ``object_datatype`` that is a CURIE or a relative
            reference. The exception class is imported from ``rdf/serialize.py`` rather than
            redeclared, so both readers of a row fail as the same thing.
    """
    subject = rdflib.URIRef(document_iri(triple.subject_id, base_iri))

    predicate = triple.predicate
    term = predicate.iri if predicate is not None and predicate.iri else None
    if term is None:
        name = predicate.name if predicate is not None else str(triple.predicate_id)
        term = predicate_iri(name, base_iri)
    else:
        problem = iri_problem(term)
        if problem is not None:
            raise TurtleSerializationError(
                f"triple {triple.id} uses predicate {triple.predicate_id}, whose iri "
                f"{term!r} cannot be written as a predicate IRI: {problem}."
            )

    if triple.object_id is not None:
        obj = rdflib.URIRef(document_iri(triple.object_id, base_iri))
    else:
        datatype = triple.object_datatype
        if datatype is None:
            # A NULL datatype is RDF's plain literal, xsd:string. Written as one, so "1999"
            # round-trips as a string rather than acquiring a type it never had.
            obj = rdflib.Literal(triple.object_literal)
        else:
            problem = iri_problem(datatype)
            if problem is not None:
                raise TurtleSerializationError(
                    f"triple {triple.id} has object_datatype {datatype!r}, which cannot be "
                    f"written as a datatype IRI: {problem}."
                )
            obj = rdflib.Literal(triple.object_literal, datatype=rdflib.URIRef(datatype))

    return subject, rdflib.URIRef(term), obj


#: The SHACL target vocabulary — every term by which a shape selects focus nodes.
#: :func:`bound_shape_graph` strips these from every shape but the bound one, and that is the
#: whole of "only the bound shape runs". It is a CLOSED list because SHACL's targets are a
#: closed vocabulary; the alternative computation, "the ``sh:`` terms whose object is a
#: shape", is not closed and is what Block B finding 4 is about.
TARGET_TERMS: tuple[str, ...] = (
    "targetNode",
    "targetClass",
    "targetSubjectsOf",
    "targetObjectsOf",
    # SHACL-AF's custom (SPARQL) target. Rarer than the four above and stripped for the
    # same reason: it selects focus nodes, and a shape nobody bound must select none.
    "target",
)


def bound_shape_graph(
    rdflib,
    *,
    source_turtle: str,
    ontology_name: str,
    shape_iri: str,
    target_iris: list[str],
    strip_targets: bool = True,
) -> tuple[Any, int]:
    """The vocabulary with every shape but the bound one disarmed, and the scope as targets.

    Returns ``(graph, targets_stripped)``. **ONE function for both runs**, which is Block B
    finding 4: validation copied the bound shape's reachable subgraph out and derivation
    stripped targets from the whole vocabulary, and one of the two had to be wrong about what
    "only the bound shape runs" means.

    **The whole vocabulary is parsed and only its TARGETS are edited.** The subgraph copy
    needs a maintained list of the ``sh:`` terms whose object is a shape, and that list is
    open-ended in a way SHACL's targets are not: it did not include ``sh:rule``,
    ``sh:condition`` or ``sh:prefixes``, so a named rule node, a named condition shape, or the
    ``owl:Ontology`` node a ``sh:SPARQLRule`` cannot load without would all have been dropped.
    MEASURED (pyshacl 0.40.1): a graph that dropped a named ``sh:condition`` shape raised
    ``RuleLoadError`` rather than skipping the rule quietly — but a dropped ``sh:property``
    INSIDE a condition shape would have silently weakened the condition, and a rule that fires
    more often than its author wrote it to is a rule that writes rows nobody asked for. Keeping
    the vocabulary whole is what preserves both behaviours.

    **Stripping the targets is what disarms a shape.** A shape with no target selects no focus
    node, so none of its constraints is evaluated and none of its rules fires, while it remains
    available as a ``sh:condition``, a ``sh:node``, a named property shape or a SPARQL prefix
    declaration. ``pyshacl``'s ``use_shapes=`` is not the alternative it looks like: it PRUNES
    the shapes graph, so a rule carrying ``sh:condition ex:SomeNamedShape`` then raises
    ``RuleLoadError``. MEASURED (same versions): with a second shape's ``sh:targetSubjectsOf``
    left in place its rule fired inside the same pass, and its output would have been stored
    under the BOUND shape's ``derived_by`` — a row attributed to a rule that did not write it.

    **The scope's documents are the focus nodes, and without them nothing happens at all.**
    MEASURED: a shape whose only target is ``sh:targetClass`` selects on ``rdf:type`` triples
    this store asserts only when somebody recorded them as facts, so it validated an empty
    target set and reported ``conforms`` — a clean bill of health meaning "nothing was
    checked" — and derived nothing. ``pyshacl``'s ``focus_nodes=`` does not substitute: it
    INTERSECTS with the shape's declared targets. So the scope is injected as ``sh:targetNode``,
    which is how SHACL says "this shape is about these nodes". The bound shape's own declared
    targets are left alone: they can only select nodes that are in the data graph, and the data
    graph is already bounded by the scope, so the union is no wider than the binding.

    ``strip_targets=False`` leaves every shape armed. No caller in the appliance passes it —
    both runs want exactly one shape to run — and it exists because the claim above is a
    measurement rather than an argument: ``tests/test_shacl_rules.py`` runs the counterfactual
    through this function and watches the unbound shape's rule fire.

    Raises:
        ShapeNotInOntologyError: this vocabulary declares no triple about ``shape_iri``.
    """
    graph = rdflib.Graph()
    graph.parse(data=source_turtle, format="turtle")
    shape = rdflib.URIRef(shape_iri)
    if (shape, None, None) not in graph:
        raise ShapeNotInOntologyError(
            f"ontology {ontology_name!r} declares no triples about {shape_iri!r}; the binding "
            "was made against a vocabulary that has since been replaced"
        )

    shacl = STANDARD_PREFIXES["sh"]
    stripped = 0
    if strip_targets:
        for term in TARGET_TERMS:
            predicate = rdflib.URIRef(shacl + term)
            for subject, _, obj in list(graph.triples((None, predicate, None))):
                if subject != shape:
                    graph.remove((subject, predicate, obj))
                    stripped += 1

    target_node = rdflib.URIRef(shacl + "targetNode")
    for iri in target_iris:
        graph.add((shape, target_node, rdflib.URIRef(iri)))
    return graph, stripped


def _scope_predicate(scope_type: str, scope: dict):
    """The ``Document`` predicate one binding's scope resolves to.

    The three cases are ``models/ontology.py``'s ``SCOPE_TYPES`` and nothing else: the set is
    closed there precisely because each value names a query, and a value with no query behind
    it would silently match nothing.
    """
    if scope_type == "usetype":
        pattern = scope.get("pattern")
        if pattern is None:
            raise ValueError("a usetype scope needs a 'pattern'")
        # The appliance already has ONE semantics for a usetype pattern — glob wildcards
        # over `documents.usetype`, `repositories/search.py:153` — and a binding that
        # matched differently from a search filter would be a second meaning for the same
        # word. Imported at the point of use: `repositories.search` pulls the embedding
        # service in at module scope, which this module has no business requiring.
        from jmfts_core.repositories.search import _apply_usetype_filter

        return _apply_usetype_filter(select(Document.id), pattern).whereclause
    if scope_type == "subtree":
        parent_id = scope.get("parent_id")
        if parent_id is None:
            raise ValueError("a subtree scope needs a 'parent_id'")
        # "the node and everything below it". `documents.path` is a JSONB array of STRICT
        # ancestors, so the root is unioned in by id — the same containment
        # `DocumentRepository.get_subtree` uses (document.py:787, :824).
        return or_(
            Document.id == parent_id,
            Document.path.op("@>")(func.jsonb_build_array(parent_id)),
        )
    if scope_type == "documents":
        document_ids = scope.get("document_ids")
        if document_ids is None:
            raise ValueError("a documents scope needs 'document_ids'")
        return Document.id.in_(list(document_ids))
    raise ValueError(f"scope_type must be one of {SCOPE_TYPES}; got {scope_type!r}")


def resolve_scope_document_ids(
    session: Session,
    *,
    scope_type: str,
    scope: dict,
    max_scope_documents: Optional[int],
) -> list[int]:
    """The documents a binding's scope names, filtered to what the caller may READ.

    The access filter is applied HERE rather than to the finished graph, and that ordering is
    the whole of this module's access story: everything downstream is bounded by this list, so
    a document the caller cannot read cannot reach the graph, cannot reach the boundary cuts
    and cannot be counted in the report.

    ``max_scope_documents`` is open question 6.3's bound; see
    :data:`MAX_SCOPE_DOCUMENTS_SETTING`. ``None`` means no bound is configured — a caller
    saying "unbounded", not a default chosen here. When a bound IS given the count is taken
    first, so an oversized scope is refused before its ids are materialised.

    Raises:
        ValueError: the scope is not one of the three, or is missing its field.
        ScopeTooLargeError: more readable documents in scope than the bound allows.
    """
    predicate = _scope_predicate(scope_type, scope)
    # `settled` is NOT filtered on. Retrieval indexes are partial on it because a half-built
    # node must not surface in a search; a binding's scope is a set of documents and a triple
    # is an asserted row, neither of which the settling walk gates. Narrowing here would be a
    # second bound nobody asked for. INGEST_SPEC.md's in-flight nodes can therefore be
    # validated mid-ingest and report violations that are artefacts of the ingest rather than
    # of the data — open question 6.2's family, and not recorded there; see the lane report.
    readable = readable_filter(session)

    if max_scope_documents is not None:
        count_stmt = select(func.count()).select_from(Document).where(predicate)
        if readable is not None:
            count_stmt = count_stmt.where(readable)
        total = session.execute(count_stmt).scalar_one()
        if total > max_scope_documents:
            raise ScopeTooLargeError(total, max_scope_documents)

    stmt = select(Document.id).where(predicate)
    if readable is not None:
        stmt = stmt.where(readable)
    return sorted(session.execute(stmt).scalars().all())


def build_data_graph(
    session: Session,
    *,
    scope_type: str,
    scope: dict,
    max_scope_documents: Optional[int],
    provenance: str = "any",
    base_iri: str = DEFAULT_BASE_IRI,
) -> ScopeBoundGraph:
    """The data graph for one bound scope, under the caller's access filter.

    A triple is in the graph when every document it names is in the resolved scope. A triple
    that touches the scope at one end only is removed and recorded as a :class:`BoundaryCut`,
    which is what open question 6.2's mark is read from.

    ``provenance`` is the same three-valued split ``triples_to_turtle`` and
    ``TripleRepository.query_triples`` take, defaulting to ``"any"`` because a validation run
    asks about the live graph as it stands. Block B's ``sh:rule`` pass is the caller that will
    want ``"asserted"``. Invalidated triples are never included: the store believes them
    false, and validating against a fact that has been retracted reports on nothing.

    Raises:
        RdfStackNotInstalled: this install has no ``rdflib``.
        ValueError: an unrecognised ``provenance`` or ``scope_type``.
        ScopeTooLargeError: the scope is past the configured bound.
        TurtleSerializationError: a row cannot be written as RDF honestly.
    """
    if provenance not in PROVENANCE_LAYERS:
        raise ValueError(f"provenance must be one of {PROVENANCE_LAYERS}; got {provenance!r}")
    rdflib = require_rdflib()

    scope_ids = resolve_scope_document_ids(
        session, scope_type=scope_type, scope=scope, max_scope_documents=max_scope_documents
    )
    graph = rdflib.Graph()
    for prefix, namespace in STANDARD_PREFIXES.items():
        graph.bind(prefix, rdflib.Namespace(namespace))
    graph.bind(DOCUMENT_PREFIX, rdflib.Namespace(document_namespace(base_iri)))
    graph.bind(PREDICATE_PREFIX, rdflib.Namespace(predicate_namespace(base_iri)))

    if not scope_ids:
        # An empty scope is an empty graph and is not an error: a binding may name a usetype
        # nothing carries yet, and 0.3.0's whole claim is that binding a shape changes
        # nothing. It is reported as zero counts, not as a failure.
        return ScopeBoundGraph(
            graph=graph,
            scope_type=scope_type,
            scope_document_ids=(),
            triple_count=0,
            node_count=0,
            base_iri=base_iri,
            provenance=provenance,
        )

    # One query for the triples that touch the scope at EITHER end, partitioned in Python.
    # Not `TripleRepository.query_triples`, for a reason that has changed: it used to apply
    # its endpoint access filter AFTER LIMIT/OFFSET, so an exhaustive walk through it
    # truncated silently (`SPRINT_0_5_0.md` Block A finding 7, since fixed — the filter is a
    # condition on the statement at triple.py:327 and a page is exact). What remains is that
    # this wants EVERY triple touching the scope and `query_triples` has no unpaged form:
    # there is no LIMIT here on purpose, because `max_scope_documents` is what bounds this,
    # which is the bound open question 6.3 is about.
    conditions = [
        Triple.invalidated_at.is_(None),
        or_(Triple.subject_id.in_(scope_ids), Triple.object_id.in_(scope_ids)),
    ]
    if provenance == "asserted":
        conditions.append(Triple.derived_by.is_(None))
    elif provenance == "derived":
        conditions.append(Triple.derived_by.is_not(None))
    stmt = (
        select(Triple).options(joinedload(Triple.predicate)).where(*conditions).order_by(Triple.id)
    )
    touching = list(session.execute(stmt).unique().scalars().all())

    in_scope = set(scope_ids)
    inside: list[Triple] = []
    outside_by_triple: list[tuple[Triple, int, str]] = []
    for triple in touching:
        subject_in = triple.subject_id in in_scope
        object_in = triple.object_id is None or triple.object_id in in_scope
        if subject_in and object_in:
            inside.append(triple)
        elif subject_in:
            outside_by_triple.append((triple, triple.object_id, "outgoing"))
        else:
            outside_by_triple.append((triple, triple.subject_id, "incoming"))

    # The scope ids are already readable, so every triple in `inside` names only readable
    # documents — the access filter is enforced by construction. The cut endpoints are the
    # exception: they are outside the scope by definition and so have not been through that
    # query. Same one-query check `triples_to_turtle` runs on same_as cluster members.
    readable_outside = readable_id_subset(session, {doc_id for _, doc_id, _ in outside_by_triple})

    node_ids: set[int] = set()
    for triple in inside:
        graph.add(triple_terms(rdflib, triple, base_iri))
        node_ids.add(triple.subject_id)
        if triple.object_id is not None:
            node_ids.add(triple.object_id)

    cuts: list[BoundaryCut] = []
    for triple, outside_id, direction in outside_by_triple:
        if outside_id not in readable_outside:
            # Recording this cut would disclose that an in-scope node has an edge to
            # something, which is what the endpoint filter hides. Dropped whole; the module
            # docstring states the consequence.
            continue
        inside_id = triple.subject_id if direction == "outgoing" else triple.object_id
        _, predicate_term, _ = triple_terms(rdflib, triple, base_iri)
        cuts.append(
            BoundaryCut(
                node_iri=document_iri(inside_id, base_iri),
                predicate_iri=str(predicate_term),
                outside_iri=document_iri(outside_id, base_iri),
                direction=direction,
            )
        )

    return ScopeBoundGraph(
        graph=graph,
        scope_type=scope_type,
        scope_document_ids=tuple(scope_ids),
        triple_count=len(graph),
        node_count=len(node_ids),
        boundary_cuts=tuple(cuts),
        base_iri=base_iri,
        provenance=provenance,
    )


def graph_for_binding(
    session: Session,
    binding: ShapeBinding,
    *,
    max_scope_documents: Optional[int],
    provenance: str = "any",
    base_iri: str = DEFAULT_BASE_IRI,
) -> ScopeBoundGraph:
    """:func:`build_data_graph` for a stored binding row — the call Block A step 2 makes.

    The binding's two scope columns and nothing else. It does not read the shape: which shape
    runs against this graph is the validator's question, and one graph serves every shape
    bound to the same scope.
    """
    return build_data_graph(
        session,
        scope_type=binding.scope_type,
        scope=binding.scope or {},
        max_scope_documents=max_scope_documents,
        provenance=provenance,
        base_iri=base_iri,
    )


__all__ = [
    "MAX_SCOPE_DOCUMENTS_ENV",
    "MAX_SCOPE_DOCUMENTS_SETTING",
    "PROVENANCE_LAYERS",
    "TARGET_TERMS",
    "BoundaryCut",
    "ScopeBoundGraph",
    "ScopeTooLargeError",
    "ShapeNotInOntologyError",
    "bound_shape_graph",
    "build_data_graph",
    "graph_for_binding",
    "resolve_scope_document_ids",
    "triple_terms",
]
