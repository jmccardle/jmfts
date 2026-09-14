// The capability landing: what this appliance accepts, indexes and retrieves.
//
// docs/SPRINT_0_6_0.md Block F steps 23 and 24. This was the whole front end; step 24 made it
// the first registered view, and it is the worked example of IC-7 — a module that declares
// itself and is reached only because `shell/manifest.js` imports it.
//
// ---------------------------------------------------------------------------------------
// WHY `field()` EXISTS, AND WHY IT THROWS
//
// A visual pass over the running appliance on 2026-09-14 found three rows of this table
// reading field names that `CapabilitiesResponse` does not have:
//
//   cap.extras treated as an object, via Object.keys()  — it is list[ExtraStatus]
//   cap.formats                                         — there is no such field; the real
//                                                         ones are cap.ingest.detectable_formats
//                                                         and cap.ingest.probeable_formats
//   cap.retrieval.excluded_usetypes                     — it is retrieval.search_exclude_usetypes
//
// ALL THREE FAILED SILENTLY, and that is the part worth fixing rather than the three names.
// The old `pills(items)` rendered `(items || []).length === 0` as "none", so a field name that
// does not exist rendered IDENTICALLY to a field that is legitimately empty. The browser
// console reported nothing, because nothing went wrong: `undefined || []` is a perfectly good
// empty list. An appliance with no probeable formats and an appliance the page cannot read
// looked the same, and the second one is the one nobody would have gone looking for.
//
// The shape was never in doubt. `jmfts-client/jmfts_client/contracts/meta.py` is the
// authority, and the generated `client/verbs.d.ts` already carried it correctly —
// `extras: Array<ExtraStatus>`, `detectable_formats`, `search_exclude_usetypes`. Nothing
// checked this page against either. That is the reason this happened.
//
// So the fix is not three corrected names. It is that this view no longer reaches into the
// response with `.`: `READS` names every path it reads, `field()` walks a path and REFUSES one
// the response does not carry, and `tests/test_web_views.py` resolves all of `READS` against
// the Pydantic model. A misspelt field is now an error on the page and a red test in the
// suite, rather than the word "none".
// ---------------------------------------------------------------------------------------

import { registerView } from "../shell/registry.js";

/** This view asked for a field the response does not carry. */
export class ContractError extends Error {
  constructor(message) {
    super(message);
    this.name = "ContractError";
  }
}

/**
 * Every path this view reads out of `CapabilitiesResponse`, as dotted field names.
 *
 * `tests/test_web_views.py::test_the_capability_view_reads_only_fields_the_contract_carries`
 * resolves each of these against `jmfts_client.contracts.meta.CapabilitiesResponse` itself, so
 * a field renamed in the contract fails in the suite rather than on somebody's screen. This is
 * not a second copy of the contract — it is the list of what this ONE page uses, checked
 * against the one definition.
 */
export const READS = Object.freeze([
  "version",
  "extras",
  "embedding.local_model_available",
  "embedding.runner_url",
  "embedding.can_embed",
  "ingest.usetypes",
  "ingest.detectable_formats",
  "ingest.probeable_formats",
  "retrieval.methods",
  "retrieval.default_weights",
  "retrieval.search_exclude_usetypes",
  "retrieval.bm25_exclude_usetypes",
  "corpus",
  "corpus.documents",
  "corpus.documents_with_vectors",
  "corpus.documents_with_token_vectors",
  "corpus.bm25_indexes",
]);

/**
 * Read one declared path out of a response, refusing a field the response does not carry.
 *
 * A key that is PRESENT and null is a value and comes back as null — `embedding.runner_url` is
 * null on an appliance that embeds locally, and that is an answer. A key that is ABSENT is not
 * an answer, and the difference between the two is the whole of this function.
 */
export function field(response, path) {
  if (!READS.includes(path)) {
    throw new ContractError(
      `"${path}" is not in this view's READS. Add it there — that list is what the suite ` +
        "checks against jmfts_client.contracts.meta.CapabilitiesResponse."
    );
  }
  let node = response;
  const walked = [];
  for (const key of path.split(".")) {
    if (node === null || typeof node !== "object" || Array.isArray(node) || !(key in node)) {
      throw new ContractError(
        `/capabilities carries no ${walked.length ? `"${key}" under "${walked.join(".")}"` : `"${key}"`}` +
          ` (reading "${path}"). The appliance's CapabilitiesResponse and this page disagree; ` +
          "jmfts-client/jmfts_client/contracts/meta.py is the authority."
      );
    }
    node = node[key];
    walked.push(key);
  }
  return node;
}

// -------------------------------------------------------------------------------- drawing

const make = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

/**
 * A list of names as pills. An EMPTY list renders "none" — which is now only ever the truth,
 * because a name the response does not carry never reaches here: `field()` threw first.
 */
function pills(names, { on = () => false, describe = () => null } = {}) {
  const box = make("span");
  if (names.length === 0) {
    box.append(make("span", "dim", "none"));
    return box;
  }
  for (const name of names) {
    const pill = make("span", on(name) ? "pill on" : "pill", String(name));
    const description = describe(name);
    if (description) pill.setAttribute("title", description);
    box.append(pill);
  }
  return box;
}

