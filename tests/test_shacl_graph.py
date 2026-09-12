"""The data graph a bound shape is validated against — what is IN it and what is NOT.

``docs/SPRINT_0_5_0.md`` Part 2, Block A step 1. Nothing here validates anything: step 2 is
``require_pyshacl()``'s first caller, and this file is about the graph it will be handed.

Two claims, and the second is the one that makes this file worth its length:

**The scope bounds the graph and nothing else does.** A triple is in the graph when every
document it names is in the scope. A triple that leaves the scope is removed and RECORDED
(``BoundaryCut``), because open question 6.2 says a scope artefact is marked rather than
suppressed.

**The access filter is not a post-filter.** ``test_a_restricted_triple_is_absent_*`` builds a
graph for a REAL principal holding no grant over a restricted document and proves the triple,
the node and the scope entry are all absent. ``SPRINT_0_3_0.md`` 13.9 is that hazard arriving
by three earlier routes; a validation run reading triples the caller cannot see would be the
fourth.

One fixture, deliberately awkward, because the interesting cases are all at the boundary:

    A, B, D    usetype 'vendor', public          — the scope, under a 'vendor' binding
    S          usetype 'vendor', ACR → insider   — IN the scope, unreadable to an outsider
    C          usetype 'other',  public          — outside the scope, readable
    X          usetype 'other',  ACR → insider   — outside the scope AND unreadable

    A -knows-> B     both in scope                  → in the graph
    A -knows-> S     in scope for the owner only    → in the graph for the owner
    A -knows-> C     leaves the scope, readable     → a cut
    A -knows-> X     leaves the scope, unreadable   → a cut for the owner, NOTHING for an
                                                      outsider: the cut itself would disclose
                                                      that A has an edge to something
    C -knows-> B     enters the scope               → an incoming cut on B
    D -knows-> B     both in scope, no cuts         → the control for is_scope_artefact()
    A -label-> "Acme Ltd" (literal)                 → in the graph; a literal has no access
                                                      rule of its own
    A -founded-> B, invalidated                     → never in the graph
"""

from contextlib import contextmanager

import pytest

from jmfts_core.config import Settings
from jmfts_core.models.ontology import Ontology, ShapeBinding
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import OWNER, CurrentPrincipal, reset_principal, set_principal
from jmfts_core.rdf.names import DEFAULT_BASE_IRI, document_iri, predicate_iri
from jmfts_core.rdf.shacl import (
    MAX_SCOPE_DOCUMENTS_ENV,
    MAX_SCOPE_DOCUMENTS_SETTING,
    ScopeTooLargeError,
    ShapeNotInOntologyError,
    build_data_graph,
    graph_for_binding,
    resolve_scope_document_ids,
)
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository
from jmfts_core.task_errors import ErrorType, classify_exception

VENDOR_SCOPE = ("usetype", {"pattern": "vendor"})
KNOWS = predicate_iri("knows", DEFAULT_BASE_IRI)
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"


@contextmanager
def _as(principal):
    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


def _principal(session, name):
    row = PrincipalModel(name=name)
    session.add(row)
    session.flush()
    return CurrentPrincipal(id=row.id, name=name, is_owner=False)


