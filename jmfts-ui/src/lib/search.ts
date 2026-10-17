// One query, every ranking method, side by side: the pure half of the Search view.
//
// THE BEHAVIOUR IS `jmfts-web/jmfts_web/static/views/search.js`, NOT A NEW DESIGN. That view
// decided which `/search/` operations get a column (seven; `synthesize` is prose, so a panel),
// what each one is sent, which parts of the request live in the URL, and what it refuses before
// sending. This module states the same table and the same refusals in TypeScript so the Vue
// view shows the same facts through the same generated operations. Where the two differ it is
// said at the difference.
//
// Nothing here calls the appliance. Every function takes values and returns values, so the
// vitest suite can hold it without a server.

import type {
  AppliedFilters,
  CapabilitiesResponse,
  DocumentResponse,
  RoutingMetadata,
  SearchResultItem,
  ViewAncestor,
} from "@jmfts/client/verbs.js";
import type { OpId } from "@jmfts/client/operations.js";
import { plainWords } from "./text";

/** The search this page names could not be read, and this says which part. */
export class SearchAskError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SearchAskError";
  }
}

// ----------------------------------------------------------------------------- the columns

/** The `method` values `GET /search/` accepts: the four, plus their fusion. */
export const GET_METHODS = ["hybrid", "vector", "fulltext", "bm25", "maxsim"] as const;
export type GetMethod = (typeof GET_METHODS)[number];

export type MethodId = "vector" | "fulltext" | "bm25" | "maxsim" | "hybrid" | "auto" | "quick";

export interface MethodColumn {
  id: MethodId;
  /** What the header says this method IS: one of the four, their fusion, a router, a transport. */
  kind: "retrieval" | "fusion" | "routed" | "transport";
  title: string;
  path: string;
  opId: OpId;
  what: string;
}

/**
 * One row per `/search/` operation that answers with a ranking, in the order the columns stand.
 * `search.test.ts` holds this against the generated operation table in both directions.
 */
export const METHODS: readonly MethodColumn[] = [
  {
    id: "vector",
    kind: "retrieval",
    title: "vector",
    path: "POST /search/vector",
    opId: "SearchService.vector_search",
    what: "HNSW cosine over the 768-dim document embedding.",
  },
  {
    id: "fulltext",
    kind: "retrieval",
    title: "fulltext",
    path: "POST /search/fulltext",
    opId: "SearchService.fulltext_search",
    what: "PostgreSQL ts_vector, with a trigram fallback.",
  },
  {
    id: "bm25",
    kind: "retrieval",
    title: "bm25",
    path: "POST /search/bm25",
    opId: "SearchService.bm25_search",
    what: "The inverted index, k1/b tuned.",
  },
  {
    id: "maxsim",
    kind: "retrieval",
    title: "maxsim",
    path: "POST /search/maxsim",
    opId: "SearchService.maxsim_search",
    what: "Late interaction over the per-token 256-dim matryoshka vectors.",
  },
  {
    id: "hybrid",
    kind: "fusion",
    title: "hybrid",
    path: "POST /search/hybrid",
    opId: "SearchService.hybrid_search",
    // `methods` is not sent, so the server's own default runs and `applied` reports it.
    what: "Reciprocal Rank Fusion of the default methods, at the tuned weights.",
  },
  {
    id: "auto",
    kind: "routed",
    title: "auto",
    path: "POST /search/auto",
    opId: "SearchService.auto_search",
    what: "A heuristic router reads the query, picks one method and says why.",
  },
  {
    id: "quick",
    kind: "transport",
    title: "quick (GET)",
    path: "GET /search/",
    opId: "SearchService.quick_search",
    what: "The same algorithms in a URL. It takes no exclude_types, so it keeps the default.",
  },
];

/** The `/search/` operation that answers in prose. A panel under the columns, not a column. */
export const SYNTHESIS_OP: OpId = "SearchService.synthesize_search";

