// Where a passage sits on the page it came from: reading the stored anchor, and putting its
// rectangle on a picture of that page.
//
// THE COORDINATE SPACE IS THE APPLIANCE'S, NOT THIS FILE'S. An anchor's bbox is
// `[x0, y0, x1, y1]` in PDF points, origin at the top-left of the page AS DISPLAYED — a page
// carrying `/Rotate 90` has its rectangle measured after the turn (#38, `a7cd176`). So there
// is no rotation arithmetic here, and that is deliberate: the page image from
// `GET /documents/{id}/image` is also rendered displayed, at `dpi / 72` pixels a point, so
// the only transform between the two is one scale. A rotation handled here as well would be
// the second turn that #38 removed from the server.
//
// What is NOT claimed: that the rectangle lands on the right words. Nothing in the browser can
// know that. `placement` below says only whether the rectangle lies on the page at all, which
// is the one thing the numbers can prove; a rectangle that is on the page and still on the
// wrong words is what the appliance's own crop, shown beside it, lets a person see.

import { detailOf } from "@jmfts/client/transport.js";

/** A stored anchor that names a rectangle of a PDF page, parsed. */
export interface PdfAnchor {
  /** 0-based, as the appliance stores it. Label it `page + 1` for a person. */
  page: number;
  /** `[x0, y0, x1, y1]` in points, displayed space. */
  bbox: [number, number, number, number];
  /**
   * `source` is the document's own text layer (`source_anchor`, written by `citation`);
   * `rendition` is a box recovered from the PDF the appliance made of an office document
   * (`render_anchor`, written by `anchor:render`), and is approximate.
   */
  of: "source" | "rendition";
  /** Further pages the passage runs onto. The box covers only `page`. */
  continues: number[];
}

/** The two evidence rows an anchor can come from. */
export type AnchorRow = "render_anchor" | "source_anchor";

/**
 * What the evidence says about this node's place in its source.
 *
 * Every arm is something a person is told; none is a quiet default. `absent` and `unresolved`
 * are different facts (an anchor never attempted versus one that was attempted and failed).
 * `cells` is a spreadsheet region, which is read as cells and not drawn on a page. `other` is
 * an anchor that is neither — an `ooxml` package address or a character span — which this view
 * cannot draw and says so.
 */
export type AnchorReading =
  | { state: "pdf"; anchor: PdfAnchor }
  | { state: "cells"; sheet: string; ref: string }
  | { state: "unresolved"; row: AnchorRow; reason: string; code: string | null }
  | { state: "absent" }
  | { state: "other"; kind: string }
  | { state: "malformed"; why: string };

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function present(value: unknown): boolean {
  return value !== null && value !== undefined;
}

/**
 * Read the anchor out of a node's evidence map (`GET /documents/{id}/evidence`).
 *
 * THE PRECEDENCE IS THE APPLIANCE'S, `DocumentService._pdf_anchor`: the recovered rectangle
 * first, then the source address. A Word or PowerPoint chunk carries BOTH rows — an exact
 * `ooxml` address in `source_anchor` from `anchor:source`, and a rectangle on a page of the
 * rendition in `render_anchor` from `anchor:render` — and the one this view can draw is the
 * second. Reading `source_anchor` alone (0.7.0) answered every office chunk with "not a
 * rectangle on a PDF page" while `/region` would have cropped it. `render_anchor.unresolved`
 * is read before `source_anchor` for the same reason: for an office chunk, "the rendition was
 * searched and this passage was not found in it" is the fact that answers "where on a page",
 * and the package address beside it does not.
 *
 * A key present with `null` and a key that is missing are different facts to the appliance
 * (spec 3.2), but both mean "this node has no recorded anchor" to a reader, and the
 * `unresolved` row is what says why when there is a reason. A row that IS there but is not a
 * well-formed anchor is `malformed` and is reported, never drawn partly.
 */
