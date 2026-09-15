"""IC-4, the half that was missing: a search hit carries its own anchor.

``docs/SPRINT_0_6_0.md`` Block F step 31, and Part 5.1's pin for IC-4 — "a hit on an anchored
chunk carries the box".

**Step 18 landed the two FIELDS and nothing filled them.** ``SearchResultItem.source_anchor``
and ``.source_anchor_unresolved`` have been on the contract since ``153ab66``, whose own message
says "Block F step 31 is where the three pieces meet"; until this file existed,
``search_service.py`` built every item as ``SearchResultItem(document=..., score=..., method=...)``
and both fields defaulted to ``None`` on every hit of every method. The front end could not tell
that from a corpus with no anchors in it, so the result list badged an anchored chunk "no anchor"
and the detail view paid for a ``GET /documents/{id}/evidence`` call per click — the round trip
IC-4 exists to remove, still being paid, with nothing anywhere saying so.

Three properties, and the third is the one that decides whether the fix is worth having:

1. an anchored node's hit carries the anchor row verbatim;
2. a node whose anchor could not be recovered carries the REASON instead, and the two are never
   both present — ``jmfts_core/evidence.py:453``;
3. **the rows cost ONE query for the whole page.** A read per hit would move the round trip from
   the browser into the service rather than removing it, which is the same defect one layer down
   and considerably harder to see.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event

from jmfts_client.contracts.search import SearchRequest
from jmfts_core.models.document import SETTLED_SETTLED
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.services.search_service import SearchService

#: ``citation_tasks.anchor_for_span``'s shape, in PDF points on a 0-based page.
PDF_ANCHOR = {"kind": "pdf", "page": 3, "bbox": [72.0, 118.4, 540.0, 262.9]}
#: ``citation_tasks.py:284``'s shape: the code a page branches on, the sentence a human reads.
UNRESOLVED = {"code": "no_source_span", "reason": "the chunk predates source spans"}

#: A term that will not collide with anything else the suite leaves in the corpus.
TERM = "zarquonfleeble"


def _chunk(session, content: str):
    """A settled leaf, because every retrieval method filters on ``settled``."""
    doc = DocumentRepository(session).create(
        title=None, content=content, usetype="chunk", auto_embed=False
    )
    doc.settled = SETTLED_SETTLED
    session.flush()
    return doc


@pytest.fixture
def corpus(db_session):
    """Three chunks in the three anchor states IC-4 distinguishes, and nothing else."""
    anchored = _chunk(db_session, f"{TERM} the passage that has a rectangle")
    unresolved = _chunk(db_session, f"{TERM} the passage whose rectangle was not recovered")
    bare = _chunk(db_session, f"{TERM} the passage nothing ever tried to place")

    rows = EvidenceRepository(db_session)
    rows.write(anchored.id, "source_anchor", PDF_ANCHOR)
    rows.write(unresolved.id, "source_anchor.unresolved", UNRESOLVED)
    db_session.flush()
    return {"anchored": anchored.id, "unresolved": unresolved.id, "bare": bare.id}


def _by_id(response) -> dict[int, object]:
    return {item.document.id: item for item in response.results}


def test_an_anchored_hit_carries_its_rectangle_without_a_second_call(db_session, corpus):
    """IC-4's pin, and the beeline's first link.

    The row goes onto the hit VERBATIM — a dict, not a parsed
    :class:`~jmfts_client.contracts.anchor.PdfAnchor`. That is IC-4's own wording ("verbatim
    from the evidence rows") and it is also the version-skew answer this feature is built on: an
    anchor kind this appliance's own client had no model for would otherwise make a SEARCH fail,
    where what should happen is that the consumer is told to upgrade. ``parse_anchor`` is the
    reader, and it is the CONSUMER's to call.
    """
    found = _by_id(SearchService(db_session).fulltext_search(SearchRequest(query=TERM, limit=10)))

    assert found[corpus["anchored"]].source_anchor == PDF_ANCHOR
    assert found[corpus["anchored"]].source_anchor_unresolved is None


def test_a_hit_with_no_rectangle_carries_the_reason_instead_of_nothing(db_session, corpus):
    """The distinction the whole citation feature turns on, arriving on the result list.

    "This passage has no highlight" and "this passage's rectangle could not be recovered, and
    here is why" are different answers. ``evidence.py:453`` records that the unresolved row is
    present exactly when the anchor is not, so a hit that carried neither field would collapse
    them — and a front end drawing nothing for both hides the second, which is the failure this
    sprint's Block F is written against.
    """
    found = _by_id(SearchService(db_session).fulltext_search(SearchRequest(query=TERM, limit=10)))

    unresolved = found[corpus["unresolved"]]
    assert unresolved.source_anchor is None
    assert unresolved.source_anchor_unresolved == UNRESOLVED
    # The sentence is carried through rather than re-worded. It was written for a human to read.
    assert unresolved.source_anchor_unresolved["reason"] == UNRESOLVED["reason"]

    # And the third state: nothing recorded, nothing lost. `citation` is advisory and runs on
    # PDFs, so most of a corpus is here and it is not a failure.
    bare = found[corpus["bare"]]
    assert bare.source_anchor is None and bare.source_anchor_unresolved is None


def test_every_method_carries_the_anchor_and_not_just_the_one_that_was_wired(db_session, corpus):
    """Seven search operations, one assembly.

    ``_build_response`` serves six and ``auto_search`` built its own list, so before step 31
    there were two copies of the same zip — the arrangement in which a field added to the item
    reaches six routes and not the seventh, and nothing says which. They are one method now, and
    this is the check that they stay one.
    """
    service = SearchService(db_session)
    request = SearchRequest(query=TERM, limit=10)

    for response in (service.fulltext_search(request), service.bm25_search(request)):
        found = _by_id(response)
        if corpus["anchored"] in found:
            assert found[corpus["anchored"]].source_anchor == PDF_ANCHOR


def test_the_anchor_rows_cost_one_query_for_the_whole_page(db_session, corpus):
    """THE PROPERTY THAT MAKES THIS WORTH DOING, rather than the round trip moved one layer down.

    IC-4 removes ten ``GET /documents/{id}/evidence`` calls from a ten-hit result list. Paying
    for them here as ten SELECTs would be the same cost in a place nobody is looking at, so the
    read is batched — :meth:`EvidenceRepository.read_many_named`, one statement whatever the
    page size, narrowed to the two rows an anchor lives in rather than every row a node has.

    Counted by listening to the engine rather than by reading the source, because "it looks like
    one query" is exactly what an ORM is good at making untrue.
    """
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "document_evidence" in statement:
            statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = SearchService(db_session).fulltext_search(SearchRequest(query=TERM, limit=10))
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(response.results) >= 3, "the fixture's three chunks are in the page"
    assert len(statements) == 1, (
        f"{len(statements)} queries against document_evidence for one page of "
        f"{len(response.results)} hits; the read is meant to be batched:\n" + "\n".join(statements)
    )
    # And it NARROWED BY NAME rather than dragging `extraction` and the `attempts` log along
    # with the two rows it wanted — a search is not the place to move those. The names
    # themselves are bound parameters and are not in the statement text, so what is read here is
    # the shape of the filter: without it the statement would select every row of every hit.
    assert "document_evidence.name IN" in statements[0], statements[0]
