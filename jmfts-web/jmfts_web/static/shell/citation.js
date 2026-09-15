// The beeline's hand-off: how a search hit hands its citation to the document detail view.
//
// docs/SPRINT_0_6_0.md Block F step 31. ONE ENCODING, WRITTEN ONCE. `views/search.js` writes
// the fragment and `views/document.js` reads it back, and until this file existed each of them
// carried its own copy of the reader — `readDetailParams`, byte-identical in two modules but
// for the name of the error class. Two lanes wrote it because neither could import the other's
// file, and both said so at the definition: "identical copies reconcile at M3 by DELETING one,
// which a paraphrase would not." This is that deletion, and the copy that survived is neither
// of them.
//
// WHY NOT "THE SEARCH VIEW OWNS IT AND THE DETAIL VIEW IMPORTS IT". Because that is a view
// importing a view, which is precisely what IC-7 bought independence FROM: five worktrees add
// five views in parallel and no two of them share a file, and `views/search.js:86` states the
// rule it was following — "`shell/registry.js` is the only file a view may depend on besides
// the client and the renderers". A detail view that imported the search view would also be a
// detail view that cannot be mounted without the search view's seven-column METHODS table and
// its `/capabilities` reader loading first, for a URL parser. The direction the other way round
// is worse again: the WRITER would import the READER.
//
// SO IT IS A THIRD MODULE, AND IT SPENDS NOTHING. The layer a view may depend on already exists
// and both of these views already sit on it — `shell/registry.js` for the registration,
// `render/` for the geometry. What IC-7 removes as a shared file is THE ROUTER, because a
// router is a file every view edits; this file is a leaf that no view edits, added once, and
// two worktrees appending two views still never touch it. `shell/` rather than `render/` for
// the reason `render/surfaces.js` gives at its own `pageLink` seam: "`render/` knows nothing
// about this front end's routing and should not — a fragment router is the shell's." A route
// argument is routing. `shell/shell.js`'s `currentRoute` and `navigate` build and split the
// same fragment this file writes into and reads out of.
//
// WHAT RIDES IN THE URL, AND WHY IT IS THE URL. Both evidence rows, as JSON, on the fragment:
//
//   #/document?id=42&anchor={"kind":"pdf","page":3,"bbox":[...]}&unresolved={"code":...}
//
// That is the only encoding with all three of the properties this hand-off needs at once. The
// detail view gets the anchor with NO SECOND CALL, which is the round trip IC-4 put the rows on
// the hit to remove; a pasted link still carries it; and a reload does not quietly downgrade a
// boxed citation to an unboxed one. A module-level map keyed by document id would do the first
// and neither of the others.
//
// A KEY THAT IS ABSENT MEANS THE HIT CARRIED NO SUCH ROW, and that is a third fact rather than
// a spelling of null. `readDetailParams` reports a missing row and an absent argument
// identically — as null, which is what `highlight()` wants — so the presence of the KEY is what
// says somebody already looked. See `broughtACitation`: a hit navigates with both arguments
// when it has them and with neither when it has none, and a tree click has never looked at all.
// The one that has not looked pays for one `/evidence` call; nobody pays twice.

/** A detail route this front end cannot honour, in either direction. */
export class CitationRouteError extends Error {
  constructor(message) {
    super(message);
    this.name = "CitationRouteError";
  }
}

/** The route `views/document.js` registers. Named once, here, and imported by both views. */
export const DETAIL_PATH = "/document";

/**
 * The fragment parameters that take one citation to the detail view without losing it.
 *
 * Takes the three VALUES rather than a search hit, which is what lets this file sit under both
 * views instead of inside one of them: the hit shape is `SearchResultItem`'s and reading it is
 * `views/search.js`' declared-reads discipline, while what is encoded here is an id and two
 * evidence rows. `views/search.js` does the reading and calls this with the answers.
 *
 * The three names it takes are the three names :func:`readDetailParams` gives back, so the pair
 * round-trips by inspection: `readDetailParams(detailParams(x))` is `x`. That is the property
 * `tests/test_web_search_view.py` asserts, and it is the strongest available form of "these two
 * are one encoding" now that they are one file.
 *
 * @param {{documentId: number|string, anchor?: object|null, unresolved?: object|null}} citation
 * @returns {{id: string, anchor?: string, unresolved?: string}}
 */
export function detailParams({ documentId, anchor = null, unresolved = null }) {
  if (documentId === undefined || documentId === null || !/^\d+$/.test(String(documentId))) {
    throw new CitationRouteError(
      `a citation names the document it points into; got ${JSON.stringify(documentId ?? null)}.`
    );
  }
  const params = { id: String(documentId) };
  if (anchor !== null && anchor !== undefined) params.anchor = JSON.stringify(anchor);
  if (unresolved !== null && unresolved !== undefined) {
    params.unresolved = JSON.stringify(unresolved);
  }
  return params;
}

/**
 * Read what `detailParams` wrote. The detail view's half of the hand-off.
 *
 * Returns `{documentId, anchor, unresolved}` with BOTH evidence keys present even when null,
 * because that is the shape `highlight()` in `render/overlay.js` refuses to be called without —
 * "a caller that passes only `anchor` has not said whether it looked for the unresolved row",
 * and "I did not look" is not "there was no reason recorded".
 *
 * Unreadable JSON RAISES rather than coming back as "no anchor". A truncated fragment and a hit
 * with no citation are different facts, and collapsing them is how a highlight silently stops
 * appearing on a link somebody edited by hand — the same failure `source_anchor.unresolved`
 * exists to prevent, arriving by a different road.
 */
export function readDetailParams(params) {
  const one = (name) => {
    const text = params[name];
    if (text === undefined || text === "") return null;
    try {
      return JSON.parse(text);
    } catch (error) {
      throw new CitationRouteError(
        `the "${name}" fragment parameter is not JSON: ${error.message}. It is written by ` +
          "detailParams() in shell/citation.js and read back by readDetailParams() beside it; " +
          "a hand-edited URL can break it."
      );
    }
  };
  const id = params.id;
  if (id === undefined || !/^\d+$/.test(String(id))) {
    throw new CitationRouteError(
      `the document detail route needs an integer "id"; got ${JSON.stringify(id ?? null)}.`
    );
  }
  return { documentId: Number(id), anchor: one("anchor"), unresolved: one("unresolved") };
}

/** The href one citation points at. What a hit's title links to. */
export function detailHref(citation) {
  const query = new URLSearchParams(Object.entries(detailParams(citation))).toString();
  return `#${DETAIL_PATH}?${query}`;
}

/**
 * Did the caller hand a citation over at all?
 *
 * The presence of the KEY, not the value behind it — see this module's header. This is the one
 * question that decides whether the detail view spends a `/documents/{id}/evidence` call.
 */
export function broughtACitation(params) {
  return "anchor" in params || "unresolved" in params;
}

/**
 * A link back to the detail route with some arguments replaced, keeping the rest.
 *
 * What "keeping the rest" is for: a reader on a citation's page clicking "show page 4" must not
 * lose the citation on the way. Empty and null values are dropped rather than written as the
 * strings "null" and "", so an override can also REMOVE an argument.
 */
export function hrefWith(params, overrides) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries({ ...params, ...overrides })) {
    if (value !== null && value !== undefined && value !== "") query.append(key, String(value));
  }
  return `#${DETAIL_PATH}?${query.toString()}`;
}
