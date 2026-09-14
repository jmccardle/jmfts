// IC-8: a renderer is `(content, presentation, document) -> element`, keyed by RENDERERS.
//
// docs/SPRINT_0_6_0.md Block F step 25. The dispatch key is a value the DATABASE hands the
// page: `usetype_presentations.renderer`, reaching the browser as `ViewResponse.presentation
// .renderer` from `GET /view/{document_id}`. The sprint plan's argument for this is that
// adding a renderer becomes "a tuple edit plus a migration that every client picks up — not a
// branch in JavaScript that only this front end has".
//
// SO THIS FILE DOES NOT RESTATE THE TUPLE. `RENDERERS` lives at
// `jmfts_core/models/usetype_presentation.py:14` and a copy of it here would be the second
// list that IC-8 exists to prevent. What is here is one `registerRenderer` call per value;
// `tests/test_web_views.py` reads the tuple out of Python and asserts a bijection against what
// this module registered, in BOTH directions — a value with no renderer fails, and a renderer
// for a value the tuple does not carry fails too.
//
// WHAT IS AND IS NOT IMPLEMENTED HERE. Step 25 was the interface; step 29 wrote the renderers,
// and every value of `RENDERERS` now has one that renders what its name says. `showing` stays,
// and it is now null on all nine: a registration declares null when the renderer renders what
// its name says, and otherwise a sentence naming what the reader is actually looking at, which
// `render()` puts on the page itself. Step 25's argument for the field was that a step-29
// author "cannot finish `markdown` and forget to take the notice down, because the notice is
// not theirs to take down" — that worked, and the field is what the NEXT unfinished renderer
// declares rather than a field with no live use.
//
// THE TUPLE GREW BY FOUR, AND THAT IS A CONTRACT CHANGE. `RENDERERS` was five values;
// `pdf-page`, `image`, `sheet-region` and `office` are step 29's, so the tuple in
// `jmfts_core/models/usetype_presentation.py` is nine and `sql/migrations/023_sheet_region_
// presentation.sql` is the delta that carries it to a database that already exists. Step 25
// named that cost exactly — "adding a renderer is a tuple edit plus a migration that every
// client picks up" — and it is paid there rather than by a branch in JavaScript.
//
// FOUR OF THE NINE ASK THE APPLIANCE FOR SOMETHING, AND THEY SAY SO. A page image, a cropped
// rectangle and a grid of cells are not in `rendered_content` and cannot be: they are bytes
// behind `/documents/{id}/image`, `/region`, `/cells` and `/blob`. So a registration declares
// `needs`, `render()` refuses to dispatch to a renderer whose needs the caller did not supply,
// and the fourth argument — the CONTEXT — is where they are supplied. A view already holds
// one: the object IC-7 hands its component, `{client, params, navigate}`, has `client` on it,
// so a view passes the context it was given and nothing new has to be built.
//
// THOSE FOUR RETURN THEIR ELEMENT BEFORE THE BYTES ARRIVE, with `element.ready` on it — the
// promise of the work still running. IC-8 fixes the signature as `-> element` and `render()`
// enforces it, so a renderer cannot be asynchronous in its return value; what it can do is
// hand back the frame immediately and fill it in. A caller that wants to wait — every test
// here does — awaits `element.ready`. A caller that does not gets a page that says what it is
// loading and then shows it, and a FAILURE reaches the page as the failure's own message
// rather than as a blank frame.

import { renderMarkdown } from "./markdown.js";
import { renderTranscript } from "./transcript.js";
import { renderCells } from "./sheet.js";
import {
  DPI_DEFAULT,
  drawHighlight,
  highlightNotice,
  pictureFrom,
  rasterSurface,
} from "./surfaces.js";

/** A renderer was asked for that nothing registered. */
export class UnknownRendererError extends Error {
  constructor(renderer, known) {
    super(
      `no renderer for ${JSON.stringify(renderer)}. This page knows: ${known.join(", ")}. ` +
        "A renderer named by the database and absent here means this front end is older than " +
        "the appliance — the tuple in jmfts_core/models/usetype_presentation.py grew."
    );
    this.name = "UnknownRendererError";
    this.renderer = renderer;
  }
}

/** A renderer was handed content it cannot render, and says so rather than rendering blank. */
export class RenderError extends Error {
  constructor(message) {
    super(message);
    this.name = "RenderError";
  }
}

