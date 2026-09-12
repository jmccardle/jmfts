"""A derived triple's visibility follows its ENDPOINTS. ``SPRINT_0_5_0.md`` Block B step 9.

**The hazard, stated before anything is built.** A rule reads a restricted document and writes
a triple between two public entities. Nothing about the derived row records that a restricted
document was ever involved — ``derived_by`` names the RULE, not the input — so the row is as
visible as its two public endpoints, and a restricted fact has become a public one. That is
``SPRINT_0_3_0.md`` 13.9's exact shape reached by derivation instead of by resolution, and it
is the third route this codebase has reached it by: 13.9 was resolution,
``tests/test_raptor_structure.py`` was structure, this is derivation.

**What actually happens, and what prevents it.** The leak does NOT occur, and two separate
mechanisms stand between the rule and it. Neither is sufficient alone, and this file proves
both by removing them:

1. ``shacl_rules._document_id`` raises
   :class:`~jmfts_core.shacl_rules.TermOutOfScopeError` unless BOTH endpoints of a derived
   triple are in the pinned scope. Without it a rule could name any document in the
   appliance — including one the caller cannot read — and mint a fact about it.
2. ``ontology_service._pin_scope`` raises
   :class:`~jmfts_core.services.ontology_service.ScopeAccessNotUniformError` unless every
   document in the scope has the same access key. Without it, mechanism 1 is satisfied by the
   scope in the paragraph above — restricted R and public P1, P2 are all inside it — and the
   derived triple between the two public documents is publicly readable.
   ``test_the_uniformity_refusal_is_the_only_thing_standing_there`` bypasses the request and
   shows exactly that, because a test that only asserted the 422 would be asserting that a
   door is locked without checking that it is the door.

Together they are a proof rather than a pair of checks: uniformity means every document in the
scope has one access; endpoints-in-scope means every derived triple names only those
documents; so a derived row's access is identically the access of everything the rule read.

**What is NOT proven here, and belongs to whoever widens 6.4.** The uniformity refusal exists
because a report NODE can carry one access. It is doing double duty as this proof, and if it
is ever relaxed — a per-document report set, say — mechanism 2 disappears and this hazard
comes back with mechanism 1 alone, which does not close it.
"""

from contextlib import contextmanager

import pytest

from jmfts_client.contracts.rdf import RuleDerivationRequest, ShapeBindingCreate
from jmfts_core.access import can_read
from jmfts_core.derive_tasks import (
    DERIVATION_BLOCK,
    DERIVATION_REPORT_USETYPE,
    PARAM_BASE_IRI,
    PARAM_BINDING_ID,
    PARAM_DOCUMENT_IDS,
    PARAM_RULE,
)
from jmfts_core.ingest_tasks import TASK_DERIVE_RULE
from jmfts_core.models.document import SETTLED_IN_FLIGHT, Document
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.models.task_queue import WRITE_SELF, TaskQueue
from jmfts_core.models.triple import Triple
from jmfts_core.principal_context import OWNER, CurrentPrincipal, reset_principal, set_principal
from jmfts_core.rdf.names import DEFAULT_BASE_IRI, document_iri
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.repositories.triple import TripleRepository
from jmfts_core.services.ontology_service import OntologyService, ScopeAccessNotUniformError
from jmfts_core.shacl_rules import rule_identity

from tests.conftest import drain_ingest_queue

ONTOLOGY = "leak-rules"
LEAK_RULES = "http://example.org/LeakRules"
REACH_RULES = "http://example.org/ReachRules"
CO_SUPPLIED = "http://example.org/coSupplied"
SUPPLIES = "urn:jmfts:predicate:supplies"


def _turtle(outside_id: int) -> str:
    """``ex:LeakRules`` is the rule from the module docstring, written to leak if it can.

    Its focus node is the document the ``supplies`` triples hang off — the RESTRICTED one —
    and everything it writes is between two OTHER documents. Nothing in the output names the
    document it read, which is the whole hazard: a reader of the derived row has no way to
    know a restricted document was involved.
    """
    return f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix ex: <http://example.org/> .

