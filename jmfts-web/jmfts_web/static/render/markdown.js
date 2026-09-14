// Markdown, rendered. `docs/SPRINT_0_6_0.md` Block F step 29.
//
// Step 25 registered `markdown` as a renderer that shows its own SOURCE, with a notice saying
// so, and its argument for that was not laziness: "One that handled headings and paragraphs
// and dropped tables, links and code fences would render most documents plausibly and a few
// wrongly, and the wrong ones would look like documents that simply had no tables in them."
// That is the bar this module has to clear, so what it covers is written out below rather
// than discovered by a reader who finds a table missing.
//
// COVERED, as blocks: ATX headings, setext headings, fenced code (``` and ~~~), indented
// code, blockquotes (nested), unordered and ordered lists (nested, tight and loose),
// GFM pipe tables with alignment, thematic breaks, paragraphs. As inline: code spans, links,
// images, strong, emphasis, hard line breaks, backslash escapes.
//
// NOT COVERED, and each renders as the literal text it is rather than disappearing:
// reference links and their definitions, footnotes, HTML blocks and inline HTML, task list
// checkboxes, strikethrough, autolinks in angle brackets. A reader meets the source of those
// four, which is visibly unrendered — the failure step 25 refused was the one that is NOT
// visible, a table that renders as nothing.
//
// RAW HTML IS NEVER INTERPRETED, and that is a rule rather than an omission. Every string in
// this module reaches the page through `textContent`, so `<script>` in a stored document is
// five characters of text. `markdown` content is document content: it came from an upload, an
// extraction or an LLM, and this front end is not the place any of the three gets to run.
//
// A URL IS CHECKED BEFORE IT BECOMES AN `href`. `javascript:` and `data:` in a link a
// document supplied are the same problem as the script tag above, so `linkHref()` answers
// null for anything that is not http, https, mailto, a fragment or a relative path, and the
// link renders as its text with the refused URL beside it rather than as a live link.

/** Markdown content that this module cannot make a document out of. */
export class MarkdownError extends Error {
  constructor(message) {
    super(message);
    this.name = "MarkdownError";
  }
}

const ATX = /^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/;
const FENCE = /^(\s{0,3})(`{3,}|~{3,})\s*(\S*)\s*$/;
const BREAK = /^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const BULLET = /^(\s*)([-*+])(\s+)(.*)$/;
const ORDERED = /^(\s*)(\d{1,9})([.)])(\s+)(.*)$/;
const SETEXT = /^\s{0,3}(=+|-+)\s*$/;
const DIVIDER = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;
const INDENTED = /^(?: {4}|\t)(.*)$/;

const el = (tag, className) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  return element;
};

const text = (value, tag = "span", className = null) => {
  const element = el(tag, className);
  element.textContent = value;
  return element;
};

/**
 * One markdown document as an element.
 *
 * @param {string} source the node's `rendered_content`
 * @param {string} [className] the wrapper's class; the renderer names it
 * @returns {Element}
 */
export function renderMarkdown(source, className = "render-markdown") {
  if (typeof source !== "string") {
    throw new MarkdownError(
      `the markdown renderer was given ${typeof source}, not a string. \`rendered_content\` ` +
        "is text; a node whose content is null has no markdown to render."
    );
  }
  const root = el("div", className);
  for (const block of blocks(source.split(/\r\n|\r|\n/))) root.append(block);
  return root;
}

