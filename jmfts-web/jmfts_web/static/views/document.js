// The document detail view: one node, what it says, where it came from, and what it touches.
//
// docs/SPRINT_0_6_0.md Block F step 28, the second half. `views/tree.js` is the browser that
// walks to here; step 31 (phase F4) is what makes a SEARCH HIT arrive here with its rectangle
// already drawn. This view does not implement the beeline — it is what the beeline lands on,
// and the three things that has to mean are written down rather than left to be discovered:
//
//   1. the anchor is a ROUTE ARGUMENT, so a hit can hand one over without a second call
//      (`#/document?id=42&anchor={"kind":"pdf",...}`) — see `readDetailParams`;
//   2. the surface is built by this view and the box is drawn by `render/overlay.js`, which
//      is IC-9 and which this view CONSUMES rather than reimplements;
//   3. every one of the overlay's five statuses reaches the page as its own sentence, because
//      "no highlight" and "the highlight could not be recovered" are different answers and
//      `jmfts_core/evidence.py:453` is why.
//
// ---------------------------------------------------------------------------------------
// WHAT DRAWS WHAT, AND WHY THE BYTES ARE NOT A RENDERER'S JOB
//
// IC-8 is `(content, presentation, document) -> element`, keyed by the `renderer` value the
// DATABASE hands the page. It is SYNCHRONOUS and it is handed text, which is the right shape
// for `rendered_content` and is not a shape that can fetch anything. The three byte routes
// need a credential — `<img src="/documents/7/image">` sends no Authorization header and
// answers 401 — so a page image is fetched by the client as a blob and shown through an
// object URL. That fetch belongs to the VIEW.
//
// So the division is: `render()` draws the node's CONTENT; this view draws its SOURCE. Step
// 29 (`wt/renderers`) owns `render/` and is completing the renderer set in parallel; nothing
// here names a renderer, branches on one, or assumes which ones exist. What it does is call
// `render()` and show what comes back, including the "this renderer is not finished" notice
// that `render()` puts there itself.
//
// ---------------------------------------------------------------------------------------
// A SPREADSHEET REGION IS CELLS, NOT A PICTURE OF CELLS
//
// `services/document_service.py:1548`'s `_pdf_anchor` refuses to crop a `cells` anchor and
// names `GET /documents/{id}/cells` in the refusal; step 22's brief gives the reason ("for a
// sheet, JSON cells are the better answer than a picture of cells"). This view honours that
// on the client side: a sheet node, or any node whose anchor addresses cells, gets a GRID —
// values, formulas behind them, and the anchor's range boxed on it by the same overlay that
// boxes a PDF rectangle. `cellsSurface()` is what lets one overlay do both.

import { registerView } from "../shell/registry.js";
import { render } from "../render/renderers.js";
import { HIGHLIGHT_STATUS, OverlayError, highlight } from "../render/overlay.js";

/** A route argument this view cannot honour. */
export class DocumentParamError extends Error {
  constructor(message) {
    super(message);
    this.name = "DocumentParamError";
  }
}

/** A cell reference this grid cannot resolve. Not an `OverlayError`: see `cellsSurface`. */
export class CellRefError extends Error {
  constructor(message) {
    super(message);
    this.name = "CellRefError";
  }
}

//: The two evidence rows, spelled as the wire spells them.
//: `jmfts-client/jmfts_client/contracts/anchor.py` carries `ANCHOR_ROW` and
//: `ANCHOR_UNRESOLVED_ROW` for the same purpose on the Python side, and
//: `jmfts_core/evidence.py:349` and `:473` are the declarations. There is no generated
//: JavaScript copy of either, so these two literals are the client's spelling of them.
export const ANCHOR_ROW = "source_anchor";
export const ANCHOR_UNRESOLVED_ROW = "source_anchor.unresolved";

/** How many children a page of `GET /view/{id}/expand-children` holds. */
export const CHILD_PAGE = 20;

/**
 * The density this view asks for, which is the server's own default (`rendering.py`'s
 * `DPI_DEFAULT`) sent EXPLICITLY. The number that produced a picture is then in the call log
 * beside the picture, rather than being a server-side default a reader has to go and look up
 * in order to compute the scale the box was drawn at.
 */
export const DPI_DEFAULT = 150;

/** The grid's pitch, in CSS pixels. Fixed, and the CSS below fixes it — see `cellsSurface`. */
export const CELL_W = 104;
export const CELL_H = 24;

/**
 * Load this view's stylesheet, once.
 *
 * `shell/shell.css:4` is the rule, `views/document.css` is the file, and the call is the first
 * line of `component()` rather than a statement at module scope: `shell/manifest.js` imports
 * every view for its registration alone, and a module that reaches for `document.head` when it
 * is merely imported cannot be imported that way.
 *
 * `views/document.css` fixes the grid's pitch at `CELL_W` × `CELL_H`, which is the geometry
 * `cellsSurface()` computes its box in. Two files cannot import one number; the test named in
 * that stylesheet's header reads both and fails when they disagree.
 */
let linked = false;
function linkStyles() {
  if (linked) return;
  const sheet = document.createElement("link");
  sheet.rel = "stylesheet";
  sheet.href = new URL("./document.css", import.meta.url).href;
  document.head.append(sheet);
  linked = true;
}

