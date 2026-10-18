// @vitest-environment jsdom
//
// Preview.vue against a stubbed appliance. The evidence rows are the shapes the server writes
// (`citation_tasks.py`, `sheet_tasks._cells_anchor`), and a refusal is the generated client's
// error: a message for a log and the call event with the server's `detail`.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, RouterLinkStub } from "@vue/test-utils";
import { ref } from "vue";

const client = vi.hoisted(() => ({
  get_document: vi.fn(),
  get_document_evidence: vi.fn(),
  get_document_region: vi.fn(),
  get_document_image: vi.fn(),
  get_document_cells: vi.fn(),
  get_ancestors: vi.fn(),
}));

vi.mock("@/api", () => ({ client, baseUrl: ref(""), token: ref("t") }));

import Preview from "./Preview.vue";

function refusal(status: number, detail: string) {
  return Object.assign(new Error(`GET /x — HTTP ${status}: ${detail}`), {
    event: { status, response: { detail } },
  });
}

function nodeOf(id: number, usetype: string, extra: Record<string, unknown> = {}) {
  return { id, usetype, title: `node ${id}`, content: "The quarterly figures.", parent_id: null, ...extra };
}

function given(node: ReturnType<typeof nodeOf>, evidence: Record<string, unknown>) {
  client.get_document.mockResolvedValue(node);
  client.get_document_evidence.mockImplementation(async ({ document_id }: { document_id: number }) =>
    document_id === node.id ? { document_id, evidence } : fileEvidence(document_id)
  );
}

let fileMime: string | null = "application/pdf";
function fileEvidence(document_id: number) {
  return { document_id, evidence: { file: { filename: "f", detected_mime: fileMime } } };
}

async function render(id: number) {
  const wrapper = mount(Preview, {
    props: { id },
    global: { stubs: { RouterLink: RouterLinkStub } },
  });
  await flushPromises();
  return wrapper;
}

/** jsdom loads no images, so the page's natural size is set and its load event fired by hand. */
async function pageLoads(wrapper: Awaited<ReturnType<typeof render>>, width: number, height: number) {
  const image = wrapper.find(".sheet img");
  Object.defineProperty(image.element, "naturalWidth", { value: width });
  Object.defineProperty(image.element, "naturalHeight", { value: height });
  await image.trigger("load");
}

const PNG = new Blob(["png"], { type: "image/png" });

