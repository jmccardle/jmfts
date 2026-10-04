<script setup lang="ts">
// One node, legible in all five of the structures it belongs to.
//
// A JMFTS document is at once a node in an ordered tree, a vertex in a typed link graph, a
// subject in a bitemporal fact graph, a member of an ontological class, and the output of an
// ingest rule at some point in its lifecycle. The existing console gives each of those its own
// page, so reading one node means leaving it four times. This page does not: the tree is the
// trail and the children list, the links and triples are the relations column, the class is the
// usetype beside the title, and the lifecycle is the apparatus on the right, all at once.
//
// THREE CALLS, AND EACH ONE IS FOR SOMETHING THE OTHERS DO NOT CARRY. `/view/{id}` brings the
// rendered content, the presentation row, the ancestors, the child stubs and the edges.
// `/documents/{id}` brings the node's own columns — `produced_by`, `settled`, `content_source`,
// `position` — which `ViewResponse` does not carry. `/documents/{id}/evidence` is a separate
// route on purpose: a field would put a join on every search hit.

import { computed, ref, watch } from "vue";
import Apparatus from "@/components/Apparatus.vue";
import Fact from "@/components/Fact.vue";
import HandMark from "@/components/HandMark.vue";
import NodeLine from "@/components/NodeLine.vue";
import Panel from "@/components/Panel.vue";
import Rendered from "@/components/Rendered.vue";
import ScopeTrail from "@/components/ScopeTrail.vue";
import Tag from "@/components/Tag.vue";
import { baseUrl, client, token, type DocumentResponse, type ViewResponse } from "@/api";
import { count, glossUsetype } from "@/lib/vocab";

const props = defineProps<{ id: number }>();

const view = ref<ViewResponse | null>(null);
const node = ref<DocumentResponse | null>(null);
const evidence = ref<Record<string, unknown> | null>(null);
const evidenceError = ref<string | null>(null);
const error = ref<string | null>(null);
const loading = ref(false);

async function load(id: number) {
  loading.value = true;
  error.value = null;
  evidenceError.value = null;
  view.value = null;
  node.value = null;
  evidence.value = null;

  try {
    // The two that must succeed. An error here is shown, never swallowed: a page that renders
    // as though it had an answer is worse than a page that says it has none.
    const [viewed, document_] = await Promise.all([
      client.view_document({ document_id: id, limit_children: 60 }),
      client.get_document({ document_id: id }),
    ]);
    view.value = viewed;
    node.value = document_;
  } catch (failure) {
    error.value = (failure as Error).message;
    loading.value = false;
    return;
  }

  // Evidence is allowed to be missing without taking the page down with it — but the reason is
  // reported where the evidence would have been, rather than rendering as "no evidence".
  try {
    const recorded = await client.get_document_evidence({ document_id: id });
    evidence.value = recorded.evidence as Record<string, unknown>;
  } catch (failure) {
    evidenceError.value = (failure as Error).message;
  }

  loading.value = false;
}

// The node is asked for again when the appliance or the token changes, not only the id.
watch([() => props.id, baseUrl, token], () => load(props.id), { immediate: true });

const usetypeGloss = computed(() => glossUsetype(node.value?.usetype)?.meaning ?? null);
const childHandling = computed(() => view.value?.presentation.child_handling ?? "collapsed");
const linkHandling = computed(() => view.value?.presentation.link_handling ?? "footnotes");

// The five collections carry a server-side default, so the generator types them optional. An
// absent collection and an empty one mean the same thing HERE — the node has none — which is
// not true of every field in this API and is worth saying: it is true of these because
// `ViewResponse` builds each one with `default_factory=list`.
const ancestors = computed(() => view.value?.ancestors ?? []);
const children = computed(() => view.value?.children_stubs ?? []);
const outbound = computed(() => view.value?.outbound_links ?? []);
const inbound = computed(() => view.value?.inbound_links ?? []);
const triples = computed(() => view.value?.triples ?? []);
const relations = computed(
  () => outbound.value.length + inbound.value.length + triples.value.length
);

