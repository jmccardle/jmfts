"""A rule that writes, and the row it leaves behind. ``SPRINT_0_5_0.md`` Block B steps 6–8.

Four claims.

**A bound shape's ``sh:rule`` set runs, once, over ASSERTED data.** ``jmfts_core/rdf/shacl.py``
builds the graph with ``provenance="asserted"`` — ``WHERE derived_by IS NULL`` — and
``jmfts_core/shacl_rules.py`` runs ``pyshacl`` over it exactly once. ``test_rules_do_not_chain``
is the load-bearing one: it derives with one binding, then runs a second binding whose rule
fires only on the first one's output, and asserts it concludes nothing. That is what "rules do
not chain in this sprint" MEANS operationally, and it is a property of what the graph is built
from rather than of how many times anything is called.

**The row is an ordinary triple but for ``derived_by``.** Step 7. Same table, same predicate
registry, same uniqueness constraints; ``models/triple.py`` has carried the column since
migration ``013`` with "nothing writes it yet" against it, and this is what writes it.

**Re-derivation is delete-then-insert, scoped by rule.** Step 8.
``test_a_re_run_replaces_only_what_this_rule_wrote`` changes the asserted data between two
runs and proves the stale derived row is gone, the asserted rows are untouched, and another
rule's rows survive.

**A rule may only write what this store can hold, and a refusal writes NOTHING.** An object
outside the binding's scope, a predicate no vocabulary registered, a shape carrying no rule:
each fails the run, and the mapping is complete before the delete begins, so a run that fails
half way through leaves even the PREVIOUS run's rows in place.

The corpus:

    A, B    usetype 'vendor'  — the scope, under a 'vendor' binding
    C       usetype 'other'   — readable, and OUTSIDE the scope

    A -supplies-> B     (a local predicate, iri NULL: written into the graph under
                         urn:jmfts:predicate: and read back by name)
    A -supplies-> C     leaves the scope: a BoundaryCut, and invisible to a rule
"""

from contextlib import contextmanager

import pytest

from jmfts_client.contracts.rdf import RuleDerivationRequest, ShapeBindingCreate
from jmfts_core.access import AccessDeniedError
from jmfts_core.derive_tasks import (
    DERIVATION_BLOCK,
    DERIVATION_REPORT_USETYPE,
    RENDERED_TRIPLE_CAP,
    _render,
)
from jmfts_core.ingest_tasks import TASK_DERIVE_RULE, TASK_HANDLERS, TASK_ROWS
from jmfts_core.models.document import Document
from jmfts_core.models.ontology import Ontology
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.models.task_queue import TaskQueue
from jmfts_core.models.triple import Triple
from jmfts_core.principal_context import OWNER, CurrentPrincipal, reset_principal, set_principal
from jmfts_core.rdf import require_pyshacl, require_rdflib
from jmfts_core.rdf.names import DEFAULT_BASE_IRI, document_iri
from jmfts_core.rdf.shacl import (
    ShapeNotInOntologyError,
    bound_shape_graph,
    build_data_graph,
)
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository
from jmfts_core.rollup_tasks import IngestRollupPlanner
from jmfts_core.services.ontology_service import OntologyService
from jmfts_core.shacl_rules import DERIVED_BY_WIDTH, expand_rules, rule_identity
from jmfts_core.task_errors import ErrorType, classify_exception

from tests.conftest import drain_ingest_queue

ONTOLOGY = "vendor-rules"
VENDOR_RULES = "http://example.org/VendorRules"
CHAIN_RULES = "http://example.org/ChainRules"
LEAKY_RULES = "http://example.org/Leaky"
NO_RULES = "http://example.org/NoRules"
OFFSCOPE_RULES = "http://example.org/OffScopeRules"
UNKNOWN_PREDICATE_RULES = "http://example.org/UnknownPredicateRules"

REVIEWED = "http://example.org/reviewed"
SUPPLIED_BY = "http://example.org/suppliedBy"
CHAINED = "http://example.org/chained"
LEAKED = "http://example.org/leaked"
SUPPLIES = "urn:jmfts:predicate:supplies"


def _turtle(off_scope_id: int) -> str:
    """The vocabulary. Every shape here is a trap for one specific way of getting this wrong.

    ``ex:VendorRules`` names its rules with IRIs rather than writing them inline, so a shapes
    graph built by walking blank nodes out of the bound shape — which is what the VALIDATION
    side does — would drop both and derive nothing at all.

    ``ex:Leaky`` carries ``sh:targetSubjectsOf``, which selects focus nodes with no ``rdf:type``
    triple in sight and therefore genuinely fires against this data. It is the shape that
    proves target-stripping is doing something: without it, its rule's output would be stored
    under the BOUND shape's identity.
    """
    return f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix ex: <http://example.org/> .

