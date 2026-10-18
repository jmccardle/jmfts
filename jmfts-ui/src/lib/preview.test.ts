import { describe, expect, it, vi } from "vitest";
import {
  EDGE_TOLERANCE_PT,
  fetchAtDpi,
  offersPreview,
  overlayBox,
  pageLabel,
  pagePoints,
  readAnchor,
  readCellRef,
  refusalOf,
  sourceFormat,
  suggestedDpi,
} from "./preview";

// The rectangles below are not invented. They are the `source_anchor` rows the appliance wrote
// when four generated PDFs of the same three paragraphs were ingested: Letter (612 x 792) upright,
// the same page with /Rotate 90, with /Rotate 270, and with /Rotate 90 plus a CropBox of
// (36, 36, 560, 700). A rotated page's anchor is in DISPLAYED space (#38), so its numbers fit the
// turned page, which is 792 x 612.

const UPRIGHT = [72.0, 88.0, 387.48, 135.61] as const;
const ROTATED_90 = [656.39, 72.0, 704.0, 387.48] as const;
const ROTATED_270 = [88.0, 224.52, 135.61, 540.0] as const;
const ROTATED_90_CROPPED = [564.39, 36.0, 612.0, 351.48] as const;

// Two of `citation_tasks.UNRESOLVED_REASON`, word for word.
const NO_BLOCK =
  "the chunk's `source_span` overlaps none of the text blocks the extraction " +
  "measured, so nothing in the source accounts for it";
const NOT_IN_RENDITION =
  "none of the blocks the chunk covers could be matched against text in the rendition, so " +
  "there is no page and no rectangle to point at. The renderer reflowed, hyphenated or " +
  "dropped those characters, or the passage is text the reader wrote rather than text the " +
  "document holds";

describe("readAnchor", () => {
  it("reads a pdf anchor", () => {
    const reading = readAnchor({ source_anchor: { kind: "pdf", page: 2, bbox: [1, 2, 30, 40] } });
    expect(reading).toEqual({
      state: "pdf",
      anchor: { page: 2, bbox: [1, 2, 30, 40], of: "source", continues: [] },
    });
  });

  // A Word chunk as the appliance leaves it: `anchor:source` writes the package address to
  // `source_anchor` (citation_tasks.py, `_ooxml_anchor`) and `anchor:render` writes the
  // recovered rectangle to a SECOND row, `render_anchor`, with `of: "rendition"`
  // (`anchor_for_rendition_span`). 0.7.0's fixture put `of: "rendition"` under `source_anchor`,
  // a row the server never writes, so the precedence was never tested.
  const WORD_CHUNK = {
    source_anchor: { kind: "ooxml", part: "word/document.xml", path: "/w:document/w:body/w:p[42]" },
    render_anchor: { kind: "pdf", of: "rendition", page: 2, bbox: [72.0, 118.4, 540.0, 262.9], continues: [3] },
  };

  it("prefers the rendition's rectangle over the package address, as _pdf_anchor does", () => {
    expect(readAnchor(WORD_CHUNK)).toEqual({
      state: "pdf",
      anchor: { page: 2, bbox: [72.0, 118.4, 540.0, 262.9], of: "rendition", continues: [3] },
    });
  });

  it("says why when the rendition was searched and the passage was not in it", () => {
    const reading = readAnchor({
      source_anchor: WORD_CHUNK.source_anchor,
      "render_anchor.unresolved": { code: "not_found_in_rendition", reason: NOT_IN_RENDITION },
    });
    expect(reading).toEqual({
      state: "unresolved",
      row: "render_anchor",
      code: "not_found_in_rendition",
      reason: NOT_IN_RENDITION,
    });
  });

  it("falls back to the package address when nothing has paginated the document", () => {
    expect(readAnchor({ source_anchor: WORD_CHUNK.source_anchor })).toEqual({
      state: "other",
      kind: "ooxml",
    });
  });

  it("refuses a render_anchor that is not a rectangle, rather than reading the source row", () => {
    const reading = readAnchor({
      ...WORD_CHUNK,
      render_anchor: { kind: "ooxml", part: "word/document.xml", path: "/w:p[1]" },
    });
    expect(reading.state).toBe("malformed");
  });

  it("refuses a row whose `of` contradicts the row it is stored in", () => {
    expect(
      readAnchor({ source_anchor: { kind: "pdf", of: "rendition", page: 0, bbox: [1, 2, 3, 4] } }).state
    ).toBe("malformed");
    expect(
      readAnchor({ render_anchor: { kind: "pdf", of: "source", page: 0, bbox: [1, 2, 3, 4] } }).state
    ).toBe("malformed");
  });

  it("says absent for a node that never had one, whether the key is missing or null", () => {
    expect(readAnchor({})).toEqual({ state: "absent" });
    expect(readAnchor({ source_anchor: null })).toEqual({ state: "absent" });
    expect(readAnchor({ source_anchor: null, render_anchor: null })).toEqual({ state: "absent" });
    expect(readAnchor(null)).toEqual({ state: "absent" });
  });

  it("carries the reason when the anchor was attempted and not recovered", () => {
    // The shape `citation` writes: `{"code": code, "reason": UNRESOLVED_REASON[code]}`.
    expect(
      readAnchor({
        source_anchor: null,
        "source_anchor.unresolved": { code: "no_overlapping_block", reason: NO_BLOCK },
      })
    ).toEqual({ state: "unresolved", row: "source_anchor", code: "no_overlapping_block", reason: NO_BLOCK });
  });

  it("reads a cells anchor as the sheet and range it names", () => {
    // `sheet_tasks._cells_anchor`, on a sheet's table, records and profile.
    expect(readAnchor({ source_anchor: { kind: "cells", sheet: "Q3", ref: "A1:F40" } })).toEqual({
      state: "cells",
      sheet: "Q3",
      ref: "A1:F40",
    });
    expect(readAnchor({ source_anchor: { kind: "cells", sheet: "Q3" } }).state).toBe("malformed");
  });

  it("reports an anchor this view cannot show instead of drawing it", () => {
    expect(readAnchor({ source_anchor: { kind: "span", char_start: 0, char_end: 9 } })).toEqual({
      state: "other",
      kind: "span",
    });
  });

  it("refuses a malformed rectangle rather than drawing part of it", () => {
    for (const bbox of [[1, 2, 3], [1, 2, 3, Number.NaN], [10, 10, 5, 20], [1, 1, 9, 1], "1,2,3,4"]) {
      expect(readAnchor({ source_anchor: { kind: "pdf", page: 0, bbox } }).state).toBe("malformed");
    }
    expect(readAnchor({ source_anchor: { kind: "pdf", page: -1, bbox: [1, 2, 3, 4] } }).state).toBe(
      "malformed"
    );
    expect(readAnchor({ source_anchor: { kind: "pdf", page: 1.5, bbox: [1, 2, 3, 4] } }).state).toBe(
      "malformed"
    );
  });
});

