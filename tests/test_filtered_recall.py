"""`vector_search` reaches `idx_documents_embed`, and a filtered HNSW scan still answers.

`docs/SPRINT_0_4_0.md` Block A, all three steps, on one fixture. The block's step list was
written before the defect underneath it was found, and this file records both.

**What step 1 was, and what it turned out to be.** It was written to reproduce the
truncation `scripts/filtered_recall.py` measured — an HNSW scan under the RBAC read gate
returning an empty page — through the real `vector_search`. It did not reproduce there.
`vector_search` ordered by the SIMILARITY LABEL rather than by the distance operator: it
built ``(1 - embed <=> q).label("score")`` and then ordered by ``score DESC``. pgvector's
HNSW index answers ``ORDER BY col <=> q`` ASCENDING and nothing else; the two expressions
describe the same total order and Postgres does not rewrite one into the other, so
`idx_documents_embed` (`sql/schema.sql:478`) was built, maintained on every write, and read
by nothing. Measured on the fixture below: the label form costs 2675.91 and top-N heapsorts
a Seq Scan, the operator form costs 8.03 and index-scans, and `enable_seqscan = off` does
not move the choice — the planner was not preferring the sort, it had no index option to
prefer. So vector search was O(rows) per query, AND the truncation was not live on this
path, because truncation needs the index. Correcting the ORDER BY is what makes it live,
which is why steps 2 and 3 are PREREQUISITES of step 1 rather than peers of it.

**What each test guards now**, in the order the steps run:

* `test_the_construction_is_sound` — the fixture, unchanged. Nothing below proves anything
  if the readable rows do not sort last.
* `test_shipped_order_by_reaches_the_index` (step 1) — something reads
  `idx_documents_embed`. Red before the ORDER BY was corrected, green after.
* `test_the_native_order_by_does_not_truncate_under_the_read_gate` (step 2) — the same
  scan that returned an EMPTY page now returns a full one. It was
  `test_the_native_order_by_truncates_under_the_read_gate`, which asserted the opposite
  against a hand-built statement; see its docstring for why it inverted rather than
  disappeared.
* `test_a_short_page_says_it_is_short` (step 3) — the flag that survives step 2, because
  both iterative modes stop at `hnsw.max_scan_tuples` and an older server does not iterate
  at all.

The construction is `filtered_recall.py`'s and it is exact rather than sampled: a cluster
of UNREADABLE documents beside the query vector, a smaller cluster of READABLE ones in the
opposite half-space. Every readable document is a correct answer and every one of them
sorts strictly below the whole unreadable cluster, so the right answer is known in advance
and any shortfall is the index rather than the data running out.

No model is loaded. `vector_search` takes the query vector as an argument, unlike
`vector_search_text` which embeds, so this runs on a base install.
"""

import math
import random

from sqlalchemy import event, text

from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import SearchRepository

#: `documents.embed` is `Vector(768)` (`models/document.py:91`).
DIM = 768
#: Comfortably above pgvector's default `hnsw.ef_search` of 40, so a walk that stops at
#: `ef_search` candidates has taken none of the readable rows.
UNREADABLE = 200
#: More than `LIMIT`, so a short page cannot be explained by there being too few answers.
READABLE = 30
LIMIT = 10

SEED = 20260904

#: The index `vector_search` is supposed to be using. Partial on `settled = 'settled'`
#: (`sql/schema.sql:478`-`:480`), which is why the repository carries that predicate.
EMBED_INDEX = "idx_documents_embed"


def _unit(values):
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def _near(rng):
    """A vector close to the query, which is the first basis vector."""
    v = [rng.gauss(0.0, 0.35) for _ in range(DIM)]
    v[0] += 1.0
    return _unit(v)


def _far(rng):
    """A vector in the OPPOSITE half-space from the query, not merely orthogonal.

    Cosine distance to the query is then above 1, so every readable row sorts strictly
    below every unreadable one and the ordering is a property of the construction rather
    than of HNSW's approximation.
    """
    v = [rng.gauss(0.0, 1.0) for _ in range(DIM)]
    v[0] = -abs(v[0]) - 2.0
    return _unit(v)


def _query():
    return _unit([1.0] + [0.0] * (DIM - 1))


def _seed(session):
    """Two roots: `hidden` under an ACR with no grant to the caller, `open` ungoverned.

    Returns the outsider principal and the ids it may read. The ungoverned root is what
    makes the readable set non-empty without a grant — `readable_filter` admits a document
    no ACR governs, so the caller's own subtree needs no setup beyond existing.
    """
    repo = DocumentRepository(session)
    rng = random.Random(SEED)

    def mk(title, parent, vector):
        doc = repo.create(title=title, content=title, parent_id=parent, auto_embed=False)
        doc.embed = vector
        session.flush()
        return doc.id

    hidden = mk("hidden", None, _near(rng))
    for i in range(UNREADABLE - 1):
        mk(f"hidden-{i}", hidden, _near(rng))

    opened = mk("open", None, _far(rng))
    readable = {opened}
    for i in range(READABLE - 1):
        readable.add(mk(f"open-{i}", opened, _far(rng)))

    outsider = PrincipalModel(name="outsider")
    insider = PrincipalModel(name="insider")
    session.add_all([outsider, insider])
    session.flush()
    # The ACR on `hidden`. With no grant anywhere there are no access-control roots at all
    # and `readable_filter` returns None, which would make these tests a no-op.
    session.add(AccessGrant(document_id=hidden, principal_id=insider.id, level="read"))
    session.flush()

    return CurrentPrincipal(id=outsider.id, name="outsider", is_owner=False), readable


