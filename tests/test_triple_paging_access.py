"""``LIMIT`` on ``query_triples`` must count rows the caller may actually READ.

``SPRINT_0_5_0.md`` Block A finding 7, and the same family as ``SPRINT_0_4_0.md`` Block A:
a filter applied AFTER the page is cut hands back a short page that says nothing about
being short, and a short page is indistinguishable from an exhausted corpus.

**The shape of the defect.** ``query_triples`` used to ``ORDER BY id LIMIT n OFFSET k``,
then drop the rows whose subject or object the principal cannot read. Three consequences,
each demonstrated below:

1. A page can come back EMPTY with hundreds of readable matches behind it — the same
   empty page ``tests/test_filtered_recall.py`` measures on the retrieval side, reached
   through a different mechanism (a post-filter rather than an ANN scan bound).
2. ``offset`` counts rows the caller may not read, so page *k* and page *k+1* overlap or
   skip in a way that depends on somebody else's grants.
3. Any exhaustive walk — page until a short page arrives — stops at the first page the
   filter thinned, and returns a partial fact set that looks complete.

The fix is not a truncation flag. Unlike the ANN case, where "the scan stopped early" is
not observable from SQL, this filter is an ordinary predicate: pushed into the statement
that carries ``LIMIT``, the page is EXACT again and the ordinary paging contract (a full
page may have more behind it, a short page is the end) means what it says. So these tests
assert exactness, not a report.
"""

from contextlib import contextmanager

from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import OWNER, CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository

#: Objects of the hub's facts, alternating restricted/public in triple-id order. The
#: alternation is what makes the arithmetic visible: with the filter applied after the cut,
#: EVERY page of an even size loses half its rows, and the first page of size 3 is already
#: short.
PAIRS = 6


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
    """One public hub H with 12 facts hanging off it: R1, P1, R2, P2, … in triple-id order.

    Each ``R`` is under an access-control root granted only to the insider; each ``P`` is
    ungoverned and therefore readable by everyone with a token. So the outsider has exactly
    ``PAIRS`` readable facts, and they are every SECOND row of the id-ordered result.
    """
    docs = DocumentRepository(session)
    triples = TripleRepository(session)

    hub = docs.create(title="H", content="the hub", auto_embed=False)
    session.flush()
    knows = triples.create_predicate(name="knows")

    insider = _principal(session, "paging-insider")
    outsider = _principal(session, "paging-outsider")

    readable_triple_ids: list[int] = []
    all_triple_ids: list[int] = []
    for n in range(PAIRS):
        restricted = docs.create(title=f"R{n}", content="restricted", auto_embed=False)
        public = docs.create(title=f"P{n}", content="public", auto_embed=False)
        session.flush()
        session.add(AccessGrant(document_id=restricted.id, principal_id=insider.id, level="read"))
        session.flush()
        hidden = triples.create_triple(hub.id, knows.id, object_id=restricted.id)
        shown = triples.create_triple(hub.id, knows.id, object_id=public.id)
        session.flush()
        all_triple_ids += [hidden.id, shown.id]
        readable_triple_ids.append(shown.id)

    return hub.id, outsider, all_triple_ids, readable_triple_ids


def _walk(repo, entity_id, page_size):
    """Page until a short page arrives — the ordinary exhaustive-walk idiom."""
    collected: list[int] = []
    offset = 0
    while True:
        page = repo.query_triples(entity_id=entity_id, limit=page_size, offset=offset)
        collected += [t.id for t in page]
        if len(page) < page_size:
            return collected
        offset += page_size


# ── 1. the page the filter emptied ──────────────────────────────────────────


def test_a_first_page_of_restricted_rows_is_not_an_empty_result(db_session):
    """The outsider asks for one row and the store holds six they may read.

    Before the fix this returns ``[]``: triple id 1 is the R0 fact, the cut takes it, the
    filter drops it, and nothing in the empty list says six readable facts sit behind it.
    """
    hub, outsider, _, readable = _fixture(db_session)
    repo = TripleRepository(db_session)
    with _as(OWNER):
        assert len(repo.query_triples(entity_id=hub, limit=1)) == 1
    with _as(outsider):
        page = repo.query_triples(entity_id=hub, limit=1)
    assert [t.id for t in page] == readable[:1]


def test_a_full_page_is_full_of_rows_the_caller_may_read(db_session):
    """``LIMIT n`` means n VISIBLE rows, not n rows of which some are then taken away."""
    hub, outsider, all_ids, readable = _fixture(db_session)
    repo = TripleRepository(db_session)
    with _as(OWNER):
        assert [t.id for t in repo.query_triples(entity_id=hub, limit=4)] == all_ids[:4]
    with _as(outsider):
        assert [t.id for t in repo.query_triples(entity_id=hub, limit=4)] == readable[:4]


# ── 2. offset counts visible rows ───────────────────────────────────────────


def test_offset_skips_rows_the_caller_can_see_and_not_rows_somebody_else_can(db_session):
    """Otherwise paging depends on another principal's grants: the outsider's page 2 is
    decided by how many restricted rows happened to fall in page 1."""
    hub, outsider, _, readable = _fixture(db_session)
    repo = TripleRepository(db_session)
    with _as(outsider):
        assert [t.id for t in repo.query_triples(entity_id=hub, limit=2, offset=2)] == readable[2:4]
        assert [t.id for t in repo.query_triples(entity_id=hub, limit=2, offset=4)] == readable[4:6]
        # Past the end is empty, and it is the ONLY thing that is empty.
        assert repo.query_triples(entity_id=hub, limit=2, offset=PAIRS) == []


# ── 3. the exhaustive walk ──────────────────────────────────────────────────


def test_an_exhaustive_walk_does_not_stop_at_the_first_thinned_page(db_session):
    """The consequence finding 7 names. A caller pages until a short page arrives; before
    the fix the FIRST page of size 3 comes back with 2 rows — the walk reads that as the end
    of the store and returns a third of the facts, with nothing marking the loss."""
    hub, outsider, _, readable = _fixture(db_session)
    repo = TripleRepository(db_session)
    with _as(outsider):
        for page_size in (1, 2, 3, 5):
            assert _walk(repo, hub, page_size) == readable, f"page size {page_size}"


def test_the_owner_walk_is_unchanged(db_session):
    """The bypass path has no filter to push anywhere, and must page exactly as before."""
    hub, _, all_ids, _ = _fixture(db_session)
    repo = TripleRepository(db_session)
    with _as(OWNER):
        assert _walk(repo, hub, 5) == all_ids
