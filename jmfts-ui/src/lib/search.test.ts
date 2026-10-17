import { describe, expect, it } from "vitest";
import { OP_IDS } from "@jmfts/client/operations.js";
import type { CapabilitiesResponse, SearchResultItem } from "@jmfts/client/verbs.js";
import {
  METHODS,
  SYNTHESIS_OP,
  SearchAskError,
  argsFor,
  askFromQuery,
  cannotAnswer,
  defaultMethods,
  explainApplied,
  explainRouting,
  hybridCaveats,
  isTruncated,
  normaliseHit,
  queryFromAsk,
  snippetOf,
  synthesisArgs,
  trailOf,
  unknownAncestors,
  type SearchAsk,
} from "./search";

const ask = (over: Partial<SearchAsk> = {}): SearchAsk => ({
  query: "late interaction",
  limit: 10,
  usetype: "",
  parentId: null,
  context: "",
  methods: null,
  getMethod: "hybrid",
  includeExcluded: false,
  ...over,
});

function caps(over: {
  corpus?: Partial<NonNullable<CapabilitiesResponse["corpus"]>> | null;
  embeds?: boolean;
  defaults?: string[];
} = {}): CapabilitiesResponse {
  const corpus =
    over.corpus === null
      ? null
      : {
          documents: 40,
          documents_with_vectors: 40,
          documents_with_token_vectors: 30,
          bm25_indexes: ["default"],
          ...over.corpus,
        };
  return {
    version: "0.6.0",
    extras: [],
    embedding: {
      model: "m",
      device: "cpu",
      document_dims: 768,
      token_dims: 256,
      local_model_available: over.embeds ?? true,
      can_embed: true,
    },
    retrieval: {
      methods: ["vector", "fulltext", "bm25", "maxsim"],
      default_methods: over.defaults ?? ["vector", "bm25"],
      default_weights: { vector: 1.0, bm25: 0.6 },
      search_exclude_usetypes: ["entity", "summary"],
      bm25_exclude_usetypes: [],
    },
    ingest: {} as CapabilitiesResponse["ingest"],
    llm: { configured: false, base_url: "", model: "", requires_llm: ["synthesis"] },
    corpus,
  };
}

describe("the column table", () => {
  it("names only operations the generated client has", () => {
    for (const m of METHODS) expect(OP_IDS).toContain(m.opId);
    expect(OP_IDS).toContain(SYNTHESIS_OP);
  });

  it("gives every ranking /search/ operation a column, and synthesis none", () => {
    const searchOps = OP_IDS.filter((id) => id.startsWith("SearchService."));
    const drawn = new Set([...METHODS.map((m) => m.opId), SYNTHESIS_OP]);
    expect(searchOps.filter((id) => !drawn.has(id))).toEqual([]);
    expect(METHODS.map((m) => m.opId)).not.toContain(SYNTHESIS_OP);
  });
});