const make = (tag, className, text) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
};

const link = (className, text, href) => {
  const element = make("a", className, text);
  element.href = href;
  return element;
};

// ------------------------------------------------------------------------ route arguments

/**
 * One integer route argument — `page` or `dpi` — or null when it was not given.
 *
 * `id` does NOT come through here: it is `readDetailParams`' below, because that function is a
 * byte-for-byte copy of the one the search view writes the route with, and the two halves of a
 * link have to agree about what they will accept.
 *
 * Absent and unreadable are different. Absent is a reader who did not ask for a particular
 * page, which the node's own anchor answers; `page=three` is a link that is wrong, and reading
 * it as "no page" would show them page 1 of a document they were sent into the middle of.
 */
export function integerParam(params, name) {
  const raw = params[name];
  if (raw === undefined || raw === "") return null;
  if (!/^\d+$/.test(raw)) {
    throw new DocumentParamError(
      `${name}=${JSON.stringify(raw)} is not a whole number. A page is 0-based, as the ` +
        "anchors are, so the first page is page=0."
    );
  }
  return Number(raw);
}

/**
 * The detail route's three arguments: which document, and the citation the caller brought.
 *
 * THIS IS THE HALF OF THE BEELINE THIS STEP OWNS. IC-4 puts `source_anchor` and
 * `source_anchor_unresolved` on a search hit precisely so the front end need not call
 * `/documents/{id}/evidence` per result; a detail view that ignored what the hit already had
 * and went back to the server for it would put that round trip back one layer down. So step 31
 * navigates here with the hit's own rows, JSON-encoded on the fragment, and this reads them.
 *
 * **BYTE-IDENTICAL TO `readDetailParams` AT `views/search.js:504`**, but for the name of the
 * error class each module already has. Lane 27 (the search view) and this lane were cut from
 * the same commit and neither can import the other, so the writer and the reader of this
 * fragment are two copies on purpose — and identical copies reconcile at M3 by DELETING one,
 * which a paraphrase would not.
 *
 * Both keys come back present even when null, because `highlight()` in `render/overlay.js`
 * refuses to be called without both: a caller that passed only `anchor` has not said whether
 * it looked for the unresolved row, and "I did not look" is not "there was no reason
 * recorded". Torn JSON RAISES rather than reading as "no anchor" — a truncated fragment and a
 * hit with no citation are different facts, and collapsing them is how a highlight silently
 * stops appearing.
 */
export function readDetailParams(params) {
  const one = (name) => {
    const text = params[name];
    if (text === undefined || text === "") return null;
    try {
      return JSON.parse(text);
    } catch (error) {
      throw new DocumentParamError(
        `the "${name}" fragment parameter is not JSON: ${error.message}. It is written by ` +
          "detailParams() in views/search.js and read back here; a hand-edited URL can break it."
      );
    }
  };
  const id = params.id;
  if (id === undefined || !/^\d+$/.test(String(id))) {
    throw new DocumentParamError(
      `the document detail route needs an integer "id"; got ${JSON.stringify(id ?? null)}.`
    );
  }
  return { documentId: Number(id), anchor: one("anchor"), unresolved: one("unresolved") };
}

/**
 * Did the caller hand a citation over at all?
 *
 * `readDetailParams` reports a missing row and an absent argument identically, as null, which
 * is right for the overlay and not enough to decide whether to read the node's own evidence.
 * The presence of the KEY is what says somebody already looked: a hit navigates with both
 * arguments when it has them and with neither when it has none, and a tree click has never
 * looked at all. The one that has not looked pays for one call; nobody pays twice.
 */
export function broughtACitation(params) {
  return "anchor" in params || "unresolved" in params;
}

/**
 * The `{anchor, unresolved}` pair off a `GET /documents/{id}/evidence` response.
 *
 * A name PRESENT with a null value and a name ABSENT are different answers — 3.2, restated at
 * `DocumentEvidenceResponse`'s docstring — so this reports which of the two it found. "The
 * citation rung ran and placed nothing" and "nothing has run" are both "no box", and only one
 * of them is worth waiting for.
 */
export function evidenceFromRows(response) {
  const rows = response?.evidence;
  if (rows === null || typeof rows !== "object") {
    throw new DocumentParamError(
      "GET /documents/{id}/evidence answers {document_id, evidence}; this response carries " +
        `no evidence object (got ${JSON.stringify(response)}).`
    );
  }
  const anchor = ANCHOR_ROW in rows ? rows[ANCHOR_ROW] : null;
  const unresolved = ANCHOR_UNRESOLVED_ROW in rows ? rows[ANCHOR_UNRESOLVED_ROW] : null;
  return {
    anchor: anchor ?? null,
    unresolved: unresolved ?? null,
    attempted: ANCHOR_ROW in rows || ANCHOR_UNRESOLVED_ROW in rows,
  };
}