const RENDERERS = new Map();

/**
 * Declare the renderer for one value of the server's `RENDERERS` tuple.
 *
 * @param {string} name the value as the database stores it, e.g. "json-table"
 * @param {{showing: string|null, needs: string[], render: (content: string,
 *          presentation: object, document: object, context: object) => Element}} renderer
 */
export function registerRenderer(name, renderer) {
  if (RENDERERS.has(name)) {
    throw new RenderError(`two renderers registered for "${name}"`);
  }
  if (typeof renderer?.render !== "function") {
    throw new RenderError(`renderer "${name}" has no render function`);
  }
  if (!("showing" in renderer)) {
    throw new RenderError(
      `renderer "${name}" must declare \`showing\`: null when it renders what its name says, ` +
        "or a sentence naming what the reader is actually looking at. There is no default, " +
        "because the default would be the wrong one for whichever renderer forgot."
    );
  }
  if (!Array.isArray(renderer.needs)) {
    // Required, not defaulted to the empty list, for `showing`'s reason one field up: five of
    // the nine need nothing and four cannot work without a client, and a renderer that forgot
    // to say which it is would reach `context.client.get_document_image` and fail with
    // "cannot read properties of undefined" — a message that names neither the renderer, the
    // missing thing, nor the caller who has to supply it.
    throw new RenderError(
      `renderer "${name}" must declare \`needs\`: the context keys it cannot render without, ` +
        "or [] when it renders from its content alone."
    );
  }
  const needs = Object.freeze([...renderer.needs]);
  RENDERERS.set(name, Object.freeze({ name, ...renderer, needs }));
}

/** Every registered renderer name, in registration order. */
export function rendererNames() {
  return Object.freeze([...RENDERERS.keys()]);
}

/** One registration, or null. */
export function rendererFor(name) {
  return RENDERERS.get(name) ?? null;
}

/**
 * IC-8. Render one document's content through the renderer its presentation names.
 *
 * @param {string} content `ViewResponse.rendered_content`
 * @param {{renderer: string, child_handling: string, link_handling: string,
 *          renderer_config: object}} presentation `ViewResponse.presentation`
 * @param {object} document the `ViewResponse` itself — id, title, usetype, the link and child
 *   collections. Passed whole because a renderer that needs the node's usetype to choose a
 *   syntax should read it rather than be told it.
 * @param {{client?: object, evidence?: {anchor: object|null, unresolved: object|null},
 *          page?: number, dpi?: number, ref?: string}} [context] what a renderer may ASK with.
 *   A view passes the object IC-7 handed its component: it already carries `client`. The
 *   optional rest are what a caller knows that the node does not say — a search hit's anchor
 *   (IC-4, which saves this renderer a call), a page or a region the reader chose.
 * @returns {Element}
 */
export function render(content, presentation, document_, context = {}) {
  const name = presentation?.renderer;
  const registered = RENDERERS.get(name);
  if (!registered) throw new UnknownRendererError(name, [...RENDERERS.keys()]);

  const missing = registered.needs.filter((key) => !context || context[key] === undefined);
  if (missing.length) {
    throw new RenderError(
      `the "${name}" renderer needs ${missing.join(", ")} and this call supplied ` +
        `{${Object.keys(context || {}).join(", ") || "nothing"}}. It reads the appliance for ` +
        "what it draws — a page image, a cropped rectangle, a grid of cells — and none of " +
        "that is in `rendered_content`. Pass the context your view was handed: " +
        "`render(content, presentation, document, context)`."
    );
  }

  const element = registered.render(content, presentation, document_, context);
  if (!(element instanceof Element)) {
    throw new RenderError(`renderer "${name}" returned ${typeof element}, not an element`);
  }
  if (registered.showing === null) return element;

  // The renderer is not finished, so what it produced is wrapped in something that says so.
  // A reader comparing two documents side by side sees why one looks different from the other
  // instead of assuming this one was written that way.
  const wrapper = document.createElement("div");
  wrapper.className = "render-partial";
  const notice = document.createElement("p");
  notice.className = "render-notice";
  notice.textContent = registered.showing;
  wrapper.append(notice, element);
  return wrapper;
}

// ------------------------------------------------------------------------------- plumbing

const element = (tag, className) => {
  const made = document.createElement(tag);
  if (className) made.className = className;
  return made;
};

