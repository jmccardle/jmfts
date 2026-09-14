// The shell: navigation, token entry, the call log, and the frame a view is mounted into.
//
// docs/SPRINT_0_6_0.md Block F step 24. Everything in this file was in `index.html` before
// this step and is here now for one reason: every view needs it and none of it should be
// written twice. The token, the `sessionStorage` handling under `TOKEN_KEY`, the one client
// instance and the event stream it emits are the shell's, and a view receives what it needs
// through the context object rather than reaching for a global.
//
// `index.html` is now a document with a mount point in it. The page is the shell.
//
// THE CALL LOG STAYS HERE FOR NOW, AND STEP 30 TAKES IT. That step builds the call-log VIEW;
// what this file owns is the stream itself, because the client is the shell's and the events
// start arriving before any view is mounted. `callLog()` and `onCallLog()` below are how step
// 30 reads it without this file having to hand its panel over.
//
// ROUTING IS ON THE FRAGMENT, AND THAT IS FORCED RATHER THAN PREFERRED. The bundle is served
// by `StaticFiles(html=True)` mounted at `/app` (`jmfts_core/rest/main.py:210`), which answers
// `/app/` from `index.html` and answers `/app/search` with 404, because no such file exists.
// Real paths would need a catch-all route, `jmfts_web.index_path()` is written for exactly
// that route, and nothing mounts one — see this lane's report. Until something does, `#/search`
// is the form that survives a reload, and a view reads its arguments from the fragment's own
// query string: `#/document?id=42`.

import { JmftsClient, TOKEN_KEY, asCurl, asFetch, asPython, detailOf } from "../client/index.js";
import { navigationGroups, viewForPath, views } from "./registry.js";

// Every registered view, by the side effect of importing its module. This import is the only
// thing in the shell that knows a view exists; `manifest.js` is the only file that knows
// which ones. See that file for why one shared line per view is still safe to append from two
// worktrees at once.
import "./manifest.js";

/**
 * The one client the whole front end calls.
 *
 * Same origin, and the credential lives in `sessionStorage` under the key the client names —
 * not `localStorage` and never the URL. A closed tab has forgotten it; a pasted address does
 * not carry it. `credentialFromSession` is the transport's default, so nothing here reaches
 * for the value itself.
 */
const client = new JmftsClient();

/**
 * The call log IS the client's event stream, newest first. The shell adds nothing to it and
 * hides nothing from it, which is the property worth having: what the panel shows is every
 * request that went out. A view showing something with no event behind it computed that in
 * the browser, and you can see as much from here — property 2 of step 23.
 */
const CALLS = [];
const LOG_LISTENERS = new Set();

/** Every call this page has made, newest first. Step 30's view reads this. */
export function callLog() {
  return Object.freeze([...CALLS]);
}

/** Subscribe to the log. Returns the unsubscribe function. */
export function onCallLog(listener) {
  LOG_LISTENERS.add(listener);
  return () => LOG_LISTENERS.delete(listener);
}

client.onCall((event) => {
  CALLS.unshift(event);
  for (const listener of LOG_LISTENERS) listener(event);
});

// --------------------------------------------------------------------------------- routing

/**
 * The route the fragment names, as `{path, params}`.
 *
 * An empty fragment is not an error and not a guess: it is a reader who arrived at `/app`
 * without saying where they were going, and the first registered view is where they land.
 * `manifest.js` is append-only, so "first" is stable — the capability landing, which is the
 * page that says what this appliance can do before you ask it to do anything.
 */
