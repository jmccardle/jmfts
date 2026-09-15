"""The five renderers, and the figures a view builds out of bytes it already fetched.

``docs/SPRINT_0_6_0.md`` Block F step 29. ``tests/test_web_views.py`` holds the two contracts
this step is written against — IC-8's bijection between ``RENDERERS`` and the registrations,
and IC-9's overlay over synthetic surfaces — and this file holds what step 29 actually built:
the two renderers step 25 shipped unfinished, and the library a view composes to put a page, a
crop or a grid of cells on the screen with the passage boxed on it.

**A renderer draws CONTENT; a view draws SOURCE.** ``render()`` is synchronous, takes a string
and holds no client, so it cannot fetch a page image — and it should not, because the byte
routes need a bearer token and putting a second holder of that token inside library code makes
step 23's "a view that shows something the log has no call for computed it client-side"
unreadable. So the page, the crop and the grid are ``render/surfaces.js`` and
``render/sheet.js`` functions that take a Blob or a response the VIEW obtained. Every test
below hands them one.

Four properties are worth naming, because each is a defect this sprint was written against,
caught one layer down:

* **The origin corner is declared, never passed.** Writing the overlay once does not help if
  three callers each choose a corner, which is what was happening before this library existed.
* **A picture of the wrong page is worse than no picture** (``OFFICE_SPEC.md`` Part 5), so
  ``pageFor`` refuses rather than defaulting to page 0, and the view sends its answer
  explicitly so the server never takes the same decision twice.
* **What is withheld is said with the real numbers.** A sheet region larger than the grid this
  library will paint carries a sentence naming how many of its rows are on screen.
* **Which representation is on the page is stated from a named row.** ``sourceNotice`` reads
  whether the ``rendition`` evidence row is present and says so; nothing catches a failed call
  and substitutes extracted text for a picture.

These need no database and no browser. Where ``node`` is on PATH they execute the bundle's ES
modules against the same stubbed DOM ``tests/test_web_views.py`` uses, plus the two things a
page holding bytes needs that the stub has no reason to carry: ``Blob`` (node's own) and
``URL.createObjectURL``.
"""

from __future__ import annotations

import base64
import json
import re
import struct
import subprocess
import textwrap
import zlib
from pathlib import Path

import pytest

from jmfts_core.conversation_ingest import ParsedMessage, conversation_markdown
from tests.test_web_views import DOM_STUB, NODE, STATIC, needs_node

# --------------------------------------------------------------------------- the node harness

#: The stub ``tests/test_web_views.py`` runs the bundle against, plus object URLs.
#:
#: A page that holds bytes turns them into something an ``<img>`` can point at, and the browser
#: API for that is ``URL.createObjectURL``. It is not in the shared stub because nothing before
#: step 29 held any bytes; it is here rather than there so that this file adds what it needs
#: without editing the harness three other lanes are also reading.
RENDER_DOM = DOM_STUB + textwrap.dedent("""
    /**
     * `serialize`, plus every attribute rather than `title` alone.
     *
     * The shared one reports `title` because that is the only attribute anything before step
     * 29 set. These renderers set `src`, `href`, `download`, `alt` and a `style` carrying the
     * overlay box's position, and each of those is a thing a test has to be able to read —
     * the box's position in particular, which is the whole of IC-9's arithmetic.
     */
    export function deep(element) {
      return {
        tag: element.tagName.toLowerCase(),
        class: element.className || null,
        attrs: { ...element.attributes },
        hidden: element.hidden === true,
        text: element.textContent,
        html: element.innerHTML || null,
        children: element.childNodes.map(deep),
      };
    }

    /** Object URLs, and a record of which are still held, so a leak is visible to a test.
     *
     * ADDED TO the platform's URL rather than replacing it. `new URL(path, import.meta.url)`
     * is how every view in this bundle resolves its own stylesheet — `shell/shell.css:4` is
     * the rule and five views follow it — so a stub that shadowed the constructor made any
     * view that BOTH holds bytes and links a stylesheet impossible to mount. The document
     * detail view is the first of those, and it found this.
     */
    export function installObjectUrls() {
      const held = new Map();
      let next = 0;
      globalThis.URL.createObjectURL = (blob) => {
        const url = `blob:jmfts/${next += 1}`;
        held.set(url, blob);
        return url;
      };
      globalThis.URL.revokeObjectURL = (url) => held.delete(url);
      return held;
    }
    """)


def _node(tmp_path: Path, harness: str, **substitutions: str) -> object:
    """Run one harness module and parse the single JSON document it prints."""
    (tmp_path / "dom.mjs").write_text(RENDER_DOM, encoding="utf-8")
    script = tmp_path / "harness.mjs"
    # `str.replace` and not the `%` operator, which `tests/test_web_views.py` uses for its one
    # placeholder: a fixture here is base64 and JSON, and a literal `%` anywhere in either
    # would make the format string a TypeError about an unsupported conversion.
    filled = harness.replace("%(static)s", STATIC.as_uri())
    for name, value in substitutions.items():
        filled = filled.replace(f"%({name})s", value)
    left = re.findall(r"%\([a-z_]+\)s", filled)
    assert not left, f"the harness still carries {sorted(set(left))}"
    script.write_text(filled, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, cwd=tmp_path, timeout=60
    )
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout)


