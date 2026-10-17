<script setup lang="ts">
// Put a document in, see what the appliance will do with it first, then watch it settle.
//
// The same operations, and the same facts, as jmfts-web's `views/upload.js`:
//
//   GET  /capabilities                       what formats are identified from bytes, entry points
//   GET  /ingest/pipelines                   each entry point's source and resolved options
//   POST /ingest/analyze                     a file: probe runs and stops; the plan comes back
//   POST /ingest/explain                     text: the plan for a format, without bytes
//   POST /ingest/file                        store the bytes, enqueue probe — does NOT wait
//   POST /ingest                             an entry point; drains before it answers
//   GET  /ingest/file/{id}/frontier          counts over the subtree, polled
//   GET  /documents/{id}/evidence            the root's attempt log, polled beside it
//   GET  /documents/{id}, …/ingest-options   the parent: that it exists, and what it asks
//
// TWO THINGS THIS PAGE DOES NOT LIE ABOUT, both from `upload.js`. The ingest button does not
// exist until a plan for exactly this request is on the page, and changing anything the plan
// depended on — the file, the parent, `private`, an option — marks it stale and disables the
// button until it is asked again. And an upload response is `accepted`, never "done": only
// the frontier can say settled, and it is shown as counts, never a percentage, because the
// tasks below the frontier do not exist yet.

import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import Fact from "@/components/Fact.vue";
import Panel from "@/components/Panel.vue";
import Tag from "@/components/Tag.vue";
import {
  baseUrl,
  client,
  token,
  type AnalyzeIngestResponse,
  type CapabilitiesResponse,
  type CollectionOptionsResponse,
  type DocumentResponse,
  type ExplainIngestResponse,
  type FileUploadResponse,
  type IngestFrontierResponse,
  type IngestResponse,
  type PipelineInfo,
} from "@/api";
import {
  asFieldText,
  checkName,
  editsFromJson,
  mergeOptions,
  optionFields,
  parseField,
  parseOptions,
  parseParentId,
  sourceProblem,
  SOURCE_NOUNS,
  type OptionEdits,
  type OptionGroups,
} from "@/lib/ingestForm";
import {
  attemptsFrom,
  countRange,
  enqueuedCount,
  ingestionState,
  OUTCOMES,
  PATTERN_SOURCES,
  rungs,
  tally,
  TERMINAL,
  type RungState,
} from "@/lib/ingestPlan";
import { createPoller } from "@/lib/poller";
import type { Tone } from "@/lib/vocab";

const route = useRoute();

/** 1.5 s between readings: often enough to watch, rare enough to ignore. Same as upload.js. */
const POLL_MS = 1500;

const errorOf = (failure: unknown) => (failure as Error).message;
const bytes = (n: number) => {
  const units = ["B", "kB", "MB", "GB"];
  let value = n;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
};

// ------------------------------------------------------------------ what the appliance takes

const capabilities = ref<CapabilitiesResponse | null>(null);
const pipelines = ref<PipelineInfo[]>([]);
const aboutError = ref<string | null>(null);

let aboutAsk = 0;

async function loadAbout() {
  const mine = ++aboutAsk;
  aboutError.value = null;
  try {
    const [caps, entry] = await Promise.all([
      client.capabilities(),
      client.list_registered_pipelines(),
    ]);
    if (mine !== aboutAsk) return;
    capabilities.value = caps;
    pipelines.value = entry;
    if (!usetype.value && entry.length) usetype.value = entry[0].name;
  } catch (failure) {
    if (mine !== aboutAsk) return;
    aboutError.value = errorOf(failure);
  }
}

const missingExtras = computed(
  () => capabilities.value?.extras.filter((extra) => !extra.installed) ?? []
);

// --------------------------------------------------------------------------- the request

type Mode = "file" | "entry";
const mode = ref<Mode>("file");

const file = ref<File | null>(null);
const dragging = ref(false);
const parentText = ref("");
const isPrivate = ref(false);
const edits = reactive<OptionEdits>({});
const jsonText = ref("");
const showAdvanced = ref(false);

const usetype = ref("");
const content = ref("");
const title = ref("");

const pipeline = computed(() => pipelines.value.find((p) => p.name === usetype.value) ?? null);
const source = computed(() => pipeline.value?.source ?? "content");

// The parent: a document that must exist before the form may submit, and whose collection
// options are where an upload directly under it starts.
const parent = reactive<{
  id: number | null;
  doc: DocumentResponse | null;
  options: CollectionOptionsResponse | null;
  problem: string | null;
  checking: boolean;
}>({ id: null, doc: null, options: null, problem: null, checking: false });

let parentAsk = 0;
async function resolveParent() {
  const ask = ++parentAsk;
  parent.doc = null;
  parent.options = null;
  parent.problem = null;
  try {
    parent.id = parseParentId(parentText.value);
  } catch (failure) {
    parent.id = null;
    parent.problem = errorOf(failure);
    return;
  }
  if (parent.id === null) return;
  parent.checking = true;
  try {
    const [doc, options] = await Promise.all([
      client.get_document({ document_id: parent.id }),
      client.get_collection_options({ document_id: parent.id }),
    ]);
    if (ask !== parentAsk) return;
    parent.doc = doc;
    parent.options = options;
  } catch (failure) {
    if (ask !== parentAsk) return;
    parent.problem = errorOf(failure);
  } finally {
    if (ask === parentAsk) parent.checking = false;
  }
}

