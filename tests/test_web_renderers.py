"""The nine renderers, and the three surfaces one anchor has to land on identically.

``docs/SPRINT_0_6_0.md`` Block F step 29. ``tests/test_web_views.py`` holds the two contracts
this step is written against — IC-8's bijection between ``RENDERERS`` and the registrations,
and IC-9's overlay over synthetic surfaces — and this file holds the renderers themselves:
what they draw, what they ask the appliance for, and what they do instead of drawing when they
cannot.

Four properties are worth naming, because each is a defect this sprint was written against,
caught one layer down:

* **A renderer that cannot get its bytes says so in the failure's own words.** Not a blank
  frame, which is indistinguishable from a document with nothing in it, and never
  ``JSON.stringify(error)``, which is ``{}`` for every Error there is.
* **A picture of the wrong page is worse than no picture** (``OFFICE_SPEC.md`` Part 5), so the
  page number is decided once, on this side, and sent explicitly on every call.
* **What is withheld is said with the real numbers.** A sheet region larger than the grid this
  renderer will paint carries a sentence naming how many of its rows are on screen.
* **The office renderer names the absence before it draws.** It reads a named evidence row and
  states which representation the reader is looking at; it never calls a route that would
  refuse and substitutes the answer.

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

    /** Object URLs, and a record of which are still held, so a leak is visible to a test. */
    export function installObjectUrls() {
      const held = new Map();
      let next = 0;
      globalThis.URL = {
        createObjectURL(blob) {
          const url = `blob:jmfts/${next += 1}`;
          held.set(url, blob);
          return url;
        },
        revokeObjectURL(url) { held.delete(url); },
      };
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


# ------------------------------------------------------------------ the four that see the file

VISUAL_HARNESS = textwrap.dedent("""
    import { install, deep as serialize } from "./dom.mjs";
    import { installObjectUrls } from "./dom.mjs";
    install();
    const held = installObjectUrls();
    const { render } = await import("%(static)s/render/renderers.js");

    const PNG = Uint8Array.from(atob("%(png)s"), (c) => c.charCodeAt(0));
    const CELLS = %(cells)s;
    const EVIDENCE = %(evidence)s;
    const calls = [];
    const client = {
      get_document_evidence: async (args) => {
        calls.push(["evidence", args]);
        return { document_id: args.document_id, evidence: EVIDENCE[args.document_id] ?? {} };
      },
      get_document_image: async (args) => {
        calls.push(["image", args]);
        return new Blob([PNG], { type: "image/png" });
      },
      get_document_region: async (args) => {
        calls.push(["region", args]);
        return new Blob([PNG], { type: "image/png" });
      },
      get_document_cells: async (args) => {
        calls.push(["cells", args]);
        return CELLS[args.ref ?? ""];
      },
      get_document_blob: async (args) => {
        calls.push(["blob", args]);
        return new Blob([PNG], { type: "application/vnd.openxmlformats-officedocument" });
      },
      get_document_image_failing: null,
    };

    const presentation = (renderer, config = {}) => ({
      renderer, child_handling: "collapsed", link_handling: "hidden", renderer_config: config,
    });

    const out = { cases: {} };
    async function draw(label, content, name, doc, context, config = {}) {
      const before = calls.length;
      const element = render(content, presentation(name, config), doc, { client, ...context });
      const immediate = serialize(element);
      const ready = await element.ready;
      out.cases[label] = {
        immediate,
        tree: serialize(element),
        ready: ready.status,
        error: ready.error ? ready.error.message : null,
        value: ready.status === "drawn" ? JSON.parse(JSON.stringify(ready.value ?? null)) : null,
        calls: calls.slice(before),
        surface: element.surface ? { ...element.surface(), cellRect: undefined } : null,
      };
      return element;
    }

    const chunk = { id: 7, title: "A chunk", usetype: "chunk" };
    const file = { id: 1, title: "The report", usetype: "file" };
    const sheet = { id: 9, title: "Q3 Pipeline", usetype: "sheet" };

    await draw("page", "", "pdf-page", chunk, {});
    await draw("page_of_file", "", "pdf-page", file, {});
    await draw("page_given", "", "pdf-page", chunk, { page: 5, dpi: 200 });
    await draw("page_hit", "", "pdf-page", chunk, {
      evidence: { anchor: %(pdf)s, unresolved: null },
    });
    await draw("page_unresolved", "", "pdf-page", { id: 8, usetype: "chunk" }, {});
    await draw("page_unresolved_named", "", "pdf-page", { id: 8, usetype: "chunk" }, { page: 4 });
    await draw("page_none", "", "pdf-page", { id: 10, usetype: "chunk" }, {});
    await draw("region", "", "image", chunk, {});
    await draw("region_none", "", "image", { id: 10, usetype: "chunk" }, {});
    await draw("cells", "", "sheet-region", sheet, {});
    await draw("cells_boxed", "", "sheet-region", sheet, {
      evidence: { anchor: { kind: "cells", sheet: "Q3 Pipeline", ref: "C5:D6" }, unresolved: null },
    });
    await draw("cells_off_region", "", "sheet-region", sheet, {
      evidence: { anchor: { kind: "cells", sheet: "Q3 Pipeline", ref: "Z900" }, unresolved: null },
    });
    await draw("cells_wrong_kind", "", "sheet-region", sheet, {
      evidence: { anchor: %(pdf)s, unresolved: null },
    });
    await draw(
      "cells_huge", "", "sheet-region", { id: 11, usetype: "sheet" }, {}, { ref: "A1:B900" });
    const office = await draw("office", "# Heading\\n\\ntext", "office", file, {});
    out.cases.office.downloadBefore = calls.filter((c) => c[0] === "blob").length;
    await office.downloadOriginal();
    out.cases.office.afterDownload = serialize(office);
    out.cases.office.downloadAfter = calls.filter((c) => c[0] === "blob").length;

    // The appliance refuses, and what the reader sees is the refusal's own words.
    const broken = {
      ...client,
      get_document_image: async () => {
        const error = new Error("GET /documents/7/image - HTTP 409: these bytes are a .docx");
        error.name = "JmftsHttpError";
        throw error;
      },
    };
    const failing = render("", presentation("pdf-page"), chunk, { client: broken });
    const outcome = await failing.ready;
    out.failure = {
      tree: serialize(failing),
      status: outcome.status,
      message: outcome.error.message,
    };
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

