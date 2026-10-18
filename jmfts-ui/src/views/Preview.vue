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
//
// THE ANCHOR IS READ IN THE APPLIANCE'S ORDER (`lib/preview.ts` `readAnchor`): a Word or
// PowerPoint chunk's `render_anchor`, a rectangle on the PDF the appliance rendered of it, before
// its `source_anchor`, which is a package address with no page. A spreadsheet's `cells` anchor
// is not a picture at all: it is read from `/documents/{sheet}/cells` and shown as a grid.

import { computed, onBeforeUnmount, ref, watch } from "vue";
import CellsGrid from "@/components/CellsGrid.vue";
import Fact from "@/components/Fact.vue";
import Panel from "@/components/Panel.vue";
import Tag from "@/components/Tag.vue";
import { baseUrl, client, token, type DocumentCellsResponse, type DocumentResponse } from "@/api";
import {
  fetchAtDpi,
  overlayBox,
  pageLabel,
  pagePoints,
  readAnchor,
  readCellRef,
  refusalOf,
  sourceFormat,
  type AnchorReading,
  type PdfAnchor,
  type Refusal,
  type SourceFormat,
} from "@/lib/preview";
import { clip, plainWords } from "@/lib/text";

const props = defineProps<{ id: number }>();

// The densities asked for first: the page at one, the appliance's own crop at its default. A
// large page can be refused at either with a 413 that names a density that fits, and then the
// picture is asked for once more at that density (`fetchAtDpi`). The page's size in points is
// recovered from the image and the density it was ACTUALLY rendered at, so that is what is kept.
const PAGE_DPI = 100;
const CROP_DPI = 150;

/** Where a node with no anchor sits, which is what decides why it has none. */
type AbsentSource =
  | { kind: "sheet"; sheetId: number }
  | { kind: "file"; fileId: number; format: SourceFormat; mime: string | null }
  | { kind: "none" };

const node = ref<DocumentResponse | null>(null);
const reading = ref<AnchorReading | null>(null);
const loading = ref(false);
// The node itself would not load, or its evidence would not: nothing below can be said.
const failed = ref<Refusal | null>(null);
// The appliance refused the anchor, or the page: said verbatim, in place of a picture.
const refused = ref<{ what: string; refusal: Refusal } | null>(null);
// Something the answers contradict, found here rather than said by the appliance: a cells anchor
// with no worksheet above it, or cells served for a different sheet than the anchor names.
const problem = ref<string | null>(null);
const pageUrl = ref<string | null>(null);
const cropUrl = ref<string | null>(null);
const pageDpi = ref(PAGE_DPI);
const cropDpi = ref(CROP_DPI);
const imageSize = ref<{ width: number; height: number } | null>(null);
const cells = ref<DocumentCellsResponse | null>(null);
const absentSource = ref<AbsentSource | null>(null);

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
  problem.value = null;
  node.value = null;
  reading.value = null;
  imageSize.value = null;
  cells.value = null;
  absentSource.value = null;
  pageDpi.value = PAGE_DPI;
  cropDpi.value = CROP_DPI;
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
  const usetype = node.value.usetype;

  // A SPREADSHEET REGION IS READ AS CELLS. `/cells` answers on the SHEET node, not on the record
  // or table that carries the anchor, so the nearest worksheet at or above this node is the one
  // asked, with the anchor's `ref` — the same node `_cells_call` names in `/region`'s 409. A
  // sheet node with no anchor of its own is asked for its used range.
  if (found.state === "cells" || (found.state === "absent" && usetype === "sheet")) {
    await loadCells(id, mine, found.state === "cells" ? found : null);
    return;
  }

  // NO ANCHOR, AND WHY DEPENDS ON WHAT THE NODE CAME FROM. 0.7.0 named the PDF `citation` rule
  // for every such node; a record of a workbook or a paragraph of a .docx was told it "was not"
  // from a PDF, which is true and is not the reason. The format is the file node's own
  // `detected_mime`, so it is read rather than guessed.
  if (found.state === "absent" && usetype !== "file") {
    await loadAbsentSource(id, mine);
    return;
  }

  // Only two nodes have a page to ask for: one with a PDF rectangle, and a file node with no
  // anchor, which is the whole document and whose page is its first. An unresolved, other or
  // malformed reading is told why not, below, and asks for no picture it would not show.
  if (found.state !== "pdf" && !(usetype === "file" && found.state === "absent")) {
    loading.value = false;
    return;
  }

  let step = "the crop of the anchor";
  try {
    if (found.state === "pdf") {
      const crop = await fetchAtDpi(
        (dpi) => client.get_document_region({ document_id: id, anchor: true, dpi }),
        CROP_DPI
      );
      if (mine !== ask) return;
      cropUrl.value = URL.createObjectURL(crop.blob);
      cropDpi.value = crop.dpi;
    }
    step = "the page";
    // A file node names no page of its own; leaving `page` out asks for its first.
    const page = await fetchAtDpi(
      (dpi) =>
        client.get_document_image({
          document_id: id,
          page: found.state === "pdf" ? found.anchor.page : null,
          dpi,
        }),
      PAGE_DPI
    );
    if (mine !== ask) return;
    pageDpi.value = page.dpi;
    pageUrl.value = URL.createObjectURL(page.blob);
  } catch (failure) {
    if (mine !== ask) return;
    // A refusal replaces every picture, including one that did arrive: a crop beside a refused
    // page, or a page beside a refused crop, is half an answer that reads as a whole one.
    release();
    refused.value = { what: step, refusal: refusalOf(failure) };
  }
  loading.value = false;
}