def _capture_plan(session, call):
    """Run `call`, capture the last SELECT it issued, and return that statement's plan.

    The statement is taken off the connection rather than rebuilt here, so what gets
    explained is what `vector_search` actually sent. Rebuilding it in the test would let
    the two drift apart, which is the whole failure this file is about.
    """
    seen = []
    bind = session.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append((statement, parameters))

    event.listen(bind, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(bind, "before_cursor_execute", record)

    assert seen, "no SELECT was issued"
    statement, parameters = seen[-1]
    # `exec_driver_sql`, not `session.execute(text(...))`. The captured statement still
    # carries psycopg2's `%(name)s` placeholders, and `text()` reads `%` as its own escape
    # and doubles them, which reaches the server as a syntax error.
    rows = (
        session.connection()
        .exec_driver_sql("EXPLAIN (ANALYZE) " + statement, parameters)
        .fetchall()
    )
    return "\n".join(r[0] for r in rows)


def _search_as(session, principal, limit=LIMIT, **kwargs):
    """Run `vector_search` as `principal` and hand back the repository with it.

    The repository, not just the rows, because Block A step 3's truncation report is
    `SearchRepository.scan_truncated` — set by the scan, read afterwards by
    `SearchService._applied`. A test that only took the rows could not tell a page that is
    short from a page that says it is short.
    """
    repo = SearchRepository(session)
    token = set_principal(principal)
    try:
        return repo.vector_search(_query(), limit=limit, **kwargs), repo
    finally:
        reset_principal(token)


def test_the_construction_is_sound(db_session):
    """The owner's page is entirely unreadable-cluster, so the readable rows sort last.

    If this fails the fixture is wrong and nothing below proves anything.
    """
    _, readable = _seed(db_session)
    got, _repo = _search_as(db_session, None)  # unbound caller bypasses the gate
    assert len(got) == LIMIT
    assert not ({r.document.id for r in got} & readable), "readable rows must sort last"


def test_shipped_order_by_reaches_the_index(db_session):
    """Step 1. FAILED on 0.3.0: `ORDER BY score DESC` sorts and the HNSW index is dead.

    `sql/schema.sql:478` builds `idx_documents_embed` and every write maintains it. This
    asserts something reads it. It is the whole content of the correction — the SELECT list
    still returns `1 - distance`, so nothing a caller can see changed except the plan.
    """
    outsider, _ = _seed(db_session)
    plan = _capture_plan(db_session, lambda: _search_as(db_session, outsider))
    assert EMBED_INDEX in plan, (
        f"{EMBED_INDEX} is absent from the plan, so every vector search is a full scan "
        f"and a sort:\n{plan[:2000]}"
    )


def test_the_native_order_by_does_not_truncate_under_the_read_gate(db_session):
    """Step 2. This test INVERTED, deliberately, and this is the record of it.

    It was `test_the_native_order_by_truncates_under_the_read_gate`, and it passed on
    0.3.0. What it guarded then: the shipped `vector_search` could not reach the index, so
    it asserted against a hand-built copy of the statement carrying the ORDER BY the index
    would need, and it asserted the caller got an EMPTY page while thirty documents it may
    read matched — the truncation `scripts/filtered_recall.py` measured, on the appliance's
    own schema. Its job was to state, before the fix landed, what the corrected ORDER BY
    would do to a caller, so that reaching the index WITHOUT addressing the scan turned it
    red.

    What it guards now: the same thing, from the other side. Step 1 corrected the ORDER BY
    and step 2 set `hnsw.iterative_scan`, so the scan re-enters the graph when the read
    gate empties a batch and the page comes back FULL. The hand-built statement is gone
    because it no longer differs from the shipped one — that identity is what step 1 was.

    `enable_seqscan = off` stays. Without it the assertion rides a cost estimate that moves
    with the machine: at 230 rows Postgres could price a Seq Scan and an exact top-N sort
    below the index scan, the page would be complete for a reason that has nothing to do
    with iterative scan, and the test would pass while proving nothing about HNSW.
    """
    outsider, readable = _seed(db_session)
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    try:
        got, repo = _search_as(db_session, outsider)
        plan = _capture_plan(db_session, lambda: _search_as(db_session, outsider))
    finally:
        db_session.execute(text("SET LOCAL enable_seqscan = on"))

    assert EMBED_INDEX in plan, f"the assertion below must be about HNSW:\n{plan[:2000]}"
    assert len(got) == LIMIT, (
        f"the read gate truncated the scan: {len(got)} of {LIMIT} with {len(readable)} "
        f"readable documents present"
    )
    assert {r.document.id for r in got} <= readable, "the gate must still hold"
    assert repo.scan_truncated is False, "a full page is not a short one"


def test_a_short_page_says_it_is_short(db_session):
    """Step 3, and it is not made redundant by step 2.

    Both iterative modes stop at `hnsw.max_scan_tuples`, and a server too old for the GUC
    at all does not iterate — so a caller still has to be able to tell a complete page from
    a bounded one. This asks for more rows than the readable set holds, which is the only
    shape of short page this fixture can produce on purpose, and asserts the flag reports
    it rather than letting a short page pass for an exhausted corpus.

    What the flag claims is exactly the fact and no more. `READABLE` = 30 rows really are
    everything the outsider may read, so this particular short page is honest exhaustion —
    and the server says "short" rather than "complete" because it cannot tell the two
    apart, which is what `AppliedFilters.truncated` documents.
    """
    outsider, readable = _seed(db_session)
    over = len(readable) + LIMIT
    got, repo = _search_as(db_session, outsider, limit=over)

    assert len(got) < over, "the fixture must not be able to fill this page"
    assert repo.scan_truncated is True, (
        f"a page of {len(got)} against a limit of {over} must report itself short; "
        f"got scan_truncated={repo.scan_truncated!r}"
    )
