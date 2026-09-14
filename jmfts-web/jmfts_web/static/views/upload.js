// The ingest view: drop a file, read what the appliance WOULD do, then let it do it.
//
// docs/SPRINT_0_6_0.md Block F step 26, IC-7. Six operations, one page: `/ingest/pipelines`,
// `/ingest/analyze`, `/ingest/explain`, `/ingest/file`, `/ingest/file/{id}/frontier` and
// `/ingest` for text that arrives as a string rather than as bytes.
//
// ---------------------------------------------------------------------------------------
// THE TWO THINGS THIS PAGE EXISTS TO NOT LIE ABOUT
//
// 1. WHAT WILL HAPPEN, BEFORE IT HAPPENS. Dropping a file runs `/ingest/analyze`, which
//    "runs probe and stops" (INGEST_SPEC.md 11.2) — no document, no blob, no queue row. The
//    button that actually ingests does not exist until that answer is on the page, and it
//    says what it is about to do: how many tasks would be enqueued, whether these bytes are
//    already stored, and whether probe would fail. Editing the options INVALIDATES the
//    analysis rather than leaving a plan on screen that no longer describes the request —
//    a stale forecast is worse than none, because it is indistinguishable from a fresh one.
//
// 2. WHAT IS HAPPENING NOW. `POST /ingest/file` returns "once the file node exists with
//    settled = 'in_flight', the blob is stored, and probe is enqueued. It does not wait for
//    extraction" (spec 5.7). A workbook's rows do not exist when that call returns. So this
//    page NEVER shows an upload as finished on the strength of the upload response: it shows
//    `accepted`, and only `GET /ingest/file/{id}/frontier` can move it to `in_flight` or
//    `settled`. `ingestionState()` below is the whole of that judgement, it is exported, and
//    `tests/test_web_upload_view.py` walks it through a draining queue.
//
// The frontier is COUNTS, never a percentage, and the contract says why: the tasks below the
// current frontier do not exist yet, so the denominator moves backward as work is discovered
// (`IngestFrontierResponse`, `INGEST_SPEC.md` 2.4). A progress bar here would be a fabricated
// number; four counts and a named state are not.
//
// ---------------------------------------------------------------------------------------
// WHY EVERY READ GOES THROUGH `field()`
//
// `views/capabilities.js` carries the story at length: three rows of that table read field
// names `CapabilitiesResponse` does not have, and all three rendered as the word "none"
// because an absent field and an empty list are the same thing to `undefined || []`. This
// view reads SEVEN contracts, so it takes the same medicine — `READS` names every path by
// the contract that owns it, `field()` refuses a path the response does not carry, and
// `test_the_upload_view_reads_only_fields_the_contracts_carry` resolves all of them against
// the Pydantic models in `jmfts-client/jmfts_client/contracts/`.
//
// That helper is duplicated between the two views rather than shared, and that is a
// deliberate small cost with a note attached: the shell owns no `field()` to import, and
// inventing a shared module in `views/` during a phase where five worktrees run in parallel
// would put a new shared file exactly where IC-7 removed the last one. If a third view wants
// it, it belongs in the shell.
// ---------------------------------------------------------------------------------------

import { registerView } from "../shell/registry.js";

/** This view asked for a field a contract does not carry. */
export class ContractError extends Error {
  constructor(message) {
    super(message);
    this.name = "ContractError";
  }
}

/**
 * Every path this view reads, keyed by the contract class that defines it.
 *
 * The keys are the PYTHON class names on purpose: the test looks each one up in
 * `jmfts_client.contracts` and resolves its paths against the model itself, so a field
 * renamed in the client distribution fails in the suite rather than on somebody's screen.
 */
export const READS = Object.freeze({
  AnalyzeIngestResponse: Object.freeze([
    "file",
    "format",
    "patterns",
    "probe_failed",
    "plan",
    "already_stored",
  ]),
  AnalyzedFile: Object.freeze([
    "filename",
    "byte_size",
    "content_hash",
    "declared_mime",
    "detected_mime",
    "detected_by",
    "mime_agrees",
  ]),
  ProbeFailure: Object.freeze(["error", "error_type"]),
  AlreadyStored: Object.freeze(["document_id", "settled", "options"]),
  ExplainIngestResponse: Object.freeze([
    "format",
    "prober_available",
    "patterns_known",
    "patterns_source",
    "patterns_ignored",
    "options",
    "tasks",
  ]),
  ExplainedTaskResponse: Object.freeze([
    "task",
    "outcome",
    "if_condition_holds",
    "reason",
    "write_mode",
    "scope",
    "requires",
    "forbids",
  ]),
  FileUploadResponse: Object.freeze([
    "document_id",
    "filename",
    "settled",
    "byte_size",
    "content_hash",
    "detected_mime",
    "was_existing",
    "governed",
    "linked_into_parent",
    "attempts",
  ]),
  AttemptRecord: Object.freeze(["task", "status", "error"]),
  IngestFrontierResponse: Object.freeze([
    "document_id",
    "settled",
    "nodes_settled",
    "nodes_in_flight",
    "nodes_failed",
    "nodes_total",
    "tasks_unfinished",
  ]),
  PipelineInfo: Object.freeze(["name", "description", "source", "options"]),
  IngestResponse: Object.freeze([
    "source_document_id",
    "title",
    "usetype",
    "tree_depth",
    "was_existing",
    "existing_document_id",
    "governed",
  ]),
});

/**
 * Read one declared path out of one contract's response, refusing what it does not carry.
 *
 * Present-and-null is a value and comes back as null — `file.detected_mime` is null for bytes
 * nothing recognises, and that is an answer. ABSENT is not an answer, and the difference
 * between the two is the whole of this function.
 */
