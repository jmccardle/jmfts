"""Seeded rank: personalized PageRank from a handful of documents, over the hops around them.

Global centrality answers "what is central in this corpus". The seeded verb answers "what is
central to these documents", and the fixture is built so the two disagree.

Links, all `link_type="cites"` unless marked:

    S1 ──▶ HUB ◀── S2          HUB is reached from both seeds
    S1 ──▶ SIDE                SIDE from one
    HUB ──▶ FAR1 ──▶ FAR2 ──▶ FAR3
    S2 ──mentions──▶ OFFTYPE   the only edge of another type
    X1 ──▶ BIG ◀── X2 ◀── ...  a distant cluster that dominates GLOBAL PageRank
"""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jmfts_client import RemoteJmftsClient
from jmfts_client.contracts import Seed, SeededRankRequest
from jmfts_core.database import get_db
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.models.triple import Predicate, Triple
from jmfts_core.principal_context import CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.rest.main import app
from jmfts_core.services.graph_service import GraphService


def _fixture(session):
    docs = DocumentRepository(session)
    root = docs.create(title="ROOT", content="root", auto_embed=False)
    session.flush()
    names = ["S1", "S2", "HUB", "SIDE", "FAR1", "FAR2", "FAR3", "OFFTYPE", "BIG"]
    names += [f"X{i}" for i in range(8)]
    ids = {"ROOT": root.id}
    for name in names:
        ids[name] = docs.create(
            title=name, content=name.lower(), parent_id=root.id, auto_embed=False
        ).id
    session.flush()

    def link(a, b, link_type="cites"):
        docs.create_link(source_id=ids[a], target_id=ids[b], link_type=link_type)

    link("S1", "HUB")
    link("S2", "HUB")
    link("S1", "SIDE")
    link("HUB", "FAR1")
    link("FAR1", "FAR2")
    link("FAR2", "FAR3")
    link("S2", "OFFTYPE", "mentions")
    for i in range(8):
        link(f"X{i}", "BIG")
    session.flush()
    return ids


def _rank(session, **kwargs):
    return GraphService(session).get_seeded_rank(SeededRankRequest(**kwargs))


def _seeds(ids, *names):
    return [Seed(document_id=ids[n]) for n in names]


def _order(ids, response):
    name = {v: k for k, v in ids.items()}
    return [name[r.document_id] for r in response.results]


def test_the_node_both_seeds_reach_outranks_one_only_one_reaches(db_session):
    ids = _fixture(db_session)
    response = _rank(db_session, seeds=_seeds(ids, "S1", "S2"), top=50)
    order = _order(ids, response)

    assert order.index("HUB") < order.index("SIDE")
    by_name = {n: r for n, r in zip(order, response.results)}
    assert by_name["S1"].is_seed and by_name["S2"].is_seed
    assert not by_name["HUB"].is_seed
    assert by_name["HUB"].hops == 1
    assert by_name["FAR1"].hops == 2


def test_global_centrality_and_seeded_rank_disagree(db_session):
    """BIG leads the corpus and is not even in the seeded subgraph."""
    ids = _fixture(db_session)
    central = GraphService(db_session).get_centrality(top=1)
    assert central.results[0].document_id == ids["BIG"]

    seeded = _rank(db_session, seeds=_seeds(ids, "S1", "S2"), top=50)
    assert ids["BIG"] not in {r.document_id for r in seeded.results}
    assert seeded.subgraph_vertices < central.total_vertices


def test_hops_bound_the_subgraph(db_session):
    """FAR3 is 4 undirected hops from S1: absent at 3, present at 4."""
    ids = _fixture(db_session)
    at3 = {r.document_id for r in _rank(db_session, seeds=_seeds(ids, "S1"), hops=3).results}
    at4 = {r.document_id for r in _rank(db_session, seeds=_seeds(ids, "S1"), hops=4).results}
    assert ids["FAR2"] in at3 and ids["FAR3"] not in at3
    assert ids["FAR3"] in at4


def test_hops_zero_is_the_seeds_alone(db_session):
    ids = _fixture(db_session)
    response = _rank(db_session, seeds=_seeds(ids, "S1", "S2"), hops=0)
    assert set(_order(ids, response)) == {"S1", "S2"}
    assert response.subgraph_edges == 0


