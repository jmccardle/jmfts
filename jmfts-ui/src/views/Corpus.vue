<script setup lang="ts">
// Where you arrive: a census, then the trees it is made of.
//
// `GET /graph/stats` answers what this appliance holds in one call — totals, and a breakdown by
// usetype, by link type and by fact type. That is a better opening than a search box, because
// the first question about an unfamiliar corpus is what is in it, and the histogram is also the
// way in: every usetype in it is a filter `GET /documents` accepts.

import { computed, onMounted, ref } from "vue";
import NodeLine from "@/components/NodeLine.vue";
import Panel from "@/components/Panel.vue";
import Tag from "@/components/Tag.vue";
import { client, type DocumentResponse, type GraphStatsResponse } from "@/api";
import { count, glossUsetype } from "@/lib/vocab";

const stats = ref<GraphStatsResponse | null>(null);
const roots = ref<DocumentResponse[]>([]);
const error = ref<string | null>(null);
const loading = ref(true);

onMounted(async () => {
  try {
    const [s, r] = await Promise.all([client.get_stats(), client.get_root_documents()]);
    stats.value = s;
    roots.value = r;
  } catch (failure) {
    error.value = (failure as Error).message;
  } finally {
    loading.value = false;
  }
});

/** Usetypes, largest first, with each one's share of the corpus. */
const usetypes = computed(() => {
  const by = stats.value?.by_usetype ?? {};
  const total = Object.values(by).reduce((a, b) => a + b, 0) || 1;
  return Object.entries(by)
    .sort((a, b) => b[1] - a[1])
    .map(([name, n]) => ({
      name,
      n,
      share: n / total,
      meaning: glossUsetype(name)?.meaning ?? null,
    }));
});

const linkTypes = computed(() =>
  Object.entries(stats.value?.by_link_type ?? {}).sort((a, b) => b[1] - a[1])
);
</script>

<template>
  <div class="corpus scroll-pane">
    <p v-if="loading" class="waiting">Asking the appliance what it holds…</p>

    <div v-else-if="error" class="failed">
      <h2>The appliance did not answer</h2>
      <p>{{ error }}</p>
      <p class="apparatus-voice">
        A 401 here means the token is missing or wrong. Everything except <code>/health</code>
        needs one.
      </p>
    </div>

    <template v-else-if="stats">
      <header class="census">
        <h1>{{ stats.total_documents.toLocaleString() }} nodes</h1>
        <p class="line">
          in {{ count(roots.length, "tree") }}, joined by
          {{ count(stats.total_links, "link") }} and
          {{ count(stats.total_triples, "asserted fact") }}.
        </p>
      </header>

      <Panel title="What the nodes are" :note="`${usetypes.length} usetypes in use`">
        <ul class="histogram">
          <li v-for="u in usetypes" :key="u.name">
            <RouterLink :to="`/?usetype=${u.name}`" class="bar-row">
              <span class="name value">{{ u.name }}</span>
              <span class="bar" aria-hidden="true">
                <span class="fill" :style="{ width: `${Math.max(u.share * 100, 0.4)}%` }" />
              </span>
              <span class="n value">{{ u.n.toLocaleString() }}</span>
            </RouterLink>
            <p v-if="u.meaning" class="gloss">{{ u.meaning }}</p>
          </li>
        </ul>
      </Panel>

      <Panel title="Trees" :note="count(roots.length, 'root')">
        <NodeLine
          v-for="root in roots"
          :key="root.id"
          :id="root.id"
          :title="root.title"
          :usetype="root.usetype"
          :preview="root.content"
        />
      </Panel>

      <Panel v-if="linkTypes.length" title="How nodes are joined" :quiet="true">
        <div class="link-types">
          <span v-for="[name, n] in linkTypes" :key="name" class="link-type">
            <Tag :value="name" />
            <span class="value n">{{ n.toLocaleString() }}</span>
          </span>
        </div>
      </Panel>
    </template>
  </div>
</template>

<style scoped>
.corpus {
  padding: var(--s6) var(--s6) var(--s7);
  max-width: 56rem;
}

.census {
  margin-bottom: var(--s6);
}

.census h1 {
  font-size: var(--t-display);
  font-weight: 600;
  letter-spacing: -0.015em;
}

.census .line {
  margin: var(--s2) 0 0;
  color: var(--ink-soft);
}

.histogram {
  list-style: none;
  margin: 0;
  padding: 0;
}

.histogram li {
  padding: var(--s2) 0;
  border-bottom: 1px solid var(--rule-soft);
}

.bar-row {
  display: grid;
  grid-template-columns: 7rem minmax(0, 1fr) 5rem;
  gap: var(--s3);
  align-items: center;
  color: inherit;
  text-decoration: none;
}

.bar-row:hover .name {
  color: var(--accent);
}

.name {
  font-size: var(--t-small);
}

.bar {
  display: block;
  height: 7px;
  background: var(--sunk);
}

.fill {
  display: block;
  height: 100%;
  background: var(--accent);
  opacity: 0.55;
}

.bar-row:hover .fill {
  opacity: 1;
}

.n {
  text-align: right;
  font-size: var(--t-small);
  color: var(--ink-soft);
}

.gloss {
  margin: 2px 0 0 7.6rem;
  font-size: var(--t-micro);
  font-style: italic;
  color: var(--ink-faint);
  max-width: 44ch;
}

.link-types {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s4);
}

.link-type {
  display: inline-flex;
  align-items: baseline;
  gap: var(--s2);
}

.waiting {
  color: var(--ink-soft);
  font-style: italic;
}

.failed {
  padding: var(--s4);
  border-left: 2px solid var(--alarm);
  background: var(--alarm-wash);
  max-width: var(--measure);
}

@media (max-width: 880px) {
  .corpus {
    padding: var(--s4);
  }
  .bar-row {
    grid-template-columns: 5.5rem minmax(0, 1fr) 4rem;
  }
  .gloss {
    margin-left: 0;
  }
}
</style>