export function field(response, contract, path) {
  const declared = READS[contract];
  if (!declared) {
    throw new ContractError(
      `"${contract}" is not a contract this view declares. READS is keyed by the class ` +
        "names in jmfts-client/jmfts_client/contracts/, and the suite resolves it there."
    );
  }
  if (!declared.includes(path)) {
    throw new ContractError(
      `"${path}" is not in READS.${contract}. Add it there — that list is what ` +
        "test_the_upload_view_reads_only_fields_the_contracts_carry checks against the model."
    );
  }
  let node = response;
  const walked = [];
  for (const key of path.split(".")) {
    if (node === null || typeof node !== "object" || Array.isArray(node) || !(key in node)) {
      throw new ContractError(
        `${contract} carries no ` +
          (walked.length ? `"${key}" under "${walked.join(".")}"` : `"${key}"`) +
          ` (reading "${path}"). The appliance and this page disagree; ` +
          "jmfts-client/jmfts_client/contracts/ is the authority."
      );
    }
    node = node[key];
    walked.push(key);
  }
  return node;
}

// ------------------------------------------------------------------- the two vocabularies

/**
 * Every outcome `ExplainedTaskResponse.outcome` can hold, with the sentence this page shows.
 *
 * The keys are pinned against that field's own description in the contract, so an appliance
 * that grows a seventh outcome fails the suite instead of rendering a word with no gloss.
 * The distinctions are the contract's and they are not interchangeable: `impossible` is
 * stronger than `not_applicable`, and `deferred` means the condition holds and nothing is
 * registered to act on it.
 */
export const OUTCOMES = Object.freeze({
  enqueued: "a queue row would be written",
  skipped: "the task runs and records that it did nothing",
  deferred: "the condition holds and no handler is registered, so nothing is queued",
  not_applicable: "its condition does not hold for these bytes",
  impossible: "its condition cannot hold for this format, whatever the bytes are",
  conditional: "undecided: nobody has measured the patterns that decide it",
});

/**
 * Where a plan's patterns came from, and what that means for how much to trust the plan.
 *
 * `patterns_source` is load-bearing rather than metadata — the contract says so — because an
 * answer that did not say where its patterns came from would be indistinguishable from one
 * that made them up.
 */
export const PATTERN_SOURCES = Object.freeze({
  probed: "probe ran over these bytes and measured them",
  supplied: "you supplied them; this is a hypothesis, not a measurement",
  no_prober: "this format has no prober, so the empty set is a fact and not a guess",
  unknown: "nobody said, so the undecidable rows below are conditional",
});

/**
 * What this page is showing about one ingestion, and the three words the step asks for.
 *
 * ACCEPTED is the upload response and nothing more: the node exists, the bytes are stored,
 * probe is queued. IN_FLIGHT and SETTLED can only come from the frontier. STALLED and FAILED
 * are the two honest extras — see `ingestionState`.
 */
export const INGESTION_STATUS = Object.freeze({
  ACCEPTED: "accepted",
  IN_FLIGHT: "in_flight",
  STALLED: "stalled",
  SETTLED: "settled",
  FAILED: "failed",
});

/** The states nothing more will happen from, so polling stops. */
const TERMINAL = Object.freeze([INGESTION_STATUS.SETTLED, INGESTION_STATUS.FAILED]);

/**
 * Which of the three states this page is entitled to show, from what it has been told.
 *
 * @param {{upload: object|null, frontier: object|null}} known
 * @returns {{status: string, headline: string, note: string|null}}
 *
 * The rules, in the order they are applied, and each one is a lie this page would otherwise
 * tell:
 *
 * 1. NO FRONTIER IS NOT PROGRESS. An upload response says the node exists; it says nothing
 *    about the queue. That is `accepted`, and it stays `accepted` until a frontier answers.
 * 2. A FAILED NODE IS NOT A FINISHED ONE, even when everything else settled. Reported first
 *    because a subtree can hold both.
 * 3. SETTLED NEEDS ALL THREE: the root's own state, nothing in flight anywhere under it, and
 *    no unfinished tasks. A root that settled while its children drain is the workbook case
 *    the demo turns on — `nodes_in_flight` above zero is not "done".
 * 4. IN FLIGHT WITH AN EMPTY QUEUE IS ITS OWN ANSWER. The contract names it: "Zero with
 *    nodes_in_flight above zero means the frontier is waiting on something other than the
 *    queue." On a demo machine that is almost always a worker that is not running, and
 *    rendering it as ordinary progress is how somebody waits ten minutes for nothing.
 */
