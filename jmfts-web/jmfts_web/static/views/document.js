// The document detail view: one node, what it says, where it came from, and what it touches.
//
// docs/SPRINT_0_6_0.md Block F step 28, the second half. `views/tree.js` is the browser that
// walks to here; step 31 (phase F4) is what makes a SEARCH HIT arrive here with its rectangle
// already drawn. This view does not implement the beeline — it is what the beeline lands on,
// and the three things that has to mean are written down rather than left to be discovered:
//
//   1. the anchor is a ROUTE ARGUMENT, so a hit can hand one over without a second call
//      (`#/document?id=42&anchor={"kind":"pdf",...}`) — the encoding is `shell/citation.js`,
//      which this view and `views/search.js` both import so that the writer and the reader of
//      that fragment cannot be two files that drift;
//   2. the surface is built by this view and the box is drawn by `render/overlay.js`, which
//      is IC-9 and which this view CONSUMES rather than reimplements;
//   3. every one of the overlay's five statuses reaches the page as its own sentence, because
//      "no highlight" and "the highlight could not be recovered" are different answers and
//      `jmfts_core/evidence.py:453` is why.
//
// ---------------------------------------------------------------------------------------
// WHAT DRAWS WHAT: A RENDERER DRAWS CONTENT, THIS VIEW DRAWS SOURCE
//
// IC-8 is `(content, presentation, document) -> element`, keyed by the `renderer` value the
// DATABASE hands the page. It is synchronous and it is handed TEXT, which is the right shape
// for `rendered_content` and is not a shape that can fetch anything. The three byte routes
// need a credential — `<img src="/documents/7/image">` sends no Authorization header and
// answers 401 — so the bytes are fetched by the client and shown through an object URL, and
// that fetch belongs to the VIEW.
//
// Step 29 tried the other arrangement for a day — four renderers that fetched, declaring what
// they needed — and reversed it (`2eec3ce`), which left the division above standing and left
// this view holding it. What `render/` ships instead is a LIBRARY OF FUNCTIONS THAT TAKE WHAT
// THE VIEW ALREADY FETCHED: `pageFigure` a Blob, `gridFigure` a cells response, `regionFigure`
// a crop. This view calls the routes and hands over the answers.
//
// SO THE ARITHMETIC IS NOT HERE ANY MORE, and that is this file's half of IC-9's one claim.
// This view had its own `pageSurface`, `cellsSurface` and box-positioning, each of which was a
// second answer to "where does a box go" — and one of them was wrong: an absolute-pixel box on
// a `max-width: 100%` picture stays where the unscaled picture used to be. `drawHighlight`
// positions as a fraction of the surface, and one copy that is right beats two that agree
// today. What stayed here is what nothing in `render/` has: which surface a node HAS, the
// three ways a passage can have no rectangle, and the route a "show page 4" link points at.
//
// ---------------------------------------------------------------------------------------
// A SPREADSHEET REGION IS CELLS, NOT A PICTURE OF CELLS
//
// `services/document_service.py:1548`'s `_pdf_anchor` refuses to crop a `cells` anchor and
// names `GET /documents/{id}/cells` in the refusal; step 22's brief gives the reason ("for a
// sheet, JSON cells are the better answer than a picture of cells"). This view honours that on
// the client side: a sheet node, or any node whose anchor addresses cells, gets a GRID from
// `render/sheet.js` — values, the formulas behind them, and the anchor's range boxed on it by
// the same overlay that boxes a PDF rectangle.

import { registerView } from "../shell/registry.js";
import { broughtACitation, hrefWith, readDetailParams } from "../shell/citation.js";
import { render } from "../render/renderers.js";
import { HIGHLIGHT_STATUS, OverlayError } from "../render/overlay.js";
import { gridFigure } from "../render/sheet.js";
import {
  DPI_DEFAULT,
  anchorPair,
  describeAnchor,
  highlightNotice,
  pageFigure,
  pageFor,
  sourceNotice,
} from "../render/surfaces.js";

/** A route argument this view cannot honour. */
export class DocumentParamError extends Error {
  constructor(message) {
    super(message);
    this.name = "DocumentParamError";
  }
}

//: The two evidence rows, spelled as the wire spells them.
//: `jmfts-client/jmfts_client/contracts/anchor.py` carries `ANCHOR_ROW` and
//: `ANCHOR_UNRESOLVED_ROW` for the same purpose on the Python side, and
//: `jmfts_core/evidence.py:349` and `:473` are the declarations. There is no generated
//: JavaScript copy of either, so these two literals are the client's spelling of them.
export const ANCHOR_ROW = "source_anchor";
export const ANCHOR_UNRESOLVED_ROW = "source_anchor.unresolved";