describe("a search, as a URL", () => {
  it("reads an empty query as the defaults", () => {
    expect(askFromQuery({})).toEqual(ask({ query: "" }));
  });

  it("round-trips, leaving defaults out", () => {
    const full = ask({
      limit: 25,
      usetype: "chunk",
      parentId: 7,
      context: "papers",
      methods: ["bm25", "maxsim"],
      getMethod: "bm25",
      includeExcluded: true,
    });
    expect(askFromQuery(queryFromAsk(full))).toEqual(full);
    expect(queryFromAsk(ask())).toEqual({ q: "late interaction" });
  });

  it("puts named methods in column order", () => {
    expect(askFromQuery({ m: "maxsim,vector" }).methods).toEqual(["vector", "maxsim"]);
  });

  it("takes the first of a repeated parameter, as vue-router hands them", () => {
    expect(askFromQuery({ q: ["one", "two"] }).query).toBe("one");
  });

  it.each([
    [{ limit: "0" }, /1\.\.100/],
    [{ limit: "101" }, /1\.\.100/],
    [{ limit: "ten" }, /whole number/],
    [{ parent: "0" }, /node id/],
    [{ m: "vector,colbert" }, /unknown method/],
    [{ m: "vector,vector" }, /duplicate/],
    [{ get: "auto" }, /GET \/search\//],
  ])("refuses %j rather than repairing it", (query, message) => {
    expect(() => askFromQuery(query)).toThrow(SearchAskError);
    expect(() => askFromQuery(query)).toThrow(message);
  });
});

describe("what each column is sent", () => {
  it("omits what the server defaults, including exclude_types", () => {
    expect(argsFor("vector", ask())).toEqual({ request: { query: "late interaction", limit: 10 } });
  });

  it("sends exclude_types: [] only when asked, and the scope and filter when set", () => {
    expect(
      argsFor("hybrid", ask({ includeExcluded: true, parentId: 3, usetype: "chunk", context: "p" }))
    ).toEqual({
      request: {
        query: "late interaction",
        limit: 10,
        usetype: "chunk",
        parent_id: 3,
        exclude_types: [],
      },
      context: "p",
    });
  });

  it("sends quick its query parameters, and never exclude_types", () => {
    expect(argsFor("quick", ask({ includeExcluded: true, getMethod: "bm25", parentId: 2 }))).toEqual(
      { q: "late interaction", limit: 10, method: "bm25", parent_id: 2 }
    );
  });

  it("builds the synthesis request", () => {
    expect(synthesisArgs(ask({ parentId: 4 }), "auto", 5)).toEqual({
      request: { query: "late interaction", search_method: "auto", top_k: 5, parent_id: 4 },
    });
  });
});

describe("which methods can answer here", () => {
  it("lets every method answer on a full corpus", () => {
    expect(defaultMethods(caps(), "hybrid")).toEqual(METHODS.map((m) => m.id));
  });

  it("says why maxsim, vector and bm25 cannot answer", () => {
    const c = caps({ corpus: { documents_with_token_vectors: 0, documents_with_vectors: 0, bm25_indexes: [] } });
    expect(cannotAnswer("maxsim", c)).toMatch(/token vectors/);
    expect(cannotAnswer("vector", c)).toMatch(/document vector/);
    expect(cannotAnswer("bm25", c)).toMatch(/BM25 index/);
    expect(cannotAnswer("fulltext", c)).toBeNull();
    expect(cannotAnswer("auto", c)).toBeNull();
    expect(cannotAnswer("hybrid", c)).toMatch(/every method it fuses/);
    expect(defaultMethods(c, "hybrid")).toEqual(["fulltext", "auto"]);
  });

  it("knows a query cannot be embedded without the model, whatever the counts", () => {
    const c = caps({ embeds: false });
    expect(cannotAnswer("vector", c)).toMatch(/cannot embed a query/);
    expect(cannotAnswer("maxsim", c)).toMatch(/cannot embed a query/);
    // hybrid_search raises ModelStackNotInstalled (501) for the whole request when a fused
    // method needs the embedder, so it is refused rather than described as degraded.
    expect(cannotAnswer("hybrid", c)).toMatch(/whole request fails with 501/);
    expect(defaultMethods(c, "hybrid")).not.toContain("hybrid");
  });

  it("names the fused methods that contribute nothing when hybrid can still answer", () => {
    const c = caps({ corpus: { documents_with_vectors: 0 } });
    expect(cannotAnswer("hybrid", c)).toBeNull();
    expect(hybridCaveats(c)).toEqual([expect.stringMatching(/^vector contributes nothing/)]);
  });

  it("still lets hybrid answer without the model when nothing it fuses embeds", () => {
    const c = caps({ embeds: false, defaults: ["fulltext", "bm25"] });
    expect(cannotAnswer("hybrid", c)).toBeNull();
  });

  it("judges quick by the method it is asked to run", () => {
    const c = caps({ corpus: { bm25_indexes: [] } });
    expect(cannotAnswer("quick", c, "bm25")).toMatch(/BM25/);
    expect(cannotAnswer("quick", c, "vector")).toBeNull();
  });

  it("says an empty appliance is empty, for every column", () => {
    const c = caps({ corpus: { documents: 0 } });
    for (const m of METHODS) expect(cannotAnswer(m.id, c)).toMatch(/no documents/);
  });

  it("does not read missing corpus counts as 'everything can answer' — nor as nothing", () => {
    const c = caps({ corpus: null });
    expect(cannotAnswer("maxsim", c)).toBeNull();
  });
});

describe("snippets", () => {
  it("strips markdown and keeps a short text whole", () => {
    expect(snippetOf("## Heading\n\n**bold** text")).toEqual({
      text: "Heading bold text",
      total: 17,
      cut: false,
      absent: false,
    });
  });

  it("cuts a long text at a word and says it did", () => {
    const long = "word ".repeat(100);
    const s = snippetOf(long, 50);
    expect(s.cut).toBe(true);
    expect(s.total).toBe(long.trim().length);
    expect(s.text.endsWith("word…")).toBe(true);
    expect(s.text.length).toBeLessThanOrEqual(51);
  });

  it("tells no content from empty content", () => {
    expect(snippetOf(null).absent).toBe(true);
    expect(snippetOf("").absent).toBe(false);
  });
});

const item = (over: Partial<SearchResultItem["document"]> = {}, rest = {}): SearchResultItem => ({
  document: {
    id: 9,
    parent_id: 3,
    title: "Chunk",
    content: "text",
    structured_content: {},
    path: [1, 3],
    depth: 2,
    usetype: "chunk",
    created_at: null,
    updated_at: null,
    content_hash: null,
    ...over,
  },
  score: 0.123456,
  method: "vector",
  ...rest,
});

describe("hits", () => {
  it("normalise across methods", () => {
    const hit = normaliseHit(
      item({}, { via: [{ document_id: 11, score: 0.5 }] }),
      2
    );
    expect(hit).toMatchObject({
      rank: 3,
      id: 9,
      pathIds: [1, 3],
      method: "vector",
      producedBy: null,
      via: [{ id: 11, title: null, score: 0.5 }],
    });
  });

  it("refuse a path that is not a list of node ids", () => {
    expect(() => normaliseHit(item({ path: [1, "x"] }), 0)).toThrow(/not a node id/);
  });

  it("collect each unknown ancestor once", () => {
    const hits = [normaliseHit(item({ path: [1, 3] }), 0), normaliseHit(item({ path: [1, 4] }), 1)];
    expect(unknownAncestors(hits, new Set([3]))).toEqual([1, 4]);
  });

  it("keep an unnamed ancestor in the trail, by id", () => {
    const named = new Map([[1, { title: "Root", usetype: "file" }]]);
    expect(trailOf([1, 3], named)).toEqual([
      { id: 1, title: "Root", usetype: "file" },
      { id: 3, title: null, usetype: null },
    ]);
  });
});

describe("explanations", () => {
  it("say what a fusion ran", () => {
    expect(
      explainApplied({
        limit: 10,
        methods: ["vector", "bm25"],
        weights: { vector: 1.0 },
        exclude_types: ["entity"],
        parent_id: 4,
      })
    ).toEqual(["limit 10", "fused vector × 1 + bm25 × 1 (RRF)", "under node 4", "held out: entity"]);
  });

  it("say when nothing was reported", () => {
    expect(explainApplied(null)[0]).toMatch(/unstated/);
    expect(explainRouting(null)).toEqual(["the router reported no decision"]);
  });

  it("flag a short page", () => {
    expect(isTruncated({ limit: 10, truncated: true })).toBe(true);
    expect(isTruncated({ limit: 10, truncated: null })).toBe(false);
  });

  it("carry the router's reason and signals", () => {
    expect(explainRouting({ method: "bm25", reason: "quoted phrase", signals: { quoted: true } })).toEqual([
      "routed to bm25: quoted phrase",
      "signals quoted=true",
    ]);
  });
});
