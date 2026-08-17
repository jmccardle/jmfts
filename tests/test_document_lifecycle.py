"""Ingest lifecycle — the `settled` column, the partial retrieval indexes, and the
`get_subtree` walk that refuses to under-report.

INGEST_SPEC.md Part 2. Three things are being guarded here and they fail differently:

1. **The state set is closed.** `settled` is TEXT with a CHECK, so a typo is rejected by
   the database rather than becoming a fourth state nothing knows how to sweep.
2. **The partial indexes are provable.** `idx_documents_embed` and
   `idx_documents_content_fts` are partial on `settled = 'settled'`; a query without
   that predicate cannot use them, which turns the "optimisation" into a silent
   sequential scan. The EXPLAIN tests below assert BOTH directions — the index is used
   when the predicate is present and NOT used when it is absent — because only the pair
   proves partiality rather than mere presence.
3. **A settled-only walk of an unsettled root is an error, not a short list.** The whole
   point of the parameter is that a caller cannot receive half a tree and mistake it for
   the whole one.

One finding recorded here rather than lost: `SearchRepository.vector_search` orders by
`(1 - (embed <=> q)) DESC`, and pgvector's HNSW index can only serve
`ORDER BY embed <=> q` ASC. Verified by EXPLAIN with `enable_seqscan = off`: the score
form seq-scans and sorts even when the index is otherwise reachable. So
`idx_documents_embed` was already unused by that query before it became partial — making
it partial takes nothing away, but the index will stay unused until the ORDER BY is
rewritten. `test_embed_index_is_partial_on_settled` therefore exercises the canonical
ANN shape, which is what the index exists to serve.

DB integration tests on the shared savepoint-rollback fixture (nothing is committed).
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jmfts_core.models.document import Document, SETTLED_STATES
from jmfts_core.repositories.document import DocumentRepository, InFlightSubtreeError
from jmfts_core.repositories.search import SearchRepository
from jmfts_core.services.document_service import DocumentService

# A constant query vector — the values are irrelevant, only that it is a 768-dim literal
# the planner can see (a subquery or bind that is not a constant defeats the HNSW path).
_QUERY_VEC = "[" + ",".join(["0.01"] * 768) + "]"


def _doc(repo, title, content, *, parent_id=None, settled="settled"):
    """A document with enough content to be searchable, never embedded (no model in CI)."""
    return repo.create(
        title=title,
        content=content,
        parent_id=parent_id,
        auto_embed=False,
        settled=settled,
    )


def _seed_indexable_rows(session, n=12):
    """Rows with content and a real 768-dim vector, inserted in raw SQL.

    Deliberately not through the repository: this seeds the planner, not the model, and
    generating real embeddings here would make an index test depend on a GPU.
    """
    session.execute(
        text("""
            INSERT INTO documents (title, content, embed, settled)
            SELECT 'lifecycle doc ' || g,
                   'the quick brown fox jumps over the lazy dog number ' || g,
                   (SELECT ('[' || string_agg(random()::text, ',') || ']')::vector
                    FROM generate_series(1, 768)),
                   'settled'
            FROM generate_series(1, :n) g
            """),
        {"n": n},
    )
    session.execute(text("ANALYZE documents"))


def _plan(session, sql, params=None):
    """The EXPLAIN output of ``sql`` as one string, with sequential scans disabled.

    `enable_seqscan = off` is the point of the exercise: it makes the planner take an
    index if one is usable at all, so a seq scan in the output means no index COULD be
    used — not merely that the table was too small to bother. SET LOCAL is scoped to the
    surrounding transaction, which the fixture rolls back.
    """
    session.execute(text("SET LOCAL enable_seqscan = off"))
    rows = session.execute(text("EXPLAIN (COSTS OFF) " + sql), params or {}).all()
    return "\n".join(r[0] for r in rows)


# ---------------------------------------------------------------------------
# 1. The column: closed set, and the default
# ---------------------------------------------------------------------------


class TestSettledColumn:
    def test_default_is_settled(self, db_session):
        """Every row written without an opinion is finished.

        This is the value that keeps the pre-lifecycle world working: the synchronous
        pipeline creates a node and is immediately done with it, so 'settled' is what it
        means, not merely what is convenient.
        """
        repo = DocumentRepository(db_session)
        doc = _doc(repo, "plain", "a document created with no lifecycle opinion")
        db_session.flush()
        assert doc.settled == "settled"

    def test_raw_insert_also_defaults_to_settled(self, db_session):
        """The server_default, not just the ORM default — raw SQL writes bypass Python."""
        doc_id = db_session.execute(
            text("INSERT INTO documents (title, content) VALUES ('raw', 'body') RETURNING id")
        ).scalar_one()
        assert (
            db_session.execute(
                text("SELECT settled FROM documents WHERE id = :i"), {"i": doc_id}
            ).scalar_one()
            == "settled"
        )

    def test_check_constraint_rejects_an_unknown_state(self, db_session):
        """The database is the authority on the state set, not convention."""
        with pytest.raises(IntegrityError):
            db_session.execute(
                text(
                    "INSERT INTO documents (title, content, settled) "
                    "VALUES ('bad', 'body', 'pending')"
                )
            )

    @pytest.mark.parametrize("state", SETTLED_STATES)
    def test_every_declared_state_is_accepted(self, db_session, state):
        repo = DocumentRepository(db_session)
        doc = _doc(repo, state, f"a document in state {state}", settled=state)
        db_session.flush()
        assert doc.settled == state

    def test_create_rejects_an_unknown_state_before_it_reaches_the_db(self, db_session):
        """A typo should read as a bad argument at the call site, not as an
        IntegrityError at flush time an arbitrary distance away."""
        repo = DocumentRepository(db_session)
        with pytest.raises(ValueError, match="settled must be one of"):
            _doc(repo, "bad", "body", settled="in-flight")

    def test_settled_reaches_the_api_contract(self, db_session):
        """The to_dict/DocumentResponse coverage guards catch a dropped field only if the
        column is actually threaded; assert the round trip directly too."""
        from jmfts_core.contracts.document import DocumentResponse

        repo = DocumentRepository(db_session)
        doc = _doc(repo, "in flight", "body of an unfinished node", settled="in_flight")
        db_session.flush()
        assert DocumentResponse.from_document(doc).settled == "in_flight"


# ---------------------------------------------------------------------------
# 2. The partial indexes
# ---------------------------------------------------------------------------


class TestPartialIndexes:
    def test_fts_index_is_partial_on_settled(self, db_session):
        _seed_indexable_rows(db_session)
        match = (
            "to_tsvector('english', COALESCE(title,'') || ' ' || COALESCE(content,'')) "
            "@@ websearch_to_tsquery('english', 'fox')"
        )
        with_pred = _plan(
            db_session,
            f"SELECT id FROM documents WHERE {match} AND settled = 'settled' LIMIT 10",
        )
        without_pred = _plan(db_session, f"SELECT id FROM documents WHERE {match} LIMIT 10")

        assert "idx_documents_content_fts" in with_pred, with_pred
        # The negative half is the one that matters: it proves the index is PARTIAL, so
        # a query that forgets the predicate is paying for a full scan.
        assert "idx_documents_content_fts" not in without_pred, without_pred

    def test_embed_index_is_partial_on_settled(self, db_session):
        _seed_indexable_rows(db_session)
        order = f"ORDER BY embed <=> '{_QUERY_VEC}'::vector LIMIT 5"
        with_pred = _plan(
            db_session,
            f"SELECT id FROM documents WHERE embed IS NOT NULL " f"AND settled = 'settled' {order}",
        )
        without_pred = _plan(
            db_session, f"SELECT id FROM documents WHERE embed IS NOT NULL {order}"
        )

        assert "idx_documents_embed" in with_pred, with_pred
        assert "idx_documents_embed" not in without_pred, without_pred

    def test_path_index_stays_usable_for_unfiltered_tree_walks(self, db_session):
        """idx_documents_path is deliberately NOT partial (migration 008 explains why).

        The tree, graph and subtree-RBAC machinery all issue bare `path @>` containment
        with no lifecycle predicate — they are the code responsible for in-flight nodes,
        so they cannot filter them out. If someone makes this index partial, every one of
        those becomes a sequential scan over `documents`, silently. This test is the
        tripwire.
        """
        repo = DocumentRepository(db_session)
        root = _doc(repo, "root", "root body", settled="in_flight")
        _doc(repo, "child", "child body", parent_id=root.id, settled="in_flight")
        db_session.flush()
        _seed_indexable_rows(db_session)

        plan = _plan(
            db_session,
            "SELECT id FROM documents WHERE path @> jsonb_build_array(:root)",
            {"root": root.id},
        )
        assert "idx_documents_path" in plan, plan


# ---------------------------------------------------------------------------
# 3. Retrieval excludes what is not settled
# ---------------------------------------------------------------------------


class TestRetrievalExcludesInFlight:
    def test_fulltext_search_skips_in_flight_documents(self, db_session):
        repo = DocumentRepository(db_session)
        settled = _doc(repo, "settled note", "quokka husbandry for beginners")
        flight = _doc(repo, "unsettled note", "quokka husbandry for beginners", settled="in_flight")
        failed = _doc(repo, "dead note", "quokka husbandry for beginners", settled="failed")
        db_session.flush()

        found = {r.document.id for r in SearchRepository(db_session).fulltext_search("quokka")}
        assert settled.id in found
        assert flight.id not in found
        assert failed.id not in found


# ---------------------------------------------------------------------------
# 4. get_subtree: settled-only by default, and loud about it
# ---------------------------------------------------------------------------


def _mixed_tree(session):
    """A settled root over a mix of settled / in-flight / failed descendants.

    root (settled)
      a  (settled)
      b  (in_flight)
        b1 (in_flight)
      c  (failed)

    The root is pinned back to 'settled' after construction. Since the Part 5 scheduler
    landed, ``create`` un-settles a parent that gains an in-flight child (restoring the
    recursive invariant), so this shape is no longer what a fresh build produces. It is
    still a REAL state: spec 6.3's correction sets an affected node back to 'in_flight'
    and relies on the settle walk to un-settle each ancestor afterwards, so between those
    two events a settled node sits over an unsettled subtree. That is precisely the
    window in which ``get_subtree``'s filter has to behave, which is what these tests
    check — so the state is staged deliberately rather than assumed.
    """
    repo = DocumentRepository(session)
    root = _doc(repo, "root", "root of a partially ingested tree")
    a = _doc(repo, "a", "a settled child", parent_id=root.id)
    b = _doc(repo, "b", "a child still in flight", parent_id=root.id, settled="in_flight")
    b1 = _doc(repo, "b1", "a grandchild still in flight", parent_id=b.id, settled="in_flight")
    c = _doc(repo, "c", "a child that died", parent_id=root.id, settled="failed")
    root.settled = "settled"
    session.flush()
    return repo, {"root": root, "a": a, "b": b, "b1": b1, "c": c}


class TestGetSubtree:
    def test_raises_when_the_root_itself_is_in_flight(self, db_session):
        repo = DocumentRepository(db_session)
        root = _doc(repo, "root", "an unfinished root", settled="in_flight")
        db_session.flush()

        with pytest.raises(InFlightSubtreeError) as exc:
            repo.get_subtree(root.id)
        assert exc.value.root_id == root.id
        assert exc.value.state == "in_flight"

    def test_raises_when_the_root_failed(self, db_session):
        """`failed` is not settled either — a permanently dead root's tree is no more
        complete than an in-flight one's, and reporting it as complete is the same bug."""
        repo = DocumentRepository(db_session)
        root = _doc(repo, "root", "a dead root", settled="failed")
        db_session.flush()

        with pytest.raises(InFlightSubtreeError):
            repo.get_subtree(root.id)

    def test_in_flight_error_is_not_a_lookup_error(self, db_session):
        """DocumentService already maps LookupError to 404 for missing AND hidden. An
        in-flight root is neither, and collapsing it into 404 would make a tree that is
        mid-ingestion indistinguishable from one that never existed."""
        assert not issubclass(InFlightSubtreeError, LookupError)

    def test_default_returns_only_settled_descendants(self, db_session):
        repo, t = _mixed_tree(db_session)
        got = {d.id for d in repo.get_subtree(t["root"].id)}
        assert got == {t["root"].id, t["a"].id}

    def test_include_in_flight_returns_the_whole_tree(self, db_session):
        repo, t = _mixed_tree(db_session)
        got = {d.id for d in repo.get_subtree(t["root"].id, include_in_flight=True)}
        assert got == {t[k].id for k in ("root", "a", "b", "b1", "c")}

    def test_include_in_flight_walks_an_in_flight_root(self, db_session):
        repo = DocumentRepository(db_session)
        root = _doc(repo, "root", "an unfinished root", settled="in_flight")
        kid = _doc(repo, "kid", "its unfinished child", parent_id=root.id, settled="in_flight")
        db_session.flush()

        got = {d.id for d in repo.get_subtree(root.id, include_in_flight=True)}
        assert got == {root.id, kid.id}

    def test_missing_root_still_returns_empty(self, db_session):
        """Unchanged behaviour: a root that does not exist is an empty list, not a raise.
        The new error is specifically about a root that exists but is not finished."""
        repo = DocumentRepository(db_session)
        assert repo.get_subtree(-1) == []

    def test_service_propagates_the_error_and_maps_it_to_409(self, db_session):
        repo = DocumentRepository(db_session)
        root = _doc(repo, "root", "an unfinished root", settled="in_flight")
        _doc(repo, "kid", "its unfinished child", parent_id=root.id, settled="in_flight")
        db_session.flush()

        service = DocumentService(db_session)
        with pytest.raises(InFlightSubtreeError):
            service.get_subtree(root.id)

        sub = service.get_subtree(root.id, include_in_flight=True)
        assert sub.total == 2

        # 409, not the 404 the LookupError path already owns.
        spec = DocumentService.get_subtree.__jmfts_expose__
        assert spec.errors[InFlightSubtreeError] == 409
        assert spec.errors[LookupError] == 404