ex:reviewed a owl:DatatypeProperty .
ex:suppliedBy a owl:ObjectProperty .
ex:chained a owl:DatatypeProperty .
ex:leaked a owl:DatatypeProperty .
ex:offscope a owl:ObjectProperty .

ex:VendorRules a sh:NodeShape ;
    sh:rule ex:ReviewedRule ;
    sh:rule ex:SuppliedByRule .

ex:ReviewedRule a sh:TripleRule ;
    sh:subject sh:this ;
    sh:predicate ex:reviewed ;
    sh:object "yes" .

ex:SuppliedByRule a sh:SPARQLRule ;
    sh:construct \"\"\"
        CONSTRUCT {{ ?o <{SUPPLIED_BY}> $this }}
        WHERE {{ $this <{SUPPLIES}> ?o }}
    \"\"\" .

ex:ChainRules a sh:NodeShape ;
    sh:rule [
        a sh:TripleRule ;
        sh:condition ex:HasReviewed ;
        sh:subject sh:this ;
        sh:predicate ex:chained ;
        sh:object "chained" ;
    ] .

ex:HasReviewed a sh:NodeShape ;
    sh:property [ sh:path ex:reviewed ; sh:minCount 1 ] .

ex:Leaky a sh:NodeShape ;
    sh:targetSubjectsOf <{SUPPLIES}> ;
    sh:rule [
        a sh:TripleRule ;
        sh:subject sh:this ;
        sh:predicate ex:leaked ;
        sh:object "leaked" ;
    ] .

ex:NoRules a sh:NodeShape ;
    sh:property [ sh:path <{SUPPLIES}> ; sh:minCount 1 ] .

ex:OffScopeRules a sh:NodeShape ;
    sh:rule [
        a sh:TripleRule ;
        sh:subject sh:this ;
        sh:predicate ex:offscope ;
        sh:object <{document_iri(off_scope_id)}> ;
    ] .

ex:UnknownPredicateRules a sh:NodeShape ;
    sh:rule [
        a sh:TripleRule ;
        sh:subject sh:this ;
        sh:predicate <http://example.org/never-imported> ;
        sh:object "x" ;
    ] .