beforeEach(() => {
  for (const fn of Object.values(client)) fn.mockReset();
  fileMime = "application/pdf";
  let n = 0;
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => `blob:${++n}`, revokeObjectURL: () => {} }));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Preview.vue", () => {
  it("draws a Word chunk's render_anchor on its rendition page, marked approximate", async () => {
    given(nodeOf(41, "chunk"), {
      source_anchor: { kind: "ooxml", part: "word/document.xml", path: "/w:document/w:body/w:p[42]" },
      render_anchor: { kind: "pdf", of: "rendition", page: 2, bbox: [72.0, 118.4, 540.0, 262.9] },
    });
    client.get_document_region.mockResolvedValue(PNG);
    client.get_document_image.mockResolvedValue(PNG);

    const wrapper = await render(41);

    expect(client.get_document_region).toHaveBeenCalledWith({ document_id: 41, anchor: true, dpi: 150 });
    expect(client.get_document_image).toHaveBeenCalledWith({ document_id: 41, page: 2, dpi: 100 });
    expect(wrapper.text()).toContain("On page 3");
    expect(wrapper.text()).toContain("approximate · our rendering");
    await pageLoads(wrapper, 850, 1100);
    const box = wrapper.find(".box").attributes("style");
    expect(box).toContain(`left: ${(72 / 612) * 100}%`);
    expect(wrapper.find(".mismatch").exists()).toBe(false);
  });

  it("shows a record's cells anchor as a grid, read from the sheet above it", async () => {
    given(nodeOf(77, "record"), { source_anchor: { kind: "cells", sheet: "Q3", ref: "A4:B5" } });
    client.get_ancestors.mockResolvedValue([nodeOf(70, "file"), nodeOf(71, "sheet"), nodeOf(72, "table")]);
    client.get_document_cells.mockResolvedValue({
      document_id: 71,
      sheet: "Q3",
      ref: "A4:B5",
      ref_source: "request",
      columns: ["A", "B"],
      rows: [{ row: 4, values: ["North", 1200] }],
      cells: { B4: { formula: "=SUM(C4:F4)", formula_shared: false, text_forced: false } },
      markdown: "",
      row_count: 1,
      cell_count: 4,
    });

    const wrapper = await render(77);

    expect(client.get_document_cells).toHaveBeenCalledWith({ document_id: 71, ref: "A4:B5" });
    expect(client.get_document_region).not.toHaveBeenCalled();
    expect(client.get_document_image).not.toHaveBeenCalled();
    const rows = wrapper.findAll("tbody tr");
    expect(rows.map((r) => r.find("th").text())).toEqual(["4", "5"]);
    expect(rows[0].findAll("td").map((c) => c.text())).toEqual(["North", "1200"]);
    expect(rows[0].findAll("td")[1].attributes("title")).toBe("=SUM(C4:F4)");
    expect(rows[1].findAll("td").map((c) => c.text())).toEqual(["", ""]);
  });

  it("refuses to show cells served for a different sheet than the anchor names", async () => {
    given(nodeOf(77, "record"), { source_anchor: { kind: "cells", sheet: "Q3", ref: "A4:B5" } });
    client.get_ancestors.mockResolvedValue([nodeOf(70, "file"), nodeOf(71, "sheet")]);
    client.get_document_cells.mockResolvedValue({
      document_id: 71, sheet: "Q4", ref: "A4:B5", ref_source: "request", columns: ["A", "B"],
      rows: [], cells: {}, markdown: "", row_count: 0, cell_count: 4,
    });

    const wrapper = await render(77);

    expect(wrapper.find("[role=alert]").text()).toContain('names sheet "Q3"');
    expect(wrapper.find("table").exists()).toBe(false);
  });

  it("shows the cells' refusal verbatim and draws no grid", async () => {
    given(nodeOf(77, "record"), { source_anchor: { kind: "cells", sheet: "Q3", ref: "A1:ZZ9999" } });
    client.get_ancestors.mockResolvedValue([nodeOf(71, "sheet")]);
    const detail = "ref A1:ZZ9999 names 7,013,298 cells, above this appliance's maximum of 50,000";
    client.get_document_cells.mockRejectedValue(refusal(413, detail));

    const wrapper = await render(77);

    const alert = wrapper.find(".refused");
    expect(alert.text()).toContain("HTTP 413");
    expect(alert.find(".detail").text()).toBe(detail);
    expect(wrapper.find("table").exists()).toBe(false);
  });

  it("reads a sheet node's used range when it has no anchor of its own", async () => {
    given(nodeOf(71, "sheet"), {});
    client.get_document_cells.mockResolvedValue({
      document_id: 71, sheet: "Q3", ref: "A1:A1", ref_source: "used_range", columns: ["A"],
      rows: [{ row: 1, values: ["x"] }], cells: {}, markdown: "", row_count: 1, cell_count: 1,
    });

    const wrapper = await render(71);

    expect(client.get_ancestors).not.toHaveBeenCalled();
    expect(client.get_document_cells).toHaveBeenCalledWith({ document_id: 71, ref: null });
    expect(wrapper.find("tbody td").text()).toBe("x");
  });

  it("explains an ooxml address with no rendition, and fetches no picture for it", async () => {
    given(nodeOf(41, "chunk"), {
      source_anchor: { kind: "ooxml", part: "word/document.xml", path: "/w:document/w:body/w:p[42]" },
    });

    const wrapper = await render(41);

    expect(wrapper.text()).toContain("anchor:render");
    expect(client.get_document_region).not.toHaveBeenCalled();
    expect(client.get_document_image).not.toHaveBeenCalled();
  });

  it("fetches no page for a file node whose anchor is unresolved, other or malformed", async () => {
    for (const evidence of [
      { "render_anchor.unresolved": { code: "not_found_in_rendition", reason: "not found" } },
      { source_anchor: { kind: "span", char_start: 0, char_end: 9 } },
      { source_anchor: { kind: "pdf", page: 0, bbox: [1, 2, 3] } },
    ]) {
      given(nodeOf(70, "file"), evidence);
      await render(70);
    }
    expect(client.get_document_image).not.toHaveBeenCalled();
    expect(client.get_document_region).not.toHaveBeenCalled();
  });

  it("says which row failed when a rendition anchor is unresolved", async () => {
    given(nodeOf(41, "chunk"), {
      source_anchor: { kind: "ooxml", part: "word/document.xml", path: "/w:p[1]" },
      "render_anchor.unresolved": { code: "not_found_in_rendition", reason: "matched nothing" },
    });

    const wrapper = await render(41);

    const line = wrapper.find("[data-unresolved=render_anchor]");
    expect(line.text()).toContain("PDF it rendered");
    expect(line.text()).toContain("matched nothing");
    expect(line.text()).toContain("not_found_in_rendition");
  });

  it("shows a refused crop verbatim and no picture in its place", async () => {
    given(nodeOf(9, "chunk"), { source_anchor: { kind: "pdf", page: 0, bbox: [1, 2, 3, 4] } });
    const detail = "this node's anchor, bbox (1.0, 2.0, 3.0, 4.0) on page 0, crops blank paper";
    client.get_document_region.mockRejectedValue(refusal(400, detail));

    const wrapper = await render(9);

    expect(wrapper.find(".refused h2").text()).toContain("the crop of the anchor");
    expect(wrapper.find(".refused .detail").text()).toBe(detail);
    expect(wrapper.find("img").exists()).toBe(false);
    expect(client.get_document_image).not.toHaveBeenCalled();
  });

  it("retries a 413 page at the density it names, and scales the box by that density", async () => {
    given(nodeOf(9, "chunk"), { source_anchor: { kind: "pdf", page: 0, bbox: [72, 88, 387.48, 135.61] } });
    client.get_document_region.mockResolvedValue(PNG);
    client.get_document_image.mockImplementation(async ({ dpi }: { dpi: number }) => {
      if (dpi > 72) throw refusal(413, "rendering ... is 40.0 Mpx, above this appliance's maximum of 25 Mpx. Retry at 72 dpi or below");
      return PNG;
    });

    const wrapper = await render(9);

    expect(client.get_document_image.mock.calls.map((c) => c[0].dpi)).toEqual([100, 72]);
    expect(wrapper.find("[data-retried=page]").text()).toContain("72 dpi");
    // 612 x 792 pixels at 72 dpi is a Letter page; at the 100 asked for it would be 440 x 570.
    await pageLoads(wrapper, 612, 792);
    expect(wrapper.find(".box").attributes("style")).toContain(`left: ${(72 / 612) * 100}%`);
    expect(wrapper.find(".mismatch").exists()).toBe(false);
  });

  it("shows a 413 that names no density as it came", async () => {
    given(nodeOf(9, "chunk"), { source_anchor: { kind: "pdf", page: 0, bbox: [1, 2, 30, 40] } });
    client.get_document_region.mockResolvedValue(PNG);
    client.get_document_image.mockRejectedValue(refusal(413, "too large"));

    const wrapper = await render(9);

    expect(client.get_document_image).toHaveBeenCalledTimes(1);
    expect(wrapper.find(".refused .detail").text()).toBe("too large");
    expect(wrapper.find("img").exists()).toBe(false);
  });

  it("raises no edge alarm for a glyph straddling the CropBox edge", async () => {
    given(nodeOf(9, "chunk"), { source_anchor: { kind: "pdf", page: 0, bbox: [-6.5, 88, 387.48, 135.61] } });
    client.get_document_region.mockResolvedValue(PNG);
    client.get_document_image.mockResolvedValue(PNG);

    const wrapper = await render(9);
    await pageLoads(wrapper, 850, 1100);

    expect(wrapper.find(".box").exists()).toBe(true);
    expect(wrapper.find(".mismatch").exists()).toBe(false);
  });

  describe("a node with no anchor is told why, for its format", () => {
    it("points a node inside a worksheet at the sheet's cells", async () => {
      given(nodeOf(80, "section"), {});
      client.get_ancestors.mockResolvedValue([nodeOf(70, "file"), nodeOf(71, "sheet")]);

      const wrapper = await render(80);

      const line = wrapper.find("[data-absent=sheet]");
      expect(line.findComponent(RouterLinkStub).props("to")).toBe("/node/71/preview");
      expect(wrapper.text()).not.toContain("citation");
    });

    it("names the rendition path for an office document", async () => {
      fileMime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
      given(nodeOf(80, "section"), {});
      client.get_ancestors.mockResolvedValue([nodeOf(70, "file")]);

      const wrapper = await render(80);

      expect(client.get_document_evidence).toHaveBeenCalledWith({ document_id: 70 });
      expect(wrapper.find("[data-absent=office]").text()).toContain("render:pdf");
      expect(wrapper.text()).not.toContain("citation");
    });

    it("names citation for a PDF", async () => {
      given(nodeOf(80, "section"), {});
      client.get_ancestors.mockResolvedValue([nodeOf(70, "file")]);

      const wrapper = await render(80);

      expect(wrapper.find("[data-absent=pdf]").text()).toContain("citation");
    });

    it("says so for a node under no uploaded file", async () => {
      given(nodeOf(80, "note"), {});
      client.get_ancestors.mockResolvedValue([]);

      const wrapper = await render(80);

      expect(wrapper.find("[data-absent=none]").exists()).toBe(true);
    });
  });
});
