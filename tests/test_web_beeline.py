"""The beeline, driven end to end: search a thing, click the hit, see where the answer came from.

``docs/SPRINT_0_6_0.md`` Block F step 31, gate M4. Every other web test in this repository
exercises one module against fixtures a Python file hands it. This one exercises the SEAMS, and
it exists because nothing else in the suite could have caught the two defects that opened this
lane: an encoding written in one view and read by a byte-identical copy in another, and a search
hit that reported "no anchor" for every result because the field IC-4 put on it was never filled
in. Both are invisible from inside either module.

**So nothing here is hand-built between the steps.** The href is read off the ``<a>`` the search
view rendered, not constructed; it is split by ``shell/shell.js``'s own ``currentRoute``, not by
``urllib``; the view it lands on is looked up in the registry by the path that came out of that
split, not named. A test that assembled the URL itself would pass with the two halves of the
encoding disagreeing, which is the exact thing it is for.

FOUR BEELINES, NOT ONE, AND THEY END IN FOUR DIFFERENT PLACES. That is the shape of the
feature today and stating it honestly is worth more than a uniform story:

1. **PDF** — ``citation`` writes ``{kind: "pdf", page, bbox}``, ``/image`` serves the page,
   ``pageFigure`` draws the box. Complete. This is the demo path.
2. **``.docx`` / ``.pptx``, paginated** — ``anchor:source`` writes ``{kind: "ooxml", part,
   path}``, which is exact and undrawable; ``render:pdf`` paginates the document and
   ``anchor:render`` writes ``{kind: "pdf", of: "rendition", page, bbox}`` beside it. The hit
   carries BOTH, ``/image`` serves a page of the rendition (``pdf_for``), and the page draws
   the rectangle and names the address as the thing the rectangle is a picture of. **It ends in
   a box, and in a caption that says whose page number that is** — "page 4 of this appliance's
   rendering", never "page 4", because the reader who opens the file in Word may find the
   passage on page 5.
3. **``.docx`` / ``.pptx``, not paginated** — the same exact address and no rendition, which is
   the ordinary state of an appliance with no LibreOffice configured. It ends at "this passage
   is at ``<path>`` of ``<part>``", named precisely and not boxed — and the page has to say
   which of the FOUR absences that is: nothing has paginated the document, a rectangle was
   looked for in a rendition and not found, a rung ran and recorded neither, or nothing ever
   tried to place this node at all.
4. **``.xlsx``** — ingest now writes ``{kind: "cells", sheet, ref}`` on a sheet's ``table``,
   each ``record`` and its ``profile`` (issue #29, the write side; ``sheet_tasks._cells_anchor``,
   pinned in ``tests/test_sheet_records.py::TestTheCellsAnchor``). The fixture here still models
   a workbook hit that carries NO ``source_anchor`` — deliberately: it is the unanchored
   response (a row ingested before #29, or any hit whose evidence has none), and what the page
   does with that is what the last test asserts. It ends at the node the row stands for, and
   the page SAYS so. A fixture for the anchored workbook hit is separate work.

**The second of those is new, and what it closed was a gap between the appliance and the
browser rather than a missing capability.** Block G step 33 landed ``anchor:render`` and
``DocumentService._pdf_anchor`` has tried the ``render_anchor`` row first ever since, so
``GET /documents/{id}/region?anchor=true`` cropped a ``.docx`` to its real rectangle while the
front end asked for ``source_anchor`` and nothing else — the rectangle existed in the database
and nothing fetched it. This file's own header said beeline 2 "ends at ``<path>`` of ``<part>``,
named precisely and not boxed" and cited step 33 as future work; that was wrong about the
appliance and right about the browser on the same day.
"""

from __future__ import annotations

import base64
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from jmfts_client.contracts.document import DocumentResponse
from jmfts_client.contracts.search import AppliedFilters, SearchResponse, SearchResultItem
from jmfts_client.contracts.view import (
    BackReferenceResponse,
    ViewAncestor,
    ViewPresentation,
    ViewResponse,
)

