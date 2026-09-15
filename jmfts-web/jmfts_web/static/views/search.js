// The search view: one query, seven methods, side by side.
//
// docs/SPRINT_0_6_0.md Block F step 27. The plan says what this page is for and it says it
// plainly: "Step 27's side-by-side is the demonstration, not a debug affordance. The same query
// run through `vector`, `bm25`, `hybrid` and `maxsim` in four columns is the shortest
// explanation of what this appliance is that anybody has written, and it is four calls the log
// shows." So the columns ARE the layout. Nothing on this page is arranged around one preferred
// ranking with the others tucked behind a toggle, because the difference between the rankings
// is the thing being shown.
//
// SEVEN, NOT FOUR, AND THE COUNT COMES FROM THE ROUTE TABLE. `/search/` mounts eight
// operations. Seven of them answer a query with a ranked list — `vector`, `fulltext`, `bm25`,
// `maxsim` (the four in `SEARCH_METHODS`), `hybrid` (their fusion), `auto` (the router that
// picks one and says why) and `quick` (the same algorithms over GET, which is the only one of
// the seven that fits in a URL). The eighth, `synthesize`, does not return a ranking at all; it
// returns prose, so it is a panel under the columns rather than an eighth column.
// `METHODS` below is that table, and `tests/test_web_search_view.py` checks it against the
// generated `client/operations.js` in BOTH directions — a `/search/` operation with no column
// fails, and a column naming an operation that does not exist fails.
//
// WHY `quick` GETS A COLUMN. It looks like a duplicate of whichever method it is asked to run,
// and that is exactly what makes it worth drawing: when the GET column and the POST column of
// the same name disagree, something in the two paths resolved differently, and the side-by-side
// is where that becomes visible instead of staying a thing nobody compares. It also carries a
// real asymmetry a reader should meet — `GET /search/` takes no `exclude_types` parameter, so
// the "show what is normally held out" toggle CANNOT apply to it, and the column says so rather
// than quietly running a different request from its neighbours.
//
// A SEARCH IS A URL. The shell routes on the fragment and hands a view its query string
// (`shell/shell.js`, "a view reads its arguments from the fragment's own query string"), so the
// form does not run the search — it navigates, and the mount runs the search from the fragment.
// `#/search?q=late+interaction&m=vector,maxsim` is therefore a link somebody can send, and the
// page has no state that a reload loses. `requestFromParams` is the one reader of that query
// string and it REFUSES what it cannot honour: an unknown method name, a duplicate, a limit
// outside the contract's 1..100. That mirrors `validate_search_methods` in
// `jmfts-client/jmfts_client/contracts/search.py`, which refuses the same three for the same
// reason — "an unrecognised name contributes nothing and raises nothing", so the caller gets a
// search they did not ask for and no way to tell.
//
// ZERO IS AN ANSWER AND IT IS RENDERED AS ONE. Every selected method gets a column whatever it
// returned, and a column that returned nothing says so with its count, its `applied` block and,
// where the corpus itself explains it, the reason: an appliance with no token vectors returns
// nothing from `maxsim` whatever the query, and that is a fact about this appliance rather than
// about this query. The page reads those from `/capabilities?corpus=true` once, at mount. A
// blank area under a method name is the failure this page is written against.
//
// THE ANCHOR IS CARRIED, NOT REDRAWN. IC-4 puts `source_anchor` and `source_anchor_unresolved`
// on every hit precisely so a result list does not have to call `/documents/{id}/evidence` per
// hit before it can offer "show me where". Step 31 draws the box; this page's job is to show
// which of the states a hit is in and to hand BOTH rows to the detail view without losing them.
// THE ENCODING OF THAT HAND-OFF IS NO LONGER HERE: it is `shell/citation.js`, which both this
// view and the detail view import. It lived here and in `views/document.js` at once, identical
// but for an error class name, because the two lanes that wrote it could not import each other;
// step 31 deleted both copies rather than picking one, and that file's header is the argument
// for why a third module spends nothing of IC-7's independence. The state vocabulary shares its
// two absence words with IC-9's `HIGHLIGHT_STATUS` by importing them, because "no anchor" and
// "an anchor that could not be recovered" are the two the whole citation feature turns on and
// two spellings of them is how they end up meaning different things on two pages.

import { HIGHLIGHT_STATUS } from "../render/overlay.js";
import { registerView } from "../shell/registry.js";
import {
  DETAIL_PATH,
  detailHref as citationHref,
  detailParams as citationParams,
} from "../shell/citation.js";

/** This page asked a response for a field its contract does not carry. */
export class ContractError extends Error {
  constructor(message) {
    super(message);
    this.name = "ContractError";
  }
}

/** The fragment named a search this page cannot run, and says which part it could not read. */
export class SearchRequestError extends Error {
  constructor(message) {
    super(message);
    this.name = "SearchRequestError";
  }
}

// ------------------------------------------------------------------- what this page reads
//
// Every path this view reads out of a response, by the contract that defines it. The capability
// landing (`views/capabilities.js`) carries the same declaration and the same `field()` walk,
// and it carries them because of a defect worth not repeating: three rows read field names
// `CapabilitiesResponse` does not have, and all three rendered as the word "none", because
// `undefined || []` is a perfectly good empty list and nothing anywhere said otherwise.
//
// The two pages do NOT share the helper. A shared `field()` would make one view import another
// view's module, and `shell/registry.js` is the only file a view may depend on besides the
// client and the renderers — five worktrees adding five views in parallel is the arrangement
// that buys, and one view importing another spends it. What is shared is the discipline and the
// check: `tests/test_web_search_view.py` resolves every path below against the Pydantic model
// named on its left, so a field renamed in `jmfts-client` fails in the suite rather than on
// somebody's screen.