export function ingestionState({ upload = null, frontier = null } = {}) {
  if (frontier === null) {
    if (upload === null) {
      throw new ContractError("ingestionState was given neither an upload nor a frontier");
    }
    const existing = field(upload, "FileUploadResponse", "was_existing");
    return {
      status: INGESTION_STATUS.ACCEPTED,
      headline: existing
        ? "accepted — these bytes were already here"
        : "accepted — stored, and probe is queued",
      note: existing
        ? "This request created nothing and enqueued nothing: it resolved to the node that " +
          "already held these bytes. What the frontier reports below is the earlier run."
        : "The node exists and the bytes are stored. Nothing has been extracted yet; ask the " +
          "frontier.",
    };
  }

  const root = field(frontier, "IngestFrontierResponse", "settled");
  const settled = field(frontier, "IngestFrontierResponse", "nodes_settled");
  const inFlight = field(frontier, "IngestFrontierResponse", "nodes_in_flight");
  const failed = field(frontier, "IngestFrontierResponse", "nodes_failed");
  const total = field(frontier, "IngestFrontierResponse", "nodes_total");
  const tasks = field(frontier, "IngestFrontierResponse", "tasks_unfinished");
  const counted = `${settled} settled, ${inFlight} in flight, ${failed} failed, of ${total}`;

  if (failed > 0 || root === "failed") {
    return {
      status: INGESTION_STATUS.FAILED,
      headline: `failed — ${counted}`,
      note: "A failed node keeps its attempt log; the document's own page is where the "
        + "exception and its classification are.",
    };
  }
  if (root === "settled" && inFlight === 0 && tasks === 0) {
    return {
      status: INGESTION_STATUS.SETTLED,
      headline: `settled — ${counted}`,
      note: `${total} node${total === 1 ? "" : "s"} are settled, which is what retrieval can `
        + "reach: every search path filters settled = 'settled'.",
    };
  }
  if (inFlight > 0 && tasks === 0) {
    return {
      status: INGESTION_STATUS.STALLED,
      headline: `in flight, with an empty queue — ${counted}`,
      note: "The queue owes no work and these nodes are still in flight, so the frontier is "
        + "waiting on something other than the queue. A worker that is not running looks "
        + "exactly like this: check that `jmfts-worker` is up.",
    };
  }
  return {
    status: INGESTION_STATUS.IN_FLIGHT,
    headline: `in flight — ${counted}`,
    note: `${tasks} queued task${tasks === 1 ? "" : "s"} still owe work. These nodes are `
      + "invisible to search until they settle, which is the point rather than a delay.",
  };
}

/** Count a plan's tasks by outcome, in the order `OUTCOMES` declares them. */
export function planSummary(plan) {
  const tasks = field(plan, "ExplainIngestResponse", "tasks");
  const counts = new Map();
  for (const task of tasks) {
    const outcome = field(task, "ExplainedTaskResponse", "outcome");
    if (!(outcome in OUTCOMES)) {
      throw new ContractError(
        `the plan reports outcome "${outcome}", which this page has no word for. The ` +
          "vocabulary is ExplainedTaskResponse.outcome's own description; this appliance " +
          "is newer than this page."
      );
    }
    counts.set(outcome, (counts.get(outcome) ?? 0) + 1);
  }
  return Object.keys(OUTCOMES)
    .filter((outcome) => counts.has(outcome))
    .map((outcome) => [outcome, counts.get(outcome)]);
}

// -------------------------------------------------------------------------------- drawing

const make = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

/** `1234` as `1.2 kB`. Decimal, because that is what a file manager shows for an upload. */
export function bytes(count) {
  const units = ["B", "kB", "MB", "GB"];
  let value = count;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
}

function pills(names) {
  const box = make("span");
  if (names.length === 0) {
    box.append(make("span", "dim", "none"));
    return box;
  }
  for (const name of names) box.append(make("span", "pill", String(name)));
  return box;
}

/** A two-column table of label/value, which is how every fact on this page is shown. */
function facts(rows) {
  const table = make("table", "upload-facts");
  for (const [label, value, note] of rows) {
    const line = make("tr");
    const cell = make("td");
    cell.append(typeof value === "string" ? make("span", null, value) : value);
    if (note) cell.append(make("p", "dim note", note));
    line.append(make("th", null, label), cell);
    table.append(line);
  }
  return table;
}

function heading(element, title, sub) {
  element.append(make("h2", null, title));
  if (sub) element.append(make("p", "dim upload-sub", sub));
}

function problem(message) {
  return make("p", "bad upload-problem", message);
}

/**
 * A node id, as a link to the detail view that shows it.
 *
 * `#/document?id=N` is step 28's route and this page does not import anything of that lane's
 * to build it — a fragment is a string, and the shell resolves it. If that view is not
 * registered the shell answers "No such view" and lists what is, which is a better thing for
 * a reader to meet than an id they have to paste somewhere themselves.
 */
function documentLink(documentId) {
  const link = make("a", null, String(documentId));
  link.href = `#/document?id=${documentId}`;
  return link;
}

// ------------------------------------------------------------------------- the plan panel

/**
 * One plan, from `/ingest/explain` or from the `plan` inside an `/ingest/analyze` answer.
 *
 * ONE renderer for both, because they are one shape: `ANALYZE` is `EXPLAIN` with the third
 * input measured rather than assumed, and `AnalyzeIngestResponse.plan` IS an
 * `ExplainIngestResponse`. Two renderers would be two chances to describe the same forecast
 * differently.
 */