export function readAnchor(evidence: Record<string, unknown> | null | undefined): AnchorReading {
  const rendered = evidence?.["render_anchor"];
  if (present(rendered)) {
    const record = asRecord(rendered);
    if (record === null) return { state: "malformed", why: "the render_anchor row is not an object" };
    if (record.kind !== "pdf") {
      // The server refuses this too rather than falling through to `source_anchor`: the only
      // writer of the row is `anchor:render`, which writes a page and a rectangle.
      return {
        state: "malformed",
        why:
          `the render_anchor row is of kind ${JSON.stringify(record.kind)}, and a rendition ` +
          "anchor is a page and a rectangle; nothing in the appliance writes that row",
      };
    }
    return readPdf(record, "render_anchor");
  }
  const renderWhy = evidence?.["render_anchor.unresolved"];
  if (present(renderWhy)) return unresolved("render_anchor", renderWhy);

  const row = evidence?.["source_anchor"];
  if (!present(row)) {
    const why = evidence?.["source_anchor.unresolved"];
    if (present(why)) return unresolved("source_anchor", why);
    return { state: "absent" };
  }
  const record = asRecord(row);
  if (record === null) return { state: "malformed", why: "the source_anchor row is not an object" };
  if (record.kind === "cells") {
    if (typeof record.sheet !== "string" || typeof record.ref !== "string" || !record.ref) {
      return {
        state: "malformed",
        why: `a cells anchor names a sheet and a ref, and this one is ${JSON.stringify(record)}`,
      };
    }
    return { state: "cells", sheet: record.sheet, ref: record.ref };
  }
  if (record.kind !== "pdf") {
    return { state: "other", kind: String(record.kind) };
  }
  return readPdf(record, "source_anchor");
}

function asRecord(row: unknown): Record<string, unknown> | null {
  return row && typeof row === "object" && !Array.isArray(row) ? (row as Record<string, unknown>) : null;
}

function readPdf(record: Record<string, unknown>, row: AnchorRow): AnchorReading {
  const of = row === "render_anchor" ? "rendition" : "source";
  // `of` travels on a rendition anchor so it stays distinguishable downstream. A row that says
  // the other thing about itself is contradicting where it is stored, and is not drawn.
  if (present(record.of) && record.of !== of) {
    return {
      state: "malformed",
      why: `the ${row} row says it is of ${JSON.stringify(record.of)}, and that row holds ${of} anchors`,
    };
  }
  const { page, bbox } = record;
  if (!Number.isInteger(page) || (page as number) < 0) {
    return { state: "malformed", why: `page ${JSON.stringify(page)} is not a 0-based page number` };
  }
  if (!Array.isArray(bbox) || bbox.length !== 4 || !bbox.every(isFiniteNumber)) {
    return { state: "malformed", why: `bbox ${JSON.stringify(bbox)} is not four finite numbers` };
  }
  const [x0, y0, x1, y1] = bbox as number[];
  if (!(x1 > x0) || !(y1 > y0)) {
    return { state: "malformed", why: `bbox ${JSON.stringify(bbox)} has no area` };
  }
  const continues = Array.isArray(record.continues)
    ? (record.continues as unknown[]).filter(Number.isInteger).map(Number)
    : [];
  return {
    state: "pdf",
    anchor: { page: page as number, bbox: [x0, y0, x1, y1], of, continues },
  };
}

function unresolved(row: AnchorRow, why: unknown): AnchorReading {
  const record = asRecord(why);
  const code = record && typeof record.code === "string" ? record.code : null;
  return { state: "unresolved", row, reason: describeUnresolved(why), code };
}

/** The `.unresolved` row's own words when it has them, else the row as it arrived. */
function describeUnresolved(row: unknown): string {
  if (row && typeof row === "object") {
    const record = row as Record<string, unknown>;
    for (const key of ["reason", "detail", "why"]) {
      if (typeof record[key] === "string") return record[key] as string;
    }
  }
  return typeof row === "string" ? row : JSON.stringify(row);
}

/** A page in a person's numbering. Stored pages are 0-based; this is the only place they shift. */
export function pageLabel(page: number): string {
  return `page ${page + 1}`;
}

/** Where `bbox` is relative to a page. `partly` and `outside` are the numbers contradicting the image. */
export type Placement = "inside" | "partly" | "outside";

/** A rectangle as percentages of the page image, which is what a CSS overlay can hold at any zoom. */
export interface OverlayBox {
  left: number;
  top: number;
  width: number;
  height: number;
  placement: Placement;
}

/**
 * The page's size in points, recovered from the image the appliance rendered.
 *
 * The render is `dpi / 72` pixels a point (`rendering.render_page`), and the appliance refuses
 * rather than clamps a density it will not serve, so the image's own size and the density that
 * was asked for are the page. Nothing else about the page's geometry is needed.
 */
