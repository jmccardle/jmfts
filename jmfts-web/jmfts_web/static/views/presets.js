// Presets: a saved search configuration, written from the browser and run from the search page.
//
// docs/SPRINT_0_6_0.md Block F, and question 4.6 — "a preset you can create and not use is the
// mechanism with no examples". This page is the other half of `views/search.js`, which reads
// `/search-contexts/` and deliberately writes none of it. Step 27 said why, at its `CONTEXT_OP`:
// "a preset editor is CRUD, it has a surface of its own". This is that surface, and the split is
// load-bearing rather than tidy — `tests/test_web_search_view.py` asserts that the three write
// verbs appear nowhere in the search view, so the two pages cannot merge by accident.
//
// A PRESET IS GLOBAL AND THIS PAGE SAYS SO ON EVERY BUTTON. `tests/test_expose_principal_audit.py`
// lists all five `/search-contexts/` verbs under `CONFIG_OBJECT`, whose recorded reason is: "A
// named configuration row, not a document. Subtree RBAC is defined over the document tree… there
// is no root above this row to consult. That leaves it shared mutable state any token holder may
// change." So every token holder on this appliance sees these rows, can overwrite them and can
// delete them. That is a recorded decision and not a defect — but a button labelled "Save" that
// silently writes state shared with everybody is a surprise, and a surprise is a bug here. The
// controls say what they will do before they are pressed, which is the rule `views/upload.js`
// applies to its Ingest button ("Ingest — stores 4.2 kB, enqueues 3 tasks").
//
// SAVING ALWAYS TRIES TO CREATE, AND THERE IS NO SECOND PATH. A name that is already taken comes
// back 409, and a 409 is an ANSWER: the page fetches what that name holds, draws it, draws what an
// overwrite would write, and offers the overwrite as a separate deliberate act. That ordering is
// not politeness. `PUT` is destructive of state shared with every other token holder, and the row
// may have been written a second ago by somebody this page never saw — so the contents shown
// before an overwrite are FETCHED at that moment rather than taken from the list this page loaded
// at mount.
//
// WHAT THIS EDITOR CAN SET, AND WHY IT IS FOUR OF EIGHT. `config` is free-form JSONB whose
// contract names eight keys (`SearchContextCreate.config`). This page edits the four that a search
// on `views/search.js` can express — `method`, `usetype`, `parent_id`, `limit` — and CARRIES the
// rest verbatim. The rule is "what you can save is what you can run and see": a preset whose
// `threshold` this page could set but whose effect no page here can exercise is a preset you have
// to trust rather than check. Everything not edited is preserved exactly, listed by name with its
// value, and the overwrite panel shows it surviving. Dropping a key on save is how a preset
// silently loses half its configuration, and it is the failure this page is written against.
//
// NOTHING VALIDATES A PRESET'S `usetype` AGAINST WHAT THIS APPLIANCE WRITES — `ROADMAP.md` known
// gap 5, and `tests/test_search_context_presets.py` is the executable form of it. A glob that
// matches no producible usetype is accepted at every layer and returns an empty page with no error
// to say why. This page cannot close that gap from a browser: the producible set is an `ast` scan
// of `jmfts_core` and no route reports it. What it does instead is refuse the shapes it CAN judge
// (an empty glob, a duplicate, a limit outside the contract's range) and put a "Run it" link on
// every row, because running the preset is the only check available and a page that hid that would
// be pretending otherwise.

import { registerView } from "../shell/registry.js";

/** This page asked a response for a field its contract does not carry. */
export class ContractError extends Error {
  constructor(message) {
    super(message);
    this.name = "ContractError";
  }
}

/** The form named a preset this page will not send, and says which control produced it. */
export class PresetError extends Error {
  constructor(message) {
    super(message);
    this.name = "PresetError";
  }
}

// ------------------------------------------------------------------- what this page reads
//
// The same declaration `views/search.js` and `views/capabilities.js` carry, for the reason the
// first of them records: three rows once read field names `CapabilitiesResponse` does not have and
// all three rendered as the word "none", because `undefined || []` is a perfectly good empty list.
// The helper is NOT shared between views — a view may depend on the registry, the client and the
// renderers and on no other view — so what is shared is the discipline and the check:
// `tests/test_web_presets_view.py` resolves every path below against the Pydantic model named on
// its left.

export const READS = Object.freeze({
  "meta.CapabilitiesResponse": Object.freeze([
    "retrieval.methods",
    "retrieval.default_methods",
    "retrieval.default_weights",
  ]),
  "search_context.SearchContextResponse": Object.freeze([
    "name",
    "description",
    "config",
    "created_at",
    "updated_at",
  ]),
});