export function renderPlan(plan) {
  const box = make("div", "upload-plan");

  const source = field(plan, "ExplainIngestResponse", "patterns_source");
  const known = field(plan, "ExplainIngestResponse", "patterns_known");
  if (!(source in PATTERN_SOURCES)) {
    throw new ContractError(
      `the plan reports patterns_source "${source}", which this page has no word for.`
    );
  }
  box.append(
    make(
      "p",
      known ? "upload-basis" : "upload-basis upload-basis-partial",
      `${known ? "Every task below is decided" : "Some tasks below are undecided"}: ` +
        `patterns ${source} — ${PATTERN_SOURCES[source]}.`
    )
  );

  const ignored = field(plan, "ExplainIngestResponse", "patterns_ignored");
  if (ignored.length) {
    const line = make("p", "dim note", "Patterns nothing schedules on, reported not rejected: ");
    line.append(pills(ignored));
    box.append(line);
  }

  const summary = make("p", "upload-summary");
  const counts = planSummary(plan);
  if (counts.length === 0) {
    summary.append(make("span", "dim", "no tasks at all"));
  } else {
    for (const [outcome, count] of counts) {
      summary.append(make("span", `pill outcome-${outcome}`, `${count} ${outcome}`));
    }
  }
  box.append(summary);

  const table = make("table", "upload-plan-table");
  const head = make("tr");
  for (const label of ["Task", "Outcome", "Scope", "Why"]) head.append(make("th", null, label));
  table.append(head);

  for (const task of field(plan, "ExplainIngestResponse", "tasks")) {
    const outcome = field(task, "ExplainedTaskResponse", "outcome");
    const conditional = field(task, "ExplainedTaskResponse", "if_condition_holds");
    const reason = field(task, "ExplainedTaskResponse", "reason");
    const requires = field(task, "ExplainedTaskResponse", "requires");
    const forbids = field(task, "ExplainedTaskResponse", "forbids");

    const line = make("tr", `outcome-row-${outcome}`);
    line.append(make("td", "mono", field(task, "ExplainedTaskResponse", "task")));

    const verdict = make("td");
    verdict.append(make("span", `pill outcome-${outcome}`, outcome));
    if (conditional !== null) {
      verdict.append(make("p", "dim note", `becomes "${conditional}" if its condition holds`));
    }
    line.append(verdict);

    const scope = make("td", "mono");
    scope.append(make("span", null, field(task, "ExplainedTaskResponse", "scope")));
    const writeMode = field(task, "ExplainedTaskResponse", "write_mode");
    if (writeMode !== null) scope.append(make("p", "dim note", `writes: ${writeMode}`));
    line.append(scope);

    const why = make("td");
    // The reason is the sentence the run would write into the node's attempt log. Shown
    // verbatim: rewriting it here would replace an explanation with a paraphrase of one.
    why.append(make("span", reason === null ? "dim" : null, reason ?? "—"));
    if (requires.length || forbids.length) {
      const conditions = make("p", "dim note");
      if (requires.length) conditions.append(make("span", null, `requires ${requires.join(", ")}`));
      if (requires.length && forbids.length) conditions.append(make("span", null, "; "));
      if (forbids.length) conditions.append(make("span", null, `forbids ${forbids.join(", ")}`));
      why.append(conditions);
    }
    line.append(why);
    table.append(line);
  }
  box.append(table);
  return box;
}

// ------------------------------------------------------------------------------ the view

/** 1.5 seconds between frontier readings: often enough to watch, rare enough to ignore. */
export const FRONTIER_POLL_MS = 1500;

/**
 * Build the page and the actions that drive it.
 *
 * Exported beside `component` so the suite can drive the same object the component does —
 * the DOM stub the tests run against has no events, so a drop or a click cannot be
 * simulated, and a page whose only entry point is an event handler would be untestable
 * without a browser. There is no test-only code path here: `component` calls this and
 * returns its element.
 *
 * `schedule` is injected for the same reason `JmftsTransport` takes `fetchImpl` and `now`:
 * the polling loop is behaviour worth asserting, and a test that had to wait 1.5 real
 * seconds per tick would assert it badly.
 */
