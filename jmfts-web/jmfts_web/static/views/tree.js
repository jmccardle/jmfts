// The tree browser: a JMFTS document is a tree, and this is the page that walks it.
//
// docs/SPRINT_0_6_0.md Block F step 28, the first half. The detail view beside it is
// `views/document.js`; this one answers "what is in here", that one answers "what is this
// node". They are two views and not one page with a mode, because they take different
// arguments and a reader arrives at each from a different place — a link into a subtree, and
// a search hit.
//
// ---------------------------------------------------------------------------------------
// WHY THIS READS `/documents/*` AND NOT `/view/*`, WHICH IS THE OTHER HALF OF THE STEP
//
// `GET /view/{id}` carries `children_stubs`, which look like exactly what a tree wants: a
// title, a preview and a `child_count` per child, in one call. They are the wrong rows for a
// TREE, and the reason is in `jmfts_core/view_renderer.py:107` — `build_children_stubs`
// returns `[]` when the usetype's presentation says `child_handling: "hidden"`. That is a
// correct rendering decision for a reading view and a catastrophe for a browser: a subtree
// would be absent from the tree because a row in `usetype_presentations` said not to inline
// it, and nothing on the page would say so.
//
// So the tree reads `GET /documents/{id}/children`, which is the tree as the database holds
// it — every child, whatever any presentation row says about showing it. The detail view
// reads the presentation-filtered stubs, because that IS its job, and it says out loud when
// the presentation hid something and points here.
//
// ---------------------------------------------------------------------------------------
// ONE PARENT, SEVERAL CHILD SHAPES, AND THE TREE HAS TO SHOW ALL OF THEM
//
// Block C landed at M1 and a worksheet can now emit BOTH a `record` shape and a `table` shape
// for the same sheet: `jmfts_core/models/document.py` at USETYPE_TABLE says they are "two
// renderings of one sheet, they are separate documents, and a query matching both returns
// both", and `sheet.shape` is a LIST for exactly that reason. A browser that drew one list of
// children and left it at that would be truthful and unreadable — 400 `record` nodes with a
// `table` and a `profile` lost among them.
//
// `byUsetype()` below is the answer: children are grouped by usetype in first-appearance
// order, each group headed by its usetype and its count, so "this sheet has 400 records AND a
// table AND a profile" is the first thing the page says rather than something a reader has to
// count. A parent with one child shape gets one group and reads as an ordinary list.

import { registerView } from "../shell/registry.js";

/** A route argument this view cannot honour, with the reason in the message. */
export class TreeParamError extends Error {
  constructor(message) {
    super(message);
    this.name = "TreeParamError";
  }
}

/**
 * The most children this view asks for in one call.
 *
 * `GET /documents/{id}/children` takes a `limit` and has no `offset`, so there is no page 2
 * to ask for — raising the limit is the only way to see more, and doing that silently in a
 * loop is how a browser hangs on the 21-workbook subtree in `docs/STRESS_CORPUS.md`. When a
 * group comes back at exactly this size the page SAYS the list may be short rather than
 * implying it is complete, and the call is in the shell's log where step 30's view can replay
 * it with a bigger limit. That is the honest version of "load more" for a route with no
 * cursor.
 */
export const CHILD_LIMIT = 200;

/**
 * Load this view's stylesheet, once.
 *
 * `shell/shell.css:4` is the rule — a view adds its own styling from its own module, because
 * index.html is the file five F3 worktrees would otherwise all edit. The URL is resolved
 * against this module's, so it is the same origin by construction and there is no path here
 * for a CDN to arrive through.
 *
 * At MOUNT and not at import: a module that touches `document.head` when it is imported is a
 * module that cannot be imported for its registration alone, and `shell/manifest.js` imports
 * every view exactly that way.
 */
let linked = false;
function linkStyles() {
  if (linked) return;
  const sheet = document.createElement("link");
  sheet.rel = "stylesheet";
  sheet.href = new URL("./tree.css", import.meta.url).href;
  document.head.append(sheet);
  linked = true;
}

const make = (tag, className, text) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
};

const link = (className, text, href) => {
  const element = make("a", className, text);
  // setAttribute, not the reflected property. They are the same thing to a browser, and the
  // attribute is the one a test can read back — `tests/test_web_renderers.py`'s `deep()`
  // serialises attributes, and the alternative was a second DOM stub that serialises
  // properties, which is the thing this repository has exactly one of.
  element.setAttribute("href", href);
  return element;
};

