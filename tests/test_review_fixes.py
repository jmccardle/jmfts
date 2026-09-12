"""The wrong-answer paths an outside review found, each pinned by a test.

Every case below is one where the appliance answered 200 with something other than what
the caller asked for, and answered it silently:

* a retrieval method name nothing recognised contributed nothing and raised nothing, so a
  four-method fusion was labelled as one and ran as another;
* the same three method names in a different order resolved to a different method set,
  because the request's own field default was the sentinel for "not specified";
* ``GET /search/?method=`` fell through its ``elif`` ladder into the hybrid branch;
* a result set was filtered by ``JMFTS_SEARCH_EXCLUDE_USETYPES`` with nothing on the wire
  saying so;
* ``POST /documents`` wrote vectors and no BM25 postings, so the document was invisible to
  one leg of the fusion;
* ``GET /health`` — the liveness probe, and the one health path reachable without a token —
  made up to two outbound HTTP calls at a 5 s timeout each.

The first half needs no database. The second half is marked ``requires_db``.
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jmfts_client.contracts.search import (
    DEFAULT_HYBRID_METHODS,
    SEARCH_METHODS,
    HybridSearchRequest,
)
from jmfts_core.rest.main import app
from jmfts_core.repositories.search import (
    TUNED_HYBRID_WEIGHTS,
    effective_exclude_types,
    effective_weights,
)
from tests.conftest import AUTH_HEADERS, requires_db

client = TestClient(app)


# ── the method vocabulary is closed ─────────────────────────────────────────


def test_unknown_method_name_is_rejected_not_ignored():
    """``"maxsim "`` and ``"hybrid"`` are the two the review actually typed."""
    for bad in ("maxsim ", "hybrid", "vetcor", ""):
        with pytest.raises(ValidationError):
            HybridSearchRequest(query="q", methods=["vector", bad])


def test_empty_method_list_is_rejected():
    """A fusion over no methods returns nothing; asking for it is a mistake, not a default."""
    with pytest.raises(ValidationError):
        HybridSearchRequest(query="q", methods=[])


def test_duplicate_method_names_are_rejected():
    with pytest.raises(ValidationError):
        HybridSearchRequest(query="q", methods=["vector", "vector"])


def test_methods_defaults_to_not_specified():
    """``None``, not a list — which is what makes the field order-independent.

    The old default was ``["vector", "fulltext", "bm25"]`` and the service compared the
    request against it with ``!=``. So the declared order resolved to "unspecified" and ran
    two methods, and any other order ran three. There is no permutation of ``None``.
    """
    assert HybridSearchRequest(query="q").methods is None


def test_every_declared_method_is_accepted():
    assert HybridSearchRequest(query="q", methods=list(SEARCH_METHODS)).methods == list(
        SEARCH_METHODS
    )


def test_reordering_the_methods_does_not_change_the_method_set():
    """The defect, stated directly: permuting the list must not change what runs."""
    a = HybridSearchRequest(query="q", methods=["vector", "fulltext", "bm25"])
    b = HybridSearchRequest(query="q", methods=["bm25", "vector", "fulltext"])
    assert set(a.methods) == set(b.methods)


def test_unknown_method_on_quick_search_is_400_not_a_hybrid_search():
    """It used to fall through the ``elif`` ladder and answer 200 from the ``else`` branch."""
    resp = client.get("/search/", params={"q": "x", "method": "vetcor"}, headers=AUTH_HEADERS)
    assert resp.status_code == 400, resp.text
    assert "vetcor" in resp.text


# ── resolution is reported, not inferred ────────────────────────────────────


def test_effective_exclude_types_reports_the_config_default_when_unspecified():
    from jmfts_core.config import get_settings

    assert effective_exclude_types(None, None) == list(get_settings().search_exclude_usetypes)


def test_an_explicit_empty_list_disables_exclusion():
    assert effective_exclude_types(None, []) == []


def test_a_positive_usetype_overrides_exclusion():
    """A usetype filter is a keep-list, so nothing is excluded on top of it."""
    assert effective_exclude_types("chunk", None) == []
    assert effective_exclude_types("chunk", ["entity"]) == []


def test_effective_weights_distinguishes_unspecified_from_no_opinion():
    assert effective_weights(None) == TUNED_HYBRID_WEIGHTS
    assert effective_weights({}) == {}


def test_an_untuned_method_outfused_the_tuned_ones():
    """The reason the old default could not have run the methods it named.

    ``weights.get(name, 1.0)`` gives an untuned method 1.0, and every tuned weight is below
    that. So adding ``fulltext`` to the list without adding its weight does not add a small
    signal — it adds the largest one.
    """
    assert all(w < 1.0 for w in TUNED_HYBRID_WEIGHTS.values())
    assert set(TUNED_HYBRID_WEIGHTS) == set(DEFAULT_HYBRID_METHODS)


# ── the probe endpoints ─────────────────────────────────────────────────────


def test_health_is_public_and_makes_no_llm_call():
    """The liveness probe. If this ever calls ``_probe_llm`` again, this fails."""
    with patch("jmfts_core.rest.main._probe_llm") as probe:
        resp = client.get("/health")
    assert resp.status_code == 200
    probe.assert_not_called()
    assert resp.json()["llm"] is None


def test_the_llm_probe_lives_on_its_own_path():
    resp = client.get("/health/llm", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["llm"] is not None


def test_the_llm_probe_is_not_public():
    """It dials a configured host on demand; an unauthenticated caller must not trigger that."""
    assert client.get("/health/llm").status_code == 401


# ── capability discovery ────────────────────────────────────────────────────


def test_capabilities_answers_without_a_corpus_and_without_a_network_call():
    with patch("jmfts_core.rest.main._probe_llm") as probe:
        resp = client.get("/capabilities", headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    probe.assert_not_called()
    body = resp.json()
    assert body["corpus"] is None
    assert body["retrieval"]["methods"] == list(SEARCH_METHODS)
    assert body["retrieval"]["default_methods"] == list(DEFAULT_HYBRID_METHODS)
    assert {e["name"] for e in body["extras"]} >= {"embed", "office", "rdf"}


def test_capabilities_is_gated():
    assert client.get("/capabilities").status_code == 401


def test_capabilities_separates_a_local_model_from_a_configured_runner():
    """A worker with ``JMFTS_RUNNER_URL`` set and no torch can embed and has no model.

    One "can it embed" would flatten the two, and the two are what decide whether adding
    the ``embed`` extra to that node would change anything.
    """
    body = client.get("/capabilities", headers=AUTH_HEADERS).json()["embedding"]
    assert body["can_embed"] == (body["local_model_available"] or bool(body["runner_url"]))


# ── database-backed ─────────────────────────────────────────────────────────


class _MockEmbeddingService:
    """Deterministic unit vectors keyed by the text. No model, no network.

    Complete enough to serve the DEFAULT write path — `auto_embed` and `embed_tokens` are
    both on unless a request says otherwise, and the tests below deliberately do not say
    otherwise, because the README's worked example does not either.
    """

    dim = 768

    def _unit(self, seed: str):
        rng = np.random.default_rng(hash(seed) % (2**31))
        vec = rng.standard_normal(self.dim).astype(np.float32)
        return vec / np.linalg.norm(vec)

    def embed_text(self, text, normalize=True, prefix=""):
        return self._unit(text)

    def embed_with_tokens(self, text, top_percent=0.35, token_selector=None, prefix=""):
        from jmfts_core.embedding import EmbeddingResult, TokenEmbeddingResult

        words = (text.split() or ["empty"])[:3]
        return EmbeddingResult(
            document_embedding=self._unit(text),
            token_embeddings=[
                TokenEmbeddingResult(
                    token_idx=i,
                    token_text=w,
                    importance_score=1.0 - i * 0.2,
                    embedding=self._unit(f"{text}_{i}"),
                )
                for i, w in enumerate(words)
            ],
        )

    def truncate_embedding(self, embedding, target_dim, normalize=True):
        trunc = embedding[:target_dim].copy()
        if normalize:
            norm = np.linalg.norm(trunc)
            if norm > 0:
                trunc /= norm
        return trunc


@pytest.fixture
def api(db_session):
    """A TestClient bound to the savepoint-wrapped session, with embedding mocked."""
    from jmfts_core.rest.wiring import get_db

    svc = _MockEmbeddingService()

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with (
        patch("jmfts_core.repositories.document.get_embedder", return_value=svc),
        patch("jmfts_core.repositories.search.get_embedding_service", return_value=svc),
    ):
        yield TestClient(app, headers=AUTH_HEADERS)
    app.dependency_overrides.pop(get_db, None)


@requires_db
def test_create_document_indexes_for_bm25(api, db_session):
    """The README's worked example: create, then find it with the BM25 leg.

    Before ``auto_index_bm25`` this document had vectors and no postings, so it was
    reachable by ``/search/vector`` and ``/search/fulltext`` and invisible to ``bm25``.
    """
    created = api.post(
        "/documents",
        json={
            "title": "Ada",
            "content": "Ada Lovelace wrote the first algorithm",
            "usetype": "chunk",
        },
    )
    assert created.status_code in (200, 201), created.text
    doc_id = created.json()["id"]

    hits = api.post("/search/bm25", json={"query": "Lovelace", "limit": 10})
    assert hits.status_code == 200, hits.text
    assert doc_id in [h["document"]["id"] for h in hits.json()["results"]]


@requires_db
def test_opting_out_of_bm25_indexing_leaves_the_document_out_of_the_index(api):
    created = api.post(
        "/documents",
        json={
            "title": "Grace",
            "content": "Grace Hopper coined the compiler",
            "usetype": "chunk",
            "auto_index_bm25": False,
        },
    )
    assert created.status_code in (200, 201), created.text
    doc_id = created.json()["id"]

    hits = api.post("/search/bm25", json={"query": "Hopper", "limit": 10})
    assert doc_id not in [h["document"]["id"] for h in hits.json()["results"]]


@requires_db
def test_search_reports_the_exclusions_it_applied(api):
    """The short-result-list defect: the filter is now on the wire."""
    from jmfts_core.config import get_settings

    resp = api.post("/search/vector", json={"query": "anything", "limit": 5})
    assert resp.status_code == 200, resp.text
    applied = resp.json()["applied"]
    assert applied["exclude_types"] == list(get_settings().search_exclude_usetypes)
    assert applied["limit"] == 5
    # A single-method search fuses nothing, so these read as not-applicable rather than
    # as an empty fusion.
    assert applied["methods"] is None
    assert applied["weights"] is None


@requires_db
def test_hybrid_reports_the_methods_and_weights_it_fused(api):
    resp = api.post("/search/hybrid", json={"query": "anything", "limit": 5})
    assert resp.status_code == 200, resp.text
    applied = resp.json()["applied"]
    assert applied["methods"] == list(DEFAULT_HYBRID_METHODS)
    assert applied["weights"] == TUNED_HYBRID_WEIGHTS


@requires_db
def test_hybrid_echoes_a_named_method_and_the_weight_it_got(api):
    """A caller who does name ``fulltext`` can see it was fused at 1.0."""
    resp = api.post(
        "/search/hybrid",
        json={"query": "anything", "limit": 5, "methods": ["vector", "fulltext", "bm25"]},
    )
    assert resp.status_code == 200, resp.text
    applied = resp.json()["applied"]
    assert applied["methods"] == ["vector", "fulltext", "bm25"]
    assert "fulltext" not in applied["weights"]


@requires_db
def test_hybrid_rejects_an_unknown_method_over_the_wire(api):
    resp = api.post("/search/hybrid", json={"query": "x", "methods": ["vector", "maxsim "]})
    assert resp.status_code == 422, resp.text


@requires_db
def test_capabilities_corpus_counts_are_opt_in(api):
    assert api.get("/capabilities").json()["corpus"] is None
    corpus = api.get("/capabilities", params={"corpus": True}).json()["corpus"]
    assert corpus is not None
    assert corpus["documents"] >= corpus["documents_with_vectors"]
    assert isinstance(corpus["bm25_indexes"], list)


@requires_db
def test_the_access_audit_says_what_is_unprotected(api, db_session):
    """With no grants anywhere, every document is open — and now it says so."""
    from jmfts_core.repositories.document import DocumentRepository

    root = DocumentRepository(db_session).create(
        title="Ungoverned root", content="open to anyone", auto_embed=False
    )
    db_session.flush()

    resp = api.get("/access/audit")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_control_roots"] == 0
    assert body["ungoverned_documents"] == body["total_documents"]
    assert root.id in [t["id"] for t in body["trees"]]


@requires_db
def test_a_grant_moves_its_tree_out_of_the_audit(api, db_session):
    """The audit is the inverse of the grant table, and has to track it."""
    from jmfts_core.models.principal import AccessGrant, Principal
    from jmfts_core.repositories.document import DocumentRepository

    repo = DocumentRepository(db_session)
    root = repo.create(title="Governed root", content="restricted", auto_embed=False)
    db_session.flush()

    principal = Principal(name="audit-test-principal", is_owner=False)
    db_session.add(principal)
    db_session.flush()
    db_session.add(AccessGrant(document_id=root.id, principal_id=principal.id, level="read"))
    db_session.flush()

    body = api.get("/access/audit").json()
    assert body["access_control_roots"] >= 1
    assert root.id not in [t["id"] for t in body["trees"]]