// THE PRESENTATION ROW IS AN INSTRUCTION, SO THIS PAGE FOLLOWS IT.
//
// `link_handling` says where a node's edges belong, and `/view/{id}` has already acted on it:
// under `footnotes` and `inline-citations` the appliance writes them into `rendered_content`
// itself — that is where the "Footnotes" section on a file node comes from. Listing them again
// below would be the same edges twice, which is how a reader stops trusting either copy. So the
// panel is the SIDEBAR arm of that tuple, and the other arms say where the edges went instead.
//
// `hidden` is a presentation choice rather than a permission, so it can be overruled here — by
// the reader, on this node, once. What it must not do is quietly not apply.
const revealLinks = ref(false);
const revealChildren = ref(false);
watch(() => props.id, () => {
  revealLinks.value = false;
  revealChildren.value = false;
});

const linksAreInTheText = computed(
  () => linkHandling.value === "footnotes" || linkHandling.value === "inline-citations"
);
const showRelations = computed(
  () => relations.value > 0 && (linkHandling.value === "sidebar" || revealLinks.value)
);
const showChildren = computed(
  () => children.value.length > 0 && (childHandling.value !== "hidden" || revealChildren.value)
);
</script>

<template>
  <div class="inspector">
    <div class="subject scroll-pane">
      <p v-if="loading" class="waiting">Reading node {{ id }}…</p>

      <div v-else-if="error" class="failed">
        <h2>Node {{ id }} did not load</h2>
        <p>{{ error }}</p>
        <p class="apparatus-voice">
          An unreadable node and a missing one answer the same way, so this is also what you see
          for a node your token has no grant on.
        </p>
      </div>

      <template v-else-if="view && node">
        <ScopeTrail :ancestors="ancestors" :current="{ id: view.id, title: view.title }" />

        <header class="head">
          <h1>{{ view.title || `Node ${view.id}` }}</h1>
          <div class="identity">
            <Tag v-if="node.usetype" :value="node.usetype" :meaning="usetypeGloss" />
            <Fact label="id" :value="node.id" />
            <Fact
              label="you may"
              :value="view.can_write ? 'edit this' : 'read this only'"
              meaning="Asked of the appliance rather than discovered by trying and being refused."
            />
          </div>
          <p v-if="usetypeGloss" class="gloss">{{ usetypeGloss }}</p>
        </header>

        <HandMark :produced-by="node.produced_by" bracket>
          <Rendered
            :content="view.rendered_content"
            :renderer="view.presentation.renderer"
            :config="view.presentation.renderer_config"
          />
        </HandMark>

        <Panel
          v-if="children.length > 0"
          title="Inside this node"
          :note="`${count(children.length, 'child', 'children')} · ${childHandling}`"
          :quiet="!showChildren"
        >
          <template v-if="childHandling === 'hidden'" #control>
            <button type="button" class="overrule" @click="revealChildren = !revealChildren">
              {{ revealChildren ? "Follow the presentation row" : "List them anyway" }}
            </button>
          </template>

          <p v-if="!showChildren" class="empty">
            This usetype's presentation row asks for children to be hidden, so they are. They are
            still here, and still reachable.
          </p>

          <template v-else>
            <NodeLine
              v-for="stub in children"
              :key="stub.id"
              :id="stub.id"
              :title="stub.title"
              :usetype="stub.usetype"
              :preview="stub.preview"
              :trailing="stub.child_count > 0 ? `${stub.child_count} inside` : null"
            />
          </template>
        </Panel>

        <Panel
          title="What this connects to"
          :note="relations === 0 ? null : `${relations} · ${linkHandling}`"
          :quiet="!showRelations"
        >
          <template v-if="relations > 0 && linkHandling !== 'sidebar'" #control>
            <button type="button" class="overrule" @click="revealLinks = !revealLinks">
              {{ revealLinks ? "Follow the presentation row" : "List them here" }}
            </button>
          </template>

          <p v-if="relations === 0" class="empty">
            No links and no facts reach this node. Links arrive from the
            <code>propose:links</code> rule or from
            <code>POST /documents/{{ node.id }}/links</code>.
          </p>

          <p v-else-if="!showRelations && linksAreInTheText" class="empty">
            The appliance put {{ count(relations, "relation") }} into the text above as
            {{ linkHandling }}, which is what this usetype's presentation row asks for.
          </p>

          <p v-else-if="!showRelations" class="empty">
            This usetype's presentation row asks for relations to be hidden, so they are. There
            are {{ relations }}.
          </p>

          <template v-else>
            <NodeLine
              v-for="link in outbound"
              :key="`out-${link.id}`"
              :id="link.target_id"
              :title="link.title"
              :trailing="`${link.link_type} →`"
            />
            <NodeLine
              v-for="link in inbound"
              :key="`in-${link.id}`"
              :id="link.source_id"
              :title="link.title"
              :trailing="`← ${link.link_type}`"
            />
            <div v-for="triple in triples" :key="`t-${triple.id}`" class="triple">
              <RouterLink :to="`/node/${triple.subject_id}`" class="term">{{
                triple.subject_title || `Node ${triple.subject_id}`
              }}</RouterLink>
              <span class="predicate value">{{ triple.predicate_name }}</span>
              <RouterLink
                v-if="triple.object_id"
                :to="`/node/${triple.object_id}`"
                class="term"
                >{{ triple.object_title || `Node ${triple.object_id}` }}</RouterLink
              >
              <span v-else class="term literal value">{{ triple.object_literal }}</span>
              <span v-if="triple.valid_until" class="until apparatus-voice"
                >until {{ String(triple.valid_until).slice(0, 10) }}</span
              >
            </div>
          </template>
        </Panel>
      </template>
    </div>

    <Apparatus
      v-if="view && node"
      :node="node"
      :presentation="view.presentation"
      :evidence="evidence"
      :evidence-error="evidenceError"
    />
  </div>