/**
 * Everything a renderer may ask with: `render()`'s fourth argument.
 *
 * STEP 29 MADE THIS ARGUMENT LOAD-BEARING. Four of the nine renderers draw bytes that are not
 * in `rendered_content` — a page image, a cropped rectangle, a grid of cells — so they declare
 * `needs: ["client"]` and `render()` refuses to dispatch without them rather than reaching
 * `undefined.get_document_image`. Migration 023 points usetype `sheet` at `sheet-region`, so a
 * three-argument call is not a missed opportunity here: it is every worksheet in the corpus
 * raising.
 *
 * The base is the context IC-7 handed the component, unpicked and rebuilt nowhere. On top of
 * it go the three things only this view knows:
 *
 * * `evidence` — the pair already resolved for the source panel, so a renderer that wants an
 *   anchor does not re-read what a search hit already carried (IC-4, one layer further down);
 * * `dpi` — sent explicitly so the density that produced a picture is in the log beside it;
 * * `page` and `ref` — ONLY when the route named one. `pageOf` in `render/renderers.js` reads a
 *   present key as an instruction and refuses a null, and "the reader did not say which page"
 *   has to arrive there as an absence rather than as a value.
 */
export function renderContext(context, { evidence, dpi, page, ref }) {
  return {
    ...context,
    evidence,
    dpi,
    ...(page === null || page === undefined ? {} : { page }),
    ...(ref === null || ref === undefined || ref === "" ? {} : { ref }),
  };
}

/** A link back to this view with some arguments replaced, keeping the rest of the route. */
export function hrefWith(params, overrides) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries({ ...params, ...overrides })) {
    if (value !== null && value !== undefined && value !== "") query.append(key, String(value));
  }
  return `#/document?${query.toString()}`;
}

// ------------------------------------------------------------------------------- surfaces

/**
 * The surface a rendered PDF page presents to the overlay.
 *
 * `pixels` is what the PNG DECODED to, read off the image rather than computed, and `source`
 * is derived from it: `jmfts_core/rendering.py` rasterises at `dpi/72` of the page's points,
 * so points are pixels × 72 ÷ dpi. Deriving it this way rather than asking for the media box
 * means there is no second number to be wrong — the scale the overlay divides by is the scale
 * the bytes were actually rendered at, including the rounding pymupdf did.
 *
 * `originCorner: "top-left"` because pymupdf's page space is top-left with y downward, which
 * is also what a `source_anchor` bbox is written in (`citation_tasks.py`) and what CSS wants.
 * The overlay refuses a surface that does not declare it, and that refusal is the reason this
 * is stated here rather than assumed three lines lower.
 */
export function pageSurface({ page, pixels, dpi }) {
  const scale = 72 / dpi;
  return {
    kind: "pdf-page",
    page,
    pixels: { width: pixels.width, height: pixels.height },
    source: { width: pixels.width * scale, height: pixels.height * scale },
    originCorner: "top-left",
  };
}

const A1_CELL = /^([A-Z]+)([0-9]+)$/;

/**
 * The surface a rendered cell grid presents to the overlay.
 *
 * IC-9 delegates A1 to the surface on purpose: "`jmfts_core/office/cells.py` owns that grammar
 * and a second parser in the client would be a second grammar", and a third in JavaScript
 * would be worse again. So this does not PARSE A1 — it LOOKS UP a reference against the
 * columns and rows the server said it rendered. `B4` is resolved by finding "B" in
 * `cells.columns` and 4 in `cells.rows`, which cannot drift from the grammar because it never
 * implements it: anything that is not a plain `<letters><digits>` pair, or a pair of them
 * either side of a colon, REFUSES rather than being guessed at.
 *
 * The coordinate space is CELLS, one unit per column and per row, with the header row and the
 * row-number gutter counted as the first unit of each axis — so the box lands on the grid this
 * module draws below, whose CSS pitch is `CELL_W` × `CELL_H` and is fixed for exactly that
 * reason. A range outside what was rendered returns null, which the overlay reads as
 * "elsewhere" rather than as a failure.
 */
export function cellsSurface(cells) {
  const columns = cells.columns;
  const rows = cells.rows.map((row) => row.row);
  const at = (text, where) => {
    const match = A1_CELL.exec(String(text).trim().toUpperCase());
    if (!match) {
      throw new CellRefError(
        `${JSON.stringify(where)} is not an A1 reference this grid can look up. It resolves ` +
          "a plain COLUMNROW pair such as B4, or two of them as B4:H120 — the grammar itself " +
          "lives in jmfts_core/office/cells.py and is not reimplemented here."
      );
    }
    const column = columns.indexOf(match[1]);
    const row = rows.indexOf(Number(match[2]));
    return column === -1 || row === -1 ? null : { column, row };
  };
  return {
    kind: "sheet-region",
    sheet: cells.sheet,
    source: { width: columns.length + 1, height: rows.length + 1 },
    pixels: { width: (columns.length + 1) * CELL_W, height: (rows.length + 1) * CELL_H },
    originCorner: "top-left",
    cellRect: (ref) => {
      const parts = String(ref).split(":");
      if (parts.length > 2) {
        throw new CellRefError(
          `${JSON.stringify(ref)} has ${parts.length - 1} colons; a range is one rectangle.`
        );
      }
      const first = at(parts[0], ref);
      const last = at(parts[parts.length - 1], ref);
      if (first === null || last === null) return null;
      // +1 on both axes for the header row and the gutter column, and +1 again on the far
      // edge because a cell occupies its whole unit rather than sitting at a point.
      return [
        Math.min(first.column, last.column) + 1,
        Math.min(first.row, last.row) + 1,
        Math.max(first.column, last.column) + 2,
        Math.max(first.row, last.row) + 2,
      ];
    },
  };
}