/**
 * One integer route argument, or null when it was not given.
 *
 * Absent is a reader who did not say — the roots are a sane answer to that. PRESENT AND
 * UNREADABLE is not: `#/documents?root=fortytwo` is a link somebody pasted wrong, and showing
 * them the roots instead would be answering a question they did not ask.
 */
export function integerParam(params, name) {
  const raw = params[name];
  if (raw === undefined || raw === "") return null;
  if (!/^\d+$/.test(raw)) {
    throw new TreeParamError(
      `${name}=${JSON.stringify(raw)} is not a document id. A document id is a whole number, ` +
        `as in #/documents?${name}=42.`
    );
  }
  return Number(raw);
}

/**
 * Children grouped by usetype, in the order the usetypes first appear.
 *
 * `[[usetype, [child, ...]], ...]` rather than an object, for `navigationGroups()`'s reason in
 * `shell/registry.js`: the order is the server's row order and not whatever key order a JS
 * engine happens to give. A child with no usetype at all groups under `null` and is LABELLED
 * as having none — a node whose usetype is missing is a node no rule claims, which is worth
 * seeing rather than smoothing into "document".
 */
export function byUsetype(children) {
  const groups = new Map();
  for (const child of children) {
    const key = child.usetype ?? null;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(child);
  }
  return [...groups.entries()].map(([usetype, members]) => [usetype, members]);
}

const PREVIEW_CHARS = 140;

/**
 * One line of the node's text, and how much of it that is.
 *
 * CUT THE DRAWING, NEVER THE DATA, AND SAY WHAT WAS CUT. An ellipsis says something was left
 * out; it does not say whether that was six characters or sixty thousand, and a row that is
 * 140 characters of a chunk reads identically to a row that is 140 characters of a whole
 * workbook. The count is the difference between a preview and a claim.
 */
function preview(node) {
  const text = (node.content || "").replace(/\s+/g, " ").trim();
  if (!text) return null;
  const cut = text.length > PREVIEW_CHARS;
  const line = make("p", "tree-preview", cut ? `${text.slice(0, PREVIEW_CHARS)}…` : text);
  if (cut) {
    line.append(
      make("span", "dim", ` (first ${PREVIEW_CHARS} of ${text.length} characters)`)
    );
  }
  if (node.content_source === "effective") {
    // A container's `content` is NULL in the column and filled by projection from its
    // subtree (`contracts/document.py`, DocumentResponse.content_source). Saying so is the
    // difference between "this node holds this text" and "this text is its children's".
    line.append(make("span", "dim", " — effective content, computed from the subtree"));
  }
  return line;
}

/**
 * One row: what the node is, and the two places a reader can go from it.
 *
 * Both destinations are `<a href="#…">` rather than click handlers, so they are copyable,
 * middle-clickable and survive a reload — the shell routes on the fragment and a link is the
 * whole of navigation. The only handler here is the expander, which is not navigation.
 */
export function nodeRow(node, { client }) {
  const box = make("div", "tree-node");
  box.setAttribute("data-id", String(node.id));
  const row = make("div", "tree-row");

  const expander = make("button", "tiny tree-expander", "+");
  expander.type = "button";
  const children = make("div", "tree-children");
  children.hidden = true;

  const title = link(
    node.title ? "tree-title" : "tree-title untitled",
    node.title || "(untitled)",
    `#/document?id=${node.id}`
  );
  row.append(expander, title);
  row.append(make("span", "pill", node.usetype || "no usetype"));
  row.append(make("span", "dim mono", `#${node.id}`));

  // Retrieval indexes are partial on `settled` (CLAUDE.md, "Queued ingest"), so an in-flight
  // node is invisible to every search while being perfectly visible here. A browser that did
  // not say so would be the one place the two surfaces disagree without explanation.
  if (node.settled !== "settled") {
    row.append(
      make("span", "bad", `${node.settled} — not yet reachable by any search`)
    );
  }
  if (node.produced_by) row.append(make("span", "dim", `by ${node.produced_by}`));
  row.append(link("dim", "browse here", `#/documents?root=${node.id}`));

  box.append(row);
  const body = preview(node);
  if (body) box.append(body);
  box.append(children);

  let loaded = false;
  const toggle = async () => {
    if (loaded) {
      children.hidden = !children.hidden;
      expander.textContent = children.hidden ? "+" : "−";
      return;
    }
    expander.disabled = true;
    expander.textContent = "…";
    try {
      await expandInto(children, node.id, { client });
      loaded = true;
      children.hidden = false;
      expander.textContent = "−";
    } catch (error) {
      // A click handler is outside the shell's catch, so this is where the Fail Early rule
      // is done for an expansion: the failure lands where the children would have been, with
      // the message the appliance sent, and the row stays usable.
      children.textContent = "";
      children.append(make("p", "bad", error.message));
      children.hidden = false;
      expander.textContent = "!";
    } finally {
      expander.disabled = false;
    }
  };
  expander.addEventListener("click", toggle);

  return { element: box, children, expander, toggle };
}