</template>

<style scoped>
.inspector {
  display: grid;
  grid-template-columns: minmax(0, 1fr) var(--apparatus);
  height: 100%;
  min-height: 0;
}

.subject {
  padding: var(--s5) var(--s6) var(--s7);
  min-width: 0;
}

.head {
  margin: var(--s4) 0 var(--s5);
}

h1 {
  max-width: var(--measure);
  margin-bottom: var(--s2);
}

.identity {
  display: flex;
  align-items: baseline;
  gap: var(--s4);
  flex-wrap: wrap;
}

.gloss {
  margin: var(--s2) 0 0;
  color: var(--ink-soft);
  font-style: italic;
  font-size: var(--t-small);
}

:deep(.markdown),
:deep(.plain),
:deep(.transcript) {
  max-width: var(--measure);
}

.overrule {
  padding: 2px var(--s2);
  border: 1px solid var(--rule);
  border-radius: var(--r);
  background: transparent;
  color: var(--ink-soft);
  font-size: var(--t-micro);
  cursor: pointer;
}

.overrule:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.waiting,
.empty {
  max-width: var(--measure);
  color: var(--ink-soft);
  font-style: italic;
}

.failed {
  max-width: var(--measure);
  padding: var(--s4);
  border-left: 2px solid var(--alarm);
  background: var(--alarm-wash);
}

.triple {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--s2);
  padding: var(--s2) 0;
  border-bottom: 1px solid var(--rule-soft);
}

.predicate {
  color: var(--ink-soft);
  font-size: var(--t-micro);
}

.term {
  color: var(--ink);
  text-decoration: none;
  border-bottom: 1px solid var(--rule);
}

.term:hover {
  border-bottom-color: var(--accent);
  color: var(--accent);
}

.literal {
  border-bottom-style: dotted;
}

.until {
  margin-left: auto;
}

@media (max-width: 1080px) {
  .inspector {
    grid-template-columns: minmax(0, 1fr);
    overflow-y: auto;
  }
  .subject {
    overflow: visible;
    padding: var(--s4) var(--s4) var(--s5);
  }
  :deep(.apparatus) {
    border-left: 0;
    border-top: 1px solid var(--rule);
    overflow: visible;
    padding: var(--s5) var(--s4);
  }
}
</style>
