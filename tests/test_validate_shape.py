"""A bound shape that runs, and the report it leaves behind. ``SPRINT_0_5_0.md`` Block A 2–4.

Three claims, and the third is the one this file exists for.

**The bound shape runs against the bound scope.** ``jmfts_core/rdf/shacl.py`` builds the data
graph; ``jmfts_core/validate_tasks.py`` is ``require_pyshacl()``'s first caller and makes the
binding's documents the shape's ``sh:targetNode`` focus set. Without that last step a shape is
selected by ``sh:targetClass`` against ``rdf:type`` triples the store does not assert, and
every run would report ``conforms`` over an empty target set — a clean bill of health meaning
"nothing was checked", which is the exact failure the step exists to avoid.

**VALIDATION NEVER MUTATES.** ``services/ontology_service.py:11`` says an uploaded shape
"constrains nothing, and even bound it changes no existing row", and Block A keeps it:
``test_validation_changes_no_data_row`` snapshots every document, triple and link either side
of a run, and ``test_a_run_enqueues_no_other_work`` drains with the REAL rollup planner to
prove the settling walk cannot turn a validation into a summarization. Block B is what writes,
deliberately and under a column built for it.

**A report is as restricted as what it reports on.** ``SPRINT_0_5_0.md`` 3.4: a derived node
inherits the access of its SOURCES. The scope is resolved under the caller's filter and pinned
to the task row, because a worker holds no principal; the report node carries grants matching
that access; a scope governed two ways is refused rather than reported into one node.

The fixture, deliberately awkward at the boundary:

    A, B    usetype 'vendor'  — the scope, under a 'vendor' binding
    C       usetype 'other'   — readable, and OUTSIDE the scope

    A -name->    "Acme" (literal)   → in the graph
    A -country-> C                  → leaves the scope: a BoundaryCut on A
    B -name->    "Beta" (literal)   → in the graph
    B has no country at all         → a genuine violation

So a shape requiring one ``country`` per vendor reports two violations that look identical and
are not: A's is an artefact of the scope bound and is marked (open question 6.2), B's is real.
"""

from contextlib import contextmanager

import pytest

from jmfts_client.contracts.rdf import ShapeBindingCreate, ShapeValidationRequest
from jmfts_core.access import AccessDeniedError
from jmfts_core.config import get_settings
from jmfts_core.ingest_tasks import TASK_HANDLERS, TASK_ROWS, TASK_VALIDATE_SHAPE
from jmfts_core.models.document import Document, DocumentLink
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.models.task_queue import TaskQueue
from jmfts_core.models.triple import Triple
from jmfts_core.principal_context import OWNER, CurrentPrincipal, reset_principal, set_principal
from jmfts_core.rdf.names import DEFAULT_BASE_IRI, document_iri, predicate_iri
from jmfts_core.rdf.shacl import ScopeTooLargeError
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository
from jmfts_core.rollup_tasks import IngestRollupPlanner
from jmfts_core.services.ontology_service import (
    OntologyService,
    ScopeAccessNotUniformError,
    ScopeEmptyError,
)
from jmfts_core.validate_tasks import (
    PARAM_DOCUMENT_IDS,
    RENDERED_VIOLATION_CAP,
    VALIDATION_BLOCK,
    VALIDATION_REPORT_USETYPE,
    _render,
)

from tests.conftest import drain_ingest_queue

ONTOLOGY = "vendor-shapes"
VENDOR_SHAPE = "http://example.org/VendorShape"
NOTHING = predicate_iri("nothing-has-this")
NAME = predicate_iri("name")
COUNTRY = predicate_iri("country")

#: Two shapes, and the second one is a trap for two separate mistakes.
#:
#: ``ex:CountryProperty`` is a NAMED property shape rather than a blank node, which
#: ``rdflib``'s concise bounded description does not follow: a subgraph built with ``cbd()``
#: would validate ``VendorShape`` as a node shape with one property and report nothing about
#: ``country``. ``ex:OtherShape`` carries its own ``sh:targetNode`` and a constraint every
#: document fails, so a run that pulled the whole vocabulary into the shapes graph instead of
#: the bound shape alone would report violations nobody asked for.
TURTLE = f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.org/> .
@prefix prop: <urn:jmfts:predicate:> .

