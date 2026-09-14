// The surfaces IC-9 draws on, and the one place each one's geometry is declared.
//
// `docs/SPRINT_0_6_0.md` Block F step 29. `render/overlay.js` is IC-9's transform and it takes
// a surface; this module is where a surface comes from. The split is the one overlay.js states
// in its own header: the transform is shared and never branches on `surface.kind`, and what a
// particular surface KNOWS about itself — its extent, its origin corner, how it resolves an
// address — belongs beside the thing that built it.
//
// ORIGIN CORNER IS DECLARED HERE AND IS NOT A PARAMETER. overlay.js refuses a surface that
// does not declare one, and gives the reason: "a page rendered by pymupdf is top-left with y
// downward; a rectangle in PDF user space is bottom-left with y upward. They differ by a flip
// and nothing here can tell them apart." A factory that took the corner as an argument would
// move that coin flip one file along — three renderers, three chances to pass the wrong
// string. So :func:`rasterSurface` states `top-left` once, with the reason it is right:
//
//   * the PNG comes from `rendering.render_page`, which is `pymupdf`'s `get_pixmap`, and
//   * the anchor comes from `citation_tasks`, which measured it off `pymupdf`'s own page
//     rectangles (`citation_tasks.py:6`).
//
// Both halves are in pymupdf's space, whose origin is the page's top-left with y increasing
// downward — the same direction CSS wants. A rectangle read out of a PDF's own user space
// would be the other one, which is why the field exists at all and why this file says which of
// the two it is rather than leaving it to the caller.
//
// A SURFACE IS A FUNCTION ON THE ELEMENT, NOT AN OBJECT BESIDE IT. Every figure here hangs
// `element.surface()` on what it returns. A function because a surface carries the extent the
// thing is rendered AT, and a caller holding an object from before a re-render would draw a
// box on the previous picture — a wrong box, confidently placed, which is the failure the
// whole contract is shaped against.
//
// NOTHING IN THIS FILE FETCHES ANYTHING, and that is the division this bundle settled on.
// `render()` (IC-8) draws CONTENT — the string the view already has — and the VIEW draws
// SOURCE: it holds the client, it makes the call, and it hands the bytes here. So every
// function below takes a Blob, a response or a number that the caller already obtained, and
// the token stays in one place. See the header of `render/renderers.js` for the argument.
//
// WHAT THAT LEAVES SHARED IS THE PART THAT WAS GOING TO GO WRONG. Deriving a page's extent
// from a PNG and a density, choosing an origin corner, turning a box into CSS, and saying the
// sentence that goes beside a passage with no box — four things three views would each have
// written, and the plan names the third of them as the failure IC-9 exists to prevent. They
// are here once.

import { highlight, HIGHLIGHT_STATUS } from "./overlay.js";

/** A surface could not be built out of what came back. */
export class SurfaceError extends Error {
  constructor(message) {
    super(message);
    this.name = "SurfaceError";
  }
}

/**
 * The density every render here asks for, mirroring `jmfts_core/rendering.py:44`.
 *
 * SENT EXPLICITLY ON EVERY CALL, and that is the reason for a second copy of a constant the
 * server already has. The page derives the page's extent in points from the PNG's size in
 * pixels, which is only possible if the page knows the density the PNG was rendered at. A
 * client that omitted `dpi` and assumed the server's default would keep drawing boxes after
 * that default moved, each one off by the ratio — and nothing on the page would say so.
 */
export const DPI_DEFAULT = 150;

/** PDF's own unit: 72 points to the inch, which is what `dpi / 72` is a ratio of. */
const POINTS_PER_INCH = 72;

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

/**
 * The pixel extent of a PNG, read out of its own header.
 *
 * Not `img.naturalWidth` after a `load` event, for two reasons. The extent is needed to BUILD
 * the surface, so waiting for the browser to decode the image would make every surface
 * asynchronous in a second way; and the IHDR is the picture's own statement of its size, which
 * is available the moment the bytes arrive and is the same number in a browser and in a test.
 *
 * Refuses anything that is not a PNG rather than reading four bytes of something else as a
 * width. `/image` and `/region` declare `image/png` (`rendering.PNG_MEDIA_TYPE`), so bytes
 * that are not one mean the route answered something nobody expected.
 */