/**
 * A reader for one contract: `reader("search_context.SearchContextResponse")(row, "config")`.
 *
 * A key that is PRESENT and null is a value and comes back as null — `description` is null on a
 * preset nobody described, and that is an answer. A key that is ABSENT is not an answer, and the
 * difference between the two is the whole of this function.
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
          "list is what tests/test_web_presets_view.py resolves against the Pydantic model."
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
const readPreset = reader("search_context.SearchContextResponse");

// ------------------------------------------------------------------------ the four operations

/**
 * Every operation this page calls, by what it is for.
 *
 * Four of the five `/search-contexts/` verbs, and the fifth — `list_contexts` — makes five: this
 * page is the whole CRUD surface, which is exactly what `views/search.js` is not. `get_context` is
 * here and it is not redundant with the list: it is the FRESH read taken at the moment a 409 says
 * a name is taken, and the point of taking it then is that the list this page loaded at mount may
 * be older than the row it is about to overwrite.
 */
export const OPS = Object.freeze({
  list: "SearchContextService.list_contexts",
  get: "SearchContextService.get_context",
  create: "SearchContextService.create_context",
  update: "SearchContextService.update_context",
  delete: "SearchContextService.delete_context",
  capabilities: "MetaService.capabilities",
});

// ------------------------------------------------------------------------ the config vocabulary

/**
 * The eight keys `SearchContextCreate.config` names, what reads each, and whether this page sets
 * it.
 *
 * `config` is free-form JSONB — the appliance stores whatever is put there — so this table is the
 * vocabulary rather than a schema. `tests/test_web_presets_view.py` resolves the eight names
 * against the contract field's own description, so a key added there fails the suite instead of
 * quietly becoming a key this page calls unknown.
 *
 * `reads` is where the key is consumed, and it is worth reading before saving one: the resolution
 * is `SearchContextRepository.resolve_params`, which merges `config` under the request's own
 * non-None arguments, and each search method then pulls the keys it knows by name. A key NOT in
 * this table is stored faithfully and read by nothing — there is no `**params` splat anywhere in
 * `jmfts_core/services/search_service.py`, only `params.get("…")` per key — so this page carries
 * an unknown key rather than dropping it, and says that it does nothing.
 */
export const CONFIG_KEYS = Object.freeze({
  method: Object.freeze({
    editable: true,
    reads: "GET /search/ only (search_service.py:791). A POST names its method in the path.",
  }),
  methods: Object.freeze({
    editable: false,
    reads: "the hybrid fusion's method list (search_service.py:251), and GET /search/?method=hybrid",
    why:
      "the search page sends no method list — it ticks which COLUMNS to draw, which is a " +
      "different thing — so a fusion set saved here could not be checked by running it.",
  }),
  weights: Object.freeze({
    editable: false,
    reads: "the fusion weights (search_service.py:253), through effective_weights()",
    why:
      "effective_weights() merges a partial map into the appliance's defaults, so half a map " +
      "is not half an answer; and no page here can exercise one.",
  }),
  usetype: Object.freeze({
    editable: true,
    reads: "every search method, as the positive usetype filter",
  }),
  parent_id: Object.freeze({
    editable: true,
    reads: "every search method, as the subtree filter",
  }),
  index_name: Object.freeze({
    editable: false,
    reads: "bm25 and hybrid (search_service.py:266, :429), naming a BM25 index",
    why:
      "it names a row on the /indexes surface, which is a page this front end does not have; a " +
      "name no index answers to returns nothing and says nothing.",
  }),
  threshold: Object.freeze({
    editable: false,
    reads: "vector search only (search_service.py:326)",
    why: "the search page has no threshold control, so a saved one could not be checked here.",
  }),
  limit: Object.freeze({
    editable: true,
    reads: "GET /search/ when the caller leaves limit at 10 — see LIMIT_IS_ALMOST_ALWAYS_OVERRIDDEN",
  }),
});

/** The four this page sets, in the order the form draws them. */
export const EDITABLE = Object.freeze(
  Object.keys(CONFIG_KEYS).filter((key) => CONFIG_KEYS[key].editable)
);

/**
 * What a saved `limit` actually does, said on the page because it is not what a reader expects.
 *
 * Every POST search resolves its context with `"limit": request.limit` in the override map, and
 * `SearchRequest.limit` is `Field(default=10, ge=1, le=100)` — an int that is never None. The
 * override loop in `SearchContextRepository.resolve_params` applies every non-None value, so the
 * request's limit ALWAYS wins and a preset's `limit` never reaches a POST search. `GET /search/`
 * is the exception and it is deliberate: `quick_search` passes `limit if limit != 10 else None`,
 * so a preset's limit is used exactly when the caller did not ask for one.
 *
 * `tests/test_web_presets_view.py::test_a_presets_limit_reaches_a_get_search_and_never_a_post`
 * runs both halves against the real resolver rather than trusting this paragraph.
 */
export const LIMIT_IS_ALMOST_ALWAYS_OVERRIDDEN =
  "A saved limit reaches GET /search/ only, and only when the caller leaves limit at its " +
  "default of 10. Every POST search sends its own limit (SearchRequest.limit defaults to 10 and " +
  "is never absent), and a sent value always overrides the preset.";

// ------------------------------------------------------- reading a config into the four controls

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

