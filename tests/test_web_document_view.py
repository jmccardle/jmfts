"""The document detail view: ``jmfts-web/jmfts_web/static/views/document.js``.

``docs/SPRINT_0_6_0.md`` Block F step 28, the second half — and the page step 31 lands on. Four
contracts meet here and each is pinned by the property that would be worth finding out about:

* **IC-3** — a write control is offered from ``ViewResponse.can_write`` and from nothing else.
  A read-only principal gets a sentence saying so instead of a button that 403s.
* **IC-4 / step 31** — the anchor arrives as a ROUTE ARGUMENT, so a search hit that already
  carries one costs no second call. Given one, this view does not ask the appliance for
  evidence; given none, it asks exactly once.
* **IC-8** — the content goes through ``render()`` and a renderer that fails is shown inside
  the content panel rather than replacing the page.
* **IC-9** — the overlay's five statuses each reach the page as their own sentence, and one
  box is drawn by one transform whether the surface is a rendered PDF page or a grid of cells.

The grid is the other half of step 22's decision: a spreadsheet region comes back as CELLS and
is drawn as cells, with the anchor's A1 range boxed on it — not as a picture of cells.

No database and no browser. Where ``node`` is on PATH the real ES modules run against a stubbed
DOM, which is the arrangement ``tests/test_web_views.py`` established; the stub here adds an
``Image`` and an object-URL factory, because a page image is fetched as a blob and measured
before the overlay can put a box on it.
"""

from __future__ import annotations

import base64
import json
import re
import textwrap
from pathlib import Path

import pytest

from jmfts_client.contracts.document import (
    CellNoteResponse,
    CellRowResponse,
    DocumentCellsResponse,
    DocumentResponse,
    LinkResponse,
)
from jmfts_client.contracts.view import (
    BackReferenceItem,
    BackReferenceResponse,
    ViewAncestor,
    ViewChildStub,
    ViewLinkRef,
    ViewPresentation,
    ViewResponse,
    ViewTripleRef,
)

# THE ONE DOM STUB, AND THE ONE EXTENSION OF IT. `tests/test_web_views.py` owns the stub every
# browser test in this repository runs against; `tests/test_web_renderers.py` adds the two
# things a page holding BYTES needs — attribute-level serialisation and object URLs — and this
# view is the other page that holds bytes, so it imports that rather than growing a third
# answer to "what does a document have".
from tests.test_web_renderers import _node, _style, png_bytes
from tests.test_web_views import NODE, STATIC, _find

REPO = Path(__file__).resolve().parents[1]
VIEW = STATIC / "views" / "document.js"
MANIFEST = STATIC / "shell" / "manifest.js"

#: US Letter at 150 dpi — `rendering.py`'s own default density, which this view sends
#: explicitly. 612x792 points becomes 1275x1650 pixels, and that ratio is the scale every box
#: below is positioned by.
PAGE_PIXELS = (1275, 1650)
PAGE_POINTS = (612.0, 792.0)
PAGE_DPI = 150

#: ``citation_tasks.py``'s shape, in PDF points, on a 0-based page.
PDF_ANCHOR = {"kind": "pdf", "page": 3, "bbox": [72.0, 118.4, 540.0, 262.9]}
CELLS_ANCHOR = {"kind": "cells", "sheet": "Q3 Pipeline", "ref": "C5:D6"}
SPAN_ANCHOR = {"kind": "span", "char_start": 6, "char_end": 11}
#: ``anchor:source``'s shape (``citation_tasks.py:424``), the kind the contract gained at
#: ``471e8f7``. Exactly addressed, and with no rectangle by construction — ``OoxmlAnchor``:
#: "this has no rectangle, and that is not a gap". This one also RUNS ON, into the speaker
#: notes of the slide it began on, which is the office spelling of ``PdfAnchor.continues``
#: and is a list of PART names rather than of page numbers.
OOXML_ANCHOR = {
    "kind": "ooxml",
    "part": "ppt/slides/slide3.xml",
    "path": "/p:sld/p:cSld/p:spTree/p:sp[2]",
    "slide": 3,
    "through": "/p:sld/p:cSld/p:spTree/p:sp[4]",
    "continues": ["ppt/notesSlides/notesSlide3.xml"],
}


def _box(node: dict) -> dict:
    """The one overlay box in a tree, as four percentages off its inline style.

    PERCENTAGES, and that is what this reads rather than pixels. The picture is
    ``max-width: 100%`` so a narrow window scales it, and a box in absolute pixels would stay
    where the unscaled picture used to be — which is what this view drew until ``drawHighlight``
    took the arithmetic over.
    """
    boxes = _find(node, "overlay-box")
    assert len(boxes) == 1, f"expected exactly one box, found {len(boxes)}"
    return _style(boxes[0])


# ---------------------------------------------------------------------------- the fixtures


def _view(**overrides) -> dict:
    """A real ``ViewResponse``, dumped. See ``_capabilities_fixture`` in test_web_views.py."""
    fields = {
        "id": 7,
        "title": "Chapter 4",
        "usetype": "chunk",
        "renderer": "plain",
        "rendered_content": "hello world, this is the passage",
        "can_write": True,
        "presentation": ViewPresentation(
            renderer="plain", child_handling="collapsed", link_handling="footnotes"
        ),
        "ancestors": [ViewAncestor(id=1, title="The Book.pdf", usetype="file")],
        "children_stubs": [
            ViewChildStub(
                id=8,
                title="4.1",
                usetype="chunk",
                preview="the first part",
                child_count=2,
                expand_url="/view/8",
            )
        ],
        "outbound_links": [
            ViewLinkRef(
                id=3,
                source_id=7,
                target_id=9,
                direction="outbound",
                link_type="cites",
                score=1.0,
                title="Another chapter",
                target_url="/view/9",
            )
        ],
        "inbound_links": [],
        "triples": [
            ViewTripleRef(
                id=4,
                subject_id=7,
                subject_title="Chapter 4",
                predicate_name="published_on",
                object_id=None,
                object_title=None,
                object_literal="2026-01-01",
                object_datatype="xsd:date",
                fact_type="static",
                valid_from=None,
                valid_until=None,
                subject_url="/view/7",
                object_url=None,
            )
        ],
    }
    fields.update(overrides)
    return json.loads(ViewResponse(**fields).model_dump_json())


