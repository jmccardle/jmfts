// What a plan says will happen, and what the attempt log says did — reduced to one row per rung.
//
// Three answers meet on the Ingest page and this module is the only place they are combined:
//
//   `POST /ingest/analyze` (or `/ingest/explain`)  — the plan: every task, its outcome, and the
//                                                   sentence that explains a row that will not run
//   `GET /documents/{id}/evidence` → `attempts`     — the root node's attempt log, newest last
//   `GET /ingest/file/{id}/frontier`               — counts over the whole subtree
//
// The attempt log is the ROOT node's. A task scoped `@children_of(...)` writes its attempts on
// the children it runs on, so the root's log cannot say how those went, and this module does
// not pretend it can: such a row is `on_children`, and the frontier's counts are what describes
// it. Asking every child for its log would be a call per sheet; the frontier is one call.
//
// Nothing here invents a state. A plan row with no attempt yet is `waiting` — the plan said it
// would be queued and the log does not show it yet — which is different from `pending` (the
// queue row exists) and from `not_applicable` (the plan said it would never run, and why).

import type {
  AttemptRecord,
  ExplainedTaskResponse,
  ExplainIngestResponse,
  IngestFrontierResponse,
} from "@/api";

/** Every outcome `ExplainedTaskResponse.outcome` can hold, glossed. Keys are the contract's. */
export const OUTCOMES: Record<string, string> = {
  enqueued: "a queue row will be written",
  skipped: "the task runs and records that it did nothing",
  deferred: "the condition holds and no handler is registered, so nothing is queued",
  not_applicable: "its condition does not hold for these bytes",
  impossible: "its condition cannot hold for this format, whatever the bytes are",
  conditional: "undecided: nobody has measured the patterns that decide it",
};

/** Outcomes under which the plan says the task will never run on this ingestion. */
const WILL_NOT_RUN = new Set(["deferred", "not_applicable", "impossible"]);

/** Where a plan's patterns came from. `patterns_source` is load-bearing, not metadata. */
export const PATTERN_SOURCES: Record<string, string> = {
  probed: "probe ran over these bytes and measured them",
  supplied: "supplied by the caller; a hypothesis, not a measurement",
  no_prober: "this format has no prober, so the empty set is a fact and not a guess",
  unknown: "nobody measured them, so the undecidable rows are conditional",
};

/** A rung's state, as this page names it. */
export type RungState =
  | "waiting" // planned to run; the log does not show it yet
  | "pending" // queued
  | "running"
  | "done" // completed
  | "skipped" // ran and recorded that it did nothing
  | "failed"
  | "not_applicable" // the plan says it will not run, and `reason` says why
  | "on_children"; // runs on child nodes, whose logs are theirs

export interface Rung {
  task: string;
  /** The plan's outcome, or null for a log entry no plan row names. */
  outcome: string | null;
  state: RungState;
  /** The plan's reason, verbatim. The same sentence the run writes into the log. */
  reason: string | null;
  scope: string | null;
  writeMode: string | null;
  /** What the plan expects this task to write, in the atom's own words, or null. */
  expects: string | null;
  /** How many children the latest attempt reports it produced, or null if it said nothing. */
  produced: number | null;
  /** The failed attempt's error text, verbatim, with its classification. */
  error: string | null;
  errorType: string | null;
  /** Which attempt this is, 1-based, when the log has one. */
  attempt: number | null;
  /** True for a log entry the plan did not list — rollups and settling run outside Part 4. */
  unplanned: boolean;
}

const ROOT_SCOPE = "@root";

/** A `CountResponse` as `3`, `0–4` or `2 or more`. */
export function countRange(count: { low: number; high: number | null }): string {
  if (count.high === null) return `${count.low} or more`;
  if (count.high === count.low) return String(count.low);
  return `${count.low}–${count.high}`;
}

