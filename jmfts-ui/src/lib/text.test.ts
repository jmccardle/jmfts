import { describe, expect, it } from "vitest";
import { clip, plainWords } from "./text";

describe("plainWords", () => {
  it("is empty for no content", () => {
    expect(plainWords(null)).toBe("");
    expect(plainWords(undefined)).toBe("");
    expect(plainWords("")).toBe("");
  });

  it("drops heading marks and keeps the words", () => {
    expect(plainWords("# Optimal detection of changepoints\n### Killick, R.")).toBe(
      "Optimal detection of changepoints Killick, R."
    );
  });

  it("keeps link and image text, not their targets", () => {
    expect(plainWords("see [the paper](https://x.org/a) and ![a figure](fig.png)")).toBe(
      "see the paper and a figure"
    );
  });

  it("drops emphasis, strong and inline code marks", () => {
    expect(plainWords("**bold** __also__ *em* _em_ `code`")).toBe("bold also em em code");
  });

  it("drops fenced code entirely", () => {
    expect(plainWords("before\n```py\nprint(1)\n```\nafter")).toBe("before after");
  });

  it("drops quote and bullet marks", () => {
    expect(plainWords("> quoted\n- one\n* two\n+ three")).toBe("quoted one two three");
  });

  // KNOWN DEFECT: the emphasis rule matches across words, so identifiers lose their underscores
  // in a preview. `it.fails` passes while that holds; make it an `it` once it is fixed.
  it.fails("KNOWN DEFECT: keeps intraword underscores, as in identifiers", () => {
    expect(plainWords("set max_len and min_len")).toBe("set max_len and min_len");
  });

  it("collapses whitespace to single spaces and trims", () => {
    expect(plainWords("  a\n\n\tb   c  ")).toBe("a b c");
  });
});

describe("clip", () => {
  it("leaves text at or under the limit alone", () => {
    expect(clip("short")).toBe("short");
    expect(clip("x".repeat(90))).toBe("x".repeat(90));
    expect(clip("abcde", 5)).toBe("abcde");
  });

  it("marks the cut with an ellipsis and stays within the limit", () => {
    const out = clip("x".repeat(100));
    expect(out).toBe(`${"x".repeat(89)}…`);
    expect(out.length).toBe(90);
  });

  it("does not leave a space before the ellipsis", () => {
    expect(clip("abc def ghi", 5)).toBe("abc…");
  });
});