def _fixture(session):
    """The corpus above. Returns ``(ids, outsider)`` — ids by letter, plus the triple ids."""
    docs = DocumentRepository(session)
    triples = TripleRepository(session)

    def _doc(title, usetype):
        return docs.create(title=title, content=f"{title} body " * 3, usetype=usetype)

    a = _doc("A", "vendor")
    b = _doc("B", "vendor")
    d = _doc("D", "vendor")
    s = _doc("S", "vendor")
    c = _doc("C", "other")
    x = _doc("X", "other")
    session.flush()

    knows = triples.create_predicate(name="knows")
    label = triples.create_predicate(name="label")
    founded = triples.create_predicate(name="founded")
    ids = {
        "A": a.id,
        "B": b.id,
        "C": c.id,
        "D": d.id,
        "S": s.id,
        "X": x.id,
        "knows": knows.id,
    }
    ids["T_AB"] = triples.create_triple(a.id, knows.id, object_id=b.id).id
    ids["T_AS"] = triples.create_triple(a.id, knows.id, object_id=s.id).id
    ids["T_AC"] = triples.create_triple(a.id, knows.id, object_id=c.id).id
    ids["T_AX"] = triples.create_triple(a.id, knows.id, object_id=x.id).id
    ids["T_CB"] = triples.create_triple(c.id, knows.id, object_id=b.id).id
    ids["T_DB"] = triples.create_triple(d.id, knows.id, object_id=b.id).id
    ids["T_LIT"] = triples.create_triple(a.id, label.id, object_literal="Acme Ltd").id
    dead = triples.create_triple(a.id, founded.id, object_id=b.id)
    triples.invalidate_triple(dead.id, reason="superseded in the fixture")
    ids["T_DEAD"] = dead.id

    insider = _principal(session, "shacl-insider")
    outsider = _principal(session, "shacl-outsider")
    session.add(AccessGrant(document_id=s.id, principal_id=insider.id, level="read"))
    session.add(AccessGrant(document_id=x.id, principal_id=insider.id, level="read"))
    session.flush()
    return ids, outsider


def _build(session, scope=VENDOR_SCOPE, **kwargs):
    scope_type, scope_body = scope
    kwargs.setdefault("max_scope_documents", None)
    return build_data_graph(session, scope_type=scope_type, scope=scope_body, **kwargs)


def _iris(bound):
    """Every IRI the graph names, subjects predicates and objects, as plain strings."""
    seen = set()
    for term in bound.graph.all_nodes():
        seen.add(str(term))
    for _, predicate, _ in bound.graph:
        seen.add(str(predicate))
    return seen


def _statements(bound):
    return {(str(s), str(p), str(o)) for s, p, o in bound.graph}


# ── what the scope puts in, and keeps out ───────────────────────────────────


def test_the_scope_bounds_the_graph(db_session):
    ids, _ = _fixture(db_session)
    with _as(OWNER):
        bound = _build(db_session)

    a, b, c = (document_iri(ids[k]) for k in ("A", "B", "C"))
    assert (a, KNOWS, b) in _statements(bound)
    # C is readable and has triples at both ends of the scope; it is out because the
    # binding did not name it, and for no other reason.
    assert c not in _iris(bound)
    assert ids["C"] not in bound.scope_document_ids
    assert set(bound.scope_document_ids) == {ids["A"], ids["B"], ids["D"], ids["S"]}


def test_a_literal_object_is_in_the_graph(db_session):
    """A literal has no document id and therefore no access rule of its own."""
    ids, _ = _fixture(db_session)
    with _as(OWNER):
        bound = _build(db_session)
    assert (document_iri(ids["A"]), predicate_iri("label"), "Acme Ltd") in _statements(bound)


def test_an_invalidated_triple_is_never_in_the_graph(db_session):
    ids, _ = _fixture(db_session)
    with _as(OWNER):
        bound = _build(db_session)
    assert predicate_iri("founded") not in _iris(bound)


def test_the_graph_carries_no_labels(db_session):
    """Unlike ``triples_to_turtle``. A shape constraining rdfs:label must not pass on a
    triple the store never asserted — the export's commentary is not data."""
    _fixture(db_session)
    with _as(OWNER):
        bound = _build(db_session)
    assert RDFS_LABEL not in _iris(bound)
    assert bound.triple_count == len(bound.graph)


def test_the_counts_describe_what_was_built(db_session):
    ids, _ = _fixture(db_session)
    with _as(OWNER):
        bound = _build(db_session)
    # A knows B, A knows S, D knows B, A label "Acme Ltd".
    assert bound.triple_count == 4
    # Document nodes only: A, B, D, S. The literal is not a node the bound counts.
    assert bound.node_count == 4
    assert bound.provenance == "any"
    assert bound.scope_type == "usetype"


# ── the access filter, with a real principal ────────────────────────────────