/** The absolutely positioned rectangle, in the surface's pixels. */
export function boxElement(box) {
  const element = make("div", "overlay-box");
  element.setAttribute(
    "style",
    `left:${box.left}px;top:${box.top}px;width:${box.width}px;height:${box.height}px`
  );
  return element;
}

/**
 * What to SAY about a highlight result, beside whatever was drawn.
 *
 * Every one of IC-9's five statuses gets a sentence, and that is the requirement rather than a
 * flourish: a viewer that drew a box for one status and nothing for the other four would be
 * the surface that hides `source_anchor.unresolved`, which is the failure the whole contract
 * is shaped around. Returns null only for a plain box that continues nowhere — there the
 * rectangle IS the statement.
 */
export function highlightNotice(result, { params = {}, page = null } = {}) {
  const human = (zeroBased) => `page ${zeroBased + 1}`;
  switch (result.status) {
    case HIGHLIGHT_STATUS.BOX:
      if (!result.continues || result.continues.length === 0) return null;
      return make(
        "p",
        "overlay-notice",
        `The box covers this page only; the passage continues on ` +
          `${result.continues.map(human).join(", ")}.`
      );
    case HIGHLIGHT_STATUS.CONTINUED: {
      const notice = make(
        "p",
        "overlay-notice",
        `The passage runs through ${human(result.page)} and the rectangle was recorded on ` +
          `${human(result.from)}, so nothing is boxed here. `
      );
      notice.append(
        link(null, `Show ${human(result.from)}`, hrefWith(params, { page: result.from }))
      );
      return notice;
    }
    case HIGHLIGHT_STATUS.ELSEWHERE: {
      if (result.sheet !== undefined) {
        return make(
          "p",
          "overlay-notice",
          result.ref !== undefined
            ? `The passage is at ${result.ref} of ${result.sheet}, which is outside the region ` +
              "this grid rendered."
            : `The passage is on sheet ${result.sheet} and this grid is showing ` +
              `${result.showing ?? "another sheet"}.`
        );
      }
      const notice = make(
        "p",
        "overlay-notice",
        `The passage is on ${human(result.page)}, and this is ` +
          `${page === null ? "another page" : human(page)}. `
      );
      notice.append(
        link(null, `Show ${human(result.page)}`, hrefWith(params, { page: result.page }))
      );
      return notice;
    }
    case HIGHLIGHT_STATUS.NONE:
      return make(
        "p",
        "dim",
        "No rectangle is recorded for this node, and no reason is recorded either — so " +
          "nothing has tried to place it. `citation` is the rung that writes one."
      );
    case HIGHLIGHT_STATUS.UNRESOLVED:
      return make(
        "p",
        "overlay-notice",
        `No rectangle could be recovered for this node: ${result.reason} (code ${result.code}).`
      );
    default:
      throw new Error(
        `the overlay answered with a status this view has no sentence for: ${result.status}`
      );
  }
}

/**
 * Decode a PNG blob far enough to know its pixel size, and hand back the element itself.
 *
 * The same element is then shown, so the browser decodes once. An object URL rather than a
 * data URL because the bytes are already a Blob and a base64 round trip of a 1275×1650 page is
 * work with nothing on the other end of it.
 */
async function decodeImage(blob) {
  const url = URL.createObjectURL(blob);
  const element = new Image();
  const measured = new Promise((resolve, reject) => {
    element.onload = () => resolve({ width: element.naturalWidth, height: element.naturalHeight });
    element.onerror = () =>
      reject(
        new Error("the appliance answered with bytes this browser could not decode as an image")
      );
  });
  element.src = url;
  try {
    return { element, pixels: await measured };
  } finally {
    // Revoked once the bytes are decoded, which is the whole life of the URL: the element
    // keeps the image, and the handle is what would otherwise keep the blob alive for the
    // life of the tab. A viewer that walks fifty pages of a PDF holds fifty of them.
    URL.revokeObjectURL(url);
  }
}

// -------------------------------------------------------------------------- source panels

/**
 * A rendered page with the box on it. Returns the panel and what the overlay decided.
 *
 * Exported for `tests/test_web_document_view.py`, and for step 31: the beeline is this
 * function reached with an anchor the search hit carried, and nothing else about it is new.
 */
