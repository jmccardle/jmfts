// What the appliance's own words mean, in the reader's words.
//
// Every value here is a value the API hands over — a usetype, a lifecycle state, a task name.
// This module does not invent any of them and does not normalise any of them: an unglossed
// value renders as itself, because a gloss that guessed would be a sentence the appliance
// never said. `gloss()` returning null is the honest answer and the components draw the raw
// value when it does.

export type Tone = "plain" | "active" | "quiet" | "alarm";

export interface Gloss {
  /** One line, in the reader's language, for a title attribute or the apparatus. */
  meaning: string;
  tone?: Tone;
}

/** What the nodes of this corpus are. Keys are `documents.usetype` values. */
const USETYPES: Record<string, Gloss> = {
  file: { meaning: "An uploaded file, as the top of its own tree." },
  section: { meaning: "A heading and the text under it, cut on the document's own headings." },
  segment: { meaning: "A run of neighbouring chunks that a topic boundary grouped together." },
  chunk: { meaning: "A passage cut from a larger document, sized to fit the embedder." },
  summary: { meaning: "Written about other nodes rather than read from a source." },
  derived: { meaning: "Produced by a rule from other nodes; held out of search results." },
  entity: { meaning: "A thing facts are asserted about, not a piece of text." },
  cell: { meaning: "A spreadsheet region." },
  conversation: { meaning: "A thread of turns between speakers." },
  transcript: { meaning: "Speech, written down." },
  raw: { meaning: "Text kept exactly as it arrived." },
  markdown: { meaning: "A markdown document." },
};

/** `documents.settled` — whether this node is an answer yet. */
const LIFECYCLE: Record<string, Gloss> = {
  settled: { meaning: "Finished ingesting. Retrieval can return it.", tone: "plain" },
  in_flight: {
    meaning: "Still ingesting. Retrieval cannot return it yet, and what is here may change.",
    tone: "active",
  },
  failed: { meaning: "Ingestion stopped on this node. It will not appear in results.", tone: "alarm" },
};

/** `documents.content_source` — whether the text below is stored or was just computed. */
const CONTENT_SOURCE: Record<string, Gloss> = {
  stored: { meaning: "This node's own text, as stored." },
  effective: {
    meaning:
      "Computed at read time by joining this node's children. Nothing stores it, because the " +
      "subtree it came from already does.",
    tone: "active",
  },
};

/** Evidence rows, keyed as `jmfts_core.evidence.REGISTRY` names them. */
const EVIDENCE: Record<string, Gloss> = {
  attempts: { meaning: "Every ingest task that ran on this node, in order, with its result." },
  effective_content: { meaning: "How this node's text was joined from its children." },
  extraction: { meaning: "How text was read out of the original bytes." },
  file: { meaning: "The uploaded bytes: name, size, and where they are kept." },
  matched: { meaning: "What probing the bytes found, before anything was read." },
  options: { meaning: "The ingest settings this node was processed under." },
  structure: { meaning: "How the document was divided, and how much of it the division covered." },
  source_anchor: { meaning: "Where in the original this passage sits." },
  "source_anchor.unresolved": { meaning: "Why the place in the original could not be recovered." },
  render_anchor: { meaning: "Where this passage sits in the rendered copy." },
  "render_anchor.unresolved": { meaning: "Why the place in the rendered copy could not be recovered." },
  rendition: { meaning: "The PDF this document was converted to, so pages could be shown." },
};

export const glossUsetype = (value: string | null | undefined): Gloss | null =>
  (value && USETYPES[value]) || null;

export const glossLifecycle = (value: string | null | undefined): Gloss | null =>
  (value && LIFECYCLE[value]) || null;

export const glossContentSource = (value: string | null | undefined): Gloss | null =>
  (value && CONTENT_SOURCE[value]) || null;

export const glossEvidence = (key: string): Gloss | null => EVIDENCE[key] || null;

/**
 * Who wrote this node.
 *
 * `produced_by` holds the name of the ingest rule that wrote the node, and NULL means a person
 * asserted it — an upload, an import, an edit. A person editing a produced node clears the
 * column, so NULL after an edit is a real answer rather than a gap. This is the distinction the
 * console carries in the margin rather than in a colour.
 */
export interface Hand {
  kind: "rule" | "asserted";
  label: string;
  meaning: string;
}

export function handOf(producedBy: string | null | undefined): Hand {
  if (!producedBy) {
    return {
      kind: "asserted",
      label: "asserted",
      meaning: "A person put this here — uploaded, imported, or edited it. No rule wrote it.",
    };
  }
  return {
    kind: "rule",
    label: producedBy,
    meaning: `Written by the ingest rule ${producedBy}.`,
  };
}

/** A count, with its noun, and no count of one written as a plural. */
export function count(n: number, singular: string, plural = `${singular}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? singular : plural}`;
}

/** A timestamp as the reader would say it, with the exact value kept for the title. */
export function when(iso: string | null | undefined): { text: string; exact: string } | null {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  const exact = at.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
  const seconds = (Date.now() - at.getTime()) / 1000;
  const text =
    seconds < 90
      ? "just now"
      : seconds < 5400
        ? `${Math.round(seconds / 60)} min ago`
        : seconds < 172800
          ? `${Math.round(seconds / 3600)} hr ago`
          : at.toISOString().slice(0, 10);
  return { text, exact };
}
