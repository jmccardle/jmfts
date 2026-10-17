<script setup lang="ts">
// Where in the source a node came from: the page of the PDF, with the node's stored anchor
// drawn on it.
//
// THREE CALLS, EACH FOR SOMETHING THE OTHERS DO NOT CARRY. `/documents/{id}/evidence` brings the
// anchor (a page and a rectangle in points). `/documents/{id}/region?anchor=true` is the
// appliance cropping to that same anchor — and it is also the only call that VALIDATES it: the
// whole-page `/image` takes a page number and never looks at the rectangle, so a stale anchor
// is refused here and nowhere else. `/documents/{id}/image` is the page the rectangle is drawn
// on. The crop is asked for first, and when it is refused this page shows the refusal and
// stops: a whole page with no box on it would answer a question nobody asked, and look like
// success.
//
// THE BOX IS DRAWN IN THE APPLIANCE'S FRAME. An anchor is in points in the page AS DISPLAYED —
// rotation applied — and the image is rendered displayed too, so `lib/preview.ts` scales and
// does nothing else. The crop beside the page is the same rectangle cut by the appliance
// itself, which is the check a person can make: the words in the crop are the words the box
// should surround. A rotated page that ALSO has a CropBox is an open question upstream (#44),
// so that comparison is the honest way to read one rather than trusting either picture alone.

import { computed, onBeforeUnmount, ref, watch } from "vue";
import Fact from "@/components/Fact.vue";
import Panel from "@/components/Panel.vue";
import Tag from "@/components/Tag.vue";
import { baseUrl, client, token, type DocumentResponse } from "@/api";
import {
  overlayBox,
  pageLabel,
  pagePoints,
  readAnchor,
  refusalOf,
  type AnchorReading,
  type PdfAnchor,
  type Refusal,
} from "@/lib/preview";
import { clip, plainWords } from "@/lib/text";

const props = defineProps<{ id: number }>();

// The page is shown at this density and the appliance's own crop at the default. The page's size
// in points is recovered from the image and this number, so it is the only one the two share.
const PAGE_DPI = 100;
const CROP_DPI = 150;

const node = ref<DocumentResponse | null>(null);
const reading = ref<AnchorReading | null>(null);
const loading = ref(false);
// The node itself would not load, or its evidence would not: nothing below can be said.
const failed = ref<Refusal | null>(null);
// The appliance refused the anchor, or the page: said verbatim, in place of a picture.
const refused = ref<{ what: string; refusal: Refusal } | null>(null);
const pageUrl = ref<string | null>(null);
const cropUrl = ref<string | null>(null);
const imageSize = ref<{ width: number; height: number } | null>(null);

// Object URLs hold the blob until revoked, so each one is released when it is replaced.
function release() {
  if (pageUrl.value) URL.revokeObjectURL(pageUrl.value);
  if (cropUrl.value) URL.revokeObjectURL(cropUrl.value);
  pageUrl.value = null;
  cropUrl.value = null;
}
onBeforeUnmount(release);

// Navigating from one node to another while the first is still loading must not let the first
// answer land on the second's route.
let ask = 0;

async function load(id: number) {
  const mine = ++ask;
  loading.value = true;
  failed.value = null;
  refused.value = null;
  node.value = null;
  reading.value = null;
  imageSize.value = null;
  release();

  try {
    const [document_, recorded] = await Promise.all([
      client.get_document({ document_id: id }),
      client.get_document_evidence({ document_id: id }),
    ]);
    if (mine !== ask) return;
    node.value = document_;
    reading.value = readAnchor(recorded.evidence);
  } catch (failure) {
    if (mine !== ask) return;
    failed.value = refusalOf(failure);
    loading.value = false;
    return;
  }

  const found = reading.value;
  const isFile = node.value.usetype === "file";
  // Only two nodes have a page to ask for: one with a PDF rectangle, and a file node, which is
  // the whole document and whose page is its first. Everything else is told why not, below.
  if (found.state !== "pdf" && !isFile) {
    loading.value = false;
    return;
  }

  let step = "the crop of the anchor";
  try {
    if (found.state === "pdf") {
      const crop = await client.get_document_region({
        document_id: id,
        anchor: true,
        dpi: CROP_DPI,
      });
      if (mine !== ask) return;
      cropUrl.value = URL.createObjectURL(crop);
    }
    step = "the page";
    // A file node names no page of its own; leaving `page` out asks for its first.
    const page = await client.get_document_image({
      document_id: id,
      page: found.state === "pdf" ? found.anchor.page : null,
      dpi: PAGE_DPI,
    });
    if (mine !== ask) return;
    pageUrl.value = URL.createObjectURL(page);
  } catch (failure) {
    if (mine !== ask) return;
    // A refusal replaces every picture, including one that did arrive: a crop beside a refused
    // page, or a page beside a refused crop, is half an answer that reads as a whole one.
    release();
    refused.value = { what: step, refusal: refusalOf(failure) };
  }
  loading.value = false;
}