def _document(**overrides) -> dict:
    fields = {
        "id": 7,
        "parent_id": 1,
        "title": "Chapter 4",
        "content": "hello world, this is the passage",
        "structured_content": {},
        "path": [1],
        "depth": 1,
        "usetype": "chunk",
        "created_at": None,
        "updated_at": None,
        "content_hash": None,
        "produced_by": "chunk:text",
    }
    fields.update(overrides)
    return json.loads(DocumentResponse(**fields).model_dump_json())


def _cells() -> dict:
    """Four columns, four rows, one of them carrying a formula behind its value."""
    return json.loads(
        DocumentCellsResponse(
            document_id=7,
            sheet="Q3 Pipeline",
            ref="A3:D6",
            ref_source="used_range",
            columns=["A", "B", "C", "D"],
            rows=[
                CellRowResponse(row=3, values=["Account", "Stage", "Value", "Owner"]),
                CellRowResponse(row=4, values=["Acme", "Won", 12000, "kim"]),
                CellRowResponse(row=5, values=["Globex", "Lost", 4000, None]),
                CellRowResponse(row=6, values=["Initech", "Open", 9000, "sam"]),
            ],
            cells={"C4": CellNoteResponse(formula="=B4*1.2")},
            markdown="| Account |",
            row_count=4,
            cell_count=16,
        ).model_dump_json()
    )


def _back(**overrides) -> dict:
    fields = {
        "document_id": 7,
        "total": 1,
        "references": [
            BackReferenceItem(
                document_id=12,
                title="An index",
                usetype="section",
                via="link",
                relation="cites",
                snippet="…as chapter 4 shows…",
                url="/view/12",
            )
        ],
    }
    fields.update(overrides)
    return json.loads(BackReferenceResponse(**fields).model_dump_json())


def _link_created() -> dict:
    return json.loads(
        LinkResponse(
            id=44,
            source_id=7,
            target_id=9,
            link_type="related",
            score=1.0,
            metadata={},
            created_at=None,
        ).model_dump_json()
    )


# ------------------------------------------------------------------------- the declaration


def test_the_document_view_is_registered_and_the_manifest_imports_it():
    source = VIEW.read_text(encoding="utf-8")
    assert re.search(r'registerView\(\{\s*\n\s*id: "document"', source)
    assert 'path: "/document"' in source
    assert 'import "../views/document.js";' in MANIFEST.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(
        line
        for line in VIEW.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith(("//", "*", "/*"))
    )


#: Which contract each consistently-named local in the module holds.
HOLDERS = {
    "view": ViewResponse,
    "node": DocumentResponse,
    "cells": DocumentCellsResponse,
    "stub": ViewChildStub,
    "edge": ViewLinkRef,
    "triple": ViewTripleRef,
    "ancestor": ViewAncestor,
    "reference": BackReferenceItem,
    "back": BackReferenceResponse,
    "created": LinkResponse,
    "row": CellRowResponse,
    "note": CellNoteResponse,
}


def test_the_view_reads_only_fields_the_contracts_carry():
    """Every field this page reads off a response is a field that response has.

    The defect that opened step 24's lane was three field names the contract does not carry,
    each rendering as the word "none" because ``undefined`` is a perfectly good empty. This
    page has eleven response shapes rather than one, so the check is mechanical instead of a
    declared list: each variable below holds exactly one contract, and every property read
    through it is resolved against the Pydantic model itself — which is what the appliance
    serialises, and therefore stronger than the generated ``.d.ts`` beside it.
    """
    code = _code()
    for name, model in HOLDERS.items():
        # The lookbehind keeps a path out of it: `jmfts_core/office/cells.py` appears in a
        # refusal message in this module and is not a field read off a response.
        reads = sorted(set(re.findall(rf"(?<![\w./\"]){name}\.([a-z_]+)", code)))
        unknown = [field for field in reads if field not in model.model_fields]
        assert not unknown, (
            f"document.js reads {unknown} off a {model.__name__}, which carries "
            f"{sorted(model.model_fields)}."
        )
    # And the check is not vacuous: the two biggest shapes really are read here.
    assert re.search(r"\bview\.presentation\b", code)
    assert re.search(r"\bview\.rendered_content\b", code)


# ------------------------------------------------------- IC-4: the anchor rides on the route

PARAM_HARNESS = textwrap.dedent("""
    import { install } from "./dom.mjs";
    install();
    const view = await import("%(static)s/views/document.js");
    // THE ROUTE READER MOVED OUT FROM UNDER THIS VIEW AT STEP 31, and this harness reaches for
    // it where it went. It was defined here AND in `views/search.js`, byte-identical but for
    // the error class; both copies are gone and `shell/citation.js` holds the one encoding,
    // which is why the refusals below now name `CitationRouteError`. This view's own
    // `DocumentParamError` stays for what is genuinely its own — `integerParam`,
    // `evidenceFromRows`.
    const route = await import("%(static)s/shell/citation.js");

    const out = { parsed: {}, brought: {}, refusals: {}, rows: {} };
    const attempt = (label, params) => {
      out.brought[label] = route.broughtACitation(params);
      try { out.parsed[label] = route.readDetailParams(params); }
      catch (error) { out.refusals[label] = [error.name, error.message]; }
    };

    attempt("none", { id: "7" });
    attempt("anchor", { id: "7", anchor: JSON.stringify(%(anchor)s) });
    attempt("unresolved", {
      id: "7", unresolved: JSON.stringify({ code: "no_source_span", reason: "the chunk predates it" }),
    });
    attempt("not json", { id: "7", anchor: "{page: 3}" });
    attempt("no id", { anchor: "{}" });
    attempt("bad id", { id: "seven" });

    out.rows.present = view.evidenceFromRows(
      { document_id: 7, evidence: { source_anchor: %(anchor)s } });
    out.rows.ranAndFound = view.evidenceFromRows(
      { document_id: 7, evidence: { source_anchor: null } });
    out.rows.unresolved = view.evidenceFromRows({ document_id: 7, evidence: {
      "source_anchor.unresolved": { code: "no_text_layer", reason: "the page is a scan" } } });
    out.rows.nothingRan = view.evidenceFromRows({ document_id: 7, evidence: {} });
    try { view.evidenceFromRows({ document_id: 7 }); out.refusals.noEvidence = null; }
    catch (error) { out.refusals.noEvidence = [error.name, error.message]; }

    out.href = route.hrefWith({ id: "7", anchor: "{}" }, { page: 3 });
    console.log(JSON.stringify(out));
    """)


