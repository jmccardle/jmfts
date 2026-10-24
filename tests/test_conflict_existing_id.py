"""A duplicate create is a 409 that names the row already there (#54, #55).

Before, a duplicate predicate said only "already exists" and a duplicate triple was a 500,
because nothing caught the ``IntegrityError``. Both now answer 409 with
``{"message", "existing_id"}``. The race test uses real, separately-committed sessions and
real threads, as ``tests/test_ingest_scheduling_races.py`` does: the savepoint ``db_session``
fixture is one connection and cannot stage two writers.
"""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from jmfts_client.contracts.triple import PredicateCreate, TripleCreate
from jmfts_core.database import get_db, get_session
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository
from jmfts_core.rest.main import app
from jmfts_core.services.triple_service import (
    PredicateConflictError,
    TripleConflictError,
    TripleService,
)
from tests.conftest import AUTH_HEADERS, DB_READY

pytestmark = pytest.mark.skipif(not DB_READY, reason="test database not provisioned")

_JOIN_TIMEOUT = 30


@pytest.fixture
def http(db_session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app, headers=AUTH_HEADERS)
    app.dependency_overrides.pop(get_db, None)


def test_duplicate_predicate_is_a_409_naming_the_row(http):
    first = http.post("/triples/predicates", json={"name": "conflict_pred"})
    assert first.status_code == 201
    again = http.post("/triples/predicates", json={"name": "conflict_pred"})
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["existing_id"] == first.json()["id"]
    assert "conflict_pred" in detail["message"]


def test_a_predicate_without_triples_is_listed_by_default(http):
    created = http.post("/triples/predicates", json={"name": "listed_without_triples"})
    assert created.status_code == 201
    names = {p["name"] for p in http.get("/triples/predicates").json()}
    assert "listed_without_triples" in names


@pytest.fixture
def committed_predicate_name():
    name = "raced_conflict_predicate"
    yield name
    with get_session() as session:
        session.execute(text("DELETE FROM predicates WHERE name = :n"), {"n": name})


def test_parallel_predicate_create_is_one_201_and_the_rest_409(
    committed_predicate_name, monkeypatch
):
    """N creators pass the existence check together; the unique key picks one winner.

    The barrier sits after the check and before the insert, so every thread has seen "not
    found" — the window the savepoint exists for. Exactly one wins, and every loser names
    the winner's id.
    """
    n = 6
    barrier = threading.Barrier(n)
    original = TripleRepository.get_predicate_by_name
    local = threading.local()

    def checked_then_wait(self, name):
        found = original(self, name)
        # Only the service's pre-check waits; the lookup after a lost race must not.
        if not getattr(local, "waited", False):
            local.waited = True
            barrier.wait(timeout=_JOIN_TIMEOUT)
        return found

    monkeypatch.setattr(TripleRepository, "get_predicate_by_name", checked_then_wait)

    outcomes: list = []

    def create():
        try:
            with get_session() as session:
                created = TripleService(session).create_predicate(
                    PredicateCreate(name=committed_predicate_name)
                )
            outcomes.append(("created", created.id))
        except PredicateConflictError as exc:
            outcomes.append(("conflict", exc.http_detail["existing_id"]))
        except BaseException as exc:  # noqa: BLE001 — reported by the assertion below
            outcomes.append(("error", exc))

    threads = [threading.Thread(target=create) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT)
    assert not any(t.is_alive() for t in threads), "a thread never finished — deadlock?"

    assert [o for o in outcomes if o[0] == "error"] == []
    created = [o[1] for o in outcomes if o[0] == "created"]
    conflicts = [o[1] for o in outcomes if o[0] == "conflict"]
    assert len(created) == 1 and len(conflicts) == n - 1
    assert set(conflicts) == set(created)


def test_duplicate_iri_on_a_new_name_is_a_409_naming_the_row(http):
    iri = "http://example.org/vocab#conflicts_with"
    first = http.post("/triples/predicates", json={"name": "iri_first", "iri": iri})
    assert first.status_code == 201
    again = http.post("/triples/predicates", json={"name": "iri_second", "iri": iri})
    assert again.status_code == 409
    assert again.json()["detail"]["existing_id"] == first.json()["id"]


@pytest.fixture
def committed_fact():
    """A committed subject, object and predicate; the triples made on them go with them."""
    with get_session() as session:
        docs = DocumentRepository(session)
        s = docs.create(title="race subject", content="s", usetype="raw", auto_embed=False)
        o = docs.create(title="race object", content="o", usetype="raw", auto_embed=False)
        p = TripleRepository(session).create_predicate(name="raced_triple_predicate")
        session.flush()
        ids = (s.id, o.id, p.id)
    yield ids
    with get_session() as session:
        session.execute(text("DELETE FROM documents WHERE id = ANY(:ids)"), {"ids": list(ids[:2])})
        session.execute(text("DELETE FROM predicates WHERE id = :p"), {"p": ids[2]})


def test_parallel_triple_create_is_one_created_and_the_rest_conflicts(committed_fact):
    """N creators of one node triple: the unique key picks one winner, losers name it."""
    subject_id, object_id, predicate_id = committed_fact
    n = 6
    barrier = threading.Barrier(n)
    outcomes: list = []

    def create():
        try:
            barrier.wait(timeout=_JOIN_TIMEOUT)
            with get_session() as session:
                made = TripleService(session).create_triple(
                    TripleCreate(
                        subject_id=subject_id, predicate_id=predicate_id, object_id=object_id
                    )
                )
            outcomes.append(("created", made.id))
        except TripleConflictError as exc:
            outcomes.append(("conflict", exc.http_detail["existing_id"]))
        except BaseException as exc:  # noqa: BLE001 — reported by the assertion below
            outcomes.append(("error", exc))

    threads = [threading.Thread(target=create) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT)
    assert not any(t.is_alive() for t in threads), "a thread never finished — deadlock?"

    assert [o for o in outcomes if o[0] == "error"] == []
    created = [o[1] for o in outcomes if o[0] == "created"]
    conflicts = [o[1] for o in outcomes if o[0] == "conflict"]
    assert len(created) == 1 and len(conflicts) == n - 1
    assert set(conflicts) == set(created)