watch([() => props.id, baseUrl, token], () => load(props.id), { immediate: true });

function measured(event: Event) {
  const image = event.target as HTMLImageElement;
  imageSize.value = { width: image.naturalWidth, height: image.naturalHeight };
}

const anchor = computed<PdfAnchor | null>(() =>
  reading.value?.state === "pdf" ? reading.value.anchor : null
);
const pageSize = computed(() =>
  imageSize.value ? pagePoints(imageSize.value.width, imageSize.value.height, PAGE_DPI) : null
);
const box = computed(() =>
  anchor.value && pageSize.value ? overlayBox(anchor.value.bbox, pageSize.value) : null
);
const boxStyle = computed(() =>
  box.value
    ? {
        left: `${box.value.left}%`,
        top: `${box.value.top}%`,
        width: `${box.value.width}%`,
        height: `${box.value.height}%`,
      }
    : {}
);
const nodeText = computed(() => clip(plainWords(node.value?.content), 400));
const bboxText = computed(() => anchor.value?.bbox.map((v) => v.toFixed(1)).join(", ") ?? "");
</script>

<template>
  <div class="preview scroll-pane">
    <p v-if="loading" class="waiting">Looking for node {{ id }} on its page…</p>

    <div v-else-if="failed" class="failed">
      <h2>Node {{ id }} did not load</h2>
      <p v-if="failed.status !== null" class="value">HTTP {{ failed.status }}</p>
      <p class="detail">{{ failed.detail }}</p>
    </div>

    <template v-else-if="node && reading">
      <header class="head">
        <h1>{{ node.title || `Node ${node.id}` }}</h1>
        <div class="identity">
          <Tag v-if="node.usetype" :value="node.usetype" />
          <Fact label="id" :value="node.id" />
          <RouterLink :to="`/node/${node.id}`" class="back">Back to the node</RouterLink>
        </div>
      </header>

      <!-- The appliance refused, and what it said is what is shown. -->
      <div v-if="refused" class="refused" role="alert">
        <h2>The appliance refused {{ refused.what }}</h2>
        <p v-if="refused.refusal.status !== null" class="value">
          HTTP {{ refused.refusal.status }}
        </p>
        <p class="detail">{{ refused.refusal.detail }}</p>
        <p class="apparatus-voice">
          No picture is shown in its place. This page does not draw a box on a page the appliance
          would not stand behind.
        </p>
      </div>

      <!-- No PDF rectangle: say which of the ways that can be. -->
      <p v-else-if="reading.state === 'absent' && node.usetype !== 'file'" class="plain">
        This node has no recorded place in a source document, so there is no page to show. An
        anchor is written by the <code>citation</code> rule for text taken from a PDF; this node
        was not.
      </p>
      <p v-else-if="reading.state === 'unresolved'" class="plain">
        The appliance tried to find this node's place in its source and could not:
        <span class="detail">{{ reading.reason }}</span>
      </p>
      <p v-else-if="reading.state === 'other'" class="plain">
        This node's anchor is of kind <code>{{ reading.kind }}</code>, not a rectangle on a PDF
        page, and this view only draws those.
      </p>
      <div v-else-if="reading.state === 'malformed'" class="refused" role="alert">
        <h2>This node's stored anchor is not usable</h2>
        <p class="detail">{{ reading.why }}</p>
      </div>

      <template v-else-if="pageUrl">
        <p v-if="reading.state === 'absent'" class="plain">
          This node is the whole document, so there is no rectangle to draw; this is its first page.
        </p>

        <div class="surfaces">
          <Panel
            :title="anchor ? `On ${pageLabel(anchor.page)}` : 'Page 1'"
            :note="anchor && anchor.of === 'rendition' ? 'approximate · our rendering' : null"
          >
            <div class="sheet">
              <img :src="pageUrl" :alt="`The page, with the anchor drawn on it`" @load="measured" />
              <div
                v-if="box && box.placement !== 'outside'"
                class="box"
                :style="boxStyle"
                title="The stored anchor"
              />
            </div>
            <p v-if="box && box.placement === 'outside'" class="mismatch" role="alert">
              The anchor <span class="value">[{{ bboxText }}]</span> lies entirely outside this
              page, which is {{ pageSize?.width.toFixed(0) }} by
              {{ pageSize?.height.toFixed(0) }} points as displayed. Nothing is drawn.
            </p>
            <p v-else-if="box && box.placement === 'partly'" class="mismatch" role="alert">
              Part of the anchor <span class="value">[{{ bboxText }}]</span> lies beyond the page,
              so the box is cut at its edge.
            </p>
            <p v-if="anchor && anchor.continues.length > 0" class="apparatus-voice">
              The passage continues on
              {{ anchor.continues.map(pageLabel).join(", ") }}. The box covers only where it
              begins.
            </p>
          </Panel>

          <Panel v-if="cropUrl" title="The appliance's crop" :note="`${CROP_DPI} dpi`">
            <img class="crop" :src="cropUrl" alt="The page cut to the anchor, by the appliance" />
            <p v-if="nodeText" class="said"><span class="apparatus-voice">The node says</span> {{ nodeText }}</p>
            <p class="apparatus-voice">
              The same rectangle, cut by the appliance rather than drawn here. If these words are
              not the ones the box should surround, the stored anchor is wrong, not this page.
            </p>
          </Panel>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.preview {
  padding: var(--s5) var(--s6) var(--s7);
  min-width: 0;
}