@pytest.fixture(scope="module")
def params(tmp_path_factory):
    if NODE is None:
        pytest.skip("no JavaScript runtime on PATH")
    return _node(tmp_path_factory.mktemp("params"), PARAM_HARNESS, anchor=json.dumps(PDF_ANCHOR))


def test_the_detail_route_is_the_one_the_search_view_links_to(params):
    """What step 31 hands this view, and what this view does with it.

    IC-4 puts ``source_anchor`` and ``source_anchor_unresolved`` on a search hit so the front
    end need not call ``/documents/{id}/evidence`` per result. A detail view that ignored what
    the hit already had and re-read it from the server would put that round trip back one layer
    down, so the pair is a route argument: the evidence row, as JSON, unchanged.

    ``readDetailParams`` was BYTE-IDENTICAL to ``readDetailParams`` in ``views/search.js`` but
    for the error class, because the writer of this fragment and its reader were cut from one
    commit in two worktrees and could not import each other. Identical copies reconcile by
    deleting one; a paraphrase reconciles by somebody reading both. Step 31 deleted BOTH and put
    the encoding in ``shell/citation.js``, under the shell because a route argument is routing —
    so what this asserts now is a seam rather than a coincidence.
    """
    assert params["parsed"]["none"] == {"documentId": 7, "anchor": None, "unresolved": None}
    assert params["parsed"]["anchor"]["anchor"] == PDF_ANCHOR
    assert params["parsed"]["unresolved"]["unresolved"]["code"] == "no_source_span"

    # Both keys come back even when null, because `highlight()` refuses to be called without
    # both: passing only `anchor` has not said whether the caller looked for the other row.
    assert set(params["parsed"]["none"]) == {"documentId", "anchor", "unresolved"}

    # Which of those nulls means "the hit had none" and which means "nobody looked" is the
    # presence of the KEY, and that is the one thing that decides whether to spend a call.
    assert params["brought"] == {
        "none": False,
        "anchor": True,
        "unresolved": True,
        "not json": True,
        "no id": True,
        "bad id": False,
    }

    # And the route survives a hop: a link to another page of the same document keeps it.
    assert params["href"] == "#/document?id=7&anchor=%7B%7D&page=3"


def test_an_anchor_that_cannot_be_read_refuses_rather_than_disappearing(params):
    """The failure this refusal is written against is a page with no box on it.

    A dropped anchor renders identically to a passage that has no rectangle — which is the
    exact confusion ``source_anchor.unresolved`` exists to prevent, arriving by a different
    road. A truncated fragment and a hit with no citation are different facts, so torn JSON
    raises rather than softening to null.
    """
    name, message = params["refusals"]["not json"]
    assert name == "CitationRouteError"
    assert "shell/citation.js" in message, "the message names who writes the fragment"

    for label in ("no id", "bad id"):
        name, message = params["refusals"][label]
        assert name == "CitationRouteError"
        assert 'integer "id"' in message

    name, message = params["refusals"]["noEvidence"]
    assert name == "DocumentParamError"
    assert "evidence" in message


def test_a_row_that_is_absent_and_a_row_that_is_null_are_different_answers(params):
    """3.2's rule, read off the evidence response.

    "An atom ran and produced nothing" and "nothing has run" are both "no box", and only one of
    them is worth waiting for. ``DocumentEvidenceResponse`` says a name present with a null
    value IS PRESENT, so the reader is told which it met.
    """
    assert params["rows"]["present"]["anchor"] == PDF_ANCHOR
    assert params["rows"]["ranAndFound"] == {"anchor": None, "unresolved": None, "attempted": True}
    assert params["rows"]["nothingRan"] == {
        "anchor": None,
        "unresolved": None,
        "attempted": False,
    }
    assert params["rows"]["unresolved"]["unresolved"]["code"] == "no_text_layer"


# ------------------------------------------------------------------- IC-9: the two surfaces

