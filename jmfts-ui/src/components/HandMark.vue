<script setup lang="ts">
// Whether a rule wrote this node, or a person put it here.
//
// This is the distinction the console carries in the margin rather than in a colour, because it
// has to be readable next to a usetype, a lifecycle state and a score without any of the four
// shouting over the others. A produced node gets an editorial bracket — the mark a printed
// edition uses for text the editor supplied. A node a person asserted gets nothing, because
// unmarked text is the reader's default assumption and the API agrees: `produced_by` is NULL.

import { computed } from "vue";
import { handOf } from "@/lib/vocab";

const props = withDefaults(
  defineProps<{
    producedBy?: string | null;
    /** Draw the bracket in the margin around this component's content. */
    bracket?: boolean;
  }>(),
  { producedBy: null, bracket: false }
);

const hand = computed(() => handOf(props.producedBy));
</script>

<template>
  <span v-if="!bracket" class="mark" :class="hand.kind" :title="hand.meaning">
    <span v-if="hand.kind === 'rule'" class="rules" aria-hidden="true" />
    <span class="name">{{ hand.label }}</span>
  </span>
  <div v-else class="bracketed" :class="hand.kind" :title="hand.meaning">
    <slot />
  </div>
</template>

<style scoped>
.mark {
  display: inline-flex;
  align-items: center;
  gap: 0.34em;
}

.rules {
  display: inline-block;
  width: 4px;
  height: 0.85em;
  border-left: 1px solid var(--ink-faint);
  border-right: 1px solid var(--ink-faint);
}

.name {
  font-family: var(--face-value);
  font-size: var(--t-micro);
  color: var(--ink-soft);
}

.mark.asserted .name {
  font-family: var(--face-text);
  font-size: var(--t-small);
  font-style: italic;
  color: var(--ink-faint);
}

.bracketed.rule {
  border-left: 1px solid var(--rule);
  box-shadow: -3px 0 0 -2px var(--rule);
  padding-left: var(--s4);
  margin-left: -1px;
}
</style>