# The one DOM stub, reached through the one extension of it that models object URLs — a page
# that draws a rendered PDF page holds bytes. `tests/test_web_views.py` owns the stub and
# `tests/test_web_renderers.py` owns the extension; this file adds neither.
from tests.test_web_document_view import PAGE_DPI, PAGE_PIXELS, _cells
from tests.test_web_renderers import RENDER_DOM, _style, png_bytes
from tests.test_web_views import NODE, STATIC, _find, needs_node

#: The anchors, in the states the four beelines end in.
PDF_ANCHOR = {"kind": "pdf", "page": 3, "bbox": [72.0, 118.4, 540.0, 262.9]}
OOXML_ANCHOR = {
    "kind": "ooxml",
    "part": "word/document.xml",
    "path": "/w:document/w:body/w:p[42]",
}
#: The other anchor of a paginated office document — `anchor:render`'s row. Same rectangle as
#: the PDF one so that the two draw identically and the only thing that differs on the page is
#: what it is CALLED, which is the half of this feature that is not geometry.
RENDER_ANCHOR = {
    "kind": "pdf",
    "of": "rendition",
    "page": 3,
    "bbox": [72.0, 118.4, 540.0, 262.9],
}

#: The four nodes the four hits point at.
PDF_DOC = 11
OOXML_DOC = 12
SHEET_DOC = 13
RENDERED_DOC = 14


def _doc(id_: int, title: str | None, usetype: str, content: str, **extra) -> DocumentResponse:
    return DocumentResponse(
        id=id_,
        parent_id=None,
        title=title,
        content=content,
        structured_content={},
        path=[],
        depth=0,
        usetype=usetype,
        created_at=None,
        updated_at=None,
        content_hash=None,
        **extra,
    )


def _search_response() -> dict:
    """One ranking, three hits, three anchor states — built by Pydantic, not by hand.

    The fixture is a real ``SearchResponse`` because the fields under test are the ones step 18
    added to ``SearchResultItem`` and step 31 filled in: a dict literal would keep passing after
    a rename in ``jmfts-client``, which is the drift the contracts exist to prevent.
    """
    return json.loads(
        SearchResponse(
            results=[
                SearchResultItem(
                    document=_doc(PDF_DOC, "Q3 board pack", "chunk", "late interaction"),
                    score=0.913,
                    method="vector",
                    source_anchor=PDF_ANCHOR,
                ),
                SearchResultItem(
                    document=_doc(OOXML_DOC, "The proposal", "chunk", "the paragraph that hit"),
                    score=0.802,
                    method="vector",
                    source_anchor=OOXML_ANCHOR,
                ),
                SearchResultItem(
                    document=_doc(
                        SHEET_DOC, None, "record", "Acme | Won | 12000", produced_by="sheets"
                    ),
                    score=0.501,
                    method="vector",
                ),
                # The office document this appliance HAS paginated: both anchors on one hit,
                # which is the round trip IC-4 removes arriving in its office spelling.
                SearchResultItem(
                    document=_doc(RENDERED_DOC, "The contract", "chunk", "the clause that hit"),
                    score=0.488,
                    method="vector",
                    source_anchor=OOXML_ANCHOR,
                    render_anchor=RENDER_ANCHOR,
                ),
            ],
            total=4,
            latency_ms=11.0,
            applied=AppliedFilters(limit=10, exclude_types=[]),
        ).model_dump_json()
    )


def _view(id_: int, title: str, usetype: str, content: str) -> dict:
    return json.loads(
        ViewResponse(
            id=id_,
            title=title,
            usetype=usetype,
            renderer="plain",
            rendered_content=content,
            can_write=True,
            presentation=ViewPresentation(
                renderer="plain", child_handling="collapsed", link_handling="footnotes"
            ),
            ancestors=[ViewAncestor(id=1, title="The source", usetype="file")],
            children_stubs=[],
            outbound_links=[],
            inbound_links=[],
            triples=[],
        ).model_dump_json()
    )


