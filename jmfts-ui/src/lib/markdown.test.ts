import { describe, expect, it } from "vitest";
import { parseInline, parseMarkdown, parseTranscript } from "./markdown";

const text = (v: string) => ({ t: "text", v }) as const;

describe("parseInline", () => {
  it("leaves plain text as one text run", () => {
    expect(parseInline("just words")).toEqual([text("just words")]);
  });

  it("is empty for empty text", () => {
    expect(parseInline("")).toEqual([]);
  });

  it("reads code, strong and emphasis", () => {
    expect(parseInline("a `x` **b** __c__ *d* _e_")).toEqual([
      text("a "),
      { t: "code", v: "x" },
      text(" "),
      { t: "strong", c: [text("b")] },
      text(" "),
      { t: "strong", c: [text("c")] },
      text(" "),
      { t: "em", c: [text("d")] },
      text(" "),
      { t: "em", c: [text("e")] },
    ]);
  });

  it("nests emphasis inside strong", () => {
    expect(parseInline("**a *b* c**")).toEqual([
      { t: "strong", c: [text("a "), { t: "em", c: [text("b")] }, text(" c")] },
    ]);
  });

  it("does not read marks inside inline code", () => {
    expect(parseInline("``a **b** `c` ``")).toEqual([{ t: "code", v: "a **b** `c`" }]);
  });

  it("reads a link, dropping a title after the target", () => {
    expect(parseInline('[the *paper*](https://x.org/a "title")')).toEqual([
      { t: "link", href: "https://x.org/a", c: [text("the "), { t: "em", c: [text("paper")] }] },
    ]);
  });

  it("reads an autolink as a link to itself", () => {
    expect(parseInline("see <https://x.org>")).toEqual([
      text("see "),
      { t: "link", href: "https://x.org", c: [text("https://x.org")] },
    ]);
  });

  it("keeps raw HTML as text", () => {
    expect(parseInline("<script>alert(1)</script>")).toEqual([text("<script>alert(1)</script>")]);
  });

  it("keeps an unclosed mark as literal text", () => {
    expect(parseInline("**open")).toEqual([text("**open")]);
  });

  // KNOWN DEFECTS. `it.fails` passes while the parser is wrong and fails once it is fixed, at
  // which point the test becomes an ordinary `it`. CommonMark's flanking rules say neither of
  // these is emphasis; this parser reads both as emphasis and drops the marks.
  it.fails("KNOWN DEFECT: keeps intraword underscores, as in identifiers", () => {
    expect(parseInline("max_len and min_len")).toEqual([text("max_len and min_len")]);
  });

  it.fails("KNOWN DEFECT: keeps an asterisk followed by a space as text", () => {
    expect(parseInline("2 * 3 * 4")).toEqual([text("2 * 3 * 4")]);
  });
});