ex:coSupplied a owl:ObjectProperty .
ex:reaches a owl:ObjectProperty .

ex:LeakRules a sh:NodeShape ;
    sh:rule [
        a sh:SPARQLRule ;
        sh:construct \"\"\"
            CONSTRUCT {{ ?a <{CO_SUPPLIED}> ?b }}
            WHERE {{
                $this <{SUPPLIES}> ?a .
                $this <{SUPPLIES}> ?b .
                FILTER(?a != ?b)
            }}
        \"\"\" ;
    ] .

ex:ReachRules a sh:NodeShape ;
    sh:rule [
        a sh:TripleRule ;
        sh:subject sh:this ;
        sh:predicate ex:reaches ;
        sh:object <{document_iri(outside_id)}> ;
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


def _grant(session, document_id, principal, level="write"):
    session.add(AccessGrant(document_id=document_id, principal_id=principal.id, level=level))
    session.flush()


def _corpus(session):
    """R restricted, P1 and P2 public, and one document nobody but the insider may read.

        R  -supplies-> P1
        R  -supplies-> P2

    So a rule whose focus node is R can conclude ``P1 coSupplied P2`` — a fact between two
    PUBLIC documents, derived entirely from a restricted one.
    """
    docs = DocumentRepository(session)
    triples = TripleRepository(session)

    r = docs.create(title="R", content="restricted", usetype="vendor")
    p1 = docs.create(title="P1", content="public one", usetype="vendor")
    p2 = docs.create(title="P2", content="public two", usetype="vendor")
    hidden = docs.create(title="H", content="hidden and out of scope", usetype="other")
    session.flush()

    supplies = triples.create_predicate(name="supplies")
    triples.create_triple(r.id, supplies.id, object_id=p1.id)
    triples.create_triple(r.id, supplies.id, object_id=p2.id)
    session.flush()
    return {"R": r.id, "P1": p1.id, "P2": p2.id, "H": hidden.id}


def _import(session, ids):
    OntologyService(session).import_ontology(
        _turtle(ids["H"]), name=ONTOLOGY, base_iri=DEFAULT_BASE_IRI
    )


def _bind(session, shape_iri, *, usetype_pattern="vendor"):
    return OntologyService(session).create_binding(
        ONTOLOGY, ShapeBindingCreate(shape_iri=shape_iri, usetype_pattern=usetype_pattern)
    )


def _derived(session):
    return session.query(Triple).filter(Triple.derived_by.is_not(None)).all()


# ── the hazard, and the refusal that closes it ─────────────────────────────


def test_a_scope_that_mixes_restricted_and_public_is_refused(db_session):
    """The brief's scenario, run for real: R restricted to the insider, P1 and P2 public, one
    binding over all three, and a rule that would write a public triple out of restricted
    data. The run never starts."""
    ids = _corpus(db_session)
    _import(db_session, ids)
    insider = _principal(db_session, "leak-insider")
    _grant(db_session, ids["R"], insider)
    binding = _bind(db_session, LEAK_RULES)

    with _as(insider):
        with pytest.raises(ScopeAccessNotUniformError) as raised:
            OntologyService(db_session).derive_binding(
                ONTOLOGY, RuleDerivationRequest(binding_id=binding.id)
            )
    assert "governed" in str(raised.value)
    assert _derived(db_session) == []
    assert db_session.query(TaskQueue).count() == 0


def test_the_uniformity_refusal_is_the_only_thing_standing_there(db_session):
    """**A characterisation of the mechanism, not a wish.** The endpoint's uniformity check is
    bypassed here — the task row is written by hand with the mixed scope the request would
    have refused — and the leak then happens exactly as the hazard describes: an outsider with
    no grant anywhere reads a triple whose every input was restricted.

    This is what makes the test above meaningful. Asserting a 422 proves a door is locked; this
    proves it is the door. If somebody relaxes ``ScopeAccessNotUniformError`` — a per-document
    report set would, and that is a live alternative — THIS is what comes back, and mechanism 1
    (endpoints-in-scope) does not stop it, because all three documents are in the scope.
    """
    ids = _corpus(db_session)
    _import(db_session, ids)
    insider = _principal(db_session, "bypass-insider")
    outsider = _principal(db_session, "bypass-outsider")
    _grant(db_session, ids["R"], insider)
    binding = _bind(db_session, LEAK_RULES)

    rule = rule_identity(
        ontology_name=ONTOLOGY,
        shape_iri=LEAK_RULES,
        scope_type=binding.scope_type,
        scope=binding.scope,
    )
    node = DocumentRepository(db_session).create(
        title="bypass",
        content=None,
        parent_id=None,
        usetype=DERIVATION_REPORT_USETYPE,
        structured_content={DERIVATION_BLOCK: {}},
        auto_embed=False,
        embed_tokens=False,
        settled=SETTLED_IN_FLIGHT,
        produced_by=TASK_DERIVE_RULE,
    )
    TaskQueueRepository(db_session).enqueue(
        TASK_DERIVE_RULE,
        node.id,
        WRITE_SELF,
        params={
            PARAM_BINDING_ID: binding.id,
            # The mixed scope. `_pin_scope` would never have produced this list.
            PARAM_DOCUMENT_IDS: [ids["R"], ids["P1"], ids["P2"]],
            PARAM_BASE_IRI: DEFAULT_BASE_IRI,
            PARAM_RULE: rule,
        },
    )
    with _as(None):
        drain_ingest_queue(db_session)
    db_session.expire_all()

    leaked = [row for row in _derived(db_session) if row.predicate.iri == CO_SUPPLIED]
    assert leaked, "the rule concluded nothing, so this test proves nothing"
    assert {(row.subject_id, row.object_id) for row in leaked} == {
        (ids["P1"], ids["P2"]),
        (ids["P2"], ids["P1"]),
    }

    # And the leak, demonstrated rather than argued: the outsider holds no grant anywhere and
    # cannot read R, yet reads a fact that exists only because R does.
    with _as(outsider):
        assert can_read(db_session, db_session.get(Document, ids["R"])) is False
        visible = TripleRepository(db_session).query_triples(
            entity_id=ids["P1"], provenance="derived"
        )
    # Both directions of the co-supply, read by somebody who may read neither input.
    assert len(visible) == 2
    assert {row.predicate.iri for row in visible} == {CO_SUPPLIED}


# ── mechanism 1: both endpoints inside the pinned scope ────────────────────


def test_a_rule_may_not_name_a_document_the_caller_never_established_access_to(db_session):
    """``ex:ReachRules`` hard-codes H's IRI. H is outside the binding's scope, so nobody
    checked whether this caller may write it — or read it. The run fails and writes nothing.

    Mechanism 1, and the reason it is an access rule rather than a tidiness one: without it a
    rule could mint a fact ABOUT any document in the appliance, and a fact about a document is
    visible to everyone who can read that document.
    """
    ids = _corpus(db_session)
    _import(db_session, ids)
    binding = _bind(db_session, REACH_RULES)

    with _as(OWNER):
        response = OntologyService(db_session).derive_binding(
            ONTOLOGY, RuleDerivationRequest(binding_id=binding.id)
        )
        with _as(None):
            drain_ingest_queue(db_session)
    db_session.expire_all()

    task = (
        db_session.query(TaskQueue)
        .filter(TaskQueue.scope_document_id == response.report_document_id)
        .one()
    )
    assert task.status == "failed", task.error
    assert "not in the binding's scope" in (task.error or "")
    assert _derived(db_session) == []


# ── the property itself: visibility follows the endpoints ──────────────────


def test_a_derived_triple_is_exactly_as_visible_as_its_endpoints(db_session):
    """A uniform, restricted scope. The rule derives a triple between two restricted
    documents, and the row is invisible to a principal with no grant — by the same
    ``readable_id_subset`` filter over ``query_triples`` that hides an asserted one, with no
    branch on ``derived_by`` anywhere in it.

    This is step 7's "same access rules" made falsifiable. It is also why ``derived_by`` is
    NOT an access dimension: a rule's identity says nothing about who may read what it wrote.
    """
    ids = _corpus(db_session)
    _import(db_session, ids)
    insider = _principal(db_session, "uniform-insider")
    outsider = _principal(db_session, "uniform-outsider")
    for key in ("R", "P1", "P2"):
        _grant(db_session, ids[key], insider)
    binding = _bind(db_session, LEAK_RULES)

    with _as(insider):
        response = OntologyService(db_session).derive_binding(
            ONTOLOGY, RuleDerivationRequest(binding_id=binding.id)
        )
        with _as(None):
            drain_ingest_queue(db_session)
    db_session.expire_all()

    assert response.governed is True
    rows = _derived(db_session)
    assert {(row.subject_id, row.object_id) for row in rows} == {
        (ids["P1"], ids["P2"]),
        (ids["P2"], ids["P1"]),
    }

    repo = TripleRepository(db_session)
    with _as(insider):
        assert len(repo.query_triples(entity_id=ids["P1"], provenance="derived")) == 2
    with _as(outsider):
        assert repo.query_triples(entity_id=ids["P1"], provenance="derived") == []
        # And not by a filter that happens to catch the derived layer: the ASSERTED triples
        # about the same documents are hidden identically, which is the point.
        assert repo.query_triples(entity_id=ids["P1"]) == []


def test_the_report_node_is_as_restricted_as_the_scope_it_derived_from(db_session):
    """The report lists every triple the rule wrote, so it is a second copy of the same facts
    in a full-text searchable document. It carries the scope's grants for that reason."""
    ids = _corpus(db_session)
    _import(db_session, ids)
    insider = _principal(db_session, "report-insider")
    outsider = _principal(db_session, "report-outsider")
    for key in ("R", "P1", "P2"):
        _grant(db_session, ids[key], insider)
    binding = _bind(db_session, LEAK_RULES)

    with _as(insider):
        response = OntologyService(db_session).derive_binding(
            ONTOLOGY, RuleDerivationRequest(binding_id=binding.id)
        )
        with _as(None):
            drain_ingest_queue(db_session)
    db_session.expire_all()

    node = db_session.get(Document, response.report_document_id)
    assert str(ids["P1"]) in node.content
    grants = db_session.query(AccessGrant).filter(AccessGrant.document_id == node.id).all()
    assert {(g.principal_id, g.level) for g in grants} == {(insider.id, "write")}
    with _as(outsider):
        assert can_read(db_session, node) is False
    with _as(insider):
        assert can_read(db_session, node) is True


def test_an_unreadable_document_never_reaches_the_scope_at_all(db_session):
    """The first line of defence, and the cheapest: a document the caller cannot READ is not
    in the resolved scope, so it is neither an input to the rule nor a legal endpoint of its
    output. Here the outsider's scope is P1 and P2 alone — R is absent, so the rule's focus
    set does not include it and there is nothing for it to conclude.

    This is what makes ``ScopeAccessNotUniformError`` reachable only in the direction that
    matters: a caller can never widen a scope past what they may read, only find that what
    they may read is governed two ways.
    """
    ids = _corpus(db_session)
    _import(db_session, ids)
    insider = _principal(db_session, "hidden-insider")
    outsider = _principal(db_session, "hidden-outsider")
    _grant(db_session, ids["R"], insider)
    binding = _bind(db_session, LEAK_RULES)

    with _as(outsider):
        response = OntologyService(db_session).derive_binding(
            ONTOLOGY, RuleDerivationRequest(binding_id=binding.id)
        )
        with _as(None):
            drain_ingest_queue(db_session)
    db_session.expire_all()

    task = (
        db_session.query(TaskQueue)
        .filter(TaskQueue.scope_document_id == response.report_document_id)
        .one()
    )
    assert task.params[PARAM_DOCUMENT_IDS] == [ids["P1"], ids["P2"]]
    assert response.scope_document_count == 2
    assert task.status == "completed", task.error
    # Nothing derived: the rule's focus nodes are P1 and P2, and neither supplies anything.
    assert _derived(db_session) == []