export const READS = Object.freeze({
  "meta.CapabilitiesResponse": Object.freeze([
    "retrieval.search_exclude_usetypes",
    "llm.configured",
    "llm.model",
    "llm.requires_llm",
    "corpus",
    "corpus.documents",
    "corpus.documents_with_vectors",
    "corpus.documents_with_token_vectors",
    "corpus.bm25_indexes",
  ]),
  "search_context.SearchContextResponse": Object.freeze(["name", "description", "config"]),
  "search.SearchResponse": Object.freeze([
    "results",
    "total",
    "latency_ms",
    "applied",
    "applied.methods",
    "applied.weights",
    "applied.exclude_types",
    "applied.usetype",
    "applied.parent_id",
    "applied.limit",
    "applied.truncated",
  ]),
  // The SAME eleven, plus the routing block. Restated rather than shared with the list above
  // because they are two contracts: `AutoSearchResponse` is a different class that happens to
  // carry the same fields today, and a shared list would stop noticing the day one of them
  // moves. The suite resolves both lists against their own model.
  "search.AutoSearchResponse": Object.freeze([
    "results",
    "total",
    "latency_ms",
    "applied",
    "applied.methods",
    "applied.weights",
    "applied.exclude_types",
    "applied.usetype",
    "applied.parent_id",
    "applied.limit",
    "applied.truncated",
    "routing",
    "routing.method",
    "routing.reason",
  ]),
  "search.SearchResultItem": Object.freeze([
    "document",
    "score",
    "method",
    "source_anchor",
    "source_anchor_unresolved",
  ]),
  "document.DocumentResponse": Object.freeze([
    "id",
    "title",
    "usetype",
    "content",
    "content_source",
    "produced_by",
  ]),
  "search.SynthesizeResponse": Object.freeze([
    "synthesis",
    "sources",
    "llm_model",
    "search_latency_ms",
    "total_latency_ms",
    "llm_available",
  ]),
  "search.SourceReference": Object.freeze(["document_id", "title", "score", "method"]),
});

/**
 * A reader for one contract: `reader("search.SearchResponse")(response, "applied.limit")`.
 *
 * A key that is PRESENT and null is a value and comes back as null — `applied.usetype` is null
 * on a search that named no positive filter, and that is an answer. A key that is ABSENT is not
 * an answer, and the difference between the two is the whole of this function.
 */
export function reader(model) {
  const declared = READS[model];
  if (!declared) {
    throw new ContractError(
      `nothing in this view declares reads against ${model}; READS is what the suite resolves ` +
        "against the contracts in jmfts-client."
    );
  }
  return (response, path) => {
    if (!declared.includes(path)) {
      throw new ContractError(
        `"${path}" is not declared under ${model} in this view's READS. Add it there — that ` +
          "list is what tests/test_web_search_view.py resolves against the Pydantic model."
      );
    }
    let node = response;
    const walked = [];
    for (const key of path.split(".")) {
      if (node === null) {
        throw new ContractError(
          `"${walked.join(".")}" is null, so "${path}" cannot be read through it. Read the ` +
            "nullable field first and branch on it."
        );
      }
      if (typeof node !== "object" || Array.isArray(node) || !(key in node)) {
        throw new ContractError(
          `${model} carries no ` +
            (walked.length ? `"${key}" under "${walked.join(".")}"` : `"${key}"`) +
            ` (reading "${path}"). The appliance and this page disagree; the contract in ` +
            "jmfts-client is the authority."
        );
      }
      node = node[key];
      walked.push(key);
    }
    return node;
  };
}

const readCapabilities = reader("meta.CapabilitiesResponse");
const readContext = reader("search_context.SearchContextResponse");
const readResponse = reader("search.SearchResponse");
const readAuto = reader("search.AutoSearchResponse");
const readHit = reader("search.SearchResultItem");
const readDocument = reader("document.DocumentResponse");
const readSynthesis = reader("search.SynthesizeResponse");
const readSource = reader("search.SourceReference");

// ------------------------------------------------------------------------- the seven, and one
//
// One row per operation, and the row carries the argument shape as a function rather than a
// description of one. `kind` is what the column header says this method IS: four of them are
// the retrieval methods `SEARCH_METHODS` names, one fuses them, one routes between them, and
// one is a transport over the rest.

/** The `method` values `GET /search/` accepts: the four, plus their fusion. */
export const GET_METHODS = Object.freeze(["hybrid", "vector", "fulltext", "bm25", "maxsim"]);

/** A body for the four single-method searches. Omitted keys take the server's own default. */
function searchBody(request) {
  const body = { query: request.query, limit: request.limit };
  if (request.usetype) body.usetype = request.usetype;
  if (request.parentId !== null) body.parent_id = request.parentId;
  // `[]` disables exclusion entirely; OMITTING the field applies the appliance's default
  // (`JMFTS_SEARCH_EXCLUDE_USETYPES`). Sending the default back would be this page deciding
  // what is held out, which is the server's decision and is echoed in `applied.exclude_types`.
  if (request.includeExcluded) body.exclude_types = [];
  return body;
}

const withContext = (request, args) =>
  request.context ? { ...args, context: request.context } : args;

export const METHODS = Object.freeze([
  Object.freeze({
    id: "vector",
    kind: "retrieval",
    title: "vector",
    path: "POST /search/vector",
    op_id: "SearchService.vector_search",
    what: "HNSW cosine over the 768-dim document embedding.",
    args: (request) => withContext(request, { request: searchBody(request) }),
  }),
  Object.freeze({
    id: "fulltext",
    kind: "retrieval",
    title: "fulltext",
    path: "POST /search/fulltext",
    op_id: "SearchService.fulltext_search",
    what: "PostgreSQL ts_vector, with a trigram fallback.",
    args: (request) => withContext(request, { request: searchBody(request) }),
  }),
  Object.freeze({
    id: "bm25",
    kind: "retrieval",
    title: "bm25",
    path: "POST /search/bm25",
    op_id: "SearchService.bm25_search",
    what: "The inverted index, k1/b tuned. A workbook's record nodes reach it.",
    args: (request) => withContext(request, { request: searchBody(request) }),
  }),
  Object.freeze({
    id: "maxsim",
    kind: "retrieval",
    title: "maxsim",
    path: "POST /search/maxsim",
    op_id: "SearchService.maxsim_search",
    what: "ColBERT-style late interaction over the per-token 256-dim matryoshka vectors.",
    args: (request) => withContext(request, { request: searchBody(request) }),
  }),
  Object.freeze({
    id: "hybrid",
    kind: "fusion",
    title: "hybrid",
    path: "POST /search/hybrid",
    op_id: "SearchService.hybrid_search",
    // `methods` is deliberately NOT sent. Omitting it runs DEFAULT_HYBRID_METHODS with the
    // tuned weights, and `applied.methods`/`applied.weights` report what that resolved to —
    // which is the honest way to show a default, rather than this page naming one.
    what: "Reciprocal Rank Fusion. The methods it fused are in its applied block.",
    args: (request) => withContext(request, { request: searchBody(request) }),
  }),
  Object.freeze({
    id: "auto",
    kind: "routed",
    title: "auto",
    path: "POST /search/auto",
    op_id: "SearchService.auto_search",
    what: "A heuristic router reads the query, picks one method and says why.",
    args: (request) => withContext(request, { request: searchBody(request) }),
  }),
  Object.freeze({
    id: "quick",
    kind: "transport",
    title: "quick (GET)",
    path: "GET /search/",
    op_id: "SearchService.quick_search",
    what: "The same algorithms in a URL. It takes no exclude_types, so it keeps the default.",
    args: (request) =>
      withContext(request, {
        q: request.query,
        limit: request.limit,
        method: request.getMethod,
        ...(request.usetype ? { usetype: request.usetype } : {}),
        ...(request.parentId !== null ? { parent_id: request.parentId } : {}),
      }),
  }),
]);