// --------------------------------------------------------------------- options, validated

/** The options the appliance resolved for this request, which the fields are built from. */
const resolved = computed<OptionGroups | null>(() => {
  if (mode.value === "file") return (analysis.value?.plan?.options as OptionGroups) ?? null;
  return (pipeline.value?.options as OptionGroups | undefined) ?? null;
});

const fields = computed(() => (resolved.value ? optionFields(resolved.value) : []));

const pasted = computed<{ edits: OptionEdits; problem: string | null }>(() => {
  try {
    return { edits: editsFromJson(jsonText.value), problem: null };
  } catch (failure) {
    return { edits: {}, problem: errorOf(failure) };
  }
});

const parsed = computed(() => {
  const all = { ...edits, ...pasted.value.edits };
  if (Object.keys(all).length === 0) return { options: null, problems: [] as string[] };
  if (resolved.value === null) {
    return {
      options: null,
      problems: [
        mode.value === "file"
          ? "Options can be checked once the appliance has analysed a file and said what it resolves."
          : "Options can be checked once the entry points have loaded.",
      ],
    };
  }
  return parseOptions(all, resolved.value);
});

const problems = computed(() => {
  const list: string[] = [];
  if (pasted.value.problem) list.push(pasted.value.problem);
  list.push(...parsed.value.problems);
  if (parent.problem) list.push(`Parent: ${parent.problem}`);
  if (parent.checking) list.push("Checking the parent…");
  return list;
});

function fieldValue(key: string, resolvedValue: unknown): string {
  return key in edits ? edits[key] : asFieldText(resolvedValue);
}

function fieldProblem(key: string, kind: Parameters<typeof parseField>[0]): string | null {
  if (!(key in edits)) return null;
  try {
    parseField(kind, edits[key]);
    return null;
  } catch (failure) {
    return errorOf(failure);
  }
}

function setField(key: string, text: string) {
  edits[key] = text;
}

function resetField(key: string) {
  delete edits[key];
}

function resetAll() {
  for (const key of Object.keys(edits)) delete edits[key];
  jsonText.value = "";
}

// ------------------------------------------------------------ analyse (file) / explain (text)

const analysis = ref<AnalyzeIngestResponse | null>(null);
const analysisKey = ref<string | null>(null);
const analyzing = ref(false);
const analyzeError = ref<string | null>(null);

const explained = ref<ExplainIngestResponse | null>(null);
const explainKey = ref<string | null>(null);
const explaining = ref(false);
const explainError = ref<string | null>(null);

// A file has no identity of its own in the browser; name, size and modification time are what
// distinguishes a second choice from the first.
const fileId = computed(() =>
  file.value ? `${file.value.name}|${file.value.size}|${file.value.lastModified}` : null
);

/** Everything a plan for this request depended on. A plan made under another key is stale. */
const requestKey = computed(() =>
  JSON.stringify(
    mode.value === "file"
      ? { file: fileId.value, parent: parent.id, private: isPrivate.value, options: parsed.value.options }
      : { usetype: usetype.value, options: parsed.value.options }
  )
);

const planFresh = computed(() =>
  mode.value === "file"
    ? analysis.value !== null && analysisKey.value === requestKey.value
    : explained.value !== null && explainKey.value === requestKey.value
);

// Generation counters: an answer that arrives after a newer question (or after another file was
// chosen) describes something the page is no longer asking about, and must not be shown as if it
// did. Same device as `parentAsk` above and the one Search.vue uses.
let analyzeAsk = 0;
let explainAsk = 0;

async function analyze() {
  // A request that does not validate is not sent; the button that calls this is disabled
  // for the same reason, and this is the same check for the call `choose()` makes.
  if (!file.value || problems.value.length) return;
  const key = requestKey.value;
  const ask = ++analyzeAsk;
  analyzing.value = true;
  analyzeError.value = null;
  try {
    const answer = await client.analyze_ingest({
      file: file.value,
      options: parsed.value.options ?? undefined,
      parent_id: parent.id ?? undefined,
      private: isPrivate.value,
    });
    if (ask !== analyzeAsk) return;
    analysis.value = answer;
    analysisKey.value = key;
  } catch (failure) {
    if (ask !== analyzeAsk) return;
    // The key goes, so the plan reads as stale and nothing can be submitted from it. The
    // analysis stays: it is what the option fields are built from, and dropping it removed
    // the fields and left the user no way to correct the edit that failed except re-picking
    // the file. A different file clears it in `choose()`.
    analysisKey.value = null;
    analyzeError.value = errorOf(failure);
  } finally {
    if (ask === analyzeAsk) analyzing.value = false;
  }
}

function choose(chosen: File | null | undefined) {
  if (!chosen) return;
  file.value = chosen;
  // The previous answer described previous bytes, and its options were resolved for them. An
  // analysis still in flight is for the previous file too: bumping the counter discards it.
  analyzeAsk++;
  analyzing.value = false;
  analysis.value = null;
  analysisKey.value = null;
  resetAll();
  void analyze();
}