const text = (tag, className, value) => {
  const made = element(tag, className);
  // textContent, never innerHTML: `rendered_content` is document text and this front end is
  // not the place a stored `<script>` gets its chance.
  made.textContent = value;
  return made;
};

const pre = (value, className) => {
  const block = element("pre", className);
  block.textContent = value;
  return block;
};

const title = (document_) => document_?.title || `document ${document_?.id}`;

/** The failure's own words. Never `JSON.stringify(error)`, which is `{}` for every Error. */
const messageOf = (error) => (error instanceof Error ? error.message : String(error));

/** A size a reader can weigh, so a Blob is reported as bytes and a type rather than as `{}`. */
const describeBlob = (blob) =>
  `${blob.size.toLocaleString()} bytes of ${blob.type || "an undeclared type"}`;

/**
 * The frame of a renderer that has to ask the appliance for what it draws.
 *
 * Returns synchronously — IC-8 says a renderer returns an element and `render()` enforces it —
 * with `element.ready` carrying the rest. What is on the page in the meantime is a line naming
 * what is being fetched, and what is on the page if it fails is the failure's own message.
 * Never a blank frame: a renderer that drew nothing on an error would be indistinguishable
 * from a document that has nothing in it, which is the defect this whole sprint is written
 * against, one layer up.
 *
 * `ready` NEVER REJECTS. It resolves to `{status: "drawn", value}` or `{status: "failed",
 * error}`. The failure is already on the page, so rejecting would be a second channel for a
 * fact the reader can see — and an unhandled rejection in a view that did not await is noise
 * that names nothing. A caller that needs to branch reads `status`, which is the same tagged
 * shape `highlight()` answers in.
 */
function deferred(className, asking, work) {
  const figure = element("figure", className);
  const status = text("p", "dim render-status", `Asking the appliance for ${asking}...`);
  figure.append(status);
  figure.ready = Promise.resolve()
    .then(() => work(figure))
    .then(
      (value) => {
        status.hidden = true;
        status.className = "dim render-status render-status-done";
        return { status: "drawn", value };
      },
      (error) => {
        status.className = "bad render-status render-status-failed";
        status.textContent = messageOf(error);
        return { status: "failed", error };
      }
    );
  return figure;
}

/** What goes under a picture: what it is, and what the overlay had to say about it. */
function caption(figure, what, notice) {
  figure.append(text("figcaption", "render-caption", what));
  if (notice) figure.append(text("p", "overlay-notice", notice));
}

/** The density every raster call is made at. See `surfaces.DPI_DEFAULT` for why it is sent. */
function density(presentation, context) {
  const named = presentation?.renderer_config?.dpi ?? context.dpi ?? DPI_DEFAULT;
  if (!Number.isInteger(named) || named < 1) {
    throw new RenderError(
      `dpi ${JSON.stringify(named)} is not a density. jmfts_core/rendering.py bounds it at ` +
        "both ends and refuses rather than clamping, so a nonsense value here would come " +
        "back as a 400 with the same sentence."
    );
  }
  return named;
}

/**
 * The anchor pair for a node: the caller's, or the node's own evidence rows.
 *
 * BOTH KEYS ALWAYS, which is `highlight()`'s rule and `evidence.py:453`'s reason — "no
 * highlight" and "the rectangle could not be recovered, and here is why" are different facts.
 * A caller that already has them passes `context.evidence`: a search hit carries
 * `source_anchor` and `source_anchor_unresolved` (IC-4) precisely so that showing where a hit
 * came from does not cost a second round trip per hit.
 *
 * A row that is ABSENT and a row written with a null value both come back as null, and here
 * that is one fact rather than two: `source_anchor` written-and-null says the anchor task ran
 * and placed nothing, which is the same "there is no rectangle" the caller has to draw.
 */
async function anchorOf(client, document_, context) {
  if (context.evidence !== undefined) {
    const given = context.evidence;
    if (given === null || !("anchor" in given) || !("unresolved" in given)) {
      throw new RenderError(
        "context.evidence is {anchor, unresolved}, both keys present even when both are " +
          `null; got ${given === null ? "null" : `{${Object.keys(given).join(", ")}}`}. A ` +
          'caller that passes one has not said whether it looked for the other, and "I did ' +
          'not look" is not "there was no reason recorded".'
      );
    }
    return given;
  }
  const found = await client.get_document_evidence({ document_id: document_.id });
  const rows = found?.evidence ?? {};
  return {
    anchor: rows["source_anchor"] ?? null,
    unresolved: rows["source_anchor.unresolved"] ?? null,
  };
}

