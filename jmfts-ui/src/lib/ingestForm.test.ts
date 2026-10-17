import { describe, expect, it } from "vitest";
import {
  checkName,
  editsFromJson,
  extensionOf,
  fieldKind,
  mergeOptions,
  optionFields,
  parseOptions,
  parseParentId,
  sourceProblem,
} from "./ingestForm";

const RESOLVED = {
  structure: { max_words: 120, min_chunk_length: 20, chunk_strategy: "sentence", overlap: 0.1 },
  facts: { enabled: false, model: null },
};

describe("option fields", () => {
  it("come from the resolved options, typed by their resolved value", () => {
    expect(fieldKind(true)).toBe("boolean");
    expect(fieldKind(120)).toBe("number");
    expect(fieldKind(0.1)).toBe("number");
    expect(fieldKind("sentence")).toBe("text");
    expect(fieldKind(null)).toBe("json");
    expect(fieldKind([1])).toBe("json");
    expect(optionFields(RESOLVED).map((f) => f.key)).toEqual([
      "facts.enabled",
      "facts.model",
      "structure.chunk_strategy",
      "structure.max_words",
      "structure.min_chunk_length",
      "structure.overlap",
    ]);
  });
});

describe("parseOptions", () => {
  it("sends every edited field, typed", () => {
    const parsed = parseOptions(
      { "structure.max_words": "60", "structure.min_chunk_length": "20", "facts.enabled": "true" },
      RESOLVED
    );
    expect(parsed.problems).toEqual([]);
    expect(parsed.options).toEqual({
      structure: { max_words: 60, min_chunk_length: 20 },
      facts: { enabled: true },
    });
  });

  it("sends nothing when nothing was edited", () => {
    expect(parseOptions({}, RESOLVED)).toEqual({ options: null, problems: [] });
  });

  it("refuses a value of the wrong type rather than coercing it", () => {
    const { problems } = parseOptions(
      { "structure.max_words": "sixty", "structure.overlap": "x", "facts.model": "{" },
      RESOLVED
    );
    expect(problems).toHaveLength(3);
    expect(problems[0]).toMatch(/max_words must be a number/);
    expect(problems[1]).toMatch(/overlap must be a number/);
    expect(problems[2]).toMatch(/facts.model must be JSON/);
  });

  it("lets a float option that resolved to a whole value take a fraction", () => {
    // JSON.parse("1.0") === 1; the form cannot tell rollup.penalty from a count.
    const resolved = { rollup: { penalty: 1 } };
    expect(parseOptions({ "rollup.penalty": "0.5" }, resolved)).toEqual({
      options: { rollup: { penalty: 0.5 } },
      problems: [],
    });
  });

  it("refuses the renamed max_tokens and names max_words", () => {
    const { options, problems } = parseOptions({ "structure.max_tokens": "60" }, RESOLVED);
    expect(options).toBeNull();
    expect(problems[0]).toMatch(/renamed to "structure.max_words"/);
  });

  it("gives no rename hint to an appliance that still has the old name", () => {
    const old = { structure: { max_tokens: 120 } };
    expect(parseOptions({ "structure.max_tokens": "60" }, old)).toEqual({
      options: { structure: { max_tokens: 60 } },
      problems: [],
    });
    expect(parseOptions({ "structure.max_words": "60" }, old).problems[0]).toMatch(
      /not an option of "structure". It has: max_tokens/
    );
  });

  it("refuses an unknown group and lists the groups that exist", () => {
    expect(parseOptions({ "chunking.size": "1" }, RESOLVED).problems[0]).toMatch(
      /"chunking" is not an option group.*facts, structure/
    );
  });
});

describe("editsFromJson", () => {
  it("flattens a pasted options object into field edits", () => {
    expect(editsFromJson('{"structure": {"max_words": 60, "chunk_strategy": "paragraph"}}')).toEqual({
      "structure.max_words": "60",
      "structure.chunk_strategy": "paragraph",
    });
    expect(editsFromJson("  ")).toEqual({});
  });

  it("refuses text that is not an object of groups", () => {
    expect(() => editsFromJson("{")).toThrow(/not JSON/);
    expect(() => editsFromJson("[1]")).toThrow(/object of option groups/);
    expect(() => editsFromJson('{"structure": 5}')).toThrow(/options.structure must be an object/);
  });
});

describe("parent and source", () => {
  it("parses a parent id or refuses it", () => {
    expect(parseParentId("")).toBeNull();
    expect(parseParentId(" 42 ")).toBe(42);
    expect(() => parseParentId("0")).toThrow(/not one/);
    expect(() => parseParentId("abc")).toThrow(/not one/);
  });

  it("checks what is certain about an entry point's content", () => {
    expect(sourceProblem("content", " ")).toMatch(/no text/);
    expect(sourceProblem("url", "example.com/a.pdf")).toMatch(/not an http/);
    expect(sourceProblem("url", "https://example.com/a.pdf")).toBeNull();
    expect(sourceProblem("arxiv", "2401.00001")).toBeNull();
    expect(sourceProblem("arxiv", "")).toMatch(/no arXiv id/);
  });
});

describe("checkName", () => {
  const detectable = ["pdf", "png", "jpeg", "docx", "xlsx", "pptx", "zip", "ole2", "text"];

  it("reads the extension", () => {
    expect(extensionOf("Report.PDF")).toBe("pdf");
    expect(extensionOf("README")).toBeNull();
    expect(extensionOf(".bashrc")).toBeNull();
    expect(extensionOf("a.")).toBeNull();
  });

  it("agrees when the bytes measured as what the name says", () => {
    expect(checkName("a.pdf", "pdf", "magic_bytes", detectable).verdict).toBe("agrees");
    expect(checkName("a.JPG", "jpeg", "magic_bytes", detectable).verdict).toBe("agrees");
    expect(checkName("old.xls", "ole2", "magic_bytes", detectable).verdict).toBe("agrees");
    expect(checkName("notes.md", "text", "content_sniff", detectable).verdict).toBe("agrees");
    expect(checkName("data.csv", "text", "content_sniff", detectable).verdict).toBe("agrees");
  });

  it("warns when they disagree", () => {
    const wrong = checkName("budget.xlsx", "zip", "magic_bytes", detectable);
    expect(wrong.verdict).toBe("disagrees");
    expect(wrong.message).toMatch(/\.xlsx but the bytes measured as zip/);
    expect(checkName("scan.pdf", "text", "content_sniff", detectable).verdict).toBe("disagrees");
    expect(checkName("a.docx", "pdf", "magic_bytes", detectable).verdict).toBe("disagrees");
  });

  it("does not compare when nothing measured the bytes", () => {
    expect(checkName("a.weird", "weird", null, detectable).verdict).toBe("unmeasured");
    expect(checkName("README", "text", "content_sniff", detectable).verdict).toBe("no_extension");
  });
});

describe("mergeOptions", () => {
  it("layers overrides option by option and leaves the base alone", () => {
    const merged = mergeOptions(RESOLVED, { structure: { max_words: 60 }, embed: { batch: 8 } });
    expect(merged.structure).toEqual({ ...RESOLVED.structure, max_words: 60 });
    expect(merged.embed).toEqual({ batch: 8 });
    expect(RESOLVED.structure.max_words).toBe(120);
    expect(mergeOptions(RESOLVED, null)).toEqual(RESOLVED);
  });
});