#: A region larger than the grid this renderer will paint. `GET /documents/{id}/cells` bounds a
#: region at 50,000 cells, which for two columns is 25,000 rows — enough table to stop a
#: browser — so the renderer paints `GRID_ROWS_MAX` of them and says which.
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

#: Three nodes' evidence: one with an anchor, one with the reason there is none, one with
#: neither. Those are three different answers and the renderers have to keep them apart.
EVIDENCE_ROWS = {
    "7": {"source_anchor": PDF_ANCHOR},
    "8": {
        "source_anchor.unresolved": {
            "code": "ambiguous_match",
            "reason": "the chunk's text appears twice on page 4",
        }
    },
    "10": {},
    "1": {"file": {"filename": "report.docx", "detected_mime": "application/vnd.ms-word"}},
}


@pytest.fixture(scope="module")
def visual(tmp_path_factory):
    if NODE is None:
        pytest.skip("no JavaScript runtime on PATH")
    return _node(
        tmp_path_factory.mktemp("visual"),
        VISUAL_HARNESS,
        png=base64.b64encode(png_bytes(*PAGE_PIXELS)).decode(),
        cells=json.dumps({"": CELLS_RESPONSE, "A1:B900": BIG_CELLS_RESPONSE}),
        evidence=json.dumps(EVIDENCE_ROWS),
        pdf=json.dumps(PDF_ANCHOR),
    )