ex:VendorShape a sh:NodeShape ;
    sh:property [ sh:path prop:name ; sh:minCount 1 ] ;
    sh:property ex:CountryProperty .

ex:CountryProperty
    sh:path prop:country ;
    sh:minCount 1 .

ex:OtherShape a sh:NodeShape ;
    sh:targetNode <{document_iri(1)}> ;
    sh:property [ sh:path <urn:jmfts:predicate:nothing-has-this> ; sh:minCount 1 ] .
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


def _fixture(session, *, usetype_b="vendor"):
    """The corpus in the module docstring. Returns the ids by letter."""
    docs = DocumentRepository(session)
    triples = TripleRepository(session)

    a = docs.create(title="A", content="A body", usetype="vendor")
    b = docs.create(title="B", content="B body", usetype=usetype_b)
    c = docs.create(title="C", content="C body", usetype="other")
    session.flush()

    name = triples.create_predicate(name="name", iri=NAME)
    country = triples.create_predicate(name="country", iri=COUNTRY)
    triples.create_triple(a.id, name.id, object_literal="Acme")
    triples.create_triple(a.id, country.id, object_id=c.id)
    triples.create_triple(b.id, name.id, object_literal="Beta")
    session.flush()
    return {"A": a.id, "B": b.id, "C": c.id}


def _bind(session, *, usetype_pattern="vendor", shape_iri=VENDOR_SHAPE):
    service = OntologyService(session)
    service.import_ontology(TURTLE, name=ONTOLOGY, base_iri=DEFAULT_BASE_IRI)
    binding = service.create_binding(
        ONTOLOGY, ShapeBindingCreate(shape_iri=shape_iri, usetype_pattern=usetype_pattern)
    )
    return binding


def _run(session, binding_id, *, planner=None, **kwargs):
    """Enqueue a validation run and drain it. Returns ``(response, report node)``.

    **The drain runs UNBOUND, and that is the whole point of draining separately from the
    request.** A worker holds no principal, and an unbound caller bypasses every access check
    (``jmfts_core/access.py``), so a test that drained inside the requesting principal's
    context would prove nothing about what the worker can see. Here the handler runs exactly
    as it does in production — able to read everything — and what it validates is still only
    what the request pinned onto the task row.
    """
    response = OntologyService(session).validate_binding(
        ONTOLOGY, ShapeValidationRequest(binding_id=binding_id, **kwargs)
    )
    with _as(None):
        drain_ingest_queue(
            session, planner=planner if planner is not None else IngestRollupPlanner()
        )
    session.expire_all()
    return response, session.get(Document, response.report_document_id)


def _report(node) -> dict:
    return (node.structured_content or {})[VALIDATION_BLOCK]["report"]


# ── the task is a task, and it is not a row of Part 4's table ───────────────


def test_the_validator_is_a_registered_handler_and_not_a_task_row():
    """``TASK_ROWS`` is evaluated from what ``probe`` measured; nothing probe measures says
    somebody asked for a validation run. The three ``fetch:*`` types are outside the table
    for the mirror-image reason, and this is the same shape: a task the REQUEST enqueues."""
    assert TASK_VALIDATE_SHAPE in TASK_HANDLERS
    assert TASK_VALIDATE_SHAPE not in {row.task for row in TASK_ROWS}


# ── the shape runs, against the documents the binding names ────────────────


def test_the_bound_shape_reports_a_violation(db_session):
    ids = _fixture(db_session)
    binding = _bind(db_session)
    with _as(OWNER):
        response, node = _run(db_session, binding.id)

    report = _report(node)
    assert response.scope_document_count == 2
    assert report["documents_resolved"] == 2
    assert report["conforms"] is False
    # Both vendors are missing `country` in the graph — B genuinely, A because its only
    # country triple points outside the scope. Neither is missing `name`.
    focus = {v["focus_node"] for v in report["violations"]}
    assert focus == {document_iri(ids["A"]), document_iri(ids["B"])}
    assert {v["result_path"] for v in report["violations"]} == {COUNTRY}