def _node_doc(id_: int, usetype: str, content: str, produced_by: str) -> dict:
    return json.loads(
        DocumentResponse(
            id=id_,
            parent_id=1,
            title=None,
            content=content,
            structured_content={},
            path=[1],
            depth=1,
            usetype=usetype,
            created_at=None,
            updated_at=None,
            content_hash=None,
            produced_by=produced_by,
        ).model_dump_json()
    )


def _back(id_: int) -> dict:
    return json.loads(
        BackReferenceResponse(document_id=id_, total=0, references=[]).model_dump_json()
    )


def _fixture() -> str:
    return json.dumps(
        {
            "search": _search_response(),
            "views": {
                str(PDF_DOC): _view(PDF_DOC, "Q3 board pack", "chunk", "late interaction"),
                str(OOXML_DOC): _view(OOXML_DOC, "The proposal", "chunk", "the paragraph"),
                str(SHEET_DOC): _view(SHEET_DOC, "row 4", "record", "Acme | Won | 12000"),
                str(RENDERED_DOC): _view(RENDERED_DOC, "The contract", "chunk", "the clause"),
            },
            "nodes": {
                str(PDF_DOC): _node_doc(PDF_DOC, "chunk", "late interaction", "chunk:text"),
                str(OOXML_DOC): _node_doc(OOXML_DOC, "chunk", "the paragraph", "chunk:text"),
                str(SHEET_DOC): _node_doc(SHEET_DOC, "record", "Acme | Won", "extract:sheet"),
                str(RENDERED_DOC): _node_doc(RENDERED_DOC, "chunk", "the clause", "chunk:text"),
            },
            "backs": {
                str(PDF_DOC): _back(PDF_DOC),
                str(OOXML_DOC): _back(OOXML_DOC),
                str(SHEET_DOC): _back(SHEET_DOC),
                str(RENDERED_DOC): _back(RENDERED_DOC),
            },
            "cells": _cells(),
            "dpi": PAGE_DPI,
        }
    )


# ------------------------------------------------------------------------------ the harness
#
# TWO VIEWS AND THE REAL ROUTER IN ONE PROCESS, which is the arrangement that makes this a test
# of the seams rather than a third test of either view. `shell/shell.js` is imported for
# `currentRoute` alone — it is safe to import here because its module scope only calls `start`
# when `document.getElementById("shell")` finds a mount point, and the stub answers null.