PANEL_HARNESS = textwrap.dedent("""
    import { install, deep as serialize, installObjectUrls } from "./dom.mjs";
    install();
    installObjectUrls();
    const view = await import("%(static)s/views/document.js");
    const { CELL } = await import("%(static)s/render/sheet.js");

    const ANCHORS = %(anchors)s;
    const PNG = Uint8Array.from(atob("%(png)s"), (c) => c.charCodeAt(0));
    const calls = [];
    const client = {
      get_document_image: async (args) => {
        calls.push(["image", args]);
        return new Blob([PNG], { type: "image/png" });
      },
      get_document_cells: async (args) => { calls.push(["cells", args]); return %(cells)s; },
    };

    const out = { calls: {}, panels: {}, results: {}, planned: {}, cell: CELL };

    const page3 = await view.pagePanel({
      client, documentId: 7, page: 3, dpi: %(dpi)s, label: "The report",
      evidence: { anchor: ANCHORS.pdf, unresolved: null }, params: { id: "7" },
    });
    out.calls.image = [...calls];
    out.panels.page = serialize(page3.element);
    out.results.page = page3.result;
    out.surface = { ...page3.surface, cellRect: undefined };

    const page9 = await view.pagePanel({
      client, documentId: 7, page: 9, dpi: %(dpi)s, label: "The report",
      evidence: { anchor: ANCHORS.pdf, unresolved: null }, params: { id: "7" },
    });
    out.panels.elsewhere = serialize(page9.element);
    out.results.elsewhere = page9.result;

    const continued = await view.pagePanel({
      client, documentId: 7, page: 4, dpi: %(dpi)s, label: "The report",
      evidence: { anchor: { ...ANCHORS.pdf, continues: [4] }, unresolved: null },
      params: { id: "7", anchor: "{}" },
    });
    out.panels.continued = serialize(continued.element);
    out.results.continued = continued.result;

    calls.length = 0;
    const grid = await view.cellsPanel({
      client, documentId: 7, ref: null,
      evidence: { anchor: ANCHORS.cells, unresolved: null },
    });
    out.calls.cells = [...calls];
    out.panels.cells = serialize(grid.element);
    out.results.cells = grid.result;
    out.cellsSurface = { ...grid.surface, cellRect: undefined };

    // A span anchor on a page surface: the overlay refuses and the refusal is the answer.
    try {
      await view.pagePanel({
        client, documentId: 7, page: 0, dpi: %(dpi)s, label: "The report",
        evidence: { anchor: ANCHORS.span, unresolved: null }, params: { id: "7" },
      });
      out.results.span = null;
    } catch (error) { out.results.span = [error.name, error.message]; }

    out.planned.pdf = view.plannedSurface({ node: { usetype: "chunk" }, anchor: ANCHORS.pdf });
    out.planned.cells = view.plannedSurface({ node: { usetype: "chunk" }, anchor: ANCHORS.cells });
    out.planned.span = view.plannedSurface({ node: { usetype: "chunk" }, anchor: ANCHORS.span });
    out.planned.sheet = view.plannedSurface({ node: { usetype: "sheet" }, anchor: null });
    out.planned.text = view.plannedSurface({ node: { usetype: "chunk" }, anchor: null });
    out.planned.pdfFile = view.plannedSurface({
      node: { usetype: "file" }, anchor: null, mime: "application/pdf" });
    out.planned.otherFile = view.plannedSurface({
      node: { usetype: "file" }, anchor: null, mime: "application/vnd.oasis.opendocument.text" });
    out.planned.unknownFile = view.plannedSurface({ node: { usetype: "file" }, anchor: null });
    out.planned.ooxml = view.plannedSurface({ node: { usetype: "chunk" }, anchor: ANCHORS.ooxml });

    out.address = serialize(view.ooxmlWhere(ANCHORS.ooxml));
    out.addressPlain = serialize(view.ooxmlWhere(
      { kind: "ooxml", part: "word/document.xml", path: "/w:document/w:body/w:p[42]" }));

    const summary = (evidence) => serialize(view.anchorSummary(evidence));
    out.summaries = {
      unresolved: summary({
        anchor: null, unresolved: { code: "no_text_layer", reason: "the page is a scan" } }),
      nothingRan: summary({ anchor: null, unresolved: null, attempted: false }),
      ranAndFound: summary({ anchor: null, unresolved: null, attempted: true }),
      unknownKind: summary({ anchor: { kind: "waveform" }, unresolved: null }),
      ooxml: summary({ anchor: ANCHORS.ooxml, unresolved: null }),
    };
    console.log(JSON.stringify(out));
    """)


@pytest.fixture(scope="module")
def panels(tmp_path_factory):
    if NODE is None:
        pytest.skip("no JavaScript runtime on PATH")
    return _node(
        tmp_path_factory.mktemp("panels"),
        PANEL_HARNESS,
        anchors=json.dumps(
            {
                "pdf": PDF_ANCHOR,
                "cells": CELLS_ANCHOR,
                "span": SPAN_ANCHOR,
                "ooxml": OOXML_ANCHOR,
            }
        ),
        png=base64.b64encode(png_bytes(*PAGE_PIXELS)).decode(),
        dpi=str(PAGE_DPI),
        cells=json.dumps(_cells()),
    )


def test_a_rendered_page_declares_the_scale_it_was_rendered_at(panels):
    """The surface is derived from the bytes, and this view no longer does the deriving.

    ``rasterSurface`` takes the pixel extent ``pngSize`` read out of the PNG's own IHDR and the
    density THIS VIEW ASKED FOR, and the page's size in points is their ratio. Sending ``dpi``
    rather than letting the server default it is what makes that ratio knowable: a client that
    omitted it would keep drawing boxes after the default moved, each one off by the ratio, and
    nothing on the page would say so.

    The extent comes from the header rather than from ``naturalWidth`` after a load event,
    which is the half this lane got wrong and deleted: it is available the moment the bytes
    arrive, and it is the same number in a browser and in a test with no layout at all.
    """
    assert panels["calls"]["image"] == [["image", {"document_id": 7, "page": 3, "dpi": PAGE_DPI}]]
    assert panels["surface"]["pixels"] == {"width": PAGE_PIXELS[0], "height": PAGE_PIXELS[1]}
    assert panels["surface"]["source"] == pytest.approx(
        {"width": PAGE_POINTS[0], "height": PAGE_POINTS[1]}
    )
    # Declared by the factory, never passed: a page rendered by pymupdf is top-left with y
    # downward, and a caller that could pass a corner is a coin flip on every box.
    assert panels["surface"]["originCorner"] == "top-left"
    assert panels["surface"]["page"] == 3


def test_a_pdf_anchor_is_boxed_on_the_page_it_names(panels):
    """The beeline's last inch: the rectangle the answer came from, on the page it is on.

    The box is positioned as a FRACTION of the surface — 72 of 612 points is 11.7647% — so it
    survives the scaling a narrow window does to a ``max-width: 100%`` picture. That is
    ``drawHighlight``'s arithmetic and not this view's; what this view did with the same
    rectangle was absolute pixels, which is a box that slides off the picture on a phone.
    """
    assert panels["results"]["page"]["status"] == "box"
    assert panels["results"]["page"]["continues"] is None
    assert _box(panels["panels"]["page"]) == pytest.approx(
        {"left": 11.7647, "top": 14.9495, "width": 76.4706, "height": 18.2449}, abs=1e-4
    )
    frame = _find(panels["panels"]["page"], "overlay-surface")[0]
    assert [child["tag"] for child in frame["children"]] == ["img", "div"]
    assert (
        "Page 4, rendered at 150 dpi." in panels["panels"]["page"]["text"]
    ), "pages are labelled 1-based for a reader and 0-based on the wire"