def test_a_restricted_triple_is_absent_for_an_ungranted_principal(db_session):
    """The claim this file exists for. S is in the binding's scope and unreadable."""
    ids, outsider = _fixture(db_session)

    with _as(OWNER):
        owner_graph = _build(db_session)
    with _as(outsider):
        outsider_graph = _build(db_session)

    a, s = document_iri(ids["A"]), document_iri(ids["S"])
    assert (a, KNOWS, s) in _statements(owner_graph)
    assert ids["S"] in owner_graph.scope_document_ids

    # Not the triple, not the node, not the scope entry, and not a cut either.
    assert (a, KNOWS, s) not in _statements(outsider_graph)
    assert s not in _iris(outsider_graph)
    assert ids["S"] not in outsider_graph.scope_document_ids
    assert all(s not in (cut.node_iri, cut.outside_iri) for cut in outsider_graph.boundary_cuts)


def test_resolve_scope_document_ids_hides_what_the_principal_cannot_read(db_session):
    ids, outsider = _fixture(db_session)
    with _as(outsider):
        resolved = resolve_scope_document_ids(
            db_session, scope_type="usetype", scope={"pattern": "vendor"}, max_scope_documents=None
        )
    assert set(resolved) == {ids["A"], ids["B"], ids["D"]}


def test_an_explicit_documents_scope_cannot_name_its_way_past_the_filter(db_session):
    """The 'documents' scope is the one that takes ids straight from the caller."""
    ids, outsider = _fixture(db_session)
    scope = ("documents", {"document_ids": [ids["A"], ids["S"]]})
    with _as(outsider):
        bound = _build(db_session, scope=scope)
    assert bound.scope_document_ids == (ids["A"],)
    assert document_iri(ids["S"]) not in _iris(bound)


# ── the boundary, and open question 6.2's mark ──────────────────────────────


def test_a_cut_records_the_node_and_the_property(db_session):
    ids, _ = _fixture(db_session)
    with _as(OWNER):
        bound = _build(db_session)

    a, b, c, x = (document_iri(ids[k]) for k in ("A", "B", "C", "X"))
    cuts = {
        (cut.node_iri, cut.predicate_iri, cut.outside_iri, cut.direction)
        for cut in bound.boundary_cuts
    }
    assert (a, KNOWS, c, "outgoing") in cuts  # A knows C left the scope
    assert (a, KNOWS, x, "outgoing") in cuts  # A knows X left the scope
    assert (b, KNOWS, c, "incoming") in cuts  # C knows B entered it
    assert len(cuts) == 3


def test_is_scope_artefact_separates_a_cut_node_from_an_intact_one(db_session):
    ids, _ = _fixture(db_session)
    with _as(OWNER):
        bound = _build(db_session)

    a, b, d = (document_iri(ids[k]) for k in ("A", "B", "D"))
    assert bound.is_scope_artefact(a) is True
    assert bound.cuts_at(a) == (KNOWS,)
    assert bound.is_scope_artefact(b) is True  # incoming cut
    # D's picture in the graph is complete, so a violation naming only D is the data's.
    assert bound.is_scope_artefact(d) is False
    assert bound.cuts_at(d) == ()
    assert bound.is_scope_artefact(None) is False
    assert bound.is_scope_artefact(d, a) is True


def test_a_cut_to_an_unreadable_document_is_not_reported(db_session):
    """Recording it would disclose that an in-scope node has an edge to something."""
    ids, outsider = _fixture(db_session)
    with _as(outsider):
        bound = _build(db_session)

    a, c, s, x = (document_iri(ids[k]) for k in ("A", "C", "S", "X"))
    outside = {cut.outside_iri for cut in bound.boundary_cuts}
    assert c in outside  # the readable neighbour is still reported
    assert s not in outside
    assert x not in outside
    assert bound.cuts_at(a) == (KNOWS,)


def test_a_scope_closed_under_its_triples_reports_no_cuts(db_session):
    """No cuts means the graph is the whole neighbourhood of the scope, and every violation
    reported against it is a violation of the data. It takes a pair with no edge leaving
    them — in the fixture above every node has one, which is the normal case and the reason
    6.2 needed an answer at all."""
    docs = DocumentRepository(db_session)
    triples = TripleRepository(db_session)
    left = docs.create(title="left", content="left body " * 3, usetype="closed")
    right = docs.create(title="right", content="right body " * 3, usetype="closed")
    db_session.flush()
    pred = triples.create_predicate(name="knows-closed")
    triples.create_triple(left.id, pred.id, object_id=right.id)
    db_session.flush()

    with _as(OWNER):
        bound = _build(db_session, scope=("usetype", {"pattern": "closed"}))
    assert bound.boundary_cuts == ()
    assert _statements(bound) == {
        (document_iri(left.id), predicate_iri("knows-closed"), document_iri(right.id))
    }