function rows(cap, operationCount) {
  const out = [];
  const row = (label, value, note) => out.push([label, value, note ?? null]);

  row("Version", make("span", null, field(cap, "version")));

  // `extras` is a LIST of {name, installed, provides, install} — the first of the three
  // defects above. Each pill is named, marked when the extra's imports resolve in that
  // process, and carries what it provides and how to install it.
  const extras = field(cap, "extras");
  row(
    "Extras",
    pills(
      extras.map((e) => e.name),
      {
        on: (name) => extras.find((e) => e.name === name).installed,
        describe: (name) => {
          const extra = extras.find((e) => e.name === name);
          return extra.installed
            ? `installed — ${extra.provides}`
            : `not installed — ${extra.provides}. ${extra.install}`;
        },
      }
    ),
    `${extras.filter((e) => e.installed).length} of ${extras.length} installed`
  );

  const runner = field(cap, "embedding.runner_url");
  row(
    "Can produce a vector",
    field(cap, "embedding.local_model_available")
      ? make("span", null, "yes, in this process")
      : runner !== null
        ? make("span", null, `yes, via ${runner}`)
        : make("span", "bad", "no — neither a local model nor a runner"),
    field(cap, "embedding.can_embed") ? null : "ModelStackNotInstalled on any request for one"
  );

  row("Ingest entry points", pills(field(cap, "ingest.usetypes")));

  // The second defect: there is no `cap.formats`. There are two lists and they answer
  // different questions, so the page shows both rather than picking one and calling it
  // "formats" — identifying a `.docx` from its bytes and being able to look INSIDE it are
  // separate capabilities, and the second is the `office` extra.
  row(
    "Formats identified from the bytes",
    pills(field(cap, "ingest.detectable_formats")),
    "anything else falls back to the filename extension, which is a hint"
  );
  row(
    "Formats something can look inside",
    pills(field(cap, "ingest.probeable_formats")),
    "a format outside this list gets an empty pattern set, and the structure rungs plan from that"
  );

  const weights = field(cap, "retrieval.default_weights");
  row("Retrieval methods", pills(field(cap, "retrieval.methods"), {
    describe: (name) => (name in weights ? `default RRF weight ${weights[name]}` : "weighted 1.0"),
  }));

  // The third defect: the field is `search_exclude_usetypes`. The row beside it is a
  // different exclusion with a different remedy, and they were never distinguished here.
  row(
    "Held out of every result",
    pills(field(cap, "retrieval.search_exclude_usetypes")),
    "excluded at SEARCH time, when a request names no exclude_types"
  );
  row(
    "Never written to a BM25 index",
    pills(field(cap, "retrieval.bm25_exclude_usetypes")),
    "excluded at INDEX time — re-indexing changes this, not a search argument"
  );

  // The fourth finding, and it was a decision rather than a typo: the page asked for
  // `corpus=true` and rendered none of what came back. These four counts are the answer to
  // "will a search return anything on this appliance", which is the first thing anybody doing
  // eyes-on testing needs. Rendering them was the choice; the alternative was to stop asking,
  // and a call that fetches a block the page drops is the same class of defect as a field name
  // nothing reads.
  const corpus = field(cap, "corpus");
  if (corpus === null) {
    row(
      "Corpus",
      make("span", "bad", "this appliance was asked for corpus counts and returned none"),
      "the request set corpus=true"
    );
  } else {
    const documents = field(cap, "corpus.documents");
    const vectors = field(cap, "corpus.documents_with_vectors");
    const tokens = field(cap, "corpus.documents_with_token_vectors");
    const indexes = field(cap, "corpus.bm25_indexes");
    row("Documents", make("span", null, String(documents)));
    row(
      "Documents a vector search can reach",
      make("span", vectors === 0 ? "bad" : null, String(vectors)),
      vectors === 0 ? "/search/vector returns nothing here" : null
    );
    row(
      "Documents MaxSim can rank",
      make("span", tokens === 0 ? "bad" : null, String(tokens)),
      tokens === 0 ? "/search/maxsim returns nothing here, whatever else is installed" : null
    );
    row(
      "BM25 indexes",
      pills(indexes),
      indexes.length === 0 ? "the bm25 leg of hybrid contributes nothing" : null
    );
  }

  // The one row that is NOT from a call, and it says so. Property 2 of step 23 is that a view
  // showing something the log has no call for computed it in the browser; a page claiming that
  // has to hold itself to it, and the way to hold to it is to label the exception rather than
  // to quietly have one.
  row(
    "Operations this client knows",
    make("span", null, String(operationCount)),
    "generated table, not a call"
  );

  return out;
}

async function component({ client }) {
  const page = make("div", "view-capabilities");
  page.append(make("h1", null, "JMFTS"));
  page.append(
    make(
      "p",
      "lede",
      "Matryoshka embeddings, ColBERT-style late interaction, and BM25 over PostgreSQL. " +
        "Every row below is read from /capabilities, which makes no network call of its own."
    )
  );

  // `/health` is the one path in PUBLIC_PATHS, so this answers with no credential — which is
  // what lets the next failure read as "the token is wrong" rather than "nothing is running".
  // Both failures propagate to the shell, which shows the message rather than a blank page.
  try {
    await client.health_check_liveness();
  } catch (error) {
    throw new Error(`the appliance is not answering /health: ${error.message}`);
  }

  const cap = await client.capabilities({ corpus: true });

  const table = make("table", "capability-table");
  for (const [label, value, note] of rows(cap, Object.keys(client.operations).length)) {
    const line = make("tr");
    const head = make("th", null, label);
    const cell = make("td");
    cell.append(value);
    if (note) cell.append(make("p", "dim note", note));
    line.append(head, cell);
    table.append(line);
  }
  page.append(table);
  return page;
}

registerView({
  id: "capabilities",
  title: "This appliance",
  path: "/capabilities",
  component,
  tags: ["appliance"],
});
