// IC-8: a renderer is `(content, presentation, document) -> element`, keyed by RENDERERS.
//
// docs/SPRINT_0_6_0.md Block F step 25. The dispatch key is a value the DATABASE hands the
// page: `usetype_presentations.renderer`, reaching the browser as `ViewResponse.presentation
// .renderer` from `GET /view/{document_id}`. The sprint plan's argument for this is that
// adding a renderer becomes "a tuple edit plus a migration that every client picks up — not a
// branch in JavaScript that only this front end has".
//
// SO THIS FILE DOES NOT RESTATE THE TUPLE. `RENDERERS` lives at
// `jmfts_core/models/usetype_presentation.py:14` and a copy of it here would be the second
// list that IC-8 exists to prevent. What is here is one `registerRenderer` call per value;
// `tests/test_web_views.py` reads the tuple out of Python and asserts a bijection against what
// this module registered, in BOTH directions — a value with no renderer fails, and a renderer
// for a value the tuple does not carry fails too.
//
// WHAT IS AND IS NOT IMPLEMENTED HERE. Step 25 was the interface; step 29 finished the two
// renderers it shipped unfinished. `showing` stays and reads null on all five: a registration
// declares null when the renderer renders what its name says, and otherwise a sentence naming
// what the reader is actually looking at, which `render()` puts on the page itself. Step 25's
// argument for the field was that a step-29 author "cannot finish `markdown` and forget to
// take the notice down, because the notice is not theirs to take down" — that worked, and the
// field is what the NEXT unfinished renderer declares rather than a field with no live use.
//
// A RENDERER DRAWS CONTENT. A VIEW DRAWS SOURCE. That is the division, it is the reason this
// signature is synchronous and holds no client, and step 29 is where it was settled rather
// than assumed.
//
// Step 29's brief names four more renderers — `pdf-page`, `image`, `sheet-region`, `office` —
// and the first three cannot exist under this signature. `content` is
// `ViewResponse.rendered_content`, a string; a page image is bytes behind
// `/documents/{id}/image`; the byte routes need a bearer token, so an `<img src>` answers 401
// and the only thing that can fetch one is the generated client, which a renderer does not
// hold. Three ways out were on the table at M3 and this is the one taken:
//
//   A. render() draws content, the view draws source.        <- this
//   B. IC-8 grows an async variant, or takes the client.
//   C. The renderer is handed a surface the view prepared.
//
// A, for two reasons. The token is held in ONE place: a renderer that made calls would be a
// second path the credential and the call log flow through, and step 23's "a view that shows
// something the log has no call for computed it client-side" stops being readable the moment
// library code makes calls the view did not make. And the dispatch key cannot express the
// question anyway — `usetype_presentations.renderer` is keyed by USETYPE, while a page image,
// a crop and an office rendition are properties of the FORMAT: a PDF, a `.docx` and a `.md`
// are all `usetype = 'file'`, so no row could name `pdf-page` for one without naming it for
// the other two. `RENDERERS` therefore stays at five.
//
// WHAT STEP 29 SHIPS INSTEAD OF THOSE THREE is a library the views compose, in
// `render/surfaces.js` and `render/sheet.js`: `pageFigure` (a page with the box on it),
// `regionFigure` (the crop), `gridFigure` (a worksheet region as cells), `pageFor`,
// `anchorPair`, `sourceNotice`, `downloadLink`. Each takes bytes or a response the VIEW
// fetched. That keeps what was actually going to go wrong in one place — `wt/view-tree`
// derived a page's scale and picked an origin corner by hand before the library existed,
// which is the disagreement IC-9 was written to prevent — without moving the credential.
//
// AND THE OFFICE CASE IS THEN NOT A FALLBACK AT ALL. Step 29's brief says the `office`
// renderer "falls back to extracted markdown when no rendition exists, and says so on the
// page". Under A there is no office renderer: an office node's `rendered_content` IS the
// extracted markdown, so `markdown` renders it, and the sentence saying which representation
// the reader is looking at is `surfaces.sourceNotice()` — put on the page by the view, from a
// named evidence row, BEFORE anything is drawn. Nothing catches a failure and substitutes a
// result, which is the only thing the anti-pattern names.