# ── the other two scope types ───────────────────────────────────────────────


def test_a_subtree_scope_is_the_node_and_everything_below_it(db_session):
    docs = DocumentRepository(db_session)
    triples = TripleRepository(db_session)
    root = docs.create(title="root", content="root body " * 3)
    child = docs.create(title="child", content="child body " * 3, parent_id=root.id)
    grandchild = docs.create(title="grandchild", content="gc body " * 3, parent_id=child.id)
    elsewhere = docs.create(title="elsewhere", content="other body " * 3)
    db_session.flush()
    knows = triples.create_predicate(name="knows-subtree")
    triples.create_triple(child.id, knows.id, object_id=grandchild.id)
    triples.create_triple(child.id, knows.id, object_id=elsewhere.id)
    db_session.flush()

    with _as(OWNER):
        bound = _build(db_session, scope=("subtree", {"parent_id": root.id}))

    assert set(bound.scope_document_ids) == {root.id, child.id, grandchild.id}
    assert bound.triple_count == 1
    assert [cut.outside_iri for cut in bound.boundary_cuts] == [document_iri(elsewhere.id)]


def test_a_usetype_scope_takes_the_wildcard_the_rest_of_the_appliance_takes(db_session):
    """One semantics for a usetype pattern, ``repositories/search.py``'s."""
    docs = DocumentRepository(db_session)
    docs.create(title="sheet", content="sheet body " * 3, usetype="profile:sheet")
    docs.create(title="row", content="row body " * 3, usetype="profile:row")
    docs.create(title="other", content="other body " * 3, usetype="vendor")
    db_session.flush()

    with _as(OWNER):
        wildcard = resolve_scope_document_ids(
            db_session,
            scope_type="usetype",
            scope={"pattern": "profile:*"},
            max_scope_documents=None,
        )
        exact = resolve_scope_document_ids(
            db_session,
            scope_type="usetype",
            scope={"pattern": "profile:sheet"},
            max_scope_documents=None,
        )
    assert len(wildcard) == 2
    assert len(exact) == 1


def test_an_empty_scope_is_an_empty_graph_and_not_an_error(db_session):
    with _as(OWNER):
        bound = _build(db_session, scope=("usetype", {"pattern": "nothing-carries-this"}))
    assert bound.scope_document_ids == ()
    assert bound.triple_count == 0
    assert bound.node_count == 0
    assert bound.boundary_cuts == ()


# ── open question 6.3's bound ───────────────────────────────────────────────


def test_a_scope_past_the_configured_bound_raises_the_named_error(db_session):
    ids, _ = _fixture(db_session)
    with _as(OWNER), pytest.raises(ScopeTooLargeError) as caught:
        _build(db_session, max_scope_documents=2)
    assert caught.value.bound == 2
    assert caught.value.scope_document_count == 4
    assert "shacl_max_scope_documents" in str(caught.value)


def test_the_bound_is_measured_after_the_access_filter(db_session):
    """An outsider's scope is three documents, so a bound of three admits it and the same
    bound refuses the owner's four. The bound is on what will be BUILT, not on what the
    binding names."""
    _, outsider = _fixture(db_session)
    with _as(outsider):
        assert _build(db_session, max_scope_documents=3).triple_count == 3
    with _as(OWNER), pytest.raises(ScopeTooLargeError):
        _build(db_session, max_scope_documents=3)


def test_no_configured_bound_builds_the_graph(db_session):
    """``None`` is the caller saying 'unbounded', and it stays a supported value: the two task
    handlers pass it because the REQUEST already applied the operator's bound."""
    _fixture(db_session)
    with _as(OWNER):
        assert _build(db_session, max_scope_documents=None).triple_count == 4


