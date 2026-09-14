// IC-9: the highlight overlay — `(source_anchor, surface) -> box`. ONE component, three
// surfaces.
//
// docs/SPRINT_0_6_0.md Block F step 25. `pdf-page`, `image` and `sheet-region` all draw the
// same box, and the sprint plan's reason for writing it once is exact: "Writing it three times
// is how the three end up disagreeing about which corner the origin is in."
//
// SO THE GEOMETRY NEVER BRANCHES ON `surface.kind`. That is the property, stated as a rule a
// reader can check by reading: `toPixels()` below is the only place a source coordinate
// becomes a pixel, and it reads `source`, `pixels` and `originCorner` — never the kind. The
// kind names the surface in error messages and is what
// `tests/test_web_views.py::test_one_anchor_renders_identically_on_all_three_surfaces` varies
// while holding the geometry fixed. If the kind ever selects a formula, that test goes red.
//
// THE ANCHOR SHAPES ARE REAL AND THEY ALREADY REACH THE API. The authority is
// `jmfts-client/jmfts_client/contracts/anchor.py`, and this module is its counterpart in the
// browser — same three kinds, same refusal on a fourth:
//
//   {"kind": "pdf",   "page": 3, "bbox": [x0, y0, x1, y1]}   PDF points (citation_tasks.py:6)
//   {"kind": "cells", "sheet": "Q3 Pipeline", "ref": "B4:H120"}  (document_service.py:141)
//   {"kind": "span",  "char_start": n, "char_end": m}            (atoms.py:191)
//
// TWO ABSENCES, AND THEY ARE NOT THE SAME ABSENCE. `source_anchor.unresolved` is its own
// evidence row, "present exactly when `anchor` is not" (`jmfts_core/evidence.py:453`). So
// "this passage has no highlight" and "this passage's rectangle could not be recovered, and
// here is the code and the reason" are different answers, and a viewer that drew nothing for
// both would be hiding the second. That is the Fail Early rule applied to a surface, and
// collapsing the two into a blank page is the exact failure this sprint is written against —
// so `highlight()` returns a TAGGED result and never a bare box-or-null, and the caller has
// to have handled `"unresolved"` in order to have handled anything.
//
// COORDINATES ARE THE SOURCE DOCUMENT'S OWN. The anchor contract says so at length: a stored
// pixel value would bake one viewer's zoom into the record. Converting is this module's job
// and it is the only job this module has.

/** The overlay was asked for something it cannot honestly draw. */
export class OverlayError extends Error {
  constructor(message) {
    super(message);
    this.name = "OverlayError";
  }
}

/**
 * An anchor row named a `kind` this front end has no geometry for.
 *
 * Thrown rather than returning "no highlight", for the reason `UnknownAnchorKind` in
 * `contracts/anchor.py` gives: a client older than the appliance meets a kind that arrived
 * with an office format, and the alternative is a highlight that silently never appears. The
 * answer is "upgrade the front end", not "the citation feature is broken for some documents".
 */
export class UnknownAnchorKind extends OverlayError {
  constructor(kind) {
    super(
      `anchor kind ${JSON.stringify(kind)} is not one of pdf, cells, span. This front end is ` +
        "older than the appliance that wrote it."
    );
    this.name = "UnknownAnchorKind";
    this.kind = kind;
  }
}

/** The five outcomes of asking where a passage is. Frozen so a caller can switch on them. */
export const HIGHLIGHT_STATUS = Object.freeze({
  /** A rectangle, in this surface's pixels. */
  BOX: "box",
  /** The passage runs through this page, but no rectangle was recorded for it here. */
  CONTINUED: "continued",
  /** The passage is on a different page of this document. */
  ELSEWHERE: "elsewhere",
  /** No anchor and no unresolved row: nothing was recorded, and nothing was lost. */
  NONE: "none",
  /** No anchor, and a row saying why one could not be recovered. Show the reason. */
  UNRESOLVED: "unresolved",
});