/**
 * The comma form of a usetype filter, or null when this page cannot express the value.
 *
 * `usetype_globs` in `jmfts-client/jmfts_client/contracts/search.py` accepts two shapes: a
 * comma-joined string, and a LIST — and the list exists for exactly one reason, which that file
 * states: it "is the only way to name a usetype that contains a comma". So a list whose members
 * are comma-free means the same thing as the joined string and this page's text control can hold
 * it; a list with a comma inside a member does NOT, and flattening it would change the filter into
 * a different filter that still looks right. That value is carried instead of edited.
 */
export function usetypeText(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value) && value.every((one) => typeof one === "string")) {
    return value.some((one) => one.includes(",")) ? null : value.join(",");
  }
  return null;
}

/**
 * Split a stored config into the four controls and everything this page will carry untouched.
 *
 * @param {object} config a preset's `config`, or `{}` for a new one
 * @returns {{fields: object, carried: Array<{key: string, value: *, why: string}>,
 *            usetypeIsList: boolean}}
 *   `fields.usetype` is `null` — not `""` — when the stored value is one this page cannot express;
 *   `null` means "carried, do not touch" and `""` means "blank, remove the key", and the two must
 *   not be spelled the same way or an unexpressible filter would be deleted by saving.
 */
export function splitConfig(config) {
  if (!isObject(config)) {
    throw new PresetError(
      `a preset's config is a JSON object; this one is ${Array.isArray(config) ? "an array" : typeof config}. ` +
        "The appliance stores config as free-form JSONB, so a row written by something else can " +
        "be shaped like this; this page will not edit it."
    );
  }

  const text = usetypeText(config.usetype);
  const fields = {
    method: typeof config.method === "string" ? config.method : "",
    usetype: text,
    parent_id: config.parent_id === undefined || config.parent_id === null ? "" : String(config.parent_id),
    limit: config.limit === undefined || config.limit === null ? "" : String(config.limit),
  };

  const carried = [];
  for (const [key, spec] of Object.entries(CONFIG_KEYS)) {
    if (!(key in config)) continue;
    if (key === "usetype" && text === null) {
      carried.push({
        key,
        value: config.usetype,
        why:
          "a list whose members contain a comma — the one filter shape a comma-joined text box " +
          "cannot express (contracts/search.py, usetype_globs). Kept exactly as it is.",
      });
      continue;
    }
    if (spec.editable) continue;
    carried.push({ key, value: config[key], why: spec.why });
  }
  for (const key of Object.keys(config).sort()) {
    if (key in CONFIG_KEYS) continue;
    carried.push({
      key,
      value: config[key],
      why:
        "not one of the eight keys the contract names. It is stored faithfully and read by " +
        "nothing: the search path pulls config keys by name and never splats the dict.",
    });
  }

  return { fields, carried, usetypeIsList: Array.isArray(config.usetype) };
}

/**
 * The four controls, prefilled from `views/search.js`'s own fragment vocabulary.
 *
 * The link that brings a reader here is the search's own URL with the route swapped, so the names
 * read here are that page's names rather than the config's: `parent` is `parent_id` and `get` —
 * "GET /search/ runs" — is `method`, because `method` in a config is read by `quick_search` and by
 * nothing else. `q` is deliberately not among them: a preset holds a configuration and not a
 * query, and the page says so where the query would have been.
 *
 * Only keys the fragment actually carries are returned. `views/search.js` leaves its defaults OUT
 * of the URL, so a search run at `limit=10` arrives here with no limit at all — which is right: it
 * did not ask for one, and writing 10 into the preset would save a decision nobody made.
 */
export function fieldsFromSearchParams(params = {}) {
  const out = {};
  if (params.get !== undefined && params.get !== "") out.method = String(params.get);
  if (params.usetype !== undefined && params.usetype !== "") out.usetype = String(params.usetype);
  if (params.parent !== undefined && params.parent !== "") out.parent_id = String(params.parent);
  if (params.limit !== undefined && params.limit !== "") out.limit = String(params.limit);
  return out;
}

/**
 * What the link from a search carried that a preset cannot hold, so the page says it out loud.
 *
 * "Save this search as a preset" is a link whose query string is the SEARCH's, and three of that
 * page's parameters have no home in a `config`. Two of them a reader would reasonably expect to be
 * saved, which is why this is a paragraph on the page rather than a silent omission:
 *
 * * `q` — the query. A preset holds what a search runs UNDER, not the search.
 * * `m` — which of the seven columns the side-by-side draws. That is a property of that page's
 *   layout and never leaves the browser; the config key called `methods` is a different thing
 *   entirely (the list a hybrid search FUSES), and conflating the two would save a fusion nobody
 *   asked for.
 * * `x` — "send exclude_types: []". `exclude_types` is not one of the eight keys a config names
 *   and no search reads it out of one: `search_service.py` passes `request.exclude_types` to the
 *   repository directly, never `params.get("exclude_types")`.
 *
 * @returns {Array<{param: string, value: string, what: string}>} only the ones actually present
 */