def test_a_named_property_shape_is_reached(db_session):
    """``rdflib``'s CBD stops at a named node; the closure in ``_shape_graph`` does not.

    Without it the `country` constraint — the only one this fixture violates — would never
    run, and the report would say the data conforms.
    """
    _fixture(db_session)
    binding = _bind(db_session)
    with _as(OWNER):
        _, node = _run(db_session, binding.id)
    assert _report(node)["violation_count"] == 2


def test_only_the_bound_shape_runs(db_session):
    """``ex:OtherShape`` targets a node explicitly and every document fails its constraint.

    A shapes graph built by parsing the whole vocabulary would report it. The binding names
    one shape, and one shape is what runs.

    Asserted on the PATH and not on ``source_shape``: ``ex:OtherShape``'s constraint is an
    inline property shape, so its ``sh:sourceShape`` is a blank node and the report records
    ``None`` for it (a blank node's id is an artefact of the parse). A path is a name the
    document wrote.
    """
    _fixture(db_session)
    binding = _bind(db_session)
    with _as(OWNER):
        _, node = _run(db_session, binding.id)
    violations = _report(node)["violations"]
    assert NOTHING not in {v["result_path"] for v in violations}, violations
    assert len(violations) == 2


def test_an_empty_target_set_is_not_what_conformance_means(db_session):
    """The regression guard for the failure this step exists to avoid.

    A shape bound to a scope must be TARGETED at that scope. If the ``sh:targetNode``
    injection were removed, the run would still succeed and still report ``conforms`` — so
    the assertion that matters is not "conforms is False" but that the violations name the
    documents the binding named.
    """
    ids = _fixture(db_session)
    binding = _bind(db_session)
    with _as(OWNER):
        _, node = _run(db_session, binding.id)
    report = _report(node)
    assert report["node_count"] > 0
    assert report["violation_count"] > 0
    assert document_iri(ids["C"]) not in {v["focus_node"] for v in report["violations"]}


# ── open question 6.2: a scope artefact is marked, never suppressed ─────────


def test_a_scope_artefact_is_marked_and_a_real_violation_is_not(db_session):
    ids = _fixture(db_session)
    binding = _bind(db_session)
    with _as(OWNER):
        _, node = _run(db_session, binding.id)

    report = _report(node)
    by_focus = {v["focus_node"]: v for v in report["violations"]}
    artefact = by_focus[document_iri(ids["A"])]
    genuine = by_focus[document_iri(ids["B"])]

    assert artefact["scope_artefact"] is True
    assert artefact["cut_paths"] == [COUNTRY]
    assert genuine["scope_artefact"] is False
    assert genuine["cut_paths"] == []
    assert report["scope_artefact_count"] == 1
    assert report["boundary_cuts"] == 1


# ── open question 6.5: the run's timestamp, and the caller compares ─────────


def test_the_report_carries_the_runs_timestamp(db_session):
    """No expiry and no background revalidation: a stored report is a statement about the
    data at a time, and the time is on it so a caller can decide whether it is still true."""
    _fixture(db_session)
    binding = _bind(db_session)
    with _as(OWNER):
        response, node = _run(db_session, binding.id)

    report = _report(node)
    assert report["validated_at"] >= response.requested_at.isoformat()
    # The rendered text is the node's `content` — the evidence name `text`, and what
    # full-text search reads. The timestamp is in it too, because a reader who found the
    # report by searching is the reader most likely to act on a stale one.
    assert report["validated_at"] in node.content
    assert node.usetype == VALIDATION_REPORT_USETYPE
    assert node.parent_id is None


# ── validation never mutates ───────────────────────────────────────────────