def test_the_page_renderer_decides_the_page_here_and_sends_it(visual):
    """``pdf-page``: the page, the box, and one decision about which page.

    ``get_document_image`` will decide an omitted ``page`` itself — 0 for a file node, the
    node's own anchor otherwise — and this renderer applies the same rule. That is a copy, and
    the copy is then made harmless by always sending the answer: the server never takes the
    decision, so the two cannot disagree. It is not avoidable, either. A PNG carries no page
    number and ``BinaryPayload`` is bytes and their type, so a page that has to SAY which page
    it is showing has to work it out.
    """
    page = visual["cases"]["page"]
    assert page["ready"] == "drawn"
    assert page["calls"] == [
        ["evidence", {"document_id": 7}],
        ["image", {"document_id": 7, "page": 3, "dpi": 150}],
    ]
    assert page["surface"]["kind"] == "pdf-page"
    assert page["surface"]["page"] == 3
    assert page["surface"]["source"] == pytest.approx({"width": 587.52, "height": 760.32})

    # A file node is the whole document, so its first page is page 0 — and it needs no anchor
    # to say so, which is the one case where the evidence read is pure cost.
    assert visual["cases"]["page_of_file"]["calls"][1][1]["page"] == 0

    # A caller that names a page and a density gets them, and they are what goes on the wire.
    # The evidence read still happens, because the anchor is what the BOX needs and naming a
    # page says nothing about where the passage is on it.
    assert visual["cases"]["page_given"]["calls"] == [
        ["evidence", {"document_id": 7}],
        ["image", {"document_id": 7, "page": 5, "dpi": 200}],
    ]

    # A hit already carries its anchor (IC-4), so showing where it came from costs one call
    # and not two. That is the round trip step 18's second half was written to remove.
    assert [call[0] for call in visual["cases"]["page_hit"]["calls"]] == ["image"]


def test_the_page_renderer_draws_the_box_and_says_why_when_it_cannot(visual):
    """The three answers a highlight has, each reaching the page as itself."""
    box = _find(visual["cases"]["page"]["tree"], "overlay-box")
    assert len(box) == 1
    placed = _style(box[0])
    # 150 dpi, so a point is 25/12 pixels: 72 pt from the left of a 612 pt page is 150 of 1224
    # pixels, which is 12.2549% however wide the browser draws it.
    assert placed == pytest.approx(
        {"left": 12.2549, "top": 15.5724, "width": 79.6569, "height": 19.0052}, abs=1e-4
    )

    caption = _find(visual["cases"]["page"]["tree"], "render-caption")[0]["text"]
    assert caption == "Page 4, rendered at 150 dpi."

    # "No rectangle was recorded" and "the rectangle could not be recovered, and here is the
    # reason" are different facts — `evidence.py:453` keeps them in different rows and the page
    # keeps them in different sentences. The reason is carried through VERBATIM, because it is
    # a sentence written for a human and re-wording it would replace it with a shrug.
    #
    # An unresolved anchor on a chunk takes the PAGE with it: the same row that would have said
    # where on the page the passage is, is the row that says which page. So there is no picture
    # to show and the reason is what the reader gets — this is the one place the recorded
    # reason does duty as a render failure, and it says which failure it is.
    unresolved = visual["cases"]["page_unresolved"]
    assert unresolved["ready"] == "failed"
    assert "the chunk's text appears twice on page 4" in unresolved["error"]
    assert "ambiguous_match" in unresolved["error"]
    assert not _find(unresolved["tree"], "overlay-box")

    # Name the page and there IS a picture, with the same reason beside it saying why nothing
    # is boxed on it. A rendered page and an unrecoverable rectangle are not the same absence.
    named = visual["cases"]["page_unresolved_named"]
    assert named["ready"] == "drawn"
    assert named["calls"][-1] == ["image", {"document_id": 8, "page": 4, "dpi": 150}]
    assert not _find(named["tree"], "overlay-box")
    notice = _find(named["tree"], "overlay-notice")[0]["text"]
    assert "the chunk's text appears twice on page 4" in notice
    assert "ambiguous_match" in notice

    # A chunk with no anchor and no reason cannot name a page at all, and a picture of the
    # wrong page is what OFFICE_SPEC.md Part 5 calls worse than no picture. So it refuses, and
    # the refusal is on the page rather than a blank frame.
    none = visual["cases"]["page_none"]
    assert none["ready"] == "failed"
    assert "no pdf anchor" in none["error"]
    assert _find(none["tree"], "bad render-status render-status-failed")[0]["text"] == none["error"]