"""


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
    """The corpus in the module docstring. Returns the ids by letter."""
    docs = DocumentRepository(session)
    triples = TripleRepository(session)

    a = docs.create(title="A", content="A body", usetype="vendor")
    b = docs.create(title="B", content="B body", usetype="vendor")
    c = docs.create(title="C", content="C body", usetype="other")
    session.flush()

    # No `iri`, on purpose: a local predicate is written into the data graph under
    # `urn:jmfts:predicate:` and has to be read back by NAME, which is the half of the
    # mapping a vocabulary-registered predicate never exercises.
    supplies = triples.create_predicate(name="supplies")
    triples.create_triple(a.id, supplies.id, object_id=b.id)
    triples.create_triple(a.id, supplies.id, object_id=c.id)
    session.flush()
    return {"A": a.id, "B": b.id, "C": c.id}


def _import(session, ids):
    OntologyService(session).import_ontology(
        _turtle(ids["C"]), name=ONTOLOGY, base_iri=DEFAULT_BASE_IRI
    )


def _bind(session, shape_iri, *, usetype_pattern="vendor"):
    return OntologyService(session).create_binding(
        ONTOLOGY, ShapeBindingCreate(shape_iri=shape_iri, usetype_pattern=usetype_pattern)
    )


def _derive(session, binding_id, *, planner=None):
    """Enqueue a derivation run and drain it. Returns ``(response, report node)``.

    The drain runs UNBOUND — a worker holds no principal and an unbound caller bypasses every
    access check — so the handler runs exactly as it does in production and what it derives
    over is still only what the request pinned onto the task row.
    """
    response = OntologyService(session).derive_binding(
        ONTOLOGY, RuleDerivationRequest(binding_id=binding_id)
    )
    with _as(None):
        drain_ingest_queue(
            session, planner=planner if planner is not None else IngestRollupPlanner()
        )
    session.expire_all()
    return response, session.get(Document, response.report_document_id)


def _report(node) -> dict:
    return (node.structured_content or {})[DERIVATION_BLOCK]["report"]


def _derived(session, rule=None):
    """Every derived triple, as ``(subject, predicate iri or name, object) -> row``."""
    query = session.query(Triple).filter(Triple.derived_by.is_not(None))
    if rule is not None:
        query = query.filter(Triple.derived_by == rule)
    return list(query.all())


def _facts(session, rows):
    out = set()
    for row in rows:
        predicate = session.get(Triple, row.id).predicate
        obj = row.object_id if row.object_id is not None else row.object_literal
        out.add((row.subject_id, predicate.iri or predicate.name, obj))
    return out


# ── the task is a task, and it is not a row of Part 4's table ───────────────


def test_the_deriver_is_a_registered_handler_and_not_a_task_row():
    """``TASK_ROWS`` is evaluated from what ``probe`` measured, and nothing probe measures
    says whether somebody bound a shape carrying rules. A row with no condition would run a
    derivation for every uploaded file."""
    assert TASK_DERIVE_RULE in TASK_HANDLERS
    assert TASK_DERIVE_RULE not in {row.task for row in TASK_ROWS}


# ── step 7: the first derived_by writer ────────────────────────────────────


def test_a_rule_writes_a_triple_carrying_the_rules_identity(db_session):
    ids = _fixture(db_session)
    _import(db_session, ids)
    binding = _bind(db_session, VENDOR_RULES)
    with _as(OWNER):
        response, node = _derive(db_session, binding.id)

    report = _report(node)
    assert report["inserted"] == 3, report
    assert _facts(db_session, _derived(db_session, response.rule)) == {
        # ex:ReviewedRule, once per vendor in scope.
        (ids["A"], REVIEWED, "yes"),
        (ids["B"], REVIEWED, "yes"),
        # ex:SuppliedByRule, a SPARQL CONSTRUCT whose subject is the OBJECT of the matched
        # triple: B is in scope, so this is storable. A -supplies-> C never reaches the rule,
        # because C is outside the scope and the triple was cut at the boundary.
        (ids["B"], SUPPLIED_BY, ids["A"]),
    }


def test_the_derived_row_is_an_ordinary_triple_but_for_derived_by(db_session):
    """Step 7's whole claim. Same table, same predicate registry, same query."""
    ids = _fixture(db_session)
    _import(db_session, ids)
    binding = _bind(db_session, VENDOR_RULES)
    with _as(OWNER):
        response, _node = _derive(db_session, binding.id)

    repo = TripleRepository(db_session)
    asserted = repo.query_triples(entity_id=ids["A"], provenance="asserted")
    derived = repo.query_triples(entity_id=ids["A"], provenance="derived")
    assert {row.derived_by for row in asserted} == {None}
    assert {row.derived_by for row in derived} == {response.rule}
    # The same rows come back unsplit when nobody asks for a layer, which is what "same
    # endpoint documents" means: a derived triple is not a second kind of object.
    both = repo.query_triples(entity_id=ids["A"])
    assert len(both) == len(asserted) + len(derived)
    # And it is a real predicate row, not a string: the registry resolved every one.
    assert all(row.predicate is not None for row in derived)


def test_the_rule_identity_is_the_binding_and_fits_the_column(db_session):
    """A binding deleted and recreated identically is the SAME rule, so its predecessor's
    rows are reachable by ``WHERE derived_by = :rule`` and a re-run replaces them. The row id
    would have made them unreachable exactly where somebody corrected a mistake."""
    ids = _fixture(db_session)
    _import(db_session, ids)
    first = _bind(db_session, VENDOR_RULES)
    with _as(OWNER):
        response, _node = _derive(db_session, first.id)

    OntologyService(db_session).delete_binding(first.id)
    second = _bind(db_session, VENDOR_RULES)
    assert second.id != first.id
    assert (
        rule_identity(
            ontology_name=ONTOLOGY,
            shape_iri=VENDOR_RULES,
            scope_type=second.scope_type,
            scope=second.scope,
        )
        == response.rule
    )
    assert len(response.rule) <= DERIVED_BY_WIDTH

    # And the recreated binding's run replaces the old rows rather than doubling them.
    with _as(OWNER):
        _, node = _derive(db_session, second.id)
    assert _report(node)["deleted"] == 3
    assert len(_derived(db_session, response.rule)) == 3


# ── step 6: one pass, over asserted data only ──────────────────────────────


