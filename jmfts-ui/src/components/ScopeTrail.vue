<script setup lang="ts">
// Where you are, and what "here" means for everything else on the page.
//
// This is the one control that unifies the console. `parent_id` is a filter on all seven search
// methods and on the document list, and `ancestors` comes back with every node, so the trail a
// reader uses to climb the tree is the same value that scopes a search. Clicking an ancestor
// moves the subject AND narrows what search will look at; the two are not separate ideas here.

import { computed } from "vue";
import type { ViewAncestor } from "@/api";
import { clip } from "@/lib/text";

const props = defineProps<{ ancestors: ViewAncestor[] }>();

// THE CURRENT NODE IS NOT IN THE TRAIL. It is the heading directly below, and printing it twice
// makes a five-deep path — which is ordinary here — wrap onto a second line for no information.
//
// A long path keeps its two ends and folds the middle. The ends are what a reader navigates by:
// the tree this node is in, and the parent it sits under. The folded middle stays reachable,
// because the parent is one click away and its own trail is unfolded there.
const FOLD_OVER = 5;

const shown = computed(() => {
  const all = props.ancestors;
  if (all.length <= FOLD_OVER) return { head: all, folded: 0, tail: [] as ViewAncestor[] };
  return { head: all.slice(0, 2), folded: all.length - 3, tail: all.slice(-1) };
});
</script>

<template>
  <nav class="trail" aria-label="Path to this node">
    <RouterLink to="/" class="step root">Corpus</RouterLink>
    <template v-for="a in shown.head" :key="a.id">
      <span class="sep" aria-hidden="true">›</span>
      <RouterLink :to="`/node/${a.id}`" class="step">{{
        clip(a.title || `Node ${a.id}`, 44)
      }}</RouterLink>
    </template>
    <template v-if="shown.folded > 0">
      <span class="sep" aria-hidden="true">›</span>
      <span class="folded" :title="`${shown.folded} more levels between here and the tree`"
        >{{ shown.folded }} more</span
      >
      <template v-for="a in shown.tail" :key="a.id">
        <span class="sep" aria-hidden="true">›</span>
        <RouterLink :to="`/node/${a.id}`" class="step">{{
          clip(a.title || `Node ${a.id}`, 44)
        }}</RouterLink>
      </template>
    </template>
  </nav>
</template>

<style scoped>
.trail {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--s2);
  font-size: var(--t-small);
  line-height: 1.5;
}

.step {
  color: var(--ink-soft);
  text-decoration: none;
  border-bottom: 1px solid transparent;
}

a.step:hover {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

.folded {
  color: var(--ink-faint);
  font-style: italic;
}

.sep {
  color: var(--ink-faint);
}
</style>
