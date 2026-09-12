"""``sh:rule``, one pass, over asserted data. ``docs/SPRINT_0_5_0.md`` Block B steps 6, 7 and 8.

A separate module from :mod:`jmfts_core.rdf.shacl`, which builds both graphs a run needs — the
data graph a shape is run against, and the shapes graph with every shape but the bound one
disarmed (:func:`~jmfts_core.rdf.shacl.bound_shape_graph`, which used to live here and moved
under Block B finding 4) — and from :mod:`jmfts_core.validate_tasks`, which reports without
changing a row. **This one writes**, and everything below is arranged around three properties
the plan names before any of it is built.

**ONE PASS, OVER ASSERTED DATA ONLY.** The input graph is
``build_data_graph(..., provenance="asserted")`` — ``WHERE derived_by IS NULL`` — so no rule
can read another rule's output, and there is no loop here to iterate: :func:`expand_rules`
calls ``pyshacl`` exactly once. A rule whose input is another rule's output is a fixpoint over
a store that also accepts writes, which is a scheduling problem rather than a derivation one,
and Block B refuses it. What that restriction actually buys is stated where it is enforced:
the endpoint takes NO ``provenance`` parameter, because a caller who could ask for
``"any"`` would be asking for exactly the chain the block declined.

MEASURED, and it qualifies the sentence above rather than contradicting it: within a single
``pyshacl`` expansion, rules in a later ``sh:order`` group DO see the triples an earlier group
added (pyshacl 0.40.1, rdflib 7.6.0 — a ``sh:order 2`` rule whose ``sh:condition`` was an
``sh:order 1`` rule's output fired). That is SHACL-AF's defined semantics for one expansion,
declared by the shape's author and bounded by the number of order groups; it is not a fixpoint
and it is not a second call. The restriction this module enforces is the one about the STORE:
a derived row is never an input, whichever run produced it.

**THE ROW IS AN ORDINARY TRIPLE BUT FOR ``derived_by``.** Step 7. Same table, same predicate
registry, same endpoint documents, same access rules — ``TripleRepository.query_triples``
filters a derived row by its endpoints exactly as it filters an asserted one, and
``tests/test_derived_triple_visibility.py`` is what proves it rather than assuming it.

**RE-DERIVATION IS DELETE-THEN-INSERT, SCOPED BY RULE.** Step 8. ``WHERE derived_by = :rule``
is a complete description of what one rule produced, so :func:`apply_derivation` deletes that
set and rebuilds it. No diffing and no reconciliation: the plan declined both, and the
discipline is what ``013_rdf_layer.sql`` already bought by adding the column at all.

**WHAT A RULE MAY WRITE IS NARROWER THAN WHAT ``sh:rule`` MAY SAY, and every refusal here is a
refusal rather than a skip.** A triple this store can hold names a document as its subject, a
registered predicate, and either a document or a literal as its object; the endpoints must be
in the binding's scope. Anything else — a class IRI as an object (which is what an
``rdf:type`` rule produces), a blank node, a language-tagged literal, a predicate no
vocabulary registered, a document outside the scope — raises, and NOTHING is written for that
run. A partial derivation would be a rule whose output nobody can characterise, which is the
one thing ``derived_by`` exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import unquote

from sqlalchemy.orm import Session

from jmfts_core.models.triple import Triple
from jmfts_core.rdf.names import (
    DEFAULT_BASE_IRI,
    STANDARD_PREFIXES,
    document_namespace,
    iri_problem,
    predicate_namespace,
)
from jmfts_core.repositories.triple import TripleRepository

_SH = STANDARD_PREFIXES["sh"]

#: What a rule's identity is spelled with. See :func:`rule_identity`.
RULE_PREFIX = "shacl:"

#: How wide ``triples.derived_by`` is, read off the model rather than repeated as a number.
#: :func:`rule_identity` is a fixed-width string well inside it, and
#: ``tests/test_shacl_rules.py`` is what keeps that true.
DERIVED_BY_WIDTH: int = Triple.derived_by.type.length

#: How wide ``triples.object_datatype`` is. A datatype IRI longer than this cannot be stored,
#: and saying so by name beats an ``IntegrityError`` from the driver.
DATATYPE_WIDTH: int = Triple.object_datatype.type.length


class ShapeDeclaresNoRuleError(ValueError):
    """The bound shape declares no ``sh:rule``, so a derivation run would derive nothing.

    Refused rather than reported as "0 triples derived", because that number is
    indistinguishable from "the rules ran and matched nothing" — and those are opposite
    facts about the vocabulary. The same refusal ``ScopeEmptyError`` is: an answer that
    cannot be told apart from a different answer is not one.
    """


class UnstorableTermError(ValueError):
    """A rule produced a triple this store cannot hold without saying something untrue.

    The three that will be met in practice, and none of them is a bug in the rule:

    * an object that is a class IRI — ``$this rdf:type ex:Person``. ``triples.object_id`` is
      a foreign key to ``documents``, so a class is neither a document node nor a literal,
      and writing it as one would claim a resource is a string.
    * a blank node at either end. A blank node's id is an artefact of this parse
      (``rdf/parse.py`` refuses a blank-node ``NodeShape`` for the same reason), so a row
      carrying one would name something that does not survive the next run.
    * a language-tagged literal. There is no language column, and dropping the tag makes
      ``"Köln"@de`` and ``"Köln"@en`` one fact.
    """


class TermOutOfScopeError(ValueError):
    """A rule named a document outside the binding's scope.

    **This is an access rule and not a tidiness rule.** ``SPRINT_0_5_0.md`` Block B step 9:
    a derived triple's visibility follows its ENDPOINTS, so a rule that could name a
    document outside the scope could mint a fact ABOUT a document whose access nobody
    checked — the request only ever established write access over the scope. Keeping both
    endpoints inside the scope is what makes the derived row exactly as visible as the data
    it was derived from.
    """


class PredicateNotRegisteredError(ValueError):
    """A rule wrote a predicate IRI no row in ``predicates`` carries.

    Not minted here, deliberately. ``OntologyService.import_ontology`` is where a vocabulary
    term becomes a predicate row, and it is where the genuine ambiguity is REPORTED rather
    than resolved: a term whose local name is already taken by a different predicate is a
    ``PredicateImportConflict`` and the existing row is left alone. A rule pass that minted
    predicates would have to make that decision silently, in a worker, with nobody reading
    the answer.
    """


@dataclass(frozen=True)
class DerivedTriple:
    """One row a rule asks for, in the store's own terms rather than RDF's."""

    subject_id: int
    predicate_id: int
    object_id: Optional[int] = None
    object_literal: Optional[str] = None
    object_datatype: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "predicate_id": self.predicate_id,
            "object_id": self.object_id,
            "object_literal": self.object_literal,
            "object_datatype": self.object_datatype,
        }


@dataclass(frozen=True)
class DerivationOutcome:
    """What one delete-then-insert did. Step 8's whole result."""

    rule: str
    #: Rows removed because they carried this rule's identity. A first run deletes nothing.
    deleted: int
    #: Rows written, each with ``derived_by`` set to :attr:`rule`.
    inserted: int
    #: Candidates the store already held — as an assertion, or under another rule's
    #: identity. Recorded rather than counted, because "the rule concluded something that
    #: was already known" and "the rule concluded something new" are different facts and a
    #: reader of the report is owed which. See :func:`apply_derivation`.
    already_present: tuple[dict, ...]


