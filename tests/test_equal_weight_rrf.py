"""Equal-weight RRF baseline: hybrid_search weight resolution.

``hybrid_search`` fuses with Reciprocal Rank Fusion, with a per-method multiplier on each
RRF term. The tuned default (0.86/0.14) is what the successive-halving sweep produced;
this test pins the new None-vs-empty distinction that exposes plain equal-weight RRF as a
first-class, tuning-free baseline without silently changing the default:

    weights=None -> tuned default (production ranking, unchanged)
    weights={}   -> equal weight (every method 1.0)

The two identity assertions are deterministic given one corpus (same inputs -> same
fusion), so they prove the resolution precisely without depending on a rank flip.
"""

from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import SearchRepository

# Two docs pulling in different lexical/semantic directions, so vector and bm25 need not
# agree — enough for the weight to matter, without asserting a specific flip (which would
# depend on the embedding model).
_DOCS = [
    ("Weather report", "the rain in spain falls mainly on the plain today"),
    ("Machine learning", "gradient descent optimizes neural network weights over epochs"),
    ("Cooking", "simmer the tomato sauce with basil and garlic for an hour"),
]
_QUERY = "how do neural networks learn weights"


def _seed(db_session) -> SearchRepository:
    doc_repo = DocumentRepository(db_session)
    search_repo = SearchRepository(db_session)
    for title, content in _DOCS:
        doc = doc_repo.create(title=title, content=content, auto_embed=True)
        db_session.flush()
        search_repo.index_document(doc.id, "default")
    db_session.flush()
    return search_repo


def _ranking(results):
    return [(r.document.id, round(r.score, 12)) for r in results]


def test_empty_weights_is_equal_weight_rrf(db_session):
    """{} must fuse identically to explicitly giving every method weight 1.0."""
    repo = _seed(db_session)
    empty = repo.hybrid_search(_QUERY, limit=3, methods=["vector", "bm25"], weights={})
    ones = repo.hybrid_search(
        _QUERY, limit=3, methods=["vector", "bm25"], weights={"vector": 1.0, "bm25": 1.0}
    )
    assert _ranking(empty) == _ranking(ones)


def test_none_weights_is_the_tuned_default(db_session):
    """None must fuse identically to the explicit tuned 0.86/0.14 default (unchanged)."""
    repo = _seed(db_session)
    default = repo.hybrid_search(_QUERY, limit=3, methods=["vector", "bm25"], weights=None)
    tuned = repo.hybrid_search(
        _QUERY, limit=3, methods=["vector", "bm25"], weights={"vector": 0.86, "bm25": 0.14}
    )
    assert _ranking(default) == _ranking(tuned)


def test_empty_dict_no_longer_collapses_to_the_default(db_session):
    """Regression: the old `weights or default` swallowed {} into 0.86/0.14.

    With more than one candidate the equal-weight and tuned fusions must be able to
    differ; asserting they are not forced equal proves {} is honoured as its own request
    rather than coerced back to the tuned default.
    """
    repo = _seed(db_session)
    equal = repo.hybrid_search(_QUERY, limit=3, methods=["vector", "bm25"], weights={})
    tuned = repo.hybrid_search(_QUERY, limit=3, methods=["vector", "bm25"], weights=None)
    # Same doc set surfaces, but the fused scores are computed from different weights, so
    # at least one score must differ (a bm25 hit is weighted 1.0 vs 0.14).
    assert dict(_ranking(equal)) != dict(_ranking(tuned))