/** The eighth `/search/` operation. Prose, not a ranking, so it is a panel and not a column. */
export const SYNTHESIS_OP = "SearchService.synthesize_search";

/**
 * The one `/search-contexts/` operation this page calls.
 *
 * `list_contexts` returns whole `SearchContextResponse` rows, `config` included, so calling
 * `get_context` for the selected preset would fetch a row this page is already holding — a call
 * whose answer it already has. The three write verbs (`create`, `update`, `delete`) are
 * deliberately absent: a preset editor is CRUD, it has a surface of its own, and
 * `docs/SPRINT_0_6_0.md` Part 3 has the preset question open on 4.6. Reading them here is what
 * step 27 needs; writing them here would be this page taking a decision that is still open.
 */
export const CONTEXT_OP = "SearchContextService.list_contexts";

/**
 * The one operation this page calls that is not a search.
 *
 * It answers what the corpus can do, which is what turns a zero into an explained zero. Called
 * through `client.call` like every other operation rather than through its generated verb, so
 * that one rule holds for the whole page: every call this view makes names an operation id, and
 * `runSearch` can check those ids against the generated table before it sends anything.
 */
export const CAPABILITIES_OP = "MetaService.capabilities";

// -------------------------------------------------------------------------- the anchor states

/**
 * What a hit's two anchor fields say, as one word.
 *
 * `NONE` and `UNRESOLVED` are IC-9's own words, imported rather than respelled: `evidence.py`
 * :453 says the unresolved row is "present exactly when `anchor` is not", and the whole
 * citation feature turns on a reader being able to tell "nothing was ever recorded here" from
 * "something tried to recover a rectangle and could not, and here is why". Two spellings of
 * those two is how the overlay and the result list end up meaning different things.
 *
 * `ADDRESSED` IS THE THIRD ANSWER AND IT IS THE ONE STEP 31 ADDED. An `ooxml` anchor says
 * exactly where a passage is — `/w:document/w:body/w:p[42]` of `word/document.xml`, recovered
 * from the uploaded package by `anchor:source` — and has no rectangle, because a `.docx` has no
 * pages until something paginates it (`OoxmlAnchor`, "this has no rectangle, and that is not a
 * gap"). Reading that as `ANCHORED` would promise a box the detail view cannot draw; reading it
 * as `UNREADABLE`, which is what this page did until step 31, says "this front end is older
 * than the appliance" about a kind it can name precisely. **A reader has to be able to tell "we
 * know exactly where this is and cannot draw it yet" from "we have no idea where this is",** and
 * one word for both is how that distinction stops existing.
 *
 * The last three are states only a LIST can be in, because it looks at many hits and one of
 * them may be malformed. `CONTRADICTORY` is both rows at once, which `evidence.py` says the
 * appliance cannot produce; `UNREADABLE` is an anchor this front end cannot describe — an
 * unknown `kind` (this page really is older than the appliance) or a known kind missing the
 * fields that say where it points. Neither takes the page down: a hit whose anchor is wrong is
 * still a hit, and a rendering that threw would hide the six good hits beside it. They are shown
 * in the bad colour with the reason on the badge, which is showing the fault rather than
 * swallowing it.
 */
export const ANCHOR_STATE = Object.freeze({
  ANCHORED: "anchored",
  ADDRESSED: "addressed",
  UNRESOLVED: HIGHLIGHT_STATUS.UNRESOLVED,
  NONE: HIGHLIGHT_STATUS.NONE,
  CONTRADICTORY: "contradictory",
  UNREADABLE: "unreadable",
});

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

/**
 * Classify one hit's anchor, and say where it points in words.
 *
 * @param {object} hit a `SearchResultItem`
 * @returns {{state: string, where: string, detail: string|null, anchor: object|null,
 *            unresolved: object|null}}
 */