/** How many children a page of `GET /view/{id}/expand-children` holds. */
export const CHILD_PAGE = 20;

/**
 * Load this view's stylesheet, once.
 *
 * `shell/shell.css:4` is the rule, `views/document.css` is the file, and the call is the first
 * line of `component()` rather than a statement at module scope: `shell/manifest.js` imports
 * every view for its registration alone, and a module that reaches for `document.head` when it
 * is merely imported cannot be imported that way.
 *
 * It styles this view's own chrome and nothing else. The grid a sheet region is drawn on used
 * to be in here too, at a pitch that had to agree with a constant in this module; both moved to
 * `render/sheet.js`, which declares the pitch in the markup so that the surface knows its own
 * extent without measuring a layout.
 */
let linked = false;
function linkStyles() {
  if (linked) return;
  const sheet = document.createElement("link");
  sheet.rel = "stylesheet";
  sheet.href = new URL("./document.css", import.meta.url).href;
  document.head.append(sheet);
  linked = true;
}

const make = (tag, className, text) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
};

const link = (className, text, href) => {
  const element = make("a", className, text);
  // setAttribute, not the reflected property. They are the same thing to a browser, and the
  // attribute is the one a test can read back — `tests/test_web_renderers.py`'s `deep()`
  // serialises attributes, and the alternative was a second DOM stub that serialises
  // properties, which is the thing this repository has exactly one of.
  element.setAttribute("href", href);
  return element;
};

// ------------------------------------------------------------------------ route arguments

/**
 * One integer route argument — `page` or `dpi` — or null when it was not given.
 *
 * `id` does NOT come through here: it belongs to `readDetailParams` in `shell/citation.js`,
 * which is also where the search view's `detailParams` WRITES it — one module, so the two
 * halves of a link cannot come to disagree about what they will accept.
 *
 * Absent and unreadable are different. Absent is a reader who did not ask for a particular
 * page, which the node's own anchor answers; `page=three` is a link that is wrong, and reading
 * it as "no page" would show them page 1 of a document they were sent into the middle of.
 */
export function integerParam(params, name) {
  const raw = params[name];
  if (raw === undefined || raw === "") return null;
  if (!/^\d+$/.test(raw)) {
    throw new DocumentParamError(
      `${name}=${JSON.stringify(raw)} is not a whole number. A page is 0-based, as the ` +
        "anchors are, so the first page is page=0."
    );
  }
  return Number(raw);
}

// THE DETAIL ROUTE'S OWN ARGUMENTS ARE READ BY `shell/citation.js`, NOT HERE ANY MORE.
//
// `readDetailParams`, `broughtACitation` and `hrefWith` were all defined in this file, and
// `readDetailParams` was ALSO defined in `views/search.js` — byte-identical but for the name of
// the error class, because lane 27 and lane 28 were cut from one commit and neither could
// import the other. Both copies said so at the definition and both predicted the same fix:
// identical copies reconcile by DELETING one, which a paraphrase would not. Step 31 deleted
// BOTH and put the encoding under the shell, where the fragment router already lives and where
// a view may depend on it without depending on another view; that file's header is the
// argument for why a third module spends nothing of IC-7's independence.
//
// What it changes here is the name of the error a bad route argument raises: `CitationRouteError`
// rather than this module's `DocumentParamError`, which stays for what is genuinely this view's
// — `integerParam` and `evidenceFromRows`.

/**
 * The anchor pair off a `GET /documents/{id}/evidence` response, plus whether anything tried.
 *
 * The pair itself is `anchorPair` in `render/surfaces.js` — one reader, and this is not a
 * second one. What is added is `attempted`, and it is a distinction that module deliberately
 * does not make: it collapses "absent" and "present and null" because for DRAWING they are the
 * same fact, and they are. For SAYING they are not. 3.2 has them as two facts —
 * `DocumentEvidenceResponse`'s docstring restates it — and "the citation rung ran and placed
 * nothing" is a finished answer while "nothing has run yet" is a reason to come back later.
 * This page says which; `anchorPair` draws the same box either way.
 */
export function evidenceFromRows(response) {
  const rows = response?.evidence;
  if (rows === null || typeof rows !== "object") {
    throw new DocumentParamError(
      "GET /documents/{id}/evidence answers {document_id, evidence}; this response carries " +
        `no evidence object (got ${JSON.stringify(response)}).`
    );
  }
  return {
    ...anchorPair(response),
    attempted: ANCHOR_ROW in rows || ANCHOR_UNRESOLVED_ROW in rows,
  };
}

