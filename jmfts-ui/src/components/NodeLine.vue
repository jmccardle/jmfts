<script setup lang="ts">
// One reference to a node, on one line. Children lists, link lists and search hits all use it,
// which is what makes a node recognisable as the same object wherever it turns up.
//
// A CHUNK'S TITLE IS OFTEN ITS GRANDPARENT'S. On the corpus this was built against, chunk
// 27400 is titled "Contribution Statement — chunk 135" and chunk 24292 is titled
// "arXiv:2104.08663v4  [cs.IR]  21 Oct 2021" — neither says what the passage is about. So the
// preview is not decoration here, and a node with no title falls back to it rather than to the
// word "Untitled", which would be true and useless.

import { computed } from "vue";
import Tag from "./Tag.vue";
import { glossUsetype } from "@/lib/vocab";
import { plainWords } from "@/lib/text";

const props = withDefaults(
  defineProps<{
    id: number;
    title?: string | null;
    usetype?: string | null;
    preview?: string | null;
    /** Right-hand column: a score, a link type, a child count. */
    trailing?: string | null;
    active?: boolean;
  }>(),
  { title: null, usetype: null, preview: null, trailing: null, active: false }
);

const previewText = computed(() => plainWords(props.preview));
const heading = computed(() => props.title?.trim() || previewText.value || `Node ${props.id}`);
const showPreview = computed(() => Boolean(props.title?.trim()) && previewText.value.length > 0);
const gloss = computed(() => glossUsetype(props.usetype)?.meaning ?? null);
</script>

<template>
  <RouterLink :to="`/node/${id}`" class="line" :class="{ active }">
    <span class="id value">{{ id }}</span>
    <span class="body">
      <span class="heading">{{ heading }}</span>
      <span v-if="showPreview" class="preview">{{ previewText }}</span>
    </span>
    <span class="meta">
      <Tag v-if="usetype" :value="usetype" :meaning="gloss" />
      <span v-if="trailing" class="trailing value">{{ trailing }}</span>
    </span>
  </RouterLink>
</template>

<style scoped>
.line {
  display: grid;
  grid-template-columns: 4.2rem minmax(0, 1fr) auto;
  gap: var(--s3);
  align-items: baseline;
  padding: var(--s2) var(--s2) var(--s2) 0;
  border-bottom: 1px solid var(--rule-soft);
  color: inherit;
  text-decoration: none;
}

.line:hover {
  background: var(--leaf);
}

.line.active {
  background: var(--accent-wash);
}

.id {
  color: var(--ink-faint);
  text-align: right;
  font-size: var(--t-micro);
}

.body {
  min-width: 0;
}

.heading {
  display: block;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.line:hover .heading {
  text-decoration: underline;
  text-underline-offset: 2px;
}

.preview {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  margin-top: 1px;
  color: var(--ink-soft);
  font-size: var(--t-small);
  line-height: 1.45;
}

.meta {
  display: flex;
  align-items: baseline;
  gap: var(--s2);
  white-space: nowrap;
}

.trailing {
  color: var(--ink-soft);
  font-size: var(--t-micro);
}
</style>