export async function pagePanel({ client, documentId, page, dpi, evidence, params }) {
  const blob = await client.get_document_image({ document_id: documentId, page, dpi });
  const { element: image, pixels } = await decodeImage(blob);
  image.setAttribute("alt", `page ${page + 1} of document ${documentId}`);

  const panel = make("div");
  const scroll = make("div", "doc-frame");
  const frame = make("div", "overlay-surface");
  frame.append(image);
  scroll.append(frame);
  panel.append(scroll);
  // WHAT CAME BACK, AS BYTES AND AS PIXELS. A binary response has no shape a page can print —
  // `JSON.stringify` flattens a Blob to `{}`, which is how bytes become an empty object on
  // screen — so what is printed is its size and its media type, beside the density that
  // produced it. Those three are what a reader needs in order to know the box was drawn at the
  // scale the picture was rendered at rather than at some other one.
  panel.append(
    make(
      "p",
      "dim doc-caption",
      `page ${page + 1} — ${blob.size} bytes of ${blob.type || "an undeclared media type"}, ` +
        `${pixels.width}×${pixels.height} px at ${dpi} dpi`
    )
  );

  const surface = pageSurface({ page, pixels, dpi });
  let result;
  try {
    result = highlight(evidence, surface);
  } catch (error) {
    if (!(error instanceof OverlayError)) throw error;
    // A span anchor is a VALID anchor with no geometry, and an unknown kind means this front
    // end is older than the appliance. Both messages are written for a reader and both say
    // what to do next, so they go on the page as they are rather than being restated.
    panel.append(make("p", "overlay-notice", error.message));
    return { element: panel, result: null, surface };
  }
  if (result.status === HIGHLIGHT_STATUS.BOX) frame.append(boxElement(result.box));
  const notice = highlightNotice(result, { params, page });
  if (notice) panel.append(notice);
  return { element: panel, result, surface };
}

/** The grid, with the anchor's range boxed on it. */
export async function cellsPanel({ client, documentId, ref, evidence, params }) {
  const cells = await client.get_document_cells({ document_id: documentId, ref });
  const surface = cellsSurface(cells);

  const panel = make("div");
  const heading = make(
    "p",
    "dim",
    `${cells.sheet} — ${cells.ref} (${cells.row_count} rows, ${cells.cell_count} cells), ` +
      `addressed by ${cells.ref_source}`
  );
  panel.append(heading);

  const scroll = make("div", "doc-frame");
  const frame = make("div", "overlay-surface");
  frame.append(gridElement(cells));
  scroll.append(frame);
  panel.append(scroll);

  let result;
  try {
    result = highlight(evidence, surface);
  } catch (error) {
    if (!(error instanceof OverlayError)) throw error;
    panel.append(make("p", "overlay-notice", error.message));
    return { element: panel, result: null, surface };
  }
  if (result.status === HIGHLIGHT_STATUS.BOX) frame.append(boxElement(result.box));
  const notice = highlightNotice(result, { params });
  if (notice) panel.append(notice);
  return { element: panel, result, surface };
}

/** The cells as a table on the fixed pitch `cellsSurface` computes its coordinates in. */
export function gridElement(cells) {
  const table = make("table", "doc-grid");
  const head = make("tr");
  head.append(make("th", null, ""));
  for (const column of cells.columns) head.append(make("th", null, column));
  table.append(head);

  for (const row of cells.rows) {
    const line = make("tr");
    line.append(make("th", null, String(row.row)));
    row.values.forEach((value, index) => {
      const column = cells.columns[index];
      const note = cells.cells[`${column}${row.row}`];
      // A value and the formula behind it are two facts about one cell, and the grid shows
      // the value with the formula on it rather than one instead of the other — which is the
      // whole reason a sheet region is served as cells and not as a picture.
      const cell = make(
        "td",
        note && note.formula ? "formula" : null,
        value === null ? "" : String(value)
      );
      if (note && note.formula) cell.setAttribute("title", note.formula);
      line.append(cell);
    });
    table.append(line);
  }
  return table;
}

/**
 * What this node says about its source, plus the button that asks for a picture anyway.
 *
 * The button exists because the appliance is the only thing that KNOWS: a node with no anchor
 * may still sit under a PDF whose page 1 is a perfectly good answer, and the alternative to
 * asking is a page that decided on the reader's behalf that there was nothing to see. What
 * this must not do is fire that call unasked on every text node in the corpus — one log of
 * every call this front end makes is the property step 23 built, and filling it with expected
 * 409s is how that stops being readable.
 *
 * Every refusal reaches the page verbatim. `document_service.py` writes them for a reader —
 * "this is a .docx", "Document 7 has no stored blob", "retry at 218 dpi or below" — and
 * rewriting one into "could not load" throws away the half that says what to do next.
 */
export function sourceOffer({ client, documentId, page, dpi, evidence, params }) {
  const panel = make("div");
  panel.append(anchorSummary(evidence));

  const offer = make("button", "tiny", `Render page ${page + 1} of the source anyway`);
  offer.type = "button";
  const into = make("div");
  const load = async () => {
    offer.disabled = true;
    into.textContent = "";
    try {
      const drawn = await pagePanel({ client, documentId, page, dpi, evidence, params });
      into.append(drawn.element);
      return drawn;
    } catch (error) {
      into.append(make("p", "bad", error.message));
      return null;
    } finally {
      offer.disabled = false;
    }
  };
  offer.addEventListener("click", load);
  panel.append(offer, into);
  return { element: panel, load, into };
}

// ------------------------------------------------------------------------------ the page

const USETYPE_SHEET = "sheet";

/**
 * Which source surface this node has, before anything is fetched.
 *
 * A decision taken from the anchor and the usetype rather than by calling `/image` and reading
 * the refusal, because firing a call that is expected to 409 on every text node is noise in
 * the one log this front end asks to be judged by. Where there is nothing to decide from, the
 * panel is OFFERED and not loaded: the reader asks, the appliance answers, and its refusal —
 * "this is a .docx", "there are no stored bytes" — is written to be read and goes on the page
 * as it arrives.
 */