def rule_identity(*, ontology_name: str, shape_iri: str, scope_type: str, scope: dict) -> str:
    """What goes in ``triples.derived_by``: one rule's identity, fixed width.

    **The binding is the rule.** A ``sh:rule`` is only ever run because a shape was bound to
    a scope, so what identifies the derivation is the four fields ``uq_shape_binding`` makes
    unique — the vocabulary, the shape, the kind of scope and the scope itself. Not the
    binding's row id: a binding deleted and recreated identically would then be a DIFFERENT
    rule, its predecessor's rows would be unreachable by ``WHERE derived_by = :rule``, and
    step 8's property would have a hole in it exactly where somebody corrected a mistake.

    **Not the shape's CONTENT either**, and that is the other half of the same decision. A
    vocabulary re-uploaded under the same name REPLACES the previous one
    (``models/ontology.py``), so a shape's definition can change under a binding that did
    not. Content-addressing would make the changed rule a new rule and strand every row the
    old one wrote; addressing the binding makes the next run a RE-derivation, which deletes
    them.

    **It is a digest and therefore opaque, and that is a cost this pays deliberately.**
    ``triples.derived_by`` is ``String(200)`` and ``ontologies.name`` is ``String(200)`` on
    its own, so no readable encoding of the four fields fits without truncating one of them —
    and a truncated identity is one that two rules can share. The full SHA-256 is
    {prefix}+64 characters whatever it is given. What it expands to is recorded on the
    derivation report node and returned by the endpoint that starts a run, so the mapping is
    stored rather than lost; a ``derived_rules`` table that made it a join is the thing to
    build when a second reader needs it.
    """
    payload = json.dumps(
        {
            "ontology": ontology_name,
            "shape": shape_iri,
            "scope_type": scope_type,
            # Verbatim, in the shape the binding row holds. `document_ids: [1, 2]` and
            # `[2, 1]` are two different JSONB values and therefore two different binding
            # rows under `uq_shape_binding`; the digest agrees with the constraint rather
            # than canonicalising past it.
            "scope": scope,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return RULE_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def declares_a_rule(rdflib, shapes_graph, shape_iri: str) -> bool:
    """Whether this shape carries any ``sh:rule`` at all. See :class:`ShapeDeclaresNoRuleError`."""
    return (
        rdflib.URIRef(shape_iri),
        rdflib.URIRef(_SH + "rule"),
        None,
    ) in shapes_graph


def expand_rules(pyshacl, data_graph, shapes_graph) -> list[tuple]:
    """Run the rule pass ONCE and return the triples it added, sorted.

    ``pyshacl.shacl_rules`` and not ``pyshacl.validate(advanced=True)``, which is what the
    plan named. Same machinery — 0.40.1 exposes rule expansion as its own entry point
    (``RuleExpandRunner``) — and the difference is that this one does not also validate: a
    derivation run that reported violations would be two answers in one node, and Block A
    already owns the other one.

    ``inplace=True`` because the graph is this process's own, built from the store a moment
    ago and thrown away after; MEASURED, it is the passed graph that grows, while the
    returned object is a quad-yielding wrapper. The diff against the snapshot is therefore
    taken on the graph, not on the return value.

    ``inference="none"``: no RDFS and no OWL. What a rule concludes is what its author wrote,
    and an inferred triple would arrive with no rule to attribute it to.
    """
    before = set(data_graph)
    pyshacl.shacl_rules(data_graph, shacl_graph=shapes_graph, inplace=True, inference="none")
    return sorted(set(data_graph) - before, key=lambda t: (str(t[0]), str(t[1]), str(t[2])))


def _document_id(rdflib, term, *, base_iri: str, scope_ids: set[int], role: str) -> int:
    namespace = document_namespace(base_iri)
    if not isinstance(term, rdflib.URIRef):
        raise UnstorableTermError(
            f"a rule produced a triple whose {role} is {term!r}, which is not an IRI; a "
            "triple's subject and its resource object are both document nodes here"
        )
    text = str(term)
    if not text.startswith(namespace):
        raise UnstorableTermError(
            f"a rule produced a triple whose {role} is {text!r}, which names no document in "
            f"this appliance (document IRIs hang off {namespace!r}). An object that is a "
            "class IRI — what an rdf:type rule writes — is this case: triples.object_id is a "
            "foreign key to documents, so a class is neither a document node nor a literal"
        )
    tail = text[len(namespace) :]
    if not tail.isdigit():
        raise UnstorableTermError(
            f"a rule produced a triple whose {role} is {text!r}; the document namespace is "
            f"right but {tail!r} is not a document id"
        )
    document_id = int(tail)
    if document_id not in scope_ids:
        raise TermOutOfScopeError(
            f"a rule produced a triple whose {role} is document {document_id}, which is not "
            "in the binding's scope. A derived triple is as visible as the documents it "
            "names, and the run established write access over the scope and nothing else"
        )
    return document_id


def _predicate_id(session: Session, rdflib, term, *, base_iri: str) -> int:
    if not isinstance(term, rdflib.URIRef):
        raise UnstorableTermError(
            f"a rule produced a triple whose predicate is {term!r}, which is not an IRI"
        )
    text = str(term)
    repo = TripleRepository(session)
    by_iri = repo.get_predicate_by_iri(text)
    if by_iri is not None:
        return by_iri.id
    namespace = predicate_namespace(base_iri)
    if text.startswith(namespace):
        # A predicate with no `iri` is written into the data graph under the local
        # namespace (`rdf/shacl.triple_terms`), percent-encoded because `predicates.name`
        # admits a space. Reading it back is that mapping inverted, and the `iri is None`
        # check is what keeps it from claiming a row that a vocabulary already named.
        name = unquote(text[len(namespace) :])
        by_name = repo.get_predicate_by_name(name)
        if by_name is not None and by_name.iri is None:
            return by_name.id
    raise PredicateNotRegisteredError(
        f"a rule produced a triple on predicate {text!r}, which no row in `predicates` "
        "carries. Import the vocabulary that declares it — POST /ontologies registers every "
        "sh:path and every rdf:Property it finds — rather than having a worker mint it"
    )


def _object_terms(rdflib, term, *, base_iri: str, scope_ids: set[int]):
    """``(object_id, object_literal, object_datatype)`` for one RDF object term."""
    if isinstance(term, rdflib.Literal):
        if term.language:
            raise UnstorableTermError(
                f"a rule produced the language-tagged literal {term!r}; `triples` has no "
                "language column, and storing the lexical form alone would make two "
                "literals that differ only by tag one fact"
            )
        datatype = None if term.datatype is None else str(term.datatype)
        if datatype is not None:
            problem = iri_problem(datatype)
            if problem is not None:
                raise UnstorableTermError(
                    f"a rule produced a literal typed {datatype!r}, which cannot be written "
                    f"as a datatype IRI: {problem}"
                )
            if len(datatype) > DATATYPE_WIDTH:
                raise UnstorableTermError(
                    f"a rule produced a literal typed {datatype!r}, which is longer than "
                    f"triples.object_datatype ({DATATYPE_WIDTH} characters)"
                )
        return None, str(term), datatype
    return (
        _document_id(rdflib, term, base_iri=base_iri, scope_ids=scope_ids, role="object"),
        None,
        None,
    )


def map_to_rows(
    session: Session,
    rdflib,
    triples: list[tuple],
    *,
    base_iri: str = DEFAULT_BASE_IRI,
    scope_ids: set[int],
) -> list[DerivedTriple]:
    """Every derived RDF triple as a row this store can hold, or a refusal naming the term.

    **Nothing is written by this function and nothing is skipped by it.** The whole list is
    mapped before :func:`apply_derivation` deletes anything, so a rule that produces one
    unstorable triple leaves the store exactly as it was — including the rows a previous run
    of the same rule wrote, which a partial re-derivation would already have deleted.
    """
    rows: list[DerivedTriple] = []
    for subject, predicate, obj in triples:
        object_id, object_literal, object_datatype = _object_terms(
            rdflib, obj, base_iri=base_iri, scope_ids=scope_ids
        )
        rows.append(
            DerivedTriple(
                subject_id=_document_id(
                    rdflib, subject, base_iri=base_iri, scope_ids=scope_ids, role="subject"
                ),
                predicate_id=_predicate_id(session, rdflib, predicate, base_iri=base_iri),
                object_id=object_id,
                object_literal=object_literal,
                object_datatype=object_datatype,
            )
        )
    return rows


def _existing(session: Session, candidate: DerivedTriple) -> Optional[Triple]:
    """The row already occupying this candidate's uniqueness slot, if there is one.

    Matched on the same columns ``uq_triple`` and ``uq_triple_literal`` are built from, and
    **not filtered by** ``invalidated_at``: neither index excludes an invalidated row, so a
    retracted fact still occupies the slot and inserting over it would be an
    ``IntegrityError`` rather than a second fact.

    The literal half compares the lexical form where the index compares its md5, which is
    strictly narrower — the only pair this misses is two different literals that collide on
    md5 under one subject and predicate, and that pair raises at INSERT instead of being
    silently deduplicated. ``models/triple.py`` states the same cost against the index.
    """
    query = session.query(Triple).filter(
        Triple.subject_id == candidate.subject_id,
        Triple.predicate_id == candidate.predicate_id,
    )
    if candidate.object_id is not None:
        return query.filter(Triple.object_id == candidate.object_id).first()
    return (
        query.filter(
            Triple.object_literal == candidate.object_literal,
            (
                Triple.object_datatype.is_(None)
                if candidate.object_datatype is None
                else Triple.object_datatype == candidate.object_datatype
            ),
        )
        .filter(Triple.object_id.is_(None))
        .first()
    )


def apply_derivation(
    session: Session, *, rule: str, candidates: list[DerivedTriple]
) -> DerivationOutcome:
    """Delete everything this rule produced, then write what it produces now. Step 8.

    In that order and with no diff between them. ``WHERE derived_by = :rule`` is a complete
    description of one rule's output, so the delete needs no knowledge of what the previous
    run concluded and the insert needs no knowledge of what this one changed. A rule that
    wrote rows it could not later identify could only be undone by dropping everything
    derived, which is what the column was added to avoid.

    The delete runs FIRST so that a candidate the previous run already wrote is written
    again rather than being read as "already present": after it, every row
    :func:`_existing` can find belongs to somebody else — an assertion, or another rule.
    Those are skipped and recorded, because the store cannot hold the same fact twice
    (``uq_triple``) and because "the rule concluded something already known" is a result
    worth reading rather than an error.
    """
    if len(rule) > DERIVED_BY_WIDTH:
        raise ValueError(
            f"rule identity {rule!r} is {len(rule)} characters and triples.derived_by holds "
            f"{DERIVED_BY_WIDTH}"
        )
    deleted = (
        session.query(Triple).filter(Triple.derived_by == rule).delete(synchronize_session=False)
    )
    session.flush()

    repo = TripleRepository(session)
    inserted = 0
    already: list[dict] = []
    for candidate in candidates:
        row = _existing(session, candidate)
        if row is not None:
            already.append({**candidate.as_dict(), "held_by": row.id, "derived_by": row.derived_by})
            continue
        repo.create_triple(
            subject_id=candidate.subject_id,
            predicate_id=candidate.predicate_id,
            object_id=candidate.object_id,
            object_literal=candidate.object_literal,
            object_datatype=candidate.object_datatype,
            # NOT SET, and it is a decision rather than an omission: `source_document_id`
            # names the document a fact was READ OUT OF, and a rule reads a graph rather
            # than a document. What produced this row is `derived_by`, which is the column
            # the question has an answer in.
            source_document_id=None,
            derived_by=rule,
        )
        inserted += 1
    session.flush()
    return DerivationOutcome(
        rule=rule, deleted=deleted, inserted=inserted, already_present=tuple(already)
    )


__all__ = [
    "DATATYPE_WIDTH",
    "DERIVED_BY_WIDTH",
    "RULE_PREFIX",
    "DerivationOutcome",
    "DerivedTriple",
    "PredicateNotRegisteredError",
    "ShapeDeclaresNoRuleError",
    "TermOutOfScopeError",
    "UnstorableTermError",
    "apply_derivation",
    "declares_a_rule",
    "expand_rules",
    "map_to_rows",
    "rule_identity",
]