def test_the_setting_the_error_names_is_the_setting_that_exists():
    """Open question 6.3, taken 2026-09-10. ``MAX_SCOPE_DOCUMENTS_SETTING`` exists so that the
    module which RAISES the refusal and the module which CONFIGURES it agree on one spelling,
    and this is what keeps the two from drifting: the constant is looked up against the real
    field rather than compared to a string written twice.

    512 is deliberately far below every measured rung (138,000 documents peaked at 2,039 MiB,
    ``docs/MEASURE_SHACL_SCOPE.md``). It is a conservative default an operator raises, not a
    capacity limit, and the number is asserted here so that changing it is a decision somebody
    makes rather than a value that drifts.
    """
    field = Settings.model_fields[MAX_SCOPE_DOCUMENTS_SETTING]
    assert field.default == 512
    # The refusal has to teach the knob, because the operator reading it is the one who moves
    # it — and both spellings, since the field name is not what anybody types into a shell.
    message = str(ScopeTooLargeError(900, 512))
    assert MAX_SCOPE_DOCUMENTS_SETTING in message
    assert MAX_SCOPE_DOCUMENTS_ENV in message


def test_a_missing_shape_is_permanent_and_not_retried():
    """``SPRINT_0_5_0.md`` Block B finding 6, and the shipped comment it falsified.

    :class:`ShapeNotInOntologyError` subclassed ``LookupError`` on both sides of the seam and
    both docstrings said a ``LookupError`` is PERMANENT. It is not: ``classify_exception``'s
    permanent tuple is ``(ValueError, TypeError, KeyError, AttributeError, ImportError)``, and
    ``KeyError`` being a ``LookupError`` does not make the base one — so a binding whose
    vocabulary was replaced burned three backoffs before settling failed.

    The fix is on the class and NOT on the tuple: widening the tuple to ``LookupError`` takes
    ``IndexError`` with it, and this asserts that too, because the day somebody "simplifies"
    this class back to a ``LookupError`` the test that fails should be the one that says why.
    """
    assert classify_exception(ShapeNotInOntologyError("gone")) is ErrorType.PERMANENT
    assert classify_exception(LookupError("gone")) is not ErrorType.PERMANENT
    assert classify_exception(IndexError("off the end")) is not ErrorType.PERMANENT


# ── provenance, and the refusals ────────────────────────────────────────────


def test_provenance_splits_the_asserted_layer_from_the_derived_one(db_session):
    ids, _ = _fixture(db_session)
    triples = TripleRepository(db_session)
    triples.create_triple(
        ids["A"], ids["knows"], object_id=ids["D"], derived_by="shapes/ExampleRule"
    )
    db_session.flush()

    with _as(OWNER):
        asserted = _build(db_session, provenance="asserted")
        derived = _build(db_session, provenance="derived")
        both = _build(db_session, provenance="any")

    a, d = document_iri(ids["A"]), document_iri(ids["D"])
    assert (a, KNOWS, d) not in _statements(asserted)
    assert _statements(derived) == {(a, KNOWS, d)}
    assert both.triple_count == asserted.triple_count + derived.triple_count


def test_an_unrecognised_provenance_raises_rather_than_widening(db_session):
    with pytest.raises(ValueError, match="provenance"):
        _build(db_session, provenance="everything")


def test_an_unknown_scope_type_raises(db_session):
    with pytest.raises(ValueError, match="scope_type"):
        _build(db_session, scope=("neighbourhood", {"pattern": "vendor"}))


def test_a_scope_missing_its_field_raises(db_session):
    with pytest.raises(ValueError, match="parent_id"):
        _build(db_session, scope=("subtree", {}))


# ── the call the task handler makes ─────────────────────────────────────────


def test_graph_for_binding_reads_the_scope_off_the_row(db_session):
    ids, _ = _fixture(db_session)
    db_session.add(
        Ontology(
            name="vendors", base_iri="http://example.org/vendors#", source_turtle="", shapes={}
        )
    )
    db_session.flush()
    binding = ShapeBinding(
        ontology_name="vendors",
        shape_iri="http://example.org/vendors#VendorShape",
        scope_type="usetype",
        scope={"pattern": "vendor"},
    )
    db_session.add(binding)
    db_session.flush()

    with _as(OWNER):
        bound = graph_for_binding(db_session, binding, max_scope_documents=None)
    assert set(bound.scope_document_ids) == {ids["A"], ids["B"], ids["D"], ids["S"]}
    assert bound.scope_type == "usetype"