/**
 * Fetch one node's children and draw them into `container`, grouped by usetype.
 *
 * Exported because it is the whole of what expanding a row does, and a click event is not a
 * thing a headless test can honestly produce: `tests/test_web_tree_view.py` calls this the way
 * the handler does.
 */
export async function expandInto(container, documentId, { client, limit = CHILD_LIMIT } = {}) {
  const children = await client.get_children({ document_id: documentId, limit });
  container.textContent = "";
  if (children.length === 0) {
    container.append(make("p", "dim", "no children"));
    return [];
  }

  const groups = byUsetype(children);
  for (const [usetype, members] of groups) {
    // The heading is shown even for a single group: "12 chunk" is a fact about the node, and
    // a heading that appears only sometimes is one a reader has to notice the absence of.
    container.append(
      make("p", "tree-group", `${members.length} × ${usetype ?? "no usetype"}`)
    );
    for (const child of members) container.append(nodeRow(child, { client }).element);
  }

  if (children.length === limit) {
    container.append(
      make(
        "p",
        "dim",
        `${limit} children is the most this page asks for in one call, and exactly ${limit} ` +
          "came back — so there may be more. GET /documents/{id}/children takes a larger " +
          "`limit`; the call is in the log below, ready to replay with one."
      )
    );
  }
  return groups;
}

/** Root-first ancestors as a crumb trail, each one a link into the tree at that node. */
function crumbs(ancestors, node) {
  const bar = make("div", "tree-crumbs");
  bar.append(link("dim", "all roots", "#/documents"));
  for (const ancestor of ancestors) {
    bar.append(make("span", "dim", "/"));
    bar.append(link(null, ancestor.title || `#${ancestor.id}`, `#/documents?root=${ancestor.id}`));
  }
  bar.append(make("span", "dim", "/"));
  bar.append(make("span", null, node.title || `#${node.id}`));
  return bar;
}

async function component({ client, params }) {
  linkStyles();
  const root = integerParam(params, "root");
  const page = make("div", "view-tree");
  page.append(make("h1", null, "Documents"));

  if (root === null) {
    page.append(
      make(
        "p",
        "lede",
        "Every root of the corpus. A root is a document with no parent — an upload, an " +
          "import, or anything created without one. Expand a row to walk down; the children " +
          "are grouped by usetype, because one parent routinely has several child shapes."
      )
    );
    const roots = await client.get_root_documents();
    if (roots.length === 0) {
      page.append(
        make(
          "p",
          "dim",
          "No document in this appliance is without a parent, so there is nothing to start " +
            "from. An empty corpus reads the same way; /capabilities counts the documents."
        )
      );
      return page;
    }
    for (const node of roots) page.append(nodeRow(node, { client }).element);
    return page;
  }

  // A focused subtree. Two calls and they are the two questions a reader arriving on a deep
  // link has: what is this node, and how did I get here.
  const [node, ancestors] = await Promise.all([
    client.get_document({ document_id: root }),
    client.get_ancestors({ document_id: root }),
  ]);
  page.append(crumbs(ancestors, node));
  page.append(
    make("p", "lede", `The subtree under #${node.id}. Its own page is linked from the row.`)
  );

  const row = nodeRow(node, { client });
  page.append(row.element);
  // A focused root arrives open. Asking a reader to click the one node they navigated TO
  // would be a step with no decision in it.
  await row.toggle();
  return page;
}

registerView({
  id: "tree",
  title: "Tree",
  path: "/documents",
  component,
  tags: ["corpus", "documents"],
});
