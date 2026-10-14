<script setup lang="ts">
// One question, every ranking method, side by side.
//
// The behaviour is `jmfts-web/jmfts_web/static/views/search.js`; the table, the request each
// column is sent, and the refusals are `src/lib/search.ts`. What this file adds is the drawing,
// and three things the bundled view does not do:
//
// - A METHOD THAT CANNOT ANSWER HERE IS NOT RUN BY DEFAULT. `/capabilities?corpus=true` says
//   when a method returns nothing whatever the query (no token vectors, no BM25 index, no model
//   to embed the query). Its column is drawn narrow with that reason instead of an empty list,
//   and its box stays tickable: the reader can run it anyway and see the appliance agree.
// - EACH COLUMN SETTLES ON ITS OWN. The seven calls go out together and each column draws when
//   its own answer lands, so a slow MaxSim does not hold the BM25 column hostage.
// - EACH HIT CARRIES ITS PATH. `documents.path` gives ancestor ids only, so the titles are
//   fetched once per id with `get_document` and kept for the session. Those calls are on the
//   Calls page like every other one; an ancestor that could not be named says so.
//
// A SEARCH IS A URL. The form navigates and the route runs the search, so `#/search?q=…&m=…` is
// a link somebody can send and a reload loses nothing.

import { computed, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import NodeLine from "@/components/NodeLine.vue";
import Panel from "@/components/Panel.vue";
import ScopeTrail from "@/components/ScopeTrail.vue";
import Tag from "@/components/Tag.vue";
import {
  baseUrl,
  client,
  token,
  type AutoSearchResponse,
  type CapabilitiesResponse,
  type DocumentResponse,
  type SearchContextResponse,
  type SearchResponse,
  type SynthesizeResponse,
} from "@/api";
import {
  GET_METHODS,
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
  pathIdsOf,
  queryFromAsk,
  scoreText,
  synthesisArgs,
  trailOf,
  unknownAncestors,
  type GetMethod,
  type Hit,
  type MethodColumn,
  type MethodId,
  type SearchAsk,
} from "@/lib/search";
import { count } from "@/lib/vocab";

const route = useRoute();
const router = useRouter();

/** A failure as a sentence with its class on the front; never `JSON.stringify` an Error. */
const errorText = (failure: unknown): string =>
  failure instanceof Error ? `${failure.name}: ${failure.message}` : `failed: ${String(failure)}`;

// ------------------------------------------------------------------ what the appliance is

const caps = ref<CapabilitiesResponse | null>(null);
const capsError = ref<string | null>(null);
const contexts = ref<SearchContextResponse[] | null>(null);
const contextsError = ref<string | null>(null);

async function loadMeta() {
  caps.value = null;
  capsError.value = null;
  contexts.value = null;
  contextsError.value = null;
  ran.value = null;
  // Capabilities are not decoration: without the corpus counts a zero column cannot tell "the
  // query matched nothing" from "this method could never answer here". So a failure here stops
  // the page, and says why, rather than running searches it cannot explain.
  const [c, x] = await Promise.allSettled([
    client.capabilities({ corpus: true }),
    client.list_contexts(),
  ]);
  if (c.status === "fulfilled") caps.value = c.value;
  else capsError.value = errorText(c.reason);
  // A context list that failed costs the context control and nothing else; it says why there.
  if (x.status === "fulfilled") contexts.value = x.value;
  else contextsError.value = errorText(x.reason);
}

// ----------------------------------------------------------------------- the ask, from the URL

const parsed = computed<{ ask: SearchAsk | null; error: string | null }>(() => {
  try {
    return { ask: askFromQuery(route.query), error: null };
  } catch (failure) {
    if (!(failure instanceof SearchAskError)) throw failure;
    return { ask: null, error: failure.message };
  }
});

/** The methods this ask runs: what the URL names, else every one that can answer here. */
const effective = computed<MethodId[]>(() => {
  const ask = parsed.value.ask;
  if (!ask || !caps.value) return [];
  return ask.methods ?? defaultMethods(caps.value, ask.getMethod);
});

const contextProblem = computed<string | null>(() => {
  const name = parsed.value.ask?.context;
  if (!name || contexts.value === null) return null;
  if (contexts.value.some((c) => c.name === name)) return null;
  const known = contexts.value.map((c) => c.name).join(", ") || "none";
  return `no search context is named "${name}" on this appliance. The ones it has: ${known}.`;
});

const chosenContext = computed(
  () => contexts.value?.find((c) => c.name === parsed.value.ask?.context) ?? null
);

// ------------------------------------------------------------------------------- the form

const form = reactive({
  query: "",
  limit: 10,
  parent: "",
  usetype: "",
  context: "",
  getMethod: "hybrid" as GetMethod,
  includeExcluded: false,
  methods: new Set<MethodId>(),
});
const formProblem = ref<string | null>(null);
const explain = ref(false);

/** Put the URL's search back into the controls, so the form always shows what ran. */
function fillForm() {
  const ask = parsed.value.ask;
  if (!ask) return;
  form.query = ask.query;
  form.limit = ask.limit;
  form.parent = ask.parentId === null ? "" : String(ask.parentId);
  form.usetype = ask.usetype;
  form.context = ask.context;
  form.getMethod = ask.getMethod;
  form.includeExcluded = ask.includeExcluded;
  form.methods = new Set(effective.value);
}

function toggleMethod(id: MethodId, on: boolean) {
  const next = new Set(form.methods);
  if (on) next.add(id);
  else next.delete(id);
  form.methods = next;
}

function submit() {
  formProblem.value = null;
  if (!form.query.trim()) {
    formProblem.value = "Enter a query; a search for nothing is not a search.";
    return;
  }
  const chosen = METHODS.map((m) => m.id).filter((id) => form.methods.has(id));
  if (chosen.length === 0) {
    formProblem.value = "Select at least one method; an empty side-by-side is not one.";
    return;
  }
  const defaults = caps.value ? defaultMethods(caps.value, form.getMethod) : null;
  const sameAsDefault =
    defaults !== null && defaults.length === chosen.length && defaults.every((id, i) => id === chosen[i]);
  let query: Record<string, string>;
  try {
    // Through the same reader the URL goes through, so a form and a link are refused alike.
    const ask = askFromQuery({
      q: form.query,
      limit: String(form.limit ?? ""),
      parent: form.parent,
      usetype: form.usetype,
      context: form.context,
      get: form.getMethod,
      m: chosen.join(","),
      x: form.includeExcluded ? "1" : "",
    });
    query = queryFromAsk({ ...ask, methods: sameAsDefault ? null : ask.methods });
  } catch (failure) {
    if (!(failure instanceof SearchAskError)) throw failure;
    formProblem.value = failure.message;
    return;
  }
  const same = JSON.stringify(query) === JSON.stringify(queryFromAsk(parsed.value.ask ?? askFromQuery({})));
  // Pressing Search on the search already in the URL runs it again rather than doing nothing.
  if (same && parsed.value.ask) maybeRun(true);
  else void router.push({ name: "search", query });
}

function wholeCorpus() {
  form.parent = "";
}

// ------------------------------------------------------------------------------ running it

type Outcome =
  | { state: "loading" }
  | { state: "error"; error: string }
  | {
      state: "done";
      response: SearchResponse | AutoSearchResponse;
      hits: Hit[];
      /** A hit list the page could not read; the response is still shown, the fault is named. */
      unreadable: string | null;
    };

const outcomes = reactive(new Map<MethodId, Outcome>());
const ranAsk = ref<SearchAsk | null>(null);
const ran = ref<string | null>(null);
let generation = 0;

function maybeRun(force = false) {
  const ask = parsed.value.ask;
  if (!ask || !ask.query || !caps.value || contextProblem.value) return;
  const key = JSON.stringify(queryFromAsk(ask));
  if (!force && key === ran.value) return;
  ran.value = key;
  run(ask);
}

function run(ask: SearchAsk) {
  const mine = ++generation;
  outcomes.clear();
  ranAsk.value = ask;
  synthesis.value = null;
  synthesisError.value = null;
  for (const id of effective.value) {
    const column = METHODS.find((m) => m.id === id) as MethodColumn;
    outcomes.set(id, { state: "loading" });
    client
      .call(column.opId, argsFor(id, ask))
      .then((raw) => {
        if (mine !== generation) return;
        const response = raw as SearchResponse | AutoSearchResponse;
        let hits: Hit[] = [];
        let unreadable: string | null = null;
        try {
          hits = response.results.map(normaliseHit);
        } catch (failure) {
          unreadable = errorText(failure);
        }
        outcomes.set(id, { state: "done", response, hits, unreadable });
        void nameAncestors(hits);
      })
      .catch((failure) => {
        if (mine !== generation) return;
        // The server's own words, where the results would have been. JmftsHttpError's message
        // carries the method, path, status and detail; rewriting it would lose all four.
        outcomes.set(id, { state: "error", error: errorText(failure) });
      });
  }
}

// -------------------------------------------------------------------- naming the ancestors

const named = reactive(new Map<number, { title: string | null; usetype: string | null }>());
const naming = new Set<number>();
const unnamed = reactive(new Map<number, string>());

async function nameAncestors(hits: Hit[]) {
  const wanted = unknownAncestors(hits, new Set([...named.keys(), ...naming, ...unnamed.keys()]));
  await Promise.all(wanted.map(nameOne));
}

async function nameOne(id: number): Promise<DocumentResponse | null> {
  naming.add(id);
  try {
    const doc = await client.get_document({ document_id: id });
    named.set(id, { title: doc.title, usetype: doc.usetype });
    return doc;
  } catch (failure) {
    unnamed.set(id, errorText(failure));
    return null;
  } finally {
    naming.delete(id);
  }
}

const unnamedNote = computed(() => {
  if (unnamed.size === 0) return null;
  const [id, why] = [...unnamed.entries()][0];
  return `${count(unnamed.size, "ancestor")} could not be named and show by id. Node ${id}: ${why}`;
});

// ------------------------------------------------------------------------------- the scope

const scopeNode = ref<DocumentResponse | null>(null);
const scopeError = ref<string | null>(null);

async function loadScope(id: number | null) {
  scopeNode.value = null;
  scopeError.value = null;
  if (id === null) return;
  try {
    const doc = await client.get_document({ document_id: id });
    if (parsed.value.ask?.parentId !== id) return;
    const ancestors = pathIdsOf(doc);
    scopeNode.value = doc;
    named.set(doc.id, { title: doc.title, usetype: doc.usetype });
    const known = new Set([...named.keys(), ...naming, ...unnamed.keys()]);
    await Promise.all(ancestors.filter((a) => !known.has(a)).map(nameOne));
  } catch (failure) {
    if (parsed.value.ask?.parentId === id) scopeError.value = errorText(failure);
  }
}

const scopeTrail = computed(() => {
  const node = scopeNode.value;
  if (!node) return [];
  return trailOf([...pathIdsOf(node), node.id], named);
});

// ------------------------------------------------------------------------------- synthesis

const synthMethod = ref<string>("auto");
const synthTopK = ref(5);
const synthesis = ref<SynthesizeResponse | null>(null);
const synthesisError = ref<string | null>(null);
const synthesizing = ref(false);

async function synthesize() {
  const ask = ranAsk.value;
  if (!ask) return;
  const mine = generation;
  synthesizing.value = true;
  synthesis.value = null;
  synthesisError.value = null;
  try {
    const answer = (await client.call(
      SYNTHESIS_OP,
      synthesisArgs(ask, synthMethod.value, synthTopK.value)
    )) as SynthesizeResponse;
    if (mine === generation) synthesis.value = answer;
  } catch (failure) {
    if (mine === generation) synthesisError.value = errorText(failure);
  } finally {
    synthesizing.value = false;
  }
}

// ---------------------------------------------------------------------------- the wiring

// The appliance or the token changed: everything this page knows about the appliance is stale.
watch(
  [baseUrl, token],
  async () => {
    named.clear();
    unnamed.clear();
    await loadMeta();
    fillForm();
    void loadScope(parsed.value.ask?.parentId ?? null);
    maybeRun();
  },
  { immediate: true }
);

watch(
  () => route.query,
  () => {
    formProblem.value = null;
    fillForm();
    maybeRun();
  }
);

watch(
  () => parsed.value.ask?.parentId ?? null,
  (id) => void loadScope(id)
);

// ---------------------------------------------------------------------------- the drawing

interface Column {
  method: MethodColumn;
  outcome: Outcome | null;
  /** Why this method returns nothing here, whatever the query. */
  cannot: string | null;
  caveats: string[];
}

const columns = computed<Column[]>(() => {
  const c = caps.value;
  const ask = ranAsk.value;
  if (!c || !ask) return [];
  return METHODS.filter((m) => outcomes.has(m.id) || cannotAnswer(m.id, c, ask.getMethod) !== null)
    .map((method) => ({
      method,
      outcome: outcomes.get(method.id) ?? null,
      cannot: cannotAnswer(method.id, c, ask.getMethod),
      caveats: method.id === "hybrid" ? hybridCaveats(c) : [],
    }));
});

// A column that was not run carries one sentence, so it is drawn narrow: the width goes to the
// columns that have hits to compare.
const columnWidths = computed(() =>
  columns.value.map((c) => (c.outcome ? "minmax(20rem, 1fr)" : "minmax(11rem, 0.5fr)")).join(" ")
);

const reasonFor = (id: MethodId) =>
  caps.value ? cannotAnswer(id, caps.value, form.getMethod) : null;

const held = computed(() => caps.value?.retrieval.search_exclude_usetypes ?? []);

const trailFor = (hit: Hit) => trailOf(hit.pathIds, named);

const countText = (o: Extract<Outcome, { state: "done" }>) =>
  `${count(o.response.total, "result")} in ${o.response.latency_ms} ms`;
</script>

<template>
  <div class="search scroll-pane">
    <header class="head">
      <h1>Search</h1>
      <p class="lede">
        One question, every ranking method, side by side. Each column is one call, and each is on
        the Calls page. A column that returned nothing says so, and says why when the corpus
        explains it.
      </p>
    </header>

    <div v-if="capsError" class="failed">
      <h2>The appliance did not say what it can do</h2>
      <p>{{ capsError }}</p>
      <p class="apparatus-voice">
        This page reads <code>GET /capabilities?corpus=true</code> before it searches, because
        without it a column with no hits cannot say whether the method could ever have answered.
        A 401 means the token is missing or wrong.
      </p>
    </div>

    <form class="ask" autocomplete="off" @submit.prevent="submit">
      <label class="field wide">
        <span class="name">Question</span>
        <input v-model="form.query" class="q" type="search" placeholder="what are you looking for?" />
      </label>

      <div class="field wide scope">
        <span class="name">Scope</span>
        <div class="scope-row">
          <input
            v-model="form.parent"
            class="value narrow"
            type="text"
            inputmode="numeric"
            placeholder="whole corpus"
            aria-label="Search under node id"
          />
          <button v-if="form.parent" type="button" class="ghost" @click="wholeCorpus">
            Whole corpus
          </button>
          <span class="hint">a node id: search only its subtree (<code>parent_id</code>)</span>
        </div>
        <div v-if="parsed.ask?.parentId != null" class="scope-now">
          <p v-if="scopeError" class="bad">Node {{ parsed.ask.parentId }}: {{ scopeError }}</p>
          <ScopeTrail v-else-if="scopeNode" :ancestors="scopeTrail" />
          <p v-else class="waiting">Reading node {{ parsed.ask.parentId }}…</p>
        </div>
      </div>

      <label class="field">
        <span class="name">Limit</span>
        <input v-model.number="form.limit" class="value narrow" type="number" min="1" max="100" />
      </label>
      <label class="field">
        <span class="name">Usetype globs</span>
        <input v-model="form.usetype" class="value" type="text" placeholder="any" />
      </label>
      <label class="field">
        <span class="name">Search context</span>
        <select v-model="form.context" :disabled="contexts === null">
          <option value="">(none)</option>
          <option v-for="c in contexts ?? []" :key="c.name" :value="c.name">{{ c.name }}</option>
        </select>
        <span v-if="contextsError" class="bad small">{{ contextsError }}</span>
      </label>
      <label class="field">
        <span class="name"><code>GET /search/</code> runs</span>
        <select v-model="form.getMethod">
          <option v-for="m in GET_METHODS" :key="m" :value="m">{{ m }}</option>
        </select>
      </label>

      <fieldset class="field wide methods">
        <legend class="name">Methods</legend>
        <label v-for="m in METHODS" :key="m.id" class="method" :class="{ cannot: reasonFor(m.id) }">
          <input
            type="checkbox"
            :checked="form.methods.has(m.id)"
            @change="toggleMethod(m.id, ($event.target as HTMLInputElement).checked)"
          />
          <span class="value">{{ m.title }}</span>
          <span v-if="reasonFor(m.id)" class="why">cannot answer here: {{ reasonFor(m.id) }}</span>
        </label>
        <label class="method">
          <input v-model="form.includeExcluded" type="checkbox" />
          <span>
            Include what is normally held out
            <span class="hint">(sends <code>exclude_types: []</code>; held out here:
              {{ held.join(", ") || "nothing" }})</span>
          </span>
        </label>
      </fieldset>

      <div class="actions wide">
        <button type="submit" :disabled="!caps">Search</button>
        <label class="toggle">
          <input v-model="explain" type="checkbox" />
          Show what each method ran
        </label>
        <span v-if="formProblem" class="bad">{{ formProblem }}</span>
      </div>
    </form>

    <p v-if="parsed.error" class="failed">This address names a search this page cannot run: {{ parsed.error }}</p>
    <p v-if="contextProblem" class="failed">{{ contextProblem }}</p>
    <p v-if="chosenContext" class="note">
      Context <span class="value">{{ chosenContext.name }}</span>:
      {{ chosenContext.description ?? "no description" }} —
      <span class="value">{{ JSON.stringify(chosenContext.config) }}</span>
    </p>
    <p v-if="caps && !caps.corpus" class="note bad">
      The appliance was asked for corpus counts and returned none, so a column with no hits cannot
      be told apart from a method that cannot answer here.
    </p>

    <p v-if="!ranAsk && caps && !parsed.error" class="waiting">
      Ask a question above. The methods run in parallel and each column fills when its own answer
      lands.
    </p>

    <p v-if="unnamedNote" class="note bad">{{ unnamedNote }}</p>

    <div v-if="columns.length" class="columns" :style="{ gridTemplateColumns: columnWidths }">
      <section
        v-for="col in columns"
        :key="col.method.id"
        class="column"
        :class="{ skipped: !col.outcome }"
      >
        <header class="col-head">
          <h2 class="value">{{ col.method.title }}</h2>
          <p class="path value">{{ col.method.path }}</p>
          <p class="what">{{ col.method.what }}</p>
          <p v-if="col.method.id === 'quick'" class="what">
            <span class="value">?method={{ ranAsk?.getMethod }}</span>
            <template v-if="ranAsk?.includeExcluded">
              — <span class="bad">did NOT send <code>exclude_types: []</code>; GET has no such
              parameter, so this column kept the default exclusions.</span>
            </template>
          </p>
        </header>

        <!-- Not run, because it cannot answer here. Its reason, not an empty list. -->
        <p v-if="!col.outcome" class="cannot-note">
          Not run — this method cannot answer here: {{ col.cannot }}. Tick it above to run it
          anyway.
        </p>

        <p v-else-if="col.outcome.state === 'loading'" class="waiting">searching…</p>

        <div v-else-if="col.outcome.state === 'error'" class="failed small">
          {{ col.outcome.error }}
        </div>

        <template v-else>
          <p class="tally value">{{ countText(col.outcome) }}</p>
          <p v-for="c in col.caveats" :key="c" class="note small">{{ c }}</p>
          <p v-if="isTruncated(col.outcome.response.applied)" class="note bad small">
            Short page: an approximate index scan answered with fewer rows than it asked for, so
            this is not an exhausted corpus.
          </p>
          <div v-if="explain" class="explain value">
            <template v-if="col.method.kind === 'routed'">
              <p
                v-for="line in explainRouting((col.outcome.response as AutoSearchResponse).routing)"
                :key="line"
              >
                {{ line }}
              </p>
            </template>
            <p v-for="line in explainApplied(col.outcome.response.applied)" :key="line">{{ line }}</p>
          </div>
          <p v-else-if="col.method.kind === 'routed'" class="note small">
            {{ explainRouting((col.outcome.response as AutoSearchResponse).routing)[0] }}
          </p>

          <p v-if="col.outcome.unreadable" class="failed small">{{ col.outcome.unreadable }}</p>

          <p v-else-if="col.outcome.hits.length === 0" class="empty">
            <template v-if="col.cannot">
              No hits — this method cannot answer here: {{ col.cannot }}.
            </template>
            <template v-else>No hits. The method ran and nothing matched this query.</template>
          </p>

          <ol v-else class="hits">
            <li v-for="hit in col.outcome.hits" :key="hit.id" class="hit">
              <NodeLine
                :id="hit.id"
                :title="hit.title"
                :usetype="hit.usetype"
                :preview="hit.title?.trim() ? null : hit.snippet.text"
                :trailing="scoreText(hit.score)"
              />
              <div class="under">
                <p v-if="hit.title?.trim() && hit.snippet.text" class="snippet">
                  {{ hit.snippet.text }}
                </p>
                <ScopeTrail :ancestors="trailFor(hit)" />
                <div class="marks">
                  <Tag
                    v-if="col.method.kind === 'fusion' || col.method.kind === 'routed'"
                    :value="hit.method"
                    meaning="which method produced this hit"
                  />
                  <Tag
                    v-if="hit.producedBy"
                    :value="hit.producedBy"
                    meaning="the ingest rule that wrote this node"
                  />
                  <Tag
                    v-if="hit.contentSource === 'effective'"
                    value="effective content"
                    tone="active"
                    meaning="a container with no stored text; joined from its subtree at read time"
                  />
                  <span v-if="hit.snippet.absent" class="small faint">stores no content</span>
                  <span v-else-if="hit.snippet.cut" class="small faint"
                    >excerpt of {{ hit.snippet.total.toLocaleString() }} characters</span
                  >
                  <span v-if="hit.via.length" class="small faint"
                    >via {{ hit.via.map((v) => `#${v.id}`).join(", ") }}</span
                  >
                </div>
              </div>
            </li>
          </ol>
        </template>
      </section>
    </div>

    <Panel v-if="ranAsk && caps" title="Synthesis" :quiet="!caps.llm.configured">
      <p v-if="!caps.llm.configured" class="note">
        No LLM is configured on this appliance (<code>JMFTS_LLM_*</code>), so
        <code>POST /search/synthesize</code> cannot answer. What that costs:
        {{ caps.llm.requires_llm.join(", ") }}. Everything above works without one.
      </p>
      <template v-else>
        <form class="synth" @submit.prevent="synthesize">
          <label class="field">
            <span class="name">Retrieve with</span>
            <select v-model="synthMethod">
              <option value="auto">auto</option>
              <option v-for="m in GET_METHODS" :key="m" :value="m">{{ m }}</option>
            </select>
          </label>
          <label class="field">
            <span class="name">Top k</span>
            <input v-model.number="synthTopK" class="value narrow" type="number" min="1" max="20" />
          </label>
          <button type="submit" :disabled="synthesizing">Synthesize an answer</button>
          <span class="hint">one LLM call, model <span class="value">{{ caps.llm.model }}</span></span>
        </form>
        <p v-if="synthesizing" class="waiting">asking the model…</p>
        <div v-else-if="synthesisError" class="failed small">{{ synthesisError }}</div>
        <div v-else-if="synthesis" class="answer">
          <p v-if="synthesis.llm_available === false" class="note bad">
            The LLM did not answer, so the text below is the server's placeholder rather than a
            synthesis. The sources under it are real retrieval results.
            <template v-if="synthesis.llm_error"> {{ synthesis.llm_error.message }}</template>
          </p>
          <p class="prose">{{ synthesis.synthesis }}</p>
          <p class="small faint value">
            {{ synthesis.llm_model }} · retrieval {{ synthesis.search_latency_ms }} ms · total
            {{ synthesis.total_latency_ms }} ms
          </p>
          <p v-if="synthesis.sources.length === 0" class="note bad">
            No sources: the answer cites nothing in this corpus.
          </p>
          <NodeLine
            v-for="s in synthesis.sources"
            :key="s.document_id"
            :id="s.document_id"
            :title="s.title"
            :trailing="`${scoreText(s.score)} via ${s.method}`"
          />
        </div>
      </template>
    </Panel>
  </div>
</template>

<style scoped>
.search {
  padding: var(--s6) var(--s6) var(--s7);
}

.head {
  margin-bottom: var(--s5);
}

.head h1 {
  font-size: var(--t-display);
  letter-spacing: -0.015em;
}

.lede {
  margin-top: var(--s2);
  color: var(--ink-soft);
}

.ask {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--s3) var(--s4);
  max-width: 72rem;
  margin-bottom: var(--s5);
  padding-bottom: var(--s5);
  border-bottom: 1px solid var(--rule);
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--s1);
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
}

