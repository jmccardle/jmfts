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

import json
import re
import shutil
import subprocess
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

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "jmfts-web" / "jmfts_web" / "static"
VIEW = STATIC / "views" / "document.js"
MANIFEST = STATIC / "shell" / "manifest.js"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="no JavaScript runtime on PATH")

#: A US Letter page (612x792 points) rendered at 150 dpi, which is what
#: ``jmfts_core/rendering.py`` calls ``DPI_DEFAULT``. The numbers are real because the scale
#: this test asserts on is their ratio.
PAGE_PIXELS = {"width": 1275, "height": 1650}
PAGE_DPI = 150

#: ``citation_tasks.py``'s shape, in PDF points, on a 0-based page.
PDF_ANCHOR = {"kind": "pdf", "page": 3, "bbox": [72.0, 118.4, 540.0, 262.9]}
CELLS_ANCHOR = {"kind": "cells", "sheet": "Q3 Pipeline", "ref": "B4:C5"}
SPAN_ANCHOR = {"kind": "span", "char_start": 6, "char_end": 11}

DOM_STUB = textwrap.dedent("""
    export class Element {
      constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.childNodes = [];
        this.attributes = {};
        this.className = "";
        this.dataset = {};
        this.hidden = false;
        this.href = null;
        this.value = "";
        this.disabled = false;
        this.listeners = {};
        this._text = "";
      }
      append(...nodes) {
        for (const node of nodes) {
          if (!(node instanceof Element)) throw new TypeError("append() takes elements");
          this.childNodes.push(node);
        }
      }
      setAttribute(name, value) { this.attributes[name] = String(value); }
      getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
      set textContent(value) {
        this.childNodes = [];
        this._text = value === null || value === undefined ? "" : String(value);
      }
      get textContent() {
        return this._text + this.childNodes.map((n) => n.textContent).join("");
      }
      addEventListener(name, handler) { this.listeners[name] = handler; }
    }

    /** An <img> that decodes to whatever `globalThis.__pixels` says, or fails on demand. */
    export class Image extends Element {
      constructor() { super("img"); this.naturalWidth = 0; this.naturalHeight = 0; }
      set src(value) {
        this._src = value;
        queueMicrotask(() => {
          if (globalThis.__undecodable) return this.onerror(new Error("undecodable"));
          this.naturalWidth = globalThis.__pixels.width;
          this.naturalHeight = globalThis.__pixels.height;
          this.onload();
        });
      }
      get src() { return this._src; }
    }

    const head = new Element("head");
    export const document = { createElement: (tag) => new Element(tag), head };

    export function serialize(element) {
      return {
        tag: element.tagName.toLowerCase(),
        class: element.className || null,
        text: element.textContent,
        href: element.href,
        style: element.getAttribute("style"),
        title: element.getAttribute("title"),
        children: element.childNodes.map(serialize),
      };
    }

    export function install() {
      globalThis.__head = head;
      globalThis.Element = Element;
      globalThis.Image = Image;
      globalThis.document = document;
      globalThis.__pixels = { width: 0, height: 0 };
      globalThis.__revoked = [];
      // Added TO the platform's URL rather than replacing it: `new URL(path, import.meta.url)`
      // is how a view resolves its own stylesheet, and a stub that shadowed the constructor
      // would be testing against a DOM the browser does not have.
      globalThis.URL.createObjectURL = () => "blob:the-page";
      globalThis.URL.revokeObjectURL = (url) => globalThis.__revoked.push(url);
    }
    """)


def _node(tmp_path: Path, harness: str, **substitutions: str) -> object:
    """Run one harness module and parse the single JSON document it prints."""
    (tmp_path / "dom.mjs").write_text(DOM_STUB, encoding="utf-8")
    source = harness.replace("%(static)s", STATIC.as_uri())
    for name, value in substitutions.items():
        source = source.replace(f"%({name})s", value)
    script = tmp_path / "harness.mjs"
    script.write_text(source, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, cwd=tmp_path, timeout=60
    )
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout)


def _find(node: dict, className: str) -> list[dict]:
    found = [node] if node["class"] == className else []
    for child in node["children"]:
        found.extend(_find(child, className))
    return found