/**
 * What the `file` evidence row says about the bytes this node came from, or null.
 *
 * Feeds `sourceNotice`, which needs `rendition` as "is `file.rendition_ref` PRESENT" rather
 * than "is it truthy" — `OFFICE_SPEC.md` Part 6 names that key, and a row that carries it with
 * a null value is a rendition that was attempted, which is not the same as one nobody asked
 * for. Null when this page never read the evidence, which is the search-hit case: a sentence
 * about a row nobody read would be a claim rather than a reading.
 */
export function sourceFacts(response) {
  const file = response?.evidence?.file;
  if (file === null || file === undefined || typeof file !== "object") return null;
  return {
    rendition: Object.prototype.hasOwnProperty.call(file, "rendition_ref"),
    filename: file.filename ?? null,
    mime: file.detected_mime ?? null,
  };
}

// -------------------------------------------------------------------------- source panels
//
// Three panels, and none of them owns any geometry. `render/surfaces.js` and `render/sheet.js`
// take what was fetched and hand back a `<figure>` carrying `surface()`, `overlayContainer`
// and `highlight` — the box already drawn and the sentence already written. What is left here
// is the half only a view can do: make the call, and turn "the passage is on page 4" into a
// link to page 4.

/**
 * The route-aware half of a highlight that is not a box.
 *
 * `highlightNotice` in `render/surfaces.js` writes the sentence — "This passage is on page 4."
 * — and cannot write the link, because `render/` knows nothing about this front end's routing
 * and should not: a fragment router is the shell's, and a module that drew boxes would be
 * carrying a second reason to change. So the prose stays there and the NAVIGATION is here.
 * There is exactly one sentence per outcome and it is theirs; this adds a control beside it.
 */
export function pageLink(found, params) {
  if (!found) return null;
  const page =
    found.status === HIGHLIGHT_STATUS.CONTINUED
      ? found.from
      : found.status === HIGHLIGHT_STATUS.ELSEWHERE && found.page !== undefined
        ? found.page
        : null;
  // A `cells` anchor answers ELSEWHERE with a sheet rather than a page, and there is no route
  // to another sheet: `/cells` serves the region this node is addressed at. Null, not a guess.
  if (page === null) return null;
  return link("doc-jump", `Show page ${page + 1}`, hrefWith(params, { page }));
}

/**
 * A rendered page with the passage boxed on it. The beeline's picture.
 *
 * Exported for `tests/test_web_document_view.py`, and for step 31: the beeline is this function
 * reached with an anchor the search hit carried, and nothing else about it is new.
 *
 * `dpi` is SENT rather than left to the server's default, because the scale the box is drawn at
 * is the ratio of the picture's pixels to the page's points and the second of those is derived
 * from the first using this number. A client that assumed the server's default would keep
 * drawing boxes after that default moved, each one off by the ratio.
 */
export async function pagePanel({ client, documentId, page, dpi, evidence, label, params }) {
  const blob = await client.get_document_image({ document_id: documentId, page, dpi });
  const figure = await pageFigure({ blob, page, dpi, evidence, label });
  const jump = pageLink(figure.highlight, params);
  if (jump) figure.append(jump);
  return { element: figure, result: figure.highlight, surface: figure.surface() };
}

/**
 * The grid, with the anchor's range boxed on it.
 *
 * `ref` is omitted rather than sent as null when the caller names none, which asks `/cells` for
 * the node's own region — its `cells` anchor if it has one, the sheet's measured used range
 * otherwise, with `ref_source` saying which. That decision is `_cells_bounds`' and this does
 * not reproduce it.
 */
export async function cellsPanel({ client, documentId, ref, evidence }) {
  const cells = await client.get_document_cells({
    document_id: documentId,
    ...(ref === null || ref === undefined ? {} : { ref }),
  });
  const figure = gridFigure(cells, evidence);
  return { element: figure, result: figure.highlight ?? null, surface: figure.surface() };
}

/**
 * What this node says about its source, plus the button that asks for a picture anyway.
 *
 * The button exists because the appliance is the only thing that KNOWS: a node with no anchor
 * may still sit under a PDF whose page 1 is a perfectly good answer, and the alternative to
 * asking is a page that decided on the reader's behalf that there was nothing to see. What
 * this must not do is fire that call unasked on every text node in the corpus — one log of
 * every call this front end makes is the property step 23 built, and filling it with expected
 * 409s is how that stops being readable.
 *
 * Every refusal reaches the page verbatim. `document_service.py` writes them for a reader —
 * "this is a .docx", "Document 7 has no stored blob", "retry at 218 dpi or below" — and
 * rewriting one into "could not load" throws away the half that says what to do next.
 */
