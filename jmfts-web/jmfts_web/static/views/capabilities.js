// The capability landing: what this appliance accepts, indexes and retrieves.
//
// docs/SPRINT_0_6_0.md Block F steps 23 and 24. This was the whole front end; step 24 made it
// the first registered view, and it is the worked example of IC-7 — a module that declares
// itself and is reached only because `shell/manifest.js` imports it.
//
// MOVED, NOT REWRITTEN. Every line below was `render()` in `index.html`, and it is here in the
// state it was in, three defects included: a visual pass on 2026-09-14 found that this table
// reads three field names `CapabilitiesResponse` does not carry. They are fixed in the very
// next commit, with the test that catches them, and they are moved unchanged first so that the
// diff which fixes them shows the three names and nothing else.

import { registerView } from "../shell/registry.js";

const make = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const pills = (items, live) => {
  const box = make("span");
  if ((items || []).length === 0) {
    box.append(make("span", "dim", "none"));
    return box;
  }
  for (const item of items) {
    box.append(make("span", live && live.includes(item) ? "pill on" : "pill", String(item)));
  }
  return box;
};

function rows(cap, operationCount) {
  const e = cap.extras || {};
  const installed = Object.keys(e).filter((k) => e[k]?.installed ?? e[k]);
  return [
    ["Version", make("span", null, cap.version ?? "unknown")],
    ["Extras", pills(Object.keys(e), installed)],
    [
      "Can produce a vector",
      cap.embedding?.local_model_available
        ? make("span", null, "yes, in this process")
        : cap.embedding?.runner_url
          ? make("span", null, `yes, via ${cap.embedding.runner_url}`)
          : make("span", "bad", "no — neither a local model nor a runner"),
    ],
    ["Formats identified", pills(cap.formats)],
    ["Ingest entry points", pills(cap.ingest?.usetypes)],
    ["Retrieval methods", pills(cap.retrieval?.methods)],
    ["Held out of every result", pills(cap.retrieval?.excluded_usetypes)],
    // The one row that is NOT from a call, and it says so. Property 2 of step 23 is that a
    // view showing something the log has no call for computed it in the browser; a page
    // claiming that has to hold itself to it, and the way to hold to it is to label the
    // exception rather than to quietly have one.
    ["Operations this client knows", make("span", null, String(operationCount))],
  ];
}

async function component({ client }) {
  const page = make("div", "view-capabilities");
  page.append(make("h1", null, "JMFTS"));
  page.append(
    make(
      "p",
      "lede",
      "Matryoshka embeddings, ColBERT-style late interaction, and BM25 over PostgreSQL. " +
        "Every row below is read from /capabilities, which makes no network call of its own."
    )
  );

  // `/health` is the one path in PUBLIC_PATHS, so this answers with no credential — which is
  // what lets the next failure read as "the token is wrong" rather than "nothing is running".
  // Both failures propagate to the shell, which shows the message rather than a blank page.
  try {
    await client.health_check_liveness();
  } catch (error) {
    throw new Error(`the appliance is not answering /health: ${error.message}`);
  }

  const cap = await client.capabilities({ corpus: true });

  const table = make("table", "capability-table");
  for (const [label, value] of rows(cap, Object.keys(client.operations).length)) {
    const line = make("tr");
    const head = make("th", null, label);
    const cell = make("td");
    cell.append(value);
    line.append(head, cell);
    table.append(line);
  }
  page.append(table);
  return page;
}

registerView({
  id: "capabilities",
  title: "This appliance",
  path: "/capabilities",
  component,
  tags: ["appliance"],
});