def png_bytes(width: int, height: int) -> bytes:
    """A REAL PNG of the named size, not twenty-four bytes that look like one.

    ``surfaces.pngSize`` reads the extent out of the IHDR, so a header alone would exercise the
    code. It would also be a fixture that is not what the route serves: ``/image`` answers a
    whole PNG, and a test whose fixture is a fragment cannot tell a reader whether the page
    would have drawn it. This is eight-bit greyscale, every pixel white, which compresses to
    about a kilobyte at any size this suite uses.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


#: US Letter at 144 dpi: 612x792 points becomes 1224x1584 pixels, and the ratio is exactly 2.
#: Chosen so that every number in the assertions below is exact rather than rounded, which is
#: what lets a wrong scale be told from floating-point tail.
PAGE_PIXELS = (1224, 1584)
PAGE_POINTS = (612.0, 792.0)

#: The anchor every surface in this file is asked to place, verbatim from the shape
#: ``citation_tasks.py:6`` writes and ``tests/test_web_views.py`` pins.
PDF_ANCHOR = {"kind": "pdf", "page": 3, "bbox": [72.0, 118.4, 540.0, 262.9]}


def _style(node: dict) -> dict[str, float]:
    """One ``style`` attribute as ``{property: percentage}``."""
    raw = node["attrs"]["style"]
    return {
        name: float(value.rstrip("%"))
        for name, value in (part.split(":") for part in raw.split(";"))
    }


def _find(node: dict, className: str) -> list[dict]:
    found = [node] if node["class"] == className else []
    for child in node["children"]:
        found.extend(_find(child, className))
    return found


# ------------------------------------------------------------------------------- markdown

MARKDOWN_HARNESS = textwrap.dedent("""
    import { install, deep as serialize } from "./dom.mjs";
    install();
    const { renderMarkdown, linkHref, MarkdownError } =
      await import("%(static)s/render/markdown.js");

    const documents = %(documents)s;
    const out = { rendered: {}, hrefs: {}, refusals: {} };
    for (const [name, source] of Object.entries(documents)) {
      out.rendered[name] = serialize(renderMarkdown(source));
    }
    for (const url of %(urls)s) out.hrefs[url] = linkHref(url);
    try {
      renderMarkdown(null);
      out.refusals.notText = null;
    } catch (error) {
      out.refusals.notText = [error.name, error.message];
    }
    console.log(JSON.stringify(out));
    """)


#: One document per block type the renderer claims, because the claim is the thing under test.
#:
#: Step 25 refused to ship half a markdown renderer, and its reason names the failure exactly:
#: "One that handled headings and paragraphs and dropped tables, links and code fences would
#: render most documents plausibly and a few wrongly, and the wrong ones would look like
#: documents that simply had no tables in them." So every block type its header claims has a
#: case here, and the two it refuses have one too.
MARKDOWN_DOCUMENTS = {
    "heading": "## A heading with `code` in it",
    "setext": "Underlined\n==========",
    "paragraph": "one\ntwo",
    "emphasis": "plain *em* **strong** `code`",
    "escaped": r"not \*emphasis\* here",
    "link": "see [the spec](https://example.invalid/spec) for more",
    "danger": "[click](javascript:alert(1)) and <script>alert(2)</script>",
    "image": "![a plot](https://example.invalid/plot.png)",
    "bullets": "- one\n- two\n  - nested\n- three",
    "loose": "- one\n\n- two",
    "ordered": "3. third\n4. fourth",
    "quote": "> quoted **text**\n> and more",
    "fence": "```python\nx = 1\n```",
    "indented": "    x = 1\n    y = 2",
    "table": "| a | b |\n|:--|--:|\n| 1 | 2 |\n| 3 |",
    "rule": "above\n\n---\n\nbelow",
}


@needs_node
def test_markdown_renders_every_block_type_it_claims(tmp_path):
    """The renderer step 25 declined to ship half of, checked against the whole claim."""
    result = _node(
        tmp_path,
        MARKDOWN_HARNESS,
        documents=json.dumps(MARKDOWN_DOCUMENTS),
        urls=json.dumps([]),
    )
    rendered = result["rendered"]
    tags = {name: [child["tag"] for child in tree["children"]] for name, tree in rendered.items()}

    assert tags["heading"] == ["h2"]
    assert rendered["heading"]["children"][0]["children"][1]["class"] == "render-md-code-span"
    assert tags["setext"] == ["h1"]
    assert rendered["setext"]["children"][0]["text"] == "Underlined"
    assert tags["paragraph"] == ["p"]
    assert tags["bullets"] == ["ul"]
    assert tags["ordered"] == ["ol"]
    assert tags["quote"] == ["blockquote"]
    assert tags["fence"] == ["pre"]
    assert tags["indented"] == ["pre"]
    assert tags["table"] == ["table"]
    assert tags["rule"] == ["p", "hr", "p"]

    # A fenced block keeps the language on the class every highlighter looks for, the same way
    # the `code` renderer does — so one can be added later without either changing.
    assert rendered["fence"]["children"][0]["children"][0]["class"] == "language-python"

    # Emphasis, and an escape that must NOT be emphasis. The second is the one that is easy to
    # get wrong: resolving escapes after the scan resolves them too late.
    assert [c["tag"] for c in rendered["emphasis"]["children"][0]["children"]] == [
        "span",
        "em",
        "span",
        "strong",
        "span",
        "code",
    ]
    assert rendered["escaped"]["children"][0]["text"] == "not *emphasis* here"

    # Nesting is not a special case: an item's content is parsed by the same block parser, so a
    # list inside a list is an ordinary parse rather than a depth counter.
    nested = rendered["bullets"]["children"][0]["children"][1]
    assert [c["tag"] for c in nested["children"]] == ["p", "ul"]

    # A blank line BETWEEN items belongs to the list. Two lists here would be a document
    # rendered wrongly in a way that looks like a document written that way.
    assert len(rendered["loose"]["children"]) == 1
    assert len(rendered["loose"]["children"][0]["children"]) == 2

    # A short table row is rendered short. Padding it would invent cells the document does not
    # have, and the header is what says how many there should be.
    rows = rendered["table"]["children"][0]["children"]
    assert [len(row["children"]) for row in rows] == [2, 2, 1]
    assert rows[0]["children"][1]["class"] == "align-right"


@needs_node
def test_markdown_never_makes_a_document_into_markup_or_a_request(tmp_path):
    """Two rules, and both are about content this appliance did not write.

    ``rendered_content`` arrived by upload, by extraction, or out of an LLM. So a ``<script>``
    in it is five characters of text and never a tag — every string in that module reaches the
    page through ``textContent`` — and a ``javascript:`` URL never becomes an ``href``. The
    third is the one that is easy to miss: an ``<img src>`` built out of a document's own
    markdown would put ``test_nothing_in_the_bundle_loads_from_the_network`` in the document's
    hands, fetching whatever a markdown file names from wherever it names it. The alt text and
    the URL are shown instead, and the reader decides.
    """
    result = _node(
        tmp_path,
        MARKDOWN_HARNESS,
        documents=json.dumps(MARKDOWN_DOCUMENTS),
        urls=json.dumps(
            [
                "https://example.invalid/x",
                "http://example.invalid/x",
                "mailto:someone@example.invalid",
                "#section",
                "../other.md",
                "javascript:alert(1)",
                "data:text/html,<script>alert(1)</script>",
                "vbscript:msgbox",
                "   ",
            ]
        ),
    )

    assert result["hrefs"] == {
        "https://example.invalid/x": "https://example.invalid/x",
        "http://example.invalid/x": "http://example.invalid/x",
        "mailto:someone@example.invalid": "mailto:someone@example.invalid",
        "#section": "#section",
        "../other.md": "../other.md",
        "javascript:alert(1)": None,
        "data:text/html,<script>alert(1)</script>": None,
        "vbscript:msgbox": None,
        "   ": None,
    }

    good = _find(result["rendered"]["link"], "render-md-link")
    assert len(good) == 1
    assert good[0]["attrs"]["href"] == "https://example.invalid/spec"

    danger = result["rendered"]["danger"]
    assert _find(danger, "render-md-link") == [], "a javascript: URL became a live link"
    refused = _find(danger, "render-md-refused-link")
    assert len(refused) == 1
    assert "not followed" in refused[0]["text"]
    # The script tag is text. `serialize` reports `innerHTML` separately from `textContent`
    # exactly so that this assertion cannot pass by accident.
    assert "<script>alert(2)</script>" in danger["text"]
    assert danger["html"] is None

    picture = _find(result["rendered"]["image"], "render-md-image-ref")
    assert len(picture) == 1
    assert "a plot" in picture[0]["text"] and "plot.png" in picture[0]["text"]
    assert not _find(result["rendered"]["image"], "render-picture")

    assert result["refusals"]["notText"][0] == "MarkdownError"


# ----------------------------------------------------------------------------- transcripts

TRANSCRIPT_HARNESS = textwrap.dedent("""
    import { install, deep as serialize } from "./dom.mjs";
    install();
    const { renderTranscript, readTurns, TranscriptError } =
      await import("%(static)s/render/transcript.js");

    const out = { rendered: serialize(renderTranscript(%(source)s)), refusals: {} };
    out.turns = readTurns(%(source)s);
    out.preamble = readTurns(%(preamble)s);
    try {
      renderTranscript("just some prose, with no turns in it");
      out.refusals.notATranscript = null;
    } catch (error) {
      out.refusals.notATranscript = [error.name, error.message];
    }
    console.log(JSON.stringify(out));
    """)


@needs_node
def test_a_transcript_is_turns_and_the_delimiter_is_read_off_its_writer(tmp_path):
    """Step 25's stated reason for holding this renderer back, checked rather than assumed.

    It reads: "a transcript renderer has to know how a turn is delimited in
    ``rendered_content``, and nothing in this tree states that shape. Guessing it here would
    put a guess behind an interface five later views plug into." The first half is right and
    the second was one grep from being false — ``conversation_ingest.conversation_markdown``
    writes the shape and ``structure_tasks._extract_conversation`` is its only caller.

    So the fixture here is built by THAT FUNCTION rather than by hand. A test that spelled
    ``[role]: content`` itself would be checking the renderer against the same guess step 25
    refused to make; built this way, changing the writer fails this test.
    """
    messages = [
        ParsedMessage(role="user", content="What does `settled` mean?", turn_index=0),
        ParsedMessage(
            role="assistant",
            # A turn whose own content holds a blank line, which is what makes "split on the
            # marker" and "split on the blank line between turns" different rules. The writer
            # joins turns with "\n\n" and escapes nothing, so the second rule would cut this
            # message into two turns, the later one with no speaker.
            content="Retrieval indexes are partial on it.\n\nSo an in-flight node is invisible.",
            turn_index=1,
        ),
        ParsedMessage(role="user", content="- and a list\n- of two things", turn_index=2),
    ]
    source = conversation_markdown(messages)
    assert "[assistant]: " in source, "the writer's shape changed; read it again"

    result = _node(
        tmp_path,
        TRANSCRIPT_HARNESS,
        source=json.dumps(source),
        preamble=json.dumps("stray text\n\n[user]: hello"),
    )

    assert [turn["role"] for turn in result["turns"]] == ["user", "assistant", "user"]
    assert result["turns"][1]["content"] == messages[1].content
    assert [turn["role"] for turn in result["preamble"]] == [None, "user"]

    tree = result["rendered"]
    assert tree["class"] == "render-transcript"
    assert [c["class"] for c in tree["children"]] == ["render-turn"] * 3
    roles = [c["children"][0]["text"] for c in tree["children"]]
    assert roles == ["user", "assistant", "user"]

    # A turn's content is markdown, because the writer passes a message body through untouched
    # and a message body is what the participants typed.
    assert _find(tree["children"][0], "render-md-code-span")[0]["text"] == "settled"
    assert [c["tag"] for c in tree["children"][2]["children"][1]["children"]] == ["ul"]

    # Prose under a `transcript` presentation is a data fault somebody has to see — the same
    # refusal `json-table` makes for content that is not JSON, for the same reason.
    assert result["refusals"]["notATranscript"][0] == "TranscriptError"
    assert "conversation_ingest.py:164" in result["refusals"]["notATranscript"][1]


# ------------------------------------------------------------- IC-9, over the real surfaces

SURFACE_HARNESS = textwrap.dedent("""
    import { install } from "./dom.mjs";
    install();
    const { rasterSurface, gridSurface, pngSize, drawHighlight, SurfaceError } =
      await import("%(static)s/render/surfaces.js");

    const PIXELS = { width: 1224, height: 1584 };
    const out = { same: {}, corners: {}, refusals: {}, boxes: {} };

    // Three surfaces, three kinds, one declared geometry. The numbers are contrived so that
    // the grid's cell units and the page's points are the same space: 612x792 cells of 2x2
    // pixels is 1224x1584 pixels, and so is 612x792 points at 144 dpi.
    const surfaces = {
      "pdf-page": rasterSurface({ kind: "pdf-page", pixels: PIXELS, dpi: 144, page: 3 }),
      image: rasterSurface({ kind: "image", pixels: PIXELS, dpi: 144, page: 3 }),
      "sheet-region": gridSurface({
        kind: "sheet-region", columns: 612, rows: 792, cell: { width: 2, height: 2 },
        sheet: "Q3 Pipeline", cellRect: () => [72.0, 118.4, 540.0, 262.9],
      }),
    };
    const anchors = {
      "pdf-page": %(pdf)s, image: %(pdf)s,
      "sheet-region": { kind: "cells", sheet: "Q3 Pipeline", ref: "B4:H120" },
    };
    for (const [kind, surface] of Object.entries(surfaces)) {
      const container = document.createElement("div");
      const found = drawHighlight(container, { anchor: anchors[kind], unresolved: null }, surface);
      out.same[kind] = found;
      out.boxes[kind] = container.childNodes.map((node) => ({
        class: node.className, style: node.getAttribute("style"),
      }));
      out.corners[kind] = surface.originCorner;
      out[`source_${kind}`] = surface.source;
    }

    // The corner is DECLARED by the factory and is not a parameter: passing one is ignored.
    out.corners.asked = rasterSurface({
      kind: "pdf-page", pixels: PIXELS, dpi: 144, page: 0, originCorner: "bottom-left",
    }).originCorner;

    const refuse = (label, fn) => {
      try { fn(); out.refusals[label] = null; }
      catch (error) { out.refusals[label] = [error.name, error.message]; }
    };
    refuse("not a png", () => pngSize(new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8, 9])));
    refuse("no density", () => rasterSurface({ kind: "x", pixels: PIXELS, dpi: 0 }));
    refuse("no cells", () =>
      gridSurface({ kind: "x", columns: 0, rows: 3, cell: { width: 1, height: 1 } }));
    out.size = pngSize(Uint8Array.from(atob("%(png)s"), (c) => c.charCodeAt(0)));
    console.log(JSON.stringify(out));
    """)


@needs_node
def test_one_anchor_lands_in_one_place_on_all_three_real_surfaces(tmp_path):
    """IC-9's pin, over the surfaces the renderers actually build.

    ``tests/test_web_views.py`` holds the same property over synthetic surfaces, which checks
    the transform. This checks the two factories that produce every surface in the bundle:
    that a page, a crop and a grid of cells declare their geometry the same way, and that one
    anchor therefore draws one box on all three.

    The origin corner is the part worth reading. overlay.js refuses a surface that does not
    declare one, and the sprint plan names "which corner the origin is in" as the disagreement
    that writing the overlay three times produces. Writing it once does not help if three
    callers each pass a corner, so the factories do not TAKE one: `rasterSurface` states
    ``top-left`` because the anchor comes from pymupdf's rectangles and the picture comes from
    pymupdf's pixmap, and a corner passed in is ignored.
    """
    result = _node(
        tmp_path,
        SURFACE_HARNESS,
        pdf=json.dumps(PDF_ANCHOR),
        png=base64.b64encode(png_bytes(*PAGE_PIXELS)).decode(),
    )

    assert set(result["same"]) == {"pdf-page", "image", "sheet-region"}
    assert len({json.dumps(found, sort_keys=True) for found in result["same"].values()}) == 1
    assert result["same"]["pdf-page"]["status"] == "box"

    for kind in ("pdf-page", "image", "sheet-region"):
        assert result[f"source_{kind}"] == {"width": PAGE_POINTS[0], "height": PAGE_POINTS[1]}
        assert result["corners"][kind] == "top-left"
    assert result["corners"]["asked"] == "top-left", (
        "a surface factory took an origin corner as an argument, which puts the coin flip IC-9 "
        "removed back one file along"
    )

    # The box is positioned as a FRACTION of the surface and not in absolute pixels: the
    # picture is `max-width: 100%`, so a narrow browser scales it, and a box in pixels would
    # stay where the unscaled picture used to be.
    for kind, drawn in result["boxes"].items():
        assert [node["class"] for node in drawn] == ["overlay-box"], kind
        placed = _style({"attrs": {"style": drawn[0]["style"]}})
        assert placed == pytest.approx(
            {"left": 11.7647, "top": 14.9495, "width": 76.4706, "height": 18.2449}, abs=1e-4
        ), kind

    # The extent comes out of the picture's own header, which is available the moment the bytes
    # arrive rather than after the browser has decoded them.
    assert result["size"] == {"width": PAGE_PIXELS[0], "height": PAGE_PIXELS[1]}
    assert result["refusals"]["not a png"][0] == "SurfaceError"
    assert "image/png" in result["refusals"]["not a png"][1]
    assert result["refusals"]["no density"][0] == "SurfaceError"
    assert result["refusals"]["no cells"][0] == "SurfaceError"


# ------------------------------------------------- the figures a view builds from its bytes

FIGURE_HARNESS = textwrap.dedent("""
    import { install, deep as serialize, installObjectUrls } from "./dom.mjs";
    install();
    const held = installObjectUrls();
    const surfaces = await import("%(static)s/render/surfaces.js");
    const { gridFigure } = await import("%(static)s/render/sheet.js");

    const PNG = Uint8Array.from(atob("%(png)s"), (c) => c.charCodeAt(0));
    const CELLS = %(cells)s;
    const EVIDENCE = %(evidence)s;
    const page = () => new Blob([PNG], { type: "image/png" });

    const out = { figures: {}, pages: {}, refusals: {}, notices: {} };
    const record = async (label, build) => {
      const figure = await build();
      out.figures[label] = {
        tree: serialize(figure),
        surface: figure.surface ? { ...figure.surface(), cellRect: undefined } : null,
        highlight: figure.highlight ?? null,
      };
      return figure;
    };

    // What a view does: read the evidence it fetched, decide the page, send it, draw the box.
    const anchored = surfaces.anchorPair(EVIDENCE["7"]);
    const unresolved = surfaces.anchorPair(EVIDENCE["8"]);
    const bare = surfaces.anchorPair(EVIDENCE["10"]);
    // The office node: an exact address that cannot be drawn, and a rectangle that can.
    const both = surfaces.anchorPair(EVIDENCE["12"]);
    out.pairs = { anchored, unresolved, bare, both };

    await record("page", () => surfaces.pageFigure({
      blob: page(), page: surfaces.pageFor({ id: 7, usetype: "chunk" }, anchored),
      dpi: 150, evidence: anchored, label: "A chunk",
    }));
    await record("page_unresolved", () => surfaces.pageFigure({
      blob: page(), page: 4, dpi: 150, evidence: unresolved, label: "A chunk",
    }));
    await record("page_elsewhere", () => surfaces.pageFigure({
      blob: page(), page: 9, dpi: 150, evidence: anchored, label: "A chunk",
    }));
    await record("page_none", () => surfaces.pageFigure({
      blob: page(), page: 0, dpi: 150, evidence: bare, label: "The report",
    }));
    // The SAME function, the same transform, the same box — and a caption that says whose
    // pagination the page number belongs to, read off `PdfAnchor.of` and off nothing else.
    await record("page_rendition", () => surfaces.pageFigure({
      blob: page(), page: both.rendition.anchor.page, dpi: 150,
      evidence: both.rendition, label: "The proposal.docx",
    }));
    await record("region", () => surfaces.regionFigure({
      blob: page(), dpi: 150, anchor: anchored.anchor, label: "A chunk",
    }));
    await record("cells", () => gridFigure(CELLS[""], null));
    await record("cells_boxed", () => gridFigure(CELLS[""], {
      anchor: { kind: "cells", sheet: "Q3 Pipeline", ref: "C5:D6" }, unresolved: null,
    }));
    await record("cells_off_region", () => gridFigure(CELLS[""], {
      anchor: { kind: "cells", sheet: "Q3 Pipeline", ref: "Z900" }, unresolved: null,
    }));
    await record("cells_huge", () => gridFigure(CELLS["A1:B900"], null));

    // `pageFor`, which is the one decision this side takes that the server would also take.
    const ask = (label, document_, evidence, named) => {
      try { out.pages[label] = surfaces.pageFor(document_, evidence, named); }
      catch (error) { out.pages[label] = [error.name, error.message]; }
    };
    ask("anchor", { id: 7, usetype: "chunk" }, anchored, undefined);
    ask("file", { id: 1, usetype: "file" }, bare, undefined);
    ask("named", { id: 7, usetype: "chunk" }, anchored, 5);
    ask("unresolved", { id: 8, usetype: "chunk" }, unresolved, undefined);
    ask("nothing", { id: 10, usetype: "chunk" }, bare, undefined);
    ask("not a page", { id: 1, usetype: "file" }, bare, -2);

    // The sentence that says which representation of a document is on the page.
    const evidence = EVIDENCE["1"].evidence;
    const file = evidence.file;
    // The row, not a key inside `file` — `INGEST_SPEC.md` 3.3 forbids writing a derivation
    // into the record of what was received. `hasOwnProperty`, because a row present and
    // empty is a rendition and `evidence.rendition` being falsy would call it absent.
    out.notices.extracted = serialize(surfaces.sourceNotice({
      rendition: Object.prototype.hasOwnProperty.call(evidence, "rendition"),
      filename: file.filename, mime: file.detected_mime,
    }));
    out.notices.rendition = serialize(surfaces.sourceNotice({
      rendition: true, filename: file.filename, mime: file.detected_mime,
    }));

    // A box that is only part of its passage, and a box that is the whole of one. The two must
    // not read the same; `overlay.js` carries `continues` precisely so they cannot.
    const overlay = await import("%(static)s/render/overlay.js");
    const anchor = %(pdf)s;
    const boxed = surfaces.rasterSurface({
      kind: "pdf-page", pixels: { width: 1224, height: 1584 }, dpi: 144, page: anchor.page,
    });
    // Both keys, always — IC-9 refuses a caller that passes only one, because "I did not look"
    // is not "there was no reason recorded". There is no unresolved row in any of these cases.
    const notice = (continues) => surfaces.highlightNotice(
      overlay.highlight(
        { anchor: continues === null ? anchor : { ...anchor, continues }, unresolved: null },
        boxed
      )
    );
    out.notices.whole = notice(null);
    out.notices.partialOne = notice([4]);
    out.notices.partialMany = notice([4, 5]);
    out.notices.partialEmpty = notice([]);

    // The original bytes, once a view has asked for them.
    const original = new Blob([PNG], { type: "application/vnd.openxmlformats-officedocument" });
    out.notices.download = serialize(surfaces.downloadLink(original, "report.docx"));

    const refuse = (label, fn) => {
      try { fn(); out.refusals[label] = null; }
      catch (error) { out.refusals[label] = [error.name, error.message]; }
    };
    refuse("pdf anchor on a grid", () =>
      gridFigure(CELLS[""], { anchor: %(pdf)s, unresolved: null }));
    refuse("not a picture", () => surfaces.pngSize(new Uint8Array([0, 1, 2, 3, 4, 5, 6, 7, 8])));
    out.errorWords = surfaces.messageOf(new Error("HTTP 409: these bytes are a .docx"));
    out.blobWords = surfaces.describeBlob(original);
    out.held = held.size;
    console.log(JSON.stringify(out));
    """)


#: One `/cells` response, shaped by ``DocumentCellsResponse``.
CELLS_RESPONSE = {
    "document_id": 9,
    "sheet": "Q3 Pipeline",
    "ref": "B4:D7",
    "ref_source": "used_range",
    "columns": ["B", "C", "D"],
    # Row 6 is absent, which is a row the sheet holds nothing in — the grid still paints it,
    # because the overlay's transform is linear and a grid with a row missing is not.
    "rows": [
        {"row": 4, "values": ["Region", "Q3", "Q4"]},
        {"row": 5, "values": ["North", 1200, None]},
        {"row": 7, "values": ["South", 900, 950]},
    ],
    "cells": {"C7": {"formula": "=SUM(C5:C6)", "formula_shared": False, "text_forced": False}},
    "markdown": "| | B | C | D |",
    "row_count": 3,
    "cell_count": 12,
}

#: A region larger than the grid this library will paint. `GET /documents/{id}/cells` bounds a
#: region at 50,000 cells, which for two columns is 25,000 rows — enough table to stop a
#: browser — so it paints `GRID_ROWS_MAX` of them and says which.
BIG_CELLS_RESPONSE = {
    "document_id": 11,
    "sheet": "Ledger",
    "ref": "A1:B900",
    "ref_source": "request",
    "columns": ["A", "B"],
    "rows": [{"row": 1, "values": ["opening", 0]}, {"row": 900, "values": ["closing", 41]}],
    "cells": {},
    "markdown": "",
    "row_count": 2,
    "cell_count": 1800,
}

#: Three nodes' evidence as `GET /documents/{id}/evidence` answers it: one with an anchor, one
#: with the reason there is none, one with neither. Those are three different answers and
#: `anchorPair` has to keep them apart.
EVIDENCE_ROWS = {
    "7": {"document_id": 7, "evidence": {"source_anchor": PDF_ANCHOR}},
    "8": {
        "document_id": 8,
        "evidence": {
            "source_anchor.unresolved": {
                "code": "ambiguous_match",
                "reason": "the chunk's text appears twice on page 4",
            }
        },
    },
    "10": {"document_id": 10, "evidence": {}},
    # The office node, and the reason `anchorPair` grew a second pair: `OFFICE_SPEC.md` Part 5's
    # "Two anchors, not one". The `ooxml` address cannot be wrong and cannot be drawn; the
    # rectangle is what `anchor:render` found in the PDF `render:pdf` produced, and it says so
    # with `of: "rendition"` rather than leaving a viewer to infer it from the row it came in.
    "12": {
        "document_id": 12,
        "evidence": {
            "source_anchor": {
                "kind": "ooxml",
                "part": "word/document.xml",
                "path": "/w:document/w:body/w:p[42]",
            },
            "render_anchor": {
                "kind": "pdf",
                "of": "rendition",
                "page": 3,
                "bbox": [72.0, 118.4, 540.0, 262.9],
            },
        },
    },
    "1": {
        "document_id": 1,
        "evidence": {
            "file": {"filename": "report.docx", "detected_mime": "application/vnd.ms-word"}
        },
    },
}


@pytest.fixture(scope="module")
def figures(tmp_path_factory):
    if NODE is None:
        pytest.skip("no JavaScript runtime on PATH")
    return _node(
        tmp_path_factory.mktemp("figures"),
        FIGURE_HARNESS,
        png=base64.b64encode(png_bytes(*PAGE_PIXELS)).decode(),
        cells=json.dumps({"": CELLS_RESPONSE, "A1:B900": BIG_CELLS_RESPONSE}),
        evidence=json.dumps(EVIDENCE_ROWS),
        pdf=json.dumps(PDF_ANCHOR),
    )


def test_a_page_figure_is_the_page_with_the_passage_boxed_on_it(figures):
    """The beeline's picture, built out of bytes the view fetched.

    The scale is the page's own: the PNG declares 1224x1584 and the view asked for 150 dpi, so
    the page is 587.52x760.32 points and a rectangle at 72 points from the left is 12.2549% of
    the way across — however wide the browser happens to draw it, which is why the box is
    positioned as a fraction rather than in pixels.
    """
    page = figures["figures"]["page"]
    assert page["surface"]["kind"] == "pdf-page"
    assert page["surface"]["page"] == 3
    assert page["surface"]["originCorner"] == "top-left"
    assert page["surface"]["source"] == pytest.approx({"width": 587.52, "height": 760.32})
    assert page["highlight"]["status"] == "box"

    picture = _find(page["tree"], "render-picture")[0]
    assert picture["attrs"]["src"].startswith("blob:")
    assert picture["attrs"]["width"] == "1224"
    assert "page 4" in picture["attrs"]["alt"]

    box = _find(page["tree"], "overlay-box")
    assert len(box) == 1
    assert _style(box[0]) == pytest.approx(
        {"left": 12.2549, "top": 15.5724, "width": 79.6569, "height": 19.0052}, abs=1e-4
    )
    assert _find(page["tree"], "render-caption")[0]["text"] == "Page 4, rendered at 150 dpi."


def test_the_two_anchor_rows_come_back_as_two_pairs(figures):
    """``OFFICE_SPEC.md`` Part 5's "Two anchors, not one", as the shape a page reads.

    A ``.docx`` chunk carries an exact ``ooxml`` address AND a rectangle recovered from the
    rendition, and they are two evidence rows because they are two facts with different truth
    conditions. ``anchorPair`` therefore answers with two PAIRS rather than four loose keys, and
    the nesting is not tidiness: IC-9's ``highlight()`` refuses a call carrying one key of a pair
    and not the other, so each of the two things a caller might draw has to be separately a
    legal argument. ``{anchor, unresolved}`` and ``rendition: {anchor, unresolved}`` are both
    that; four flat keys would have made ``highlight(evidence)`` mean "the source one" by
    accident of naming.
    """
    both = figures["pairs"]["both"]
    assert both["anchor"]["kind"] == "ooxml", "what the passage IS"
    assert both["unresolved"] is None
    assert both["rendition"]["anchor"]["of"] == "rendition", "where it appears on a page"
    assert both["rendition"]["unresolved"] is None

    # A node with neither still answers with both pairs and four nulls, so no caller has to ask
    # whether `evidence.rendition` exists before asking what is in it.
    assert figures["pairs"]["bare"] == {
        "anchor": None,
        "unresolved": None,
        "rendition": {"anchor": None, "unresolved": None},
    }


def test_a_rendition_page_says_whose_pagination_it_is(figures):
    """The label the whole office beeline turns on, and it is read off ``PdfAnchor.of``.

    **Authority runs opposite to drawability.** The ``ooxml`` address is computed from the
    uploaded bytes, cannot be wrong, and cannot be drawn. The rectangle is a position in a PDF
    LibreOffice produced from a document that has no pages of its own — it is the only one that
    can be drawn, and another LibreOffice may put the passage on a different page. So a caption
    reading "Page 4" under a rendition would be the appliance making the document's claim on its
    behalf, and the reader who opens the ``.docx`` in Word and finds it on page 5 would have no
    way to tell which of the two was lying.

    ``of`` and not the row it arrived in, because a parsed anchor travels away from its row: by
    the time this caption is written, the value has been through a URL fragment and two
    function calls, and the field is the only thing that still says where it came from.
    """
    drawn = figures["figures"]["page_rendition"]
    assert drawn["highlight"]["status"] == "box", "the same transform draws the same box"
    assert len(_find(drawn["tree"], "overlay-box")) == 1

    caption = _find(drawn["tree"], "render-caption")[0]["text"]
    assert "Page 4 of this appliance's rendering" in caption
    assert "no pages of its own" in caption
    assert "may paginate" in caption
    assert caption != "Page 4, rendered at 150 dpi.", "which is what a source page says"

    # The alternative text too, because a screen reader is the reader most likely to be handed
    # the picture without the caption beside it.
    assert (
        "this appliance's rendering of The proposal.docx"
        in _find(drawn["tree"], "render-picture")[0]["attrs"]["alt"]
    )


def test_a_page_with_no_box_says_which_absence_it_is(figures):
    """Three answers where a viewer might expect one, each reaching the page as itself.

    "No rectangle was recorded" and "the rectangle could not be recovered, and here is the
    reason" are different facts — ``evidence.py:453`` keeps them in different rows and the page
    keeps them in different sentences. The reason is carried through VERBATIM, because it is a
    sentence written for a human at ``citation_tasks.py:284`` and re-wording it would replace
    an explanation with a shrug. A passage on another page gets neither: it gets its page
    number, and never the starting page's rectangle drawn on this one.
    """
    unresolved = figures["figures"]["page_unresolved"]
    assert unresolved["highlight"]["status"] == "unresolved"
    assert not _find(unresolved["tree"], "overlay-box")
    notice = _find(unresolved["tree"], "overlay-notice")[0]["text"]
    assert "the chunk's text appears twice on page 4" in notice
    assert "ambiguous_match" in notice

    elsewhere = figures["figures"]["page_elsewhere"]
    assert elsewhere["highlight"] == {"status": "elsewhere", "page": 3}
    assert not _find(elsewhere["tree"], "overlay-box")
    assert "This passage is on page 4." in _find(elsewhere["tree"], "overlay-notice")[0]["text"]

    # Nothing recorded and nothing lost: a page, no box, and no sentence either, because there
    # is genuinely nothing to explain.
    none = figures["figures"]["page_none"]
    assert none["highlight"] == {"status": "none"}
    assert not _find(none["tree"], "overlay-box")
    assert not _find(none["tree"], "overlay-notice")


def test_which_page_is_decided_here_and_refused_rather_than_defaulted(figures):
    """``pageFor``, which is the one decision this side takes that the server would also take.

    ``get_document_image`` decides an omitted ``page`` the same way — 0 for a file node, the
    node's own anchor otherwise. This exists because a PNG carries no page number and
    ``BinaryPayload`` is bytes and their type, so a view that has to SAY which page it is
    showing has to work it out. The duplication is then made harmless the other way: the view
    sends the answer as ``?page=``, so the server never takes the decision and the two cannot
    disagree.

    What it must never do is fall back to page 0. A picture of the wrong page is what
    ``OFFICE_SPEC.md`` Part 5 calls worse than no picture at all, because the reader cannot
    tell — so both no-page cases refuse, and each says which one it is.
    """
    pages = figures["pages"]
    assert pages["anchor"] == 3
    assert pages["file"] == 0
    assert pages["named"] == 5

    assert pages["unresolved"][0] == "SurfaceError"
    assert "the chunk's text appears twice on page 4" in pages["unresolved"][1]
    assert pages["nothing"][0] == "SurfaceError"
    assert "no pdf anchor" in pages["nothing"][1]
    assert "worse than no picture" in pages["nothing"][1]
    assert pages["not a page"][0] == "SurfaceError"


def test_a_region_figure_is_the_rectangle_and_draws_no_box_on_itself(figures):
    """The picture of the words that matched, and the box that must not be drawn.

    The crop IS the anchor's rectangle, so a box would cover the whole picture — and it would
    cover it in the wrong PLACE, because the anchor's coordinates are the page's and this
    surface's are the crop's. The two differ by the crop's origin, ``toPixels()`` has no offset
    for that, and a box 15% to the right of where it belongs is exactly the confidently-wrong
    rectangle this contract exists to make impossible. The caption states the page and the
    rectangle in points instead.
    """
    region = figures["figures"]["region"]
    assert region["surface"]["kind"] == "image"
    assert "page" not in region["surface"]
    assert not _find(region["tree"], "overlay-box")

    caption = _find(region["tree"], "render-caption")[0]["text"]
    assert "page 4, [72, 118.4, 540, 262.9] in points" in caption
    assert "no box to draw on it" in caption


def test_a_grid_figure_paints_cells_and_places_a_cells_anchor_on_them(figures):
    """Cells, not a picture of cells, with the same overlay over them.

    ``OFFICE_SPEC.md`` Part 7: "a page image of a spreadsheet answers a different question and
    is rarely the one asked". So the surface a ``cells`` anchor lands on is a table, and it
    goes through the same ``toPixels`` a rendered page does — which is the whole of IC-9's
    claim, here over a surface whose unit is one cell rather than one point.
    """
    cells = figures["figures"]["cells"]
    grid = _find(cells["tree"], "render-sheet-grid")[0]

    # Four rows of the region plus the header of column letters, and every row is painted —
    # including row 6, which the response does not carry. A grid with a row missing is not
    # linear, and the overlay's transform is.
    assert [row["children"][0]["text"] for row in grid["children"][1:]] == ["4", "5", "6", "7"]
    assert [c["text"] for c in grid["children"][1]["children"]] == ["4", "Region", "Q3", "Q4"]
    assert [c["text"] for c in grid["children"][2]["children"]] == ["5", "North", "1200", ""]
    assert [c["text"] for c in grid["children"][3]["children"]] == ["6", "", "", ""]

    # A formula is marked and carries its own text, which is what a reader of a spreadsheet
    # actually wants to see behind a number.
    assert _find(cells["tree"], "render-sheet-formula")[0]["attrs"]["title"] == "=SUM(C5:C6)"

    caption = _find(cells["tree"], "render-sheet-caption")[0]["text"]
    assert "Q3 Pipeline" in caption and "B4:D7" in caption
    assert "measured this sheet to hold" in caption

    # The surface counts the gutter column and the header row, because they are part of the
    # grid a box is positioned in — and `cellRect` adds the same one, so a caller never sees it.
    assert cells["surface"]["source"] == {"width": 4, "height": 5}
    assert cells["surface"]["originCorner"] == "top-left"

    boxed = figures["figures"]["cells_boxed"]
    box = _find(boxed["tree"], "overlay-box")
    assert len(box) == 1
    # C5:D6 is columns 2 and 3 of four (after the gutter) and rows 2 and 3 of five (after the
    # header): left 2/4, top 2/5, width 2/4, height 2/5.
    assert _style(box[0]) == pytest.approx(
        {"left": 50.0, "top": 40.0, "width": 50.0, "height": 40.0}
    )


def test_the_grid_says_what_it_did_not_paint_and_refuses_what_it_cannot(figures):
    """Two absences that are not the same absence, and neither is a blank grid.

    A region bigger than ``GRID_ROWS_MAX`` is a VIEWPORT rather than a truncated answer: the
    response holds every row, this library paints two hundred of them, and the caption says so
    with the real numbers. A silent cut would be indistinguishable from a region that ends
    there, which is the same defect class as a missing field rendered as the word "none".

    An anchor naming a cell below the fold then answers "outside the region shown here" — true
    of this surface, and it reads correctly beside that caption. And a PDF anchor on a grid is
    refused outright, because a grid declares no page and overlay.js will not draw a page's
    rectangle on whatever surface happens to be up.
    """
    huge = figures["figures"]["cells_huge"]
    grid = _find(huge["tree"], "render-sheet-grid")[0]
    assert len(grid["children"]) == 201, "the header row plus GRID_ROWS_MAX rows"
    caption = _find(huge["tree"], "render-sheet-caption")[0]["text"]
    assert "Showing rows 1 to 200 — 200 of the region's 900 rows" in caption
    assert "The response carries all 900" in caption

    off = figures["figures"]["cells_off_region"]
    assert not _find(off["tree"], "overlay-box")
    assert "outside the region shown here" in _find(off["tree"], "overlay-notice")[0]["text"]

    wrong = figures["refusals"]["pdf anchor on a grid"]
    assert wrong[0] == "OverlayError"
    assert "must declare which 0-based page it shows" in wrong[1]


def test_the_page_says_which_representation_of_a_document_it_is_showing(figures):
    """Step 29's brief calls this a fallback; it is a statement, and here is the difference.

    A fallback in the anti-pattern sense CATCHES a failure and substitutes a result, so that a
    reader cannot tell the substitute from the real thing. Nothing here does that: no call is
    made to a route that would refuse, and no exception is recovered from. What is read is a
    named row — the ``rendition`` row step 32 writes, spelled once at
    ``RenditionRepository.EVIDENCE_NAME`` — and BOTH answers are sentences on the page,
    written before anything is drawn.

    NOT ``file.rendition_ref``. ``OFFICE_SPEC.md`` Part 6 names that key and this test asserted
    it until step 32 landed; the merge commit that changed it records why, and the short version
    is that ``INGEST_SPEC.md`` 3.3 says the ``file`` block "never changes after upload", so
    nothing may write a derivation into it. Part 6 is the document to correct.

    The markdown is not a substitute for the document either. It is the document as this
    appliance read it: the text that was chunked, embedded and indexed, and the thing a search
    hit is a hit ON.

    **The second sentence stopped being about a step number.** It said "this front end cannot
    show it yet — serving a citation image off a rendition is Block G step 33" for as long as
    that was true, which was until the browser learnt to read the ``render_anchor`` row. What it
    has to say now is the thing that stays true: a page drawn off a rendition is a page of
    LIBREOFFICE'S pagination, and the document it was made from has no pages of its own.
    """
    extracted = figures["notices"]["extracted"]["text"]
    assert "NOT a rendered page" in extracted
    assert "no `rendition` row" in extracted
    assert "render:pdf" in extracted and "JMFTS_CONVERT_URL" in extracted
    assert "report.docx, application/vnd.ms-word" in extracted

    # The other answer is a sentence too, rather than the absence of one — and it is about
    # whose pagination the reader is looking at, which is a fact with no expiry date.
    held = figures["notices"]["rendition"]["text"]
    assert "holds a rendition" in held
    assert "not of the uploaded file" in held, "whose pages these are"
    assert "paginate it differently" in held
    assert "step 33" not in held, (
        "this sentence was written in the future tense about a step that has since shipped and "
        "been consumed; a page telling a reader to wait for something they are looking at is "
        "the stale half of the defect this lane closed"
    )


def test_a_box_over_part_of_a_passage_does_not_read_like_a_box_over_all_of_it(figures):
    """The one thing `continues` was carried for, said out loud at last.

    `overlay.js` has frozen `continues` onto every BOX since step 25, with a comment saying it
    is there "so a viewer can say 'continues on p. 4' rather than presenting a partial box as a
    whole one" — and until this merge nothing said it. A box over the first page of a two-page
    passage drew identically to a box over the whole passage, which is the capability table's
    defect (absent drawn the same as complete) in the one place this sprint is about. Found by
    `wt/view-tree` while adopting this library, fixed here rather than in either worktree
    because `render/surfaces.js` is the one place that composes this wording.

    A whole passage still gets NO sentence. The box is the statement, and a second one saying
    "this is all of it" would be noise on every citation in the corpus.
    """
    assert figures["notices"]["whole"] is None
    assert figures["notices"]["partialEmpty"] is None, "absent, not empty — the anchor contract"

    one = figures["notices"]["partialOne"]
    assert "continues on page 5" in one, one
    assert "this page's part" in one

    many = figures["notices"]["partialMany"]
    assert "continues on pages 5 and 6" in many, many


def test_bytes_and_failures_are_each_drawn_as_themselves(figures):
    """Never ``JSON.stringify``, which is ``{}`` for a Blob and for every Error there is.

    A reader about to download a file is being told its size, and a reader whose page did not
    load is being told why. Both of those are objects that flatten to nothing, and a page that
    printed the flattening would reproduce the capability table's defect — absent drawn
    identically to empty — in a new place.
    """
    assert re.fullmatch(
        r"\d[\d,]* bytes of application/vnd\.openxmlformats-officedocument",
        figures["blobWords"],
    ), figures["blobWords"]
    assert figures["errorWords"] == "HTTP 409: these bytes are a .docx"

    save = figures["notices"]["download"]
    assert save["attrs"]["download"] == "report.docx"
    assert save["attrs"]["href"].startswith("blob:")
    assert re.search(r"Save report\.docx — \d[\d,]* bytes of ", save["text"]), save["text"]

    assert figures["refusals"]["not a picture"][0] == "SurfaceError"
    assert "image/png" in figures["refusals"]["not a picture"][1]