const ALL_IDS: readonly MethodId[] = METHODS.map((m) => m.id);

export const methodById = (id: MethodId): MethodColumn => {
  const found = METHODS.find((m) => m.id === id);
  if (!found) throw new SearchAskError(`no column is called ${JSON.stringify(id)}`);
  return found;
};

// ------------------------------------------------------------------- a search, as a URL

/** One search as this page holds it. Named `SearchAsk` so it is not mistaken for the contract. */
export interface SearchAsk {
  query: string;
  limit: number;
  /** Usetype globs, as typed. Empty means no positive filter. */
  usetype: string;
  /** The subtree to search under; null is the whole corpus. */
  parentId: number | null;
  /** A search-context (preset) name; empty is none. */
  context: string;
  /** The columns to run. Null means "the ones that can answer here", decided with capabilities. */
  methods: MethodId[] | null;
  getMethod: GetMethod;
  /** Send `exclude_types: []`: show what the appliance normally holds out. */
  includeExcluded: boolean;
}

export const DEFAULTS = { limit: 10, getMethod: "hybrid" as GetMethod };

/** A route query value, as vue-router hands it over. */
type QueryValue = string | null | undefined | (string | null)[];

const first = (value: QueryValue): string => {
  const one = Array.isArray(value) ? value[0] : value;
  return (one ?? "").trim();
};

function whole(raw: string, name: string): number | null {
  if (raw === "") return null;
  if (!/^-?\d+$/.test(raw)) {
    throw new SearchAskError(`${name} must be a whole number; got ${JSON.stringify(raw)}`);
  }
  return Number(raw);
}

/**
 * The search a route query names, or a refusal saying which part could not be read.
 *
 * Refuses rather than repairs, as `requestFromParams` in search.js does and for its reason: an
 * unknown method "contributes nothing and raises nothing", so a reader would get a side-by-side
 * missing a column and no way to tell that from a column that returned nothing.
 */
export function askFromQuery(query: Record<string, QueryValue>): SearchAsk {
  const limit = whole(first(query.limit), "limit") ?? DEFAULTS.limit;
  if (limit < 1 || limit > 100) {
    throw new SearchAskError(
      `limit is 1..100 (SearchRequest.limit); got ${limit}. The appliance would refuse it, so ` +
        "this page does not send it."
    );
  }
  const parentId = whole(first(query.parent), "parent");
  if (parentId !== null && parentId < 1) {
    throw new SearchAskError(`parent must be a node id; got ${parentId}`);
  }

  let methods: MethodId[] | null = null;
  const m = first(query.m);
  if (m !== "") {
    const named = m.split(",").map((s) => s.trim());
    const unknown = named.filter((id) => !ALL_IDS.includes(id as MethodId));
    if (unknown.length) {
      throw new SearchAskError(
        `unknown method(s) ${JSON.stringify(unknown)}; this page draws ${ALL_IDS.join(", ")}.`
      );
    }
    const duplicates = named.filter((id, i) => named.indexOf(id) !== i);
    if (duplicates.length) throw new SearchAskError(`duplicate method(s) ${JSON.stringify(duplicates)}`);
    methods = METHODS.map((c) => c.id).filter((id) => named.includes(id));
  }

  const getRaw = first(query.get) || DEFAULTS.getMethod;
  if (!(GET_METHODS as readonly string[]).includes(getRaw)) {
    throw new SearchAskError(
      `GET /search/ runs one of ${GET_METHODS.join(", ")}; got ${JSON.stringify(getRaw)}.`
    );
  }

  return {
    query: first(query.q),
    limit,
    usetype: first(query.usetype),
    parentId,
    context: first(query.context),
    methods,
    getMethod: getRaw as GetMethod,
    includeExcluded: first(query.x) === "1",
  };
}