function onPick(event: Event) {
  choose((event.target as HTMLInputElement).files?.[0]);
}

function onDrop(event: DragEvent) {
  dragging.value = false;
  choose(event.dataTransfer?.files?.[0]);
}

/**
 * A plan for an entry point whose content is text: the format is `text`, because UTF-8 bytes
 * sniff as text, and the options are the entry point's own with the edits on top. Probe has not
 * measured anything, so rows its patterns decide come back `conditional`, and say so.
 */
async function explainEntry() {
  if (!pipeline.value || source.value !== "content") return;
  const key = requestKey.value;
  const ask = ++explainAsk;
  explaining.value = true;
  explainError.value = null;
  try {
    const answer = await client.explain_ingest({
      request: {
        format: "text",
        options: mergeOptions(
          (pipeline.value.options ?? {}) as OptionGroups,
          parsed.value.options
        ),
        patterns: null,
      },
    });
    if (ask !== explainAsk) return;
    explained.value = answer;
    explainKey.value = key;
  } catch (failure) {
    if (ask !== explainAsk) return;
    explained.value = null;
    explainKey.value = null;
    explainError.value = errorOf(failure);
  } finally {
    if (ask === explainAsk) explaining.value = false;
  }
}

const plan = computed<ExplainIngestResponse | null>(() =>
  mode.value === "file" ? (analysis.value?.plan ?? null) : explained.value
);

const nameCheck = computed(() => {
  const a = analysis.value;
  if (!a || !capabilities.value) return null;
  return checkName(
    a.file.filename,
    a.format,
    a.file.detected_by,
    capabilities.value.ingest.detectable_formats
  );
});

// ------------------------------------------------------------------------------- submit

const submitting = ref(false);
const submitError = ref<string | null>(null);
const upload = ref<FileUploadResponse | null>(null);
const entryResult = ref<IngestResponse | null>(null);

const contentProblem = computed(() =>
  mode.value === "entry" ? sourceProblem(source.value, content.value) : null
);

/** Why the ingest button is disabled, or null when it is not. */
const blocked = computed<string | null>(() => {
  if (submitting.value) return "Ingesting…";
  if (problems.value.length) return "Fix the problems above first.";
  if (mode.value === "file") {
    if (!file.value) return "Choose a file.";
    if (analyzing.value) return "Analysing…";
    if (!planFresh.value) return "The request changed since it was analysed. Analyse it again.";
    return null;
  }
  if (!pipeline.value) return "Choose an entry point.";
  if (contentProblem.value) return `${contentProblem.value[0].toUpperCase()}${contentProblem.value.slice(1)}.`;
  return null;
});

const goLabel = computed(() => {
  if (mode.value === "entry") return `Ingest through ${usetype.value || "…"}`;
  const a = analysis.value;
  if (!a) return "Ingest";
  if (a.already_stored) return "Ingest — resolves to the existing node";
  if (a.probe_failed) return "Ingest anyway — probe will fail";
  const n = a.plan ? enqueuedCount(a.plan) : 0;
  return `Ingest — stores ${bytes(a.file.byte_size)}, enqueues ${n} task${n === 1 ? "" : "s"}`;
});

async function submit() {
  if (blocked.value) return;
  submitting.value = true;
  submitError.value = null;
  upload.value = null;
  entryResult.value = null;
  try {
    if (mode.value === "file") {
      const landed = await client.upload_file({
        file: file.value as File,
        options: parsed.value.options ?? undefined,
        parent_id: parent.id ?? undefined,
        private: isPrivate.value,
      });
      upload.value = landed;
      watchedPlan.value = analysis.value?.plan ?? null;
      startWatching(landed.document_id, landed.was_existing ?? false);
    } else {
      const made = await client.ingest_content({
        request: {
          content: content.value,
          usetype: usetype.value,
          title: title.value.trim() || null,
          parent_id: parent.id,
          options: parsed.value.options as Record<string, Record<string, unknown>> | null,
        },
      });
      entryResult.value = made;
      watchedPlan.value = planFresh.value ? explained.value : null;
      startWatching(
        made.was_existing && made.existing_document_id !== null && made.existing_document_id !== undefined
          ? made.existing_document_id
          : made.source_document_id,
        made.was_existing ?? false
      );
    }
  } catch (failure) {
    submitError.value = errorOf(failure);
  } finally {
    submitting.value = false;
  }
}

// ------------------------------------------------------------------------------- watching

const watching = ref<number | null>(null);
const watchedExisting = ref(false);
const watchedPlan = ref<ExplainIngestResponse | null>(null);
const frontier = ref<IngestFrontierResponse | null>(null);
const evidence = ref<Record<string, unknown> | null>(null);
const watchError = ref<string | null>(null);
const polling = ref(false);
// The loop itself is `lib/poller.ts`, which owns the timer and a generation counter, so that a
// read still in flight when the page unmounts or re-watches is disowned rather than allowed to
// schedule a successor. This page keeps only the refs it draws.
const poller = createPoller({
  read: async (id: number) => {
    const [f, e] = await Promise.all([
      client.file_frontier({ document_id: id }),
      client.get_document_evidence({ document_id: id }),
    ]);
    return { frontier: f, evidence: e.evidence };
  },
  isTerminal: (reading) => TERMINAL.has(ingestionState(reading.frontier).status),
  intervalMs: POLL_MS,
  onBegin: () => {
    polling.value = true;
    watchError.value = null;
  },
  onResult: (_id, reading) => {
    frontier.value = reading.frontier;
    evidence.value = reading.evidence;
  },
  // Shown and stopped. A reading that failed once is not retried behind the reader's back;
  // "Ask again" is one click and the reason stays on the page.
  onError: (_id, failure) => {
    watchError.value = errorOf(failure);
  },
  onIdle: () => {
    polling.value = false;
  },
});