def test_the_region_renderer_crops_the_rectangle_and_draws_no_box_on_it(visual):
    """``image``: the picture of the words that matched, and the box that must not be drawn.

    The crop IS the anchor's rectangle, so a box would cover the whole picture — and it would
    cover it in the wrong PLACE, because the anchor's coordinates are the page's and this
    surface's are the crop's. The two differ by the crop's origin, ``toPixels()`` has no offset
    for that, and a box 15% to the right of where it belongs is exactly the confidently-wrong
    rectangle this contract exists to make impossible. So the caption states the page and the
    rectangle in points instead.
    """
    region = visual["cases"]["region"]
    assert region["ready"] == "drawn"
    assert region["calls"] == [
        ["evidence", {"document_id": 7}],
        ["region", {"document_id": 7, "anchor": True, "dpi": 150}],
    ]
    assert not _find(region["tree"], "overlay-box")
    caption = _find(region["tree"], "render-caption")[0]["text"]
    assert "page 4, [72, 118.4, 540, 262.9] in points" in caption
    assert "no box to draw on it" in caption

    # The surface is real even though nothing is drawn on it: it is the space a query-time
    # `search_for` within this node would produce coordinates in, which the sprint plan defers
    # by name rather than leaves out by accident.
    assert region["surface"]["kind"] == "image"
    assert "page" not in region["surface"]

    # And a node with no rectangle at all is a refusal, not an empty picture.
    assert visual["cases"]["region_none"]["ready"] == "failed"
    assert "no source_anchor row" in visual["cases"]["region_none"]["error"]


def test_the_sheet_renderer_paints_cells_and_places_a_cells_anchor_on_them(visual):
    """``sheet-region``: cells, not a picture of cells, with the same overlay over them."""
    cells = visual["cases"]["cells"]
    assert cells["ready"] == "drawn"
    assert cells["calls"] == [["cells", {"document_id": 9}]], (
        "omitting `ref` asks for the node's own region, and which region that is is the "
        "server's decision (`_cells_bounds`); this renderer prints the answer rather than "
        "reproducing the rule"
    )

    grid = _find(cells["tree"], "render-sheet-grid")[0]
    # Four rows of the region plus the header of column letters, and every row is painted —
    # including row 6, which the response does not carry. A grid with a row missing is not
    # linear, and the overlay's transform is.
    assert [row["children"][0]["text"] for row in grid["children"][1:]] == ["4", "5", "6", "7"]
    assert [cell["text"] for cell in grid["children"][1]["children"]] == ["4", "Region", "Q3", "Q4"]
    assert [cell["text"] for cell in grid["children"][2]["children"]] == ["5", "North", "1200", ""]
    assert [cell["text"] for cell in grid["children"][3]["children"]] == ["6", "", "", ""]

    # A formula is marked and carries its own text, which is the thing a reader of a
    # spreadsheet actually wants to see behind a number.
    formula = _find(cells["tree"], "render-sheet-formula")[0]
    assert formula["attrs"]["title"] == "=SUM(C5:C6)"

    caption = _find(cells["tree"], "render-sheet-caption")[0]["text"]
    assert "Q3 Pipeline" in caption and "B4:D7" in caption
    assert "measured this sheet to hold" in caption

    # The surface counts the gutter column and the header row, because they are part of the
    # grid a box is positioned in — and `cellRect` adds the same one, so a caller never sees it.
    assert visual["cases"]["cells"]["surface"]["source"] == {"width": 4, "height": 5}
    boxed = visual["cases"]["cells_boxed"]
    box = _find(boxed["tree"], "overlay-box")
    assert len(box) == 1
    placed = _style(box[0])
    # C5:D6 is columns 2 and 3 of four (after the gutter) and rows 2 and 3 of five (after the
    # header): left 2/4, top 2/5, width 2/4, height 2/5.
    assert placed == pytest.approx({"left": 50.0, "top": 40.0, "width": 50.0, "height": 40.0})


def test_the_sheet_renderer_says_what_it_did_not_paint_and_refuses_what_it_cannot(visual):
    """Two absences that are not the same absence, and neither is a blank grid.

    A region bigger than ``GRID_ROWS_MAX`` is a VIEWPORT rather than a truncated answer: the
    response holds every row, this renderer paints two hundred of them, and the caption says so
    with the real numbers. A silent cut would be indistinguishable from a region that ends
    there, which is the same defect class as a missing field rendered as the word "none".

    An anchor naming a cell below the fold then answers "outside the region shown here" — which
    is true of this surface and reads correctly beside that caption. And a PDF anchor on a grid
    is refused outright, because a grid declares no page and overlay.js will not draw a page's
    rectangle on whatever surface happens to be up.
    """
    huge = visual["cases"]["cells_huge"]
    assert huge["ready"] == "drawn"
    grid = _find(huge["tree"], "render-sheet-grid")[0]
    assert len(grid["children"]) == 201, "the header row plus GRID_ROWS_MAX rows"
    caption = _find(huge["tree"], "render-sheet-caption")[0]["text"]
    assert "Showing rows 1 to 200 — 200 of the region's 900 rows" in caption
    assert "The response carries all 900" in caption

    off = visual["cases"]["cells_off_region"]
    assert off["ready"] == "drawn"
    assert not _find(off["tree"], "overlay-box")
    assert "outside the region shown here" in _find(off["tree"], "overlay-notice")[0]["text"]

    wrong = visual["cases"]["cells_wrong_kind"]
    assert wrong["ready"] == "failed"
    assert "must declare which 0-based page it shows" in wrong["error"]