def _box(node: dict) -> dict:
    """The one overlay box in a tree, as four numbers off its inline style."""
    boxes = _find(node, "overlay-box")
    assert len(boxes) == 1, f"expected exactly one box, found {len(boxes)}"
    return {
        name: float(value)
        for name, value in re.findall(r"([a-z]+):([-0-9.e]+)px", boxes[0]["style"])
    }


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
    assert re.search(r"\bcells\.columns\b", code)


# ------------------------------------------------------- IC-4: the anchor rides on the route

PARAM_HARNESS = textwrap.dedent("""
    import { install } from "./dom.mjs";
    install();
    const view = await import("%(static)s/views/document.js");

    const out = { parsed: {}, brought: {}, refusals: {}, rows: {} };
    const attempt = (label, params) => {
      out.brought[label] = view.broughtACitation(params);
      try { out.parsed[label] = view.readDetailParams(params); }
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

    out.href = view.hrefWith({ id: "7", anchor: "{}" }, { page: 3 });
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

    ``readDetailParams`` here is BYTE-IDENTICAL to ``readDetailParams`` at
    ``views/search.js:504`` but for the error class, because the writer of this fragment and its
    reader were cut from one commit in two worktrees and cannot import each other. Identical
    copies reconcile by deleting one; a paraphrase reconciles by somebody reading both.
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
    assert name == "DocumentParamError"
    assert "views/search.js" in message, "the message names who writes the fragment"

    for label in ("no id", "bad id"):
        name, message = params["refusals"][label]
        assert name == "DocumentParamError"
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
    import { install, serialize } from "./dom.mjs";
    install();
    const view = await import("%(static)s/views/document.js");
    const { HIGHLIGHT_STATUS } = await import("%(static)s/render/overlay.js");

    const ANCHORS = %(anchors)s;
    globalThis.__pixels = %(pixels)s;
    const calls = [];
    const client = {
      get_document_image: async (args) => {
        calls.push(["image", args]);
        // A real Blob: the page prints its size and its media type, because JSON.stringify
        // flattens one to `{}` and "bytes came back" is not something a reader can check.
        return new Blob([new Uint8Array(4)], { type: "image/png" });
      },
      get_document_cells: async (args) => { calls.push(["cells", args]); return %(cells)s; },
    };

    const out = { calls: {}, panels: {}, results: {}, notices: {}, refusals: {}, planned: {} };

    const page3 = await view.pagePanel({
      client, documentId: 7, page: 3, dpi: %(dpi)s,
      evidence: { anchor: ANCHORS.pdf, unresolved: null }, params: { id: "7" },
    });
    out.calls.image = [...calls];
    out.panels.page = serialize(page3.element);
    out.results.page = page3.result;
    out.surface = page3.surface;
    out.revoked = [...globalThis.__revoked];

    const page9 = await view.pagePanel({
      client, documentId: 7, page: 9, dpi: %(dpi)s,
      evidence: { anchor: ANCHORS.pdf, unresolved: null }, params: { id: "7" },
    });
    out.panels.elsewhere = serialize(page9.element);
    out.results.elsewhere = page9.result;

    const span = await view.pagePanel({
      client, documentId: 7, page: 0, dpi: %(dpi)s,
      evidence: { anchor: ANCHORS.span, unresolved: null }, params: { id: "7" },
    });
    out.panels.span = serialize(span.element);
    out.results.span = span.result;

    calls.length = 0;
    const grid = await view.cellsPanel({
      client, documentId: 7, ref: null,
      evidence: { anchor: ANCHORS.cells, unresolved: null }, params: { id: "7" },
    });
    out.calls.cells = [...calls];
    out.panels.cells = serialize(grid.element);
    out.results.cells = grid.result;
    out.cellsSurface = {
      kind: grid.surface.kind, sheet: grid.surface.sheet,
      source: grid.surface.source, pixels: grid.surface.pixels,
      originCorner: grid.surface.originCorner,
    };

    const surface = view.cellsSurface(%(cells)s);
    out.rects = { inside: surface.cellRect("B4:C5"), outside: surface.cellRect("AA900") };
    for (const [label, ref] of [["a sheet prefix", "Q3!B4"], ["absolute", "$B$4"],
                                ["two colons", "B4:C5:D6"]]) {
      try { surface.cellRect(ref); out.refusals[label] = null; }
      catch (error) { out.refusals[label] = [error.name, error.message]; }
    }

    for (const [label, result] of [
      ["box", { status: HIGHLIGHT_STATUS.BOX, continues: null }],
      ["spread", { status: HIGHLIGHT_STATUS.BOX, continues: [4, 5] }],
      ["continued", { status: HIGHLIGHT_STATUS.CONTINUED, page: 4, from: 3 }],
      ["elsewhere", { status: HIGHLIGHT_STATUS.ELSEWHERE, page: 3 }],
      ["otherSheet", { status: HIGHLIGHT_STATUS.ELSEWHERE, sheet: "Q4", showing: "Q3 Pipeline" }],
      ["none", { status: HIGHLIGHT_STATUS.NONE }],
      ["unresolved", { status: HIGHLIGHT_STATUS.UNRESOLVED, code: "no_text_layer",
                       reason: "the page is a scan" }],
    ]) {
      const notice = view.highlightNotice(result, { params: { id: "7" }, page: 0 });
      out.notices[label] = notice === null ? null : serialize(notice);
    }

    out.planned.pdf = view.plannedSurface({ node: { usetype: "chunk" }, anchor: ANCHORS.pdf });
    out.planned.cells = view.plannedSurface({ node: { usetype: "chunk" }, anchor: ANCHORS.cells });
    out.planned.span = view.plannedSurface({ node: { usetype: "chunk" }, anchor: ANCHORS.span });
    out.planned.sheet = view.plannedSurface({ node: { usetype: "sheet" }, anchor: null });
    out.planned.text = view.plannedSurface({ node: { usetype: "chunk" }, anchor: null });
    console.log(JSON.stringify(out));
    """)