def _snapshot(session):
    documents = {
        doc.id: (doc.title, doc.content, doc.structured_content, doc.parent_id, doc.settled)
        for doc in session.query(Document).all()
    }
    triples = {
        row.id: (
            row.subject_id,
            row.predicate_id,
            row.object_id,
            row.object_literal,
            row.invalidated_at,
            row.derived_by,
        )
        for row in session.query(Triple).all()
    }
    links = {row.id for row in session.query(DocumentLink).all()}
    return documents, triples, links


def test_validation_changes_no_data_row(db_session):
    _fixture(db_session)
    binding = _bind(db_session)
    before_docs, before_triples, before_links = _snapshot(db_session)

    with _as(OWNER):
        response, _node = _run(db_session, binding.id)
    db_session.expire_all()
    after_docs, after_triples, after_links = _snapshot(db_session)

    # The report node is the ONLY row this run added anywhere.
    assert set(after_docs) - set(before_docs) == {response.report_document_id}
    assert {k: v for k, v in after_docs.items() if k != response.report_document_id} == before_docs
    assert after_triples == before_triples
    assert after_links == before_links


def test_a_run_enqueues_no_other_work(db_session):
    """The settling walk asks the rollup planner at every level above a completed task's
    scope node. A report node has no parent, so the walk ends at it — which is why this
    drains with the REAL ``IngestRollupPlanner`` rather than the suite's ``NO_ROLLUP``.
    """
    _fixture(db_session)
    binding = _bind(db_session)
    with _as(OWNER):
        _run(db_session, binding.id, planner=IngestRollupPlanner())

    queued = {row.task_type for row in db_session.query(TaskQueue).all()}
    assert queued == {TASK_VALIDATE_SHAPE}


# ── who may run one, and what the report is readable by ────────────────────


def test_a_reader_may_not_run_a_validation(db_session):
    """Open question 6.4's default, taken for validation as well as for derivation: a run
    MINTS a searchable node derived from every document in the scope, so it takes write."""
    ids = _fixture(db_session)
    binding = _bind(db_session)
    reader = _principal(db_session, "validate-reader")
    for doc_id in (ids["A"], ids["B"]):
        db_session.add(AccessGrant(document_id=doc_id, principal_id=reader.id, level="read"))
    db_session.flush()

    with _as(reader):
        with pytest.raises(AccessDeniedError):
            OntologyService(db_session).validate_binding(
                ONTOLOGY, ShapeValidationRequest(binding_id=binding.id)
            )


def test_the_report_inherits_the_scopes_grants(db_session):
    ids = _fixture(db_session)
    binding = _bind(db_session)
    writer = _principal(db_session, "validate-writer")
    for doc_id in (ids["A"], ids["B"]):
        db_session.add(AccessGrant(document_id=doc_id, principal_id=writer.id, level="write"))
    db_session.flush()

    with _as(writer):
        response, _node = _run(db_session, binding.id)

    assert response.governed is True
    grants = (
        db_session.query(AccessGrant)
        .filter(AccessGrant.document_id == response.report_document_id)
        .all()
    )
    assert {(g.principal_id, g.level) for g in grants} == {(writer.id, "write")}


def test_a_mixed_access_scope_is_refused(db_session):
    """One report node has one access. A scope governed two ways has none it could carry:
    the union over-shares and the intersection cannot be expressed, because the empty
    access key already means ungoverned, which is public."""
    ids = _fixture(db_session)
    binding = _bind(db_session)
    insider = _principal(db_session, "validate-insider")
    db_session.add(AccessGrant(document_id=ids["A"], principal_id=insider.id, level="write"))
    db_session.flush()

    with _as(OWNER):
        with pytest.raises(ScopeAccessNotUniformError):
            OntologyService(db_session).validate_binding(
                ONTOLOGY, ShapeValidationRequest(binding_id=binding.id)
            )