export function plannedSurface({ node, anchor }) {
  if (anchor && anchor.kind === "cells") return { kind: "cells", ref: null };
  if (anchor && anchor.kind === "pdf") return { kind: "page", page: anchor.page };
  if (anchor && anchor.kind === "span") return { kind: "span" };
  if (node.usetype === USETYPE_SHEET) return { kind: "cells", ref: null };
  return { kind: "offer", page: 0 };
}

/**
 * What this node's evidence says about where it came from, when there is no surface to draw.
 *
 * Five answers and they are five different facts. The two that a viewer is most tempted to
 * collapse are the two the anchor contract separates hardest: a row that is ABSENT means
 * nothing has tried, a row that is PRESENT AND NULL means an atom ran and produced nothing
 * (`DocumentEvidenceResponse`, 3.2), and `source_anchor.unresolved` means it tried and failed
 * and recorded why. "No highlight" for all three is how the third one stays invisible.
 */
export function anchorSummary(evidence) {
  const { anchor, unresolved, attempted } = evidence;
  if (anchor === null && unresolved !== null) {
    return make(
      "p",
      "overlay-notice",
      `No rectangle could be recovered for this node: ${unresolved.reason} ` +
        `(code ${unresolved.code}).`
    );
  }
  if (anchor === null) {
    return make(
      "p",
      "dim",
      attempted === false
        ? "No source_anchor row and no unresolved row: nothing has tried to place this node " +
          "in its source. `citation` is the rung that writes one, and it runs over a PDF " +
          "text layer."
        : "This node carries no anchor into its source."
    );
  }
  return make(
    "p",
    "overlay-notice",
    `This node's anchor is of kind ${JSON.stringify(anchor.kind)}, which this front end has ` +
      "no geometry for — it is older than the appliance that wrote the anchor. " +
      "jmfts-client/jmfts_client/contracts/anchor.py is the authority on the kinds."
  );
}

function head(view, node) {
  const bar = make("div", "doc-head");
  bar.append(make("h1", null, view.title || `Document #${view.id}`));
  bar.append(make("span", "pill", view.usetype || "no usetype"));
  bar.append(make("span", "dim mono", `#${view.id}`));
  if (node.settled !== "settled") {
    bar.append(make("span", "bad", `${node.settled} — not yet reachable by any search`));
  }
  if (node.produced_by) bar.append(make("span", "dim", `produced by ${node.produced_by}`));
  bar.append(
    make("span", view.can_write ? "dim" : "bad", view.can_write ? "writable" : "read-only")
  );
  return bar;
}

function crumbs(view) {
  const bar = make("div", "doc-crumbs");
  bar.append(link("dim", "all roots", "#/documents"));
  for (const ancestor of view.ancestors) {
    bar.append(make("span", "dim", "/"));
    bar.append(link(null, ancestor.title || `#${ancestor.id}`, `#/document?id=${ancestor.id}`));
  }
  bar.append(make("span", "dim", "/"));
  bar.append(make("span", null, view.title || `#${view.id}`));
  bar.append(link("dim", "browse the subtree", `#/documents?root=${view.id}`));
  return bar;
}

/**
 * The create-link control, or the sentence that says why there is not one.
 *
 * IC-3 exists for this paragraph. `ViewResponse.can_write` reports whether the CALLING
 * principal may write this node, from the same `jmfts_core.access.can_write` the repository
 * gate calls — so a control offered here and a call refused there cannot disagree. Before it,
 * a page had to fire the write and read the 403 back to find out, which is a button that lies.
 *
 * The form is returned with its `submit` beside it so a test can drive it without inventing a
 * DOM event, and so the failure path is one function rather than a handler.
 */
export function createLinkControl({ view, client, onCreated }) {
  const panel = make("div");
  if (!view.can_write) {
    panel.append(
      make(
        "p",
        "dim",
        "This token may not write to this document, so no create-link control is offered. " +
          "That is ViewResponse.can_write reporting the access the appliance would enforce, " +
          "not a guess — offering the control and letting the call 403 is the button that lies."
      )
    );
    return { element: panel, submit: null };
  }

  const form = make("form", "doc-form");
  const target = make("input");
  target.setAttribute("placeholder", "target document id");
  target.setAttribute("aria-label", "target document id");
  const type = make("input");
  type.value = "related";
  type.setAttribute("placeholder", "link type");
  type.setAttribute("aria-label", "link type");
  const button = make("button", null, "Create link");
  button.type = "submit";
  const said = make("span", "dim", "");
  form.append(target, type, button, said);
  panel.append(form);

  const submit = async () => {
    const id = String(target.value ?? "").trim();
    const linkType = String(type.value ?? "").trim();
    // The form's own validation, which is the half of Fail Early that belongs to a form: an
    // input that cannot be a request does not become one. Everything the SERVER decides — a
    // target that does not exist, an edge it refuses — is left to the server and shown.
    if (!/^\d+$/.test(id) || !linkType) {
      said.className = "bad";
      said.textContent = "a target document id (a whole number) and a link type are both needed";
      return null;
    }
    said.className = "dim";
    said.textContent = "creating…";
    try {
      const created = await client.create_link({
        document_id: view.id,
        request: { source_id: view.id, target_id: Number(id), link_type: linkType, score: 1.0 },
      });
      said.className = "dim";
      said.textContent = `created link #${created.id}`;
      if (onCreated) onCreated(created);
      return created;
    } catch (error) {
      said.className = "bad";
      said.textContent = error.message;
      return null;
    }
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });
  return { element: panel, submit, fields: { target, type }, said };
}