@pytest.fixture(scope="module")
def panels(tmp_path_factory):
    if NODE is None:
        pytest.skip("no JavaScript runtime on PATH")
    return _node(
        tmp_path_factory.mktemp("panels"),
        PANEL_HARNESS,
        anchors=json.dumps({"pdf": PDF_ANCHOR, "cells": CELLS_ANCHOR, "span": SPAN_ANCHOR}),
        pixels=json.dumps(PAGE_PIXELS),
        dpi=str(PAGE_DPI),
        cells=json.dumps(_cells()),
    )


def test_a_rendered_page_declares_the_scale_it_was_rendered_at(panels):
    """The surface is derived from the bytes, not from a second number about the document.

    ``rendering.py`` rasterises at ``dpi/72`` of the page's points, so points are pixels × 72 ÷
    dpi and the scale the overlay divides by is the scale the image was actually produced at —
    pymupdf's rounding included. Asking the appliance for the media box instead would introduce
    a second number that can be wrong, and a box drawn at a silently wrong scale is the failure
    ``_check_bounds`` refuses to clamp for.
    """
    assert panels["calls"]["image"] == [["image", {"document_id": 7, "page": 3, "dpi": PAGE_DPI}]]
    assert panels["surface"]["pixels"] == PAGE_PIXELS
    assert panels["surface"]["source"] == pytest.approx({"width": 612.0, "height": 792.0})
    # No default: pymupdf's page space is top-left with y downward, and the overlay refuses a
    # surface that does not say which corner it means.
    assert panels["surface"]["originCorner"] == "top-left"
    assert panels["surface"]["page"] == 3
    # The object URL lives exactly as long as the decode. Fifty pages is otherwise fifty blobs.
    assert panels["revoked"] == ["blob:the-page"]

    # WHAT CAME BACK, PRINTED AS BYTES. A binary response has no shape a page can render —
    # `JSON.stringify` flattens a Blob to `{}` — so the caption names its size, its media type,
    # and the density that produced the pixels the box was scaled against.
    caption = _find(panels["panels"]["page"], "dim doc-caption")[0]
    assert caption["text"] == "page 4 — 4 bytes of image/png, 1275×1650 px at 150 dpi"


def test_a_pdf_anchor_is_boxed_on_the_page_it_names(panels):
    """The beeline's last inch, and the geometry that makes it land.

    612x792 points at 150 dpi is 1275x1650 pixels, so the scale is 25/12. The anchor's
    rectangle in points times that scale is the box, and it is positioned inside the same
    ``.overlay-surface`` the image is in — which is what ``shell.css`` styles for IC-9.
    """
    assert panels["results"]["page"]["status"] == "box"
    assert panels["results"]["page"]["continues"] is None
    assert _box(panels["panels"]["page"]) == pytest.approx(
        {"left": 150.0, "top": 246.667, "width": 975.0, "height": 301.042}, rel=1e-4
    )
    frame = _find(panels["panels"]["page"], "overlay-surface")[0]
    assert [child["tag"] for child in frame["children"]] == ["img", "div"]


