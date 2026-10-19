"""Literal-object triples over REST (issue #48).

``POST /triples``, ``PUT /triples`` and ``POST /triples/{id}/supersede`` accept
``object_literal`` (+ optional ``object_datatype``) in place of ``object_id``: exactly one.
A literal fact's identity is ``uq_triple_literal`` — ``(subject, predicate,
md5(object_literal), COALESCE(object_datatype, ''))`` — and ``PUT`` is idempotent over it.

Most of this drives the real app through a ``TestClient`` so the 422 bodies asserted here are
the ones a network caller reads; the access cases run the service under a bound principal, as
``tests/test_access_triple_writes.py`` does.
"""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from jmfts_client import RemoteJmftsClient
from jmfts_client.contracts.triple import TripleCreate, TripleSupersedRequest
from jmfts_core.access import AccessDeniedError
from jmfts_core.database import get_db
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository
from jmfts_core.rest.main import app
from jmfts_core.services.triple_service import TripleService

XSD = "http://www.w3.org/2001/XMLSchema#"
INT = XSD + "integer"


@pytest.fixture
def http(db_session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    from tests.conftest import AUTH_HEADERS

    yield TestClient(app, headers=AUTH_HEADERS)
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def facts(db_session):
    """(subject, other node, predicate) ids."""
    docs = DocumentRepository(db_session)
    s = docs.create(title="source", content="a source", usetype="raw", auto_embed=False)
    o = docs.create(title="other", content="another node", usetype="raw", auto_embed=False)
    p = TripleRepository(db_session).create_predicate(name="language")
    db_session.flush()
    return s.id, o.id, p.id


def _body(facts, **extra):
    s, _o, p = facts
    return {"subject_id": s, "predicate_id": p, **extra}


def _stored(http, facts):
    return http.get("/triples/query", params={"entity_id": facts[0]}).json()


# ── POST ────────────────────────────────────────────────────────────────────


def test_create_with_a_literal(http, facts):
    r = http.post("/triples", json=_body(facts, object_literal="en"))
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["object_id"] is None
    assert row["object_literal"] == "en"
    assert row["object_datatype"] is None


def test_create_with_a_typed_literal(http, facts):
    r = http.post("/triples", json=_body(facts, object_literal="42", object_datatype=INT))
    assert r.status_code == 201, r.text
    assert (r.json()["object_literal"], r.json()["object_datatype"]) == ("42", INT)
    got = http.get(f"/triples/{r.json()['id']}")
    assert got.json()["object_literal"] == "42"


def test_the_node_object_path_is_unchanged(http, facts):
    r = http.post("/triples", json=_body(facts, object_id=facts[1]))
    assert r.status_code == 201
    assert r.json()["object_id"] == facts[1]
    assert r.json()["object_literal"] is None


@pytest.mark.parametrize("verb", ["post", "put"])
def test_both_or_neither_object_is_a_422_naming_both_fields(http, facts, verb):
    send = getattr(http, verb)
    both = send("/triples", json=_body(facts, object_id=facts[1], object_literal="x"))
    neither = send("/triples", json=_body(facts))
    for r in (both, neither):
        assert r.status_code == 422
        msg = r.json()["detail"][0]["msg"]
        assert "object_id" in msg and "object_literal" in msg
    assert _stored(http, facts) == []


def test_a_datatype_without_a_literal_is_a_422(http, facts):
    r = http.post("/triples", json=_body(facts, object_id=facts[1], object_datatype=INT))
    assert r.status_code == 422
    assert "object_datatype" in r.json()["detail"][0]["msg"]
    r = http.post("/triples", json=_body(facts, object_datatype=INT))
    assert r.status_code == 422


@pytest.mark.parametrize(
    "literal, datatype, fragment",
    [
        ("12", XSD + "nonsense", "not one of the datatypes this appliance knows"),
        ("abc", INT, "not a valid xsd:integer value"),
        ("2024-02-30", XSD + "date", "not a valid xsd:date value"),
        ("maybe", XSD + "boolean", "not a valid xsd:boolean value"),
        ("5", "xsd:integer", "CURIE"),
        ("5", "integer", "RELATIVE"),
        ("5", "http://example.org/" + "t" * 100, "holds 100"),
    ],
)
def test_an_unknown_datatype_or_a_bad_value_is_refused_not_coerced(
    http, facts, literal, datatype, fragment
):
    for verb in (http.post, http.put):
        r = verb("/triples", json=_body(facts, object_literal=literal, object_datatype=datatype))
        assert r.status_code == 422, r.text
        assert fragment in r.json()["detail"]
    assert _stored(http, facts) == []


def test_a_vocabulary_datatype_outside_xsd_is_stored_opaquely(http, facts):
    r = http.post(
        "/triples",
        json=_body(facts, object_literal="anything", object_datatype="http://example.org/t#T"),
    )
    assert r.status_code == 201


def test_duplicate_create_without_dedup_matches_the_node_object_behaviour(db_session, facts):
    """A plain duplicate raises IntegrityError for a node object; a literal does the same."""
    s, o, p = facts
    svc = TripleService(db_session)
    svc.create_triple(TripleCreate(subject_id=s, predicate_id=p, object_id=o))
    svc.create_triple(TripleCreate(subject_id=s, predicate_id=p, object_literal="en"))
    with pytest.raises(IntegrityError), db_session.begin_nested():
        svc.create_triple(TripleCreate(subject_id=s, predicate_id=p, object_id=o))
    with pytest.raises(IntegrityError), db_session.begin_nested():
        svc.create_triple(TripleCreate(subject_id=s, predicate_id=p, object_literal="en"))


def test_duplicate_create_with_dedup_returns_the_existing_literal_fact(http, facts):
    first = http.post("/triples", json=_body(facts, object_literal="en"))
    again = http.post("/triples?dedup=true", json=_body(facts, object_literal="en"))
    assert again.status_code == 201
    assert again.json()["id"] == first.json()["id"]


# ── PUT ─────────────────────────────────────────────────────────────────────


def test_put_is_idempotent_over_a_literal(http, facts):
    body = _body(facts, object_literal="42", object_datatype=INT)
    first = http.put("/triples", json=body)
    second = http.put("/triples", json=body)
    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json()["id"] == first.json()["id"]
    assert len(_stored(http, facts)) == 1


def test_put_does_not_overwrite_the_first_assertions_metadata(http, facts):
    first = http.put("/triples", json=_body(facts, object_literal="en", fact_type="static"))
    again = http.put("/triples", json=_body(facts, object_literal="en", fact_type="dynamic"))
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["fact_type"] == "static"


def test_the_literal_key_is_uq_triple_literal(http, facts):
    """Same text under another datatype, or untyped vs xsd:string, is another fact."""
    variants = (
        {"object_literal": "7"},
        {"object_literal": "7", "object_datatype": INT},
        {"object_literal": "7", "object_datatype": XSD + "string"},
        {"object_literal": "7", "object_datatype": XSD + "long"},
        {"object_literal": "8"},
    )
    ids = {http.put("/triples", json=_body(facts, **o)).json()["id"] for o in variants}
    assert len(ids) == 5


def test_a_long_literal_is_keyed_by_its_whole_text(http, facts):
    """md5 keys the index, so two literals past the btree entry cap still differ."""
    a = "x" * 5000 + "a"
    b = "x" * 5000 + "b"
    ia = http.put("/triples", json=_body(facts, object_literal=a)).json()["id"]
    ib = http.put("/triples", json=_body(facts, object_literal=b)).json()["id"]
    assert ia != ib
    assert http.put("/triples", json=_body(facts, object_literal=a)).json()["id"] == ia


def test_a_literal_does_not_collide_with_a_node_triple(http, facts):
    node = http.put("/triples", json=_body(facts, object_id=facts[1])).json()
    lit = http.put("/triples", json=_body(facts, object_literal=str(facts[1]))).json()
    assert node["id"] != lit["id"]
    assert node["object_id"] == facts[1] and lit["object_id"] is None
    assert http.put("/triples", json=_body(facts, object_id=facts[1])).json()["id"] == node["id"]


# ── supersede ───────────────────────────────────────────────────────────────


def test_supersede_with_a_literal(http, facts):
    old = http.post("/triples", json=_body(facts, object_literal="en")).json()
    r = http.post(f"/triples/{old['id']}/supersede", json=_body(facts, object_literal="de"))
    assert r.status_code == 201, r.text
    new = r.json()
    assert (new["object_literal"], new["object_id"]) == ("de", None)
    assert http.get(f"/triples/{old['id']}").json()["invalidated_by"] == new["id"]


def test_supersede_can_change_a_node_object_into_a_literal(http, facts):
    old = http.post("/triples", json=_body(facts, object_id=facts[1])).json()
    r = http.post(
        f"/triples/{old['id']}/supersede",
        json=_body(facts, object_literal="3", object_datatype=INT),
    )
    assert r.status_code == 201
    assert r.json()["object_datatype"] == INT


def test_supersede_validates_like_create(http, facts):
    old = http.post("/triples", json=_body(facts, object_literal="en")).json()
    url = f"/triples/{old['id']}/supersede"
    assert http.post(url, json=_body(facts)).status_code == 422
    assert http.post(url, json=_body(facts, object_id=1, object_literal="x")).status_code == 422
    bad = _body(facts, object_literal="x", object_datatype=XSD + "zz")
    assert http.post(url, json=bad).status_code == 422
    assert http.get(f"/triples/{old['id']}").json()["invalidated_at"] is None


# ── generated client ────────────────────────────────────────────────────────


def test_the_generated_client_round_trips_a_literal(http, facts):
    client = RemoteJmftsClient("http://testserver", client=http)
    s, _o, p = facts
    req = TripleCreate(subject_id=s, predicate_id=p, object_literal="42", object_datatype=INT)
    created = client.create_triple(req)
    assert (created.object_literal, created.object_datatype, created.object_id) == ("42", INT, None)
    assert client.upsert_triple(req).id == created.id
    successor = client.supersede_triple(
        created.id,
        TripleSupersedRequest(
            subject_id=s, predicate_id=p, object_literal="43", object_datatype=INT
        ),
    )
    assert successor.object_literal == "43"


def test_the_client_models_refuse_a_malformed_object_before_sending():
    with pytest.raises(ValueError, match="object_id and object_literal"):
        TripleCreate(subject_id=1, predicate_id=1)
    with pytest.raises(ValueError, match="object_datatype"):
        TripleCreate(subject_id=1, predicate_id=1, object_id=2, object_datatype=INT)


# ── access: the same gate as a node-object triple ───────────────────────────


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


def test_literal_writes_need_write_on_the_subject_and_nothing_on_an_object(db_session):
    docs = DocumentRepository(db_session)
    owned = docs.create(title="OWNED", content="owned " * 3, auto_embed=False)
    p = TripleRepository(db_session).create_predicate(name="mentions")
    author = _principal(db_session, "author")
    reader = _principal(db_session, "reader")
    db_session.add_all(
        [
            AccessGrant(document_id=owned.id, principal_id=author.id, level="write"),
            AccessGrant(document_id=owned.id, principal_id=reader.id, level="read"),
        ]
    )
    db_session.flush()
    svc = TripleService(db_session)
    req = TripleCreate(subject_id=owned.id, predicate_id=p.id, object_literal="en")

    # read-only on the subject: refused on every write verb, as for a node object.
    with _as(reader):
        with pytest.raises(AccessDeniedError):
            svc.create_triple(req)
        with pytest.raises(AccessDeniedError):
            svc.upsert_triple(req)
    with _as(author):
        made = svc.create_triple(req)
        assert svc.upsert_triple(req).id == made.id
        successor = svc.supersede_triple(
            made.id,
            TripleSupersedRequest(subject_id=owned.id, predicate_id=p.id, object_literal="de"),
        )
    assert successor.object_literal == "de"
    with _as(reader), pytest.raises(AccessDeniedError):
        svc.supersede_triple(
            successor.id,
            TripleSupersedRequest(subject_id=owned.id, predicate_id=p.id, object_literal="fr"),
        )