def test_the_scope_is_pinned_under_the_callers_access_filter(db_session):
    """The worker holds no principal and bypasses every check, so what it validates has to
    be decided by the request. A document the caller cannot read is not on the task row and
    therefore cannot reach the graph, the report or the violation list."""
    ids = _fixture(db_session)
    binding = _bind(db_session)
    insider = _principal(db_session, "pin-insider")
    outsider = _principal(db_session, "pin-outsider")
    # A is restricted to the insider; B is ungoverned, so both principals may write it.
    db_session.add(AccessGrant(document_id=ids["A"], principal_id=insider.id, level="write"))
    db_session.flush()

    with _as(outsider):
        response, node = _run(db_session, binding.id)

    task = db_session.query(TaskQueue).filter(TaskQueue.task_type == TASK_VALIDATE_SHAPE).one()
    assert task.params[PARAM_DOCUMENT_IDS] == [ids["B"]]
    assert response.scope_document_count == 1
    assert {v["focus_node"] for v in _report(node)["violations"]} == {document_iri(ids["B"])}


# ── the two refusals ───────────────────────────────────────────────────────


def test_an_empty_scope_is_refused(db_session):
    """An empty graph conforms to every shape, so a clean report over a scope that resolved
    to nothing is indistinguishable from one over data that passed."""
    _fixture(db_session)
    binding = _bind(db_session, usetype_pattern="no-such-usetype")
    with _as(OWNER):
        with pytest.raises(ScopeEmptyError):
            OntologyService(db_session).validate_binding(
                ONTOLOGY, ShapeValidationRequest(binding_id=binding.id)
            )


def test_a_scope_past_the_operators_bound_is_refused_by_the_request(db_session, monkeypatch):
    """Open question 6.3's bound, read at the ONE place a run's scope is pinned.

    ``_pin_scope`` is the shared front door for ``validate:shape`` and ``derive:rule``, so
    ``Settings.shacl_max_scope_documents`` binds both from a single call site. The handlers
    still pass ``None``: the request has already applied the bound, and applying it a second
    time in the worker would refuse a run the request accepted.

    The refusal is a ``ValueError``, which is 422 at the ``@expose`` layer and PERMANENT in the
    queue — a scope does not shrink on the third attempt — and it names the setting, because
    the person reading it is the person who moves it.
    """
    _fixture(db_session)
    binding = _bind(db_session)
    monkeypatch.setattr(get_settings(), "shacl_max_scope_documents", 1)
    with _as(OWNER), pytest.raises(ScopeTooLargeError) as caught:
        OntologyService(db_session).validate_binding(
            ONTOLOGY, ShapeValidationRequest(binding_id=binding.id)
        )
    assert (caught.value.scope_document_count, caught.value.bound) == (2, 1)
    assert "shacl_max_scope_documents" in str(caught.value)
    # Nothing was minted: the count runs before the report node is created, so a refused run
    # leaves no in-flight node behind for somebody to poll forever.
    assert (
        db_session.query(Document).filter(Document.usetype == VALIDATION_REPORT_USETYPE).count()
        == 0
    )


def test_the_default_bound_admits_an_ordinary_scope(db_session):
    """512 is conservative, not obstructive: the configured default is what every other test
    in this file runs under, and this says so on purpose rather than by implication."""
    assert get_settings().shacl_max_scope_documents == 512
    _fixture(db_session)
    binding = _bind(db_session)
    with _as(OWNER):
        response, _ = _run(db_session, binding.id)
    assert response.scope_document_count == 2


def test_a_binding_under_another_vocabulary_is_not_found(db_session):
    _fixture(db_session)
    binding = _bind(db_session)
    with _as(OWNER):
        with pytest.raises(LookupError):
            OntologyService(db_session).validate_binding(
                ONTOLOGY, ShapeValidationRequest(binding_id=binding.id + 10_000)
            )


# ── Block A finding 6: the rendered list is capped, and says so ─────────────