import { renderMarkdown } from "./markdown.js";
import { renderTranscript } from "./transcript.js";

/** A renderer was asked for that nothing registered. */
export class UnknownRendererError extends Error {
  constructor(renderer, known) {
    super(
      `no renderer for ${JSON.stringify(renderer)}. This page knows: ${known.join(", ")}. ` +
        "A renderer named by the database and absent here means this front end is older than " +
        "the appliance — the tuple in jmfts_core/models/usetype_presentation.py grew."
    );
    this.name = "UnknownRendererError";
    this.renderer = renderer;
  }
}

/** A renderer was handed content it cannot render, and says so rather than rendering blank. */
export class RenderError extends Error {
  constructor(message) {
    super(message);
    this.name = "RenderError";
  }
}

const RENDERERS = new Map();

/**
 * Declare the renderer for one value of the server's `RENDERERS` tuple.
 *
 * @param {string} name the value as the database stores it, e.g. "json-table"
 * @param {{showing: string|null, render: (content: string, presentation: object,
 *          document: object) => Element}} renderer
 */
export function registerRenderer(name, renderer) {
  if (RENDERERS.has(name)) {
    throw new RenderError(`two renderers registered for "${name}"`);
  }
  if (typeof renderer?.render !== "function") {
    throw new RenderError(`renderer "${name}" has no render function`);
  }
  if (!("showing" in renderer)) {
    throw new RenderError(
      `renderer "${name}" must declare \`showing\`: null when it renders what its name says, ` +
        "or a sentence naming what the reader is actually looking at. There is no default, " +
        "because the default would be the wrong one for whichever renderer forgot."
    );
  }
  RENDERERS.set(name, Object.freeze({ name, ...renderer }));
}

/** Every registered renderer name, in registration order. */
export function rendererNames() {
  return Object.freeze([...RENDERERS.keys()]);
}

/** One registration, or null. */
export function rendererFor(name) {
  return RENDERERS.get(name) ?? null;
}

/**
 * IC-8. Render one document's content through the renderer its presentation names.
 *
 * @param {string} content `ViewResponse.rendered_content`
 * @param {{renderer: string, child_handling: string, link_handling: string,
 *          renderer_config: object}} presentation `ViewResponse.presentation`
 * @param {object} document the `ViewResponse` itself — id, title, usetype, the link and child
 *   collections. Passed whole because a renderer that needs the node's usetype to choose a
 *   syntax should read it rather than be told it.
 * @returns {Element}
 */
export function render(content, presentation, document_) {
  const name = presentation?.renderer;
  const registered = RENDERERS.get(name);
  if (!registered) throw new UnknownRendererError(name, [...RENDERERS.keys()]);

  const element = registered.render(content, presentation, document_);
  if (!(element instanceof Element)) {
    throw new RenderError(`renderer "${name}" returned ${typeof element}, not an element`);
  }
  if (registered.showing === null) return element;

  // The renderer is not finished, so what it produced is wrapped in something that says so.
  // A reader comparing two documents side by side sees why one looks different from the other
  // instead of assuming this one was written that way.
  const wrapper = document.createElement("div");
  wrapper.className = "render-partial";
  const notice = document.createElement("p");
  notice.className = "render-notice";
  notice.textContent = registered.showing;
  wrapper.append(notice, element);
  return wrapper;
}

// --------------------------------------------------------------------- the five that exist

const pre = (text, className) => {
  const element = document.createElement("pre");
  element.className = className;
  // textContent, never innerHTML: `rendered_content` is document text and this front end is
  // not the place a stored `<script>` gets its chance.
  element.textContent = text;
  return element;
};

registerRenderer("plain", {
  showing: null,
  // Complete. Preformatted text with the whitespace the document has is the whole of what
  // "plain" claims, and there is no step-29 version of it that does more.
  render: (content) => pre(content, "render-plain"),
});

registerRenderer("code", {
  showing: null,
  // Complete, and syntax highlighting is not a missing half of it. Highlighting means a
  // library, a library means a CDN or a build step, and the bundle has neither by design
  // (`jmfts_web/__init__.py`, and `test_nothing_in_the_bundle_loads_from_the_network`). The
  // language goes on the class the way every highlighter expects to find it, so one can be
  // added later without this renderer changing.
  render: (content, presentation) => {
    const language = presentation?.renderer_config?.language;
    const block = pre("", "render-code");
    const code = document.createElement("code");
    if (typeof language === "string" && language) code.className = `language-${language}`;
    code.textContent = content;
    block.append(code);
    return block;
  },
});