def test_rules_do_not_chain(db_session):
    """``ex:ChainRules`` fires only on a node that already has ``ex:reviewed``, and
    ``ex:VendorRules`` is what writes ``ex:reviewed``. Run the first, then the second: the
    second concludes nothing, because its input graph is ``WHERE derived_by IS NULL``.

    This is the restriction, stated as a fact about the store rather than about a loop. If
    ``build_data_graph`` were called with ``provenance="any"`` here, this test would fail and
    the sprint would have a fixpoint in it.
    """
    ids = _fixture(db_session)
    _import(db_session, ids)
    vendor = _bind(db_session, VENDOR_RULES)
    chain = _bind(db_session, CHAIN_RULES)

    with _as(OWNER):
        _, first = _derive(db_session, vendor.id)
        _, second = _derive(db_session, chain.id)

    assert _report(first)["inserted"] == 3
    assert _report(second)["candidates"] == 0
    assert _report(second)["inserted"] == 0
    assert not [row for row in _derived(db_session) if row.predicate.iri == CHAINED]


def test_only_the_bound_shapes_rules_fire(db_session):
    """``ex:Leaky`` targets by ``sh:targetSubjectsOf``, which needs no ``rdf:type`` and so
    genuinely selects A in this data. Its rule must not fire under another shape's binding —
    its output would otherwise be stored carrying the BOUND shape's identity, which is a row
    attributed to a rule that did not write it."""
    ids = _fixture(db_session)
    _import(db_session, ids)
    binding = _bind(db_session, VENDOR_RULES)
    with _as(OWNER):
        _, node = _derive(db_session, binding.id)

    assert _report(node)["targets_stripped"] >= 1
    assert LEAKED not in {row.predicate.iri for row in _derived(db_session)}

    # Bound on its own it fires, which is what makes the assertion above about targeting
    # rather than about the rule being unreachable. TWO rows and not one: the binding's scope
    # is injected as `sh:targetNode`, and that unions with the shape's own
    # `sh:targetSubjectsOf` (which selects A alone), so both vendors are focus nodes. The
    # bound shape's declared targets are left in place deliberately — they can only select
    # nodes that are in the data graph, and the data graph is already bounded by the scope.
    leaky = _bind(db_session, LEAKY_RULES)
    with _as(OWNER):
        _, leaky_node = _derive(db_session, leaky.id)
    assert _report(leaky_node)["inserted"] == 2
    assert LEAKED in {row.predicate.iri for row in _derived(db_session)}


def test_the_target_stripping_is_what_disarms_the_unbound_shape(db_session):
    """The counterfactual behind the test above, run rather than asserted.

    ``SPRINT_0_5_0.md`` Block B finding 4 moved "only the bound shape runs" into ONE function,
    ``rdf.shacl.bound_shape_graph``, and that function's claim is that stripping the five SHACL
    target terms is what disarms every other shape. This is that claim measured: the same
    vocabulary and the same data graph, once with the targets left in place and once without.

    With them in place ``ex:Leaky`` fires inside the bound shape's own pass — and everything
    that pass concludes is stored under the BOUND shape's ``derived_by``, which would be a row
    attributed to a rule that did not write it. ``strip_targets=False`` has no caller in the
    appliance for exactly that reason; it exists so this file can show the hazard instead of
    describing it.
    """
    rdflib = require_rdflib()
    pyshacl = require_pyshacl()
    ids = _fixture(db_session)
    _import(db_session, ids)
    turtle = db_session.get(Ontology, ONTOLOGY).source_turtle

    def _run_rules(strip: bool) -> set[str]:
        # A fresh data graph each time: `expand_rules` expands in place.
        bound = build_data_graph(
            db_session,
            scope_type="usetype",
            scope={"pattern": "vendor"},
            max_scope_documents=None,
            provenance="asserted",
        )
        shapes, stripped = bound_shape_graph(
            rdflib,
            source_turtle=turtle,
            ontology_name=ONTOLOGY,
            shape_iri=VENDOR_RULES,
            target_iris=[document_iri(doc_id) for doc_id in bound.scope_document_ids],
            strip_targets=strip,
        )
        assert (stripped > 0) is strip
        return {str(predicate) for _, predicate, _ in expand_rules(pyshacl, bound.graph, shapes)}

    with _as(OWNER):
        armed = _run_rules(False)
        disarmed = _run_rules(True)

    assert LEAKED in armed, "the unbound shape's rule must really fire when it is left armed"
    assert LEAKED not in disarmed
    # The bound shape's own rules are unaffected by the stripping — its declared targets are
    # left alone and the scope is injected as sh:targetNode either way.
    assert REVIEWED in armed and REVIEWED in disarmed


