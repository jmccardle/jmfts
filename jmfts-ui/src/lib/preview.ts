// Where a passage sits on the page it came from: reading the stored anchor, and putting its
// rectangle on a picture of that page.
//
// THE COORDINATE SPACE IS THE APPLIANCE'S, NOT THIS FILE'S. A `source_anchor` bbox is
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

/** A stored anchor that names a rectangle of a PDF page, parsed. */
export interface PdfAnchor {
  /** 0-based, as the appliance stores it. Label it `page + 1` for a person. */
  page: number;
  /** `[x0, y0, x1, y1]` in points, displayed space. */
  bbox: [number, number, number, number];
  /** `source` is the document's own text layer; `rendition` is a recovered, approximate box. */
  of: "source" | "rendition";
  /** Further pages the passage runs onto. The box covers only `page`. */
  continues: number[];
}

/**
 * What the evidence says about this node's place in its source.
 *
 * Every arm is something a person is told; none is a quiet default. `absent` and `unresolved`
 * are different facts (an anchor never attempted versus one that was attempted and failed),
 * and `other` is an anchor that is not a PDF rectangle — an `ooxml` address or a cell range —
 * which this view cannot draw and says so.
 */
export type AnchorReading =
  | { state: "pdf"; anchor: PdfAnchor }
  | { state: "unresolved"; reason: string }
  | { state: "absent" }
  | { state: "other"; kind: string }
  | { state: "malformed"; why: string };

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * Read the anchor out of a node's evidence map (`GET /documents/{id}/evidence`).
 *
 * A key present with `null` and a key that is missing are different facts to the appliance
 * (spec 3.2), but both mean "this node has no recorded anchor" to a reader, and the
 * `unresolved` row is what says why when there is a reason. A row that IS there but is not a
 * well-formed rectangle is `malformed` and is reported, never drawn partly.
 */
export function readAnchor(evidence: Record<string, unknown> | null | undefined): AnchorReading {
  const row = evidence?.["source_anchor"];
  if (row === null || row === undefined) {
    const why = evidence?.["source_anchor.unresolved"];
    if (why !== null && why !== undefined) {
      return { state: "unresolved", reason: describeUnresolved(why) };
    }
    return { state: "absent" };
  }
  if (typeof row !== "object" || Array.isArray(row)) {
    return { state: "malformed", why: "the source_anchor row is not an object" };
  }
  const record = row as Record<string, unknown>;
  if (record.kind !== "pdf") {
    return { state: "other", kind: String(record.kind) };
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
    anchor: {
      page: page as number,
      bbox: [x0, y0, x1, y1],
      of: record.of === "rendition" ? "rendition" : "source",
      continues,
    },
  };
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
 * The anchor's rectangle in the page image's own frame.
 *
 * One scale and no rotation (see the top of this file). The percentages are of the page
 * IMAGE, so the overlay stays on its words when the image is resized to fit a column. A part
 * of the rectangle beyond the page is clipped for drawing and reported by `placement`, because
 * clipping silently would draw a box that is smaller than the one the appliance holds.
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
  const whole = (x1 - x0) * (y1 - y0);
  let placement: Placement = "partly";
  if (kept <= 0) placement = "outside";
  else if (Math.abs(kept - whole) < 1e-6) placement = "inside";
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
 * A file node is offered because its whole document is the thing to look at, and any node whose
 * anchor is a PDF rectangle because that rectangle is. A node with no anchor, an unresolved one
 * or one this view cannot draw is not offered a link to a page that would only say so; the
 * route still answers for it if someone types the address.
 */
export function offersPreview(
  usetype: string | null | undefined,
  evidence: Record<string, unknown> | null | undefined
): boolean {
  return usetype === "file" || readAnchor(evidence).state === "pdf";
}
