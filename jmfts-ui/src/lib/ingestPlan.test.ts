import { describe, expect, it } from "vitest";
import type { AttemptRecord, ExplainedTaskResponse, ExplainIngestResponse } from "@/api";
import {
  attemptsFrom,
  countRange,
  enqueuedCount,
  ingestionState,
  rungs,
  tally,
  TERMINAL,
} from "./ingestPlan";

function task(over: Partial<ExplainedTaskResponse>): ExplainedTaskResponse {
  return {
    task: "probe",
    outcome: "enqueued",
    after: [],
    requires: [],
    forbids: [],
    params: {},
    multiplicity: { low: 1, high: 1 },
    writes: { low: 0, high: 0 },
    scope: "@root",
    ...over,
  };
}

function plan(tasks: ExplainedTaskResponse[]): ExplainIngestResponse {
  return {
    format: "pdf",
    prober_available: true,
    patterns_known: true,
    patterns_source: "probed",
    patterns_ignored: [],
    options: {},
    tasks,
    cost: {
      tasks: { low: 0, high: 0 },
      nodes: { low: 0, high: 0 },
      by_cost_class: {},
      unmeasured: [],
      not_counted: [],
    },
  };
}

function attempt(over: Partial<AttemptRecord>): AttemptRecord {
  return {
    task: "probe",
    status: "completed",
    scope_document_id: 7,
    param_fingerprint: "x",
    ...over,
  };
}

const PLAN = plan([
  task({ task: "probe" }),
  task({
    task: "extract:text",
    write_mode: "self",
    writes: { low: 1, high: null },
    writes_usetype: "section",
    writes_basis: "one per heading",
  }),
  task({ task: "ocr", outcome: "not_applicable", reason: "has_text_layer is true" }),
  task({ task: "profile:sheet", scope: "@children_of(structure:sheets):sheet" }),
  task({
    task: "embed:chunk",
    outcome: "impossible",
    scope: "@children_of(chunk):chunk",
    reason: "no rule writes chunks for pdf",
  }),
]);

describe("rungs", () => {
  it("forecast each row from the plan alone before anything ran", () => {
    const rows = rungs(PLAN, [], null);
    expect(rows.map((r) => [r.task, r.state])).toEqual([
      ["probe", "waiting"],
      ["extract:text", "waiting"],
      ["ocr", "not_applicable"],
      ["profile:sheet", "on_children"],
      ["embed:chunk", "not_applicable"],
    ]);
    expect(rows[2].reason).toBe("has_text_layer is true");
    expect(rows[1].expects).toBe("1 or more section — one per heading");
  });

  it("take each root task's state from its latest attempt", () => {
    const rows = rungs(
      PLAN,
      [
        attempt({ task: "probe", status: "failed", error: "first", attempt: 1 }),
        attempt({ task: "probe", status: "completed", attempt: 2 }),
        attempt({
          task: "extract:text",
          status: "failed",
          error: "ValueError: no text layer",
          error_type: "permanent",
          produced: null,
        }),
      ],
      7
    );
    expect(rows[0]).toMatchObject({ state: "done", attempt: 2, error: null });
    expect(rows[1]).toMatchObject({
      state: "failed",
      error: "ValueError: no text layer",
      errorType: "permanent",
    });
  });

  it("report what an attempt produced", () => {
    const rows = rungs(
      PLAN,
      [attempt({ task: "extract:text", produced: { node_count: 12, child_ids: [] } })],
      7
    );
    expect(rows[1].produced).toBe(12);
  });

  it("ignore attempts scoped to another node", () => {
    const rows = rungs(PLAN, [attempt({ task: "probe", scope_document_id: 99 })], 7);
    expect(rows[0].state).toBe("waiting");
  });

  it("append root attempts no plan row names, marked unplanned", () => {
    const rows = rungs(PLAN, [attempt({ task: "rollup:settle", status: "running" })], 7);
    expect(rows.at(-1)).toMatchObject({ task: "rollup:settle", state: "running", unplanned: true });
  });

  it("are the log alone when there is no plan", () => {
    const rows = rungs(null, [attempt({ task: "probe", status: "pending" })], 7);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ task: "probe", state: "pending", unplanned: false });
  });

  it("refuse an outcome or status the contract does not declare", () => {
    expect(() => rungs(plan([task({ outcome: "maybe" })]), [], null)).toThrow(/no word for/);
    expect(() =>
      rungs(null, [attempt({ status: "lost" as AttemptRecord["status"] })], 7)
    ).toThrow(/does not declare/);
  });

  it("tally by state, failures first", () => {
    expect(tally(rungs(PLAN, [], null))).toEqual([
      ["waiting", 2],
      ["on_children", 1],
      ["not_applicable", 2],
    ]);
  });
});

describe("attemptsFrom", () => {
  it("reads the attempts row, absent meaning nothing ran", () => {
    expect(attemptsFrom(null)).toEqual([]);
    expect(attemptsFrom({ matched: {} })).toEqual([]);
    expect(attemptsFrom({ attempts: [attempt({})] })).toHaveLength(1);
    expect(() => attemptsFrom({ attempts: { probe: 1 } })).toThrow(/not a list/);
  });
});

describe("counts", () => {
  it("print a CountResponse", () => {
    expect(countRange({ low: 3, high: 3 })).toBe("3");
    expect(countRange({ low: 0, high: 4 })).toBe("0–4");
    expect(countRange({ low: 2, high: null })).toBe("2 or more");
    expect(enqueuedCount(PLAN)).toBe(3);
  });
});

describe("ingestionState", () => {
  const frontier = (over: object) => ({
    document_id: 7,
    settled: "in_flight",
    nodes_settled: 0,
    nodes_in_flight: 1,
    nodes_failed: 0,
    nodes_total: 1,
    tasks_unfinished: 1,
    ...over,
  });

  it("is accepted with no frontier, and says when the bytes were already there", () => {
    expect(ingestionState(null).status).toBe("accepted");
    expect(ingestionState(null, true).headline).toMatch(/already here/);
  });

  it("walks a draining queue to settled", () => {
    expect(ingestionState(frontier({})).status).toBe("in_flight");
    // The workbook case: the root settled while children drain is not done.
    expect(
      ingestionState(frontier({ settled: "settled", nodes_in_flight: 3, tasks_unfinished: 4 }))
        .status
    ).toBe("in_flight");
    const done = ingestionState(
      frontier({ settled: "settled", nodes_settled: 4, nodes_in_flight: 0, nodes_total: 4, tasks_unfinished: 0 })
    );
    expect(done.status).toBe("settled");
    expect(TERMINAL.has(done.status)).toBe(true);
  });

  it("reports a failed node before anything else", () => {
    const failed = ingestionState(frontier({ settled: "settled", nodes_failed: 1, tasks_unfinished: 0 }));
    expect(failed.status).toBe("failed");
    expect(TERMINAL.has(failed.status)).toBe(true);
  });

  it("names an empty queue with nodes in flight as stalled, and keeps polling", () => {
    const stalled = ingestionState(frontier({ tasks_unfinished: 0 }));
    expect(stalled.status).toBe("stalled");
    expect(stalled.note).toMatch(/jmfts-worker/);
    expect(TERMINAL.has(stalled.status)).toBe(false);
  });
});