export function pagePoints(imageWidth: number, imageHeight: number, dpi: number) {
  if (!(imageWidth > 0) || !(imageHeight > 0) || !(dpi > 0)) {
    throw new Error(`a page image of ${imageWidth}x${imageHeight} at ${dpi} dpi has no size`);
  }
  const scale = 72 / dpi;
  return { width: imageWidth * scale, height: imageHeight * scale };
}

/**
 * How far, in points, a rectangle may run past the page's edge and still count as on it.
 *
 * A glyph that straddles a CropBox edge keeps its whole bounding box in the text layer, so a
 * passage whose first letters are cut by the crop has an anchor with x0 at -5 to -8 points
 * (the 0.7.0 preview audit, item 18, on CropBox pages). That is the page's own text, placed
 * correctly, and a red alarm over it teaches a reader to ignore the alarm. Ten points is a
 * little over the largest overhang measured and far below a real mismatch: a pre-#38 anchor on
 * a turned page misses by hundreds of points. The rectangle is still CLIPPED to the page for
 * drawing; this decides only whether the overhang is reported.
 */
export const EDGE_TOLERANCE_PT = 10;

/**
 * The anchor's rectangle in the page image's own frame.
 *
 * One scale and no rotation (see the top of this file). The percentages are of the page
 * IMAGE, so the overlay stays on its words when the image is resized to fit a column. A part
 * of the rectangle beyond the page is clipped for drawing and reported by `placement`, because
 * clipping silently would draw a box that is smaller than the one the appliance holds — unless
 * the overhang is within `EDGE_TOLERANCE_PT`, which is a glyph at a crop edge and not a fault.
 */
export function overlayBox(
  bbox: readonly [number, number, number, number],
  page: { width: number; height: number }
): OverlayBox {
  const [x0, y0, x1, y1] = bbox;
  const cx0 = Math.min(Math.max(x0, 0), page.width);
  const cy0 = Math.min(Math.max(y0, 0), page.height);
  const cx1 = Math.min(Math.max(x1, 0), page.width);
  const cy1 = Math.min(Math.max(y1, 0), page.height);
  const kept = (cx1 - cx0) * (cy1 - cy0);
  const within =
    x0 >= -EDGE_TOLERANCE_PT &&
    y0 >= -EDGE_TOLERANCE_PT &&
    x1 <= page.width + EDGE_TOLERANCE_PT &&
    y1 <= page.height + EDGE_TOLERANCE_PT;
  let placement: Placement = "partly";
  if (kept <= 0) placement = "outside";
  else if (within) placement = "inside";
  return {
    left: (cx0 / page.width) * 100,
    top: (cy0 / page.height) * 100,
    width: ((cx1 - cx0) / page.width) * 100,
    height: ((cy1 - cy0) / page.height) * 100,
    placement,
  };
}

/**
 * Whether the node's inspector should offer this view.
 *
 * A file node is offered because its whole document is the thing to look at, and a sheet node
 * because its used range is. Any other node is offered when its anchor is something this view
 * can show: a rectangle on a page (its own text layer's or a rendition's), or a cells range. A
 * node with no anchor, an unresolved one, or one this view cannot show is not offered a link to
 * a page that would only say so; the route still answers for it if someone types the address.
 */
export function offersPreview(
  usetype: string | null | undefined,
  evidence: Record<string, unknown> | null | undefined
): boolean {
  if (usetype === "file" || usetype === "sheet") return true;
  const state = readAnchor(evidence).state;
  return state === "pdf" || state === "cells";
}

/** One end of an A1 reference. */
export interface CellAddress {
  column: string;
  row: number;
}

/**
 * The two ends of an A1 reference.
 *
 * Not a parser of the A1 grammar; `jmfts_core/office/cells.py` owns that. It reads where the
 * letters stop and the digits start and refuses anything else rather than guessing, which is
 * `readRef` in `jmfts-web/jmfts_web/static/render/sheet.js`. The grid needs only the first and
 * last row: the response's `columns` already lists the letters it served.
 */