/** The route query an ask round-trips through. Defaults are left out, so links stay short. */
export function queryFromAsk(ask: SearchAsk): Record<string, string> {
  const out: Record<string, string> = {};
  if (ask.query) out.q = ask.query;
  if (ask.limit !== DEFAULTS.limit) out.limit = String(ask.limit);
  if (ask.usetype) out.usetype = ask.usetype;
  if (ask.parentId !== null) out.parent = String(ask.parentId);
  if (ask.context) out.context = ask.context;
  if (ask.methods !== null) out.m = ask.methods.join(",");
  if (ask.getMethod !== DEFAULTS.getMethod) out.get = ask.getMethod;
  if (ask.includeExcluded) out.x = "1";
  return out;
}

// ------------------------------------------------------------------- what each one is sent

/**
 * The arguments one column's operation is called with, in the generated client's shape.
 *
 * Omitted keys take the server's own default. `exclude_types` in particular is OMITTED unless
 * the reader asked for `[]`: sending the default back would be this page deciding what is held
 * out, which is the appliance's decision and is echoed in `applied.exclude_types`.
 */
export function argsFor(id: MethodId, ask: SearchAsk): Record<string, unknown> {
  const context = ask.context ? { context: ask.context } : {};
  if (id === "quick") {
    return {
      q: ask.query,
      limit: ask.limit,
      method: ask.getMethod,
      ...(ask.usetype ? { usetype: ask.usetype } : {}),
      ...(ask.parentId !== null ? { parent_id: ask.parentId } : {}),
      ...context,
    };
  }
  const request: Record<string, unknown> = { query: ask.query, limit: ask.limit };
  if (ask.usetype) request.usetype = ask.usetype;
  if (ask.parentId !== null) request.parent_id = ask.parentId;
  if (ask.includeExcluded) request.exclude_types = [];
  return { request, ...context };
}

/** The synthesis request. One call, made only when asked, because it costs an LLM call. */
export function synthesisArgs(
  ask: SearchAsk,
  searchMethod: string,
  topK: number
): Record<string, unknown> {
  const request: Record<string, unknown> = {
    query: ask.query,
    search_method: searchMethod,
    top_k: topK,
  };
  if (ask.usetype) request.usetype = ask.usetype;
  if (ask.parentId !== null) request.parent_id = ask.parentId;
  return ask.context ? { request, context: ask.context } : { request };
}

// ------------------------------------------------------------ which methods can answer here

/**
 * Why this method cannot return anything on this appliance, whatever the query — or null.
 *
 * Read from `GET /capabilities?corpus=true`, which is the appliance describing itself. These are
 * the facts search.js's `emptyBecause` reads, plus one it does not: `/search` embeds its query
 * in-process (`search_embeds_locally`), so an appliance without the `embed` extra cannot run a
 * vector or MaxSim query at all, even with a runner configured for ingest.
 *
 * A null `corpus` is NOT read as "everything can answer": it returns null here and the view
 * says, once, that the counts are missing.
 */