describe("parseMarkdown", () => {
  it("is empty for empty or blank input", () => {
    expect(parseMarkdown("")).toEqual([]);
    expect(parseMarkdown("\n  \n\n")).toEqual([]);
  });

  it("reads ATX headings at every level", () => {
    expect(parseMarkdown("# One\n###### Six")).toEqual([
      { t: "heading", level: 1, c: [text("One")] },
      { t: "heading", level: 6, c: [text("Six")] },
    ]);
  });

  it("keeps a seventh hash and a hash with no space as paragraph text", () => {
    expect(parseMarkdown("####### seven")).toEqual([{ t: "para", c: [text("####### seven")] }]);
    expect(parseMarkdown("#tag")).toEqual([{ t: "para", c: [text("#tag")] }]);
  });

  it("joins consecutive lines into one paragraph and splits on a blank line", () => {
    expect(parseMarkdown("a\nb\n\nc")).toEqual([
      { t: "para", c: [text("a b")] },
      { t: "para", c: [text("c")] },
    ]);
  });

  it("normalises CRLF and CR line ends", () => {
    expect(parseMarkdown("# H\r\nbody\rmore")).toEqual([
      { t: "heading", level: 1, c: [text("H")] },
      { t: "para", c: [text("body more")] },
    ]);
  });

  it("ends a paragraph at a heading, fence, quote, rule or list", () => {
    expect(parseMarkdown("p\n# H").map((b) => b.t)).toEqual(["para", "heading"]);
    expect(parseMarkdown("p\n```\nx\n```").map((b) => b.t)).toEqual(["para", "code"]);
    expect(parseMarkdown("p\n> q").map((b) => b.t)).toEqual(["para", "quote"]);
    expect(parseMarkdown("p\n***").map((b) => b.t)).toEqual(["para", "rule"]);
    expect(parseMarkdown("p\n- i").map((b) => b.t)).toEqual(["para", "list"]);
    expect(parseMarkdown("p\n1. i").map((b) => b.t)).toEqual(["para", "list"]);
  });

  it("reads fenced code verbatim, with its language", () => {
    expect(parseMarkdown("```py\n# not a heading\n  **x**\n```")).toEqual([
      { t: "code", lang: "py", v: "# not a heading\n  **x**" },
    ]);
    expect(parseMarkdown("~~~\nplain\n~~~")).toEqual([{ t: "code", lang: null, v: "plain" }]);
  });

  it("runs an unterminated fence to the end of the document", () => {
    expect(parseMarkdown("```\na\n\nb")).toEqual([{ t: "code", lang: null, v: "a\n\nb" }]);
  });

  it("reads thematic breaks", () => {
    expect(parseMarkdown("---\n***\n___")).toEqual([{ t: "rule" }, { t: "rule" }, { t: "rule" }]);
  });

  it("reads a blockquote as nested blocks", () => {
    expect(parseMarkdown("> # H\n> body\n>\n> more")).toEqual([
      {
        t: "quote",
        c: [
          { t: "heading", level: 1, c: [text("H")] },
          { t: "para", c: [text("body")] },
          { t: "para", c: [text("more")] },
        ],
      },
    ]);
  });

  it("reads a bullet list, with indented continuation lines", () => {
    expect(parseMarkdown("- one\n  still one\n* two\n+ three")).toEqual([
      {
        t: "list",
        ordered: false,
        start: 1,
        items: [
          [{ t: "para", c: [text("one still one")] }],
          [{ t: "para", c: [text("two")] }],
          [{ t: "para", c: [text("three")] }],
        ],
      },
    ]);
  });

  it("reads an ordered list and keeps its start number", () => {
    expect(parseMarkdown("3. c\n4) d")).toEqual([
      {
        t: "list",
        ordered: true,
        start: 3,
        items: [[{ t: "para", c: [text("c")] }], [{ t: "para", c: [text("d")] }]],
      },
    ]);
  });

  it("starts a new list where bullets and numbers change", () => {
    const blocks = parseMarkdown("- a\n1. b");
    expect(blocks).toHaveLength(2);
    expect(blocks.map((b) => b.t === "list" && b.ordered)).toEqual([false, true]);
  });

  it("reads a pipe table", () => {
    expect(parseMarkdown("| a | *b* |\n|---|:--:|\n| 1 | 2 |\n| 3 | 4 |\n\nafter")).toEqual([
      {
        t: "table",
        head: [[text("a")], [{ t: "em", c: [text("b")] }]],
        rows: [
          [[text("1")], [text("2")]],
          [[text("3")], [text("4")]],
        ],
      },
      { t: "para", c: [text("after")] },
    ]);
  });

  it("reads a table without outer pipes", () => {
    expect(parseMarkdown("a | b\n-- | --\n1 | 2")).toEqual([
      { t: "table", head: [[text("a")], [text("b")]], rows: [[[text("1")], [text("2")]]] },
    ]);
  });

  it("keeps a pipe with no divider row as paragraph text", () => {
    expect(parseMarkdown("a | b\nc | d")).toEqual([{ t: "para", c: [text("a | b c | d")] }]);
  });

  it("keeps raw HTML and setext headings as literal text", () => {
    expect(parseMarkdown("<script>alert(1)</script>")).toEqual([
      { t: "para", c: [text("<script>alert(1)</script>")] },
    ]);
    expect(parseMarkdown("Title\n=====")).toEqual([{ t: "para", c: [text("Title =====")] }]);
  });
});

describe("parseTranscript", () => {
  it("splits [role]: lines into turns", () => {
    expect(parseTranscript("[user]: hi\n[assistant]: hello")).toEqual([
      { role: "user", text: "hi" },
      { role: "assistant", text: "hello" },
    ]);
  });

  it("joins continuation lines into the turn above and trims it", () => {
    expect(parseTranscript("[user]: line one\nline two\n\n[bot]:\n  reply  \n")).toEqual([
      { role: "user", text: "line one\nline two" },
      { role: "bot", text: "reply" },
    ]);
  });

  it("keeps text before the first role as a turn with no role", () => {
    expect(parseTranscript("\npreamble\n[user]: hi")).toEqual([
      { role: "", text: "preamble" },
      { role: "user", text: "hi" },
    ]);
  });

  it("is empty for empty input", () => {
    expect(parseTranscript("")).toEqual([]);
    expect(parseTranscript("\n\n")).toEqual([]);
  });

  it("normalises CRLF", () => {
    expect(parseTranscript("[a]: x\r\ny\r\n[b]: z")).toEqual([
      { role: "a", text: "x\ny" },
      { role: "b", text: "z" },
    ]);
  });
});