HARNESS = textwrap.dedent("""
    import { install, deep as serialize, installObjectUrls } from "./dom.mjs";
    install();
    installObjectUrls();

    // Both views, imported for their registrations the way `shell/manifest.js` imports them.
    await import("%(static)s/views/search.js");
    await import("%(static)s/views/document.js");
    const { viewById, viewForPath } = await import("%(static)s/shell/registry.js");
    const { currentRoute } = await import("%(static)s/shell/shell.js");
    const { METHODS, renderColumns } = await import("%(static)s/views/search.js");

    const FIXTURE = %(fixture)s;
    const PNG = Uint8Array.from(atob("%(png)s"), (c) => c.charCodeAt(0));

    // ---------------------------------------------------------------- step 1: the result list
    //
    // One column, drawn by the search view's own renderer off a real `SearchResponse`. The
    // badge each hit carries and the href each hit's title points at are what the next step
    // reads; nothing here builds either.
    const request = { getMethod: "hybrid", includeExcluded: false };
    const vector = METHODS.find((m) => m.id === "vector");
    const columns = renderColumns(
      [{ method: vector, response: FIXTURE.search, error: null }], null, request);

    const links = [];
    const walk = (node) => {
      if (node.tagName === "A") links.push(node.href);
      for (const child of node.childNodes) walk(child);
    };
    walk(columns);

    const out = { results: serialize(columns), hrefs: links, landings: {}, calls: {} };

    // ------------------------------------------------- step 2: the click, through the router
    //
    // `currentRoute` is the shell's, unchanged, and it is what a `hashchange` would feed it.
    // Splitting the href here with anything else would let the two halves of the encoding
    // disagree and still pass.
    const detail = viewById("document");
    const open = async (href, label) => {
      const route = currentRoute(href);
      const registration = viewForPath(route.path);
      if (registration !== detail) {
        throw new Error(`${href} routed to ${route.path}, which is not the detail view`);
      }
      const calls = [];
      const id = route.params.id;
      const client = {
        view_document: async (a) => { calls.push(["view", a]); return FIXTURE.views[id]; },
        get_document: async (a) => { calls.push(["document", a]); return FIXTURE.nodes[id]; },
        get_back_references: async (a) => { calls.push(["back", a]); return FIXTURE.backs[id]; },
        get_document_evidence: async (a) => {
          calls.push(["evidence", a]);
          // Answers "no rows at all" for the one node that reaches here — the workbook row,
          // which this fixture models as UNANCHORED on purpose (ingest writes cells anchors
          // since #29). What this stub is for is COUNTING: the anchored hits must not reach it.
          return { document_id: Number(id), evidence: {} };
        },
        get_document_image: async (a) => {
          calls.push(["image", a]);
          return new Blob([PNG], { type: "image/png" });
        },
        get_document_cells: async (a) => { calls.push(["cells", a]); return FIXTURE.cells; },
        expand_children: async (a) => { calls.push(["expand", a]); return []; },
        create_link: async (a) => { calls.push(["link", a]); return null; },
      };
      const page = await registration.component(
        { client, params: route.params, navigate: () => {} });
      out.landings[label] = serialize(page);
      out.calls[label] = calls;
      out.landings[label + ":route"] = route;
    };

    await open(links[0], "pdf");
    await open(links[1], "ooxml");
    await open(links[2], "sheet");
    await open(links[3], "rendered");
    console.log(JSON.stringify(out));
    """)


def _run(tmp_path: Path) -> dict:
    (tmp_path / "dom.mjs").write_text(RENDER_DOM, encoding="utf-8")
    script = tmp_path / "harness.mjs"
    filled = (
        HARNESS.replace("%(static)s", STATIC.as_uri())
        .replace("%(fixture)s", _fixture())
        .replace("%(png)s", base64.b64encode(png_bytes(*PAGE_PIXELS)).decode())
    )
    script.write_text(filled, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, cwd=tmp_path, timeout=60
    )
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout)


@pytest.fixture(scope="module")
def beeline(tmp_path_factory):
    if NODE is None:
        pytest.skip("no JavaScript runtime on PATH")
    return _run(tmp_path_factory.mktemp("beeline"))


# ------------------------------------------------------------------ beeline 1: the PDF, boxed


@needs_node
def test_a_pdf_hit_arrives_at_its_own_page_with_the_box_already_on_it(beeline):
    """THE BEELINE, and the whole of it in one assertion chain.

    The hit's title carries the anchor on the fragment; the shell's router splits it; the detail
    view reads it with the reader both views now share; ``/image`` is called at the anchor's own
    page and at the density the client asked for; and one box lands at 72/612 of the way across
    a US Letter page. No step of that is done by this test — every one of them is a module in
    the bundle, and what is asserted is that they agree.
    """
    route = beeline["landings"]["pdf:route"]
    assert route["path"] == "/document"
    assert route["params"]["id"] == str(PDF_DOC)
    assert json.loads(route["params"]["anchor"]) == PDF_ANCHOR, "the anchor survived the URL"

    calls = [name for name, _ in beeline["calls"]["pdf"]]
    # IC-4's whole purpose, measured at the only place it can be: no evidence call, because the
    # hit already carried the rows. Before step 31 the server left both fields null, so the hit
    # carried nothing, so this call happened on every click — the round trip the field exists to
    # remove, still being paid, with nothing on any page saying so.
    assert "evidence" not in calls, beeline["calls"]["pdf"]
    assert ["image", {"document_id": PDF_DOC, "page": 3, "dpi": PAGE_DPI}] in [
        [name, args] for name, args in beeline["calls"]["pdf"]
    ]

    boxes = _find(beeline["landings"]["pdf"], "overlay-box")
    assert len(boxes) == 1, "one passage, one box"
    # 72 of 612 points across, 118.4 of 792 down — as PERCENTAGES, so the box stays on the
    # passage when a narrow window scales the picture.
    assert _style(boxes[0]) == pytest.approx(
        {"left": 11.7647, "top": 14.9495, "width": 76.4706, "height": 18.2449}, abs=1e-4
    )
    assert f"rendered at {PAGE_DPI} dpi" in beeline["landings"]["pdf"]["text"]
    # And the page is the one the passage is on, labelled the way a reader counts.
    assert "Page 4" in beeline["landings"]["pdf"]["text"]