.wide {
  grid-column: 1 / -1;
}

.name {
  font-size: var(--t-micro);
  color: var(--ink-faint);
  letter-spacing: 0.03em;
  text-transform: uppercase;
  padding: 0;
}

input[type="text"],
input[type="search"],
input[type="number"],
select {
  padding: 5px var(--s2);
  border: 1px solid var(--rule);
  border-radius: var(--r);
  background: var(--leaf);
  color: var(--ink);
  font: inherit;
  font-size: var(--t-small);
  min-width: 0;
}

input:focus,
select:focus {
  border-color: var(--accent);
}

input.q {
  font-size: var(--t-mid);
  padding: var(--s2) var(--s3);
  font-family: var(--face-text);
}

input.narrow {
  width: 9rem;
}

.scope-row {
  display: flex;
  align-items: center;
  gap: var(--s3);
  flex-wrap: wrap;
}

.scope-now {
  margin-top: var(--s1);
}

.methods {
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  gap: var(--s2) var(--s5);
}

.methods legend {
  margin-bottom: var(--s1);
  width: 100%;
}

.method {
  display: inline-flex;
  align-items: baseline;
  gap: var(--s2);
  font-size: var(--t-small);
}

.method.cannot .value {
  color: var(--ink-faint);
}

.why {
  font-size: var(--t-micro);
  font-style: italic;
  color: var(--ink-faint);
  max-width: 28ch;
}

