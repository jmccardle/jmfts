// A transcript, rendered as turns. `docs/SPRINT_0_6_0.md` Block F step 29.
//
// STEP 25 LEFT THIS ONE UNFINISHED FOR A STATED REASON, AND THE REASON WAS WRONG BY ONE
// GREP. Its note reads: "a transcript renderer has to know how a turn is delimited in
// `rendered_content`, and nothing in this tree states that shape. Guessing it here would put a
// guess behind an interface five later views plug into." The first half is exactly right and
// the second is the thing to check rather than assume — the tree DOES state the shape, in the
// one function that writes it:
//
//   `jmfts_core/conversation_ingest.py:164` conversation_markdown(messages)
//       "\n\n".join(f"[{m.role}]: {m.content}" for m in messages)
//
// That is the writer. `structure_tasks._extract_conversation` (`:301`) is its only caller and
// it puts the result on the file node's `content`, which is what `/view/{id}` serves as
// `rendered_content`. So the delimiter is not guessed here; it is read off the writer, and
// this module names it so that a change to that function fails against this comment.
//
// A TURN IS A MARKER LINE PLUS EVERYTHING UNTIL THE NEXT ONE, and that is not the same rule as
// "split on the blank line between turns". A message's own content may contain blank lines —
// the writer joins with "\n\n" and does not escape anything — so splitting on blank lines
// would cut a long turn into several turns with no speaker, which is the kind of wrongness
// that reads as a badly formatted transcript rather than as a parser that is wrong.
//
// CONTENT BEFORE THE FIRST MARKER IS SHOWN, NOT DROPPED. Nothing in this tree writes a
// preamble, so meeting one means this content came from somewhere else; rendering it under a
// label that says so is what makes that visible instead of invisible.
//
// A TURN'S CONTENT IS MARKDOWN. The writer puts a message body through untouched, and every
// message body this appliance has ingested came out of an assistant transcript where markdown
// is what the participants wrote. `render/markdown.js` is the same renderer the `markdown`
// value gets, so a fenced code block in a turn renders as a fenced code block.

import { renderMarkdown } from "./markdown.js";

/** Content the transcript renderer cannot read as turns. */
export class TranscriptError extends Error {
  constructor(message) {
    super(message);
    this.name = "TranscriptError";
  }
}

/**
 * The marker `conversation_ingest.conversation_markdown` writes, as a pattern.
 *
 * Exported because it is the one fact this module reads out of another module's behaviour,
 * and `tests/test_web_renderers.py` builds its fixture with the Python writer's own format
 * string rather than by hand — so the day that function changes, the test fails here rather
 * than a reader meeting a transcript rendered as one long turn.
 */
export const TURN_MARKER = /^\[([^\]\n]+)\]:[ \t]?([\s\S]*)$/;

/**
 * One transcript as an element.
 *
 * @param {string} source the node's `rendered_content`
 * @param {string} [className]
 * @returns {Element}
 */
export function renderTranscript(source, className = "render-transcript") {
  if (typeof source !== "string") {
    throw new TranscriptError(
      `the transcript renderer was given ${typeof source}, not a string. A node whose ` +
        "content is null has no transcript to render."
    );
  }
  const turns = readTurns(source);
  if (!turns.some((turn) => turn.role !== null)) {
    // The same refusal `json-table` makes for content that is not JSON, and for the same
    // reason: a usetype whose presentation row says `transcript` and whose content carries no
    // turn at all is a data fault somebody has to see. Rendering it as one anonymous block
    // would make a transcript that was never parsed look like a transcript with one speaker.
    throw new TranscriptError(
      "this content carries no `[role]: ` turn marker, so it is not a transcript as " +
        "jmfts_core/conversation_ingest.py:164 writes one. Either the content or the " +
        "usetype's presentation row is wrong."
    );
  }

  const root = document.createElement("div");
  root.className = className;
  for (const turn of turns) {
    const block = document.createElement("section");
    block.className = turn.role === null ? "render-turn render-turn-unmarked" : "render-turn";
    const speaker = document.createElement("h4");
    speaker.className = "render-turn-role";
    speaker.textContent =
      turn.role === null ? "before the first turn, with no speaker named" : turn.role;
    block.append(speaker, renderMarkdown(turn.content, "render-turn-content"));
    root.append(block);
  }
  return root;
}

/**
 * The turns in one transcript, in order.
 *
 * Exported for the test, and because a view that wants to count turns or jump to one should
 * read them rather than re-split the string a second way.
 *
 * @returns {{role: string|null, content: string}[]} `role` is null only for content that
 *   precedes the first marker.
 */
export function readTurns(source) {
  const turns = [];
  let current = null;
  for (const line of source.split(/\r\n|\r|\n/)) {
    const marker = TURN_MARKER.exec(line);
    if (marker) {
      current = { role: marker[1], content: marker[2] ? [marker[2]] : [] };
      turns.push(current);
      continue;
    }
    if (current === null) {
      if (!line.trim()) continue;
      current = { role: null, content: [line] };
      turns.push(current);
      continue;
    }
    current.content.push(line);
  }
  return turns.map((turn) => ({ role: turn.role, content: turn.content.join("\n").trim() }));
}