/** The block parse. Returns elements in document order; never drops a line. */
function blocks(lines) {
  const out = [];
  let at = 0;
  while (at < lines.length) {
    const line = lines[at];

    if (!line.trim()) {
      at += 1;
      continue;
    }

    const fence = FENCE.exec(line);
    if (fence) {
      const [, , ticks, language] = fence;
      const closer = new RegExp(`^\\s{0,3}${ticks[0]}{${ticks.length},}\\s*$`);
      const body = [];
      at += 1;
      while (at < lines.length && !closer.test(lines[at])) {
        body.push(lines[at]);
        at += 1;
      }
      // An unterminated fence runs to the end of the document. Not an error: the fence is
      // what the author wrote, and the alternative — treating it as a paragraph after the
      // fact — would re-render the same bytes two different ways depending on the last line.
      at += 1;
      out.push(codeBlock(body.join("\n"), language));
      continue;
    }

    if (BREAK.test(line)) {
      out.push(el("hr", "render-md-rule"));
      at += 1;
      continue;
    }

    const heading = ATX.exec(line);
    if (heading) {
      const node = el(`h${heading[1].length}`, "render-md-heading");
      for (const part of inline(heading[2])) node.append(part);
      out.push(node);
      at += 1;
      continue;
    }

    if (QUOTE.test(line)) {
      const inner = [];
      while (at < lines.length && (QUOTE.test(lines[at]) || lazy(lines, at))) {
        const match = QUOTE.exec(lines[at]);
        inner.push(match ? match[1] : lines[at]);
        at += 1;
      }
      const quote = el("blockquote", "render-md-quote");
      // Recursive, so a list or a code fence inside a quote is the same parse it would be
      // outside one. This is the reason the parser is written over a line array.
      for (const block of blocks(inner)) quote.append(block);
      out.push(quote);
      continue;
    }

    if (BULLET.test(line) || ORDERED.test(line)) {
      const [list, next] = listAt(lines, at);
      out.push(list);
      at = next;
      continue;
    }

    if (INDENTED.test(line)) {
      const body = [];
      while (at < lines.length && (INDENTED.test(lines[at]) || !lines[at].trim())) {
        if (!lines[at].trim() && !nextIsIndented(lines, at)) break;
        body.push(INDENTED.exec(lines[at])?.[1] ?? "");
        at += 1;
      }
      out.push(codeBlock(body.join("\n"), ""));
      continue;
    }

    const table = tableAt(lines, at);
    if (table) {
      out.push(table.element);
      at = table.next;
      continue;
    }

    // A paragraph: every line up to a blank one, the start of another block, or a setext
    // underline — which is the one block marker that comes AFTER the text it applies to, and
    // therefore the one a paragraph has to stop before rather than consume.
    const paragraph = [];
    while (
      at < lines.length &&
      lines[at].trim() &&
      !startsBlock(lines, at) &&
      !(paragraph.length && SETEXT.test(lines[at]))
    ) {
      paragraph.push(lines[at]);
      at += 1;
    }
    const underline = at < lines.length && SETEXT.test(lines[at]) ? lines[at].trim() : null;
    if (underline && paragraph.length) {
      const node = el(underline.startsWith("=") ? "h1" : "h2", "render-md-heading");
      for (const part of inline(paragraph.join(" "))) node.append(part);
      out.push(node);
      at += 1;
      continue;
    }
    const node = el("p", "render-md-paragraph");
    for (const part of inline(paragraph.join("\n"))) node.append(part);
    out.push(node);
  }
  return out;
}

/** Does the line at `index` open a block that a paragraph must stop before? */
function startsBlock(lines, index) {
  const line = lines[index];
  return (
    FENCE.test(line) ||
    BREAK.test(line) ||
    ATX.test(line) ||
    QUOTE.test(line) ||
    BULLET.test(line) ||
    ORDERED.test(line) ||
    (index + 1 < lines.length && line.includes("|") && DIVIDER.test(lines[index + 1]))
  );
}

/** A blockquote's lazy continuation: an ordinary paragraph line under a `>` line. */
function lazy(lines, index) {
  return Boolean(lines[index].trim()) && !startsBlock(lines, index) && index > 0;
}

function nextIsIndented(lines, index) {
  for (let i = index + 1; i < lines.length; i += 1) {
    if (lines[i].trim()) return INDENTED.test(lines[i]);
  }
  return false;
}

function codeBlock(body, language) {
  const block = el("pre", "render-md-code");
  const code = el("code", language ? `language-${language}` : null);
  code.textContent = body;
  block.append(code);
  return block;
}

/**
 * One list, and every item's content parsed as blocks.
 *
 * Nesting is not a special case: an item's content is de-indented and handed back to
 * `blocks()`, so a list inside a list inside a quote is three ordinary parses rather than a
 * depth counter. A tight item — one whose content is a single paragraph — is unwrapped, which
 * is the difference a reader sees between a tight and a loose list.
 */