.hint {
  font-size: var(--t-micro);
  color: var(--ink-faint);
}

.actions {
  display: flex;
  align-items: center;
  gap: var(--s4);
  flex-wrap: wrap;
}

.toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--s2);
  font-size: var(--t-small);
  color: var(--ink-soft);
}

button {
  padding: 5px var(--s4);
  border: 1px solid var(--accent);
  border-radius: var(--r);
  background: var(--accent);
  color: var(--leaf);
  font-size: var(--t-small);
  cursor: pointer;
}

button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

button.ghost {
  background: transparent;
  border-color: var(--rule);
  color: var(--ink-soft);
}

.columns {
  display: grid;
  gap: var(--s4);
  overflow-x: auto;
  padding-bottom: var(--s4);
  margin-bottom: var(--s5);
  scrollbar-width: thin;
}

.column {
  min-width: 0;
  border-top: 2px solid var(--ink);
  padding-top: var(--s2);
}

.column.skipped {
  border-top-color: var(--rule);
}

.col-head h2 {
  font-size: var(--t-mid);
  font-weight: 600;
}

.col-head p {
  margin: 0;
}

.path {
  font-size: var(--t-micro);
  color: var(--ink-faint);
}

.what {
  font-size: var(--t-small);
  color: var(--ink-soft);
  line-height: 1.45;
  margin-bottom: var(--s2) !important;
}

