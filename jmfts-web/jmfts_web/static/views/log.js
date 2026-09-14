// The call log: every request this page has made, newest first, with what went out and what
// came back.
//
// docs/SPRINT_0_6_0.md Block F step 30, over step 23's IC-6 event stream. The shell has held
// that stream since step 24 and says at its own line 11 that this step takes it: `callLog()`
// is the history and `onCallLog()` is the subscription, and they are the only two things this
// module needs from it.
//
// WHY A WHOLE PAGE FOR THIS. Step 23's second property is that a view showing something the
// log has no event for computed it in the browser. That property is checkable by a test today
// and by nobody else: the shell's panel is four lines and a status code. This page is the same
// claim made READABLE — every row prints the `curl` that would have produced it, so somebody
// demoing this appliance can open the page they were just on, come here, and see that nothing
// happened in the browser that could not have happened over `curl`. That is an argument a
// reader can check rather than one they have to take from the source.
//
// WHAT THIS VIEW HOLDS: nothing. The events live in the shell's array, which is the one copy;
// this module renders from it and keeps no second list, because a log view that accumulated
// its own would be a second answer to "what did this page call" and the two would drift the
// first time a view called something before this page was ever opened.
//
// WHAT IT RENDERS: a bounded amount of each event, and it says where the bound fell. See
// PREVIEW_CHARACTERS for the measurement that set it.

import { asCurl, asFetch, asPython, detailOf } from "../client/index.js";
import { registerView } from "../shell/registry.js";
import { callLog, onCallLog } from "../shell/shell.js";

/**
 * How many characters of one event's `body` or `response` this page draws.
 *
 * A preview, and it has to be, because a response can be enormous and the whole log is on one
 * page. Measured against the shapes this appliance actually returns rather than picked:
 *
 * * `GET /documents/{id}?include_embed=true` answers with `DocumentResponse.embed`, which is
 *   768 floats — about 15 kB of JSON for ONE document, and the row is a metadata lookup nobody
 *   would expect to be large.
 * * `POST /search/hybrid` answers `SearchResponse.results`, up to `limit` (max 100) of
 *   `SearchResultItem`, each carrying a whole `DocumentResponse` — `content`, the chunk's full
 *   text, plus `structured_content` and `path`. A hundred hits is a few hundred kB.
 * * `POST /runner/embed/tokens` answers with `token_embeddings`, base64 float16 for every kept
 *   token of the text.
 *
 * So 4000 characters is roughly "a screen and a half of pretty-printed JSON": enough to read
 * the shape and the first hits, small enough that fifty rows of it are still a page. What is
 * NOT done is truncating the event — the shell still holds the whole thing, `replay()` still
 * re-sends it, and the copy-out below still prints the full request. Only the drawing is cut,
 * and every cut says how much it cut.
 */
export const PREVIEW_CHARACTERS = 4000;

/**
 * Response fields this page refuses to draw, by operation id.
 *
 * `JMFTS_API_TOKEN` cannot reach an event in the first place: `transport.js` puts the
 * credential in a request HEADER, and IC-6's nine fields carry no headers. That is the
 * property `tests/test_web_log_view.py` pins, and it is a property of the contract rather than
 * of this file's care.
 *
 * ONE response body is a different matter. `POST /access/principals/{id}/tokens` answers with
 * `TokenMintResponse.token` — "The raw bearer token. Shown once; store it now." — and it is
 * the only field in the whole contract package that is a live credential (grep for "raw bearer
 * token"). A minted token rendered into a scrolling log is a credential sitting on a screen
 * that is very often being shared, long after the moment anybody meant to look at it.
 *
 * So the log declines to draw that one value, SAYS SO where it would have been, and hides
 * nothing else. It is not redaction-in-general and it is not a filter over field names that
 * look sensitive: it is one operation, named, with the contract that makes it a credential
 * cited. `tests/test_web_log_view.py::test_the_log_declares_every_response_field_that_is_a_credential`
 * resolves this table against `jmfts_core.registry.REGISTRY` and the contract models, so an
 * operation that starts returning a credential fails the suite rather than appearing here.
 *
 * The value is not lost by this: it is in the response object the CALLING view received, which
 * is where "shown once" was always going to have to be honoured.
 */