def test_a_box_is_never_drawn_on_a_page_the_passage_is_not_on(panels):
    """Confidently wrong is worse than absent, and indistinguishable from right.

    Page 9 of a passage anchored on page 3 gets no rectangle, the sentence naming the page that
    has one, and — this view's half — a LINK to it. The sentence is ``highlightNotice``'s, in
    ``render/surfaces.js``, because there is one wording for each outcome; the link is here,
    because ``render/`` knows nothing about this front end's routing and should not.

    The same holds for a page the passage merely runs onto: ``continues`` covers only the page
    the passage BEGINS on, so page 4 of a 3-to-4 passage is "continued" and the link points
    back at page 4 — one-based — of the page the rectangle is on.
    """
    assert panels["results"]["elsewhere"] == {"status": "elsewhere", "page": 3}
    assert not _find(panels["panels"]["elsewhere"], "overlay-box")
    assert "This passage is on page 4." in panels["panels"]["elsewhere"]["text"]
    jump = _find(panels["panels"]["elsewhere"], "doc-jump")[0]
    assert jump["text"] == "Show page 4"
    assert jump["attrs"]["href"] == "#/document?id=7&page=3"

    assert panels["results"]["continued"]["status"] == "continued"
    continued = _find(panels["panels"]["continued"], "doc-jump")[0]
    assert continued["text"] == "Show page 4"
    # The rest of the route rides along, so a hit's anchor is not lost by following the link.
    assert continued["attrs"]["href"] == "#/document?id=7&anchor=%7B%7D&page=3"


def test_a_span_anchor_says_where_it_can_be_drawn_instead_of_failing_silently(panels):
    """A span IS a valid anchor; what it has no geometry for is a page.

    The overlay refuses and its message points at the text renderer. This view lets that reach
    the page rather than rewriting it, because the sentence already says what to do next — and
    the panel a span-anchored node gets by default is the passage itself, not a page at all.
    """
    name, message = panels["results"]["span"]
    assert name == "OverlayError"
    assert "anchor.py" in message


def test_the_box_on_a_grid_lands_on_the_cell_the_ref_names(panels):
    """THE GUTTER OFFSET, pinned against the markup rather than against the arithmetic.

    This is the one thing that had to be tested before this view's own ``cellsSurface`` could be
    deleted. Two internally consistent conventions can disagree by one cell: the grid draws a
    row-number gutter and a column-letter header, and whether those count as the first unit of
    the coordinate space is a decision both the surface and ``cellRect`` have to take the same
    way. ``render/sheet.js`` takes it in two places — ``columns + 1``/``shown + 1`` in
    ``surface()``, and ``+1``/``+2`` in ``rectFor`` — and this asserts they agree with the
    TABLE, which is the thing a reader actually looks at.

    The check is deliberately not "the percentages are these numbers", which would be a second
    copy of the same arithmetic. It converts the box back into cell indices and then reads the
    cell at those indices out of the rendered grid: the box must sit on the cells holding
    ``Won`` and ``kim``, which is what ``C5:D6`` names in this fixture, and not on their
    neighbours.
    """
    surface = panels["cellsSurface"]
    cell = panels["cell"]
    box = _box(panels["panels"]["cells"])

    # Percentages back to cell indices, using the surface's own extent.
    columns = surface["source"]["width"]
    rows = surface["source"]["height"]
    left = round(box["left"] / 100 * columns)
    top = round(box["top"] / 100 * rows)
    width = round(box["width"] / 100 * columns)
    height = round(box["height"] / 100 * rows)
    assert (width, height) == (2, 2), "C5:D6 is two cells by two cells"

    # The grid as drawn: row 0 is the column letters, column 0 is the row-number gutter.
    grid = _find(panels["panels"]["cells"], "render-sheet-grid")[0]
    corner = grid["children"][top]["children"][left]
    far = grid["children"][top + height - 1]["children"][left + width - 1]
    # C5 is the third column of the row numbered 5 — `4000` — and D6 is `sam`. Written out
    # like this because the first draft of this test asserted the cells one row and one column
    # up, which is exactly the off-by-one-cell it was written to catch: reading the answer off
    # the fixture by hand is the check, and the grid disagreed with the hand.
    assert corner["text"] == "4000", f"the box starts on {corner['text']!r}, not on C5"
    assert far["text"] == "sam", f"the box ends on {far['text']!r}, not on D6"

    # And the pitch the surface measures in is the pitch the markup declares, which is what
    # lets a box be placed without measuring a layout.
    assert surface["pixels"] == {"width": columns * cell["width"], "height": rows * cell["height"]}
    assert panels["results"]["cells"]["status"] == "box"


def test_a_spreadsheet_region_is_asked_for_once_and_as_cells(panels):
    """Step 22's decision, and the call that carries it.

    ``ref`` is omitted rather than sent as null, which asks ``/cells`` for the node's own region
    — its ``cells`` anchor if it has one, the measured used range otherwise, with ``ref_source``
    saying which. One call, and the grid is drawn from what it answered.
    """
    assert panels["calls"]["cells"] == [["cells", {"document_id": 7}]]
    assert "Q3 Pipeline" in panels["panels"]["cells"]["text"]
    assert "measured this sheet to hold" in panels["panels"]["cells"]["text"]


def test_the_three_ways_a_passage_can_have_no_rectangle_read_differently(panels):
    """The distinction this view keeps that ``anchorPair`` deliberately drops.

    For DRAWING, an absent ``source_anchor`` row and one written with a null value are the same
    fact and ``anchorPair`` collapses them. For SAYING they are not: "nothing has tried to place
    this node" is a reason to come back later and "an atom ran and placed nothing" is a finished
    answer. ``source_anchor.unresolved`` is the third, and its sentence is
    ``highlightNotice``'s rather than a second wording written here.
    """
    assert "the page is a scan" in panels["summaries"]["unresolved"]["text"]
    assert "no_text_layer" in panels["summaries"]["unresolved"]["text"]
    assert "nothing has tried to place this node" in panels["summaries"]["nothingRan"]["text"]
    assert (
        panels["summaries"]["ranAndFound"]["text"] == "This node carries no anchor into its source."
    )
    assert "waveform" in panels["summaries"]["unknownKind"]["text"]
    assert "older than the appliance" in panels["summaries"]["unknownKind"]["text"]