export function sourceOffer({ client, documentId, page, dpi, evidence, facts, label, params }) {
  const panel = make("div");
  // `sourceNotice` is `render/surfaces.js`'s ONE wording for "what is on this page is the text
  // this appliance read out of the document, not a rendered page of it", and step 29's brief
  // requires the page to say it. It is shown only when this page actually read the `file` row
  // it is a statement about — a sentence about a row nobody read would be a claim rather than
  // a reading, and a search hit that handed over its own citation costs no evidence call.
  if (facts !== null) panel.append(sourceNotice(facts));
  panel.append(anchorSummary(evidence));

  const offer = make("button", "tiny", `Render page ${page + 1} of the source anyway`);
  offer.type = "button";
  const into = make("div");
  const load = async () => {
    offer.disabled = true;
    into.textContent = "";
    try {
      const drawn = await pagePanel({ client, documentId, page, dpi, evidence, label, params });
      into.append(drawn.element);
      return drawn;
    } catch (error) {
      into.append(make("p", "bad", error.message));
      return null;
    } finally {
      offer.disabled = false;
    }
  };
  offer.addEventListener("click", load);
  panel.append(offer, into);
  return { element: panel, load, into };
}

// ------------------------------------------------------------------------------ the page

const USETYPE_SHEET = "sheet";
const USETYPE_FILE = "file";
const PDF_MIME = "application/pdf";

/**
 * Which source surface this node has, before anything is fetched.
 *
 * A decision taken from what this page already knows — the anchor, the usetype, and the media
 * type the `file` row recorded — rather than by calling `/image` and reading the refusal.
 * Firing a call that is expected to 409 on every text node is noise in the one log this front
 * end asks to be judged by, and step 23's second property is that the log is the evidence.
 *
 * Where there is nothing to decide from, the panel is OFFERED and not loaded: the reader asks,
 * the appliance answers, and its refusal — "this is a .docx", "there are no stored bytes" — is
 * written to be read and goes on the page as it arrives.
 *
 * `mime` is why a PDF file node opens showing page 1 rather than a button. It is the one case
 * where a node with no anchor certainly HAS a picture: `/image` on a `file` node renders page
 * 0 by that node's own definition (`get_document_image`: "a file node is the whole document").
 * Absent — the search-hit case, where no evidence was read — it falls to the offer, which is
 * the honest answer to "I do not know what these bytes are".
 */
export function plannedSurface({ node, anchor, mime = null }) {
  if (anchor && anchor.kind === "cells") return { kind: "cells" };
  if (anchor && anchor.kind === "pdf") return { kind: "page" };
  if (anchor && anchor.kind === "span") return { kind: "span" };
  // An ooxml anchor is a surface of its own, and the reason it is not `offer` is the reason
  // this whole panel exists. It says exactly where the passage is — a part of the package and
  // an element inside it — and offering "render page 1 anyway" instead would bury that behind
  // a button whose call `/image` refuses by name ("this is a .docx"). A precise address shown
  // is a better answer than a refusal a reader has to ask for.
  if (anchor && anchor.kind === "ooxml") return { kind: "ooxml" };
  if (node.usetype === USETYPE_SHEET) return { kind: "cells" };
  if (node.usetype === USETYPE_FILE && mime === PDF_MIME) return { kind: "page" };
  return { kind: "offer" };
}

/**
 * Where an office passage is, in the source package's own structure. The office beeline's end.
 *
 * NOT A BOX, AND IT SAYS SO IN THOSE WORDS. `render/overlay.js` throws `OverlayError` for an
 * `ooxml` anchor on any surface and its message is exact — "an address in the source package
 * and has no rectangle to draw" — but a thrown error renders as a failure, and this is not one.
 * `OoxmlAnchor`'s docstring is the authority: "**This has no rectangle, and that is not a
 * gap.** A `.docx` has no pages until something paginates it." So the passage is NAMED, with
 * everything the anchor carries about how far it runs, and the sentence underneath says which
 * of the two absences a reader is looking at.
 *
 * The distinction this keeps is the one the whole citation feature turns on, in its office
 * spelling: "we know exactly where this is and cannot draw it yet" is a different fact from "we
 * have no idea where this is", and `source_anchor.unresolved` is the second one. A page that
 * showed nothing for both would be hiding the first, which is a complete record.
 */