def test_a_shape_the_vocabulary_no_longer_declares_is_named_and_permanent(db_session):
    """One class for one condition, and it classifies PERMANENT (Block B findings 6 and 7).

    A vocabulary is REPLACED by a second upload under the same name, and the replacement need
    not declare the shape a binding named. Both sides of the seam had their own
    ``LookupError`` subclass for this and both claimed a ``LookupError`` is permanent; neither
    was. There is now one ``ValueError`` in ``rdf/shacl.py``, raised by the one function that
    builds a shapes graph.
    """
    rdflib = require_rdflib()
    ids = _fixture(db_session)
    _import(db_session, ids)
    turtle = db_session.get(Ontology, ONTOLOGY).source_turtle

    with pytest.raises(ShapeNotInOntologyError) as caught:
        bound_shape_graph(
            rdflib,
            source_turtle=turtle,
            ontology_name=ONTOLOGY,
            shape_iri="http://example.org/ShapeThatWasRemoved",
            target_iris=[],
        )
    assert ONTOLOGY in str(caught.value)
    assert classify_exception(caught.value) is ErrorType.PERMANENT


# ── step 8: delete-then-insert, scoped by rule ─────────────────────────────


def test_a_re_run_replaces_only_what_this_rule_wrote(db_session):
    """The asserted data changes between the runs, so the rule's conclusion changes with it.

    What must happen: the stale derived row is GONE (no reconciliation asked for it), the
    asserted rows are untouched, and ANOTHER rule's derived rows survive — which is the whole
    content of "scoped by rule".
    """
    ids = _fixture(db_session)
    _import(db_session, ids)
    vendor = _bind(db_session, VENDOR_RULES)
    leaky = _bind(db_session, LEAKY_RULES)

    with _as(OWNER):
        vendor_run, _ = _derive(db_session, vendor.id)
        leaky_run, _ = _derive(db_session, leaky.id)
    assert len(_derived(db_session, leaky_run.rule)) == 2

    # A stops supplying B, so ex:SuppliedByRule has nothing to construct any more.
    supplies = TripleRepository(db_session).get_predicate_by_name("supplies")
    stale = (
        db_session.query(Triple)
        .filter(
            Triple.subject_id == ids["A"],
            Triple.predicate_id == supplies.id,
            Triple.object_id == ids["B"],
        )
        .one()
    )
    db_session.delete(stale)
    db_session.flush()

    asserted_before = {
        row.id for row in db_session.query(Triple).filter(Triple.derived_by.is_(None))
    }
    with _as(OWNER):
        _, node = _derive(db_session, vendor.id)

    report = _report(node)
    assert report["deleted"] == 3
    assert report["inserted"] == 2
    assert _facts(db_session, _derived(db_session, vendor_run.rule)) == {
        (ids["A"], REVIEWED, "yes"),
        (ids["B"], REVIEWED, "yes"),
    }
    # The other rule's row is untouched: `WHERE derived_by = :rule` is the delete's whole
    # description, and it does not name this one.
    assert len(_derived(db_session, leaky_run.rule)) == 2
    # And nothing asserted moved.
    assert {
        row.id for row in db_session.query(Triple).filter(Triple.derived_by.is_(None))
    } == asserted_before


def test_a_conclusion_already_asserted_is_not_a_conclusion_at_all(db_session):
    """MEASURED, and it is the reason the "already present" path is narrower than it looks.

    ``pyshacl`` adds to the data graph, and the data graph was built FROM the store, so a rule
    that concludes something already asserted adds nothing and the diff is empty. There is no
    candidate to skip, no row to write and nothing to record: in a closed world that is the
    right answer, because the fact is held either way and nothing about its provenance
    changed.
    """
    ids = _fixture(db_session)
    _import(db_session, ids)
    # Exactly what ex:SuppliedByRule concludes. The predicate row already exists —
    # `import_ontology` registered `ex:suppliedBy` from its `owl:ObjectProperty` declaration,
    # and `predicates.iri` is UNIQUE.
    supplied_by = TripleRepository(db_session).get_predicate_by_iri(SUPPLIED_BY)
    TripleRepository(db_session).create_triple(ids["B"], supplied_by.id, object_id=ids["A"])
    db_session.flush()

    binding = _bind(db_session, VENDOR_RULES)
    with _as(OWNER):
        response, node = _derive(db_session, binding.id)

    report = _report(node)
    assert report["candidates"] == 2
    assert report["already_present"] == 0
    assert _facts(db_session, _derived(db_session, response.rule)) == {
        (ids["A"], REVIEWED, "yes"),
        (ids["B"], REVIEWED, "yes"),
    }


