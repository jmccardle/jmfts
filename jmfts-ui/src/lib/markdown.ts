// A markdown reader that produces a token tree, never a string of HTML.
//
// NO `innerHTML`, ANYWHERE. Document content in this corpus is text somebody else wrote, and a
// renderer that assembles markup as a string is one escaping mistake away from executing it.
// This returns data; `RenderMarkdown.vue` turns it into elements with `h()`, so a stored
// `<script>` is text at every stage and never has a form in which it could run.
//
// WHAT IS COVERED: ATX headings, paragraphs, fenced code, blockquotes, bullet and ordered
// lists, pipe tables, thematic breaks, and inline code, strong, emphasis, links and autolinks.
//
// WHAT IS NOT, and how it looks when it appears: reference links, footnotes, task lists, setext
// headings and raw HTML all render as the literal characters the document holds. That is
// deliberate — visibly unrendered is a state a reader can see and report, where invisibly
// dropped is a document that looks like it simply had no table in it.
//
// LINK TARGETS ARE ALLOW-LISTED: `http:`, `https:`, `mailto:`, and relative hrefs (which is how
// the appliance writes its own `/view/{id}` footnotes and citations). Anything else —
// `javascript:`, `data:`, `vbscript:`, `file:`, `blob:` — is not a link here. Vue does not
// sanitise a bound `href`, so `[x](javascript:…)` reaching `h("a", { href })` would be a live
// script one click away; no `innerHTML` is not enough on its own. A refused link renders as the
// literal characters the document holds, by the rule above, and `safeHref` is the one check.

export type Inline =
  | { t: "text"; v: string }
  | { t: "code"; v: string }
  | { t: "strong"; c: Inline[] }
  | { t: "em"; c: Inline[] }
  | { t: "link"; href: string; c: Inline[] };

export type Block =
  | { t: "heading"; level: number; c: Inline[] }
  | { t: "para"; c: Inline[] }
  | { t: "code"; lang: string | null; v: string }
  | { t: "quote"; c: Block[] }
  | { t: "list"; ordered: boolean; start: number; items: Block[][] }
  | { t: "table"; head: Inline[][]; rows: Inline[][][] }
  | { t: "rule" };

const HEADING = /^(#{1,6})\s+(.*)$/;
const FENCE = /^(?:```|~~~)\s*([\w+-]*)\s*$/;
const BULLET = /^[ \t]*[-*+]\s+(.*)$/;
const ORDERED = /^[ \t]*(\d+)[.)]\s+(.*)$/;
const QUOTE = /^>\s?(.*)$/;
const RULE = /^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/;
const TABLE_DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

export function parseMarkdown(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (line.trim() === "") {
      i += 1;
      continue;
    }

    const fence = FENCE.exec(line);
    if (fence) {
      const lang = fence[1] || null;
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !FENCE.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      // An unterminated fence runs to the end of the document. Kept rather than refused: the
      // content is still the content, and the document is the one that is malformed.
      i += 1;
      blocks.push({ t: "code", lang, v: body.join("\n") });
      continue;
    }

    if (RULE.test(line)) {
      blocks.push({ t: "rule" });
      i += 1;
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({ t: "heading", level: heading[1].length, c: parseInline(heading[2].trim()) });
      i += 1;
      continue;
    }

    if (QUOTE.test(line)) {
      const body: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i])) {
        body.push(QUOTE.exec(lines[i])![1]);
        i += 1;
      }
      blocks.push({ t: "quote", c: parseMarkdown(body.join("\n")) });
      continue;
    }

    if (lines[i + 1] !== undefined && line.includes("|") && TABLE_DIVIDER.test(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;
      const rows: Inline[][][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      blocks.push({ t: "table", head, rows });
      continue;
    }

    if (BULLET.test(line) || ORDERED.test(line)) {
      const ordered = !BULLET.test(line);
      const start = ordered ? Number(ORDERED.exec(line)![1]) : 1;
      const items: Block[][] = [];
      while (i < lines.length) {
        const bullet = BULLET.exec(lines[i]);
        const numbered = ORDERED.exec(lines[i]);
        if (!bullet && !numbered) break;
        if (ordered !== !bullet) break;
        const first = bullet ? bullet[1] : numbered![2];
        const body = [first];
        i += 1;
        // A continuation line is indented and is not itself a new item.
        while (
          i < lines.length &&
          lines[i].trim() !== "" &&
          /^[ \t]+/.test(lines[i]) &&
          !BULLET.test(lines[i]) &&
          !ORDERED.test(lines[i])
        ) {
          body.push(lines[i].trim());
          i += 1;
        }
        items.push(parseMarkdown(body.join("\n")));
      }
      blocks.push({ t: "list", ordered, start, items });
      continue;
    }

    const body = [line];
    i += 1;
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !HEADING.test(lines[i]) &&
      !FENCE.test(lines[i]) &&
      !QUOTE.test(lines[i]) &&
      !RULE.test(lines[i]) &&
      !BULLET.test(lines[i]) &&
      !ORDERED.test(lines[i])
    ) {
      body.push(lines[i]);
      i += 1;
    }
    blocks.push({ t: "para", c: parseInline(body.join(" ")) });
  }

  return blocks;
}

function splitRow(line: string): Inline[][] {
  return line
    .replace(/^\s*\|/, "")
    .replace(/\|\s*$/, "")
    .split("|")
    .map((cell) => parseInline(cell.trim()));
}

/** Schemes a link may carry. A relative href resolves against an `http:` base, so it passes. */
const SAFE_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

/**
 * `raw` if it is safe to bind as an `href`, otherwise null.
 *
 * Parsed with the WHATWG URL parser rather than matched with a regex, because the parser is
 * what the browser will use: it strips leading C0 controls and spaces, drops tabs and newlines
 * anywhere, and lower-cases the scheme, so `\u0001javascript:`, `JaVaScRiPt:` and
 * `java\nscript:` all arrive as `javascript:` — exactly the spellings a hand regex misses. The
 * base is a host that cannot resolve, there only so a relative href has something to be
 * relative to; what is returned is `raw`, unchanged, never the resolved URL.
 */
export function safeHref(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw, "http://relative.invalid/");
  } catch {
    return null;
  }
  return SAFE_PROTOCOLS.has(url.protocol) ? raw : null;
}

const INLINE = /(`+)([\s\S]*?)\1|\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|\*([^*\n]+?)\*|_([^_\n]+?)_|\[([^\]]*)\]\(([^)\s]+)[^)]*\)|<(https?:\/\/[^>\s]+)>/;

