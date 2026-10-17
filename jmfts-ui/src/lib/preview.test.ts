import { describe, expect, it } from "vitest";
import { offersPreview, overlayBox, pageLabel, pagePoints, readAnchor, refusalOf } from "./preview";

// The rectangles below are not invented. They are the `source_anchor` rows the appliance wrote
// when four generated PDFs of the same three paragraphs were ingested: Letter (612 x 792) upright,
// the same page with /Rotate 90, with /Rotate 270, and with /Rotate 90 plus a CropBox of
// (36, 36, 560, 700). A rotated page's anchor is in DISPLAYED space (#38), so its numbers fit the
// turned page, which is 792 x 612.

const UPRIGHT = [72.0, 88.0, 387.48, 135.61] as const;
const ROTATED_90 = [656.39, 72.0, 704.0, 387.48] as const;
const ROTATED_270 = [88.0, 224.52, 135.61, 540.0] as const;
const ROTATED_90_CROPPED = [564.39, 36.0, 612.0, 351.48] as const;

describe("readAnchor", () => {
  it("reads a pdf anchor", () => {
    const reading = readAnchor({ source_anchor: { kind: "pdf", page: 2, bbox: [1, 2, 30, 40] } });
    expect(reading).toEqual({
      state: "pdf",
      anchor: { page: 2, bbox: [1, 2, 30, 40], of: "source", continues: [] },
    });
  });

  it("keeps a rendition anchor distinguishable and the pages it continues onto", () => {
    const reading = readAnchor({
      source_anchor: { kind: "pdf", page: 0, bbox: [1, 2, 3, 4], of: "rendition", continues: [1, 2] },
    });
    expect(reading.state === "pdf" && reading.anchor.of).toBe("rendition");
    expect(reading.state === "pdf" && reading.anchor.continues).toEqual([1, 2]);
  });

  it("says absent for a node that never had one, whether the key is missing or null", () => {
    expect(readAnchor({})).toEqual({ state: "absent" });
    expect(readAnchor({ source_anchor: null })).toEqual({ state: "absent" });
    expect(readAnchor(null)).toEqual({ state: "absent" });
  });

  it("carries the reason when the anchor was attempted and not recovered", () => {
    expect(
      readAnchor({ source_anchor: null, "source_anchor.unresolved": { reason: "no text layer" } })
    ).toEqual({ state: "unresolved", reason: "no text layer" });
  });

  it("reports an anchor that is not a pdf rectangle instead of drawing it", () => {
    expect(readAnchor({ source_anchor: { kind: "cells", sheet: "S", ref: "A1:B2" } })).toEqual({
      state: "other",
      kind: "cells",
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
});

describe("offersPreview", () => {
  it("offers a file node, whose document is the thing to look at", () => {
    expect(offersPreview("file", {})).toBe(true);
  });

  it("offers a node whose anchor is a pdf rectangle", () => {
    expect(offersPreview("chunk", { source_anchor: { kind: "pdf", page: 0, bbox: [0, 0, 1, 1] } })).toBe(
      true
    );
  });

  it("does not offer a page that would only say there is nothing", () => {
    expect(offersPreview("chunk", {})).toBe(false);
    expect(offersPreview("chunk", null)).toBe(false);
    expect(offersPreview("chunk", { source_anchor: { kind: "cells", sheet: "S", ref: "A1" } })).toBe(false);
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