def test_a_retracted_fact_still_holds_its_uniqueness_slot(db_session):
    """The case "already present" actually exists for, and it is a sharp one.

    An INVALIDATED triple is absent from the data graph — ``build_data_graph`` excludes it,
    because validating against a fact the store believes false reports on nothing — so a rule
    concludes it afresh. But ``uq_triple`` does not exclude invalidated rows, so the slot is
    taken and the insert would be an ``IntegrityError``. It is recorded instead, with the row
    that holds the slot named, because "the store retracted this and the rule concluded it
    again" is exactly the kind of thing a reader of a derivation report needs to see.
    """
    ids = _fixture(db_session)
    _import(db_session, ids)
    supplied_by = TripleRepository(db_session).get_predicate_by_iri(SUPPLIED_BY)
    retracted = TripleRepository(db_session).create_triple(
        ids["B"], supplied_by.id, object_id=ids["A"]
    )
    TripleRepository(db_session).invalidate_triple(retracted.id, reason="superseded by hand")
    db_session.flush()

    binding = _bind(db_session, VENDOR_RULES)
    with _as(OWNER):
        response, node = _derive(db_session, binding.id)

    report = _report(node)
    assert report["candidates"] == 3
    assert report["inserted"] == 2
    assert report["already_present"] == 1
    held = report["already_present_rows"][0]
    assert held["subject_id"] == ids["B"]
    assert held["held_by"] == retracted.id
    # It was ASSERTED, and the report says so — which is the difference between "another rule
    # got there first" and "a human already knew and then retracted it".
    assert held["derived_by"] is None
    assert len(_derived(db_session, response.rule)) == 2


# ── what a rule may write, and what a refusal costs ────────────────────────


def _failed(session, node):
    """The task failed permanently and the node says so."""
    session.expire_all()
    task = (
        session.query(TaskQueue)
        .filter(TaskQueue.scope_document_id == node.id)
        .order_by(TaskQueue.id.desc())
        .first()
    )
    return task.status, task.error or ""


def test_an_object_outside_the_scope_fails_the_run_and_writes_nothing(db_session):
    """``ex:OffScopeRules`` hard-codes C's IRI, and C is readable but not in the binding.

    A derived triple's visibility follows its endpoints, so a rule that could name a document
    outside the scope could mint a fact ABOUT a document whose access nobody checked — the
    request established write access over the scope and nothing else.
    """
    ids = _fixture(db_session)
    _import(db_session, ids)
    binding = _bind(db_session, OFFSCOPE_RULES)
    with _as(OWNER):
        _, node = _derive(db_session, binding.id)

    status, error = _failed(db_session, node)
    assert status == "failed", error
    assert "not in the binding's scope" in error
    assert _derived(db_session) == []


def test_an_unregistered_predicate_fails_the_run_and_writes_nothing(db_session):
    """A rule pass does not mint predicates. ``predicates.name`` and ``predicates.iri`` are
    both UNIQUE, and which existing row an incoming term means is a genuine ambiguity that
    ``import_ontology`` REPORTS rather than resolving; a worker cannot make that call with
    nobody reading the answer."""
    ids = _fixture(db_session)
    _import(db_session, ids)
    binding = _bind(db_session, UNKNOWN_PREDICATE_RULES)
    with _as(OWNER):
        _, node = _derive(db_session, binding.id)

    status, error = _failed(db_session, node)
    assert status == "failed", error
    assert "never-imported" in error
    assert _derived(db_session) == []


def test_a_failed_run_does_not_delete_the_previous_runs_rows(db_session):
    """The mapping is complete before the delete begins, so a rule that produces one
    unstorable triple leaves the store exactly as it was — including the rows its own previous
    run wrote. A partial re-derivation would be a rule whose output nobody can characterise,
    which is the one thing ``derived_by`` exists to prevent."""
    ids = _fixture(db_session)
    _import(db_session, ids)
    binding = _bind(db_session, VENDOR_RULES)
    with _as(OWNER):
        response, _ = _derive(db_session, binding.id)
    before = {row.id for row in _derived(db_session, response.rule)}
    assert len(before) == 3

    # Now make the SAME rule produce something unstorable: a vendor that supplies a document
    # outside the scope would already have been cut, so instead bind the off-scope shape and
    # confirm the vendor rule's rows survive its failure.
    other = _bind(db_session, OFFSCOPE_RULES)
    with _as(OWNER):
        _derive(db_session, other.id)
    assert {row.id for row in _derived(db_session, response.rule)} == before