export function currentRoute(fragment = window.location.hash) {
  const text = fragment.replace(/^#/, "");
  if (!text) {
    const [first] = views();
    if (!first) throw new Error("no views are registered; shell/manifest.js imports nothing");
    return { path: first.path, params: {} };
  }
  const cut = text.indexOf("?");
  const path = cut === -1 ? text : text.slice(0, cut);
  const query = cut === -1 ? "" : text.slice(cut + 1);
  return { path, params: Object.fromEntries(new URLSearchParams(query)) };
}

/** Go to a route. `params` becomes the fragment's query string. */
export function navigate(path, params = {}) {
  const query = new URLSearchParams(
    Object.entries(params).map(([key, value]) => [key, String(value)])
  ).toString();
  window.location.hash = `#${path}${query ? `?${query}` : ""}`;
}

// ---------------------------------------------------------------------------------- chrome

const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function buildChrome(root) {
  root.textContent = "";

  const header = element("header", "shell-header");
  const brand = element("a", "shell-brand", "JMFTS");
  brand.href = "#";
  const nav = element("nav", "shell-nav");
  header.append(brand, nav);

  const auth = element("form", "shell-auth");
  auth.autocomplete = "off";
  const token = element("input");
  token.type = "password";
  token.id = "shell-token";
  token.placeholder = "JMFTS_API_TOKEN";
  token.setAttribute("aria-label", "API token");
  const connect = element("button", null, "Connect");
  connect.type = "submit";
  const forget = element("button", "ghost", "Forget");
  forget.type = "button";
  const status = element("span", "dim shell-status");
  auth.append(token, connect, forget, status);

  const main = element("main", "shell-view");
  const logSection = element("section", "shell-log-section");
  logSection.append(element("h2", null, "Calls this page made"));
  const log = element("div", "shell-log");
  logSection.append(log);

  root.append(header, auth, main, logSection);
  return { nav, auth, token, forget, status, main, log };
}

function renderNav(nav, active) {
  nav.textContent = "";
  for (const [tag, group] of navigationGroups()) {
    const box = element("span", "shell-nav-group");
    box.append(element("span", "shell-nav-tag", tag));
    for (const view of group) {
      const link = element("a", "shell-nav-link", view.title);
      link.href = `#${view.path}`;
      link.dataset.view = view.id;
      if (view.path === active) link.classList.add("on");
      box.append(link);
    }
    nav.append(box);
  }
}

function renderLog(log) {
  if (CALLS.length === 0) {
    log.innerHTML = '<div class="dim">Nothing yet.</div>';
    return;
  }
  log.innerHTML = CALLS.map(
    (c, i) => `
    <div>
      <div class="row">
        <span class="mono grow">${esc(c.method)} ${esc(c.path)}
          ${esc(
            new URLSearchParams(
              Object.entries(c.query).map(([k, v]) => [k, String(v)])
            ).toString()
          )}
          &rarr; ${
            c.status === 0 || c.status >= 400
              ? `<span class="bad">${c.status || esc(detailOf(c.response))}</span>`
              : c.status
          }
          <span class="dim">${c.ms} ms</span></span>
        <button class="tiny" data-i="${i}" data-as="curl">curl</button>
        <button class="tiny" data-i="${i}" data-as="python">python</button>
        <button class="tiny" data-i="${i}" data-as="fetch">fetch</button>
      </div>
      <div class="out"></div>
    </div>`
  ).join("");
}

// ----------------------------------------------------------------------------- mounting

/**
 * Mount the view the route names.
 *
 * A component may return an element or a promise of one, and anything it throws is CAUGHT
 * AND SHOWN. That is the shell doing the Fail Early rule on the views' behalf: a view is free
 * to let an error propagate, and what it must never do is render a page that looks like an
 * answer. One place catches, so there is one place to read.
 */
async function mount(main, route) {
  const view = viewForPath(route.path);
  if (!view) {
    main.textContent = "";
    const box = element("div", "shell-error");
    box.append(element("h1", null, "No such view"));
    box.append(
      element("p", null, `Nothing is registered at ${route.path}. This page has:`)
    );
    const list = element("ul");
    for (const registered of views()) {
      const item = element("li");
      const link = element("a", null, `${registered.title} — ${registered.path}`);
      link.href = `#${registered.path}`;
      item.append(link);
      list.append(item);
    }
    box.append(list);
    main.append(box);
    return;
  }

  main.textContent = "";
  main.append(element("p", "dim", "loading…"));
  try {
    const produced = await view.component({ client, params: route.params, navigate });
    if (!(produced instanceof Element)) {
      throw new TypeError(
        `view "${view.id}" returned ${typeof produced} rather than an element. A component is ` +
          "(context) -> element, or a promise of one."
      );
    }
    main.textContent = "";
    main.append(produced);
  } catch (error) {
    // Shown, not swallowed, and shown with the message the failure carried. A transport error
    // for a missing credential already names the credential; an HTTP error already carries
    // the server's `detail`. Rewriting either into "something went wrong" is how a page stops
    // being able to tell a reader what to do next.
    main.textContent = "";
    const box = element("div", "shell-error");
    box.append(element("h1", null, view.title));
    box.append(element("p", "bad", error.message));
    main.append(box);
  }
}

// ------------------------------------------------------------------------------- start

export function start(root) {
  const parts = buildChrome(root);

  const saidToken = () => {
    parts.status.textContent = window.sessionStorage.getItem(TOKEN_KEY)
      ? "connected with a token"
      : "a token is needed for everything except /health";
  };

  parts.auth.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const value = parts.token.value.trim();
    // An empty submit is not a request to clear the credential — "Forget" is. Refusing it is
    // the form not pretending a no-op was an action.
    if (!value) {
      parts.status.innerHTML = '<span class="bad">enter a token, or press Forget to clear it</span>';
      return;
    }
    window.sessionStorage.setItem(TOKEN_KEY, value);
    parts.token.value = "";
    saidToken();
    route();
  });

  parts.forget.addEventListener("click", () => {
    window.sessionStorage.removeItem(TOKEN_KEY);
    parts.status.textContent = "token forgotten";
    route();
  });

  // Copy-out. The event is a call plus its arguments, so it prints three ways from the same
  // table the request was built from — and the credential is NEVER in any of them.
  parts.log.addEventListener("click", (ev) => {
    const button = ev.target.closest("button[data-as]");
    if (!button) return;
    const event = CALLS[Number(button.dataset.i)];
    const printers = { curl: asCurl, python: asPython, fetch: asFetch };
    const options = { baseUrl: window.location.origin };
    const target = button.closest("div").parentElement.querySelector(".out");
    target.innerHTML = `<pre class="copy">${esc(printers[button.dataset.as](event, options))}</pre>`;
  });

  onCallLog(() => renderLog(parts.log));
  renderLog(parts.log);

  const route = () => {
    const where = currentRoute();
    renderNav(parts.nav, where.path);
    return mount(parts.main, where);
  };

  window.addEventListener("hashchange", route);
  saidToken();
  return route();
}

const root = document.getElementById("shell");
if (root) start(root);