def test_the_office_renderer_names_the_absence_before_it_draws(visual):
    """The question step 29's brief asks out loud, answered in the markup.

    A fallback in the anti-pattern sense catches a failure and substitutes a result, so that a
    caller cannot tell the substitute from the real thing. This renderer does not do that, and
    the structure is the argument: it never calls ``/image`` on a ``.docx`` and handles the
    409. ``NotAPdfSource`` is a real answer about a real deployment, and swallowing it is the
    failure ``document_service.py`` names at that class — "a renderer that quietly extracted
    markdown instead would be the failure step 29 is written to avoid, one route earlier".

    What it does instead is ask which representations exist and say which one is on screen.
    The markdown is not a substitute for the document: it is the document as this appliance
    read it — the text that was chunked, embedded and indexed, and the thing a search hit is a
    hit ON. The page a rendition would give does not exist, because nothing in this tree writes
    one, and the notice says that with the name of the step that will.
    """
    office = visual["cases"]["office"]
    assert office["ready"] == "drawn"
    assert [call[0] for call in office["calls"]] == ["evidence"], (
        "the office renderer must not call a route it knows will refuse and then recover from "
        "the refusal; it reads the evidence and says what it found"
    )
    assert office["value"] == {"rendition": False, "filename": "report.docx"}

    notice = _find(office["tree"], "render-notice")[0]["text"]
    assert "NOT a rendered page" in notice
    assert "file.rendition_ref" in notice
    assert "render:pdf" in notice
    assert "Block G step 32" in notice

    # The markdown itself is rendered by the same renderer the `markdown` value gets, so an
    # office document's headings are headings.
    assert _find(office["tree"], "render-md-heading")[0]["text"] == "Heading"
    assert "report.docx, application/vnd.ms-word" in office["tree"]["text"]


def test_the_original_bytes_are_fetched_when_they_are_asked_for(visual):
    """``/blob`` is the whole upload, so the page offers it rather than pulling it down.

    An office document is megabytes and most of them are never downloaded. The control is a
    named function on the figure as well as a click handler, which is what lets a test drive it
    without a browser — and what makes "it did not fetch until it was asked" checkable rather
    than asserted.
    """
    office = visual["cases"]["office"]
    assert office["downloadBefore"] == 0
    assert office["downloadAfter"] == 1

    ready = _find(office["afterDownload"], "render-download-ready")[0]
    assert ready["attrs"]["download"] == "report.docx"
    assert ready["attrs"]["href"].startswith("blob:")
    # A Blob drawn as itself. `JSON.stringify(blob)` is `{}`, which would render the size of
    # the file a reader is about to download as nothing at all.
    assert re.search(r"\d[\d,]* bytes of application/vnd\.", ready["text"]), ready["text"]


def test_a_renderer_that_cannot_get_its_bytes_puts_the_failure_on_the_page(visual):
    """Never a blank frame, and never ``{}``.

    A renderer that drew nothing on an error would be indistinguishable from a document with
    nothing in it, which is this sprint's theme one layer down — "a check that is absent, and
    an absence that looks like an answer". The failure's own message goes on the page, and
    ``ready`` RESOLVES rather than rejecting: the reader can already see what happened, so a
    rejection would be a second channel for the same fact and an unhandled rejection in a view
    that did not await.
    """
    failure = visual["failure"]
    assert failure["status"] == "failed"
    assert "HTTP 409" in failure["message"] and ".docx" in failure["message"]
    shown = _find(failure["tree"], "bad render-status render-status-failed")
    assert len(shown) == 1
    assert shown[0]["text"] == failure["message"]
    assert not _find(failure["tree"], "overlay-box")

    # Every object URL this harness made was revoked on `load`, except the ones whose picture
    # never loaded — the stub fires no events, so this is the count of pictures, not a leak.
    assert visual["held"] >= 1