export const REDACTED_RESPONSE_FIELDS = Object.freeze({
  "AccessService.mint_token": Object.freeze(["token"]),
});

/** What stands in for a value this page will not draw. */
export const REDACTION_MARK = "<<not drawn by the call log — see REDACTED_RESPONSE_FIELDS>>";

// --------------------------------------------------------------------------------- drawing

const make = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

/** This view's own stylesheet, linked once, from this module rather than from `index.html`. */
let linked = false;
function linkStyles() {
  if (linked) return;
  linked = true;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  // Resolved against this module's own URL: same origin, no configured path, and it keeps
  // working wherever the bundle is mounted. `index.html` is the file five F3 worktrees would
  // otherwise all have edited, which is the conflict IC-7 exists to remove.
  link.href = new URL("./log.css", import.meta.url).href;
  document.head.append(link);
}

/**
 * One value, as at most :data:`PREVIEW_CHARACTERS` of text, plus what kind of value it was.
 *
 * The three non-JSON kinds are not edge cases, they are the response types the transport
 * documents: a binary operation resolves to a `Blob`, a request that never completed carries
 * the `Error` itself as its response (with `status` 0), and a route answering something other
 * than JSON hands back text. Printing `JSON.stringify` over any of them gives `{}`, which is a
 * page saying an empty object came back when one did not.
 *
 * @returns {{kind: string, text: string, shown: number, total: number}}
 */
export function preview(value, { redact = [] } = {}) {
  if (value === undefined) return { kind: "absent", text: "", shown: 0, total: 0 };
  if (value === null) return { kind: "null", text: "null", shown: 4, total: 4 };
  if (typeof Blob !== "undefined" && value instanceof Blob) {
    const text = `${value.size} bytes of ${value.type || "an undeclared type"}`;
    return { kind: "blob", text, shown: text.length, total: text.length };
  }
  if (value instanceof Error) {
    const text = `${value.name}: ${value.message}`;
    return { kind: "error", text, shown: text.length, total: text.length };
  }

  let kind = "json";
  let subject = value;
  if (redact.length && typeof value === "object" && !Array.isArray(value)) {
    const hidden = redact.filter((name) => name in value);
    if (hidden.length) {
      subject = { ...value };
      for (const name of hidden) subject[name] = REDACTION_MARK;
    }
  }
  let text;
  if (typeof subject === "string") {
    kind = "text";
    text = subject;
  } else {
    text = JSON.stringify(subject, null, 2);
  }
  const total = text.length;
  return {
    kind,
    text: total > PREVIEW_CHARACTERS ? text.slice(0, PREVIEW_CHARACTERS) : text,
    shown: Math.min(total, PREVIEW_CHARACTERS),
    total,
  };
}

/** A labelled block, with the note that says what was withheld and why, when something was. */
function block(label, shown, note) {
  const box = make("div", "log-block");
  box.append(make("h3", null, label));
  if (shown.kind === "absent") {
    box.append(make("p", "dim", "none"));
    return box;
  }
  box.append(make("pre", "copy", shown.text));
  const said = [];
  if (shown.kind === "blob") {
    said.push("bytes, not text — the log names them rather than drawing them");
  }
  if (shown.kind === "error") {
    said.push("the request did not complete; this is what it threw");
  }
  if (shown.total > shown.shown) {
    said.push(
      `showing the first ${shown.shown} of ${shown.total} characters — the event itself is whole`
    );
  }
  if (note) said.push(note);
  if (said.length) box.append(make("p", "dim note", said.join(". ")));
  return box;
}