export function readCellRef(ref: string): [CellAddress, CellAddress] {
  const ends = ref.split(":");
  if (ends.length > 2) throw new Error(`"${ref}" is not a cell or a rectangle of cells`);
  const read = ends.map((end) => {
    const match = /^\$?([A-Za-z]{1,3})\$?(\d{1,7})$/.exec(end.trim());
    if (!match) throw new Error(`"${end}" is not an A1 cell reference`);
    return { column: match[1].toUpperCase(), row: Number(match[2]) };
  });
  return [read[0], read[read.length - 1]];
}

/** What kind of upload a node came from, as far as the reason it has no anchor goes. */
export type SourceFormat = "pdf" | "office" | "other" | "unrecognised";

/**
 * The upload's format, from the file node's `file.detected_mime`.
 *
 * Only the formats an anchor writer exists for are named: a PDF (`citation`), and an OOXML or
 * legacy office file (`anchor:source` on the package, `anchor:render` on its rendition). A null
 * `detected_mime` is `unrecognised`, because probe did not know the bytes, and it is not
 * guessed from the filename.
 */
export function sourceFormat(detectedMime: unknown): SourceFormat {
  if (typeof detectedMime !== "string" || !detectedMime) return "unrecognised";
  if (detectedMime === "application/pdf") return "pdf";
  if (
    detectedMime.startsWith("application/vnd.openxmlformats-officedocument.") ||
    detectedMime === "application/msword" ||
    detectedMime === "application/vnd.ms-powerpoint" ||
    detectedMime === "application/vnd.ms-excel"
  ) {
    return "office";
  }
  return "other";
}

/** What the appliance said when it refused, or what stopped the call from reaching it. */
export interface Refusal {
  /** The HTTP status; null when the request never got a response. */
  status: number | null;
  /** The server's `detail`, character for character. Never reworded here. */
  detail: string;
}

/**
 * The refusal a failed call carries, with the server's message untouched.
 *
 * The generated client's `JmftsHttpError.message` wraps the detail in `GET /path — HTTP 400:`,
 * which is right for a log and wrong for a person who needs to read what the appliance said and
 * act on it (the stale-anchor 400 says which tasks to re-run). The detail is taken from the
 * call event's own response with the client's `detailOf`, so what is shown is what was sent.
 * Anything that is not an HTTP answer keeps its own message and no status.
 */
export function refusalOf(failure: unknown): Refusal {
  const event = (failure as { event?: { status?: unknown; response?: unknown } } | null)?.event;
  if (event && typeof event.status === "number" && event.status > 0) {
    return { status: event.status, detail: detailOf(event.response) };
  }
  return { status: null, detail: failure instanceof Error ? failure.message : String(failure) };
}

/**
 * The density a 413 from `/image` or `/region` says will fit, or null when it names none.
 *
 * `rendering._check_bounds` refuses rather than clamps, and says what would fit in words:
 * "... Retry at 287 dpi or below" (too many pixels) or "... Retry at 600 or below" (above
 * `DPI_MAX`). There is no structured field for it, so this reads that sentence and nothing
 * else. A 413 whose detail does not say it is left as it came rather than answered with a
 * density made up here, and a suggestion of 0 (a page too large at any density) is not one.
 */
export function suggestedDpi(refusal: Refusal): number | null {
  if (refusal.status !== 413) return null;
  const match = /Retry at (\d+)(?: dpi)? or below/.exec(refusal.detail);
  if (!match) return null;
  const dpi = Number(match[1]);
  return dpi >= 1 ? dpi : null;
}

/**
 * Fetch a picture at `dpi`, and once more at the density a 413 suggests.
 *
 * ONE retry, only downward, and only at the appliance's own number: in 0.7.0 a large page at
 * the view's fixed density was a dead end. The density actually used is returned with the
 * image, because the page's size in points is recovered from the image and that number, and an
 * overlay scaled by the density asked for would be off by the ratio of the two. A second
 * refusal is thrown as it came, and so is every refusal that is not a 413 with a suggestion.
 */
export async function fetchAtDpi(
  fetch: (dpi: number) => Promise<Blob>,
  dpi: number
): Promise<{ blob: Blob; dpi: number }> {
  try {
    return { blob: await fetch(dpi), dpi };
  } catch (failure) {
    const fits = suggestedDpi(refusalOf(failure));
    if (fits === null || fits >= dpi) throw failure;
    return { blob: await fetch(fits), dpi: fits };
  }
}