export function mountUpload(context, options = {}) {
  const { client } = context;
  const schedule = options.schedule ?? whileMounted;

  const state = {
    pipelines: [],
    file: null,
    optionsText: "",
    optionsError: null,
    isPrivate: false,
    parentId: "",
    analysis: null,
    busy: null,
    upload: null,
    frontier: null,
    watching: null,
    polling: false,
    explainFormat: "",
    explain: null,
    textContent: "",
    textUsetype: "",
    textTitle: "",
    textResult: null,
    errors: {},
  };

  const page = make("div", "view-upload");
  const dropPanel = make("section", "upload-panel");
  const analysisPanel = make("section", "upload-panel");
  const progressPanel = make("section", "upload-panel");
  const explainPanel = make("section", "upload-panel");
  const textPanel = make("section", "upload-panel");

  page.append(make("h1", null, "Ingest"));
  page.append(
    make(
      "p",
      "lede",
      "Dropping a file here ANALYSES it and stores nothing: probe runs over the bytes, the " +
        "plan comes back, and the button that ingests appears underneath it. Once bytes are " +
        "ingested, this page reports the settling frontier and says which of accepted, in " +
        "flight and settled it is showing."
    )
  );
  page.append(dropPanel, analysisPanel, progressPanel, explainPanel, textPanel);

  // ------------------------------------------------------------------------ the actions

  /** Run one action, and put whatever it throws on the page rather than in the console. */
  async function attempt(name, work) {
    state.busy = name;
    state.errors[name] = null;
    draw();
    try {
      await work();
    } catch (error) {
      // Shown, not swallowed. A click handler is not inside the shell's mount-time catch,
      // so an error left to propagate here would reach an unhandled rejection and the page
      // would simply not change — which reads as "nothing happened".
      state.errors[name] = error.message;
    } finally {
      state.busy = null;
      draw();
    }
  }

  /** Parse the options box. Invalid JSON stops the request; it does not become `{}`. */
  function currentOptions() {
    const written = state.optionsText.trim();
    if (!written) return undefined;
    let parsed;
    try {
      parsed = JSON.parse(written);
    } catch (error) {
      throw new Error(`options is not JSON: ${error.message}`);
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error(
        'options is an object of option groups, e.g. {"structure": {"max_tokens": 60}}'
      );
    }
    return parsed;
  }

  const actions = {
    /** A file was chosen or dropped: analyse it, and store nothing. */
    analyze(file) {
      state.file = file;
      // The previous answer described previous bytes. Clearing it is the difference between
      // "this is what will happen" and "this is what would have happened to another file".
      state.analysis = null;
      state.upload = null;
      state.frontier = null;
      return attempt("analyze", async () => {
        const options = currentOptions();
        const analysis = await client.analyze_ingest({
          file,
          options,
          private: state.isPrivate,
        });
        state.analysis = analysis;
        state.explainFormat = field(analysis, "AnalyzeIngestResponse", "format");
      });
    },

    /** Ingest the analysed bytes. Only reachable once an analysis is on the page. */
    ingest() {
      return attempt("ingest", async () => {
        if (state.analysis === null) {
          throw new Error("nothing has been analysed, so there is nothing to ingest");
        }
        const options = currentOptions();
        const parent = state.parentId.trim();
        if (parent && !/^\d+$/.test(parent)) {
          throw new Error(`parent is a document id; "${parent}" is not one`);
        }
        const upload = await client.upload_file({
          file: state.file,
          options,
          parent_id: parent ? Number(parent) : undefined,
          private: state.isPrivate,
        });
        state.upload = upload;
        state.frontier = null;
        state.watching = field(upload, "FileUploadResponse", "document_id");
      });
    },

    /** One frontier reading, and the poll that follows it if the work is not over. */
    refreshFrontier() {
      return attempt("frontier", async () => {
        if (state.watching === null) {
          throw new Error("this page is not watching an ingestion");
        }
        const frontier = await client.file_frontier({ document_id: state.watching });
        state.frontier = frontier;
        const next = ingestionState({ upload: state.upload, frontier });
        state.polling = !TERMINAL.includes(next.status);
        if (state.polling) schedule(() => void actions.refreshFrontier(), FRONTIER_POLL_MS);
      });
    },

    /** Watch an ingestion this page did not start. `#/ingest?document=42`. */
    watch(documentId) {
      state.watching = documentId;
      state.upload = null;
      state.frontier = null;
      return actions.refreshFrontier();
    },

    explain() {
      return attempt("explain", async () => {
        const format = state.explainFormat.trim();
        if (!format) {
          throw new Error(
            "name the format probe would report — pdf, docx, xlsx, text — not a filename"
          );
        }
        state.explain = await client.explain_ingest({
          request: { format, options: currentOptions() ?? null, patterns: null },
        });
      });
    },

    ingestText() {
      return attempt("text", async () => {
        const content = state.textContent.trim();
        if (!content) throw new Error("there is no content to ingest");
        if (!state.textUsetype) throw new Error("choose an entry point");
        state.textResult = await client.ingest_content({
          request: {
            content: state.textContent,
            usetype: state.textUsetype,
            title: state.textTitle.trim() || null,
            options: currentOptions() ?? null,
          },
        });
      });
    },

    // THE FOUR SETTERS BELOW DO NOT REDRAW THE PANEL THEY WERE TYPED INTO, and that is not
    // an optimisation. `draw()` rebuilds a panel's elements, so a keystroke handler that
    // called it would replace the field mid-word and take the caret with it. Each one
    // redraws exactly what its value changed somewhere else.

    /** The options box changed. Any analysis on the page described the OLD options. */
    setOptions(written) {
      state.optionsText = written;
      state.analysis = null;
      state.optionsError = null;
      if (written.trim()) {
        try {
          currentOptions();
        } catch (error) {
          state.optionsError = error.message;
        }
      }
      showOptionsProblem();
      drawAnalysis();
    },

    setPrivate(value) {
      state.isPrivate = value;
      // `private` narrows deduplication, so an analysis made under the other setting can
      // report a different `already_stored`. Same rule as the options box.
      state.analysis = null;
      drawAnalysis();
    },

    /** Nothing else on the page reads these three, so nothing else has to be redrawn. */
    setParent(value) {
      state.parentId = value;
    },

    setExplainFormat(value) {
      state.explainFormat = value;
    },

    setText(patch) {
      Object.assign(state, patch);
    },

    /** What `POST /ingest` accepts. Read once, on mount. */
    loadPipelines() {
      return attempt("pipelines", async () => {
        state.pipelines = await client.list_registered_pipelines({});
      });
    },

    state,
  };

  // -------------------------------------------------------------------------- the panels

  /** The one element a setter writes into directly; see `showOptionsProblem`. */
  let optionsProblem = null;

  function drawDrop() {
    dropPanel.textContent = "";
    heading(
      dropPanel,
      "Drop a file",
      "POST /ingest/analyze — probe runs and stops. No document, no blob, no queue row."
    );

    const zone = make("div", state.busy === "analyze" ? "upload-drop busy" : "upload-drop");
    const chooser = make("input");
    chooser.type = "file";
    chooser.className = "upload-file";
    chooser.setAttribute("aria-label", "file to ingest");
    chooser.addEventListener("change", () => {
      const [chosen] = chooser.files ?? [];
      if (chosen) void actions.analyze(chosen);
    });
    zone.append(
      make(
        "p",
        null,
        state.file
          ? `${state.file.name} — ${bytes(state.file.size)}`
          : "Drop a file here, or choose one."
      )
    );
    zone.append(chooser);
    // Drag and drop is the same action as the chooser, wired to the browser's events. It is
    // the one part of this view no harness reaches: the DOM the suite runs against has no
    // events, so what is tested is `actions.analyze`, which is what both paths call.
    for (const name of ["dragover", "dragenter"]) {
      zone.addEventListener(name, (event) => {
        event.preventDefault();
        zone.className = "upload-drop over";
      });
    }
    zone.addEventListener("dragleave", () => {
      zone.className = "upload-drop";
    });
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.className = "upload-drop";
      const [dropped] = event.dataTransfer?.files ?? [];
      if (dropped) void actions.analyze(dropped);
    });
    dropPanel.append(zone);

    const form = make("div", "upload-controls");

    const options = make("textarea", "upload-options");
    options.value = state.optionsText;
    options.setAttribute("aria-label", "ingest options");
    options.setAttribute("placeholder", '{"structure": {"max_tokens": 60}}');
    options.addEventListener("input", () => actions.setOptions(options.value));
    const optionsLabel = make("label", "upload-field");
    optionsLabel.append(make("span", "dim", "options"), options);
    form.append(optionsLabel);

    const parent = make("input", "upload-parent");
    parent.value = state.parentId;
    parent.setAttribute("aria-label", "parent document id");
    parent.setAttribute("placeholder", "parent id (optional)");
    parent.addEventListener("input", () => actions.setParent(parent.value));
    const parentLabel = make("label", "upload-field");
    parentLabel.append(make("span", "dim", "parent"), parent);
    form.append(parentLabel);

    const isPrivate = make("input");
    isPrivate.type = "checkbox";
    isPrivate.checked = state.isPrivate;
    isPrivate.addEventListener("change", () => actions.setPrivate(isPrivate.checked));
    const privateLabel = make("label", "upload-field upload-check");
    privateLabel.append(isPrivate, make("span", null, "private"));
    form.append(privateLabel);
    dropPanel.append(form);

    dropPanel.append(
      make(
        "p",
        "dim note",
        "An upload is readable by every principal holding a token unless it lands inside a " +
          "governed subtree: that is the documented default, not an oversight. `private` " +
          "makes this node its own access-control root, and narrows deduplication to nodes " +
          "you hold a grant on."
      )
    );

    // Held rather than recreated, so `setOptions` can put the parse error on the page
    // without rebuilding the box that is being typed into.
    optionsProblem = make("p", null, "");
    dropPanel.append(optionsProblem);
    showOptionsProblem();

    if (state.errors.analyze) dropPanel.append(problem(state.errors.analyze));
    if (state.busy === "analyze") dropPanel.append(make("p", "dim", "analysing…"));
  }

  /** Show, or stop showing, whatever is wrong with what is in the options box. */
  function showOptionsProblem() {
    if (optionsProblem === null) return;
    optionsProblem.className = state.optionsError ? "bad upload-problem" : "";
    optionsProblem.textContent = state.optionsError ?? "";
  }

  function drawAnalysis() {
    analysisPanel.textContent = "";
    if (state.analysis === null) {
      if (state.file && !state.busy) {
        heading(analysisPanel, "What would happen");
        analysisPanel.append(
          make(
            "p",
            "dim",
            "The request changed, so the previous analysis no longer describes it. Choose " +
              "the file again to analyse it under these options."
          )
        );
      }
      return;
    }

    const analysis = state.analysis;
    const said = (path) => field(analysis, "AnalyzeIngestResponse", path);
    const file = said("file");
    const measured = (path) => field(file, "AnalyzedFile", path);
    const detected = measured("detected_mime");
    const agrees = measured("mime_agrees");
    const size = measured("byte_size");
    const stored = said("already_stored");
    const failure = said("probe_failed");
    const plan = said("plan");

    heading(analysisPanel, "What would happen", "Nothing below has been stored.");
    analysisPanel.append(
      facts([
        ["File", `${measured("filename")} — ${bytes(size)}`],
        [
          "Format",
          said("format"),
          // `detected_by` is null when nothing recognised the bytes, and the filename's
          // extension is then the only evidence there is. Saying so is the difference
          // between a measurement and a guess presented as one.
          `detected by ${measured("detected_by") ?? "nothing — the extension is the only hint"}`,
        ],
        [
          "Mime type",
          agrees === null
            ? make(
                "span",
                "dim",
                `${detected ?? "unrecognised"} — declared and detected cannot be compared`
              )
            : make(
                "span",
                agrees ? null : "bad",
                agrees
                  ? String(detected)
                  : `the bytes are ${detected ?? "unrecognised"}, the client said ` +
                      `${measured("declared_mime")}`
              ),
        ],
        ["Content hash", make("span", "mono", measured("content_hash"))],
        ["Patterns probe measured", pills(Object.keys(said("patterns")))],
      ])
    );

    if (stored !== null) {
      const where = field(stored, "AlreadyStored", "document_id");
      const settled = field(stored, "AlreadyStored", "settled");
      analysisPanel.append(
        make(
          "div",
          "upload-notice",
          `These bytes are already stored, as document ${where} (${settled}). Ingesting them ` +
            "again resolves to that node and runs no plan at all."
        )
      );
    }

    if (failure !== null) {
      analysisPanel.append(
        make(
          "div",
          "upload-notice bad",
          `probe would FAIL on these bytes, and there is no plan: ` +
            `${field(failure, "ProbeFailure", "error")} — classified ` +
            `${field(failure, "ProbeFailure", "error_type")}. Ingesting stores the bytes and ` +
            "leaves a node whose probe failed the same way."
        )
      );
    } else if (plan !== null) {
      analysisPanel.append(renderPlan(plan));
    }

    const go = make("button", "upload-go");
    go.type = "button";
    go.disabled = state.busy !== null;
    const counts = plan === null ? [] : planSummary(plan);
    const enqueued = counts.find(([outcome]) => outcome === "enqueued");
    go.textContent =
      stored !== null
        ? "Ingest — resolves to the existing node"
        : failure !== null
          ? "Ingest anyway — probe will fail"
          : `Ingest — stores ${bytes(size)}, enqueues ` +
            `${enqueued ? enqueued[1] : 0} task${enqueued && enqueued[1] === 1 ? "" : "s"}`;
    go.addEventListener("click", () => void actions.ingest());
    analysisPanel.append(go);
    if (state.errors.ingest) analysisPanel.append(problem(state.errors.ingest));
    if (state.busy === "ingest") analysisPanel.append(make("p", "dim", "ingesting…"));
  }

  function drawProgress() {
    progressPanel.textContent = "";
    if (state.upload === null && state.watching === null) return;

    heading(progressPanel, "This ingestion");

    // Watching a document this page did not upload, before the first reading arrives. There
    // is no state to report yet and nothing to infer one from, so it says that rather than
    // picking one of the five words on no evidence.
    if (state.upload === null && state.frontier === null) {
      progressPanel.append(
        make("p", "dim", `Watching document ${state.watching}. No reading yet.`)
      );
      const first = make("button", "ghost", "Ask the frontier");
      first.type = "button";
      first.addEventListener("click", () => void actions.refreshFrontier());
      progressPanel.append(first);
      if (state.errors.frontier) progressPanel.append(problem(state.errors.frontier));
      return;
    }

    const status = ingestionState({ upload: state.upload, frontier: state.frontier });

    const banner = make("div", `upload-status upload-status-${status.status}`);
    banner.dataset.status = status.status;
    banner.append(make("strong", null, status.headline));
    if (status.note) banner.append(make("p", "note", status.note));
    progressPanel.append(banner);

    const rows = [];
    if (state.upload !== null) {
      const landed = (path) => field(state.upload, "FileUploadResponse", path);
      const governed = landed("governed");
      rows.push([
        "Document",
        documentLink(landed("document_id")),
        landed("linked_into_parent")
          ? "placed under the parent by a `contains` link, not by parentage"
          : null,
      ]);
      rows.push([
        "Stored",
        `${landed("filename")} — ${bytes(landed("byte_size"))}, ` +
          `${landed("detected_mime") ?? "unrecognised bytes"}`,
      ]);
      rows.push([
        "Governed",
        make(
          "span",
          null,
          governed
            ? "yes — an access-control root sits at or above it"
            : "no — readable and writable by anyone holding a token"
        ),
        governed ? null : "the documented default; GET /access/audit asks it of the corpus",
      ]);
      rows.push([
        "Attempts so far",
        pills(
          landed("attempts").map((attempt) => {
            const task = field(attempt, "AttemptRecord", "task");
            const status = field(attempt, "AttemptRecord", "status");
            const error = field(attempt, "AttemptRecord", "error");
            return `${task}: ${status}${error === null ? "" : ` — ${error}`}`;
          })
        ),
      ]);
    } else {
      rows.push([
        "Document",
        documentLink(state.watching),
        "watched, not uploaded by this page",
      ]);
    }

    if (state.frontier !== null) {
      const frontier = state.frontier;
      for (const [label, path] of [
        ["Nodes settled", "nodes_settled"],
        ["Nodes in flight", "nodes_in_flight"],
        ["Nodes failed", "nodes_failed"],
        ["Nodes total", "nodes_total"],
        ["Tasks unfinished", "tasks_unfinished"],
      ]) {
        rows.push([label, String(field(frontier, "IngestFrontierResponse", path))]);
      }
      rows.push([
        "Root node",
        field(frontier, "IngestFrontierResponse", "settled"),
        "the root's own lifecycle state, which is not the subtree's",
      ]);
    }
    progressPanel.append(facts(rows));

    progressPanel.append(
      make(
        "p",
        "dim note",
        state.polling
          ? `Polling GET /ingest/file/{id}/frontier every ${FRONTIER_POLL_MS / 1000}s; it stops ` +
            "when nothing more will happen. Counts, never a percentage: the tasks below the " +
            "frontier do not exist yet, so a percentage would move backward."
          : "Not polling. Counts, never a percentage: the tasks below the frontier do not " +
            "exist yet, so a percentage would move backward."
      )
    );

    const again = make("button", "ghost");
    again.type = "button";
    again.textContent = state.frontier === null ? "Ask the frontier" : "Ask again";
    again.addEventListener("click", () => void actions.refreshFrontier());
    progressPanel.append(again);
    if (state.errors.frontier) progressPanel.append(problem(state.errors.frontier));
  }

  function drawExplain() {
    explainPanel.textContent = "";
    heading(
      explainPanel,
      "Explain a format",
      "POST /ingest/explain — the same plan, for a format and some options, with no file at all."
    );

    const form = make("div", "upload-controls");
    const format = make("input", "upload-format");
    format.value = state.explainFormat;
    format.setAttribute("aria-label", "format");
    format.setAttribute("placeholder", "pdf");
    format.addEventListener("input", () => actions.setExplainFormat(format.value));
    const label = make("label", "upload-field");
    label.append(make("span", "dim", "format"), format);
    form.append(label);

    const go = make("button");
    go.type = "button";
    go.textContent = "Explain";
    go.addEventListener("click", () => void actions.explain());
    form.append(go);
    explainPanel.append(form);
    explainPanel.append(
      make(
        "p",
        "dim note",
        "Options come from the box above, and they are resolved exactly as an upload resolves " +
          "them — an option that does not exist is a 400 here for the same reason it is one " +
          "there. No patterns are supplied, so a format with a prober comes back with " +
          "conditional rows rather than a guess."
      )
    );

    if (state.errors.explain) explainPanel.append(problem(state.errors.explain));
    if (state.explain !== null) explainPanel.append(renderPlan(state.explain));
  }

  function drawText() {
    textPanel.textContent = "";
    heading(
      textPanel,
      "Ingest text",
      "POST /ingest — content as a string, through one of the entry points below. This one is " +
        "SYNCHRONOUS: it drains the document's tasks before it answers, so there is no " +
        "frontier to watch."
    );

    const table = make("table", "upload-pipelines");
    const head = make("tr");
    for (const column of ["Entry point", "Content is", "What it does"]) {
      head.append(make("th", null, column));
    }
    table.append(head);
    for (const pipeline of state.pipelines) {
      const line = make("tr");
      const name = field(pipeline, "PipelineInfo", "name");
      line.append(make("td", "mono", name));
      line.append(make("td", "mono", field(pipeline, "PipelineInfo", "source")));
      const what = make("td");
      what.append(make("span", null, field(pipeline, "PipelineInfo", "description")));
      const tuned = Object.keys(field(pipeline, "PipelineInfo", "options"));
      if (tuned.length) {
        what.append(make("p", "dim note", `tunes: ${tuned.join(", ")}`));
      }
      what.append(
        make("p", "dim note", "everything else is decided from what probe measured")
      );
      line.append(what);
      table.append(line);
    }
    textPanel.append(table);

    const form = make("div", "upload-controls");
    const content = make("textarea", "upload-text");
    content.value = state.textContent;
    content.setAttribute("aria-label", "content");
    content.setAttribute("placeholder", "text to ingest");
    content.addEventListener("input", () => actions.setText({ textContent: content.value }));
    const contentLabel = make("label", "upload-field upload-wide");
    contentLabel.append(make("span", "dim", "content"), content);
    form.append(contentLabel);

    const usetype = make("select", "upload-usetype");
    usetype.setAttribute("aria-label", "entry point");
    for (const pipeline of state.pipelines) {
      const name = field(pipeline, "PipelineInfo", "name");
      const option = make("option", null, name);
      option.value = name;
      if (name === state.textUsetype) option.selected = true;
      usetype.append(option);
    }
    usetype.addEventListener("change", () => actions.setText({ textUsetype: usetype.value }));
    const usetypeLabel = make("label", "upload-field");
    usetypeLabel.append(make("span", "dim", "entry point"), usetype);
    form.append(usetypeLabel);

    const title = make("input", "upload-title");
    title.value = state.textTitle;
    title.setAttribute("aria-label", "title");
    title.setAttribute("placeholder", "title (optional)");
    title.addEventListener("input", () => actions.setText({ textTitle: title.value }));
    const titleLabel = make("label", "upload-field");
    titleLabel.append(make("span", "dim", "title"), title);
    form.append(titleLabel);

    const go = make("button");
    go.type = "button";
    go.textContent = "Ingest text";
    go.addEventListener("click", () => void actions.ingestText());
    form.append(go);
    textPanel.append(form);

    if (state.errors.pipelines) textPanel.append(problem(state.errors.pipelines));
    if (state.errors.text) textPanel.append(problem(state.errors.text));
    if (state.busy === "text") textPanel.append(make("p", "dim", "ingesting, and waiting…"));
    if (state.textResult !== null) {
      const made = (path) => field(state.textResult, "IngestResponse", path);
      textPanel.append(
        facts([
          [
            "Document",
            String(made("source_document_id")),
            made("was_existing")
              ? `already here as document ${made("existing_document_id")}; nothing was ingested`
              : "settled: this call drained the document's tasks before it answered",
          ],
          ["Title", made("title")],
          ["Entry point", made("usetype")],
          ["Tree depth", String(made("tree_depth"))],
          [
            "Governed",
            made("governed") ? "yes" : "no — readable and writable by anyone holding a token",
          ],
        ])
      );
    }
  }

  function draw() {
    drawDrop();
    drawAnalysis();
    drawProgress();
    drawExplain();
    drawText();
  }

  /**
   * The default tick: run the work later, unless this page has left the document.
   *
   * A view has NO UNMOUNT HOOK — the shell mounts by clearing `main` and appending — so a
   * poll started here would otherwise outlive the page that started it and keep calling the
   * appliance from a view nobody is looking at. `isConnected` goes false the instant the
   * shell mounts something else, and it is the only teardown signal there is. Reported to
   * the integrator rather than fixed here: a lifecycle hook belongs to the shell, and
   * `shell/shell.js` is not this lane's file.
   */
  function whileMounted(work, ms) {
    setTimeout(() => {
      if (!page.isConnected) return;
      work();
    }, ms);
  }

  draw();
  return { element: page, actions, state, draw };
}