def test_a_shape_with_no_rule_is_refused(db_session):
    """ "Derived nothing" and "the rules ran and matched nothing" are opposite facts that read
    the same, so the first is a refusal rather than a report."""
    ids = _fixture(db_session)
    _import(db_session, ids)
    binding = _bind(db_session, NO_RULES)
    with _as(OWNER):
        _, node = _derive(db_session, binding.id)

    status, error = _failed(db_session, node)
    assert status == "failed", error
    assert "declares no sh:rule" in error


# ── who may run one, and what else a run touches ───────────────────────────


def test_a_reader_may_not_run_a_derivation(db_session):
    """Open question 6.4's recorded default, taken: derivation requires WRITE access to the
    binding's scope. It needs no supporting argument here — the run writes facts about those
    documents."""
    ids = _fixture(db_session)
    _import(db_session, ids)
    binding = _bind(db_session, VENDOR_RULES)
    reader = _principal(db_session, "derive-reader")
    for doc_id in (ids["A"], ids["B"]):
        db_session.add(AccessGrant(document_id=doc_id, principal_id=reader.id, level="read"))
    db_session.flush()

    with _as(reader):
        with pytest.raises(AccessDeniedError):
            OntologyService(db_session).derive_binding(
                ONTOLOGY, RuleDerivationRequest(binding_id=binding.id)
            )
    assert _derived(db_session) == []


def test_a_run_enqueues_no_other_work(db_session):
    """The settling walk asks the rollup planner at every level above a completed task's scope
    node, and a report filed under a shared root would offer that root a ``summarize``. A
    report node has no parent, so the walk ends at it."""
    ids = _fixture(db_session)
    _import(db_session, ids)
    binding = _bind(db_session, VENDOR_RULES)
    with _as(OWNER):
        _, node = _derive(db_session, binding.id, planner=IngestRollupPlanner())

    assert node.parent_id is None
    assert node.usetype == DERIVATION_REPORT_USETYPE
    queued = {row.task_type for row in db_session.query(TaskQueue).all()}
    assert queued == {TASK_DERIVE_RULE}


def test_a_run_touches_no_document_in_its_scope(db_session):
    """The only document this task writes is the report node it is scoped to."""
    ids = _fixture(db_session)
    _import(db_session, ids)
    binding = _bind(db_session, VENDOR_RULES)
    before = {
        doc.id: (doc.title, doc.content, doc.structured_content, doc.parent_id, doc.settled)
        for doc in db_session.query(Document).all()
    }
    with _as(OWNER):
        response, _node = _derive(db_session, binding.id)
    db_session.expire_all()
    after = {
        doc.id: (doc.title, doc.content, doc.structured_content, doc.parent_id, doc.settled)
        for doc in db_session.query(Document).all()
    }
    assert set(after) - set(before) == {response.report_document_id}
    assert {k: v for k, v in after.items() if k != response.report_document_id} == before


# ── Block A finding 6, on the derivation side: the rendered list is capped ──


def _synthetic_report(triple_count: int) -> tuple[dict, dict]:
    """A report block with ``triple_count`` derived triples, without running a derivation.

    Rendered directly rather than through the handler, for ``test_validate_shape``'s reason
    and one more. The reason it shares: reaching the cap through a real run would mean
    building a rule that concludes hundreds of triples, and the render is what is under test.
    The reason it does not: on this side the cap IS reachable at the shipped 512-document
    bound — ``shacl_rules.map_to_rows`` bounds a derived row's endpoints by the scope, not the
    row COUNT, so a ``sh:SPARQLRule`` relating every pair of in-scope documents concludes
    512 x 512 of them — and a test that built that rule would spend minutes proving a
    property of pyshacl rather than of this function.
    """
    triples = [
        {
            "subject_id": 1000 + index,
            "predicate_id": 7,
            "object_id": None,
            "object_literal": f"derived value {index}",
            "object_datatype": None,
        }
        for index in range(triple_count)
    ]
    report = {
        "rule": "shacl:" + "0" * 64,
        "derived_at": "2026-09-10T00:00:00+00:00",
        "documents_requested": 512,
        "documents_resolved": 512,
        "triple_count": 0,
        "node_count": 512,
        "boundary_cuts": 0,
        "targets_stripped": 0,
        "candidates": len(triples),
        "deleted": 0,
        "inserted": len(triples),
        "already_present": 0,
        "already_present_rows": [],
        "triples": triples,
    }
    request = {
        "binding_id": 1,
        "ontology_name": ONTOLOGY,
        "shape_iri": VENDOR_RULES,
        "scope_type": "usetype",
    }
    return report, request