def test_an_office_passage_is_named_exactly_rather_than_boxed_or_called_missing(panels):
    """WHERE THE `.docx` / `.pptx` BEELINE ENDS, and it does not end at "no highlight".

    An ``ooxml`` anchor is the SOURCE anchor of ``OFFICE_SPEC.md`` Part 5: exact, permanent,
    computed from the uploaded package by ``anchor:source``. It has no rectangle and that is not
    a gap — an office document has no pages until ``render:pdf`` paginates it, and recovering
    geometry from that rendition is Block G step 33.

    So this panel has to separate two absences that a viewer is strongly tempted to draw
    identically: **"we know exactly where this is and cannot draw it yet"** against **"we have no
    idea where this is"**. The second is ``source_anchor.unresolved``. Both were the same blank
    space before step 31, and worse than blank on the search page, where the kind fell through to
    "this front end is older than the appliance" — said about a kind it can print element by
    element.

    The address is a caption, and the caption came out of ``describeAnchor`` — which had an
    unguarded trailing ``else`` and captioned this anchor "characters undefined to undefined"
    until step 31. A confidently wrong caption under an exactly-addressed passage is the same
    defect as a confidently wrong box, drawn in prose.
    """
    text = panels["address"]["text"]
    assert "/p:sld/p:cSld/p:spTree/p:sp[2] of ppt/slides/slide3.xml" in text
    assert "undefined" not in text, "describeAnchor used to fall through to the span branch"

    # How far it runs. `through` bounds the elements inside the SAME part and `continues` names
    # the further PARTS — a passage running from a slide into its speaker notes. A viewer that
    # printed only the first presents a partial address as a whole one, which is the office
    # spelling of `PdfAnchor.continues`' own argument.
    assert "slide 3" in text
    assert "through /p:sld/p:cSld/p:spTree/p:sp[4]" in text
    assert "ppt/notesSlides/notesSlide3.xml" in text

    # And the sentence that says which absence this is. It names the rung that RECOVERED the
    # address (so nothing is missing) and the step that would draw the box (so the reader knows
    # what they are waiting for), and it points at the other absence by name.
    assert "anchor:source" in text
    assert "step 33" in text
    assert "source_anchor.unresolved" in text

    # An anchor that does not run on says nothing about running on, rather than saying it empty.
    plain = panels["addressPlain"]["text"]
    assert "/w:document/w:body/w:p[42] of word/document.xml" in plain
    assert "It runs" not in plain

    # Reached through `anchorSummary` as well, which is the path a reader takes who asked for
    # `?page=` on an office node and therefore got no office panel. Same sentence, not a second
    # wording, and NOT the "no geometry for this kind" line an unknown kind gets.
    assert "ppt/slides/slide3.xml" in panels["summaries"]["ooxml"]["text"]
    assert "older than the appliance" not in panels["summaries"]["ooxml"]["text"]


def test_the_surface_is_chosen_before_anything_is_fetched(panels):
    """A 409 per text node is not a diagnosis, it is noise in the one log that is the evidence.

    Step 23's second property is that a view showing something the log has no call for computed
    it in the browser. A page that probed ``/image`` on every node to find out whether there was
    a picture would fill that log with expected refusals, so the surface is decided from what
    the page already knows, and where it knows nothing the panel is OFFERED.

    ``mime`` is the one that earns its place: a ``file`` node whose bytes are a PDF certainly
    has a picture — ``/image`` renders page 0 of it by that node's own definition — so it opens
    showing page 1 instead of a button. A ``file`` node that is a ``.odt`` does not, and one
    whose type was never read is not guessed at.
    """
    assert panels["planned"]["pdf"] == {"kind": "page"}
    assert panels["planned"]["cells"] == {"kind": "cells"}
    assert panels["planned"]["span"] == {"kind": "span"}
    assert panels["planned"]["sheet"] == {"kind": "cells"}
    assert panels["planned"]["text"] == {"kind": "offer"}
    assert panels["planned"]["pdfFile"] == {"kind": "page"}
    assert panels["planned"]["otherFile"] == {"kind": "offer"}
    assert panels["planned"]["unknownFile"] == {"kind": "offer"}
    # And an office node is a surface of its own rather than the offer, because the offer's
    # button calls `/image`, which refuses a `.docx` by name. A precise address shown beats a
    # refusal a reader has to press a button to be told.
    assert panels["planned"]["ooxml"] == {"kind": "ooxml"}


# ------------------------------------------------------------------------------- the page