def test_seed_weight_moves_the_restart_mass(db_session):
    ids = _fixture(db_session)
    heavy_s1 = _rank(
        db_session, seeds=[Seed(document_id=ids["S1"], weight=9), Seed(document_id=ids["S2"])]
    )
    heavy_s2 = _rank(
        db_session, seeds=[Seed(document_id=ids["S1"]), Seed(document_id=ids["S2"], weight=9)]
    )
    side = lambda r: next(x.score for x in r.results if x.document_id == ids["SIDE"])  # noqa: E731
    assert side(heavy_s1) > side(heavy_s2)


def test_relations_keep_only_the_named_link_types(db_session):
    ids = _fixture(db_session)
    everything = {r.document_id for r in _rank(db_session, seeds=_seeds(ids, "S2")).results}
    cites_only = _rank(db_session, seeds=_seeds(ids, "S2"), relations=["cites"])

    assert ids["OFFTYPE"] in everything
    assert ids["OFFTYPE"] not in {r.document_id for r in cites_only.results}
    assert cites_only.relations == ["cites"]


def test_an_unknown_relation_is_refused(db_session):
    ids = _fixture(db_session)
    with pytest.raises(ValueError, match="no links relation named \\['nonesuch'\\]"):
        _rank(db_session, seeds=_seeds(ids, "S1"), relations=["cites", "nonesuch"])


def test_an_empty_relations_list_is_refused_by_the_contract():
    with pytest.raises(ValidationError):
        SeededRankRequest(seeds=[Seed(document_id=1)], relations=[])


def test_triple_relations_filter_by_predicate_name(db_session):
    ids = _fixture(db_session)
    likes = Predicate(name="test_likes")
    owns = Predicate(name="test_owns")
    db_session.add_all([likes, owns])
    db_session.flush()
    db_session.add_all(
        [
            Triple(subject_id=ids["S1"], predicate_id=likes.id, object_id=ids["HUB"]),
            Triple(subject_id=ids["S1"], predicate_id=owns.id, object_id=ids["SIDE"]),
        ]
    )
    db_session.flush()

    response = _rank(db_session, seeds=_seeds(ids, "S1"), scope="triples", relations=["test_likes"])
    reached = set(_order(ids, response))
    assert reached == {"S1", "HUB"}

    # A link type is not a predicate: under `triples`, `cites` is unknown.
    with pytest.raises(ValueError, match="no triples relation named"):
        _rank(db_session, seeds=_seeds(ids, "S1"), scope="triples", relations=["cites"])


def test_a_missing_seed_is_a_lookup_error(db_session):
    ids = _fixture(db_session)
    with pytest.raises(LookupError, match="seed documents not in the graph"):
        _rank(db_session, seeds=[Seed(document_id=ids["S1"]), Seed(document_id=10**9)])


@contextmanager
def _as(principal):
    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


def test_an_unreadable_seed_reads_as_a_missing_one(db_session):
    ids = _fixture(db_session)
    owner = PrincipalModel(name="holder")
    stranger = PrincipalModel(name="stranger")
    db_session.add_all([owner, stranger])
    db_session.flush()
    db_session.add(AccessGrant(document_id=ids["S1"], principal_id=owner.id, level="read"))
    db_session.flush()

    with _as(CurrentPrincipal(id=stranger.id, name="stranger", is_owner=False)):
        with pytest.raises(LookupError) as unreadable:
            _rank(db_session, seeds=_seeds(ids, "S1"))
    with pytest.raises(LookupError) as missing:
        _rank(db_session, seeds=[Seed(document_id=10**9)])
    assert str(unreadable.value).split(":")[0] == str(missing.value).split(":")[0]


def test_seeded_rank_round_trips_the_generated_client(db_session):
    """POST body in, model out, and a missing seed is a 404 on the wire."""
    from jmfts_client import JmftsNotFound

    from tests.conftest import AUTH_HEADERS

    ids = _fixture(db_session)

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = RemoteJmftsClient(
            "http://testserver", client=TestClient(app, headers=AUTH_HEADERS)
        )
        response = client.get_seeded_rank(
            SeededRankRequest(seeds=_seeds(ids, "S1", "S2"), relations=["cites"])
        )
        with pytest.raises(JmftsNotFound):
            client.get_seeded_rank(SeededRankRequest(seeds=[Seed(document_id=10**9)]))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert type(response).__name__ == "SeededRankResponse"
    assert ids["HUB"] in {r.document_id for r in response.results}