# ---------------------------------------------------------------------------
# 5. The frontier
# ---------------------------------------------------------------------------


class TestSettleFrontier:
    def test_counts_a_mixed_tree_including_the_root(self, db_session):
        repo, t = _mixed_tree(db_session)
        assert repo.settle_frontier(t["root"].id) == {
            "settled": 2,  # root + a
            "in_flight": 2,  # b + b1
            "failed": 1,  # c
            "total": 5,
        }

    def test_reports_every_state_even_when_empty(self, db_session):
        """All three keys, always — a caller subtracting or displaying them should never
        have to probe for a missing key just because nothing has failed yet."""
        repo = DocumentRepository(db_session)
        root = _doc(repo, "root", "a fully settled tree")
        _doc(repo, "kid", "a settled child", parent_id=root.id)
        db_session.flush()

        assert repo.settle_frontier(root.id) == {
            "settled": 2,
            "in_flight": 0,
            "failed": 0,
            "total": 2,
        }

    def test_is_scoped_to_the_subtree(self, db_session):
        """Counting must not drift into a sibling tree — the frontier of one ingestion
        says nothing about another."""
        repo, t = _mixed_tree(db_session)
        other = _doc(repo, "other", "an unrelated in-flight root", settled="in_flight")
        _doc(repo, "other kid", "its child", parent_id=other.id, settled="in_flight")
        db_session.flush()

        assert repo.settle_frontier(t["root"].id)["total"] == 5
        assert repo.settle_frontier(other.id) == {
            "settled": 0,
            "in_flight": 2,
            "failed": 0,
            "total": 2,
        }

    def test_counts_a_deep_frontier_from_an_interior_node(self, db_session):
        repo, t = _mixed_tree(db_session)
        assert repo.settle_frontier(t["b"].id) == {
            "settled": 0,
            "in_flight": 2,  # b + b1
            "failed": 0,
            "total": 2,
        }

    def test_unknown_root_is_all_zeros(self, db_session):
        repo = DocumentRepository(db_session)
        assert repo.settle_frontier(-1) == {
            "settled": 0,
            "in_flight": 0,
            "failed": 0,
            "total": 0,
        }


# ---------------------------------------------------------------------------
# 6. The ORM model itself
# ---------------------------------------------------------------------------


def test_unflushed_document_has_no_state_yet():
    """Both defaults fire at INSERT, so a Document that has never been flushed reads
    None — the same as `created_at`. Pinned because it is the non-obvious half: it is
    what forces anything building a synthetic Document to say what state it means."""
    assert Document(title="t", content="c").settled is None


def test_response_refuses_a_document_with_no_state():
    """`from_document` will not substitute 'settled' for a row whose state is unknown.

    Quietly defaulting would report an unfinished node as finished, which is the exact
    class of answer the lifecycle exists to prevent — and it would do it on the one
    surface a client uses to decide whether a document is ready."""
    from pydantic import ValidationError

    from jmfts_core.contracts.document import DocumentResponse

    doc = Document(id=1, title="t", content="c", path=[], structured_content={})
    with pytest.raises(ValidationError):
        DocumentResponse.from_document(doc)