export function cannotAnswer(
  id: MethodId,
  caps: CapabilitiesResponse,
  getMethod: GetMethod = DEFAULTS.getMethod
): string | null {
  if (id === "quick") return cannotAnswer(getMethod, caps);
  const corpus = caps.corpus ?? null;
  if (corpus && corpus.documents === 0) return "this appliance holds no documents at all";

  const embeds = caps.embedding.local_model_available;
  const own = (m: string): string | null => {
    if ((m === "vector" || m === "maxsim") && !embeds) {
      return (
        "this appliance cannot embed a query: /search embeds in-process and the embed extra " +
        "is not installed here"
      );
    }
    if (!corpus) return null;
    if (m === "vector" && corpus.documents_with_vectors === 0) {
      return "no document here has a document vector";
    }
    if (m === "maxsim" && corpus.documents_with_token_vectors === 0) {
      return "no document here has token vectors";
    }
    if (m === "bm25" && corpus.bm25_indexes.length === 0) return "this appliance has no BM25 index";
    return null;
  };

  if (id === "hybrid") {
    // Fusion answers if ANY method it fuses by default can. When none can, say which and why.
    const fused = caps.retrieval.default_methods;
    // Fusion does not skip a method that cannot embed: SearchRepository.hybrid_search calls
    // vector_search_text / maxsim, which raise ModelStackNotInstalled (HTTP 501) for the whole
    // request. So one embedding method in the fused list is enough to refuse.
    const needsEmbedding = fused.filter((m) => m === "vector" || m === "maxsim");
    if (!embeds && needsEmbedding.length > 0) {
      return (
        `hybrid fuses ${needsEmbedding.join(" and ")} by default, and this appliance cannot ` +
        "embed a query (the embed extra is not installed here), so the whole request fails " +
        "with 501 rather than fusing the rest"
      );
    }
    const reasons = fused.map((m) => [m, own(m)] as const);
    if (fused.length > 0 && reasons.every(([, r]) => r !== null)) {
      return `every method it fuses by default cannot answer here — ${reasons
        .map(([m, r]) => `${m}: ${r}`)
        .join("; ")}`;
    }
    return null;
  }
  if (id === "auto" || id === "fulltext") return null;
  return own(id);
}

/** For hybrid: the fused methods that contribute nothing here, said rather than hidden. */
export function hybridCaveats(caps: CapabilitiesResponse): string[] {
  return caps.retrieval.default_methods
    .map((m) => [m, cannotAnswer(m as MethodId, caps)] as const)
    .filter(([, r]) => r !== null)
    .map(([m, r]) => `${m} contributes nothing: ${r}`);
}

/** The columns run when the URL names none: every one that can answer here. */
export function defaultMethods(caps: CapabilitiesResponse, getMethod: GetMethod): MethodId[] {
  return METHODS.filter((m) => cannotAnswer(m.id, caps, getMethod) === null).map((m) => m.id);
}

// ------------------------------------------------------------------------ a hit, normalised

export interface Snippet {
  /** Plain words, no markdown marks. Empty when the node stores no content. */
  text: string;
  /** Length of the plain text the snippet was cut from. */
  total: number;
  /** True when `text` is shorter than the whole, so the page can say so. */
  cut: boolean;
  /** The node stores no content at all, which is different from storing an empty string. */
  absent: boolean;
}

/** How much of a hit's text a column draws. The rest is said, not dropped. */
export const SNIPPET = 160;

/**
 * A hit's content as a plain excerpt. Cut at a word boundary where one is near, and the cut is
 * reported in the return value rather than hidden behind a silent ellipsis.
 */
export function snippetOf(content: string | null | undefined, at = SNIPPET): Snippet {
  if (content === null || content === undefined) {
    return { text: "", total: 0, cut: false, absent: true };
  }
  const plain = plainWords(content);
  if (plain.length <= at) return { text: plain, total: plain.length, cut: false, absent: false };
  let end = plain.lastIndexOf(" ", at);
  if (end < at * 0.6) end = at;
  return { text: `${plain.slice(0, end).trimEnd()}…`, total: plain.length, cut: true, absent: false };
}

export interface Hit {
  rank: number;
  id: number;
  title: string | null;
  usetype: string | null;
  score: number;
  /** Which method produced this hit — differs from the column for fusion and routing. */
  method: string;
  /** Ancestor ids, root first, the node itself excluded (`documents.path`). */
  pathIds: number[];
  snippet: Snippet;
  producedBy: string | null;
  contentSource: string | null;
  /** For a projected result: the matched leaves that put it here. */
  via: { id: number; title: string | null; score: number }[];
}

/**
 * A `SearchResultItem` in the one shape every column draws.
 *
 * `documents.path` is JSONB and typed `unknown[]` by the generator; anything in it that is not a
 * node id is a contract violation and is refused here rather than drawn as a broken link.
 */