export function ooxmlWhere(anchor) {
  const panel = make("div", "doc-address");
  panel.append(make("p", null, `This passage is at ${describeAnchor(anchor)}.`));

  const runs = [];
  if (anchor.slide !== undefined && anchor.slide !== null) runs.push(`slide ${anchor.slide}`);
  // `through` bounds the run of elements inside the SAME part; `continues` names the further
  // PARTS a passage leaves into — a chunk running from a slide into its speaker notes. Two
  // fields because neither can be read as the other (`OoxmlAnchor.continues`), and a viewer
  // that printed only the first would present a partial address as a whole one, which is the
  // office spelling of `PdfAnchor.continues`' own argument.
  if (typeof anchor.through === "string") runs.push(`through ${anchor.through} of that part`);
  if (Array.isArray(anchor.continues) && anchor.continues.length) {
    runs.push(`continuing into ${anchor.continues.join(", ")}`);
  }
  if (runs.length) panel.append(make("p", "dim", `It runs ${runs.join(", ")}.`));

  panel.append(
    make(
      "p",
      "overlay-notice",
      "That address is exact and nothing is missing from the record: `anchor:source` recovered " +
        "it from the uploaded package itself. What there is no box for is a page — an office " +
        "document has no pages until `render:pdf` paginates it, and recovering a rectangle " +
        "from that rendition is docs/SPRINT_0_6_0.md Block G step 33. This is not a highlight " +
        "that could not be recovered; see source_anchor.unresolved for what that reads like."
    )
  );
  return panel;
}

/**
 * What this node's evidence says about where it came from, when there is no surface to draw.
 *
 * Five answers and they are five different facts. The two that a viewer is most tempted to
 * collapse are the two the anchor contract separates hardest: a row that is ABSENT means
 * nothing has tried, a row that is PRESENT AND NULL means an atom ran and produced nothing
 * (`DocumentEvidenceResponse`, 3.2), and `source_anchor.unresolved` means it tried and failed
 * and recorded why. "No highlight" for all three is how the third one stays invisible.
 */
export function anchorSummary(evidence) {
  const { anchor, unresolved, attempted } = evidence;
  if (anchor === null && unresolved !== null) {
    // The sentence is `render/surfaces.js`'s, reached through the status vocabulary rather
    // than rewritten here. There is one wording for "no rectangle could be recovered, and here
    // is the reason the recorder wrote", and a second one in a view is how the two drift.
    return make(
      "p",
      "overlay-notice",
      highlightNotice({
        status: HIGHLIGHT_STATUS.UNRESOLVED,
        code: unresolved.code,
        reason: unresolved.reason,
      })
    );
  }
  if (anchor === null) {
    return make(
      "p",
      "dim",
      attempted === false
        ? "No source_anchor row and no unresolved row: nothing has tried to place this node " +
          "in its source. Two rungs write one — `citation` over a PDF text layer, and " +
          "`anchor:source` over a .docx or .pptx package. A WORKBOOK reaches neither: nothing " +
          "in this appliance writes a `cells` anchor today, so a spreadsheet passage is " +
          "addressed by the sheet region its node stands for and by nothing finer."
        : "This node carries no anchor into its source."
    );
  }
  // An `ooxml` anchor reaching HERE is a node whose planned surface was something else — a
  // reader who asked for `?page=` on an office node, say. It is still exactly addressed, so it
  // gets the same sentence the panel gives it rather than being lumped in with an unknown kind.
  if (anchor.kind === "ooxml") return ooxmlWhere(anchor);
  return make(
    "p",
    "overlay-notice",
    `This node's anchor is of kind ${JSON.stringify(anchor.kind)}, which this front end has ` +
      "no geometry for — it is older than the appliance that wrote the anchor. " +
      "jmfts-client/jmfts_client/contracts/anchor.py is the authority on the kinds."
  );
}

function head(view, node) {
  const bar = make("div", "doc-head");
  bar.append(make("h1", null, view.title || `Document #${view.id}`));
  bar.append(make("span", "pill", view.usetype || "no usetype"));
  bar.append(make("span", "dim mono", `#${view.id}`));
  if (node.settled !== "settled") {
    bar.append(make("span", "bad", `${node.settled} — not yet reachable by any search`));
  }
  if (node.produced_by) bar.append(make("span", "dim", `produced by ${node.produced_by}`));
  bar.append(
    make("span", view.can_write ? "dim" : "bad", view.can_write ? "writable" : "read-only")
  );
  return bar;
}

function crumbs(view) {
  const bar = make("div", "doc-crumbs");
  bar.append(link("dim", "all roots", "#/documents"));
  for (const ancestor of view.ancestors) {
    bar.append(make("span", "dim", "/"));
    bar.append(link(null, ancestor.title || `#${ancestor.id}`, `#/document?id=${ancestor.id}`));
  }
  bar.append(make("span", "dim", "/"));
  bar.append(make("span", null, view.title || `#${view.id}`));
  bar.append(link("dim", "browse the subtree", `#/documents?root=${view.id}`));
  return bar;
}