/** What a plan row expects to write: `0–4 chunk`, with the bound's own sentence when given. */
export function expectedWrites(task: ExplainedTaskResponse): string | null {
  const noun = task.writes_usetype ?? null;
  if (noun === null) return null;
  const range = countRange(task.writes);
  return task.writes_basis ? `${range} ${noun} — ${task.writes_basis}` : `${range} ${noun}`;
}

/** Read the root's attempt log out of an evidence map. Absent means nothing has run. */
export function attemptsFrom(evidence: Record<string, unknown> | null): AttemptRecord[] {
  if (evidence === null || !("attempts" in evidence)) return [];
  const log = evidence.attempts;
  if (!Array.isArray(log)) {
    throw new Error(
      `the evidence row "attempts" is ${log === null ? "null" : typeof log}, not a list; ` +
        "the appliance and this page disagree about its shape"
    );
  }
  return log as AttemptRecord[];
}

/** The latest attempt per task on one node. The log is newest last. */
function latestByTask(attempts: AttemptRecord[], rootId: number | null): Map<string, AttemptRecord> {
  const latest = new Map<string, AttemptRecord>();
  for (const entry of attempts) {
    if (rootId !== null && entry.scope_document_id !== rootId) continue;
    latest.set(entry.task, entry);
  }
  return latest;
}

const STATUS_STATE: Record<AttemptRecord["status"], RungState> = {
  pending: "pending",
  running: "running",
  completed: "done",
  skipped: "skipped",
  failed: "failed",
};

function producedCount(entry: AttemptRecord): number | null {
  const produced = entry.produced;
  if (!produced || typeof produced !== "object") return null;
  const n = (produced as Record<string, unknown>).node_count;
  return typeof n === "number" ? n : null;
}

function fromAttempt(base: Omit<Rung, "state" | "produced" | "error" | "errorType" | "attempt">, entry: AttemptRecord): Rung {
  return {
    ...base,
    state: STATUS_STATE[entry.status] ?? unknownStatus(entry.status),
    produced: producedCount(entry),
    error: entry.error ?? null,
    errorType: entry.error_type ?? null,
    attempt: entry.attempt ?? null,
  };
}

function unknownStatus(status: string): never {
  throw new Error(
    `the attempt log reports status "${status}", which AttemptRecord does not declare; ` +
      "this appliance is newer than this page"
  );
}

/**
 * One row per rung: the plan's tasks in plan order, then any root attempt the plan did not list.
 *
 * `plan` may be null — a document this page did not analyse, watched by id — and then the rows
 * are the log alone. `attempts` may be empty — nothing uploaded yet — and then the rows are the
 * forecast alone, each in the state the plan predicts.
 */
export function rungs(
  plan: ExplainIngestResponse | null,
  attempts: AttemptRecord[],
  rootId: number | null
): Rung[] {
  const latest = latestByTask(attempts, rootId);
  const seen = new Set<string>();
  const rows: Rung[] = [];

  for (const task of plan?.tasks ?? []) {
    if (!(task.outcome in OUTCOMES)) {
      throw new Error(
        `the plan reports outcome "${task.outcome}", which this page has no word for; ` +
          "this appliance is newer than this page"
      );
    }
    const scope = task.scope ?? null;
    const base = {
      task: task.task,
      outcome: task.outcome,
      reason: task.reason ?? null,
      scope,
      writeMode: task.write_mode ?? null,
      expects: expectedWrites(task),
      unplanned: false,
    };
    const rootScoped = scope === null || scope === ROOT_SCOPE;
    const entry = rootScoped ? latest.get(task.task) : undefined;
    if (rootScoped) seen.add(task.task);

    if (entry) {
      rows.push(fromAttempt(base, entry));
      continue;
    }
    const state: RungState = !rootScoped
      ? WILL_NOT_RUN.has(task.outcome)
        ? "not_applicable"
        : "on_children"
      : WILL_NOT_RUN.has(task.outcome)
        ? "not_applicable"
        : "waiting";
    rows.push({ ...base, state, produced: null, error: null, errorType: null, attempt: null });
  }

  for (const [task, entry] of latest) {
    if (seen.has(task)) continue;
    rows.push(
      fromAttempt(
        {
          task,
          outcome: null,
          reason: null,
          scope: null,
          writeMode: entry.write_mode ?? null,
          expects: null,
          unplanned: plan !== null,
        },
        entry
      )
    );
  }
  return rows;
}