export function unsavedFromSearch(params = {}) {
  const said = {
    q: "a preset holds the configuration a search runs under, not the search.",
    m:
      "it is which columns that page draws, which never leaves the browser. The config key " +
      "`methods` is a different thing — the list a hybrid search fuses — and this editor does " +
      "not set that one either.",
    x:
      "`exclude_types` is not one of the eight keys a config names, and no search reads it out " +
      "of one; the request carries it directly.",
  };
  const out = [];
  for (const [param, what] of Object.entries(said)) {
    if (params[param] === undefined || params[param] === "") continue;
    out.push({ param, value: String(params[param]), what });
  }
  return out;
}

// ------------------------------------------------------------------ what the form will send

function whole(name, text) {
  if (!/^-?\d+$/.test(text)) {
    throw new PresetError(`"${name}" must be a whole number; got ${JSON.stringify(text)}`);
  }
  return Number(text);
}

/**
 * The globs a usetype text names, refused where `usetype_globs` would refuse them.
 *
 * The three refusals are that function's three, with its reasons: an empty glob "names nothing" and
 * is refused rather than read as "match everything"; a duplicate is refused; and a filter that
 * normalises to no glob at all is refused in favour of omitting the field. Refused HERE, in the
 * browser, because a form that submits an invalid value and shows the 422 afterwards has taught
 * the reader nothing the control could have told them.
 */
export function usetypeGlobs(text) {
  const parts = text.split(",").map((one) => one.trim());
  if (parts.some((one) => one === "")) {
    throw new PresetError(
      `usetype ${JSON.stringify(text)} contains an empty glob; a filter that names nothing is ` +
        "refused rather than read as 'match everything'. Clear the field to save no filter."
    );
  }
  const duplicates = parts.filter((one, i) => parts.indexOf(one) !== i);
  if (duplicates.length) {
    throw new PresetError(`duplicate usetype glob(s) ${JSON.stringify(duplicates)}`);
  }
  if (parts.length === 0) throw new PresetError("usetype names no glob at all");
  return parts;
}

/**
 * The config this page will send: the row it started from, with the four controls applied.
 *
 * EVERY KEY OF `base` SURVIVES unless one of the four names it. That is the property this whole
 * page turns on, and `tests/test_web_presets_view.py` puts a config carrying all four
 * non-editable keys plus an unknown one through here and asserts every one of them comes out the
 * other side unchanged.
 *
 * A blank control REMOVES its key rather than writing an empty value, because omitting a key and
 * storing `""` are different instructions: `resolve_params` only overrides what the request left
 * as None, so a stored `usetype: ""` would be a positive filter that matches nothing, where an
 * absent one is no filter at all.
 *
 * @param {object} base the config as the appliance currently holds it
 * @param {object} fields the four controls, as text; `usetype: null` means "carried, do not touch"
 * @param {{usetypeIsList: boolean}} shape how the stored usetype was written, so it is written back
 *   the same way — a list that came in as a list goes out as a list.
 */
export function mergeConfig(base, fields, { usetypeIsList = false } = {}) {
  const out = { ...base };

  const method = (fields.method ?? "").trim();
  if (method === "") delete out.method;
  else out.method = method;

  if (fields.usetype !== null && fields.usetype !== undefined) {
    const text = String(fields.usetype).trim();
    if (text === "") delete out.usetype;
    else {
      const globs = usetypeGlobs(text);
      // THE SHAPE IS THE ONE IT ARRIVED IN, and the glob COUNT does not decide it. A filter
      // stored as the string "chunk,section" and the same filter stored as
      // `["chunk", "section"]` mean the same thing to `usetype_globs`, so rewriting one into the
      // other on a save that did not touch the field is a change this page made and nobody asked
      // for — and it would show up as a diff on a row somebody else is also editing.
      out.usetype = usetypeIsList ? globs : globs.join(",");
    }
  }

  const parent = (fields.parent_id ?? "").trim();
  if (parent === "") delete out.parent_id;
  else {
    const id = whole("subtree (parent id)", parent);
    if (id < 1) throw new PresetError(`subtree must be a document id; got ${id}`);
    out.parent_id = id;
  }

  const limit = (fields.limit ?? "").trim();
  if (limit === "") delete out.limit;
  else {
    const value = whole("limit", limit);
    if (value < 1 || value > 100) {
      throw new PresetError(
        `limit is 1..100 (SearchRequest.limit, ge=1 le=100); got ${value}. A preset outside that ` +
          "range is stored happily and refused by every search that resolves it."
      );
    }
    out.limit = value;
  }

  return out;
}

/** A preset name the appliance will accept as a path segment, or a refusal saying why not. */
export function presetName(text) {
  const name = String(text ?? "").trim();
  if (name === "") {
    throw new PresetError("a preset needs a name; it is how a search asks for one (?context=…).");
  }
  if (name !== text) {
    throw new PresetError(
      `the name has leading or trailing whitespace. "${name}" and ${JSON.stringify(text)} would ` +
        "be two rows, and only one of them is typeable into the search page's context box."
    );
  }
  if (name.includes("/")) {
    throw new PresetError(
      `a preset name is one path segment of /search-contexts/{name}; ${JSON.stringify(name)} ` +
        "contains a slash, which would address a route that does not exist."
    );
  }
  return name;
}