function startWatching(documentId: number, existing: boolean) {
  watching.value = documentId;
  watchedExisting.value = existing;
  frontier.value = null;
  evidence.value = null;
  poller.start(documentId);
}

/** "Ask again": the same watch, read now, keeping what is on the page until the answer lands. */
function askAgain() {
  if (watching.value !== null) poller.start(watching.value);
}

onBeforeUnmount(() => poller.stop());

const ingestion = computed(() =>
  watching.value === null ? null : ingestionState(frontier.value, watchedExisting.value)
);

const attempts = computed(() => {
  try {
    return { list: attemptsFrom(evidence.value), problem: null as string | null };
  } catch (failure) {
    return { list: [], problem: errorOf(failure) };
  }
});

/** The rungs: the forecast before anything is ingested, the live states after. */
const rows = computed(() => {
  try {
    const live = watching.value !== null;
    return {
      list: rungs(live ? watchedPlan.value : plan.value, live ? attempts.value.list : [], live ? watching.value : null),
      problem: null as string | null,
    };
  } catch (failure) {
    return { list: [], problem: errorOf(failure) };
  }
});

const STATE_WORDS: Record<RungState, { word: string; tone: Tone; meaning: string }> = {
  waiting: { word: "will run", tone: "plain", meaning: "Planned; no queue row for it yet." },
  pending: { word: "pending", tone: "active", meaning: "Queued; no worker has claimed it." },
  running: { word: "running", tone: "active", meaning: "A worker is running it now." },
  done: { word: "done", tone: "plain", meaning: "Completed." },
  skipped: { word: "skipped", tone: "quiet", meaning: "Ran, and recorded that it had nothing to do." },
  failed: { word: "failed", tone: "alarm", meaning: "Failed. The error is the attempt log's, verbatim." },
  not_applicable: { word: "not applicable", tone: "quiet", meaning: "Will not run; the reason is the plan's." },
  on_children: {
    word: "on children",
    tone: "plain",
    meaning: "Runs once per child node; its attempts are on those nodes, and the counts above cover them.",
  },
};

// ---------------------------------------------------------------- lifecycle and wiring

watch([baseUrl, token], loadAbout, { immediate: true });

// The two modes resolve options from different answers, so an edit made against one is not
// carried into the other.
watch(mode, () => {
  submitError.value = null;
  resetAll();
});

/** `#/ingest?document=42` watches an ingestion this page did not start. */
watch(
  () => route.query.document,
  (raw) => {
    if (typeof raw !== "string") return;
    if (!/^\d+$/.test(raw)) {
      watchError.value = `?document= takes a document id; "${raw}" is not one`;
      return;
    }
    watchedPlan.value = null;
    startWatching(Number(raw), false);
  },
  { immediate: true }
);
</script>