export function anchorState(hit) {
  const anchor = readHit(hit, "source_anchor");
  const unresolved = readHit(hit, "source_anchor_unresolved");

  if (anchor !== null && unresolved !== null) {
    return {
      state: ANCHOR_STATE.CONTRADICTORY,
      where: "anchor and unresolved row both present",
      detail:
        "jmfts_core/evidence.py:453 says the unresolved row is present exactly when the anchor " +
        "is not, so one of these two evidence rows is wrong and this page cannot tell which.",
      anchor,
      unresolved,
    };
  }

  if (anchor === null) {
    if (unresolved === null) {
      return {
        state: ANCHOR_STATE.NONE,
        where: "no anchor",
        detail:
          "Nothing was recorded and nothing was lost. citation runs on PDFs and is advisory, " +
          "so a node without an anchor is not a node that failed.",
        anchor: null,
        unresolved: null,
      };
    }
    const code = typeof unresolved.code === "string" ? unresolved.code : "(no code)";
    const reason = typeof unresolved.reason === "string" ? unresolved.reason : "(no reason)";
    return {
      state: ANCHOR_STATE.UNRESOLVED,
      where: `no rectangle: ${code}`,
      // The sentence is carried through verbatim. It was written for a human to read at
      // `citation_tasks.py:284`; rewriting it here would replace an explanation with a shrug.
      detail: reason,
      anchor: null,
      unresolved,
    };
  }

  if (!isObject(anchor)) {
    return unreadable(anchor, `a source_anchor row is an object; got ${typeof anchor}`);
  }
  switch (anchor.kind) {
    case "pdf":
      if (!Number.isInteger(anchor.page)) {
        return unreadable(anchor, "a pdf anchor names a 0-based integer page; this one does not");
      }
      return placed(anchor, `0-based page ${anchor.page}`);
    case "cells":
      if (typeof anchor.sheet !== "string" || typeof anchor.ref !== "string") {
        return unreadable(anchor, "a cells anchor is {sheet, ref}, both strings");
      }
      return placed(anchor, `${anchor.sheet}!${anchor.ref}`);
    case "span":
      if (!Number.isInteger(anchor.char_start) || !Number.isInteger(anchor.char_end)) {
        return unreadable(anchor, "a span anchor is {char_start, char_end}, both integers");
      }
      return placed(anchor, `chars ${anchor.char_start}–${anchor.char_end}`);
    case "ooxml":
      if (typeof anchor.part !== "string" || typeof anchor.path !== "string") {
        return unreadable(anchor, "an ooxml anchor is {part, path}, both strings");
      }
      return addressed(anchor, `${anchor.path} of ${anchor.part}`);
    default:
      return unreadable(
        anchor,
        `anchor kind ${JSON.stringify(anchor.kind)} is not one of pdf, cells, span, ooxml — ` +
          "this front end is older than the appliance that wrote it."
      );
  }
}

const placed = (anchor, where) => ({
  state: ANCHOR_STATE.ANCHORED,
  where,
  detail: null,
  anchor,
  unresolved: null,
});

/**
 * Exactly located, and not drawable. See `ANCHOR_STATE.ADDRESSED`.
 *
 * The detail is a statement about this front end and about the schedule, not about the
 * document: nothing is missing from the record, and what is missing is the rendition a
 * rectangle would be measured against.
 */
const addressed = (anchor, where) => ({
  state: ANCHOR_STATE.ADDRESSED,
  where,
  detail:
    "This passage is addressed exactly, in the source package's own structure, and has no " +
    "rectangle: an office document has no pages until render:pdf paginates it. Recovering a " +
    "box from that rendition is docs/SPRINT_0_6_0.md Block G step 33.",
  anchor,
  unresolved: null,
});

const unreadable = (anchor, detail) => ({
  state: ANCHOR_STATE.UNREADABLE,
  where: "anchor this page cannot read",
  detail,
  anchor,
  unresolved: null,
});

// --------------------------------------------------------------- the hand-off to the detail view
//
// THE ENCODING IS `shell/citation.js` AND IT IS NOT REPEATED HERE. What is left in this file is
// the half only this file can do: reading the two evidence rows off a `SearchResultItem`
// through this view's declared-reads discipline, so that a field renamed in `jmfts-client`
// fails in the suite rather than on somebody's screen. The citation module takes the three
// VALUES and knows nothing about the hit shape; that is what lets the detail view, which never
// sees a hit, import the same reader.

/**
 * The route the preset editor registers, and the ONE thing this page knows about writing a preset.
 *
 * `CONTEXT_OP` above says why the three write verbs are not called from here and that has not
 * changed: this is a link, not a call. What it links WITH is this page's own fragment vocabulary —
 * `paramsFromRequest` — so "save this search" is literally the search's URL with the route
 * swapped, and the editor reads the parameters it can hold and says which ones it cannot.
 *
 * It sits here rather than in `shell/citation.js` because that module is the DETAIL hand-off's
 * encoding, both directions, and this is one view naming one route it links to. If a second page
 * ever links to the editor, this constant moves there and this comment is the record of why it
 * had not yet.
 */
export const PRESETS_PATH = "/presets";

/** The three values one hit hands over. `null` for a row the hit does not carry. */
function citationOf(hit) {
  return {
    documentId: readDocument(readHit(hit, "document"), "id"),
    anchor: readHit(hit, "source_anchor"),
    unresolved: readHit(hit, "source_anchor_unresolved"),
  };
}

/** The fragment parameters that take a hit to the detail view without losing its anchor. */
export function detailParams(hit) {
  return citationParams(citationOf(hit));
}

/** The href a hit's title points at. */
export function detailHref(hit) {
  return citationHref(citationOf(hit));
}

// ------------------------------------------------------------------- a search, as a fragment

const ALL_METHOD_IDS = Object.freeze(METHODS.map((m) => m.id));

/** The defaults, named once so `paramsFromRequest` can leave them out of the URL. */
export const DEFAULTS = Object.freeze({
  limit: 10,
  getMethod: "hybrid",
});

function whole(params, name) {
  const text = params[name];
  if (text === undefined || text === "") return null;
  if (!/^-?\d+$/.test(String(text))) {
    throw new SearchRequestError(`"${name}" must be a whole number; got ${JSON.stringify(text)}`);
  }
  return Number(text);
}

/**
 * The search the fragment names, or a refusal saying which part could not be read.
 *
 * Refuses rather than repairing, and the three refusals are the three
 * `validate_search_methods` makes in `jmfts-client/jmfts_client/contracts/search.py`: an
 * unknown name, a duplicate, and an empty set. Its reason is this page's reason — an
 * unrecognised name "contributes nothing and raises nothing", so a reader would get a
 * side-by-side missing a column and no way to tell that from a column that returned nothing.
 */