/** The nearest node of `usetype` among `ancestors` (root first, as the appliance returns them). */
function nearest(ancestors: DocumentResponse[], usetype: string): DocumentResponse | null {
  for (let at = ancestors.length - 1; at >= 0; at -= 1) {
    if (ancestors[at].usetype === usetype) return ancestors[at];
  }
  return null;
}

async function loadCells(
  id: number,
  mine: number,
  found: { sheet: string; ref: string } | null
) {
  let step = "this node's ancestors";
  try {
    let sheetId = id;
    if (node.value?.usetype !== "sheet") {
      const sheet = nearest(await client.get_ancestors({ document_id: id }), "sheet");
      if (mine !== ask) return;
      if (sheet === null) {
        problem.value =
          `This node's anchor names cells ${found?.ref} of sheet "${found?.sheet}", and no ` +
          "worksheet node sits at or above it, so there is no sheet to read them from.";
        loading.value = false;
        return;
      }
      sheetId = sheet.id;
    }
    step = "the cells";
    const answer = await client.get_document_cells({ document_id: sheetId, ref: found?.ref ?? null });
    if (mine !== ask) return;
    if (found !== null && answer.sheet !== found.sheet) {
      problem.value =
        `This node's anchor names sheet "${found.sheet}", and the worksheet above it, node ` +
        `${sheetId}, is sheet "${answer.sheet}". The cells it served are not shown.`;
    } else {
      try {
        readCellRef(answer.ref);
        cells.value = answer;
      } catch (unreadable) {
        problem.value =
          `The appliance served cells for ref "${answer.ref}", which this page cannot lay out: ` +
          (unreadable as Error).message;
      }
    }
  } catch (failure) {
    if (mine !== ask) return;
    refused.value = { what: step, refusal: refusalOf(failure) };
  }
  loading.value = false;
}

