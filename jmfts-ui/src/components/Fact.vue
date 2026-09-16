<script setup lang="ts">
// A field and what it holds — the apparatus entry, and the most-used component in the console.
//
// The field name is prose and the value is a value, so the two faces do the separating and no
// colon, dash or bullet is needed between them. A field whose value is ABSENT renders as an
// em rule rather than as an empty space, because "this node carries no anchor" and "this panel
// forgot to draw the anchor" must not look the same.

withDefaults(
  defineProps<{
    label: string;
    value?: string | number | null;
    meaning?: string | null;
    /** Lay the value under the label instead of beside it. For long values. */
    stacked?: boolean;
  }>(),
  { value: null, meaning: null, stacked: false }
);
</script>

<template>
  <div class="fact" :class="{ stacked }">
    <span class="label" :title="meaning ?? undefined">{{ label }}</span>
    <span v-if="value === null || value === undefined || value === ''" class="absent">—</span>
    <span v-else class="value fact-value"><slot>{{ value }}</slot></span>
  </div>
</template>

<style scoped>
.fact {
  display: flex;
  gap: var(--s2);
  align-items: baseline;
  min-width: 0;
}

.fact.stacked {
  display: block;
}

.label {
  flex: none;
  color: var(--ink-faint);
  font-size: var(--t-small);
}

.fact.stacked .label {
  display: block;
  margin-bottom: 1px;
}

.fact-value {
  color: var(--ink);
  min-width: 0;
  overflow-wrap: anywhere;
}

.absent {
  color: var(--ink-faint);
  font-family: var(--face-value);
  font-size: var(--t-small);
}
</style>