export function requestFromParams(params = {}) {
  const limit = whole(params, "limit") ?? DEFAULTS.limit;
  if (limit < 1 || limit > 100) {
    throw new SearchRequestError(
      `limit is 1..100 (SearchRequest.limit, ge=1 le=100); got ${limit}. The appliance would ` +
        "refuse this request, so the page does not send it."
    );
  }
  const parentId = whole(params, "parent");
  if (parentId !== null && parentId < 1) {
    throw new SearchRequestError(`parent must be a document id; got ${parentId}`);
  }

  let methods = ALL_METHOD_IDS;
  if (params.m !== undefined && params.m !== "") {
    const named = String(params.m).split(",");
    const unknown = named.filter((id) => !ALL_METHOD_IDS.includes(id));
    if (unknown.length) {
      throw new SearchRequestError(
        `unknown method(s) ${JSON.stringify(unknown)} in the fragment; this page draws ` +
          `${ALL_METHOD_IDS.join(", ")}.`
      );
    }
    const duplicates = named.filter((id, i) => named.indexOf(id) !== i);
    if (duplicates.length) {
      throw new SearchRequestError(`duplicate method(s) ${JSON.stringify(duplicates)}`);
    }
    methods = Object.freeze([...named]);
  }

  const getMethod = params.get === undefined || params.get === "" ? DEFAULTS.getMethod : params.get;
  if (!GET_METHODS.includes(getMethod)) {
    throw new SearchRequestError(
      `GET /search/ runs one of ${GET_METHODS.join(", ")}; got ${JSON.stringify(getMethod)}. ` +
        "Anything else is refused by the appliance, which used to run a hybrid search instead."
    );
  }

  return Object.freeze({
    query: (params.q ?? "").trim(),
    limit,
    usetype: (params.usetype ?? "").trim(),
    parentId,
    context: (params.context ?? "").trim(),
    methods,
    getMethod,
    includeExcluded: params.x === "1",
  });
}

/** The fragment a request round-trips through. Defaults are left out, so links stay short. */
export function paramsFromRequest(request) {
  const params = { q: request.query };
  if (request.limit !== DEFAULTS.limit) params.limit = String(request.limit);
  if (request.usetype) params.usetype = request.usetype;
  if (request.parentId !== null) params.parent = String(request.parentId);
  if (request.context) params.context = request.context;
  if (request.methods.length !== ALL_METHOD_IDS.length) params.m = request.methods.join(",");
  if (request.getMethod !== DEFAULTS.getMethod) params.get = request.getMethod;
  if (request.includeExcluded) params.x = "1";
  return params;
}

// ------------------------------------------------------------------------------ running it

/**
 * Run the selected methods against one query, in parallel.
 *
 * `allSettled`, so one method failing leaves the other six standing. That is not a swallowed
 * error: the column that failed renders its exception where its results would have been, which
 * is exactly where a reader is looking for it. A `maxsim` search on an appliance with no model
 * installed is a real and expected 4xx on a page whose whole point is comparing methods, and
 * taking the other six down with it would tell a reader less, not more.
 *
 * @returns {Promise<Array<{method: object, response: object|null, error: Error|null}>>} in
 *   `METHODS` order rather than in completion order, because the columns must not move.
 */
export async function runSearch(client, request) {
  const chosen = METHODS.filter((m) => request.methods.includes(m.id));
  if (chosen.length === 0) {
    throw new SearchRequestError("no method is selected; the page has nothing to run.");
  }
  for (const method of chosen) {
    if (!(method.op_id in client.operations)) {
      throw new ContractError(
        `this page draws a "${method.title}" column from ${method.op_id}, and the generated ` +
          "operation table has no such operation. The appliance's search surface moved; " +
          "regenerate client/ (python -m scripts.generate_ts_client) and fix METHODS."
      );
    }
  }
  const settled = await Promise.allSettled(
    chosen.map((method) => client.call(method.op_id, method.args(request)))
  );
  return chosen.map((method, index) => ({
    method,
    response: settled[index].status === "fulfilled" ? settled[index].value : null,
    error: settled[index].status === "fulfilled" ? null : settled[index].reason,
  }));
}

/** Ask the appliance to write an answer out of what it retrieved. One call, one panel. */
export async function runSynthesis(client, request, { searchMethod, topK }) {
  const body = { query: request.query, search_method: searchMethod, top_k: topK };
  if (request.usetype) body.usetype = request.usetype;
  if (request.parentId !== null) body.parent_id = request.parentId;
  return client.call(SYNTHESIS_OP, withContext(request, { request: body }));
}

// --------------------------------------------------------------------------------- drawing

const make = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

/**
 * One failure, as a sentence with its class on the front.
 *
 * NEVER `JSON.stringify` — an Error flattens to `{}` through it, and so do the Blob and the
 * string a binary route can hand back. A request that never went out is an Error with IC-6
 * status 0, and what a reader needs to see is "JmftsTransportError: … holds no credential",
 * not an empty pair of braces where a reason should be.
 */
const errorText = (error) =>
  error instanceof Error ? `${error.name}: ${error.message}` : `failed: ${String(error)}`;

const badge = (className, text, title) => {
  const node = make("span", `search-badge ${className}`, text);
  if (title) node.setAttribute("title", title);
  return node;
};

/**
 * Put this view's stylesheet on the page, once.
 *
 * `shell/shell.css`'s header states the rule — "a view that needs styling of its own adds it
 * from its own module rather than a second `<link>` here" — because `index.html` is the file
 * five parallel worktrees would otherwise all edit, which is the conflict IC-7 exists to
 * remove. `views/log.js` (step 30) established the shape and this is the same three lines.
 *
 * Called at MOUNT rather than at module scope, which is the one difference from that lane's
 * version and is not a preference: the DOM stub in `tests/test_web_views.py` models a
 * `document` with no `head`, and `shell/shell.js` imports every view's module transitively, so
 * a module-scope `document.head` would take that lane's two shell tests down. Once the stub
 * grows a `head` these three lines move up and the flag goes.
 */
let stylesheetLinked = false;

function linkStylesheet() {
  if (stylesheetLinked) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  // Resolved against THIS module's URL rather than the document's, so the bundle keeps working
  // wherever it is mounted. Same origin by construction; nothing in the bundle loads from the
  // network and `test_nothing_in_the_bundle_loads_from_the_network` holds it to that.
  link.href = new URL("./search.css", import.meta.url).href;
  document.head.append(link);
  stylesheetLinked = true;
}

function field(label, control) {
  const box = make("label", "search-field");
  box.append(make("span", null, label));
  box.append(control);
  return box;
}

function input(type, name, value) {
  const node = make("input");
  node.type = type;
  node.name = name;
  node.value = value === null || value === undefined ? "" : String(value);
  return node;
}

