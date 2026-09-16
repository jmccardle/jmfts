<script setup lang="ts">
// A named region of a page.
//
// A rule and a heading, not a card. Cards would make every region look equally important and
// this console has a strong hierarchy: one subject, and several readings of it.

withDefaults(
  defineProps<{
    title: string;
    /** A count or a state, set beside the heading rather than inside it. */
    note?: string | null;
    /** For a region whose content the appliance told us to hide. */
    quiet?: boolean;
  }>(),
  { note: null, quiet: false }
);
</script>

<template>
  <section class="panel" :class="{ quiet }">
    <header>
      <h3>{{ title }}</h3>
      <span v-if="note" class="note value">{{ note }}</span>
      <slot name="control" />
    </header>
    <slot />
  </section>
</template>

<style scoped>
.panel {
  margin-bottom: var(--s6);
}

header {
  display: flex;
  align-items: baseline;
  gap: var(--s3);
  padding-bottom: var(--s2);
  margin-bottom: var(--s3);
  border-bottom: 1px solid var(--rule-soft);
}

h3 {
  font-size: var(--t-base);
  font-weight: 600;
  color: var(--ink);
}

.quiet h3 {
  color: var(--ink-soft);
  font-weight: 400;
}

.note {
  color: var(--ink-faint);
  font-size: var(--t-micro);
}

header :slotted(*) {
  margin-left: auto;
}
</style>