/** The query string as it went out, so the summary line reads like the request did. */
function queryText(query) {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(query)) {
    for (const one of Array.isArray(value) ? value : [value]) params.append(name, String(one));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

/**
 * One event, collapsed.
 *
 * `<details>` rather than a click handler, and that is a decision: the summary is what a reader
 * scans and the bodies are what they open one of, so the page must not draw a hundred open JSON
 * blocks — and the browser already has an element for exactly that, with the keyboard handling
 * and the accessibility semantics already done. A hand-rolled expander would be a worse copy of
 * it that this view would then own.
 */
function row(event, options) {
  const item = make("details", "log-call");
  const failed = event.status === 0 || event.status >= 400;

  const summary = make("summary");
  const line = `${event.method} ${event.path}${queryText(event.query)}`;
  summary.append(make("span", "mono log-op", line));
  const outcome = make("span", failed ? "bad log-status" : "log-status");
  outcome.textContent = event.status === 0 ? "no response" : String(event.status);
  summary.append(outcome);
  summary.append(make("span", "dim log-ms", `${event.ms} ms`));
  summary.append(make("span", "dim log-id", event.op_id));
  item.append(summary);

  const body = make("div", "log-body");
  if (failed) {
    // The reason, on the row, without opening anything: a page whose failures all read
    // "no response" until you click each one is a page that hides what it is for.
    body.append(make("p", "bad", detailOf(event.response)));
  }

  // The arguments as the CALLER gave them, which is not what the summary line shows: a path
  // parameter is already substituted and percent-encoded up there, and `wiki:arxiv` reads as
  // `wiki%3Aarxiv` on the wire. Both are true and the reader wants the un-encoded one to hand.
  const params = { ...event.path_params, ...event.query };
  body.append(block("Arguments", preview(Object.keys(params).length ? params : undefined)));
  body.append(block("Request body", preview(event.body)));

  const redact = REDACTED_RESPONSE_FIELDS[event.op_id] ?? [];
  const shown = preview(event.response, { redact });
  body.append(
    block(
      "Response",
      shown,
      redact.length && shown.text.includes(REDACTION_MARK)
        ? `${redact.join(", ")} is a live credential and this page does not draw it; the ` +
            "view that made the call received it"
        : null
    )
  );

  const printed = make("div", "log-copy");
  printed.append(make("h3", null, "The same call, three ways"));
  for (const [label, print] of [
    ["curl", asCurl],
    ["python", asPython],
    ["fetch", asFetch],
  ]) {
    printed.append(make("h4", "dim", label));
    printed.append(make("pre", "copy", print(event, options)));
  }
  body.append(printed);

  item.append(body);
  return item;
}

/** The one line that says what the whole log adds up to. */
function tally(events) {
  const failed = events.filter((e) => e.status === 0 || e.status >= 400).length;
  const ms = events.reduce((total, e) => total + e.ms, 0);
  if (events.length === 0) return "No calls yet.";
  return (
    `${events.length} call${events.length === 1 ? "" : "s"}, ` +
    `${failed} of them failed, ${ms} ms spent waiting.`
  );
}

async function component() {
  linkStyles();
  const page = make("div", "view-log");
  page.append(make("h1", null, "Call log"));
  page.append(
    make(
      "p",
      "lede",
      "Every request this page has made since the tab was opened, newest first. It is the " +
        "client's own event stream: nothing is added to it and nothing is held out of it, so a " +
        "view showing something with no row here computed it in the browser. Open a row for " +
        "what went out, what came back, and the curl that would have done the same thing."
    )
  );

  const summary = make("p", "dim log-tally");
  page.append(summary);

  const empty = make(
    "p",
    "dim log-empty",
    "This page makes no call of its own, which is why it can be empty: land here first and " +
      "there is genuinely nothing to show. Use another view, then come back."
  );
  page.append(empty);

  const rows = make("div", "log-rows");
  page.append(rows);

  // The origin is the page's own, because these are printed for a reader to paste into a
  // terminal — a relative URL would be a curl invocation that does not run.
  const options = { baseUrl: window.location.origin };

  const events = callLog();
  summary.textContent = tally(events);
  empty.hidden = events.length > 0;
  for (const event of events) rows.append(row(event, options));

  // Live, and it unsubscribes itself once the shell has replaced this tree with another view's.
  // There is no unmount hook to hang that on — the shell mounts, it does not dispose — so the
  // listener asks the DOM whether it is still attached. `isConnected` is a boolean on every
  // Element; a tree the shell dropped answers false on the very next event.
  const off = onCallLog((event) => {
    if (!rows.isConnected) {
      off();
      return;
    }
    const first = rows.firstChild;
    const drawn = row(event, options);
    if (first) rows.insertBefore(drawn, first);
    else rows.append(drawn);
    empty.hidden = true;
    summary.textContent = tally(callLog());
  });

  return page;
}

registerView({
  id: "calls",
  title: "Call log",
  path: "/calls",
  component,
  tags: ["appliance"],
});