<template>
  <div class="ingest scroll-pane">
    <header class="head">
      <h1>Ingest</h1>
      <p class="line">
        See what the appliance will do with a document before it does it, then watch it settle.
        Nothing is stored until you press the ingest button.
      </p>
    </header>

    <div v-if="aboutError" class="failed">
      <h2>The appliance did not say what it accepts</h2>
      <p>{{ aboutError }}</p>
      <p class="apparatus-voice">A 401 here means the token is missing or wrong.</p>
    </div>

    <Panel
      v-if="capabilities"
      title="What this appliance accepts"
      :note="`GET /capabilities · ${capabilities.version}`"
      :quiet="true"
    >
      <div class="accepts">
        <Fact label="Identified from the bytes" :value="capabilities.ingest.detectable_formats.join(' ')">
          <span class="tags"><Tag v-for="f in capabilities.ingest.detectable_formats" :key="f" :value="f" /></span>
        </Fact>
        <Fact label="Looked inside" :value="capabilities.ingest.probeable_formats.join(' ')">
          <span class="tags"><Tag v-for="f in capabilities.ingest.probeable_formats" :key="f" :value="f" /></span>
        </Fact>
        <Fact label="Entry points" :value="capabilities.ingest.usetypes.join(' ')">
          <span class="tags"><Tag v-for="u in capabilities.ingest.usetypes" :key="u" :value="u" /></span>
        </Fact>
      </div>
      <p v-if="!capabilities.embedding.can_embed" class="warn">
        This appliance cannot produce a vector: no local model and no runner. Embedding tasks
        will fail until one is configured.
      </p>
      <p v-for="extra in missingExtras" :key="extra.name" class="apparatus-voice">
        <span class="value">{{ extra.name }}</span> is not installed — {{ extra.provides }}.
        <code>{{ extra.install }}</code>
      </p>
    </Panel>

    <nav class="modes" aria-label="What to ingest">
      <button type="button" :class="{ on: mode === 'file' }" @click="mode = 'file'">A file</button>
      <button type="button" :class="{ on: mode === 'entry' }" @click="mode = 'entry'">
        Text, a URL or an arXiv id
      </button>
    </nav>

    <form class="request" @submit.prevent="submit">
      <!-- ---------------------------------------------------------------- the file -->
      <template v-if="mode === 'file'">
        <label
          class="drop"
          :class="{ over: dragging, busy: analyzing }"
          @dragover.prevent="dragging = true"
          @dragenter.prevent="dragging = true"
          @dragleave="dragging = false"
          @drop.prevent="onDrop"
        >
          <span v-if="file" class="chosen">
            <span class="value">{{ file.name }}</span> · {{ bytes(file.size) }}
          </span>
          <span v-else>Drop a file here, or choose one.</span>
          <span class="apparatus-voice">
            Choosing a file runs <code>POST /ingest/analyze</code>: probe reads the bytes and stops.
            No document, no blob, no queue row.
          </span>
          <input type="file" class="file" aria-label="File to ingest" @change="onPick" />
        </label>
      </template>

      <!-- ------------------------------------------------------------- an entry point -->
      <template v-else>
        <div class="row">
          <label class="field">
            <span class="label">Entry point</span>
            <select v-model="usetype" class="value">
              <option v-for="p in pipelines" :key="p.name" :value="p.name">{{ p.name }}</option>
            </select>
          </label>
          <label class="field grow">
            <span class="label">Title</span>
            <input v-model="title" type="text" placeholder="optional" />
          </label>
        </div>
        <p v-if="pipeline" class="apparatus-voice">
          {{ pipeline.description }} Takes {{ SOURCE_NOUNS[source] ?? source }}.
          <code>POST /ingest</code> is synchronous: it drains the document's tasks before it answers.
        </p>
        <label class="field">
          <span class="label">{{ SOURCE_NOUNS[source] ?? "content" }}</span>
          <textarea
            v-if="source === 'content'"
            v-model="content"
            rows="8"
            placeholder="Text to ingest"
          />
          <input
            v-else
            v-model="content"
            type="text"
            class="value"
            :placeholder="source === 'url' ? 'https://…' : source === 'arxiv' ? '2401.00001' : ''"
          />
        </label>
      </template>

      <!-- --------------------------------------------------------- where it goes -->
      <div class="row">
        <label class="field">
          <span class="label">Parent node</span>
          <input
            v-model="parentText"
            type="text"
            inputmode="numeric"
            class="value narrow"
            placeholder="none"
            @change="resolveParent"
          />
        </label>
        <label v-if="mode === 'file'" class="check">
          <input v-model="isPrivate" type="checkbox" />
          <span>private</span>
        </label>
        <p class="apparatus-voice where">
          <template v-if="parent.doc">
            Under <RouterLink :to="`/node/${parent.doc.id}`">{{ parent.doc.title ?? `node ${parent.doc.id}` }}</RouterLink>
            <template v-if="parent.options && Object.keys(parent.options.effective).length">
              , which asks for <code>{{ JSON.stringify(parent.options.effective) }}</code>
            </template>.
          </template>
          <template v-else-if="!parent.problem">A new tree, at the top of the corpus.</template>
          <template v-if="mode === 'file'">
            Readable by every token holder unless it lands in a governed subtree; <em>private</em>
            makes it its own access-control root.
          </template>
        </p>
      </div>

      <!-- ------------------------------------------------------------ advanced options -->
      <details class="advanced" :open="showAdvanced" @toggle="showAdvanced = ($event.target as HTMLDetailsElement).open">
        <summary>
          Options
          <span class="apparatus-voice">
            <template v-if="resolved">{{ fields.length }} options, as the appliance resolved them</template>
            <template v-else-if="mode === 'file'">— shown once a file is analysed</template>
          </span>
        </summary>
        <template v-if="resolved">
          <p class="apparatus-voice">
            Each value is what this request resolves to now. Change one and the plan above it is stale
            until it is asked again.
          </p>
          <div v-for="f in fields" :key="f.key" class="option" :class="{ edited: f.key in edits }">
            <label :for="`opt-${f.key}`" class="value key">{{ f.key }}</label>
            <select
              v-if="f.kind === 'boolean'"
              :id="`opt-${f.key}`"
              class="value"
              :value="fieldValue(f.key, f.resolved)"
              @change="setField(f.key, ($event.target as HTMLSelectElement).value)"
            >
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
            <input
              v-else
              :id="`opt-${f.key}`"
              class="value"
              type="text"
              :inputmode="f.kind === 'number' ? 'decimal' : undefined"
              :value="fieldValue(f.key, f.resolved)"
              @input="setField(f.key, ($event.target as HTMLInputElement).value)"
            />
            <button v-if="f.key in edits" type="button" class="ghost small" @click="resetField(f.key)">
              reset
            </button>
            <span v-if="fieldProblem(f.key, f.kind)" class="bad small">{{ fieldProblem(f.key, f.kind) }}</span>
          </div>
        </template>
        <label class="field">
          <span class="label">Or paste options as JSON</span>
          <textarea
            v-model="jsonText"
            rows="3"
            class="value"
            placeholder='{"structure": {"max_words": 60}}'
          />
        </label>
      </details>

      <ul v-if="problems.length" class="problems">
        <li v-for="p in problems" :key="p">{{ p }}</li>
      </ul>

      <div class="actions">
        <button
          v-if="mode === 'file'"
          type="button"
          class="ghost"
          :disabled="!file || analyzing || problems.length > 0 || planFresh"
          @click="analyze"
        >
          {{ analyzing ? "Analysing…" : planFresh ? "Analysed" : "Analyse again" }}
        </button>
        <button
          v-else-if="source === 'content'"
          type="button"
          class="ghost"
          :disabled="explaining || problems.length > 0 || planFresh"
          @click="explainEntry"
        >
          {{ explaining ? "Explaining…" : planFresh ? "Explained" : "Explain the plan" }}
        </button>
        <button type="submit" :disabled="blocked !== null">{{ goLabel }}</button>
        <span v-if="blocked && !submitting" class="apparatus-voice">{{ blocked }}</span>
      </div>
      <p v-if="analyzeError" class="bad">{{ analyzeError }}</p>
      <p v-if="explainError" class="bad">{{ explainError }}</p>
      <p v-if="submitError" class="bad">{{ submitError }}</p>
    </form>

    <!-- ---------------------------------------------------------------- what was measured -->
    <Panel
      v-if="mode === 'file' && analysis"
      title="What the bytes are"
      :note="planFresh ? 'POST /ingest/analyze' : 'stale — the request changed'"
    >
      <div class="facts">
        <Fact label="File" :value="`${analysis.file.filename} · ${bytes(analysis.file.byte_size)}`" />
        <Fact label="Format" :value="analysis.format" />
        <Fact
          label="Detected by"
          :value="analysis.file.detected_by ?? 'nothing — the extension is the only hint'"
        />
        <Fact label="Mime type" :value="analysis.file.detected_mime ?? 'unrecognised'">
          <template v-if="analysis.file.mime_agrees === null">
            {{ analysis.file.detected_mime ?? "unrecognised" }} (declared and detected cannot be compared)
          </template>
          <span v-else-if="analysis.file.mime_agrees">{{ analysis.file.detected_mime }}</span>
          <span v-else class="bad">
            the bytes are {{ analysis.file.detected_mime ?? "unrecognised" }}, the browser said
            {{ analysis.file.declared_mime }}
          </span>
        </Fact>
        <Fact label="Content hash" :value="analysis.file.content_hash" />
        <Fact label="Patterns measured" :value="Object.keys(analysis.patterns).join(' ')">
          <span class="tags">
            <Tag
              v-for="[p, v] in Object.entries(analysis.patterns)"
              :key="p"
              :value="`${p} ${JSON.stringify(v)}`"
              :tone="v === false ? 'quiet' : 'plain'"
            />
          </span>
        </Fact>
      </div>
      <p v-if="nameCheck && nameCheck.verdict !== 'agrees'" :class="nameCheck.verdict === 'disagrees' ? 'warn' : 'apparatus-voice'">
        {{ nameCheck.message }}
      </p>
      <p v-if="analysis.already_stored" class="notice">
        These bytes are already stored as
        <RouterLink :to="`/node/${analysis.already_stored.document_id}`">node {{ analysis.already_stored.document_id }}</RouterLink>
        ({{ analysis.already_stored.settled }}). Ingesting them again resolves to that node and runs no plan.
      </p>
      <p v-if="analysis.probe_failed" class="warn">
        Probe would fail on these bytes, so there is no plan:
        <code>{{ analysis.probe_failed.error }}</code> — classified {{ analysis.probe_failed.error_type }}.
      </p>
    </Panel>

    <!-- ------------------------------------------------------------- the ingestion's state -->
    <Panel v-if="watching !== null" title="This ingestion" :note="`node ${watching}`">
      <div v-if="ingestion" class="status" :class="ingestion.status">
        <strong>{{ ingestion.headline }}</strong>
        <p>{{ ingestion.note }}</p>
      </div>
      <div class="facts">
        <template v-if="upload">
          <Fact label="Stored" :value="`${upload.filename} · ${bytes(upload.byte_size)} · ${upload.detected_mime ?? 'unrecognised bytes'}`" />
          <Fact
            label="Governed"
            :value="upload.governed ? 'yes — an access-control root sits at or above it' : 'no — readable and writable by any token holder'"
          />
          <Fact
            v-if="upload.linked_into_parent"
            label="Placement"
            value="linked under the parent by a contains link, not by parentage"
          />
        </template>
        <template v-if="entryResult">
          <Fact label="Title" :value="entryResult.title" />
          <Fact label="Entry point" :value="entryResult.usetype" />
          <Fact label="Tree depth" :value="entryResult.tree_depth" />
          <Fact
            v-if="entryResult.was_existing"
            label="Already here"
            :value="`as node ${entryResult.existing_document_id}; nothing was ingested`"
          />
        </template>
        <template v-if="frontier">
          <Fact label="Nodes settled" :value="frontier.nodes_settled" />
          <Fact label="Nodes in flight" :value="frontier.nodes_in_flight" />
          <Fact label="Nodes failed" :value="frontier.nodes_failed" />
          <Fact label="Nodes total" :value="frontier.nodes_total" />
          <Fact label="Tasks unfinished" :value="frontier.tasks_unfinished" />
          <Fact label="Root node" :value="frontier.settled" meaning="The root's own state, which is not the subtree's." />
        </template>
      </div>
      <p class="apparatus-voice">
        <template v-if="polling">Asking the frontier and the attempt log every {{ POLL_MS / 1000 }} s until nothing more will happen.</template>
        <template v-else>Not polling.</template>
        Counts, never a percentage: the tasks below the frontier do not exist yet.
      </p>
      <p v-if="watchError" class="bad">{{ watchError }}</p>
      <div class="actions">
        <button v-if="!polling" type="button" class="ghost" @click="askAgain">Ask again</button>
        <template v-if="ingestion && ingestion.status === 'settled'">
          <RouterLink :to="`/node/${watching}`" class="go">Open node {{ watching }}</RouterLink>
          <RouterLink to="/" class="go">The corpus</RouterLink>
        </template>
        <RouterLink v-else-if="ingestion && ingestion.status === 'failed'" :to="`/node/${watching}`" class="go">
          Inspect node {{ watching }}
        </RouterLink>
      </div>
      <ul v-if="entryResult && entryResult.stages.length" class="stages">
        <li v-for="s in entryResult.stages" :key="s.stage">
          <span class="value">{{ s.stage }}</span> <Tag :value="s.status" :tone="s.status === 'failed' ? 'alarm' : 'plain'" />
          <code v-if="s.error" class="error">{{ s.error }}</code>
        </li>
      </ul>
    </Panel>

    <!-- --------------------------------------------------------------------- the rungs -->
    <Panel
      v-if="plan || (watching !== null && rows.list.length)"
      :title="watching !== null ? 'Task by task' : 'What will happen'"
      :note="watching !== null ? 'plan + GET /documents/{id}/evidence' : planFresh ? 'nothing has been stored' : 'stale — the request changed'"
    >
      <p v-if="plan && watching === null" class="apparatus-voice">
        {{ plan.patterns_known ? "Every task below is decided" : "Some tasks below are undecided" }}:
        patterns {{ plan.patterns_source }} — {{ PATTERN_SOURCES[plan.patterns_source] ?? "no gloss for this source" }}.
        {{ countRange(plan.cost.tasks) }} task instances, {{ countRange(plan.cost.nodes) }} nodes written.
      </p>
      <p v-if="watching !== null && !watchedPlan" class="apparatus-voice">
        No plan for this ingestion is on the page, so these rows are the root's attempt log alone.
      </p>
      <p class="tally">
        <span v-for="[state, n] in tally(rows.list)" :key="state">
          <Tag :value="`${n} ${STATE_WORDS[state].word}`" :tone="STATE_WORDS[state].tone" />
        </span>
      </p>
      <p v-if="rows.problem" class="bad">{{ rows.problem }}</p>
      <p v-if="attempts.problem" class="bad">{{ attempts.problem }}</p>
      <table class="rungs">
        <thead>
          <tr>
            <th>Task</th>
            <th>State</th>
            <th>Scope</th>
            <th>Why, and what it writes</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows.list" :key="`${r.task}|${r.scope}`" :class="r.state">
            <td class="value">{{ r.task }}</td>
            <td>
              <Tag :value="STATE_WORDS[r.state].word" :tone="STATE_WORDS[r.state].tone" :meaning="STATE_WORDS[r.state].meaning" />
              <span v-if="r.outcome" class="sub value" :title="OUTCOMES[r.outcome]">{{ r.outcome }}</span>
              <span v-if="r.unplanned" class="sub">not in the plan</span>
              <span v-if="r.attempt && r.attempt > 1" class="sub">attempt {{ r.attempt }}</span>
            </td>
            <td class="value">
              {{ r.scope ?? "—" }}
              <span v-if="r.writeMode" class="sub">writes {{ r.writeMode }}</span>
            </td>
            <td>
              <span v-if="r.reason">{{ r.reason }}</span>
              <span v-if="r.expects" class="sub">expects {{ r.expects }}</span>
              <span v-if="r.produced !== null" class="sub">produced {{ r.produced }} node{{ r.produced === 1 ? "" : "s" }}</span>
              <code v-if="r.error" class="error">{{ r.error }}<template v-if="r.errorType"> [{{ r.errorType }}]</template></code>
            </td>
          </tr>
        </tbody>
      </table>
    </Panel>
  </div>