def test_a_box_is_never_drawn_on_a_page_the_passage_is_not_on(panels):
    """Confidently wrong is worse than absent, and indistinguishable from right.

    Page 9 of a passage anchored on page 3 gets no rectangle and a sentence naming the page
    that has one — with a link to it, because the reader's next move is obvious and making them
    edit the fragment by hand would be the other kind of defect.
    """
    assert panels["results"]["elsewhere"] == {"status": "elsewhere", "page": 3}
    assert not _find(panels["panels"]["elsewhere"], "overlay-box")
    notice = _find(panels["panels"]["elsewhere"], "overlay-notice")[0]
    assert (
        "page 4" in notice["text"]
    ), "pages are labelled 1-based for a reader, 0-based on the wire"
    assert "#/document?id=7&page=3" in json.dumps(panels["panels"]["elsewhere"])


def test_a_span_anchor_says_where_it_can_be_drawn_instead_of_failing_silently(panels):
    """A span IS a valid anchor; what it has no geometry for is a page.

    The overlay refuses and its message points at the text renderer. The panel shows that
    message rather than rewriting it, because the sentence already says what to do next.
    """
    assert panels["results"]["span"] is None
    notice = _find(panels["panels"]["span"], "overlay-notice")[0]
    assert "anchor.py" in notice["text"]
    assert not _find(panels["panels"]["span"], "overlay-box")


def test_a_spreadsheet_region_is_a_grid_of_cells_with_the_range_boxed_on_it(panels):
    """Step 22's decision, drawn: cells, not a picture of cells.

    ``B4:C5`` on a grid whose first column is A and whose first row is 3 lands one column in
    and one row down — plus one unit on each axis for the column-letter header and the
    row-number gutter, which is why ``cellsSurface`` counts them as the first unit rather than
    leaving the box to float over the wrong cells.
    """
    assert panels["calls"]["cells"] == [["cells", {"document_id": 7, "ref": None}]]
    assert panels["cellsSurface"]["source"] == {"width": 5, "height": 5}
    assert panels["cellsSurface"]["pixels"] == {"width": 5 * 104, "height": 5 * 24}
    assert panels["results"]["cells"]["status"] == "box"

    # One column in, one row down, two cells wide and two tall, on the declared pitch.
    assert _box(panels["panels"]["cells"]) == pytest.approx(
        {"left": 2 * 104.0, "top": 2 * 24.0, "width": 2 * 104.0, "height": 2 * 24.0}
    )

    # The values are values, and the formula behind one of them is on it rather than instead
    # of it — which is what a raster of the same region could not do.
    grid = _find(panels["panels"]["cells"], "doc-grid")[0]
    assert [cell["text"] for cell in grid["children"][0]["children"]] == ["", "A", "B", "C", "D"]
    assert [cell["text"] for cell in grid["children"][2]["children"]] == [
        "4",
        "Acme",
        "Won",
        "12000",
        "kim",
    ]
    formula = _find(grid, "formula")[0]
    assert formula["title"] == "=B4*1.2"
    # A blank cell is blank, and it is not the word "null".
    assert grid["children"][3]["children"][4]["text"] == ""
    assert "addressed by used_range" in panels["panels"]["cells"]["text"]


def test_the_grid_looks_a_reference_up_and_refuses_to_invent_a_grammar(panels):
    """IC-9 delegates A1 to the surface, and the surface delegates the grammar to the server.

    ``CellsAnchor``'s docstring refuses a second A1 parser in the client and a third in
    JavaScript would be worse again, so this resolves a reference against the columns and rows
    the appliance said it rendered: ``B`` is an index into ``cells.columns`` and ``4`` an index
    into ``cells.rows``. Anything that is not a plain COLUMNROW pair — a sheet prefix, an
    absolute reference, a second colon — refuses rather than being guessed at, and a range that
    is simply outside the rendered region is null, which the overlay reads as "elsewhere".
    """
    assert panels["rects"]["inside"] == [2, 2, 4, 4]
    assert panels["rects"]["outside"] is None
    for label in ("a sheet prefix", "absolute", "two colons"):
        name, message = panels["refusals"][label]
        assert name == "CellRefError", label
    assert "office/cells.py" in panels["refusals"]["absolute"][1]