async function component(context) {
  linkStyles();
  const mounted = mountUpload(context);
  await mounted.actions.loadPipelines();
  // `#/ingest?document=42` watches an ingestion this page did not start, which is how
  // another view hands one over without either of them knowing about the other.
  const watching = context.params.document;
  if (watching !== undefined) {
    if (!/^\d+$/.test(watching)) {
      throw new Error(`?document= takes a document id; "${watching}" is not one`);
    }
    await mounted.actions.watch(Number(watching));
  }
  return mounted.element;
}

/**
 * Link this view's stylesheet from this view's module.
 *
 * `shell/shell.css` states the rule and the reason: a second `<link>` in `index.html` would
 * put five F3 worktrees back in one file, which is the conflict IC-7 exists to remove. The
 * href is resolved against `import.meta.url` rather than against the page, because the page
 * is `/app/index.html` and this module is `/app/views/upload.js` — a relative string would be
 * resolved from the wrong one of those two.
 *
 * Called from `component` rather than at module scope, and that is not a style preference:
 * `shell/manifest.js` imports this module, so module-scope work here would run inside every
 * harness that imports the shell — including `tests/test_web_views.py`, whose DOM stub models
 * a document with no `<head>` — and would take the shell's own two tests down at the merge.
 *
 * The flag rather than a lookup, because mounting this view a second time must not stack a
 * second copy of the same stylesheet. Steps 27 and 30 landed the identical shape; this is one
 * pattern for one problem rather than three near-misses.
 */
let linked = false;

function linkStyles() {
  if (linked) return;
  linked = true;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = new URL("./upload.css", import.meta.url).href;
  document.head.append(link);
}

registerView({
  id: "ingest",
  title: "Ingest",
  path: "/ingest",
  component,
  tags: ["ingest"],
});