</template>

<style scoped>
.ingest {
  padding: var(--s6) var(--s6) var(--s7);
  max-width: 64rem;
}

.head {
  margin-bottom: var(--s5);
}

.head h1 {
  font-size: var(--t-display);
  font-weight: 600;
  letter-spacing: -0.015em;
}

.head .line {
  margin: var(--s2) 0 0;
  color: var(--ink-soft);
  max-width: var(--measure);
}

.failed {
  padding: var(--s4);
  border-left: 2px solid var(--alarm);
  background: var(--alarm-wash);
  max-width: var(--measure);
  margin-bottom: var(--s5);
}

.accepts,
.facts {
  display: grid;
  gap: var(--s2);
  margin-bottom: var(--s3);
}

.tags {
  display: inline-flex;
  flex-wrap: wrap;
  gap: var(--s1);
}

.modes {
  display: flex;
  gap: var(--s1);
  margin-bottom: var(--s4);
  border-bottom: 1px solid var(--rule);
}

.modes button {
  padding: var(--s2) var(--s3);
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--ink-soft);
  cursor: pointer;
  margin-bottom: -1px;
}

.modes button.on {
  color: var(--ink);
  border-bottom-color: var(--accent);
}

.request {
  display: grid;
  gap: var(--s3);
  margin-bottom: var(--s6);
}

