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
// A SURFACE IS A FUNCTION ON THE ELEMENT, NOT AN OBJECT BESIDE IT. Every renderer here hangs
// `element.surface()` on what it returns. A function because a surface carries the extent the
// thing is rendered AT, and a caller holding an object from before a re-render would draw a
// box on the previous picture — a wrong box, confidently placed, which is the failure the
// whole contract is shaped against.

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