function listAt(lines, start) {
  const opener = BULLET.exec(lines[start]) || ORDERED.exec(lines[start]);
  const ordered = !BULLET.test(lines[start]);
  const indent = opener[1].length;
  const list = el(ordered ? "ol" : "ul", "render-md-list");
  if (ordered && opener[2] !== "1") list.setAttribute("start", opener[2]);

  let at = start;
  while (at < lines.length) {
    // Blank lines BETWEEN items belong to the list, not after it. Without this, a loose list
    // — the kind with a blank line between its items — came out as one list per item, which
    // is a document rendered wrongly in a way that looks like a document written that way.
    let ahead = at;
    while (ahead < lines.length && !lines[ahead].trim()) ahead += 1;
    if (ahead > at) {
      const next = BULLET.exec(lines[ahead] ?? "") || ORDERED.exec(lines[ahead] ?? "");
      if (!next || next[1].length !== indent || Boolean(BULLET.test(lines[ahead])) === ordered) {
        break;
      }
      at = ahead;
    }
    const bullet = BULLET.exec(lines[at]);
    const number = ORDERED.exec(lines[at]);
    const marker = bullet || number;
    if (!marker || marker[1].length !== indent) break;
    if (Boolean(bullet) === ordered) break;

    const gap = marker[bullet ? 3 : 4].length;
    const width = marker[1].length + (bullet ? 1 : marker[2].length + 1) + gap;
    const body = [bullet ? bullet[4] : number[5]];
    at += 1;
    while (at < lines.length) {
      const line = lines[at];
      if (!line.trim()) {
        // A blank line ends the item unless something indented follows it, which is what
        // makes the list loose rather than two lists.
        if (!continues(lines, at, width)) break;
        body.push("");
        at += 1;
        continue;
      }
      if (line.length - line.trimStart().length < width) {
        if (BULLET.test(line) || ORDERED.test(line) || startsBlock(lines, at)) break;
        body.push(line.trim()); // lazy continuation of the item's paragraph
        at += 1;
        continue;
      }
      body.push(line.slice(width));
      at += 1;
    }

    const item = el("li", "render-md-item");
    const parsed = blocks(body);
    if (parsed.length === 1 && parsed[0].tagName === "P") {
      // Snapshotted: `childNodes` is live in a real DOM and `append` MOVES a node, so
      // iterating it while appending elsewhere would skip every other child.
      for (const part of [...parsed[0].childNodes]) item.append(part);
    } else {
      for (const block of parsed) item.append(block);
    }
    list.append(item);
  }
  return [list, at];
}

function continues(lines, index, width) {
  for (let i = index + 1; i < lines.length; i += 1) {
    if (!lines[i].trim()) continue;
    return lines[i].length - lines[i].trimStart().length >= width;
  }
  return false;
}

/** A GFM pipe table, or null when the two lines at `start` are not one. */
function tableAt(lines, start) {
  if (start + 1 >= lines.length) return null;
  if (!lines[start].includes("|") || !DIVIDER.test(lines[start + 1])) return null;

  const cells = (line) => {
    let row = line.trim();
    if (row.startsWith("|")) row = row.slice(1);
    if (row.endsWith("|") && !row.endsWith("\\|")) row = row.slice(0, -1);
    return row.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
  };

  const header = cells(lines[start]);
  const alignment = cells(lines[start + 1]).map((rule) => {
    const left = rule.startsWith(":");
    const right = rule.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    return left ? "left" : null;
  });

  const table = el("table", "render-md-table");
  const head = el("tr", null);
  header.forEach((label, column) => {
    const cell = el("th", alignment[column] ? `align-${alignment[column]}` : null);
    for (const part of inline(label)) cell.append(part);
    head.append(cell);
  });
  table.append(head);

  let at = start + 2;
  while (at < lines.length && lines[at].includes("|") && lines[at].trim()) {
    const row = el("tr", null);
    const values = cells(lines[at]);
    // A row with fewer cells than the header is a short row and is rendered short. Padding it
    // would invent cells the document does not have; the header is what says how many there
    // should be, and the gap is visible.
    values.forEach((value, column) => {
      const cell = el("td", alignment[column] ? `align-${alignment[column]}` : null);
      for (const part of inline(value)) cell.append(part);
      row.append(cell);
    });
    table.append(row);
    at += 1;
  }
  return { element: table, next: at };
}

// ------------------------------------------------------------------------------- inline

const ESCAPABLE = "\\\\`*_{}\\[\\]()#+\\-.!|>~";

const INLINE_SOURCE = [
    // FIRST, so that `\*` is consumed before the emphasis branch can open on that `*`. An
    // escape resolved after the scan — which is what `plain()` did until this branch existed
    // — is resolved too late: by then the scanner has already read the character it was
    // escaping as markup, and `not \*emphasis\* here` came out italic.
    `(?<esc>\\\\[${ESCAPABLE}])`,
    // Named backreferences, not `\1`. The alternatives share one numbering, so `\1` inside
    // the third branch would close it against the FIRST branch's opener — which is a bug that
    // reads as correct and shows up only on a document that mixes the two.
    "(?<code>`+)(?<codebody>[\\s\\S]*?)\\k<code>",
    "(?<image>!)?\\[(?<label>(?:[^\\]\\\\]|\\\\.)*)\\]\\((?<url>[^)\\s]*)(?:\\s+\"[^\"]*\")?\\)",
    "(?<strong>\\*\\*|__)(?<strongbody>[\\s\\S]+?)\\k<strong>",
    "(?<em>[*_])(?<embody>[^*_\\s][\\s\\S]*?)\\k<em>",
].join("|");