describe("pageLabel", () => {
  it("shifts the stored 0-based page for a person", () => {
    expect(pageLabel(0)).toBe("page 1");
    expect(pageLabel(3)).toBe("page 4");
  });
});

describe("pagePoints", () => {
  it("recovers the page in points from the image and the density asked for", () => {
    const upright = pagePoints(850, 1100, 100);
    expect(upright.width).toBeCloseTo(612);
    expect(upright.height).toBeCloseTo(792);
    const turned = pagePoints(1100, 850, 100);
    expect(turned.width).toBeCloseTo(792);
    expect(turned.height).toBeCloseTo(612);
  });

  it("refuses a page with no size instead of dividing by it", () => {
    expect(() => pagePoints(0, 100, 100)).toThrow(/no size/);
    expect(() => pagePoints(100, 100, 0)).toThrow(/no size/);
  });
});

describe("overlayBox", () => {
  const letter = { width: 612, height: 792 };
  // /Rotate 90 and /Rotate 270 both display as 792 x 612.
  const turned = { width: 792, height: 612 };
  // The CropBox page, displayed: 524 x 664 turned by 90 is 664 x 524.
  const croppedTurned = { width: 664, height: 524 };

  it("scales an upright anchor into fractions of the page", () => {
    const box = overlayBox(UPRIGHT, letter);
    expect(box.left).toBeCloseTo((72 / 612) * 100);
    expect(box.top).toBeCloseTo((88 / 792) * 100);
    expect(box.width).toBeCloseTo(((387.48 - 72) / 612) * 100);
    expect(box.height).toBeCloseTo(((135.61 - 88) / 792) * 100);
    expect(box.placement).toBe("inside");
  });

  it("is the same box at any image size, because it is a fraction of the page", () => {
    // 100 dpi and 150 dpi renders of one page are different images and one rectangle.
    const a = overlayBox(UPRIGHT, pagePoints(850, 1100, 100));
    const b = overlayBox(UPRIGHT, pagePoints(1275, 1650, 150));
    expect(a.left).toBeCloseTo(b.left);
    expect(a.width).toBeCloseTo(b.width);
    expect(a.top).toBeCloseTo(b.top);
    expect(a.height).toBeCloseTo(b.height);
  });

  it("puts a /Rotate 90 anchor inside the turned page, with no rotation applied here", () => {
    const box = overlayBox(ROTATED_90, turned);
    expect(box.placement).toBe("inside");
    // A narrow tall strip near the right edge: the text runs down the turned page.
    expect(box.left).toBeGreaterThan(80);
    expect(box.width).toBeLessThan(10);
    expect(box.height).toBeGreaterThan(40);
  });

  it("puts a /Rotate 270 anchor inside the turned page, near the left edge", () => {
    const box = overlayBox(ROTATED_270, turned);
    expect(box.placement).toBe("inside");
    expect(box.left).toBeLessThan(20);
    expect(box.width).toBeLessThan(10);
    expect(box.height).toBeGreaterThan(40);
  });

  it("reads a rotated, cropped page in the cropped page's own frame", () => {
    const box = overlayBox(ROTATED_90_CROPPED, croppedTurned);
    expect(box.placement).toBe("inside");
    expect(box.left + box.width).toBeCloseTo((612 / 664) * 100);
    expect(box.top).toBeCloseTo((36 / 524) * 100);
  });

  it("says so when an anchor measured on the unrotated page is placed on the turned one", () => {
    // What an anchor looked like before #38: unrotated y values run past a 612-high turned page.
    const box = overlayBox([72, 640, 400, 700], turned);
    expect(box.placement).toBe("outside");
  });

  it("clips a rectangle that hangs over the edge and reports it as partly", () => {
    const box = overlayBox([500, 700, 700, 900], letter);
    expect(box.placement).toBe("partly");
    expect(box.left + box.width).toBeCloseTo(100);
    expect(box.top + box.height).toBeCloseTo(100);
  });

  it("does not alarm over a glyph that straddles a CropBox edge", () => {
    // x0 of -5 to -8 is what the audit measured on CropBox pages; the box is still clipped.
    for (const x0 of [-5, -8, -EDGE_TOLERANCE_PT]) {
      const box = overlayBox([x0, 88, 387.48, 135.61], letter);
      expect(box.placement).toBe("inside");
      expect(box.left).toBe(0);
    }
    expect(overlayBox([72, 88, 612 + 7, 135.61], letter).placement).toBe("inside");
  });

  it("still reports an overhang beyond the tolerance", () => {
    expect(overlayBox([-EDGE_TOLERANCE_PT - 1, 88, 387.48, 135.61], letter).placement).toBe("partly");
    expect(overlayBox([72, 88, 387.48, 792 + EDGE_TOLERANCE_PT + 1], letter).placement).toBe("partly");
  });
});