/**
 * The create-link control, or the sentence that says why there is not one.
 *
 * IC-3 exists for this paragraph. `ViewResponse.can_write` reports whether the CALLING
 * principal may write this node, from the same `jmfts_core.access.can_write` the repository
 * gate calls — so a control offered here and a call refused there cannot disagree. Before it,
 * a page had to fire the write and read the 403 back to find out, which is a button that lies.
 *
 * The form is returned with its `submit` beside it so a test can drive it without inventing a
 * DOM event, and so the failure path is one function rather than a handler.
 */
export function createLinkControl({ view, client, onCreated }) {
  const panel = make("div");
  if (!view.can_write) {
    panel.append(
      make(
        "p",
        "dim",
        "This token may not write to this document, so no create-link control is offered. " +
          "That is ViewResponse.can_write reporting the access the appliance would enforce, " +
          "not a guess — offering the control and letting the call 403 is the button that lies."
      )
    );
    return { element: panel, submit: null };
  }

  const form = make("form", "doc-form");
  const target = make("input");
  target.setAttribute("placeholder", "target document id");
  target.setAttribute("aria-label", "target document id");
  const type = make("input");
  type.value = "related";
  type.setAttribute("placeholder", "link type");
  type.setAttribute("aria-label", "link type");
  const button = make("button", null, "Create link");
  button.type = "submit";
  const said = make("span", "dim", "");
  form.append(target, type, button, said);
  panel.append(form);

  const submit = async () => {
    const id = String(target.value ?? "").trim();
    const linkType = String(type.value ?? "").trim();
    // The form's own validation, which is the half of Fail Early that belongs to a form: an
    // input that cannot be a request does not become one. Everything the SERVER decides — a
    // target that does not exist, an edge it refuses — is left to the server and shown.
    if (!/^\d+$/.test(id) || !linkType) {
      said.className = "bad";
      said.textContent = "a target document id (a whole number) and a link type are both needed";
      return null;
    }
    said.className = "dim";
    said.textContent = "creating…";
    try {
      const created = await client.create_link({
        document_id: view.id,
        request: { source_id: view.id, target_id: Number(id), link_type: linkType, score: 1.0 },
      });
      said.className = "dim";
      said.textContent = `created link #${created.id}`;
      if (onCreated) onCreated(created);
      return created;
    } catch (error) {
      said.className = "bad";
      said.textContent = error.message;
      return null;
    }
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });
  return { element: panel, submit, fields: { target, type }, said };
}

function linkRows(view) {
  const table = make("table");
  const edges = [...view.outbound_links, ...view.inbound_links];
  if (edges.length === 0) {
    return make("p", "dim", "no links on this node");
  }
  for (const edge of edges) {
    const line = make("tr");
    line.append(make("th", null, `${edge.direction} ${edge.link_type}`));
    const cell = make("td");
    const other = edge.direction === "outbound" ? edge.target_id : edge.source_id;
    cell.append(link(null, edge.title || `#${other}`, `#/document?id=${other}`));
    cell.append(make("span", "dim", ` score ${edge.score}`));
    line.append(cell);
    table.append(line);
  }
  return table;
}

function tripleRows(view) {
  if (view.triples.length === 0) return make("p", "dim", "no triples on this node");
  const table = make("table");
  for (const triple of view.triples) {
    const line = make("tr");
    line.append(make("th", null, triple.predicate_name || "(no predicate)"));
    const cell = make("td");
    cell.append(
      link(
        null,
        triple.subject_title || `#${triple.subject_id}`,
        `#/document?id=${triple.subject_id}`
      )
    );
    cell.append(make("span", "dim", " → "));
    if (triple.object_id === null) {
      // A literal object has no node to navigate to, which is why the contract gives it a
      // null `object_url` rather than a `/view/None`. Shown as the literal it is.
      cell.append(make("span", null, triple.object_literal ?? "(null literal)"));
      if (triple.object_datatype) cell.append(make("span", "dim", ` ^^${triple.object_datatype}`));
    } else {
      cell.append(
        link(
          null,
          triple.object_title || `#${triple.object_id}`,
          `#/document?id=${triple.object_id}`
        )
      );
    }
    line.append(cell);
    table.append(line);
  }
  return table;
}