/**
 * One run of inline markdown as a list of elements.
 *
 * EVERY element, including the plain runs, because the stub DOM the suite runs these under
 * takes elements and not text nodes — and because a `<span>` around a text run costs nothing
 * and makes `serialize()` able to see where one run ends and the next begins.
 */
export function inline(source) {
  const out = [];
  let cursor = 0;
  // A FRESH regex per call, not one shared module-level object. This function recurses — the
  // body of a `**strong**` is inline markup too — and a recursive call on a shared `/g` regex
  // resets `lastIndex` underneath the scan it returns to, which loops forever rather than
  // failing. Building it here costs one compile per run of text and cannot do that.
  const scanner = new RegExp(INLINE_SOURCE, "g");
  let match;
  while ((match = scanner.exec(source)) !== null) {
    if (match.index > cursor) out.push(...plain(source.slice(cursor, match.index)));
    const groups = match.groups;
    if (groups.esc !== undefined) {
      out.push(text(groups.esc.slice(1)));
    } else if (groups.code !== undefined) {
      out.push(text(groups.codebody.trim(), "code", "render-md-code-span"));
    } else if (groups.url !== undefined) {
      out.push(groups.image ? image(groups.label, groups.url) : link(groups.label, groups.url));
    } else if (groups.strong !== undefined) {
      const strong = el("strong", null);
      for (const part of inline(groups.strongbody)) strong.append(part);
      out.push(strong);
    } else {
      const em = el("em", null);
      for (const part of inline(groups.embody)) em.append(part);
      out.push(em);
    }
    cursor = match.index + match[0].length;
  }
  if (cursor < source.length) out.push(...plain(source.slice(cursor)));
  return out;
}

/** A run with no inline markup left in it. Escapes are the scanner's; hard breaks are here. */
function plain(run) {
  const out = [];
  run.split(/ {2,}\n|\\\n/).forEach((part, index) => {
    if (index > 0) out.push(el("br", null));
    if (part) out.push(text(part));
  });
  return out;
}

/**
 * The `href` a link may carry, or null when it may not carry one.
 *
 * Exported because it is the decision rather than the plumbing, and because
 * `tests/test_web_renderers.py` asserts the refusals directly. A document's own link text is
 * content: it arrived by upload, extraction or an LLM, and `javascript:` from any of the three
 * is a script this page would be running on its own origin.
 */
export function linkHref(url) {
  const trimmed = String(url).trim();
  if (!trimmed) return null;
  if (/^(?:https?:|mailto:)/i.test(trimmed)) return trimmed;
  // A scheme this list does not carry is refused; anything with no scheme at all is a
  // relative path or a fragment, which resolves against this origin and cannot introduce one.
  if (/^[a-z][a-z0-9+.-]*:/i.test(trimmed)) return null;
  return trimmed;
}

function link(label, url) {
  const href = linkHref(url);
  const parts = inline(label);
  if (href === null) {
    const refused = el("span", "render-md-refused-link");
    for (const part of parts) refused.append(part);
    refused.append(text(` (link to ${url} not followed)`, "span", "dim"));
    refused.setAttribute("title", `this front end does not make a link out of ${url}`);
    return refused;
  }
  const anchor = el("a", "render-md-link");
  anchor.setAttribute("href", href);
  for (const part of parts) anchor.append(part);
  return anchor;
}

/**
 * An image reference, rendered as a reference and NOT as an `<img>`.
 *
 * The bundle loads nothing from another origin — `index.html` says so and
 * `test_nothing_in_the_bundle_loads_from_the_network` holds the source to it. An `<img src>`
 * built out of a stored document's markdown would put that rule in the document's hands: the
 * page would fetch whatever a markdown file names, from wherever it names it, every time
 * somebody opened the node. So the alt text and the URL are shown, and the reader decides.
 */
function image(label, url) {
  const href = linkHref(url);
  const figure = el("span", "render-md-image-ref");
  figure.append(text("image: ", "span", "dim"));
  figure.append(text(label || "(no alt text)", "span", null));
  figure.append(text(` — ${url}`, "span", "dim"));
  figure.setAttribute(
    "title",
    href === null
      ? `an image at ${url}, which this page will not link to either`
      : `an image at ${url}; this page does not load it, see render/markdown.js`
  );
  return figure;
}