/**
 * Which page this renderer is showing.
 *
 * The same rule `get_document_image` applies to an omitted `page` — a file node is the whole
 * document so its first page is page 0, and anything else names its page through its own
 * anchor. It is here as well as there because a PNG does not carry a page number and
 * `BinaryPayload` is bytes and their type, so a page that has to SAY which page it is showing
 * has to decide. The duplication is then removed the other way: this decision is sent
 * explicitly on every call, so the server never takes it and the two cannot disagree.
 */
function pageOf(document_, evidence, presentation, context) {
  const named = presentation?.renderer_config?.page ?? context.page;
  if (named !== undefined) {
    if (!Number.isInteger(named) || named < 0) {
      throw new RenderError(`page ${JSON.stringify(named)} is not a 0-based page number`);
    }
    return named;
  }
  // `USETYPE_FILE` (`jmfts_core/models/document.py:48`), and this is the browser's copy of it
  // rather than a third spelling: `ViewResponse.usetype` carries the string and nothing serves
  // the constant. There is no route that answers "what are the usetypes", so the alternative
  // is not a lookup, it is not knowing.
  if (document_?.usetype === "file") return 0;
  if (evidence.anchor?.kind === "pdf") return evidence.anchor.page;
  if (evidence.unresolved !== null) {
    throw new RenderError(
      "This node has no page recorded, and the reason was: " +
        `${evidence.unresolved.reason} (${evidence.unresolved.code}). Name a page to render ` +
        "one anyway."
    );
  }
  throw new RenderError(
    `Document ${document_?.id} is not a file node and carries no pdf anchor, so nothing says ` +
      "which page it came from. A picture of the wrong page is worse than no picture " +
      "(OFFICE_SPEC.md Part 5), so name one with `page` rather than being shown page 0."
  );
}

/** One anchor in the words a caption uses. */
function describe(anchor) {
  if (anchor.kind === "pdf") {
    const box = anchor.bbox.map((value) => Math.round(value * 10) / 10).join(", ");
    return `page ${anchor.page + 1}, [${box}] in points`;
  }
  if (anchor.kind === "cells") return `${anchor.sheet}!${anchor.ref}`;
  return `characters ${anchor.char_start} to ${anchor.char_end}`;
}

/**
 * "Download the original", which fetches when it is asked to and not before.
 *
 * `GET /documents/{id}/blob` serves the whole upload, and an office document is megabytes; a
 * page that pulled every one of them down in order to offer a link would spend the bandwidth
 * of the documents nobody clicks. The fetch is a named function on the figure as well as a
 * click handler, so it can be driven without a browser.
 */
function downloadControl(figure, client, document_, file) {
  const holder = element("p", "render-download");
  const button = text("button", "render-download-ask", `Download ${file.filename}`);
  holder.append(button);
  figure.downloadOriginal = async () => {
    const blob = await client.get_document_blob({ document_id: document_.id });
    const link = text(
      "a",
      "render-download-ready",
      `Save ${file.filename} — ${describeBlob(blob)}`
    );
    link.setAttribute("href", URL.createObjectURL(blob));
    link.setAttribute("download", file.filename);
    button.hidden = true;
    holder.append(link);
    return link;
  };
  button.addEventListener("click", () => figure.downloadOriginal());
  return holder;
}

// --------------------------------------------------------------------- the five that exist

registerRenderer("plain", {
  showing: null,
  needs: [],
  // Complete. Preformatted text with the whitespace the document has is the whole of what
  // "plain" claims, and there is no step-29 version of it that does more.
  render: (content) => pre(content, "render-plain"),
});

registerRenderer("code", {
  showing: null,
  needs: [],
  // Complete, and syntax highlighting is not a missing half of it. Highlighting means a
  // library, a library means a CDN or a build step, and the bundle has neither by design
  // (`jmfts_web/__init__.py`, and `test_nothing_in_the_bundle_loads_from_the_network`). The
  // language goes on the class the way every highlighter expects to find it, so one can be
  // added later without this renderer changing.
  render: (content, presentation) => {
    const language = presentation?.renderer_config?.language;
    const block = pre("", "render-code");
    const code = document.createElement("code");
    if (typeof language === "string" && language) code.className = `language-${language}`;
    code.textContent = content;
    block.append(code);
    return block;
  },
});