/**
 * The children `/view/` is willing to show, and the button that asks for the next page.
 *
 * Returns `{element, loadMore}` — `loadMore` is null when there is nothing more to ask for —
 * because a click event is not a thing a headless test can honestly produce and a handler
 * that is also a function is one less seam than a handler that is only a handler.
 */
export function childStubs(view, { client }) {
  const panel = make("div");
  if (view.presentation.child_handling === "hidden") {
    // `view_renderer.build_children_stubs` returns [] for this handling, so an empty list here
    // is a PRESENTATION decision and not a fact about the tree. Saying which of the two it is
    // costs one sentence; not saying it makes a subtree disappear from the one page a reader
    // is looking at it on.
    panel.append(
      make(
        "p",
        "dim",
        'This usetype\'s presentation sets child_handling: "hidden", so /view/ serves no ' +
          "child stubs for it. Whatever is under this node is under it still — the tree " +
          "browser reads /documents/{id}/children, which no presentation row filters."
      )
    );
    panel.append(link(null, "Browse this subtree", `#/documents?root=${view.id}`));
    return { element: panel, loadMore: null };
  }
  if (view.children_stubs.length === 0) {
    panel.append(make("p", "dim", "no children"));
    return { element: panel, loadMore: null };
  }

  const list = make("div");
  const draw = (stubs) => {
    for (const stub of stubs) {
      const box = make("div", "doc-stub");
      box.append(link(null, stub.title || `#${stub.id}`, `#/document?id=${stub.id}`));
      box.append(make("span", "pill", stub.usetype || "no usetype"));
      if (stub.child_count) box.append(make("span", "dim", ` ${stub.child_count} children`));
      if (stub.preview) box.append(make("p", null, stub.preview));
      list.append(box);
    }
  };
  draw(view.children_stubs);
  panel.append(list);

  let offset = view.children_stubs.length;
  const more = make("button", "tiny", "More children");
  more.type = "button";
  const said = make("span", "dim", "");
  const load = async () => {
    more.disabled = true;
    try {
      const page = await client.expand_children({
        document_id: view.id,
        offset,
        limit: CHILD_PAGE,
      });
      draw(page);
      offset += page.length;
      if (page.length < CHILD_PAGE) {
        more.hidden = true;
        said.textContent = `${offset} children, which is all of them`;
      }
    } catch (error) {
      said.className = "bad";
      said.textContent = error.message;
    } finally {
      more.disabled = false;
    }
  };
  more.addEventListener("click", load);
  panel.append(more, said);
  return { element: panel, loadMore: load };
}

function section(page, title) {
  page.append(make("h2", null, title));
  const panel = make("div", "doc-panel");
  page.append(panel);
  return panel;
}

function landing(page) {
  page.append(make("h1", null, "Document"));
  page.append(
    make(
      "p",
      "lede",
      "This view shows one node: its content through the renderer its usetype names, the " +
        "page or the cells it came from with the passage boxed on them, and everything it " +
        "links to. It needs to be told which node — #/document?id=42."
    )
  );
  page.append(link(null, "Browse the tree instead", "#/documents"));
  return page;
}