/** Where a preset is run. The search page reads `context` out of its own fragment. */
export function runHref(name) {
  return `#/search?context=${encodeURIComponent(name)}`;
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
 * NEVER `JSON.stringify` — an Error flattens to `{}` through it, and so does a Blob and a string.
 * A request that never went out is an Error carrying IC-6 status 0, and what a reader needs is
 * "JmftsTransportError: … holds none", not an empty pair of braces.
 */
export const errorText = (error) =>
  error instanceof Error ? `${error.name}: ${error.message}` : `failed: ${String(error)}`;

/**
 * A stored value, as text.
 *
 * `JSON.stringify` is right HERE and wrong for an error: these values came out of a JSONB column
 * through a JSON response, so they are objects, arrays, numbers, strings, booleans and null and
 * nothing else. The rule it breaks elsewhere is about Blobs and Errors, which cannot reach this.
 */
export const valueText = (value) => JSON.stringify(value);

/** This view's own stylesheet, linked at MOUNT behind a module flag. See `views/log.js`. */
let stylesheetLinked = false;

function linkStylesheet() {
  if (stylesheetLinked) return;
  stylesheetLinked = true;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  // Resolved against this module's own URL: same origin, no configured path, and it keeps working
  // wherever the bundle is mounted. `index.html` is the file the F3 worktrees would otherwise all
  // have edited, which is the conflict IC-7 exists to remove.
  link.href = new URL("./presets.css", import.meta.url).href;
  document.head.append(link);
}

function field(label, control, note) {
  const box = make("label", "preset-field");
  box.append(make("span", null, label));
  box.append(control);
  if (note) box.append(make("span", "dim preset-hint", note));
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

/**
 * The keys this page will not touch, drawn with their values and the reason for each.
 *
 * Drawn even when the list is empty, and that is the point: "this editor preserves what it does
 * not show" is a claim, and a panel that appears only when there is something to preserve leaves
 * the claim unmade in exactly the case a reader would want it — the first preset they write.
 */
export function renderCarried(carried) {
  const panel = make("div", "preset-carried");
  panel.append(make("h3", null, "Carried through untouched"));
  if (carried.length === 0) {
    panel.append(
      make(
        "p",
        "note dim",
        "This preset holds nothing outside the four controls above. If it did — weights, a " +
          "threshold, an index name, a key written by something that is not this page — it would " +
          "be listed here and saved back exactly as it arrived."
      )
    );
    return panel;
  }
  panel.append(
    make(
      "p",
      "note dim",
      `${carried.length} key${carried.length === 1 ? "" : "s"} this editor cannot set. ` +
        "Every one of them is written back exactly as it arrived."
    )
  );
  for (const { key, value, why } of carried) {
    const row = make("div", "preset-carried-row");
    row.append(make("span", "mono preset-key", key));
    row.append(make("span", "mono preset-value", valueText(value)));
    row.append(make("span", "dim preset-why", why));
    panel.append(row);
  }
  return panel;
}

/** One preset, as a row: what it holds, where to run it, and the two ways to change it. */
export function renderRow(row, { onEdit, onDelete, arming }) {
  const name = readPreset(row, "name");
  const item = make("div", "preset-row");

  const head = make("div", "preset-row-head");
  head.append(make("span", "preset-name", name));
  const run = make("a", "preset-run", "Run it →");
  run.href = runHref(name);
  run.setAttribute("title", "opens the search page with this preset selected");
  head.append(run);
  item.append(head);

  const description = readPreset(row, "description");
  item.append(
    description === null
      ? make("p", "note dim", "(no description)")
      : make("p", "note", description)
  );

  const config = readPreset(row, "config");
  const keys = Object.keys(config);
  item.append(
    make(
      "p",
      "note mono preset-config",
      keys.length === 0 ? "config {} — this preset changes nothing" : valueText(config)
    )
  );

  const updated = readPreset(row, "updated_at");
  const created = readPreset(row, "created_at");
  item.append(
    make(
      "p",
      "note dim",
      updated !== null
        ? `last changed ${updated} — by any token holder; the row does not record who`
        : created !== null
          ? `created ${created}; never changed since`
          : "this row carries no timestamps"
    )
  );

  const actions = make("div", "preset-actions");
  const edit = make("button", "ghost", "Edit a copy of this into the form");
  edit.type = "button";
  edit.addEventListener("click", () => onEdit(row));
  actions.append(edit);

  // Two presses, and the second one says what it destroys and for whom. A single Delete button on
  // state that is shared with every other token holder is one slip away from taking away
  // somebody else's preset, and there is no undo: the row is gone and its config with it.
  const remove = make("button", arming === name ? "bad" : "ghost");
  remove.type = "button";
  remove.textContent =
    arming === name
      ? `Really delete "${name}"? It goes for every token holder.`
      : `Delete "${name}"…`;
  remove.addEventListener("click", () => onDelete(name));
  actions.append(remove);

  item.append(actions);
  return item;
}

/**
 * The 409 panel: a name that is taken is an answer, and this is the answer.
 *
 * Not a red error. What a reader wants at this moment is what the name already holds, what
 * overwriting would replace it with, and an explicit act to do it — so all three are here and the
 * third is a button of its own. The `existing` row is FETCHED when the conflict arrives rather
 * than taken from the list this page loaded at mount, because the two can differ by however long
 * the page has been open and it is the fresh one that is about to be overwritten.
 */
export function renderConflict(existing, { willWrite, description, onOverwrite }) {
  const panel = make("div", "preset-conflict");
  const name = readPreset(existing, "name");
  panel.append(make("h3", null, `A preset called "${name}" already exists`));
  panel.append(
    make(
      "p",
      "note",
      "Nothing was written. Here is what that name holds on this appliance right now, read back " +
        "just now rather than from the list above:"
    )
  );

  const held = readPreset(existing, "config");
  panel.append(make("p", "note mono preset-config", valueText(held)));
  const theirs = readPreset(existing, "description");
  panel.append(make("p", "note dim", theirs === null ? "(no description)" : `“${theirs}”`));

  panel.append(make("h3", null, "What Overwrite would write"));
  panel.append(make("p", "note mono preset-config", valueText(willWrite)));
  panel.append(
    make(
      "p",
      "note dim",
      description === null
        ? "and no description — PUT sends the description field, so leaving it blank clears the " +
          "one this preset has."
        : `and the description “${description}”.`
    )
  );

  const kept = Object.keys(held).filter(
    (key) => key in willWrite && valueText(held[key]) === valueText(willWrite[key])
  );
  if (kept.length) {
    panel.append(
      make(
        "p",
        "note dim",
        `Kept from the existing preset, unchanged: ${kept.join(", ")}. This editor sets four keys ` +
          "and carries the rest."
      )
    );
  }

  const go = make("button", "bad", `Overwrite "${name}" for every token holder (PUT)`);
  go.type = "button";
  go.addEventListener("click", () => onOverwrite(willWrite));
  panel.append(go);
  return panel;
}

// ------------------------------------------------------------------------------- the view

async function component({ client, params }) {
  linkStylesheet();
  const page = make("div", "view-presets");
  page.append(make("h1", null, "Presets"));
  page.append(
    make(
      "p",
      "lede",
      "A saved search configuration. Name one here, then pick it in the search page's context " +
        "box and every method resolves its filters from it."
    )
  );

  // The first thing on the page, above every control, because it is the thing a reader would
  // otherwise find out by surprise.
  page.append(
    make(
      "p",
      "note bad preset-shared",
      "THESE ARE SHARED. A preset is not yours: every holder of this appliance's API token sees " +
        "all of them, can overwrite any of them and can delete any of them, and the row does not " +
        "record who did. Subtree access control is defined over the document tree and a preset " +
        "is not a document, so there is no root above it to consult " +
        "(tests/test_expose_principal_audit.py, CONFIG_OBJECT)."
    )
  );

  // Both are read before anything is drawn and both propagate. The capabilities call is what makes
  // the method control the appliance's own vocabulary rather than a list this page keeps: a fifth
  // retrieval method reaches the select by the appliance reporting it.
  const capabilities = await client.call(OPS.capabilities, {});
  let rows = await client.call(OPS.list, {});

  const methods = ["hybrid", ...readCapabilities(capabilities, "retrieval.methods")];

  // The state this page holds. `base` is the config a save starts from — `{}` for a new preset,
  // and the config of the row being edited otherwise — and it is the reason an unknown key
  // survives a round trip through a form that has no control for it.
  const state = {
    base: {},
    usetypeIsList: false,
    carried: [],
    arming: null,
    problem: null,
    outcome: null,
    conflict: null,
  };

  const listPanel = make("div", "preset-list");
  const formPanel = make("div", "preset-panel");
  const carriedPanel = make("div");
  const outcomePanel = make("div");

  const controls = {};
  controls.name = input("text", "name", params.name ?? params.context ?? "");
  controls.description = input("text", "description", "");
  controls.method = select(
    "method",
    [["", "(not set — the request decides)"], ...methods.map((m) => [m, m])],
    ""
  );
  controls.usetype = input("text", "usetype", "");
  controls.parent_id = input("text", "parent_id", "");
  controls.limit = input("number", "limit", "");

  /** Put a config into the four controls, and hold what this page will carry from it. */
  function load(config, { description = "", name = null } = {}) {
    const split = splitConfig(config);
    state.base = { ...config };
    state.usetypeIsList = split.usetypeIsList;
    state.carried = split.carried;
    if (name !== null) controls.name.value = name;
    controls.description.value = description;
    controls.method.value = split.fields.method;
    // A filter this page cannot express leaves the control BLANK and disabled rather than showing
    // a flattened version of it, because a text box holding "a,b" over a value that is not "a,b"
    // is a control that lies. The value itself is in the carried panel below, verbatim.
    controls.usetype.value = split.fields.usetype ?? "";
    controls.usetype.disabled = split.fields.usetype === null;
    controls.usetype.className = split.fields.usetype === null ? "preset-uneditable" : "";
    if (split.fields.usetype === null) {
      controls.usetype.setAttribute(
        "title",
        "this preset's usetype filter is a list with a comma inside a glob; a comma-separated " +
          "text box cannot express it, so it is carried below instead of edited here"
      );
    }
    controls.parent_id.value = split.fields.parent_id;
    controls.limit.value = split.fields.limit;
    drawCarried();
  }

  function drawCarried() {
    carriedPanel.textContent = "";
    carriedPanel.append(renderCarried(state.carried));
  }

  function formFields() {
    return {
      method: controls.method.value,
      // `null` — carried, do not touch — when the control is disabled because the stored value is
      // one this page cannot express. `""` would delete it.
      usetype: controls.usetype.disabled ? null : controls.usetype.value,
      parent_id: controls.parent_id.value,
      limit: controls.limit.value,
    };
  }

  const describedAs = () => {
    const text = controls.description.value.trim();
    return text === "" ? null : text;
  };

  function drawList() {
    listPanel.textContent = "";
    listPanel.append(make("h2", null, "On this appliance"));
    if (rows.length === 0) {
      listPanel.append(
        make(
          "p",
          "dim",
          "No preset exists here. The appliance seeds none on purpose — see ROADMAP.md known " +
            "gap 5 and tests/test_search_context_presets.py, which asserts the table is empty so " +
            "the absence stays a decision. One written here is not a seed."
        )
      );
      return;
    }
    for (const row of rows) {
      listPanel.append(
        renderRow(row, { onEdit: startEdit, onDelete: askDelete, arming: state.arming })
      );
    }
  }

  function drawOutcome() {
    outcomePanel.textContent = "";
    if (state.problem) outcomePanel.append(make("p", "note bad", state.problem));
    if (state.outcome) outcomePanel.append(state.outcome);
    if (state.conflict) outcomePanel.append(state.conflict);
  }

  function clearOutcome() {
    state.problem = null;
    state.outcome = null;
    state.conflict = null;
  }

  function startEdit(row) {
    clearOutcome();
    state.arming = null;
    load(readPreset(row, "config"), {
      description: readPreset(row, "description") ?? "",
      name: readPreset(row, "name"),
    });
    state.outcome = make(
      "p",
      "note dim",
      `The form now holds "${readPreset(row, "name")}". Saving tries to CREATE it, which will ` +
        "come back 409 because it exists — and that answer is where the overwrite is offered, " +
        "with what the row holds at that moment rather than what it held when this page loaded."
    );
    drawList();
    drawOutcome();
  }

  async function askDelete(name) {
    // First press arms, second press deletes. Nothing is sent by the first.
    if (state.arming !== name) {
      state.arming = name;
      clearOutcome();
      drawList();
      drawOutcome();
      return;
    }
    state.arming = null;
    clearOutcome();
    try {
      await client.call(OPS.delete, { name });
      rows = await client.call(OPS.list, {});
      state.outcome = make("p", "note", `Deleted "${name}". It is gone for everybody.`);
    } catch (error) {
      state.problem = errorText(error);
    }
    drawList();
    drawOutcome();
  }

  async function overwrite(name, config) {
    clearOutcome();
    try {
      await client.call(OPS.update, {
        name,
        request: { description: describedAs(), config },
      });
      rows = await client.call(OPS.list, {});
      const done = make("p", "note");
      done.append(make("span", null, `Overwrote "${name}" for every token holder. `));
      const link = make("a", null, "Run it →");
      link.href = runHref(name);
      done.append(link);
      state.outcome = done;
    } catch (error) {
      state.problem = errorText(error);
    }
    drawList();
    drawOutcome();
  }

  async function save() {
    clearOutcome();
    let name;
    let config;
    try {
      name = presetName(controls.name.value);
      config = mergeConfig(state.base, formFields(), { usetypeIsList: state.usetypeIsList });
    } catch (error) {
      // The form does not submit and the reason is on the page, naming the control that produced
      // it. Fail Early applies to a form: an invalid config is refused here rather than sent for
      // the appliance to refuse.
      if (!(error instanceof PresetError)) throw error;
      state.problem = error.message;
      drawOutcome();
      return;
    }

    try {
      const created = await client.call(OPS.create, {
        request: { name, description: describedAs(), config },
      });
      rows = await client.call(OPS.list, {});
      const done = make("p", "note");
      done.append(
        make("span", null, `Created "${readPreset(created, "name")}", visible to every token holder. `)
      );
      const link = make("a", null, "Run it →");
      link.href = runHref(name);
      done.append(link);
      done.append(
        make(
          "span",
          "dim",
          " Nothing checked that its filters match anything this appliance writes — running it is " +
            "the check (ROADMAP.md known gap 5)."
        )
      );
      state.outcome = done;
      drawList();
      drawOutcome();
      return;
    } catch (error) {
      if (!(error && error.status === 409)) {
        // Shown verbatim. `JmftsHttpError`'s message already carries the method, the path, the
        // status and the server's own `detail`; rewriting it would lose all four.
        state.problem = errorText(error);
        drawOutcome();
        return;
      }
      // A 409 is an answer. Read what that name holds NOW, and offer the overwrite against it.
      let existing;
      try {
        existing = await client.call(OPS.get, { name });
      } catch (second) {
        state.problem =
          `"${name}" is taken — the appliance answered 409 — and reading it back failed: ` +
          errorText(second);
        drawOutcome();
        return;
      }
      // The overwrite is composed against THEIR config, not against the one this form started
      // from, so a key somebody else added while this page was open survives the overwrite.
      const theirs = readPreset(existing, "config");
      const split = splitConfig(theirs);
      const willWrite = mergeConfig(theirs, formFields(), {
        usetypeIsList: split.usetypeIsList,
      });
      if (describedAs() === null) {
        const theirDescription = readPreset(existing, "description");
        if (theirDescription !== null) controls.description.value = theirDescription;
      }
      state.carried = split.carried;
      drawCarried();
      state.conflict = renderConflict(existing, {
        willWrite,
        description: describedAs(),
        onOverwrite: (config_) => void overwrite(name, config_),
      });
      drawOutcome();
    }
  }

  // ------------------------------------------------------------------------------ the form

  formPanel.append(make("h2", null, "Write one"));
  const form = make("form", "preset-form");
  form.autocomplete = "off";
  form.append(field("name", controls.name, "how a search asks for it: ?context=NAME"));
  form.append(field("description", controls.description, "what this preset is for"));
  form.append(
    field("method", controls.method, "GET /search/ only; a POST names its method in the path")
  );
  form.append(field("usetype globs", controls.usetype, "comma-separated; blank saves no filter"));
  form.append(field("subtree (parent id)", controls.parent_id, "a document id"));
  form.append(field("limit", controls.limit, "1..100, and read the note below"));

  const actions = make("div", "preset-field preset-wide");
  const go = make("button", null, "Create — visible to every token holder on this appliance");
  go.type = "submit";
  actions.append(go);
  form.append(actions);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void save();
  });
  formPanel.append(form);
  formPanel.append(make("p", "note dim", LIMIT_IS_ALMOST_ALWAYS_OVERRIDDEN));

  const fromSearch = fieldsFromSearchParams(params);
  for (const { param, value, what } of unsavedFromSearch(params)) {
    formPanel.append(
      make("p", "note dim", `The link carried ${param}=${JSON.stringify(value)}, and it is NOT saved: ${what}`)
    );
  }

  formPanel.append(carriedPanel);
  formPanel.append(outcomePanel);

  // What the appliance runs when a preset says nothing about it, so the two keys this editor
  // cannot set are at least readable next to the ones it can.
  const defaults = make("p", "note dim");
  defaults.textContent =
    "With no `methods` and no `weights` saved, a hybrid search fuses " +
    `${readCapabilities(capabilities, "retrieval.default_methods").join(" + ")} weighted ` +
    `${valueText(readCapabilities(capabilities, "retrieval.default_weights"))}.`;
  formPanel.append(defaults);

  page.append(formPanel);
  page.append(listPanel);

  // Start from the preset the link names, when it names one: `?name=` is this page's own, and
  // `?context=` is what `views/search.js` puts in a URL for the preset a search was run under, so
  // the link from a search that used one lands on that one.
  const named = params.name ?? params.context ?? null;
  const starting = named === null ? null : rows.find((row) => readPreset(row, "name") === named);
  if (named !== null && starting === undefined) {
    state.problem =
      `No preset is named "${named}" on this appliance. The form holds that name, so saving ` +
      "will create it.";
  }
  load(starting ? readPreset(starting, "config") : {}, {
    description: starting ? (readPreset(starting, "description") ?? "") : "",
    name: named,
  });
  // The search that sent a reader here wins over the row it is being saved onto: the point of the
  // link is to save THIS search, and what it does not name is left as the row had it. The one
  // exception is a control this page disabled because the stored value is one it cannot express —
  // writing into it would be ignored by `formFields()`, so the value is reported as NOT applied
  // rather than dropped where nobody can see it.
  for (const [key, value] of Object.entries(fromSearch)) {
    if (controls[key].disabled) {
      state.problem =
        `The link carried ${key}=${JSON.stringify(value)}, and this preset's stored ${key} is a ` +
        "value this editor cannot express — so it was NOT applied. The stored value is listed " +
        "below, untouched.";
      continue;
    }
    controls[key].value = value;
  }

  drawList();
  drawOutcome();
  return page;
}

registerView({
  id: "presets",
  title: "Presets",
  path: "/presets",
  component,
  tags: ["retrieval"],
});