# ------------------------------------------------- beeline 2: the office document, named


@needs_node
def test_an_office_hit_arrives_at_an_exact_address_and_says_why_there_is_no_box(beeline):
    """WHERE AN UNPAGINATED `.docx` BEELINE ENDS, and it is not where the PDF one does.

    ``anchor:source`` recovered this address from the uploaded package, so the record is
    complete; what does not exist is a page to draw on, because an office document has none
    until ``render:pdf`` paginates it and this appliance has not — which is the ordinary state
    of an appliance with no LibreOffice beside it. The page therefore names the passage — part
    and element — and says which of the four rendition absences this is, naming the two rungs
    and the setting rather than a sprint step somebody would have to go and read.

    The property under test is the DISTINCTION: a reader must be able to tell "we know exactly
    where this is and cannot draw it yet" from "we have no idea where this is". So the
    assertions are as much about what is NOT said as what is: no box, no "could not be
    recovered", and no call to a route that would refuse this node by name.
    """
    assert json.loads(beeline["landings"]["ooxml:route"]["params"]["anchor"]) == OOXML_ANCHOR

    calls = [name for name, _ in beeline["calls"]["ooxml"]]
    assert "evidence" not in calls, "the hit brought its own rows here too"
    # NOT CALLED. `/image` refuses an office node by name and `/region` has no rectangle to crop
    # to, so a page that fired either would spend an entry in the one log step 23 built on a
    # refusal it could already predict.
    assert "image" not in calls, beeline["calls"]["ooxml"]
    assert "cells" not in calls, beeline["calls"]["ooxml"]

    text = beeline["landings"]["ooxml"]["text"]
    assert not _find(beeline["landings"]["ooxml"], "overlay-box"), "there is no rectangle to draw"
    assert "/w:document/w:body/w:p[42] of word/document.xml" in text
    assert "render:pdf" in text and "anchor:render" in text, "what would draw the box"
    assert "JMFTS_CONVERT_URL" in text, "and what this appliance is missing to get one"
    assert "anchor:source" in text, "and that nothing is missing from the record"
    # The words the OTHER absence uses, which this is not. `highlightNotice`'s wording for an
    # unresolved row is "No rectangle was recorded for this passage: <reason>"; saying that here
    # would report a complete record as a recovery failure.
    assert "No rectangle was recorded for this passage" not in text
    # And not the words of the paginated case either, which is the hit below this one in the
    # same result list: two `.docx` hits, two endings, and the page says which is which.
    assert "of this appliance's rendering" not in text


# --------------------------------------- beeline 2b: the office document, boxed on a rendering