registerRenderer("json-table", {
  showing: null,
  // Complete for the two shapes JSON content can have that are tables: a list of objects, and
  // one object. Content that is not JSON RAISES rather than rendering an empty table — a
  // usetype whose presentation says json-table and whose content is prose is a data fault
  // somebody has to see, and an empty table is how it would stay invisible.
  render: (content) => {
    let parsed;
    try {
      parsed = JSON.parse(content);
    } catch (error) {
      throw new RenderError(
        `the json-table renderer was given content that is not JSON: ${error.message}. The ` +
          "usetype's presentation row names json-table; either the content or the row is wrong."
      );
    }
    const rows = Array.isArray(parsed) ? parsed : [parsed];
    if (rows.length === 0) {
      const empty = document.createElement("p");
      empty.className = "dim";
      empty.textContent = "no rows";
      return empty;
    }
    const columns = [];
    for (const row of rows) {
      if (row === null || typeof row !== "object" || Array.isArray(row)) {
        throw new RenderError(
          "the json-table renderer needs objects to make rows out of; this content is a " +
            `list of ${Array.isArray(row) ? "lists" : typeof row}.`
        );
      }
      for (const key of Object.keys(row)) if (!columns.includes(key)) columns.push(key);
    }
    // Built with `createElement` and `append` alone, never `insertRow`/`insertCell`. The two
    // table-specific APIs are the only ones in this module a stubbed DOM would have to
    // implement beyond the four the rest of the bundle uses, and the test harness in
    // `tests/test_web_views.py` runs these renderers under node, which has no DOM at all.
    const table = document.createElement("table");
    table.className = "render-json-table";
    const head = document.createElement("tr");
    for (const column of columns) {
      const cell = document.createElement("th");
      cell.textContent = column;
      head.append(cell);
    }
    table.append(head);
    for (const row of rows) {
      const line = document.createElement("tr");
      for (const column of columns) {
        const cell = document.createElement("td");
        // A column this row does not carry and a column whose value is null are different
        // facts, and the table says which. This is the same distinction part 1 of step 24 was
        // fixing in the capability table, in the one other place a page shows a field set.
        if (!(column in row)) {
          cell.className = "dim";
          cell.textContent = "—";
        } else {
          const value = row[column];
          // A nested object or list is printed as JSON rather than as "[object Object]",
          // which is what `String()` would make of it.
          cell.textContent = value !== null && typeof value === "object"
            ? JSON.stringify(value)
            : String(value);
        }
        line.append(cell);
      }
      table.append(line);
    }
    return table;
  },
});

registerRenderer("markdown", {
  showing: null,
  // Step 25 shipped this as the SOURCE with a notice, and refused to ship half a renderer:
  // "One that handled headings and paragraphs and dropped tables, links and code fences would
  // render most documents plausibly and a few wrongly, and the wrong ones would look like
  // documents that simply had no tables in them." `render/markdown.js` is the whole one, and
  // its header lists what it covers and what it does not — the four it does not (reference
  // links, footnotes, HTML, task lists) render as the literal text they are, which is visibly
  // unrendered rather than invisibly missing.
  //
  // It is also the renderer an OFFICE document reaches, because `office/extract.py` converts
  // `docx` and `pptx` to markdown and that markdown is the node's content. The sentence
  // saying "this is the text read out of a binary document, not a page of it" is the view's,
  // from `surfaces.sourceNotice()`; see this module's header for why it is not this
  // renderer's to say.
  render: (content) => renderMarkdown(content),
});

registerRenderer("transcript", {
  showing: null,
  // Step 25's reason for holding this back was "nothing in this tree states that shape", and
  // that was one grep from being false: `conversation_ingest.conversation_markdown` (`:164`)
  // writes `[role]: content` blocks and is the only writer. `render/transcript.js` reads the
  // shape off that function and says so at the top, so the guess step 25 refused to make is
  // not made here either — it is looked up.
  render: (content) => renderTranscript(content),
});