/**
 * IC-9. Where, on this surface, is the passage that anchor describes?
 *
 * @param {{anchor: object|null, unresolved: object|null}} evidence BOTH keys are required,
 *   even when both are null. A caller that passes only `anchor` has not said whether it
 *   looked for the unresolved row, and "I did not look" is not the same fact as "there was
 *   no reason recorded" — see this module's header. A search hit carries both fields
 *   (`SearchHit.source_anchor` and `.source_anchor_unresolved`) and so does an evidence read,
 *   so there is no caller for whom supplying both is work.
 * @param {{kind: string, source: {width: number, height: number},
 *          pixels: {width: number, height: number},
 *          originCorner: "top-left"|"bottom-left",
 *          page?: number, sheet?: string,
 *          cellRect?: (ref: string) => [number, number, number, number]}} surface
 * @returns {{status: string} & object}
 */
export function highlight(evidence, surface) {
  const { anchor, unresolved } = readEvidence(evidence);
  checkSurface(surface);

  if (anchor === null) {
    if (unresolved === null) return Object.freeze({ status: HIGHLIGHT_STATUS.NONE });
    // The reason is carried through verbatim. `code` is for the page to branch on and
    // `reason` is the sentence `citation_tasks.py:284` wrote for a human to read; a viewer
    // that showed only the code would have re-written a message that already exists.
    if (typeof unresolved.code !== "string" || typeof unresolved.reason !== "string") {
      throw new OverlayError(
        "a source_anchor.unresolved row is {code, reason}, both strings; got " +
          JSON.stringify(unresolved)
      );
    }
    return Object.freeze({
      status: HIGHLIGHT_STATUS.UNRESOLVED,
      code: unresolved.code,
      reason: unresolved.reason,
    });
  }

  switch (anchor.kind) {
    case "pdf":
      return pdfHighlight(anchor, surface);
    case "cells":
      return cellsHighlight(anchor, surface);
    case "span":
      // Not a refusal of a broken anchor — a span IS a valid anchor, and it is the one a
      // document with no geometry can offer. It has no rectangle by construction, so drawing
      // it is the text renderer's job on the text it is already showing (the same point
      // `SpanAnchor`'s docstring makes). Naming that here is what stops a viewer concluding
      // the highlight is missing.
      throw new OverlayError(
        `a span anchor (chars ${anchor.char_start}-${anchor.char_end}) has no geometry and ` +
          `cannot be drawn on a ${surface.kind} surface. Highlight it in the rendered text ` +
          "instead; see SpanAnchor in jmfts-client/jmfts_client/contracts/anchor.py."
      );
    default:
      throw new UnknownAnchorKind(anchor.kind);
  }
}

/**
 * Can this surface place this anchor at all?
 *
 * Offered so a viewer can CHOOSE a surface rather than discover its mistake as an exception —
 * a document detail view holding a cells anchor asks this before rendering a page image. It is
 * the same question `highlight()` answers; it just answers it without throwing.
 */
export function canPlace(anchor, surface) {
  try {
    highlight({ anchor, unresolved: null }, surface);
    return true;
  } catch (error) {
    if (error instanceof OverlayError) return false;
    throw error;
  }
}

// ------------------------------------------------------------------------ the one transform

/**
 * A source-space rectangle becomes a pixel box. THE ONLY PLACE THIS HAPPENS.
 *
 * `surface.kind` is not read here and must never be, because that is the whole of IC-9's
 * claim. The origin corner is DECLARED by the surface rather than assumed from its kind: a
 * page rendered by pymupdf is top-left with y increasing downward, which is also what CSS
 * wants, while a rectangle in PDF's own user space is bottom-left with y increasing upward.
 * Both are real and they differ by a flip, so a surface that did not say which it was would
 * be asking this function to guess.
 */
function toPixels(rect, surface) {
  const [x0, y0, x1, y1] = rect;
  const scaleX = surface.pixels.width / surface.source.width;
  const scaleY = surface.pixels.height / surface.source.height;
  const left = Math.min(x0, x1);
  const right = Math.max(x0, x1);
  const near = Math.min(y0, y1);
  const far = Math.max(y0, y1);
  const top = surface.originCorner === "top-left" ? near : surface.source.height - far;
  return Object.freeze({
    left: left * scaleX,
    top: top * scaleY,
    width: (right - left) * scaleX,
    height: (far - near) * scaleY,
  });
}