async function loadAbsentSource(id: number, mine: number) {
  let step = "this node's ancestors";
  try {
    const ancestors = await client.get_ancestors({ document_id: id });
    if (mine !== ask) return;
    const sheet = nearest(ancestors, "sheet");
    const file = nearest(ancestors, "file");
    if (sheet !== null) {
      absentSource.value = { kind: "sheet", sheetId: sheet.id };
    } else if (file !== null) {
      step = "the evidence of the file it came from";
      const recorded = await client.get_document_evidence({ document_id: file.id });
      if (mine !== ask) return;
      const block = recorded.evidence?.["file"] as { detected_mime?: unknown } | null | undefined;
      const mime = typeof block?.detected_mime === "string" ? block.detected_mime : null;
      absentSource.value = { kind: "file", fileId: file.id, format: sourceFormat(mime), mime };
    } else {
      absentSource.value = { kind: "none" };
    }
  } catch (failure) {
    if (mine !== ask) return;
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
  imageSize.value ? pagePoints(imageSize.value.width, imageSize.value.height, pageDpi.value) : null
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

      <!-- The answers contradict each other: said, and nothing drawn. -->
      <div v-else-if="problem" class="refused" role="alert">
        <h2>This node's cells cannot be shown</h2>
        <p class="detail">{{ problem }}</p>
      </div>

      <!-- No anchor: say why, for the kind of document this node came from. -->
      <template v-else-if="reading.state === 'absent' && absentSource">
        <p v-if="absentSource.kind === 'sheet'" class="plain" data-absent="sheet">
          This node is part of a worksheet, and a spreadsheet is read as cells rather than drawn
          on a page. The <code>cells</code> anchors are on a sheet's table, records and profile,
          and this node carries none.
          <RouterLink :to="`/node/${absentSource.sheetId}/preview`">Read the sheet's cells</RouterLink>.
        </p>
        <p
          v-else-if="absentSource.kind === 'file' && absentSource.format === 'pdf'"
          class="plain"
          data-absent="pdf"
        >
          This node has no recorded place on a page of its PDF. An anchor is written by the
          <code>citation</code> rule, for the chunks of a PDF's text layer; this node is not one
          it anchored.
        </p>
        <p
          v-else-if="absentSource.kind === 'file' && absentSource.format === 'office'"
          class="plain"
          data-absent="office"
        >
          This node came from an office document and has no recorded place in it. A Word or
          PowerPoint passage reaches a page through the PDF <code>render:pdf</code> makes of the
          document: <code>anchor:render</code> writes that rectangle, and
          <code>anchor:source</code> writes the passage's address in the package. Neither is on
          this node, so its document has no rendition yet or this node is not a chunk.
        </p>
        <p v-else-if="absentSource.kind === 'file'" class="plain" data-absent="other">
          This node came from an upload of type
          <code>{{ absentSource.mime ?? "unrecognised" }}</code>, and no rule in this appliance
          records where a passage of that format sits on a page.
        </p>
        <p v-else class="plain" data-absent="none">
          This node is not under an uploaded file, so it has no source document to be placed in.
        </p>
      </template>
      <p v-else-if="reading.state === 'unresolved'" class="plain" :data-unresolved="reading.row">
        <template v-if="reading.row === 'render_anchor'">
          The appliance looked for this passage in the PDF it rendered of the document and could
          not place it:
        </template>
        <template v-else>The appliance tried to find this node's place in its source and could not:</template>
        <span class="detail">{{ reading.reason }}</span>
        <span v-if="reading.code" class="value"> ({{ reading.code }})</span>
      </p>
      <p v-else-if="reading.state === 'other' && reading.kind === 'ooxml'" class="plain">
        This node is addressed at an element of its office package (<code>ooxml</code>), which
        is exact and has no page. A rectangle comes from <code>anchor:render</code> over the PDF
        <code>render:pdf</code> makes of the document, and neither has run for this node.
      </p>
      <p v-else-if="reading.state === 'other'" class="plain">
        This node's anchor is of kind <code>{{ reading.kind }}</code>, which has no rectangle on
        a page and no cells, so this view has nothing to show for it.
      </p>
      <div v-else-if="reading.state === 'malformed'" class="refused" role="alert">
        <h2>This node's stored anchor is not usable</h2>
        <p class="detail">{{ reading.why }}</p>
      </div>

      <div v-else-if="cells" class="surfaces">
        <Panel
          :title="reading.state === 'cells' ? `Cells ${reading.ref}` : 'The sheet'"
          :note="`${cells.row_count} rows · ${cells.cell_count} cells`"
        >
          <CellsGrid :cells="cells" />
        </Panel>
        <Panel v-if="nodeText" title="The node">
          <p class="said"><span class="apparatus-voice">The node says</span> {{ nodeText }}</p>
        </Panel>
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
            <p v-if="pageDpi !== PAGE_DPI" class="apparatus-voice" data-retried="page">
              Rendered at {{ pageDpi }} dpi: the appliance refused {{ PAGE_DPI }} dpi for this
              page's size and named {{ pageDpi }} as the density that fits.
            </p>
            <p v-if="anchor && anchor.continues.length > 0" class="apparatus-voice">
              The passage continues on
              {{ anchor.continues.map(pageLabel).join(", ") }}. The box covers only where it
              begins.
            </p>
          </Panel>

          <Panel v-if="cropUrl" title="The appliance's crop" :note="`${cropDpi} dpi`">
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