@needs_node
def test_a_paginated_office_hit_arrives_with_a_rectangle_and_says_whose_page_it_is(beeline):
    """THE LAST LINK OF THE OFFICE BEELINE, driven through the same seams as the PDF one.

    The appliance could crop this rectangle before the browser could ask for it: Block G step 33
    landed ``anchor:render`` and ``_pdf_anchor`` tries the ``render_anchor`` row first, so
    ``GET /documents/{id}/region?anchor=true`` on a ``.docx`` has served the right crop for days
    while every page in this bundle read ``source_anchor`` and nothing else. The rectangle
    existed in the database and nothing fetched it. This is the test that it is fetched.

    **What it asserts is a rectangle AND a sentence, because either one alone is a defect.** A
    box with no label is the appliance making the document's claim on its behalf — "page 4" for
    a file whose pages exist only inside LibreOffice, and the reader who opens it in Word and
    finds the passage on page 5 has no way to know which of the two is wrong. A label with no
    box is what shipped until now.

    Nothing here is hand-built between the steps, which is this file's whole method: the href
    comes off the ``<a>`` the search view rendered, the shell's own router splits it, and the
    detail view chooses the surface. What the fragment has to carry is FOUR evidence rows rather
    than two, and the first assertion is that they survived the URL.
    """
    route = beeline["landings"]["rendered:route"]
    assert json.loads(route["params"]["anchor"]) == OOXML_ANCHOR, "what the passage IS"
    assert json.loads(route["params"]["render"]) == RENDER_ANCHOR, "where it APPEARS"

    calls = [name for name, _ in beeline["calls"]["rendered"]]
    assert "evidence" not in calls, "the hit carried all four rows, so nothing is asked twice"
    # `/image` DOES get called here, and that is the difference from the hit above: `pdf_for`
    # answers an office node with its rendition, so the route that refuses an unpaginated
    # `.docx` by name serves a page for a paginated one.
    assert ["image", {"document_id": RENDERED_DOC, "page": 3, "dpi": PAGE_DPI}] in [
        [name, args] for name, args in beeline["calls"]["rendered"]
    ]

    boxes = _find(beeline["landings"]["rendered"], "overlay-box")
    assert len(boxes) == 1, "one passage, one box — and it is the office passage's"
    assert _style(boxes[0]) == pytest.approx(
        {"left": 11.7647, "top": 14.9495, "width": 76.4706, "height": 18.2449}, abs=1e-4
    ), "the same transform that boxes a PDF page, which is IC-9's one claim"

    text = beeline["landings"]["rendered"]["text"]
    # THE LABEL. Authority runs opposite to drawability, so the page says whose pagination this
    # is before a reader acts on it.
    assert "Page 4 of this appliance's rendering" in text
    assert "Page 4, rendered at 150 dpi." not in text, "that is what a SOURCE page says"
    assert "no pages of its own" in text
    # And the durable citation, kept on the page as the thing the rectangle is a picture of.
    assert "/w:document/w:body/w:p[42] of word/document.xml" in text
    assert "Cite the address" in text


# ------------------------------------------------------- beeline 3: the workbook, and the gap


@needs_node
def test_an_unanchored_workbook_hit_ends_at_the_row(beeline):
    """The UNANCHORED workbook hit, and what the page does with it.

    Ingest writes ``cells`` anchors now (issue #29, the write side): ``extract:sheet`` and
    ``profile:sheet`` put ``{kind: "cells", sheet, ref}`` on each record, the table and the
    profile — see ``tests/test_sheet_records.py::TestTheCellsAnchor``. This test is no longer the
    record of a gap in the ingest path. Its fixture deliberately models a workbook hit whose
    response carries no ``source_anchor`` (a row ingested before #29, or any node whose evidence
    has none), and pins that such a hit ends at the node the row stands for, says so, and pays
    for exactly one evidence call. Inventing a client-side anchor from the row's position is
    still the fallback this lane was told not to add.
    """
    route = beeline["landings"]["sheet:route"]
    assert route["params"] == {"id": str(SHEET_DOC)}, (
        "a workbook hit carries neither anchor row, so neither key is written into the "
        "fragment — and the detail view therefore pays for the one evidence call"
    )

    calls = [name for name, _ in beeline["calls"]["sheet"]]
    assert "evidence" in calls, "nobody looked yet, so this page looks once"
    assert calls.count("evidence") == 1, "and exactly once"

    text = beeline["landings"]["sheet"]["text"]
    assert not _find(beeline["landings"]["sheet"], "overlay-box")
    # The sentence that makes the gap readable from the page rather than only from this file.
    assert "nothing has tried to place this node" in text
    assert (
        "cells" in text and "workbook" in text.lower()
    ), "the page names which rungs write an anchor and says a workbook reaches neither"