function select(name, options, chosen) {
  const node = make("select");
  node.name = name;
  for (const [value, label] of options) {
    const option = make("option", null, label);
    option.value = value;
    if (value === chosen) option.selected = true;
    node.append(option);
  }
  node.value = chosen;
  return node;
}

function checkbox(name, checked) {
  const node = make("input");
  node.type = "checkbox";
  node.name = name;
  node.checked = Boolean(checked);
  return node;
}

/**
 * The query form. Its submit NAVIGATES — see this module's header: the fragment is the search,
 * so the same search reached by typing and by following a link runs the same way.
 */
function buildForm(request, contexts) {
  const form = make("form", "search-form");
  form.autocomplete = "off";

  // The controls are KEPT rather than looked up again with `querySelector`. A form whose
  // reader finds its own fields by name is a form with two definitions of its field names,
  // and the method checkboxes would need escaping (`m:maxsim`) for a lookup that this avoids
  // entirely.
  const controls = { methods: new Map() };

  controls.q = input("text", "q", request.query);
  controls.q.placeholder = "what are you looking for?";
  const wide = field("query", controls.q);
  wide.className = "search-field search-wide";
  form.append(wide);

  controls.limit = input("number", "limit", request.limit);
  controls.usetype = input("text", "usetype", request.usetype);
  controls.parent = input("text", "parent", request.parentId ?? "");
  controls.context = select(
    "context",
    [["", "(none)"], ...contexts.map((c) => [readContext(c, "name"), readContext(c, "name")])],
    request.context
  );
  controls.get = select(
    "get",
    GET_METHODS.map((m) => [m, m]),
    request.getMethod
  );
  form.append(field("limit", controls.limit));
  form.append(field("usetype globs", controls.usetype));
  form.append(field("subtree (parent id)", controls.parent));
  form.append(field("context", controls.context));
  form.append(field("GET /search/ runs", controls.get));

  const methods = make("div", "search-field search-wide");
  methods.append(make("span", null, "methods"));
  const boxes = make("div", "search-methods");
  for (const method of METHODS) {
    const box = checkbox(`m:${method.id}`, request.methods.includes(method.id));
    controls.methods.set(method.id, box);
    const label = make("label");
    label.append(box);
    label.append(make("span", null, method.title));
    boxes.append(label);
  }
  controls.x = checkbox("x", request.includeExcluded);
  const held = make("label");
  held.append(controls.x);
  held.append(make("span", null, "send exclude_types: [] (show what is normally held out)"));
  boxes.append(held);
  methods.append(boxes);
  form.append(methods);

  const actions = make("div", "search-field search-wide");
  const run = make("button", null, "Search");
  run.type = "submit";
  actions.append(run);

  // THE SEARCH IN THE ADDRESS BAR, not the unsaved state of the controls above it. This module's
  // header says a search IS a URL, and this link is that sentence taken at its word: it carries
  // `paramsFromRequest(request)`, which is the search that ran. A link that read the form at click
  // time would disagree with the page whenever somebody edited a control without pressing Search,
  // and it would do so silently — so the link says which one it takes.
  const save = make("a", "search-save", "Save this search as a preset →");
  save.href = `#${PRESETS_PATH}?${new URLSearchParams(paramsFromRequest(request)).toString()}`;
  save.setAttribute(
    "title",
    "saves the search this page ran — the one in the address bar. Press Search first if you " +
      "have changed a control. Presets are shared with every token holder; the editor says so."
  );
  actions.append(save);
  form.append(actions);

  return { form, controls };
}

/** Read the form back as fragment parameters, so one validator sees every request. */
export function formParams(controls) {
  const chosen = [...controls.methods.entries()]
    .filter(([, box]) => box.checked)
    .map(([id]) => id);
  const params = {
    q: controls.q.value,
    limit: controls.limit.value,
    usetype: controls.usetype.value,
    parent: controls.parent.value,
    context: controls.context.value,
    get: controls.get.value,
    m: chosen.join(","),
  };
  if (controls.x.checked) params.x = "1";
  // An EMPTY selection has to survive the round trip as an empty selection. `m=""` reads as
  // "not specified" and would silently run all seven, so the refusal below is made here where
  // the reader can be told which control produced it.
  if (chosen.length === 0) {
    throw new SearchRequestError("select at least one method; an empty side-by-side is not one.");
  }
  return params;
}

function appliedNotes(response, read) {
  const applied = read(response, "applied");
  const notes = make("div");
  if (applied === null) {
    notes.append(
      make("p", "note bad", "the server reported no applied block, so what it ran is unstated")
    );
    return notes;
  }
  const line = make("p", "note dim");
  const parts = [`limit ${read(response, "applied.limit")}`];
  const methods = read(response, "applied.methods");
  if (methods !== null) {
    const weights = read(response, "applied.weights") ?? {};
    parts.push(
      `fused ${methods.map((m) => `${m}×${weights[m] ?? 1.0}`).join(" + ")}`
    );
  }
  const usetype = read(response, "applied.usetype");
  if (usetype !== null) parts.push(`usetype ${JSON.stringify(usetype)}`);
  const parent = read(response, "applied.parent_id");
  if (parent !== null) parts.push(`subtree ${parent}`);
  const excluded = read(response, "applied.exclude_types");
  parts.push(excluded.length ? `held out: ${excluded.join(", ")}` : "nothing held out");
  line.textContent = parts.join(" · ");
  notes.append(line);

  const truncated = read(response, "applied.truncated");
  if (truncated === true) {
    notes.append(
      make(
        "p",
        "note bad",
        "SHORT PAGE: an approximate index scan behind this answered with fewer rows than it " +
          "asked for, so this is not an exhausted corpus."
      )
    );
  }
  return notes;
}

/** What the corpus itself says about a method that returned nothing. Null when it says nothing. */
export function emptyBecause(methodId, capabilities) {
  const corpus = readCapabilities(capabilities, "corpus");
  if (corpus === null) return "this appliance was asked for corpus counts and returned none";
  if (readCapabilities(capabilities, "corpus.documents") === 0) {
    return "this appliance holds no documents at all";
  }
  const count = (path) => readCapabilities(capabilities, path);
  if (methodId === "maxsim" && count("corpus.documents_with_token_vectors") === 0) {
    return "no document here has token vectors, so maxsim returns nothing whatever the query";
  }
  if (methodId === "vector" && count("corpus.documents_with_vectors") === 0) {
    return "no document here has a document vector, so vector search returns nothing";
  }
  if (methodId === "bm25" && count("corpus.bm25_indexes").length === 0) {
    return "this appliance has no BM25 index, so bm25 returns nothing";
  }
  return null;
}