registerRenderer("json-table", {
  showing: null,
  needs: [],
  // Complete for the two shapes JSON content can have that are tables: a list of objects, and
  // one object. Content that is not JSON RAISES rather than rendering an empty table — a
  // usetype whose presentation says json-table and whose content is prose is a data fault
  // somebody has to see, and an empty table is how it would stay invisible.
  render: (content) => {
    let parsed;
    try {
      parsed = JSON.parse(content);
    } catch (error) {
      throw new RenderError(
        `the json-table renderer was given content that is not JSON: ${error.message}. The ` +
          "usetype's presentation row names json-table; either the content or the row is wrong."
      );
    }
    const rows = Array.isArray(parsed) ? parsed : [parsed];
    if (rows.length === 0) {
      const empty = document.createElement("p");
      empty.className = "dim";
      empty.textContent = "no rows";
      return empty;
    }
    const columns = [];
    for (const row of rows) {
      if (row === null || typeof row !== "object" || Array.isArray(row)) {
        throw new RenderError(
          "the json-table renderer needs objects to make rows out of; this content is a " +
            `list of ${Array.isArray(row) ? "lists" : typeof row}.`
        );
      }
      for (const key of Object.keys(row)) if (!columns.includes(key)) columns.push(key);
    }
    // Built with `createElement` and `append` alone, never `insertRow`/`insertCell`. The two
    // table-specific APIs are the only ones in this module a stubbed DOM would have to
    // implement beyond the four the rest of the bundle uses, and the test harness in
    // `tests/test_web_views.py` runs these renderers under node, which has no DOM at all.
    const table = document.createElement("table");
    table.className = "render-json-table";
    const head = document.createElement("tr");
    for (const column of columns) {
      const cell = document.createElement("th");
      cell.textContent = column;
      head.append(cell);
    }
    table.append(head);
    for (const row of rows) {
      const line = document.createElement("tr");
      for (const column of columns) {
        const cell = document.createElement("td");
        // A column this row does not carry and a column whose value is null are different
        // facts, and the table says which. This is the same distinction part 1 of step 24 was
        // fixing in the capability table, in the one other place a page shows a field set.
        if (!(column in row)) {
          cell.className = "dim";
          cell.textContent = "—";
        } else {
          const value = row[column];
          // A nested object or list is printed as JSON rather than as "[object Object]",
          // which is what `String()` would make of it.
          cell.textContent = value !== null && typeof value === "object"
            ? JSON.stringify(value)
            : String(value);
        }
        line.append(cell);
      }
      table.append(line);
    }
    return table;
  },
});

registerRenderer("markdown", {
  showing: null,
  needs: [],
  // Step 25 shipped this as the SOURCE with a notice, and refused to ship half a renderer:
  // "One that handled headings and paragraphs and dropped tables, links and code fences would
  // render most documents plausibly and a few wrongly, and the wrong ones would look like
  // documents that simply had no tables in them." `render/markdown.js` is the whole one, and
  // its header lists what it covers and what it does not — the four it does not (reference
  // links, footnotes, HTML, task lists) render as the literal text they are, which is visibly
  // unrendered rather than invisibly missing.
  render: (content) => renderMarkdown(content),
});

registerRenderer("transcript", {
  showing: null,
  needs: [],
  // Step 25's reason for holding this back was "nothing in this tree states that shape", and
  // that was one grep from being false: `conversation_ingest.conversation_markdown` (`:164`)
  // writes `[role]: content` blocks and is the only writer. `render/transcript.js` reads the
  // shape off that function and says so at the top, so the guess step 25 refused to make is
  // not made here either — it is looked up.
  render: (content) => renderTranscript(content),
});

// --------------------------------------------------------------- the four that see the file
//
// Each one hands back its frame at once and fills it in from `element.ready`. The shape is the
// same in all four and it is `deferred()` below: a caption that says what is being asked for,
// the frame, and — when the call fails — the failure's OWN message on the page. Never a blank
// frame, and never `JSON.stringify(error)`, which is `{}` for every Error there is.