export function normaliseHit(item: SearchResultItem, index: number): Hit {
  const doc: DocumentResponse = item.document;
  const pathIds = pathIdsOf(doc);
  return {
    rank: index + 1,
    id: doc.id,
    title: doc.title,
    usetype: doc.usetype,
    score: item.score,
    method: item.method,
    pathIds,
    snippet: snippetOf(doc.content),
    producedBy: doc.produced_by ?? null,
    contentSource: doc.content_source ?? null,
    via: (item.via ?? []).map((v) => ({ id: v.document_id, title: v.title ?? null, score: v.score })),
  };
}

/** A score as a column prints it: four places, which is what search.js prints. */
export const scoreText = (score: number): string => score.toFixed(4);

// ---------------------------------------------------------------------------------- paths

/** A node's ancestor ids, root first, itself excluded — refused if `path` holds anything else. */
export function pathIdsOf(doc: Pick<DocumentResponse, "id" | "path">): number[] {
  return (doc.path ?? []).map((step) => {
    if (typeof step !== "number" || !Number.isInteger(step)) {
      throw new SearchAskError(
        `node ${doc.id}'s path holds ${JSON.stringify(step)}, which is not a node id`
      );
    }
    return step;
  });
}

/** Every ancestor id the hits name that is not already known, once each, in first-seen order. */
export function unknownAncestors(hits: Hit[], known: ReadonlySet<number>): number[] {
  const out: number[] = [];
  const seen = new Set<number>();
  for (const hit of hits) {
    for (const id of hit.pathIds) {
      if (!known.has(id) && !seen.has(id)) {
        seen.add(id);
        out.push(id);
      }
    }
  }
  return out;
}

/**
 * A path as the trail draws it. An id whose title has not been fetched keeps its place in the
 * trail with a null title — the trail then names it by id — rather than being dropped, which
 * would draw a shorter path than the node has.
 */
export function trailOf(
  pathIds: number[],
  named: ReadonlyMap<number, { title: string | null; usetype: string | null }>
): ViewAncestor[] {
  return pathIds.map((id) => ({
    id,
    title: named.get(id)?.title ?? null,
    usetype: named.get(id)?.usetype ?? null,
  }));
}

// -------------------------------------------------------------------- what each one ran

/**
 * The `applied` block in words: what the server actually ran. This is the explanation the search
 * operations return — there is no per-hit score breakdown in `SearchResultItem`, so the honest
 * explanation is the resolved methods, their weights, and the filters the score was taken under.
 */
export function explainApplied(applied: AppliedFilters | null | undefined): string[] {
  if (!applied) return ["the server reported no applied block, so what it ran is unstated"];
  const lines = [`limit ${applied.limit}`];
  if (applied.methods) {
    const weights = applied.weights ?? {};
    lines.push(
      `fused ${applied.methods.map((m) => `${m} × ${weights[m] ?? 1.0}`).join(" + ")} (RRF)`
    );
  }
  if (applied.usetype !== null && applied.usetype !== undefined) {
    lines.push(`usetype ${JSON.stringify(applied.usetype)}`);
  }
  if (applied.parent_id !== null && applied.parent_id !== undefined) {
    lines.push(`under node ${applied.parent_id}`);
  }
  const held = applied.exclude_types ?? [];
  lines.push(held.length ? `held out: ${held.join(", ")}` : "nothing held out");
  return lines;
}

/** Whether a short page is a short walk rather than an exhausted corpus. */
export const isTruncated = (applied: AppliedFilters | null | undefined): boolean =>
  applied?.truncated === true;

/** The router's decision, and the signals it read. */
export function explainRouting(routing: RoutingMetadata | null | undefined): string[] {
  if (!routing) return ["the router reported no decision"];
  const signals = Object.entries(routing.signals ?? {}).map(
    ([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`
  );
  return [
    `routed to ${routing.method}: ${routing.reason}`,
    ...(signals.length ? [`signals ${signals.join(" · ")}`] : []),
  ];
}