/** How much of a hit's content a column draws. The rest is said, not dropped. */
const SNIPPET = 180;

function renderHit(hit, index, showMethod) {
  const box = make("div", "search-hit");
  const document_ = readHit(hit, "document");

  const head = make("div");
  head.append(make("span", "search-rank", `${index + 1}. `));
  const link = make("a", null, readDocument(document_, "title") ?? "(untitled)");
  link.href = detailHref(hit);
  head.append(link);
  head.append(
    make("span", "dim", `  #${readDocument(document_, "id")}  ${readHit(hit, "score").toFixed(4)}`)
  );
  box.append(head);

  const marks = make("div");
  const usetype = readDocument(document_, "usetype");
  marks.append(badge("", usetype ?? "(no usetype)", "documents.usetype"));
  if (showMethod) marks.append(badge("", readHit(hit, "method"), "which method produced this hit"));
  const producedBy = readDocument(document_, "produced_by");
  if (producedBy !== null) {
    marks.append(badge("", producedBy, "the rule that wrote this node; null means asserted"));
  }
  if (readDocument(document_, "content_source") === "effective") {
    marks.append(
      badge(
        "",
        "effective content",
        "a container node with no stored content; the text was projected from its subtree at " +
          "read time"
      )
    );
  }
  const anchor = anchorState(hit);
  marks.append(badge(anchor.state, anchor.where, anchor.detail ?? undefined));
  box.append(marks);

  // A CUT IS SAID OUT LOUD. `limit` goes to 100 and every result is a whole
  // `DocumentResponse` carrying its `content`, so a column can hold a lot of text and a column
  // of seven is seven times that. What is cut here is the DRAWING and never the data: nothing
  // is dropped from the result set, the count the appliance returned stays on the column, and a
  // hit whose text was shortened says how much of it is on screen. A silent "…" is the same
  // defect class as drawing absent and empty identically — the reader cannot tell a short
  // document from a shortened one.
  const content = readDocument(document_, "content");
  if (content === null) {
    box.append(make("p", "search-snippet", "(this node stores no content)"));
    return box;
  }
  const long = content.length > SNIPPET;
  box.append(make("p", "search-snippet", long ? content.slice(0, SNIPPET) : content));
  if (long) {
    const said = `showing the first ${SNIPPET} of ${content.length} characters`;
    box.append(make("p", "search-snippet dim", said));
  }
  return box;
}

/**
 * One column per method, in `METHODS` order, whatever each of them answered.
 *
 * `request` is here so the two columns that differ from their neighbours can say HOW: `quick`
 * names the method it was asked to run, and says that `GET /search/` has no `exclude_types`
 * parameter on a page where the reader just ticked that box for the other six.
 */
export function renderColumns(outcomes, capabilities, request) {
  const columns = make("div", "search-columns");
  for (const { method, response, error } of outcomes) {
    const column = make("div", "search-column");
    column.append(make("h3", null, method.title));
    column.append(make("p", "note dim", method.path));
    column.append(make("p", "note dim", method.what));

    if (method.id === "quick") {
      column.append(make("p", "note", `?method=${request.getMethod}`));
      if (request.includeExcluded) {
        column.append(
          make(
            "p",
            "note bad",
            "this column did NOT send exclude_types: [] — GET /search/ has no such parameter, " +
              "so it ran with the appliance's default exclusions while the others did not."
          )
        );
      }
    }

    if (error) {
      // The failure goes where the results would have been. `JmftsHttpError`'s message already
      // carries the method, the path, the status and the server's `detail`, so there is nothing
      // to add and rewriting it would lose all four.
      column.append(make("p", "note bad", errorText(error)));
      columns.append(column);
      continue;
    }

    const read = method.kind === "routed" ? readAuto : readResponse;
    if (method.kind === "routed") {
      const routing = read(response, "routing");
      column.append(
        make(
          "p",
          "note",
          routing === null
            ? "the router reported no decision"
            : `routed to ${read(response, "routing.method")}: ${read(response, "routing.reason")}`
        )
      );
    }

    const results = read(response, "results");
    const total = read(response, "total");
    column.append(
      make(
        "p",
        "search-count",
        `${total} result${total === 1 ? "" : "s"} in ${read(response, "latency_ms")} ms`
      )
    );
    column.append(appliedNotes(response, read));

    if (results.length === 0) {
      // A count of zero with a method named is a fact, and this is where it is stated rather
      // than left as an empty area a reader has to interpret.
      const why = emptyBecause(method.id, capabilities);
      column.append(
        make("p", "note bad", why ? `nothing matched — ${why}` : "nothing matched this query")
      );
    }
    const showMethod = method.kind === "fusion" || method.kind === "routed";
    results.forEach((hit, index) => column.append(renderHit(hit, index, showMethod)));
    columns.append(column);
  }
  return columns;
}

/** The synthesis panel: prose, its sources, and whether an LLM answered at all. */
export function renderSynthesis(response) {
  const panel = make("div");
  if (!readSynthesis(response, "llm_available")) {
    // The route still answers 200 with a placeholder sentence and REAL sources
    // (`search_service.py`, "degrade gracefully if unavailable"), so the page has to say which
    // half of what follows is an answer. Without this line the placeholder reads as the model's
    // reply and the retrieval below it reads as the citations for a reply that never happened.
    panel.append(
      make(
        "p",
        "note bad",
        "The LLM did not answer, so the text below is the server's placeholder rather than a " +
          "synthesis. The sources under it are real retrieval results."
      )
    );
  }
  panel.append(make("p", "search-answer", readSynthesis(response, "synthesis")));
  panel.append(
    make(
      "p",
      "note dim",
      `${readSynthesis(response, "llm_model")} · ` +
        `retrieval ${readSynthesis(response, "search_latency_ms")} ms · ` +
        `total ${readSynthesis(response, "total_latency_ms")} ms`
    )
  );
  const sources = readSynthesis(response, "sources");
  if (sources.length === 0) {
    panel.append(make("p", "note bad", "no sources: the answer cites nothing in this corpus"));
    return panel;
  }
  const list = make("div");
  sources.forEach((source, index) => {
    const row = make("p", "note");
    row.append(make("span", "search-rank", `${index + 1}. `));
    const link = make("a", null, readSource(source, "title") ?? "(untitled)");
    link.href = `#${DETAIL_PATH}?id=${readSource(source, "document_id")}`;
    row.append(link);
    row.append(
      make(
        "span",
        "dim",
        `  #${readSource(source, "document_id")}  ${readSource(source, "score").toFixed(4)}` +
          `  via ${readSource(source, "method")}`
      )
    );
    list.append(row);
  });
  panel.append(list);
  return panel;
}