async function component({ client, params }) {
  linkStyles();
  const page = make("div", "view-document");

  // No id at all is a reader who has not said which document — the shell reads an empty
  // fragment the same way, and for the same reason: ambivalence is not a failure. An id that
  // is PRESENT and not a number is a link that is wrong, and `readDetailParams` refuses it.
  if (params.id === undefined || params.id === "") return landing(page);
  const { documentId: id, anchor, unresolved } = readDetailParams(params);

  const [view, node, back] = await Promise.all([
    client.view_document({ document_id: id }),
    client.get_document({ document_id: id }),
    client.get_back_references({ document_id: id, limit: 20 }),
  ]);

  // One call, and only when nobody has already paid for it. See `broughtACitation`. Note what
  // is NOT asked for: `get_document` defaults `include_embed=false`, and this page never sets
  // it — 768 floats is about 15 kB of JSON per row and there is nothing on this page that
  // reads a vector.
  const read = broughtACitation(params)
    ? null
    : await client.get_document_evidence({ document_id: id });
  const evidence = read === null ? { anchor, unresolved } : evidenceFromRows(read);
  const facts = read === null ? null : sourceFacts(read);
  const label = view.title || `document ${view.id}`;

  page.append(head(view, node));
  page.append(crumbs(view));

  // ------------------------------------------------------------------ the source panel
  const source = section(page, "Source");
  const requested = integerParam(params, "page");
  const dpi = integerParam(params, "dpi") ?? DPI_DEFAULT;
  const planned = plannedSurface({ node, anchor: evidence.anchor, mime: facts?.mime ?? null });
  // A page named on the route wins, and it wins even over a `cells` node: a reader who typed
  // `?page=` asked a question, and answering a different one is the failure this whole panel
  // is written against. `/image` refuses a worksheet by name and the refusal goes on the page.
  const shown = requested === null ? planned.kind : "page";

  try {
    if (shown === "page") {
      // WHICH PAGE is `pageFor`'s decision and not this view's: the route's number, else page
      // 0 for a file node, else the node's own pdf anchor, else a refusal naming what to do.
      // The number it returns is then SENT as `?page=`, so the server never takes the same
      // decision from the other end and the two cannot disagree.
      const drawn = await pagePanel({
        client,
        documentId: id,
        page: pageFor(view, evidence, requested),
        dpi,
        evidence,
        label,
        params,
      });
      source.append(drawn.element);
    } else if (shown === "cells") {
      const drawn = await cellsPanel({ client, documentId: id, ref: null, evidence });
      source.append(drawn.element);
    } else if (shown === "span") {
      // A span IS a valid anchor and it has no geometry — the overlay refuses to draw one on a
      // page and says to highlight the text instead. The text is right here in
      // `rendered_content`, so the passage itself is what this panel shows.
      const { char_start: start, char_end: end } = evidence.anchor;
      if (facts !== null) source.append(sourceNotice(facts));
      source.append(
        make("p", "dim", `This passage is at ${describeAnchor(evidence.anchor)} of that text.`)
      );
      source.append(make("pre", "render-plain", view.rendered_content.slice(start, end)));
    } else if (shown === "ooxml") {
      // THE OFFICE BEELINE ENDS HERE, AND IT ENDS NAMED RATHER THAN BOXED. What this panel can
      // tell a `.docx` or `.pptx` hit is which representation of the document the page is
      // showing (`sourceNotice`) and where in the package the passage is (`ooxmlWhere`). The
      // passage itself is the Content panel below and is not repeated here — unlike a `span`
      // anchor, which cuts a slice out of that same text and therefore has something of its own
      // to show. NO CALL IS MADE: `/image` refuses an office node by name and `/region` has no
      // rectangle to crop to, so firing either would spend an entry in the one log this front
      // end asks to be judged by on a refusal this page can already predict.
      if (facts !== null) source.append(sourceNotice(facts));
      source.append(ooxmlWhere(evidence.anchor));
    } else {
      const offered = sourceOffer({
        client,
        documentId: id,
        // The offer is for page 1 — the only page a node with no anchor can be offered without
        // guessing which page it came from, and `pageFor` refuses to guess that for good
        // reason. The reader asking is what makes it a question rather than an assertion.
        page: 0,
        dpi,
        evidence,
        facts,
        label,
        params,
      });
      source.append(offered.element);
    }
  } catch (error) {
    // Everything above can refuse, and every refusal is written to be read: an OverlayError
    // that names the text renderer, a `SurfaceError` that says to name a page, an HTTP error
    // carrying the appliance's own sentence. They go on the page as they arrived.
    source.append(make("p", "bad", error.message));
  }

  // ---------------------------------------------------------------------- the content
  const content = section(page, `Content — ${view.presentation.renderer}`);
  try {
    content.append(render(view.rendered_content, view.presentation, view));
  } catch (error) {
    // Shown here rather than thrown to the shell, which would replace the whole page with one
    // message and take the links, the children and the source panel down with it. An
    // unknown renderer means this front end is older than the appliance; that is worth
    // reading WITH the rest of the node still on screen.
    content.append(make("p", "bad", error.message));
  }

  // --------------------------------------------------------------------- the relations
  section(page, "Children").append(childStubs(view, { client }).element);

  const links = section(page, "Links");
  links.append(linkRows(view));
  links.append(createLinkControl({ view, client }).element);

  section(page, "Triples").append(tripleRows(view));

  const references = section(page, "Back-references");
  if (back.total === 0) {
    references.append(make("p", "dim", "nothing links or asserts at this node"));
  } else {
    references.append(make("p", "dim", `${back.total} in total, ${back.references.length} shown`));
    for (const reference of back.references) {
      const box = make("div", "doc-stub");
      box.append(
        link(
          null,
          reference.title || `#${reference.document_id}`,
          `#/document?id=${reference.document_id}`
        )
      );
      box.append(make("span", "pill", `via ${reference.via}`));
      box.append(make("span", "dim", ` ${reference.relation}`));
      if (reference.snippet) box.append(make("p", null, reference.snippet));
      references.append(box);
    }
  }

  return page;
}

registerView({
  id: "document",
  title: "Document",
  path: "/document",
  component,
  tags: ["corpus", "documents"],
});