function linkRows(view) {
  const table = make("table");
  const edges = [...view.outbound_links, ...view.inbound_links];
  if (edges.length === 0) {
    return make("p", "dim", "no links on this node");
  }
  for (const edge of edges) {
    const line = make("tr");
    line.append(make("th", null, `${edge.direction} ${edge.link_type}`));
    const cell = make("td");
    const other = edge.direction === "outbound" ? edge.target_id : edge.source_id;
    cell.append(link(null, edge.title || `#${other}`, `#/document?id=${other}`));
    cell.append(make("span", "dim", ` score ${edge.score}`));
    line.append(cell);
    table.append(line);
  }
  return table;
}

function tripleRows(view) {
  if (view.triples.length === 0) return make("p", "dim", "no triples on this node");
  const table = make("table");
  for (const triple of view.triples) {
    const line = make("tr");
    line.append(make("th", null, triple.predicate_name || "(no predicate)"));
    const cell = make("td");
    cell.append(
      link(
        null,
        triple.subject_title || `#${triple.subject_id}`,
        `#/document?id=${triple.subject_id}`
      )
    );
    cell.append(make("span", "dim", " → "));
    if (triple.object_id === null) {
      // A literal object has no node to navigate to, which is why the contract gives it a
      // null `object_url` rather than a `/view/None`. Shown as the literal it is.
      cell.append(make("span", null, triple.object_literal ?? "(null literal)"));
      if (triple.object_datatype) cell.append(make("span", "dim", ` ^^${triple.object_datatype}`));
    } else {
      cell.append(
        link(
          null,
          triple.object_title || `#${triple.object_id}`,
          `#/document?id=${triple.object_id}`
        )
      );
    }
    line.append(cell);
    table.append(line);
  }
  return table;
}

/**
 * The children `/view/` is willing to show, and the button that asks for the next page.
 *
 * Returns `{element, loadMore}` — `loadMore` is null when there is nothing more to ask for —
 * because a click event is not a thing a headless test can honestly produce and a handler
 * that is also a function is one less seam than a handler that is only a handler.
 */
export function childStubs(view, { client }) {
  const panel = make("div");
  if (view.presentation.child_handling === "hidden") {
    // `view_renderer.build_children_stubs` returns [] for this handling, so an empty list here
    // is a PRESENTATION decision and not a fact about the tree. Saying which of the two it is
    // costs one sentence; not saying it makes a subtree disappear from the one page a reader
    // is looking at it on.
    panel.append(
      make(
        "p",
        "dim",
        'This usetype\'s presentation sets child_handling: "hidden", so /view/ serves no ' +
          "child stubs for it. Whatever is under this node is under it still — the tree " +
          "browser reads /documents/{id}/children, which no presentation row filters."
      )
    );
    panel.append(link(null, "Browse this subtree", `#/documents?root=${view.id}`));
    return { element: panel, loadMore: null };
  }
  if (view.children_stubs.length === 0) {
    panel.append(make("p", "dim", "no children"));
    return { element: panel, loadMore: null };
  }

  const list = make("div");
  const draw = (stubs) => {
    for (const stub of stubs) {
      const box = make("div", "doc-stub");
      box.append(link(null, stub.title || `#${stub.id}`, `#/document?id=${stub.id}`));
      box.append(make("span", "pill", stub.usetype || "no usetype"));
      if (stub.child_count) box.append(make("span", "dim", ` ${stub.child_count} children`));
      if (stub.preview) box.append(make("p", null, stub.preview));
      list.append(box);
    }
  };
  draw(view.children_stubs);
  panel.append(list);

  let offset = view.children_stubs.length;
  const more = make("button", "tiny", "More children");
  more.type = "button";
  const said = make("span", "dim", "");
  const load = async () => {
    more.disabled = true;
    try {
      const page = await client.expand_children({
        document_id: view.id,
        offset,
        limit: CHILD_PAGE,
      });
      draw(page);
      offset += page.length;
      if (page.length < CHILD_PAGE) {
        more.hidden = true;
        said.textContent = `${offset} children, which is all of them`;
      }
    } catch (error) {
      said.className = "bad";
      said.textContent = error.message;
    } finally {
      more.disabled = false;
    }
  };
  more.addEventListener("click", load);
  panel.append(more, said);
  return { element: panel, loadMore: load };
}

function section(page, title) {
  page.append(make("h2", null, title));
  const panel = make("div", "doc-panel");
  page.append(panel);
  return panel;
}

function landing(page) {
  page.append(make("h1", null, "Document"));
  page.append(
    make(
      "p",
      "lede",
      "This view shows one node: its content through the renderer its usetype names, the " +
        "page or the cells it came from with the passage boxed on them, and everything it " +
        "links to. It needs to be told which node — #/document?id=42."
    )
  );
  page.append(link(null, "Browse the tree instead", "#/documents"));
  return page;
}