// ------------------------------------------------------------------------------- the view

async function component({ client, params, navigate }) {
  linkStylesheet();
  const page = make("div", "view-search");
  page.append(make("h1", null, "Search"));
  page.append(
    make(
      "p",
      "lede",
      "One query, seven methods, side by side. Every column is one call and the log below has " +
        "it; a column that returned nothing says so, and says why when the corpus explains it."
    )
  );

  // Both of these are read before anything is searched, and both propagate. They are not
  // decoration: without the corpus counts a zero column cannot tell a reader whether the query
  // matched nothing or the method could never have answered here, which is the distinction this
  // page exists to keep. An appliance that cannot answer /capabilities cannot answer /search
  // either, so failing here costs a reader nothing they were about to get.
  const capabilities = await client.call(CAPABILITIES_OP, { corpus: true });
  const contexts = await client.call(CONTEXT_OP, {});

  const request = requestFromParams(params);
  const { form, controls } = buildForm(request, contexts);
  page.append(form);

  const problem = make("p", "note bad");
  page.append(problem);

  const held = readCapabilities(capabilities, "retrieval.search_exclude_usetypes");
  page.append(
    make(
      "p",
      "note dim",
      `Held out of every result set unless a request says otherwise: ${held.join(", ") || "nothing"}` +
        ". A positive usetype filter overrides the exclusion entirely."
    )
  );
  if (request.context) {
    const chosen = contexts.find((c) => readContext(c, "name") === request.context);
    if (!chosen) {
      throw new SearchRequestError(
        `no search context is named "${request.context}" on this appliance. The ones it has: ` +
          `${contexts.map((c) => readContext(c, "name")).join(", ") || "none"}.`
      );
    }
    page.append(
      make(
        "p",
        "note dim",
        `context "${request.context}": ${readContext(chosen, "description") ?? "no description"} ` +
          `— ${JSON.stringify(readContext(chosen, "config"))}`
      )
    );
  }

  const results = make("div");
  page.append(results);

  const show = async (asked) => {
    results.textContent = "";
    results.append(make("p", "dim", "searching…"));
    const outcomes = await runSearch(client, asked);
    results.textContent = "";
    results.append(renderColumns(outcomes, capabilities, asked));
    results.append(synthesisPanel(client, asked, capabilities));
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    problem.textContent = "";
    let asked;
    try {
      asked = requestFromParams(formParams(controls));
    } catch (error) {
      // The form does not submit and the reason is on the page. Fail Early applies to a form:
      // an invalid request is refused here rather than sent for the appliance to refuse.
      if (!(error instanceof SearchRequestError)) throw error;
      problem.textContent = error.message;
      return;
    }
    if (!asked.query) {
      problem.textContent = "enter a query; a search for nothing is not a search.";
      return;
    }
    const target = `#/search?${new URLSearchParams(paramsFromRequest(asked)).toString()}`;
    // Navigating re-mounts and runs the search from the fragment, which is what makes the URL
    // the search. When the fragment is ALREADY this search, no `hashchange` fires, so pressing
    // Search again would do nothing at all — that case runs in place instead of looking broken.
    if (window.location.hash === target) show(asked).catch((error) => fail(results, error));
    else navigate("/search", paramsFromRequest(asked));
  });

  if (request.query) await show(request);
  else results.append(make("p", "dim", "Enter a query above. The seven columns run in parallel."));

  return page;
}

function fail(where, error) {
  where.textContent = "";
  where.append(make("p", "note bad", errorText(error)));
}

/**
 * The synthesis panel, built but not run: it costs an LLM call, so it waits to be asked.
 *
 * An appliance with no LLM configured does not get a button that will fail. `JMFTS_LLM_*` is
 * blank by default and blank is supported, so "not configured" is an ordinary state of a
 * working appliance and the panel says which capabilities that state costs.
 */
function synthesisPanel(client, request, capabilities) {
  const panel = make("div", "search-panel");
  panel.append(make("h2", null, "Synthesis"));

  if (!readCapabilities(capabilities, "llm.configured")) {
    panel.append(
      make(
        "p",
        "note bad",
        "No LLM is configured on this appliance (JMFTS_LLM_*), so POST /search/synthesize " +
          "cannot answer. What that costs: " +
          readCapabilities(capabilities, "llm.requires_llm").join(", ") +
          ". Everything above works without one."
      )
    );
    return panel;
  }

  const form = make("form");
  form.autocomplete = "off";
  const method = select("search_method", [["auto", "auto"], ...GET_METHODS.map((m) => [m, m])], "auto");
  const topK = input("number", "top_k", 5);
  form.append(field("retrieve with", method));
  form.append(field("top k", topK));
  const run = make("button", null, "Synthesize an answer");
  run.type = "submit";
  form.append(run);
  panel.append(form);

  const out = make("div");
  panel.append(out);
  panel.append(
    make("p", "note dim", `model ${readCapabilities(capabilities, "llm.model")}`)
  );

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    out.textContent = "";
    out.append(make("p", "dim", "asking the model…"));
    try {
      const response = await runSynthesis(client, request, {
        searchMethod: method.value,
        topK: Number(topK.value),
      });
      out.textContent = "";
      out.append(renderSynthesis(response));
    } catch (error) {
      fail(out, error);
    }
  });
  return panel;
}

registerView({
  id: "search",
  title: "Search",
  path: "/search",
  component,
  tags: ["retrieval"],
});
