<script setup lang="ts">
// The content, drawn the way the APPLIANCE said to draw it.
//
// `presentation.renderer` is a value the database hands the page — a row of
// `usetype_presentations`, delivered on every `GET /view/{id}`. So there is no branch here on
// usetype and no component named after one: adding a renderer is a row and a migration that
// every client picks up, and this file holds one arm per value of the server's own tuple.
//
// A renderer NAMED BY THE SERVER AND ABSENT HERE says so on the page. It means this console is
// older than the appliance it is pointed at, which is a real thing to find out and not
// something to paper over with preformatted text.

import { computed } from "vue";
import RenderMarkdown from "./RenderMarkdown.vue";
import { parseTranscript } from "@/lib/markdown";

const props = defineProps<{
  content: string;
  renderer: string;
  config?: Record<string, unknown>;
}>();

const KNOWN = ["markdown", "plain", "code", "json-table", "transcript"];
const known = computed(() => KNOWN.includes(props.renderer));

const language = computed(() => {
  const value = props.config?.language;
  return typeof value === "string" && value ? value : null;
});

const turns = computed(() => (props.renderer === "transcript" ? parseTranscript(props.content) : []));

/** A table out of JSON, or the reason there is not one. Never a silently empty table. */
const table = computed<{ columns: string[]; rows: Record<string, unknown>[] } | { error: string }>(
  () => {
    if (props.renderer !== "json-table") return { columns: [], rows: [] };
    let parsed: unknown;
    try {
      parsed = JSON.parse(props.content);
    } catch (error) {
      return {
        error:
          `This node's presentation says json-table, and its content is not JSON: ` +
          `${(error as Error).message}. Either the content or the presentation row is wrong.`,
      };
    }
    const rows = Array.isArray(parsed) ? parsed : [parsed];
    const columns: string[] = [];
    for (const row of rows) {
      if (row === null || typeof row !== "object" || Array.isArray(row)) {
        return { error: "json-table needs objects to make rows from; this content holds " +
          `${Array.isArray(row) ? "lists" : typeof row}.` };
      }
      for (const key of Object.keys(row)) if (!columns.includes(key)) columns.push(key);
    }
    return { columns, rows: rows as Record<string, unknown>[] };
  }
);

const cell = (value: unknown) =>
  value !== null && typeof value === "object" ? JSON.stringify(value) : String(value);
</script>

<template>
  <div v-if="!known" class="unknown">
    <p>
      This appliance asked for the <code>{{ renderer }}</code> renderer, which this console does
      not have. The console is older than the appliance it is pointed at.
    </p>
    <p class="apparatus-voice">It knows: {{ KNOWN.join(", ") }}.</p>
  </div>

  <RenderMarkdown v-else-if="renderer === 'markdown'" :content="content" />

  <pre v-else-if="renderer === 'plain'" class="plain">{{ content }}</pre>

  <pre v-else-if="renderer === 'code'" class="plain"><code
    :class="language ? `language-${language}` : undefined">{{ content }}</code></pre>

  <div v-else-if="renderer === 'transcript'" class="transcript">
    <div v-for="(turn, i) in turns" :key="i" class="turn">
      <span v-if="turn.role" class="role value">{{ turn.role }}</span>
      <p>{{ turn.text }}</p>
    </div>
  </div>

  <template v-else-if="renderer === 'json-table'">
    <p v-if="'error' in table" class="failed">{{ table.error }}</p>
    <p v-else-if="table.rows.length === 0" class="apparatus-voice">No rows.</p>
    <div v-else class="table-scroll">
      <table class="json-table">
        <thead>
          <tr>
            <th v-for="column in table.columns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in table.rows" :key="i">
            <td v-for="column in table.columns" :key="column">
              <!-- A column this row does not carry and a column whose value is null are
                   different facts, and the table says which. -->
              <span v-if="!(column in row)" class="absent">—</span>
              <span v-else class="value">{{ cell(row[column]) }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>
</template>

<style scoped>
.plain {
  margin: 0;
  padding: var(--s3) var(--s4);
  background: var(--sunk);
  border-left: 2px solid var(--rule);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: var(--t-small);
  line-height: 1.55;
}

.unknown,
.failed {
  padding: var(--s3) var(--s4);
  border-left: 2px solid var(--alarm);
  background: var(--alarm-wash);
  color: var(--ink);
}

.turn {
  margin-bottom: var(--s4);
}

.role {
  display: block;
  color: var(--ink-faint);
  font-size: var(--t-micro);
  margin-bottom: 1px;
}

.turn p {
  margin: 0;
  white-space: pre-wrap;
}

.table-scroll {
  overflow-x: auto;
}

.json-table {
  border-collapse: collapse;
  font-size: var(--t-small);
}

.json-table th,
.json-table td {
  padding: var(--s1) var(--s4) var(--s1) 0;
  border-bottom: 1px solid var(--rule-soft);
  text-align: left;
  vertical-align: top;
}

.json-table th {
  color: var(--ink-soft);
  border-bottom-color: var(--rule);
}

.absent {
  color: var(--ink-faint);
}
</style>