// ----------------------------------------------------------------------------- by kind

function pdfHighlight(anchor, surface) {
  if (!Array.isArray(anchor.bbox) || anchor.bbox.length !== 4 || anchor.bbox.some(notFinite)) {
    throw new OverlayError(
      `a pdf anchor's bbox is four finite numbers [x0, y0, x1, y1]; got ` +
        JSON.stringify(anchor.bbox)
    );
  }
  if (!Number.isInteger(anchor.page) || anchor.page < 0) {
    throw new OverlayError(`a pdf anchor's page is a 0-based integer; got ${anchor.page}`);
  }
  // The surface MUST say which page it is showing. Without it the box would be drawn on
  // whatever page happened to be on screen, which is a highlight that is confidently in the
  // wrong place — strictly worse than no highlight, and indistinguishable from a right one.
  if (!Number.isInteger(surface.page)) {
    throw new OverlayError(
      `a ${surface.kind} surface must declare which 0-based page it shows before a pdf ` +
        "anchor can be drawn on it. Undeclared, the box would land on whatever page is up."
    );
  }

  if (surface.page !== anchor.page) {
    const continues = Array.isArray(anchor.continues) ? anchor.continues : [];
    if (continues.includes(surface.page)) {
      // The passage runs through this page and the record holds no rectangle for it — the
      // anchor's box covers only the page the passage BEGINS on, which `anchor_for_span`
      // argues for at length. A viewer shows an edge marker or "continues from p. N" here.
      // What it must not do is draw the starting page's box on this one.
      return Object.freeze({
        status: HIGHLIGHT_STATUS.CONTINUED,
        page: surface.page,
        from: anchor.page,
      });
    }
    return Object.freeze({ status: HIGHLIGHT_STATUS.ELSEWHERE, page: anchor.page });
  }

  return Object.freeze({
    status: HIGHLIGHT_STATUS.BOX,
    box: toPixels(anchor.bbox, surface),
    // Carried through so a viewer can say "continues on p. 4" rather than presenting a
    // partial box as a whole one. Null — not an empty list — when the passage does not run on,
    // matching the anchor contract's "absent, not empty".
    continues: Array.isArray(anchor.continues) ? Object.freeze([...anchor.continues]) : null,
  });
}

function cellsHighlight(anchor, surface) {
  if (typeof anchor.sheet !== "string" || typeof anchor.ref !== "string") {
    throw new OverlayError(
      `a cells anchor is {sheet, ref}, both strings; got ${JSON.stringify(anchor)}`
    );
  }
  // A1 IS NOT PARSED HERE, and that is a decision the Python contract already took for the
  // same reason: "`jmfts_core/office/cells.py` owns that grammar and a second parser in the
  // client would be a second grammar" (CellsAnchor's docstring). A third one in JavaScript
  // would be worse again. The SURFACE resolves the ref, because the surface was built from
  // `GET /documents/{id}/cells` and therefore already knows which cells it is showing and
  // where each one landed. This module keeps what is genuinely shared — the transform and the
  // status vocabulary — and delegates what is genuinely the sheet's.
  if (typeof surface.cellRect !== "function") {
    throw new OverlayError(
      `a cells anchor needs a surface that can resolve an A1 range to its own coordinates; ` +
        `this ${surface.kind} surface declares no cellRect(ref).`
    );
  }
  if (surface.sheet !== anchor.sheet) {
    // Two worksheets of one workbook, and the wrong one on screen. `B4:H120` exists on both,
    // so a box would be drawn and it would be drawn over unrelated numbers.
    return Object.freeze({
      status: HIGHLIGHT_STATUS.ELSEWHERE,
      sheet: anchor.sheet,
      showing: surface.sheet ?? null,
    });
  }

  const rect = surface.cellRect(anchor.ref);
  if (rect === null || rect === undefined) {
    // The range is on this sheet and outside what this surface rendered — a region view
    // cropped to B4:H120 asked about AA900. Not a failure, and not a box either.
    return Object.freeze({ status: HIGHLIGHT_STATUS.ELSEWHERE, sheet: anchor.sheet, ref: anchor.ref });
  }
  if (!Array.isArray(rect) || rect.length !== 4 || rect.some(notFinite)) {
    throw new OverlayError(
      `the ${surface.kind} surface's cellRect("${anchor.ref}") returned ` +
        `${JSON.stringify(rect)}; it must return [x0, y0, x1, y1] in the surface's own source ` +
        "coordinates, or null when the range is outside what it rendered."
    );
  }
  return Object.freeze({ status: HIGHLIGHT_STATUS.BOX, box: toPixels(rect, surface), continues: null });
}