registerRenderer("pdf-page", {
  showing: null,
  needs: ["client"],
  // The page a node came from, whole, with the passage boxed on it. This is the beeline's
  // renderer — "click a result, see the page and the rectangle the answer came from" — and the
  // box is drawn by `render/overlay.js`, client-side, over the same anchor IC-4 puts on the
  // hit. `/image` has no `highlight` parameter on purpose; `get_document_image`'s docstring
  // gives the three reasons and the third is this contract.
  render: (content, presentation, document_, context) =>
    deferred("render-page", `page of ${title(document_)}`, async (figure) => {
      const dpi = density(presentation, context);
      const evidence = await anchorOf(context.client, document_, context);
      const page = pageOf(document_, evidence, presentation, context);
      const blob = await context.client.get_document_image({
        document_id: document_.id,
        // ALWAYS sent, never left to the server's default. `get_document_image` decides an
        // omitted page the same way `pageOf` does — page 0 for a file node, the node's own
        // anchor otherwise — and two copies of one decision is one copy too many. The page
        // that has to SAY which page it is showing is the page that decides.
        page,
        dpi,
      });
      const { image, pixels } = await pictureFrom(blob, {
        alt: `page ${page + 1} of ${title(document_)}, rendered at ${dpi} dpi`,
      });
      const surface = rasterSurface({ kind: "pdf-page", pixels, dpi, page });
      const positioned = element("div", "overlay-surface");
      positioned.append(image);
      figure.append(positioned);
      figure.surface = () => surface;
      figure.overlayContainer = positioned;
      const found = drawHighlight(positioned, evidence, surface);
      caption(figure, `Page ${page + 1}, rendered at ${dpi} dpi.`, highlightNotice(found));
      return found;
    }),
});

registerRenderer("image", {
  showing: null,
  needs: ["client"],
  // The picture of the words that matched: this node's OWN rectangle, cropped out of the page.
  // `/region?anchor=true` is the form a client has straight after a search hit, with no
  // coordinates to carry.
  //
  // NO BOX IS DRAWN ON IT, and that is the one thing about this renderer worth reading twice.
  // The crop IS the anchor's rectangle, so a box would cover the whole picture — and it would
  // cover it in the WRONG PLACE, because the anchor's coordinates are the page's and this
  // surface's are the crop's. The two differ by the crop's origin, `toPixels()` has no offset
  // and must not grow one behind three lanes' backs, and a box drawn 15% to the right of where
  // it belongs is exactly the confidently-wrong rectangle Part 5 calls worse than none. So the
  // caption states the page and the rectangle in points instead, and the surface this renderer
  // exposes is there for coordinates in the CROP's own space — which is what a query-time
  // `search_for` within a node would produce, and which the sprint plan defers by name
  // ("a text span within a node's content... simply not in this sprint").
  render: (content, presentation, document_, context) =>
    deferred("render-region", `the rectangle recorded for ${title(document_)}`, async (figure) => {
      const dpi = density(presentation, context);
      const evidence = await anchorOf(context.client, document_, context);
      if (evidence.anchor === null) {
        throw new RenderError(
          evidence.unresolved === null
            ? `Document ${document_.id} carries no source_anchor row, so nothing recorded a ` +
              "rectangle for it and there is no region to crop. The page is " +
              "`pdf-page`; a rectangle needs an anchor."
            : "No rectangle could be recovered for this passage, and the reason was recorded: " +
              `${evidence.unresolved.reason} (${evidence.unresolved.code})`
        );
      }
      const blob = await context.client.get_document_region({
        document_id: document_.id,
        anchor: true,
        dpi,
      });
      const { image, pixels } = await pictureFrom(blob, {
        alt: `the region ${title(document_)} was extracted from, rendered at ${dpi} dpi`,
      });
      const positioned = element("div", "overlay-surface");
      positioned.append(image);
      figure.append(positioned);
      const surface = rasterSurface({ kind: "image", pixels, dpi });
      figure.surface = () => surface;
      figure.overlayContainer = positioned;
      caption(
        figure,
        `This picture IS the rectangle recorded for this passage — ${describe(evidence.anchor)} ` +
          `— cropped out of the page at ${dpi} dpi, so there is no box to draw on it.`,
        null
      );
      return surface;
    }),
});

