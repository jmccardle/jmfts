import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  count,
  glossContentSource,
  glossEvidence,
  glossLifecycle,
  glossUsetype,
  handOf,
  when,
} from "./vocab";

describe("glosses", () => {
  it("gloss a value the appliance hands over", () => {
    expect(glossUsetype("chunk")?.meaning).toMatch(/passage/);
    expect(glossLifecycle("failed")).toMatchObject({ tone: "alarm" });
    expect(glossLifecycle("in_flight")).toMatchObject({ tone: "active" });
    expect(glossContentSource("effective")).toMatchObject({ tone: "active" });
    expect(glossEvidence("source_anchor.unresolved")?.meaning).toMatch(/could not be recovered/);
  });

  it("answer null for a value they do not know, rather than guess", () => {
    expect(glossUsetype("Chunk")).toBeNull();
    expect(glossUsetype("not-a-usetype")).toBeNull();
    expect(glossLifecycle("pending")).toBeNull();
    expect(glossContentSource("cached")).toBeNull();
    expect(glossEvidence("nope")).toBeNull();
  });

  it("answer null for no value", () => {
    for (const gloss of [glossUsetype, glossLifecycle, glossContentSource]) {
      expect(gloss(null)).toBeNull();
      expect(gloss(undefined)).toBeNull();
      expect(gloss("")).toBeNull();
    }
  });

  // KNOWN DEFECT: the tables are plain object literals, so a key that names an Object.prototype
  // member returns that member (a function) rather than null. `it.fails` passes while that holds;
  // make it an `it` once the lookups check own keys.
  it.fails("KNOWN DEFECT: do not reach Object.prototype through a key", () => {
    expect(glossUsetype("toString")).toBeNull();
    expect(glossEvidence("constructor")).toBeNull();
  });
});

describe("handOf", () => {
  it("names the rule that wrote a produced node", () => {
    expect(handOf("chunk_sections")).toEqual({
      kind: "rule",
      label: "chunk_sections",
      meaning: "Written by the ingest rule chunk_sections.",
    });
  });

  it("reads no producer as a person's assertion", () => {
    for (const none of [null, undefined, ""]) {
      expect(handOf(none)).toMatchObject({ kind: "asserted", label: "asserted" });
    }
  });
});

describe("count", () => {
  it("uses the singular for exactly one", () => {
    expect(count(1, "tree")).toBe("1 tree");
  });

  it("uses the plural otherwise, including zero", () => {
    expect(count(0, "tree")).toBe("0 trees");
    expect(count(2, "link")).toBe("2 links");
  });

  it("takes an irregular plural", () => {
    expect(count(1, "child", "children")).toBe("1 child");
    expect(count(3, "child", "children")).toBe("3 children");
  });

  it("formats the number for the reader's locale", () => {
    expect(count(12345, "fact")).toBe(`${(12345).toLocaleString()} facts`);
  });
});

describe("when", () => {
  const now = new Date("2026-10-12T12:00:00Z");
  const ago = (seconds: number) => new Date(now.getTime() - seconds * 1000).toISOString();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(now);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("is null for no value or an unparseable one", () => {
    expect(when(null)).toBeNull();
    expect(when(undefined)).toBeNull();
    expect(when("")).toBeNull();
    expect(when("not a date")).toBeNull();
  });

  it("keeps the exact value to the second, in UTC", () => {
    expect(when("2026-10-12T11:59:30.123Z")?.exact).toBe("2026-10-12 11:59:30Z");
    expect(when("2026-10-12T07:59:30-04:00")?.exact).toBe("2026-10-12 11:59:30Z");
  });

  it("says just now under ninety seconds", () => {
    expect(when(ago(0))?.text).toBe("just now");
    expect(when(ago(89))?.text).toBe("just now");
  });

  it("counts minutes up to ninety minutes", () => {
    expect(when(ago(90))?.text).toBe("2 min ago");
    expect(when(ago(30 * 60))?.text).toBe("30 min ago");
    expect(when(ago(5399))?.text).toBe("90 min ago");
  });

  it("counts hours up to two days", () => {
    expect(when(ago(5400))?.text).toBe("2 hr ago");
    expect(when(ago(172799))?.text).toBe("48 hr ago");
  });

  it("gives the date from two days on", () => {
    expect(when(ago(172800))?.text).toBe("2026-10-10");
  });
});