describe("offersPreview", () => {
  it("offers a file node, whose document is the thing to look at", () => {
    expect(offersPreview("file", {})).toBe(true);
  });

  it("offers a sheet node, whose used range is the thing to look at", () => {
    expect(offersPreview("sheet", {})).toBe(true);
  });

  it("offers a node whose anchor is a pdf rectangle", () => {
    expect(offersPreview("chunk", { source_anchor: { kind: "pdf", page: 0, bbox: [0, 0, 1, 1] } })).toBe(
      true
    );
  });

  it("offers a Word or PowerPoint chunk through its rendition anchor", () => {
    expect(
      offersPreview("chunk", {
        source_anchor: { kind: "ooxml", part: "ppt/slides/slide3.xml", path: "/p:sld/p:cSld", slide: 3 },
        render_anchor: { kind: "pdf", of: "rendition", page: 2, bbox: [36, 40, 684, 120] },
      })
    ).toBe(true);
  });

  it("offers a spreadsheet region, which is shown as cells", () => {
    expect(offersPreview("record", { source_anchor: { kind: "cells", sheet: "S", ref: "A4:F4" } })).toBe(true);
  });

  it("does not offer a page that would only say there is nothing", () => {
    expect(offersPreview("chunk", {})).toBe(false);
    expect(offersPreview("chunk", null)).toBe(false);
    expect(offersPreview("chunk", { source_anchor: { kind: "ooxml", part: "word/document.xml", path: "/w:p[1]" } })).toBe(
      false
    );
  });
});

