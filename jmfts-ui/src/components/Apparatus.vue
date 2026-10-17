<script setup lang="ts">
// The apparatus: how this node came to be, beside the node itself.
//
// Every field here is one the API volunteers. JMFTS does not return bare answers — it returns
// an answer and how it was reached, and it does that consistently enough that a console can
// have one component for the pattern: `produced_by` (which rule wrote this), `settled` (whether
// it is an answer yet), `content_source` (whether the text is stored or was just computed),
// the presentation row that chose the renderer, and the evidence the ingest run recorded.
//
// NOTHING HERE IS BEHIND A DISCLOSURE TRIANGLE except the raw JSON of an evidence row. Each row
// states what it is and its headline value in the open; the triangle only holds the full record
// for someone who wants to read it.

import { computed } from "vue";
import Fact from "./Fact.vue";
import HandMark from "./HandMark.vue";
import Tag from "./Tag.vue";
import type { DocumentResponse, ViewPresentation } from "@/api";
import { glossContentSource, glossEvidence, glossLifecycle, handOf, when } from "@/lib/vocab";
import { clip } from "@/lib/text";

const props = defineProps<{
  node: DocumentResponse;
  presentation: ViewPresentation;
  evidence: Record<string, unknown> | null;
  evidenceError: string | null;
}>();

const hand = computed(() => handOf(props.node.produced_by));

// `settled` and `content_source` carry a server-side default, so the generator types them
// optional and an absent one means the server sent its default rather than that the fact is
// unknown. That is why these two get a fallback and `produced_by` does not: NULL there is an
// answer — a person asserted this node — not a field the response left out.
const settled = computed(() => props.node.settled ?? "settled");
const contentSource = computed(() => props.node.content_source ?? "stored");
const lifecycle = computed(() => glossLifecycle(settled.value));
const source = computed(() => glossContentSource(contentSource.value));
const created = computed(() => when(props.node.created_at));
const updated = computed(() => when(props.node.updated_at));
const eventTime = computed(() => when(props.node.event_time));

/** Evidence rows in a fixed order, so two nodes read the same way. */
const ORDER = [
  "file",
  "matched",
  "extraction",
  "structure",
  "effective_content",
  "source_anchor",
  "source_anchor.unresolved",
  "render_anchor",
  "rendition",
  "options",
  "attempts",
];

const rows = computed(() => {
  if (!props.evidence) return [];
  const keys = Object.keys(props.evidence);
  keys.sort((a, b) => {
    const ia = ORDER.indexOf(a);
    const ib = ORDER.indexOf(b);
    return (ia === -1 ? ORDER.length : ia) - (ib === -1 ? ORDER.length : ib) || a.localeCompare(b);
  });
  return keys.map((key) => {
    const value = props.evidence![key];
    return {
      key,
      value,
      gloss: glossEvidence(key)?.meaning ?? null,
      headline: headline(value),
      // A scalar IS its full record, so it gets no triangle to open. Only a structure has
      // something behind the summary.
      compound: value !== null && typeof value === "object",
    };
  });
});

/** One line that says what a row holds, without making the reader open it. */
function headline(value: unknown): string {
  // A name present with a null value means the rule ran and produced nothing, which is a
  // different fact from the name being absent. The apparatus says which.
  if (value === null) return "ran, and produced nothing";
  if (Array.isArray(value)) return `${value.length} recorded`;
  if (typeof value === "object") {
    const scalars = Object.entries(value as Record<string, unknown>).filter(
      ([, v]) => v !== null && v !== "" && typeof v !== "object"
    );
    const nested = Object.entries(value as Record<string, unknown>).filter(
      ([, v]) => v !== null && typeof v === "object"
    );
    const said = scalars.slice(0, 3).map(([k, v]) => `${k} ${clip(String(v), 28)}`);
    if (nested.length > 0) said.push(nested.map(([k]) => k).join(" "));
    return clip(said.join("  ·  "), 96);
  }
  return clip(String(value), 96);
}

const pretty = (value: unknown) => JSON.stringify(value, null, 2);

/** The ingest tasks that ran, when the `attempts` row is present. */
const attempts = computed(() => {
  const value = props.evidence?.attempts;
  if (!Array.isArray(value)) return [];
  return value as { task?: string; rung?: string | null; error?: string | null }[];
});
</script>