def test_every_overlay_status_reaches_the_page_as_its_own_sentence(panels):
    """The requirement, rather than a flourish.

    A viewer that drew a box for one status and nothing for the other four would be the surface
    that hides ``source_anchor.unresolved`` — the failure IC-9 is shaped around. A plain box
    that continues nowhere is the one case with no sentence, because there the rectangle IS the
    statement.
    """
    notices = panels["notices"]
    assert notices["box"] is None
    assert "continues on page 5, page 6" in notices["spread"]["text"]
    assert "page 4" in notices["continued"]["text"] and "page 5" in notices["continued"]["text"]
    assert "#/document?id=7&page=3" in json.dumps(notices["continued"])
    assert "page 4" in notices["elsewhere"]["text"]
    assert "Q4" in notices["otherSheet"]["text"]
    assert "nothing has tried to place it" in notices["none"]["text"]
    assert "the page is a scan" in notices["unresolved"]["text"]
    assert "no_text_layer" in notices["unresolved"]["text"]


def test_the_surface_is_chosen_before_anything_is_fetched(panels):
    """A 409 per text node is not a diagnosis, it is noise in the one log that is the evidence.

    Step 23's second property is that a view showing something the log has no call for computed
    it in the browser. A page that probed ``/image`` on every node to find out whether there was
    a picture would fill that log with expected refusals, so the surface is decided from the
    anchor and the usetype, and where there is nothing to decide from the panel is OFFERED.
    """
    assert panels["planned"]["pdf"] == {"kind": "page", "page": 3}
    assert panels["planned"]["cells"] == {"kind": "cells", "ref": None}
    assert panels["planned"]["span"] == {"kind": "span"}
    assert panels["planned"]["sheet"] == {"kind": "cells", "ref": None}
    assert panels["planned"]["text"] == {"kind": "offer", "page": 0}


# ------------------------------------------------------------------------------- the page

PAGE_HARNESS = textwrap.dedent("""
    import { install, serialize } from "./dom.mjs";
    install();
    const module = await import("%(static)s/views/document.js");
    const { views } = await import("%(static)s/shell/registry.js");

    const fixtures = %(fixtures)s;
    globalThis.__pixels = %(pixels)s;
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
        return new Blob([new Uint8Array(4)], { type: "image/png" });
      },
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

    // Six mounts above, and one stylesheet — appended to <head> by the view's own module.
    out.styles = globalThis.__head.childNodes.map((n) => [n.rel, n.href]);
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
    }
    return _node(
        tmp_path_factory.mktemp("page"),
        PAGE_HARNESS,
        fixtures=json.dumps(fixtures),
        pixels=json.dumps(PAGE_PIXELS),
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
    assert _box(page["anchored"])["left"] == pytest.approx(150.0)


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


def test_the_grid_pitch_in_the_stylesheet_is_the_one_the_surface_computes_in():
    """Two files, one number, and a test rather than a request to remember.

    ``cellsSurface()`` computes the overlay's pixel extent as (columns + 1) × ``CELL_W`` by
    (rows + 1) × ``CELL_H``, so the box lands on the right cells only while the stylesheet lays
    them out on exactly that pitch. CSS cannot import a JavaScript constant, so the agreement
    is asserted here: change one and this goes red rather than the boxes going crooked.
    """
    js = VIEW.read_text(encoding="utf-8")
    css = (STATIC / "views" / "document.css").read_text(encoding="utf-8")
    width = int(re.search(r"export const CELL_W = (\d+);", js).group(1))
    height = int(re.search(r"export const CELL_H = (\d+);", js).group(1))
    grid = css[css.index(".view-document .doc-grid td,") :]
    assert f"width: {width}px;" in grid, f"document.css does not lay cells out {width}px wide"
    assert f"height: {height}px;" in grid
    assert f"max-width: {width}px;" in grid


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
    assert name == "DocumentParamError"
    assert "seven" in message