export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let rest = text;

  while (rest.length > 0) {
    const match = INLINE.exec(rest);
    if (!match || match.index === undefined) break;
    if (match.index > 0) pushText(out, rest.slice(0, match.index));

    if (match[2] !== undefined) out.push({ t: "code", v: match[2].trim() });
    else if (match[3] !== undefined) out.push({ t: "strong", c: parseInline(match[3]) });
    else if (match[4] !== undefined) out.push({ t: "strong", c: parseInline(match[4]) });
    else if (match[5] !== undefined) out.push({ t: "em", c: parseInline(match[5]) });
    else if (match[6] !== undefined) out.push({ t: "em", c: parseInline(match[6]) });
    else if (match[8] !== undefined) {
      // A refused target is shown as written, not dropped and not linked; see the header.
      const href = safeHref(match[8]);
      if (href === null) pushText(out, match[0]);
      else out.push({ t: "link", href, c: parseInline(match[7] ?? "") });
    }
    else if (match[9] !== undefined)
      out.push({ t: "link", href: match[9], c: [{ t: "text", v: match[9] }] });

    rest = rest.slice(match.index + match[0].length);
  }

  if (rest.length > 0) pushText(out, rest);
  return out;
}

/**
 * Appends text, joining it to a text run already at the end. A refused link is text that the
 * pattern still consumed as one match, and its target stops at the first `)` — so the closing
 * paren of `alert(1)` arrives as the next run. Joined, the source reads back as one run.
 */
function pushText(out: Inline[], v: string): void {
  const last = out[out.length - 1];
  if (last?.t === "text") last.v += v;
  else out.push({ t: "text", v });
}

/** The transcript shape `conversation_ingest.conversation_markdown` writes: `[role]: content`. */
export interface Turn {
  role: string;
  text: string;
}

export function parseTranscript(source: string): Turn[] {
  const turns: Turn[] = [];
  let current: Turn | null = null;
  for (const line of source.replace(/\r\n?/g, "\n").split("\n")) {
    const head = /^\[([^\]]+)\]:\s?(.*)$/.exec(line);
    if (head) {
      if (current) turns.push(current);
      current = { role: head[1], text: head[2] };
    } else if (current) {
      current.text += `\n${line}`;
    } else if (line.trim() !== "") {
      current = { role: "", text: line };
    }
  }
  if (current) turns.push(current);
  return turns.map((turn) => ({ role: turn.role, text: turn.text.trim() }));
}