.tally {
  font-size: var(--t-small);
  color: var(--ink);
  margin: var(--s2) 0;
}

.explain {
  margin: var(--s2) 0;
  padding: var(--s2) var(--s3);
  background: var(--sunk);
  font-size: var(--t-micro);
}

.explain p {
  margin: 0;
}

.hits {
  list-style: none;
  margin: 0;
  padding: 0;
}

.hit {
  padding-bottom: var(--s2);
  border-bottom: 1px solid var(--rule-soft);
}

.hit :deep(.line) {
  border-bottom: 0;
  grid-template-columns: 3rem minmax(0, 1fr) auto;
}

.under {
  padding-left: calc(3rem + var(--s3));
}

.under :deep(.trail) {
  font-size: var(--t-micro);
}

.marks {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--s2);
  margin-top: 2px;
}

.snippet {
  margin: 0 0 var(--s1);
  font-size: var(--t-small);
  line-height: 1.5;
  color: var(--ink-soft);
}

.cannot-note {
  font-size: var(--t-small);
  font-style: italic;
  color: var(--ink-soft);
  border-left: 2px solid var(--rule);
  padding-left: var(--s3);
}

.note {
  font-size: var(--t-small);
  color: var(--ink-soft);
}

.small {
  font-size: var(--t-micro);
}

.faint {
  color: var(--ink-faint);
}

.bad {
  color: var(--alarm);
}

.waiting,
.empty {
  color: var(--ink-soft);
  font-style: italic;
  font-size: var(--t-small);
}

.failed {
  max-width: var(--measure);
  padding: var(--s3) var(--s4);
  border-left: 2px solid var(--alarm);
  background: var(--alarm-wash);
  overflow-wrap: anywhere;
}

.failed.small {
  font-size: var(--t-small);
}

.synth {
  display: flex;
  align-items: flex-end;
  gap: var(--s4);
  flex-wrap: wrap;
  margin-bottom: var(--s3);
}

.prose {
  white-space: pre-wrap;
}

@media (max-width: 880px) {
  .search {
    padding: var(--s4);
  }
  .ask {
    grid-template-columns: minmax(0, 1fr);
  }
  .columns {
    grid-template-columns: minmax(0, 1fr) !important;
    overflow-x: visible;
  }
}
</style>