describe("readCellRef", () => {
  it("reads a rectangle and a single cell", () => {
    expect(readCellRef("B4:H120")).toEqual([
      { column: "B", row: 4 },
      { column: "H", row: 120 },
    ]);
    expect(readCellRef("$c$7")).toEqual([
      { column: "C", row: 7 },
      { column: "C", row: 7 },
    ]);
  });

  it("refuses what is not a letter-and-number pair", () => {
    for (const ref of ["A1:B2:C3", "R1C1", "Sheet1!A1", ""]) {
      expect(() => readCellRef(ref)).toThrow();
    }
  });
});

describe("sourceFormat", () => {
  it("names the formats an anchor writer exists for, and nothing else", () => {
    expect(sourceFormat("application/pdf")).toBe("pdf");
    expect(sourceFormat("application/vnd.openxmlformats-officedocument.wordprocessingml.document")).toBe(
      "office"
    );
    expect(sourceFormat("application/vnd.ms-powerpoint")).toBe("office");
    expect(sourceFormat("text/markdown")).toBe("other");
    expect(sourceFormat(null)).toBe("unrecognised");
  });
});

// The two sentences `rendering._check_bounds` ends a 413 with, word for word.
const TOO_MANY_PIXELS =
  "rendering 2384x3370 points at 150 dpi is 34.7 Mpx, above this appliance's maximum of 25 Mpx. " +
  "Retry at 127 dpi or below";
const ABOVE_DPI_MAX =
  "dpi 900 is above this appliance's maximum of 600; nothing consumes a higher density and the " +
  "allocation is the caller's to choose. Retry at 600 or below";

describe("suggestedDpi", () => {
  it("reads the density both 413 sentences suggest", () => {
    expect(suggestedDpi({ status: 413, detail: TOO_MANY_PIXELS })).toBe(127);
    expect(suggestedDpi({ status: 413, detail: ABOVE_DPI_MAX })).toBe(600);
  });

  it("invents nothing for a 413 that names no density, or for any other status", () => {
    expect(suggestedDpi({ status: 413, detail: "too large" })).toBeNull();
    expect(suggestedDpi({ status: 413, detail: "Retry at 0 dpi or below" })).toBeNull();
    expect(suggestedDpi({ status: 400, detail: TOO_MANY_PIXELS })).toBeNull();
  });
});

describe("fetchAtDpi", () => {
  const refusal = (status: number, detail: string) =>
    Object.assign(new Error(`HTTP ${status}`), { event: { status, response: { detail } } });

  it("retries once at the suggested density and says which density it used", async () => {
    const asked: number[] = [];
    const image = new Blob(["png"]);
    const result = await fetchAtDpi(async (dpi) => {
      asked.push(dpi);
      if (dpi > 127) throw refusal(413, TOO_MANY_PIXELS);
      return image;
    }, 150);
    expect(asked).toEqual([150, 127]);
    expect(result).toEqual({ blob: image, dpi: 127 });
  });

  it("throws a 413 that names no density as it came, without a retry", async () => {
    const failure = refusal(413, "too large");
    const fetch = vi.fn(async () => {
      throw failure;
    });
    await expect(fetchAtDpi(fetch, 100)).rejects.toBe(failure);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("does not retry twice, and does not retry upward", async () => {
    const fetch = vi.fn(async () => {
      throw refusal(413, TOO_MANY_PIXELS);
    });
    await expect(fetchAtDpi(fetch, 150)).rejects.toMatchObject({ event: { status: 413 } });
    expect(fetch).toHaveBeenCalledTimes(2);
    fetch.mockClear();
    await expect(fetchAtDpi(fetch, 100)).rejects.toMatchObject({ event: { status: 413 } });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

describe("refusalOf", () => {
  it("returns the server's detail verbatim, without the client's path-and-status wrapper", () => {
    const detail =
      "this node's anchor, bbox (1.0, 2.0, 3.0, 4.0) on page 0, crops blank paper: nothing is printed inside it";
    const failure = Object.assign(new Error(`GET /documents/9/region — HTTP 400: ${detail}`), {
      event: { status: 400, response: { detail } },
    });
    expect(refusalOf(failure)).toEqual({ status: 400, detail });
  });

  it("keeps a structured detail as the JSON the server sent", () => {
    const failure = { event: { status: 422, response: { detail: [{ loc: ["query", "dpi"] }] } } };
    expect(refusalOf(failure)).toEqual({ status: 422, detail: '[{"loc":["query","dpi"]}]' });
  });

  it("has no status for a call that never reached the appliance", () => {
    expect(refusalOf(new Error("Failed to fetch"))).toEqual({
      status: null,
      detail: "Failed to fetch",
    });
  });
});