PAGE_HARNESS = textwrap.dedent("""
    import { install, deep as serialize, installObjectUrls, document as dom } from "./dom.mjs";
    install();
    installObjectUrls();
    const module = await import("%(static)s/views/document.js");
    const { views } = await import("%(static)s/shell/registry.js");

    const fixtures = %(fixtures)s;
    const PNG = Uint8Array.from(atob("%(png)s"), (c) => c.charCodeAt(0));
    const registration = views().find((v) => v.id === "document");

    const record = () => {
      const calls = [];
      return {
        calls,
        view_document: async (a) => { calls.push(["view", a]); return fixtures.view; },
        get_document: async (a) => { calls.push(["document", a]); return fixtures.node; },
        get_back_references: async (a) => { calls.push(["back", a]); return fixtures.back; },
        get_document_evidence: async (a) => {
          calls.push(["evidence", a]);
          return { document_id: 7, evidence: { source_anchor: null } };
        },
        get_document_image: async (a) => {
          calls.push(["image", a]);
          return new Blob([PNG], { type: "image/png" });
        },
        get_document_cells: async (a) => { calls.push(["cells", a]); return fixtures.cells; },
        expand_children: async (a) => { calls.push(["expand", a]); return fixtures.more; },
        create_link: async (a) => { calls.push(["link", a]); return fixtures.created; },
      };
    };

    const out = { registration: {
      id: registration.id, title: registration.title, path: registration.path,
      tags: [...registration.tags],
    }, calls: {}, refusals: {} };

    const plain = record();
    out.page = serialize(await registration.component(
      { client: plain, params: { id: "7" }, navigate: () => {} }));
    out.calls.plain = plain.calls;

    const withAnchor = record();
    out.anchored = serialize(await registration.component({
      client: withAnchor,
      params: { id: "7", anchor: JSON.stringify(%(anchor)s) },
      navigate: () => {},
    }));
    out.calls.anchored = withAnchor.calls;

    const landing = record();
    out.landing = serialize(await registration.component(
      { client: landing, params: {}, navigate: () => {} }));
    out.calls.landing = landing.calls;

    try {
      await registration.component({ client: record(), params: { id: "seven" }, navigate: () => {} });
      out.refusals.badId = null;
    } catch (error) { out.refusals.badId = [error.name, error.message]; }

    // The renderer the database named is one this front end does not have.
    const stale = record();
    stale.view_document = async () => fixtures.staleRenderer;
    out.stale = serialize(await registration.component(
      { client: stale, params: { id: "7" }, navigate: () => {} }));

    // A read-only principal, and a presentation that hides children.
    const readOnly = record();
    readOnly.view_document = async () => fixtures.readOnly;
    out.readOnly = serialize(await registration.component(
      { client: readOnly, params: { id: "7" }, navigate: () => {} }));

    const hidden = record();
    hidden.view_document = async () => fixtures.hiddenChildren;
    out.hidden = serialize(await registration.component(
      { client: hidden, params: { id: "7" }, navigate: () => {} }));

    // The create-link control, driven directly: a click event is not a thing this can produce.
    const writing = record();
    const control = module.createLinkControl({ view: fixtures.view, client: writing });
    control.fields.target.value = "nine";
    out.invalid = await control.submit();
    control.fields.target.value = "9";
    control.fields.type.value = "related";
    out.created = await control.submit();
    out.calls.writing = writing.calls;
    out.control = serialize(control.element);

    const refused = record();
    refused.create_link = async () => { throw new Error("HTTP 409: Document 9 not found"); };
    const refusedControl = module.createLinkControl({ view: fixtures.view, client: refused });
    refusedControl.fields.target.value = "9";
    out.refusedLink = await refusedControl.submit();
    out.refusedControl = serialize(refusedControl.element);

    // More children, through the button's own function.
    const paging = record();
    const stubs = module.childStubs(fixtures.view, { client: paging });
    await stubs.loadMore();
    out.children = serialize(stubs.element);
    out.calls.paging = paging.calls;

    // A SHEET NODE, which is the case the double-`/cells` defect was found on.
    const sheet = record();
    sheet.view_document = async (a) => { sheet.calls.push(["view", a]); return fixtures.sheetView; };
    sheet.get_document = async (a) => { sheet.calls.push(["document", a]); return fixtures.sheetNode; };
    out.sheet = serialize(await registration.component(
      { client: sheet, params: { id: "9" }, navigate: () => {} }));
    out.calls.sheet = sheet.calls;

    // Seven mounts above, and one stylesheet — appended to <head> by the view's own module.
    out.styles = dom.head.childNodes.map((n) => [n.rel, n.href]);
    console.log(JSON.stringify(out));
    """)


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    if NODE is None:
        pytest.skip("no JavaScript runtime on PATH")
    fixtures = {
        "view": _view(),
        "node": _document(),
        "back": _back(),
        "created": _link_created(),
        "more": [],
        "staleRenderer": _view(
            presentation=ViewPresentation(
                renderer="mermaid", child_handling="collapsed", link_handling="footnotes"
            )
        ),
        "readOnly": _view(can_write=False),
        "hiddenChildren": _view(
            children_stubs=[],
            presentation=ViewPresentation(
                renderer="plain", child_handling="hidden", link_handling="footnotes"
            ),
        ),
        "cells": _cells(),
        "sheetView": _view(id=9, title="Q3 Pipeline", usetype="sheet", children_stubs=[]),
        "sheetNode": _document(id=9, usetype="sheet", produced_by="extract:sheet"),
    }
    return _node(
        tmp_path_factory.mktemp("page"),
        PAGE_HARNESS,
        fixtures=json.dumps(fixtures),
        png=base64.b64encode(png_bytes(*PAGE_PIXELS)).decode(),
        anchor=json.dumps(PDF_ANCHOR),
    )


def test_the_registration_is_what_the_shell_puts_in_its_navigation(page):
    assert page["registration"] == {
        "id": "document",
        "title": "Document",
        "path": "/document",
        "tags": ["corpus", "documents"],
    }


def test_the_page_is_four_reads_and_every_one_of_them_shows(page):
    """What the view composes, asserted as the calls it makes and the sections they fill."""
    assert page["calls"]["plain"] == [
        ["view", {"document_id": 7}],
        ["document", {"document_id": 7}],
        ["back", {"document_id": 7, "limit": 20}],
        ["evidence", {"document_id": 7}],
    ]
    text = page["page"]["text"]
    for heading in ("Source", "Content — plain", "Children", "Links", "Triples", "Back-references"):
        assert heading in text, heading

    # The node's own state, which /view/ does not carry and /documents/{id} does.
    assert "produced by chunk:text" in text
    # An edge and a triple, each a link to the node on the other end of it — except a literal
    # object, which has no node to navigate to and is shown as the literal it is.
    assert "#/document?id=9" in json.dumps(page["page"])
    assert "2026-01-01" in text and "xsd:date" in text
    assert "…as chapter 4 shows…" in text


def test_a_hit_that_brought_its_anchor_is_not_asked_to_bring_it_twice(page):
    """IC-4's whole point, on the page that spends it.

    With an anchor on the route there is no ``evidence`` call: the hit already paid for it. The
    surface it names is then fetched — ``/image`` at the anchor's own page — which is the one
    call that could not have been made earlier.
    """
    calls = [name for name, _ in page["calls"]["anchored"]]
    assert "evidence" not in calls
    assert ["image", {"document_id": 7, "page": 3, "dpi": 150}] in [
        [name, args] for name, args in page["calls"]["anchored"]
    ]
    # 72 of 612 points, as a fraction of the surface rather than in pixels.
    assert _box(page["anchored"])["left"] == pytest.approx(11.7647, abs=1e-4)