export function pngSize(bytes) {
  if (!(bytes instanceof Uint8Array)) {
    throw new SurfaceError(`pngSize() reads a Uint8Array; got ${typeof bytes}`);
  }
  if (bytes.length < 24 || PNG_SIGNATURE.some((byte, at) => bytes[at] !== byte)) {
    throw new SurfaceError(
      "these bytes do not start with a PNG signature, so this is not the picture " +
        "`/documents/{id}/image` and `/region` declare (image/png). Reading a width out of " +
        "them would be reading four bytes of something else as a number."
    );
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const width = view.getUint32(16);
  const height = view.getUint32(20);
  if (!width || !height) {
    throw new SurfaceError(`this PNG declares a ${width}x${height} extent, which is no picture`);
  }
  return Object.freeze({ width, height });
}

/**
 * A surface over something `pymupdf` rasterised: a page, or a rectangle of one.
 *
 * `source` is DERIVED from the pixel extent and the density rather than carried, because
 * nothing serves a page's size in points — `/image` answers a PNG and `BinaryPayload` is bytes
 * and their type, which `document_service.get_document_region` explains at length. The
 * relation is exact up to `pymupdf`'s rounding of the pixmap to whole pixels, which is under
 * one part in a thousand at 150 dpi and lands inside the border the overlay draws.
 *
 * @param {{kind: string, pixels: {width: number, height: number}, dpi: number,
 *          page?: number}} spec
 */
export function rasterSurface(spec) {
  const { kind, pixels, dpi, page } = spec;
  if (!Number.isFinite(dpi) || dpi <= 0) {
    throw new SurfaceError(`a rasterised surface is rendered at a density; got dpi ${dpi}`);
  }
  const scale = POINTS_PER_INCH / dpi;
  return Object.freeze({
    kind,
    source: Object.freeze({ width: pixels.width * scale, height: pixels.height * scale }),
    pixels: Object.freeze({ ...pixels }),
    // See this module's header. Stated here, once, for every raster surface.
    originCorner: "top-left",
    ...(page === undefined ? {} : { page }),
  });
}

/**
 * A surface over a grid of cells, whose source unit is one cell.
 *
 * The transform overlay.js applies is LINEAR, so the grid it draws on has to be: a table whose
 * columns size themselves to their contents would put the box over the wrong column whenever
 * one column held a long string. `render/sheet.js` sets a fixed cell size in the markup for
 * exactly that reason, and passes the same numbers here — so `pixels` is known without
 * measuring a layout, and a test with no layout at all gets the same answer a browser does.
 *
 * @param {{kind: string, columns: number, rows: number, cell: {width: number, height: number},
 *          sheet: string, cellRect: (ref: string) => (number[]|null)}} spec
 */
export function gridSurface(spec) {
  const { kind, columns, rows, cell, sheet, cellRect } = spec;
  if (!Number.isInteger(columns) || !Number.isInteger(rows) || columns <= 0 || rows <= 0) {
    throw new SurfaceError(
      `a grid surface spans whole cells; got ${columns} column(s) by ${rows} row(s)`
    );
  }
  return Object.freeze({
    kind,
    // One cell is one unit. A `cells` anchor names cells, so the space the anchor is written
    // in and the space this surface measures in are the same space, and the conversion the
    // overlay does is the only conversion there is.
    source: Object.freeze({ width: columns, height: rows }),
    pixels: Object.freeze({ width: columns * cell.width, height: rows * cell.height }),
    // A worksheet's row 1 is at the top and its column A is at the left, and so is a table's
    // first cell. Nothing about a grid is bottom-left.
    originCorner: "top-left",
    sheet,
    cellRect,
  });
}

/**
 * Draw one passage's highlight on a rendered surface, or say why there is no box.
 *
 * ONE function for all three renderers, which is IC-9's whole claim carried up one level: the
 * geometry is shared by `highlight()` and so is the decision about what to put on the page
 * when there is no box. The five outcomes are `HIGHLIGHT_STATUS`, and every one of them puts
 * something on the page except `NONE` — because "nothing was recorded" is the one case where
 * there is genuinely nothing to say.
 *
 * @param {Element} container the positioned element the picture sits in (`.overlay-surface`)
 * @param {{anchor: object|null, unresolved: object|null}} evidence both keys, always
 * @param {object} surface from one of the factories above
 * @returns {{status: string} & object} what `highlight()` decided, for a caller that wants it
 */
export function drawHighlight(container, evidence, surface) {
  const found = highlight(evidence, surface);
  if (found.status === HIGHLIGHT_STATUS.BOX) {
    const box = document.createElement("div");
    box.className = "overlay-box";
    // POSITIONED AS A FRACTION OF THE SURFACE, not in absolute pixels. The picture is
    // `max-width: 100%`, so a browser narrower than the render scales it — and a box in
    // absolute pixels would stay where the unscaled picture used to be. The percentages are
    // the same numbers divided by the extent the box was computed against, so they survive
    // any scaling the page does to the surface as a whole.
    const percent = (value, of) => `${((value / of) * 100).toFixed(4)}%`;
    box.setAttribute(
      "style",
      [
        `left:${percent(found.box.left, surface.pixels.width)}`,
        `top:${percent(found.box.top, surface.pixels.height)}`,
        `width:${percent(found.box.width, surface.pixels.width)}`,
        `height:${percent(found.box.height, surface.pixels.height)}`,
      ].join(";")
    );
    container.append(box);
  }
  return found;
}

/**
 * The sentence that goes beside a surface for a highlight that is not a box.
 *
 * Returns null for `BOX` and for `NONE`, which are the two states with nothing to explain:
 * one drew the box, and the other had nothing recorded and nothing lost. Every other state is
 * an absence a reader would otherwise have to guess the meaning of — and `UNRESOLVED` in
 * particular carries the reason a human wrote at `citation_tasks.py:284` rather than a
 * re-worded version of it.
 */
export function highlightNotice(found) {
  switch (found.status) {
    case HIGHLIGHT_STATUS.UNRESOLVED:
      return `No rectangle was recorded for this passage: ${found.reason} (${found.code})`;
    case HIGHLIGHT_STATUS.CONTINUED:
      return (
        `This passage runs through this page; it begins on page ${found.from + 1}, ` +
        "which is where its rectangle is."
      );
    case HIGHLIGHT_STATUS.ELSEWHERE:
      if (found.page !== undefined) return `This passage is on page ${found.page + 1}.`;
      if (found.ref !== undefined) {
        return `This passage is at ${found.ref}, which is outside the region shown here.`;
      }
      return `This passage is on sheet ${found.sheet}, and this is sheet ${found.showing}.`;
    default:
      return null;
  }
}

/**
 * An `<img>` over bytes this page already holds, plus the extent those bytes declare.
 *
 * The object URL is revoked on `load`: the browser has decoded the picture by then and the URL
 * is what keeps the Blob alive. A page that opened a hundred pages and revoked none would hold
 * a hundred PNGs for as long as the tab lived.
 *
 * Why an object URL and not `src="/documents/7/image?..."` — the route needs the API token in
 * an `Authorization` header, and an `<img src>` cannot carry one. The alternative would be a
 * credential in the query string, which puts it in every log the request passes through.
 */
export async function pictureFrom(blob, { alt }) {
  if (!blob || typeof blob.arrayBuffer !== "function") {
    throw new SurfaceError(
      "a picture is built from the Blob a binary operation resolves to; got " +
        `${blob === null ? "null" : typeof blob}. See client/transport.js readBody().`
    );
  }
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const pixels = pngSize(bytes);
  const url = URL.createObjectURL(blob);
  const image = document.createElement("img");
  image.className = "render-picture";
  image.setAttribute("src", url);
  image.setAttribute("alt", alt);
  image.setAttribute("width", String(pixels.width));
  image.setAttribute("height", String(pixels.height));
  image.addEventListener("load", () => URL.revokeObjectURL(url));
  return { image, pixels };
}

// ------------------------------------------------- what a view composes out of the above
//
// A view holds the client and makes the calls; these take what came back. Written once
// because otherwise they are written once per view: `wt/view-tree` derived a page's scale and
// chose an origin corner by hand before this existed, which is `docs/SPRINT_0_6_0.md`'s
// "writing it three times is how the three end up disagreeing about which corner the origin
// is in" arriving on schedule.

const el = (tag, className) => {
  const made = document.createElement(tag);
  if (className) made.className = className;
  return made;
};

const say = (tag, className, value) => {
  const made = el(tag, className);
  // textContent, never innerHTML. Some of these strings are a document's own filename and an
  // evidence row's own reason, and neither is this page's to execute.
  made.textContent = value;
  return made;
};

/** The failure's own words. Never `JSON.stringify(error)`, which is `{}` for every Error. */
export const messageOf = (error) => (error instanceof Error ? error.message : String(error));

/** A size a reader can weigh, so a Blob is reported as bytes and a type rather than as `{}`. */
export const describeBlob = (blob) =>
  `${blob.size.toLocaleString()} bytes of ${blob.type || "an undeclared type"}`;

/**
 * The two anchor rows of one node, out of a `GET /documents/{id}/evidence` response.
 *
 * BOTH KEYS ALWAYS, which is `highlight()`'s rule and `evidence.py:453`'s reason: "no
 * highlight" and "the rectangle could not be recovered, and here is why" are different facts,
 * and a caller that passed one has not said whether it looked for the other. A search hit
 * carries the same pair as `source_anchor` and `source_anchor_unresolved` (IC-4), which is one
 * round trip per hit cheaper — pass those straight to `highlight()` and skip this.
 *
 * A row that is ABSENT and a row written with a null value both come back as null, and here
 * that is one fact rather than two: `source_anchor` written-and-null says the anchor task ran
 * and placed nothing, which is the same "there is no rectangle" a viewer has to draw.
 */
export function anchorPair(evidenceResponse) {
  const rows = evidenceResponse?.evidence ?? {};
  return Object.freeze({
    anchor: rows["source_anchor"] ?? null,
    unresolved: rows["source_anchor.unresolved"] ?? null,
  });
}

/**
 * Which page of a document a node is on.
 *
 * The same rule `get_document_image` applies to an omitted `page` — a file node is the whole
 * document so its first page is page 0, and anything else names its page through its own
 * anchor. It is here as well as there because a PNG carries no page number and `BinaryPayload`
 * is bytes and their type, so a view that has to SAY which page it is showing has to work it
 * out. The duplication is then made harmless the other way: a view that calls this SENDS the
 * answer as `?page=`, so the server never takes the decision and the two cannot disagree.
 *
 * Refuses rather than falling back to page 0. A picture of the wrong page is what
 * `OFFICE_SPEC.md` Part 5 calls worse than no picture at all, because the reader cannot tell.
 */
export function pageFor(document_, evidence, named) {
  if (named !== undefined && named !== null) {
    if (!Number.isInteger(named) || named < 0) {
      throw new SurfaceError(`page ${JSON.stringify(named)} is not a 0-based page number`);
    }
    return named;
  }
  // `USETYPE_FILE` (`jmfts_core/models/document.py:48`), and this is the browser's copy of it
  // rather than a third spelling: `ViewResponse.usetype` carries the string and nothing serves
  // the constant. The alternative is not a lookup, it is not knowing.
  if (document_?.usetype === "file") return 0;
  if (evidence?.anchor?.kind === "pdf") return evidence.anchor.page;
  if (evidence?.unresolved) {
    throw new SurfaceError(
      "This node has no page recorded, and the reason was: " +
        `${evidence.unresolved.reason} (${evidence.unresolved.code}). Name a page to render ` +
        "one anyway."
    );
  }
  throw new SurfaceError(
    `Document ${document_?.id} is not a file node and carries no pdf anchor, so nothing says ` +
      "which page it came from. A picture of the wrong page is worse than no picture " +
      "(OFFICE_SPEC.md Part 5), so name one rather than being shown page 0."
  );
}

/** One anchor in the words a caption uses. */
export function describeAnchor(anchor) {
  if (anchor.kind === "pdf") {
    const box = anchor.bbox.map((value) => Math.round(value * 10) / 10).join(", ");
    return `page ${anchor.page + 1}, [${box}] in points`;
  }
  if (anchor.kind === "cells") return `${anchor.sheet}!${anchor.ref}`;
  return `characters ${anchor.char_start} to ${anchor.char_end}`;
}

/**
 * A rendered page, with the passage boxed on it. The beeline's picture.
 *
 * @param {{blob: Blob, page: number, dpi: number,
 *          evidence: {anchor: object|null, unresolved: object|null}, label: string}} spec
 *   `blob` is what `GET /documents/{id}/image?page=&dpi=` answered — the VIEW called it, and
 *   `dpi` is the density the view ASKED for, which is the number this page's scale is made of.
 * @returns {Promise<Element>} a `<figure>` carrying `surface()`, `overlayContainer` and
 *   `highlight` (what the overlay decided, so a caller can branch without re-deriving it)
 */
export async function pageFigure({ blob, page, dpi, evidence, label }) {
  const { image, pixels } = await pictureFrom(blob, {
    alt: `page ${page + 1} of ${label}, rendered at ${dpi} dpi`,
  });
  const surface = rasterSurface({ kind: "pdf-page", pixels, dpi, page });
  const figure = el("figure", "render-page");
  const positioned = el("div", "overlay-surface");
  positioned.append(image);
  figure.append(positioned);
  const found = drawHighlight(positioned, evidence, surface);
  figure.append(say("figcaption", "render-caption", `Page ${page + 1}, rendered at ${dpi} dpi.`));
  const notice = highlightNotice(found);
  if (notice) figure.append(say("p", "overlay-notice", notice));
  figure.surface = () => surface;
  figure.overlayContainer = positioned;
  figure.highlight = found;
  return figure;
}

/**
 * The picture of the words that matched: this node's own rectangle, cropped out of the page.
 *
 * NO BOX IS DRAWN ON IT, and that is the one thing here worth reading twice. The crop IS the
 * anchor's rectangle, so a box would cover the whole picture — and it would cover it in the
 * WRONG PLACE, because the anchor's coordinates are the page's and this surface's are the
 * crop's. The two differ by the crop's origin, `toPixels()` has no offset for that and must
 * not grow one quietly, and a box drawn 15% to the right of where it belongs is exactly the
 * confidently-wrong rectangle `OFFICE_SPEC.md` Part 5 calls worse than none. So the caption
 * states the page and the rectangle in points instead.
 *
 * The surface it returns is still real, and it is the space a query-time `search_for` within
 * one node would produce coordinates in — which the sprint plan defers by name ("a text span
 * within a node's content... simply not in this sprint") rather than leaves out by accident.
 */
export async function regionFigure({ blob, dpi, anchor, label }) {
  const { image, pixels } = await pictureFrom(blob, {
    alt: `the region ${label} was extracted from, rendered at ${dpi} dpi`,
  });
  const surface = rasterSurface({ kind: "image", pixels, dpi });
  const figure = el("figure", "render-region");
  const positioned = el("div", "overlay-surface");
  positioned.append(image);
  figure.append(positioned);
  figure.append(
    say(
      "figcaption",
      "render-caption",
      `This picture IS the rectangle recorded for this passage — ${describeAnchor(anchor)} — ` +
        `cropped out of the page at ${dpi} dpi, so there is no box to draw on it.`
    )
  );
  figure.surface = () => surface;
  figure.overlayContainer = positioned;
  return figure;
}

/**
 * The sentence that says which representation of a document is on the page.
 *
 * ONE WORDING, because three views writing three of them is how one of them ends up not
 * saying it at all. `docs/SPRINT_0_6_0.md` step 29 requires it of the office case — "a page
 * that silently shows markdown where it showed a rendered page for the document next to it is
 * the failure this step is written to avoid" — and the requirement is about the PAGE saying
 * so, not about which function says it.
 *
 * `rendition` is read from a named row rather than inferred from a failed call: pass
 * `Object.prototype.hasOwnProperty.call(file, "rendition_ref")` — `OFFICE_SPEC.md` Part 6
 * names that key — so that both answers are sentences and neither is a caught exception.
 */
export function sourceNotice({ rendition, filename, mime }) {
  const note = el("p", "render-notice");
  note.textContent = rendition
    ? "This appliance holds a rendition of this document and this front end cannot show it " +
      "yet — serving a citation image off a rendition is docs/SPRINT_0_6_0.md Block G step " +
      "33. What follows is the markdown `extract:text` read out of the source."
    : "This is the markdown this appliance read out of the document, NOT a rendered page of " +
      "it. There is no rendered page: `file.rendition_ref` is not on this node, and nothing " +
      "in this appliance writes one — `render:pdf` is Block G step 32. What is below is what " +
      "was chunked, embedded and indexed, which is what a search hit hits.";
  const described = [filename, mime].filter(Boolean).join(", ");
  if (!described) return note;
  const holder = el("div", "render-source-notice");
  holder.append(note, say("p", "dim", `Source file: ${described}.`));
  return holder;
}

/**
 * A link that saves bytes the page already holds.
 *
 * Separate from the fetch on purpose: `GET /documents/{id}/blob` serves the whole upload and
 * an office document is megabytes, so a view offers the control and calls the route when
 * somebody asks. What this does is turn the answer into something a browser will save, under
 * the uploader's own filename — which is the `file` evidence block's, the record of what was
 * received (`OFFICE_SPEC.md` 3.3).
 */
export function downloadLink(blob, filename) {
  const link = say("a", "render-download-ready", `Save ${filename} — ${describeBlob(blob)}`);
  link.setAttribute("href", URL.createObjectURL(blob));
  link.setAttribute("download", filename);
  return link;
}