# ----------------------------------------------------------------- the list they all came from


@needs_node
def test_the_result_list_says_which_of_the_three_states_each_hit_is_in(beeline):
    """One ranking, three endings, and the list says which before anything is clicked.

    This is the half of step 31 that is about the SEARCH page: a hit that can be boxed, a hit
    that is exactly addressed and cannot be, and a hit with no anchor at all are three different
    answers and they get three different badges. Two of the three used to render the same: an
    ``ooxml`` anchor fell through to "this front end is older than the appliance", which reads
    as a broken client rather than as a scheduled feature.
    """
    hits = _find(beeline["results"], "search-hit")
    assert len(hits) == 4

    states = []
    for hit in hits:
        badges = [
            node["class"]
            for node in hit["children"][1]["children"]
            if node["class"].startswith("search-badge")
        ]
        states.append(badges[-1].split()[-1])
    assert states == ["anchored", "addressed", "none", "rendered"], (
        "four hits, four states — `addressed` is not `anchored` (no box is drawable) and not "
        "`unresolved` (nothing failed), and `rendered` is neither of those two: a box IS "
        "drawable and it is this appliance's rendering rather than the document's own geometry"
    )

    # And the hand-off carries exactly what each hit had: the anchor, the anchor, nothing, both.
    assert json.loads(_query(beeline["hrefs"][0])["anchor"]) == PDF_ANCHOR
    assert json.loads(_query(beeline["hrefs"][1])["anchor"]) == OOXML_ANCHOR
    assert _query(beeline["hrefs"][2]) == {"id": str(SHEET_DOC)}
    assert json.loads(_query(beeline["hrefs"][3])["anchor"]) == OOXML_ANCHOR
    assert json.loads(_query(beeline["hrefs"][3])["render"]) == RENDER_ANCHOR


def _query(href: str) -> dict[str, str]:
    from urllib.parse import parse_qs, urlparse

    return {k: v[0] for k, v in parse_qs(urlparse(href.lstrip("#")).query).items()}


# ------------------------------------------------------------------- the encoding is one file


def test_the_detail_route_encoding_is_defined_exactly_once():
    """The defect this lane opened on, pinned so it cannot come back by the same road.

    ``readDetailParams`` was defined in ``views/search.js`` AND in ``views/document.js``, byte
    for byte but for the error class, because two lanes wrote it in two worktrees and neither
    could import the other's file. Both copies predicted the fix at their own definition:
    identical copies reconcile by DELETING one, which a paraphrase would not.

    Step 31 deleted both. The writer and the reader are now one module under the shell —
    ``shell/citation.js`` — which both views import, because a view importing a view is what
    IC-7 bought independence from and a route argument is the shell's business anyway.

    A grep, not a call, because what this asserts is an absence.
    """
    citation = STATIC / "shell" / "citation.js"
    assert citation.is_file(), "the one definition of the detail route's arguments"
    body = citation.read_text(encoding="utf-8")
    for name in ("detailParams", "readDetailParams", "detailHref", "broughtACitation", "hrefWith"):
        assert f"export function {name}" in body, name

    for view in ("search.js", "document.js"):
        source = (STATIC / "views" / view).read_text(encoding="utf-8")
        assert "export function readDetailParams" not in source, (
            f"views/{view} defines readDetailParams again; the reader and the writer of this "
            "fragment are one encoding and two copies of it drift silently — a highlight that "
            "stops appearing, which is indistinguishable from a passage that has none."
        )
        assert "shell/citation.js" in source, f"views/{view} must import the one encoding"