// ------------------------------------------------------------------------------ checking

const notFinite = (n) => typeof n !== "number" || !Number.isFinite(n);

function readEvidence(evidence) {
  if (evidence === null || typeof evidence !== "object") {
    throw new OverlayError(
      "highlight() takes {anchor, unresolved} as its first argument, both keys present even " +
        "when both are null. See this module's header for why the two absences differ."
    );
  }
  if (!("anchor" in evidence) || !("unresolved" in evidence)) {
    throw new OverlayError(
      `highlight() needs both "anchor" and "unresolved"; got {${Object.keys(evidence).join(", ")}}. ` +
        "A caller that passes only one has not said whether it looked for the other, and " +
        '"I did not look" is not "there was no reason recorded".'
    );
  }
  const { anchor, unresolved } = evidence;
  if (anchor !== null && unresolved !== null) {
    // `evidence.py:453` says the unresolved row is present exactly when the anchor is not.
    // Both at once is a contradiction the appliance should not be able to produce, and
    // silently preferring one of them is how it would stay unnoticed.
    throw new OverlayError(
      "a passage carries an anchor AND a source_anchor.unresolved row. jmfts_core/evidence.py" +
        ":453 says the second is present exactly when the first is not, so one of these two " +
        "evidence rows is wrong and the page cannot tell which."
    );
  }
  if (anchor !== null && (typeof anchor !== "object" || Array.isArray(anchor))) {
    throw new UnknownAnchorKind(Array.isArray(anchor) ? "array" : typeof anchor);
  }
  return { anchor: anchor ?? null, unresolved: unresolved ?? null };
}

function checkSurface(surface) {
  if (surface === null || typeof surface !== "object") {
    throw new OverlayError("highlight() needs a surface as its second argument");
  }
  for (const [name, value] of [
    ["source.width", surface.source?.width],
    ["source.height", surface.source?.height],
    ["pixels.width", surface.pixels?.width],
    ["pixels.height", surface.pixels?.height],
  ]) {
    if (notFinite(value) || value <= 0) {
      throw new OverlayError(
        `a surface declares its source extent and its rendered size in pixels; ${name} is ` +
          `${JSON.stringify(value)}. Both are needed: their ratio IS the scale, and a surface ` +
          "that did not carry it would be asking this module to infer one."
      );
    }
  }
  if (surface.originCorner !== "top-left" && surface.originCorner !== "bottom-left") {
    // No default. A default here is a coin flip on every box's vertical position, and the
    // sprint plan names exactly this ("which corner the origin is in") as the disagreement
    // that writing the overlay three times produces. Writing it once does not help if the one
    // copy guesses.
    throw new OverlayError(
      'a surface must declare originCorner as "top-left" or "bottom-left"; got ' +
        `${JSON.stringify(surface.originCorner)}. A page rendered by pymupdf is top-left with ` +
        "y downward; a rectangle in PDF user space is bottom-left with y upward. They differ " +
        "by a flip and nothing here can tell them apart."
    );
  }
  if (typeof surface.kind !== "string" || !surface.kind) {
    throw new OverlayError("a surface names its kind, which is what error messages call it");
  }
}