def _synthetic_report(violation_count: int) -> tuple[dict, dict]:
    """A report block with ``violation_count`` violations, without running a validation.

    The cap is unreachable through the real handler now that a scope is bounded at 512
    documents, which is the point of the finding: the row it prevents is one an operator buys
    by raising that bound, and a test that could only reach it by raising the bound too would
    be measuring the wrong thing. So the render is exercised directly.
    """
    violations = [
        {
            "focus_node": document_iri(index),
            "result_path": COUNTRY,
            "value": None,
            "severity": None,
            "constraint": "sh:MinCountConstraintComponent",
            "source_shape": None,
            "message": None,
            "scope_artefact": False,
            "cut_paths": [],
        }
        for index in range(violation_count)
    ]
    report = {
        "validated_at": "2026-09-10T00:00:00+00:00",
        "conforms": False,
        "violation_count": len(violations),
        "scope_artefact_count": 0,
        "documents_requested": len(violations),
        "documents_resolved": len(violations),
        "triple_count": 0,
        "node_count": len(violations),
        "boundary_cuts": 0,
        "violations": violations,
    }
    request = {
        "binding_id": 1,
        "ontology_name": ONTOLOGY,
        "shape_iri": VENDOR_SHAPE,
        "scope_type": "usetype",
        "provenance": "any",
    }
    return report, request


def test_the_rendered_violation_list_is_capped_and_says_it_was():
    """``SPRINT_0_5_0.md`` Block A finding 6.

    ``Document.content`` is indexed by ``idx_documents_content_fts``, a partial GIN over
    ``to_tsvector(title || ' ' || content)``, and MEASURED at the memory bound the rendered
    report was 11,688,300 characters — enough, past roughly 190,000 documents, to buy `string
    is too long for tsvector`, which classifies PERMANENT and arrives on the LAST write of a
    run that already spent a minute.

    **The cap is only half of it.** A truncated report that did not say it was truncated would
    read as "these are the violations" when it is "these are the first 512", which is the Fail
    Early failure this tree has fixed twice. So the text has to carry the total, the number
    shown, and where the rest is.
    """
    report, request = _synthetic_report(RENDERED_VIOLATION_CAP + 7)
    text = _render(report, request)

    listed = [line for line in text.splitlines() if line.startswith("- `urn:jmfts:document:")]
    assert len(listed) == RENDERED_VIOLATION_CAP
    assert f"- violations: {RENDERED_VIOLATION_CAP + 7}" in text
    assert f"first {RENDERED_VIOLATION_CAP} of {RENDERED_VIOLATION_CAP + 7}" in text
    # The machine-readable half is named, because that is where the other seven are.
    assert "structured_content" in text and VALIDATION_BLOCK in text
    # And they really are still there: rendering reads the list, it does not trim it.
    assert len(report["violations"]) == RENDERED_VIOLATION_CAP + 7


def test_a_report_inside_the_cap_carries_no_truncation_notice():
    """The notice is a statement about THIS report, so a report that lists everything must not
    carry one — a reader who saw it on every report would stop reading it."""
    report, request = _synthetic_report(3)
    text = _render(report, request)
    listed = [line for line in text.splitlines() if line.startswith("- `urn:jmfts:document:")]
    assert len(listed) == 3
    assert "first" not in text


# ── the same thing over the wire ───────────────────────────────────────────


def test_the_route_enqueues_and_answers_202(db_session):
    """``POST /ontologies/{name}/validate`` — the whole point of the status code.

    202 and not 201: the request creates a node, but the answer a caller asked for does not
    exist yet. The body says where it will be.
    """
    from fastapi.testclient import TestClient

    from jmfts_core.database import get_db
    from jmfts_core.rest.main import app

    from tests.conftest import AUTH_HEADERS

    _fixture(db_session)
    binding = _bind(db_session)

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(app, headers=AUTH_HEADERS)
        response = client.post(f"/ontologies/{ONTOLOGY}/validate", json={"binding_id": binding.id})
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["scope_document_count"] == 2
    assert body["governed"] is False
    assert body["status"] == "pending"

    with _as(None):
        drain_ingest_queue(db_session, planner=IngestRollupPlanner())
    db_session.expire_all()
    node = db_session.get(Document, body["report_document_id"])
    assert _report(node)["violation_count"] == 2