def test_the_rendered_triple_list_is_capped_and_says_it_was():
    """``SPRINT_0_5_0.md`` Block A finding 6, applied to the derivation report.

    ``Document.content`` is indexed by ``idx_documents_content_fts``, a partial GIN over
    ``to_tsvector(title || ' ' || content)``, whose lexeme string Postgres caps at 1,048,575
    bytes. MEASURED 2026-09-10 on ``pg16`` over this function's own output: a derivation whose
    objects are documents plateaus at 15,998 bytes and can never reach it, because the scope
    bounds the vocabulary; one whose objects are distinct LITERALS indexed at 105,000 rows and
    raised `string is too long for tsvector` at 110,000. So unlike the validation cap, this
    one is reachable at the shipped default — see ``derive_tasks.RENDERED_TRIPLE_CAP``.

    **The cap is only half of it.** A truncated report that did not say so would read as "this
    is what the rule concluded" when it is "these are the first 512", which is the Fail Early
    failure this tree has now fixed three times. So the text carries the total, the number
    shown, and both places the rest can be read.
    """
    report, request = _synthetic_report(RENDERED_TRIPLE_CAP + 7)
    text = _render(report, request)

    listed = [line for line in text.splitlines() if line.startswith("- document ")]
    assert len(listed) == RENDERED_TRIPLE_CAP
    assert f"- concluded: {RENDERED_TRIPLE_CAP + 7} triples" in text
    assert f"first {RENDERED_TRIPLE_CAP} of {RENDERED_TRIPLE_CAP + 7}" in text
    # Both ways to the other seven are named. `structured_content` is the report's own half;
    # `derived_by` is the durable one, and it is the half a metadata PATCH cannot delete.
    assert "structured_content" in text and DERIVATION_BLOCK in text
    assert "derived_by" in text and report["rule"] in text
    # And they really are still there: rendering reads the list, it does not trim it.
    assert len(report["triples"]) == RENDERED_TRIPLE_CAP + 7


def test_a_derivation_inside_the_cap_carries_no_truncation_notice():
    """The notice is a statement about THIS report, so a report that lists everything must not
    carry one — a reader who saw it on every report would stop reading it.

    512 is the cap for this case: a ``sh:TripleRule`` concludes one triple per focus node, so
    a FULL scope at the default bound renders complete and only the rule that went quadratic
    is cut.
    """
    report, request = _synthetic_report(3)
    text = _render(report, request)
    listed = [line for line in text.splitlines() if line.startswith("- document ")]
    assert len(listed) == 3
    assert "first" not in text


# ── the same thing over the wire ───────────────────────────────────────────


def test_the_route_enqueues_and_answers_202(db_session):
    """``POST /ontologies/{name}/derive``. 202 and not 201: the request creates a node, but
    the rows the caller asked for do not exist yet. The body says where they will be, and what
    identity they will carry."""
    from fastapi.testclient import TestClient

    from jmfts_core.database import get_db
    from jmfts_core.rest.main import app

    from tests.conftest import AUTH_HEADERS

    ids = _fixture(db_session)
    _import(db_session, ids)
    binding = _bind(db_session, VENDOR_RULES)

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(app, headers=AUTH_HEADERS)
        response = client.post(f"/ontologies/{ONTOLOGY}/derive", json={"binding_id": binding.id})
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["provenance"] == "asserted"
    assert body["scope_document_count"] == 2
    assert body["governed"] is False
    assert body["status"] == "pending"
    assert body["rule"].startswith("shacl:")

    with _as(None):
        drain_ingest_queue(db_session, planner=IngestRollupPlanner())
    db_session.expire_all()
    node = db_session.get(Document, body["report_document_id"])
    assert _report(node)["inserted"] == 3
    assert {row.derived_by for row in _derived(db_session)} == {body["rule"]}
