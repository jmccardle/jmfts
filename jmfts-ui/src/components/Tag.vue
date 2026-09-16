<script setup lang="ts">
// One categorical value from the API, drawn so a column of them can be scanned.
//
// Set in the value face, because that is the rule this console keeps: mono means a value you
// could copy. Ground rather than outline, and no pill — twenty pills in a children list is
// noise, and these appear in twenties.

import type { Tone } from "@/lib/vocab";

withDefaults(
  defineProps<{
    /** The value exactly as the API gave it. Never normalised for display. */
    value: string;
    /** One line for the reader, shown on hover. */
    meaning?: string | null;
    tone?: Tone;
  }>(),
  { meaning: null, tone: "plain" }
);
</script>

<template>
  <span class="tag" :class="`tone-${tone}`" :title="meaning ?? undefined">{{ value }}</span>
</template>

<style scoped>
.tag {
  display: inline-block;
  padding: 0.05em 0.36em;
  border-radius: 2px;
  background: var(--sunk);
  color: var(--ink-soft);
  font-family: var(--face-value);
  font-size: var(--t-micro);
  line-height: 1.5;
  white-space: nowrap;
}

.tone-active {
  color: var(--accent);
  background: var(--accent-wash);
}

.tone-alarm {
  color: var(--alarm);
  background: var(--alarm-wash);
}

.tone-quiet {
  color: var(--ink-faint);
  background: transparent;
  padding-left: 0;
  padding-right: 0;
}
</style>