.drop {
  display: grid;
  gap: var(--s2);
  padding: var(--s5);
  border: 1px dashed var(--rule);
  border-radius: var(--r);
  background: var(--leaf);
  cursor: pointer;
}

.drop.over {
  border-color: var(--accent);
  background: var(--accent-wash);
}

.drop.busy {
  opacity: 0.7;
}

.file {
  font-size: var(--t-small);
}

.row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s4);
  align-items: flex-end;
}

.field {
  display: grid;
  gap: var(--s1);
}

.field.grow {
  flex: 1;
  min-width: 12rem;
}

.label {
  font-size: var(--t-small);
  color: var(--ink-faint);
}

input[type="text"],
select,
textarea {
  padding: 5px var(--s2);
  border: 1px solid var(--rule);
  border-radius: var(--r);
  background: var(--paper);
  color: var(--ink);
  font: inherit;
  font-size: var(--t-small);
}

textarea {
  width: 100%;
  resize: vertical;
}

input.narrow {
  width: 8rem;
}

input:focus,
select:focus,
textarea:focus {
  border-color: var(--accent);
}

.check {
  display: flex;
  gap: var(--s1);
  align-items: center;
  font-size: var(--t-small);
  padding-bottom: 5px;
}

.where {
  flex: 1;
  min-width: 14rem;
  margin: 0;
}