<template>
  <aside class="apparatus scroll-pane">
    <div class="group">
      <h3>How this node got here</h3>
      <div class="lines">
        <Fact label="written by" :meaning="hand.meaning" value=" ">
          <HandMark :produced-by="node.produced_by" />
        </Fact>
        <Fact label="state" :meaning="lifecycle?.meaning ?? null" :value="settled">
          <Tag :value="settled" :tone="lifecycle?.tone ?? 'plain'" :meaning="lifecycle?.meaning ?? null" />
        </Fact>
        <Fact label="text" :meaning="source?.meaning ?? null" :value="contentSource">
          <Tag
            :value="contentSource"
            :tone="source?.tone ?? 'plain'"
            :meaning="source?.meaning ?? null"
          />
        </Fact>
        <Fact label="depth" :value="node.depth" meaning="How far below a root this node sits." />
        <Fact
          label="position"
          :value="node.position"
          meaning="Its place among its siblings. Absent when the siblings are unordered."
        />
      </div>
      <p v-if="contentSource === 'effective'" class="aside-note">
        The text on the left is not stored anywhere. It was joined from this node's children when
        you asked for it, and the node's embedding — not its text — is what search matched.
      </p>
    </div>

    <div class="group">
      <h3>How you are being shown it</h3>
      <div class="lines">
        <Fact
          label="renderer"
          :value="presentation.renderer"
          meaning="Chosen by the appliance from this usetype's presentation row, not by this console."
        />
        <Fact label="children" :value="presentation.child_handling" />
        <Fact label="relations" :value="presentation.link_handling" />
      </div>
    </div>

    <div class="group">
      <h3>When</h3>
      <div class="lines">
        <Fact label="created" :value="created?.text ?? null" :meaning="created?.exact ?? null" />
        <Fact label="updated" :value="updated?.text ?? null" :meaning="updated?.exact ?? null" />
        <Fact
          label="happened"
          :value="eventTime?.text ?? null"
          :meaning="eventTime?.exact ?? 'Domain time. Absent means the content was authored in place.'"
        />
      </div>
    </div>

    <div class="group">
      <h3>What the ingest run recorded</h3>

      <p v-if="evidenceError" class="failed">{{ evidenceError }}</p>

      <p v-else-if="rows.length === 0" class="aside-note">
        Nothing. No ingest rule ran on this node, which is what you would expect of a document
        created through the API rather than uploaded.
      </p>

      <div v-else class="evidence">
        <div v-for="row in rows" :key="row.key" class="row">
          <div class="row-head">
            <span class="key value">{{ row.key }}</span>
            <span class="headline value">{{ row.headline }}</span>
          </div>
          <p v-if="row.gloss" class="gloss">{{ row.gloss }}</p>
          <ol v-if="row.key === 'attempts'" class="attempts">
            <li v-for="(a, i) in attempts" :key="i" :class="{ failed: a.error }">
              <span class="value">{{ a.task }}</span>
              <span v-if="a.rung" class="rung value">{{ a.rung }}</span>
              <span v-if="a.error" class="err">{{ a.error }}</span>
            </li>
          </ol>
          <details v-else-if="row.compound">
            <summary>Full record</summary>
            <pre class="value">{{ pretty(row.value) }}</pre>
          </details>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.apparatus {
  padding: var(--s5) var(--s5) var(--s7) var(--s5);
  border-left: 1px solid var(--rule);
  font-size: var(--t-small);
  color: var(--ink-soft);
}

.group {
  margin-bottom: var(--s6);
}

h3 {
  margin-bottom: var(--s3);
  padding-bottom: var(--s2);
  border-bottom: 1px solid var(--rule-soft);
  font-size: var(--t-small);
  font-weight: 600;
  color: var(--ink);
}

.lines {
  display: grid;
  gap: var(--s2);
}

.aside-note {
  margin: var(--s3) 0 0;
  padding-left: var(--s3);
  border-left: 1px solid var(--rule);
  font-size: var(--t-small);
  font-style: italic;
  color: var(--ink-soft);
  max-width: none;
}

.evidence {
  display: grid;
  gap: var(--s4);
}

.row-head {
  display: flex;
  gap: var(--s2);
  align-items: baseline;
  flex-wrap: wrap;
}

.key {
  color: var(--ink);
  font-size: var(--t-micro);
}

.headline {
  color: var(--ink-faint);
  font-size: var(--t-micro);
  overflow-wrap: anywhere;
}

.gloss {
  margin: 2px 0 var(--s1);
  font-size: var(--t-micro);
  line-height: 1.45;
  color: var(--ink-soft);
  max-width: none;
}

details summary {
  cursor: pointer;
  color: var(--accent);
  font-size: var(--t-micro);
}

details pre {
  margin: var(--s2) 0 0;
  padding: var(--s2);
  background: var(--sunk);
  overflow-x: auto;
  font-size: var(--t-micro);
  line-height: 1.45;
}

.attempts {
  margin: var(--s1) 0 0;
  padding-left: 1.2em;
  font-size: var(--t-micro);
}

.attempts li {
  margin-bottom: 2px;
}

.rung {
  color: var(--ink-faint);
  margin-left: var(--s2);
}

.err {
  display: block;
  color: var(--alarm);
}

.failed {
  padding: var(--s2) var(--s3);
  border-left: 2px solid var(--alarm);
  background: var(--alarm-wash);
  color: var(--ink);
  max-width: none;
}
</style>