/** Rows counted by state, in a fixed order, omitting states nothing is in. */
export function tally(rows: Rung[]): Array<[RungState, number]> {
  const order: RungState[] = [
    "failed",
    "running",
    "pending",
    "waiting",
    "on_children",
    "done",
    "skipped",
    "not_applicable",
  ];
  const counts = new Map<RungState, number>();
  for (const row of rows) counts.set(row.state, (counts.get(row.state) ?? 0) + 1);
  return order.filter((s) => counts.has(s)).map((s) => [s, counts.get(s)!]);
}

/** How many queue rows the plan writes at the root and below, as the contract folds it. */
export function enqueuedCount(plan: ExplainIngestResponse): number {
  return plan.tasks.filter((t) => t.outcome === "enqueued").length;
}

// ------------------------------------------------------------------- the ingestion's state

export type IngestionStatus = "accepted" | "in_flight" | "stalled" | "settled" | "failed";

export interface Ingestion {
  status: IngestionStatus;
  headline: string;
  note: string;
}

/** The states nothing more will happen from, so polling stops. */
export const TERMINAL: ReadonlySet<IngestionStatus> = new Set(["settled", "failed"]);

/**
 * Which state this page is entitled to show. The same four rules `views/upload.js` applies in
 * jmfts-web, in the same order, because they are two renderings of one contract:
 *
 * 1. No frontier is not progress — an upload response only says the node exists.
 * 2. A failed node is not a finished one, even when everything else settled.
 * 3. Settled needs the root settled, nothing in flight under it, and no unfinished tasks.
 * 4. In flight with an empty queue is its own answer: the frontier is waiting on something
 *    other than the queue, which on a small appliance is almost always a stopped worker.
 */
export function ingestionState(
  frontier: IngestFrontierResponse | null,
  wasExisting = false
): Ingestion {
  if (frontier === null) {
    return wasExisting
      ? {
          status: "accepted",
          headline: "accepted — these bytes were already here",
          note:
            "Nothing was created and nothing was enqueued: the upload resolved to the node " +
            "that already held these bytes.",
        }
      : {
          status: "accepted",
          headline: "accepted — stored, and probe is queued",
          note: "The node exists and the bytes are stored. Nothing has been extracted yet.",
        };
  }
  const f = frontier;
  const counted =
    `${f.nodes_settled} settled, ${f.nodes_in_flight} in flight, ` +
    `${f.nodes_failed} failed, of ${f.nodes_total}`;

  if (f.nodes_failed > 0 || f.settled === "failed") {
    return {
      status: "failed",
      headline: `failed — ${counted}`,
      note: "A failed node keeps its attempt log; the failed rung's error is shown below verbatim.",
    };
  }
  if (f.settled === "settled" && f.nodes_in_flight === 0 && f.tasks_unfinished === 0) {
    return {
      status: "settled",
      headline: `settled — ${counted}`,
      note: "Every node is settled, so retrieval can reach all of them.",
    };
  }
  if (f.nodes_in_flight > 0 && f.tasks_unfinished === 0) {
    return {
      status: "stalled",
      headline: `in flight, with an empty queue — ${counted}`,
      note:
        "The queue owes no work and nodes are still in flight, so the frontier is waiting on " +
        "something other than the queue. A worker that is not running looks exactly like " +
        "this: check that jmfts-worker is up.",
    };
  }
  return {
    status: "in_flight",
    headline: `in flight — ${counted}`,
    note:
      `${f.tasks_unfinished} queued task${f.tasks_unfinished === 1 ? "" : "s"} still owe work. ` +
      "Nodes are invisible to search until they settle.",
  };
}