def test_a_node_with_no_anchor_is_offered_a_picture_rather_than_refused_one(page):
    """The evidence said an atom ran and placed nothing, so the page says that and then asks.

    The appliance is the only thing that knows whether there are bytes to render, and its
    refusals are written to be read. What this must not do is fire that call unasked on every
    text node in the corpus.
    """
    assert "no anchor into its source" in page["page"]["text"]
    assert "Render page 1 of the source anyway" in page["page"]["text"]
    assert not _find(page["page"], "overlay-box")


def test_a_renderer_this_front_end_does_not_have_stays_inside_the_content_panel(page):
    """IC-8's failure mode, contained.

    ``render()`` throws ``UnknownRendererError`` for a value the database named and this bundle
    has no renderer for — which means the front end is older than the appliance. Letting that
    reach the shell would replace the whole page with one sentence and take the links, the
    children and the source panel with it; the reader needs that sentence WITH the rest of the
    node still on screen.
    """
    assert "older than" in page["stale"]["text"]
    assert _find(page["stale"], "bad")
    assert "Back-references" in page["stale"]["text"], "the rest of the page survived"


def test_the_create_link_control_is_offered_only_when_can_write_says_so(page):
    """IC-3, which exists for this control.

    ``can_write`` is ``jmfts_core.access.can_write`` — the same function Block A step 1's gate
    calls — so a control offered here and a call refused there cannot disagree. A read-only
    principal is told why there is no form rather than being handed one that 403s.
    """
    assert "no create-link control is offered" in page["readOnly"]["text"]
    assert not _find(page["readOnly"], "doc-form")
    assert _find(page["page"], "doc-form"), "a writable node gets the form"


def test_the_form_refuses_what_cannot_be_a_request_and_shows_what_the_server_refuses(page):
    """The two halves of Fail Early in a form, kept apart.

    An input that cannot become a request does not become one — that is the form's own
    validation and it makes no call. Everything the SERVER decides is the server's, and its
    message goes on the page as it arrived.
    """
    assert page["invalid"] is None
    assert page["created"]["id"] == 44
    assert page["calls"]["writing"] == [
        [
            "link",
            {
                "document_id": 7,
                "request": {
                    "source_id": 7,
                    "target_id": 9,
                    "link_type": "related",
                    "score": 1.0,
                },
            },
        ]
    ], "the invalid submit must not have reached the appliance"
    assert "created link #44" in page["control"]["text"]

    assert page["refusedLink"] is None
    assert "Document 9 not found" in page["refusedControl"]["text"]


def test_a_presentation_that_hides_children_says_so_instead_of_showing_none(page):
    """An empty list that is a display rule, labelled as one.

    ``build_children_stubs`` returns ``[]`` for ``child_handling: "hidden"``, so a page that
    printed "no children" would be reporting a presentation row as a fact about the tree. It
    says which it is and points at the browser, which reads ``/documents/{id}/children`` and is
    filtered by no presentation row at all.
    """
    text = page["hidden"]["text"]
    assert 'child_handling: "hidden"' in text
    assert "#/documents?root=7" in json.dumps(page["hidden"])
    assert "no children" not in text


def test_more_children_comes_from_the_offset_the_view_surface_has(page):
    """``/view/{id}/expand-children`` takes an offset, so this one really is a next page."""
    assert page["calls"]["paging"] == [["expand", {"document_id": 7, "offset": 1, "limit": 20}]]
    assert "which is all of them" in page["children"]["text"]


def test_a_sheet_node_asks_for_its_cells_once(page):
    """The defect this lane found from the other side, checked the way it was found.

    For one day the tuple carried a ``sheet-region`` RENDERER and migration 023 pointed usetype
    ``sheet`` at it, so a sheet node drew a grid in the content panel and a second grid in this
    view's source panel — two identical tables and two ``/cells`` calls for one node. Lane 29
    reversed that (``2eec3ce``), and what this asserts is the reversal from this end: the call
    list for a sheet mount carries exactly one ``cells`` entry, and the grid on the page is the
    one this view drew.

    Kept rather than deleted with the defect. A content renderer that fetched was tried once
    and could be tried again; the property that matters — a view asks for a thing once — is
    cheap to hold and expensive to notice the loss of.
    """
    calls = [name for name, _ in page["calls"]["sheet"]]
    assert calls.count("cells") == 1, page["calls"]["sheet"]
    assert page["calls"]["sheet"] == [
        ["view", {"document_id": 9}],
        ["document", {"document_id": 9}],
        ["back", {"document_id": 9, "limit": 20}],
        ["evidence", {"document_id": 9}],
        ["cells", {"document_id": 9}],
    ]
    grids = _find(page["sheet"], "render-sheet-grid")
    assert len(grids) == 1, "one node, one grid"
    assert "Q3 Pipeline" in page["sheet"]["text"]


def test_the_stylesheet_comes_from_the_view_and_is_linked_once(page):
    """``shell/shell.css:4``'s rule, and the timing note that goes with it.

    A view adds its own styling from its own module, because ``index.html`` is a file five F3
    worktrees would otherwise all edit — the conflict IC-7 exists to remove. At MOUNT and not
    at import: ``shell/manifest.js`` imports every view for its registration alone, and a
    module that reaches for ``document.head`` when it is merely imported cannot be imported
    that way. Six mounts here, one <link>.
    """
    assert len(page["styles"]) == 1, page["styles"]
    rel, href = page["styles"][0]
    assert rel == "stylesheet"
    assert href.endswith("/views/document.css")
    assert href.startswith("file:"), "resolved against the module's own URL, not a host"


def test_no_id_is_a_landing_and_an_unreadable_id_is_a_refusal(page):
    """Ambivalence is not a failure, and a pasted link that is wrong is.

    The shell reads an empty fragment the same way — it lands on the first registered view
    rather than erroring — so a reader who has not said which document gets told what this page
    does and how to name one. ``id=seven`` is a different thing: it is a link that is wrong, and
    making no call at all is the answer.
    """
    assert page["calls"]["landing"] == []
    assert "#/document?id=42" in page["landing"]["text"]
    assert "#/documents" in json.dumps(page["landing"])
    name, message = page["refusals"]["badId"]
    assert name == "CitationRouteError"
    assert "seven" in message
