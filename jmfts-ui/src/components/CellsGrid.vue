<script setup lang="ts">
// A worksheet region as CELLS, from `GET /documents/{sheet}/cells`.
//
// Not a picture, for the reason `OFFICE_SPEC.md` Part 7 gives at the route: a page image of a
// spreadsheet answers a different question, and JSON cells are selectable and carry their
// formulas. The reading follows `jmfts-web/jmfts_web/static/render/sheet.js`: rows are
// numbered from the served `ref`, columns are the letters the response lists (never a base-26
// conversion here), a row the response does not carry is empty, and a formula is shown on
// hover. The response's `ref` is what the server actually served, which may not be what was
// asked for, so the caption names it and says where it came from.

import { computed } from "vue";
import type { DocumentCellsResponse } from "@/api";
import { readCellRef } from "@/lib/preview";

const props = defineProps<{ cells: DocumentCellsResponse }>();

/**
 * The most rows put on the page at once, as in sheet.js. A viewport, not a truncation of the
 * answer: the caption says how much of the region is shown.
 */
const GRID_ROWS_MAX = 200;

const REF_SOURCE: Record<string, string> = {
  request: "the region this node's anchor names",
  anchor: "the node's own cells anchor",
  used_range: "the whole of what profile:sheet measured this sheet to hold",
};

const span = computed(() => {
  const [start, end] = readCellRef(props.cells.ref);
  return { first: start.row, count: end.row - start.row + 1 };
});
const shown = computed(() => Math.min(span.value.count, GRID_ROWS_MAX));
const rows = computed(() => {
  const values = new Map(props.cells.rows.map((row) => [row.row, row.values]));
  return Array.from({ length: shown.value }, (_, offset) => {
    const number = span.value.first + offset;
    const carried = values.get(number);
    return {
      number,
      cells: props.cells.columns.map((letter, column) => {
        const value = carried === undefined ? null : carried[column];
        const formula = props.cells.cells[`${letter}${number}`]?.formula ?? null;
        return { text: value === null || value === undefined ? "" : String(value), formula };
      }),
    };
  });
});
const source = computed(
  () => REF_SOURCE[props.cells.ref_source] ?? `served with ref_source ${props.cells.ref_source}`
);
</script>

<template>
  <figure class="grid">
    <figcaption>
      <strong>{{ cells.sheet }}</strong>
      <span class="value">{{ cells.ref }}</span>
      <span class="apparatus-voice">{{ source }}.</span>
      <span v-if="shown < span.count" class="apparatus-voice">
        The first {{ shown }} of its {{ span.count }} rows are shown.
      </span>
    </figcaption>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th class="gutter" />
            <th v-for="letter in cells.columns" :key="letter" scope="col">{{ letter }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.number">
            <th class="gutter" scope="row">{{ row.number }}</th>
            <td
              v-for="(cell, index) in row.cells"
              :key="index"
              :class="{ formula: cell.formula }"
              :title="cell.formula ?? undefined"
            >
              {{ cell.text }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </figure>
</template>

<style scoped>
.grid {
  margin: 0;
}

figcaption {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s3);
  align-items: baseline;
  margin-bottom: var(--s3);
  font-size: var(--t-small);
}

.scroll {
  max-width: 100%;
  max-height: 70vh;
  overflow: auto;
  border: 1px solid var(--rule);
}

table {
  border-collapse: collapse;
  font-size: var(--t-small);
  font-variant-numeric: tabular-nums;
}

th,
td {
  border: 1px solid var(--rule);
  padding: 2px var(--s2);
  white-space: nowrap;
  max-width: 24ch;
  overflow: hidden;
  text-overflow: ellipsis;
}

th {
  color: var(--ink-soft);
  font-weight: normal;
}

.gutter {
  text-align: right;
}

.formula {
  text-decoration: underline dotted;
}
</style>