.advanced {
  border-top: 1px solid var(--rule-soft);
  padding-top: var(--s2);
}

.advanced summary {
  cursor: pointer;
  font-size: var(--t-small);
}

.option {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s2);
  align-items: center;
  padding: 3px 0;
}

.option .key {
  width: 16rem;
  color: var(--ink-soft);
}

.option.edited .key {
  color: var(--accent);
}

.option input {
  width: 14rem;
}

.problems {
  margin: 0;
  padding: var(--s3) var(--s4) var(--s3) var(--s6);
  border-left: 2px solid var(--alarm);
  background: var(--alarm-wash);
  color: var(--alarm);
  font-size: var(--t-small);
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s3);
  align-items: center;
}

button {
  padding: 5px var(--s3);
  border: 1px solid var(--accent);
  border-radius: var(--r);
  background: var(--accent);
  color: var(--leaf);
  font-size: var(--t-small);
  cursor: pointer;
}

button.ghost {
  background: transparent;
  border-color: var(--rule);
  color: var(--ink-soft);
}

button.small {
  padding: 1px var(--s2);
  font-size: var(--t-micro);
}

button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.go {
  font-size: var(--t-small);
}

.bad {
  color: var(--alarm);
}

.small {
  font-size: var(--t-micro);
}

.warn,
.notice {
  padding: var(--s2) var(--s3);
  border-left: 2px solid var(--alarm);
  background: var(--alarm-wash);
  font-size: var(--t-small);
  max-width: none;
}

.notice {
  border-left-color: var(--accent);
  background: var(--accent-wash);
}

.status {
  padding: var(--s3) var(--s4);
  border-left: 2px solid var(--accent);
  background: var(--accent-wash);
  margin-bottom: var(--s3);
}

.status.settled {
  border-left-color: var(--rule);
  background: var(--sunk);
}

.status.failed,
.status.stalled {
  border-left-color: var(--alarm);
  background: var(--alarm-wash);
}

.status p {
  margin: var(--s1) 0 0;
  font-size: var(--t-small);
  color: var(--ink-soft);
}

.tally {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s2);
}

.rungs {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--t-small);
}

.rungs th {
  text-align: left;
  font-weight: 400;
  color: var(--ink-faint);
  border-bottom: 1px solid var(--rule);
  padding: var(--s1) var(--s2);
}

.rungs td {
  vertical-align: top;
  padding: var(--s2);
  border-bottom: 1px solid var(--rule-soft);
}

.rungs tr.not_applicable td,
.rungs tr.skipped td {
  color: var(--ink-soft);
}

.sub {
  display: block;
  margin-top: 2px;
  font-size: var(--t-micro);
  color: var(--ink-faint);
}

.error {
  display: block;
  margin-top: var(--s1);
  padding: var(--s1) var(--s2);
  background: var(--alarm-wash);
  color: var(--alarm);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.stages {
  list-style: none;
  padding: 0;
  font-size: var(--t-small);
}

@media (max-width: 640px) {
  .ingest {
    padding: var(--s4);
  }
  .option .key,
  .option input {
    width: 100%;
  }
  .rungs th:nth-child(3),
  .rungs td:nth-child(3) {
    display: none;
  }
}
</style>