registerRenderer("sheet-region", {
  showing: null,
  needs: ["client"],
  // Cells, not a picture of cells. `render/sheet.js` builds the grid and the surface; this
  // registration is the part that talks to the appliance. `ref` comes from the caller when the
  // caller named one and is otherwise omitted, which asks `/cells` for the node's own region —
  // its `cells` anchor if it has one, its measured used range if it does not, with `ref_source`
  // saying which. That decision is the server's (`_cells_bounds`, `document_service.py:217`)
  // and this renderer does not reproduce it; it prints the answer.
  render: (content, presentation, document_, context) =>
    deferred("render-cells", `the cells of ${title(document_)}`, async (figure) => {
      const ref = presentation?.renderer_config?.ref ?? context.ref;
      const cells = await context.client.get_document_cells({
        document_id: document_.id,
        ...(ref === undefined ? {} : { ref }),
      });
      const grid = renderCells(cells);
      figure.append(grid);
      figure.surface = grid.surface;
      figure.overlayContainer = grid.overlayContainer;
      const evidence = context.evidence ?? null;
      if (evidence === null) return null;
      // A cells anchor only: this surface has no page, and overlay.js refuses a pdf anchor on
      // a surface that does not declare one rather than drawing it on whatever is up.
      const found = drawHighlight(grid.overlayContainer, evidence, grid.surface());
      const notice = highlightNotice(found);
      if (notice) figure.append(text("p", "overlay-notice", notice));
      return found;
    }),
});

registerRenderer("office", {
  showing: null,
  needs: ["client"],
  // AN OFFICE DOCUMENT, SHOWN AS THE TEXT THIS APPLIANCE READ OUT OF IT — and saying so, with
  // the reason there is no page.
  //
  // Step 29's brief calls this a fallback: "the `office` renderer falls back to extracted
  // markdown when no rendition exists, and says so on the page". It is worth being exact about
  // which of the two things it is, because the project's rule forbids one of them.
  //
  // What a fallback in the anti-pattern sense does is CATCH a failure and substitute a result,
  // so that a caller cannot tell the substitute from the real thing. This renderer does not do
  // that and must not: it never calls `/image` on a `.docx` and handles the 409, because
  // `NotAPdfSource` is a real answer about a real deployment and swallowing it is precisely
  // the failure `document_service.py` names at that class — "a renderer that quietly extracted
  // markdown instead would be the failure step 29 is written to avoid, one route earlier".
  //
  // What it does instead is choose between two REPRESENTATIONS by asking which exist. The
  // extracted markdown is not a substitute for the document: it is the document as this
  // appliance read it, the text that was chunked, embedded and indexed, and the thing a search
  // hit is a hit ON. Showing it is showing the reader what the appliance actually knows. The
  // page a rendition would give is a different and better VIEW of the same bytes, and it does
  // not exist — `render:pdf` is Block G step 32 and nothing in this tree writes one.
  //
  // So: no page is not a failure here, it is the only state, and refusing to render would
  // manufacture a problem rather than surface one. The line this renderer stays on is that the
  // absence is NAMED, from a named evidence row, before anything is drawn — never inferred
  // from an exception, and never left for the reader to notice.
  render: (content, presentation, document_, context) =>
    deferred("render-office", `what was read out of ${title(document_)}`, async (fig) => {
      const found = await context.client.get_document_evidence({ document_id: document_.id });
      const file = found.evidence?.file ?? {};
      // OFFICE_SPEC.md Part 6 names the key a rendition lands under. Asked as "is this row
      // here", not read as a value with a default — the capability table's defect was reading
      // an absent field and rendering it as the word "none", and both answers here are
      // sentences rather than a blank.
      const rendition = Object.prototype.hasOwnProperty.call(file, "rendition_ref");
      const note = element("p", "render-notice");
      note.textContent = rendition
        ? "This appliance holds a rendition of this document and this front end cannot show " +
          "it yet — serving a citation image off a rendition is docs/SPRINT_0_6_0.md Block G " +
          "step 33. What follows is the markdown `extract:text` read out of the source."
        : "This is the markdown this appliance read out of the document, NOT a rendered page " +
          "of it. There is no rendered page: `file.rendition_ref` is not on this node, and " +
          "nothing in this appliance writes one — `render:pdf` is Block G step 32. What is " +
          "below is what was chunked, embedded and indexed, which is what a search hit hits.";
      fig.append(note);
      const described = [file.filename, file.detected_mime].filter(Boolean).join(", ");
      if (described) fig.append(text("p", "dim", `Source file: ${described}.`));
      if (file.filename) fig.append(downloadControl(fig, context.client, document_, file));
      fig.append(renderMarkdown(content, "render-markdown"));
      return { rendition, filename: file.filename ?? null };
    }),
});