async function component(context) {
  // The WHOLE context, not two keys off it. IC-7 hands a component `{client, params, navigate}`
  // and step 29 made that object the fourth argument of `render()`: four of the nine renderers
  // draw bytes that are not in `rendered_content` — a page image, a cropped rectangle, a grid
  // of cells — so they declare `needs: ["client"]` and `render()` refuses rather than reaching
  // `undefined.get_document_image`. Destructuring here and rebuilding an object there would be
  // two spellings of one thing, and the second would be the one that goes stale.
  const { client, params } = context;
  linkStyles();
  const page = make("div", "view-document");

  // No id at all is a reader who has not said which document — the shell reads an empty
  // fragment the same way, and for the same reason: ambivalence is not a failure. An id that
  // is PRESENT and not a number is a link that is wrong, and `readDetailParams` refuses it.
  if (params.id === undefined || params.id === "") return landing(page);
  const { documentId: id, anchor, unresolved } = readDetailParams(params);

  const [view, node, back] = await Promise.all([
    client.view_document({ document_id: id }),
    client.get_document({ document_id: id }),
    client.get_back_references({ document_id: id, limit: 20 }),
  ]);

  // One call, and only when nobody has already paid for it. See `broughtACitation`. Note what
  // is NOT asked for: `get_document` defaults `include_embed=false`, and this page never sets
  // it — 768 floats is about 15 kB of JSON per row and there is nothing on this page that
  // reads a vector.
  const evidence = broughtACitation(params)
    ? { anchor, unresolved }
    : evidenceFromRows(await client.get_document_evidence({ document_id: id }));

  page.append(head(view, node));
  page.append(crumbs(view));

  // ------------------------------------------------------------------ the source panel
  const source = section(page, "Source");
  const requested = integerParam(params, "page");
  const dpi = integerParam(params, "dpi") ?? DPI_DEFAULT;
  const planned = plannedSurface({ node, anchor: evidence.anchor });
  const shown = requested === null ? planned : { kind: "page", page: requested };

  try {
    if (shown.kind === "page") {
      const drawn = await pagePanel({
        client,
        documentId: id,
        page: shown.page,
        dpi,
        evidence,
        params,
      });
      source.append(drawn.element);
    } else if (shown.kind === "cells") {
      const drawn = await cellsPanel({ client, documentId: id, ref: shown.ref, evidence, params });
      source.append(drawn.element);
    } else if (shown.kind === "span") {
      // The overlay refuses to draw a span on a page and says to highlight the text instead.
      // The text is right there in `rendered_content`, so the passage itself is shown — which
      // is the answer to "where did this come from" for a document nothing has paginated.
      const { char_start: start, char_end: end } = evidence.anchor;
      source.append(
        make(
          "p",
          "dim",
          `This node is addressed at characters ${start}–${end} of its source's extracted ` +
            "markdown, which is a range in text and has no rectangle on any page."
        )
      );
      source.append(make("pre", "render-plain", view.rendered_content.slice(start, end)));
    } else {
      const offered = sourceOffer({
        client,
        documentId: id,
        page: shown.page,
        dpi,
        evidence,
        params,
      });
      source.append(offered.element);
    }
  } catch (error) {
    source.append(make("p", "bad", error.message));
  }

  // ---------------------------------------------------------------------- the content
  //
  // FOUR ARGUMENTS, and the fourth is `renderContext` above. `element.ready` is deliberately
  // NOT awaited: the four visual renderers return their frame and fill it in, a failure
  // arrives as its own message inside that frame, and nothing on this page branches on the
  // outcome — so awaiting would delay the rest of the page in order to learn something the
  // reader can already see. Nor is `element.surface()` called here; it is a function because
  // an extent belongs to the render that produced it, and this view's own surfaces are the
  // ones it draws on.
  const content = section(page, `Content — ${view.presentation.renderer}`);
  try {
    content.append(
      render(
        view.rendered_content,
        view.presentation,
        view,
        renderContext(context, { evidence, dpi, page: requested, ref: params.ref })
      )
    );
  } catch (error) {
    // Shown here rather than thrown to the shell, which would replace the whole page with one
    // message and take the links, the children and the source panel down with it. An
    // unknown renderer means this front end is older than the appliance; that is worth
    // reading WITH the rest of the node still on screen.
    content.append(make("p", "bad", error.message));
  }

  // --------------------------------------------------------------------- the relations
  section(page, "Children").append(childStubs(view, { client }).element);

  const links = section(page, "Links");
  links.append(linkRows(view));
  links.append(createLinkControl({ view, client }).element);

  section(page, "Triples").append(tripleRows(view));

  const references = section(page, "Back-references");
  if (back.total === 0) {
    references.append(make("p", "dim", "nothing links or asserts at this node"));
  } else {
    references.append(make("p", "dim", `${back.total} in total, ${back.references.length} shown`));
    for (const reference of back.references) {
      const box = make("div", "doc-stub");
      box.append(
        link(
          null,
          reference.title || `#${reference.document_id}`,
          `#/document?id=${reference.document_id}`
        )
      );
      box.append(make("span", "pill", `via ${reference.via}`));
      box.append(make("span", "dim", ` ${reference.relation}`));
      if (reference.snippet) box.append(make("p", null, reference.snippet));
      references.append(box);
    }
  }

  return page;
}

registerView({
  id: "document",
  title: "Document",
  path: "/document",
  component,
  tags: ["corpus", "documents"],
});