.head {
  margin: var(--s4) 0 var(--s5);
}

.identity {
  display: flex;
  align-items: baseline;
  gap: var(--s4);
  flex-wrap: wrap;
}

.back {
  margin-left: auto;
  font-size: var(--t-small);
}

.surfaces {
  display: grid;
  grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
  gap: var(--s6);
  align-items: start;
}

/* The wrapper is exactly the image's box, so a percentage of it is a percentage of the page. */
.sheet {
  position: relative;
  display: inline-block;
  max-width: 100%;
  border: 1px solid var(--rule);
  background: #fff;
  line-height: 0;
}

.sheet img {
  display: block;
  max-width: 100%;
  height: auto;
}

.box {
  position: absolute;
  border: 2px solid var(--accent);
  background: var(--accent-wash);
  opacity: 0.55;
  pointer-events: none;
}

.crop {
  display: block;
  max-width: 100%;
  border: 1px solid var(--rule);
}

.said {
  max-width: var(--measure);
  font-size: var(--t-small);
}

.waiting,
.plain {
  max-width: var(--measure);
  color: var(--ink-soft);
  font-style: italic;
}

.failed,
.refused {
  max-width: var(--measure);
  padding: var(--s4);
  border-left: 2px solid var(--alarm);
  background: var(--alarm-wash);
}

/* The server's words, kept as it set them: wrapped, never trimmed or reflowed. */
.detail {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.mismatch {
  color: var(--alarm);
  font-size: var(--t-small);
}

@media (max-width: 1080px) {
  .surfaces {
    grid-template-columns: minmax(0, 1fr);
  }
  .preview {
    padding: var(--s4) var(--s4) var(--s5);
  }
}
</style>
