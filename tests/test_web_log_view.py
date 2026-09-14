"""The call-log view: every request the page made, and the two things it must not draw.

``docs/SPRINT_0_6_0.md`` Block F step 30, over step 23's IC-6 event stream. The step exists to
make step 23's second property — "a view that shows something the log has no call for computed
it client-side" — readable by a person rather than only checkable by a test. So the tests here
are about what a reader can see:

* every call the page made is a row, newest first, with its status and its cost;
* a row carries what went out, what came back, and the ``curl`` that would repeat it, so the
  claim that nothing happens in the browser which could not have happened over ``curl`` is one
  a reader can check on the page rather than take from the source;
* a large response is PREVIEWED and the page says how much it withheld, rather than either
  drawing a megabyte of JSON or silently cutting it;
* bytes, and a request that never completed, are not drawn as ``{}``;
* and no credential reaches the page. ``JMFTS_API_TOKEN`` cannot, because the transport puts
  it in a header and IC-6 carries no headers — asserted here by making calls with a credential
  really in play and scanning the rendered tree. The one response body in the whole contract
  package that IS a live credential is declared by the view and not drawn.

These need no database and no browser. Where ``node`` is on PATH they execute the bundle's ES
modules against a stubbed DOM and a stubbed ``fetch``, which is the arrangement
``tests/test_ts_client_codegen.py`` established and ``tests/test_web_views.py`` followed; this
is the third file in it. The DOM stub is this file's own rather than imported from
``test_web_views``: that one models what the capability table touches, this one has to model
``insertBefore``, ``hidden``, ``document.head`` and — the one that carries a property — whether
a node is still attached, which is how the live subscription knows to let go.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel

from jmfts_core.registry import REGISTRY
from jmfts_core.rest.main import app  # noqa: F401  — importing it is what fills REGISTRY

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "jmfts-web" / "jmfts_web" / "static"
VIEW = STATIC / "views" / "log.js"
MANIFEST = STATIC / "shell" / "manifest.js"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="no JavaScript runtime on PATH")

#: The credential the tab holds while every call below is made. It reaches the appliance in an
#: ``Authorization`` header — the harness asserts that it really did — and must appear nowhere
#: on the rendered page.
TOKEN = "SECRET-TOKEN"

#: What ``POST /access/principals/{id}/tokens`` answers with in the harness. A response field,
#: not a header, so nothing about the transport keeps it off the page; the view has to.
MINTED = "MINTED-CREDENTIAL"


# --------------------------------------------------------------------------- the node harness


#: Enough DOM to run this view and the shell that mounts it, and nothing beyond it.
#:
#: Two things here are more than bookkeeping. ``textContent = ""`` DETACHES the children it
#: drops, because that is what the shell does to the outgoing view and ``isConnected`` is what
#: the log's live subscription reads to decide it has been replaced. And ``hidden`` is a real
#: property rather than a class, because that is how the empty-state paragraph goes away.
DOM_STUB = textwrap.dedent("""
    export class Element {
      constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.childNodes = [];
        this.attributes = {};
        this.className = "";
        this.dataset = {};
        this.hidden = false;
        this._parent = null;
        this._attached = false;
        this._text = "";
      }
      _adopt(node) {
        if (!(node instanceof Element)) throw new TypeError("only elements go in the tree");
        node._parent = this;
        return node;
      }
      append(...nodes) {
        for (const node of nodes) this.childNodes.push(this._adopt(node));
      }
      insertBefore(node, reference) {
        const at = this.childNodes.indexOf(reference);
        if (at === -1) throw new Error("insertBefore: the reference node is not a child");
        this.childNodes.splice(at, 0, this._adopt(node));
        return node;
      }
      get firstChild() { return this.childNodes[0] ?? null; }
      // A tree the shell has thrown away answers false, which is the whole point of modelling
      // this: the view's subscription unsubscribes itself the first time it sees one.
      get isConnected() {
        if (this._attached) return true;
        return this._parent ? this._parent.isConnected : false;
      }
      setAttribute(name, value) { this.attributes[name] = String(value); }
      getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
      get classList() {
        return {
          add: (name) => { this.className = this.className ? `${this.className} ${name}` : name; },
        };
      }
      set textContent(value) {
        for (const child of this.childNodes) child._parent = null;
        this.childNodes = [];
        this._text = value === null || value === undefined ? "" : String(value);
      }
      get textContent() {
        return this._text + this.childNodes.map((n) => n.textContent).join("");
      }
      // The shell's own log panel writes markup in one go. Kept OUT of textContent so an
      // assertion about rendered text cannot accidentally match a tag.
      set innerHTML(value) {
        for (const child of this.childNodes) child._parent = null;
        this.childNodes = [];
        this._html = String(value);
      }
      get innerHTML() { return this._html ?? ""; }
      addEventListener() {}
      querySelector() { return null; }
      closest() { return null; }
    }

    export const document = {
      head: new Element("head"),
      createElement: (tag) => new Element(tag),
      getElementById: () => null,
    };

    /** The first element with this exact class, anywhere under `root`. */
    export function find(root, className) {
      if (root.className === className) return root;
      for (const child of root.childNodes) {
        const hit = find(child, className);
        if (hit) return hit;
      }
      return null;
    }

    /** The rendered tree as plain data, so the assertions can live in Python. */
    export function serialize(element) {
      return {
        tag: element.tagName.toLowerCase(),
        class: element.className || null,
        hidden: element.hidden === true,
        text: element.textContent,
        html: element.innerHTML || null,
        children: element.childNodes.map(serialize),
      };
    }

    export function install() {
      globalThis.Element = Element;
      globalThis.document = document;
      globalThis.sessionStorage = {
        _held: new Map(),
        getItem(key) { return this._held.has(key) ? this._held.get(key) : null; },
        setItem(key, value) { this._held.set(key, String(value)); },
        removeItem(key) { this._held.delete(key); },
      };
      globalThis.window = {
        location: { hash: "", origin: "http://appliance:8100" },
        sessionStorage: globalThis.sessionStorage,
        addEventListener() {},
      };
    }
    """)


def _node(tmp_path: Path, harness: str) -> object:
    """Run one harness module and parse the single JSON document it prints."""
    (tmp_path / "dom.mjs").write_text(DOM_STUB, encoding="utf-8")
    script = tmp_path / "harness.mjs"
    script.write_text(harness % {"static": STATIC.as_uri()}, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, cwd=tmp_path, timeout=60
    )
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout)


#: Six calls, chosen to cover every kind of thing a row has to draw: a plain JSON operation, a
#: response that is genuinely large, the one response that is a credential, a binary operation,
#: an appliance that said no, and a request that never went out at all.
#:
#: They are made through THE SHELL'S OWN CLIENT, not a second one, because the view reads the
#: shell's log and a client of this harness's own would not appear in it. The probe view below
#: is how the harness gets a handle on it: a view receives the client in its context, which is
#: the interface IC-7 gives it, so this is the client every real view calls.
MAIN_HARNESS = textwrap.dedent("""
    import { install, serialize, find, Element } from "./dom.mjs";
    install();

    const { registerView } = await import("%(static)s/shell/registry.js");
    let captured = null;
    registerView({
      id: "probe",
      title: "Probe",
      path: "/probe",
      component: (context) => { captured = context.client; return new Element("div"); },
      tags: ["harness"],
    });

    const replies = [];
    const sent = [];
    globalThis.fetch = async (url, request) => {
      sent.push({ url, authorization: (request.headers ?? {}).Authorization ?? null });
      const next = replies.shift();
      if (next === undefined) throw new Error(`the harness queued no reply for ${url}`);
      if (next instanceof Error) throw next;
      return next;
    };

    const shell = await import("%(static)s/shell/shell.js");
    window.sessionStorage.setItem("jmfts.token", "SECRET-TOKEN");

    const root = new Element("div");
    root._attached = true;
    window.location.hash = "#/probe";
    await shell.start(root);
    if (!captured) throw new Error("the probe view was never handed the shell's client");

    const json = (value, status = 200) => new Response(JSON.stringify(value), {
      status, headers: { "content-type": "application/json" },
    });
    const refused = async (promise) => {
      try { await promise; } catch (error) { return error.message; }
      throw new Error("that call was supposed to fail and did not");
    };

    // 1 — a plain JSON operation, with a body and a query parameter
    replies.push(json({ results: [], total: 0, latency_ms: 4.5 }));
    await captured.hybrid_search({ request: { query: "late interaction", limit: 2 }, rerank: true });

    // 2 — a response that is genuinely large: DocumentResponse.embed is 768 floats
    replies.push(json({
      id: 7, title: "A node", embed: Array.from({ length: 768 }, (_, i) => i / 1000),
    }));
    await captured.get_document({ document_id: 7, include_embed: true });

    // 3 — the one response in the contract package that is a live credential
    replies.push(json({ token_id: 1, principal_id: 3, token: "MINTED-CREDENTIAL" }, 201));
    await captured.mint_token({ principal_id: 3, request: { name: "demo" } });

    // 4 — bytes
    replies.push(new Response(new Uint8Array([137, 80, 78, 71]), {
      status: 200, headers: { "content-type": "image/png" },
    }));
    await captured.get_document_blob({ document_id: 9 });

    // 5 — the appliance answered, and it answered no
    replies.push(json({ detail: "Document 404 not found" }, 404));
    const said = await refused(captured.get_document({ document_id: 404 }));

    // 6 — the request never went out
    replies.push(new TypeError("fetch failed"));
    const threw = await refused(captured.get_document({ document_id: 5 }));

    window.location.hash = "#/calls";
    await shell.start(root);
    const mounted = serialize(root);
    const rows = find(root, "log-rows");

    // One more call while the page is open: it appears without a reload, at the top.
    replies.push(json({ results: [], total: 0, latency_ms: 1.5 }));
    await captured.hybrid_search({ request: { query: "again" } });
    const live = serialize(root);

    // And then the shell mounts another view over it. The orphaned tree must stop growing.
    window.location.hash = "#/probe";
    await shell.start(root);
    replies.push(json({ results: [], total: 0, latency_ms: 1.5 }));
    await captured.hybrid_search({ request: { query: "after" } });

    console.log(JSON.stringify({
      registered: (await import("%(static)s/shell/registry.js")).views().map((v) => v.id),
      sent,
      said,
      threw,
      mounted,
      live,
      orphan: { connected: rows.isConnected, rows: rows.childNodes.length },
    }));
    """)


EMPTY_HARNESS = textwrap.dedent("""
    import { install, serialize, Element } from "./dom.mjs";
    install();
    const shell = await import("%(static)s/shell/shell.js");
    globalThis.fetch = async () => { throw new Error("nothing should be called"); };

    const root = new Element("div");
    root._attached = true;
    window.location.hash = "#/calls";
    await shell.start(root);
    console.log(JSON.stringify(serialize(root)));
    """)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """One node run for the whole file: six calls, mounted, appended to, then orphaned."""
    if NODE is None:
        pytest.skip("no JavaScript runtime on PATH")
    return _node(tmp_path_factory.mktemp("log-view"), MAIN_HARNESS)


# ------------------------------------------------------------------------------- the walkers


def _find(node: dict, class_name: str) -> list[dict]:
    found = [node] if node["class"] == class_name else []
    for child in node["children"]:
        found.extend(_find(child, class_name))
    return found


def _calls(tree: dict) -> list[dict]:
    return _find(tree, "log-call")


def _summary(call: dict) -> dict:
    parts = call["children"][0]["children"]
    return {
        "line": parts[0]["text"],
        "status": parts[1]["text"],
        "failed": parts[1]["class"] == "bad log-status",
        "ms": parts[2]["text"],
        "op_id": parts[3]["text"],
    }


def _blocks(call: dict) -> dict[str, dict]:
    """The labelled blocks of one row, as ``{label: {"pre": text, "note": text}}``."""
    out: dict[str, dict] = {}
    for box in _find(call, "log-block"):
        label = box["children"][0]["text"]
        pre = next((c["text"] for c in box["children"] if c["tag"] == "pre"), None)
        note = next((c["text"] for c in box["children"] if c["class"] == "dim note"), None)
        out[label] = {"pre": pre, "note": note}
    return out


def _printed(call: dict) -> dict[str, str]:
    """The three renderings at the foot of a row, as ``{"curl": ..., ...}``."""
    box = _find(call, "log-copy")[0]
    rest = box["children"][1:]
    return {rest[i]["text"]: rest[i + 1]["text"] for i in range(0, len(rest), 2)}


# ------------------------------------------------------------------------- the view exists


def test_the_log_is_a_view_the_manifest_reaches(rendered):
    """IC-7, from the other side: a file in ``views/`` plus one appended import line."""
    assert VIEW.is_file()
    manifest = MANIFEST.read_text(encoding="utf-8")
    assert 'import "../views/log.js";' in manifest
    assert manifest.rstrip().endswith(
        'import "../views/log.js";'
    ), "the manifest is append-only AND appended at the END; see its header for why"
    assert "calls" in rendered["registered"]


def test_the_shell_really_carried_the_credential(rendered):
    """The premise every credential assertion below rests on.

    A page that never authenticated would pass a "the token is not on the page" test by doing
    nothing at all. So: the tab held a token, and the requests that were made carried it.
    """
    carried = [row for row in rendered["sent"] if row["authorization"] == f"Bearer {TOKEN}"]
    assert len(carried) == len(rendered["sent"]) > 5, rendered["sent"]


# ------------------------------------------------------------------------------- the rows


def test_every_call_the_page_made_is_a_row_newest_first(rendered):
    """Six calls, six rows, in reverse order, each saying what it did and what it cost."""
    rows = [_summary(call) for call in _calls(rendered["mounted"])]
    assert [row["op_id"] for row in rows] == [
        "DocumentService.get_document",  # 6, the request that never went out
        "DocumentService.get_document",  # 5, the 404
        "DocumentService.get_document_blob",
        "AccessService.mint_token",
        "DocumentService.get_document",
        "SearchService.hybrid_search",
    ]

    # The line reads the way the request did: verb, path as it went out, query string.
    assert rows[-1]["line"] == "POST /search/hybrid?rerank=true"
    assert rows[-2]["line"] == "GET /documents/7?include_embed=true"
    assert rows[2]["line"] == "GET /documents/9/blob"

    # Status, and the two ways a call fails.
    assert [row["status"] for row in rows] == ["no response", "404", "200", "201", "200", "200"]
    assert [row["failed"] for row in rows] == [True, True, False, False, False, False]
    for row in rows:
        assert re.fullmatch(r"\d+ ms", row["ms"]), row

    # A failed row says WHY on the row, without being opened: a page whose failures all read
    # "no response" until you click each one hides the thing it is for.
    bad = [box["text"] for box in _find(_calls(rendered["mounted"])[1], "bad")]
    assert "Document 404 not found" in bad
    assert rendered["said"].endswith("HTTP 404: Document 404 not found")


def test_a_row_carries_the_request_the_response_and_the_curl_that_would_repeat_it(rendered):
    """The row is the whole argument of the step: this call could have been a ``curl``."""
    search = _calls(rendered["mounted"])[-1]
    blocks = _blocks(search)
    assert set(blocks) == {"Arguments", "Request body", "Response"}
    assert json.loads(blocks["Arguments"]["pre"]) == {"rerank": True}
    assert json.loads(blocks["Request body"]["pre"]) == {"query": "late interaction", "limit": 2}
    assert json.loads(blocks["Response"]["pre"]) == {"results": [], "total": 0, "latency_ms": 4.5}
    assert blocks["Response"]["note"] is None, "nothing was withheld from this one"

    printed = _printed(search)
    assert set(printed) == {"curl", "python", "fetch"}
    assert "curl -sS -X POST" in printed["curl"]
    assert "http://appliance:8100/search/hybrid?rerank=true" in printed["curl"]
    assert "late interaction" in printed["curl"]
    assert "client.hybrid_search(" in printed["python"]
    assert "await fetch(" in printed["fetch"]

    # A path parameter is substituted and percent-encoded in the summary line; the arguments
    # block is what the caller passed. Both are true and a reader wants both.
    document = _calls(rendered["mounted"])[-2]
    assert json.loads(_blocks(document)["Arguments"]["pre"]) == {
        "document_id": 7,
        "include_embed": True,
    }
    assert _blocks(document)["Request body"]["pre"] is None, "a GET has no body, and says none"


def test_a_large_response_is_previewed_and_the_page_says_how_much_it_withheld(rendered):
    """``DocumentResponse.embed`` is 768 floats, and the whole log is on one page.

    The cut is the DRAWING and nothing else: the shell still holds the event whole, ``replay()``
    still re-sends it, and the printed ``curl`` beside it is the entire request. What must never
    happen is a page that shows 4000 characters of JSON and lets a reader believe that is what
    came back.
    """
    response = _blocks(_calls(rendered["mounted"])[-2])["Response"]
    assert len(response["pre"]) == 4000, "PREVIEW_CHARACTERS is what a row draws"
    assert response["pre"].startswith('{\n  "id": 7,')
    match = re.search(r"showing the first (\d+) of (\d+) characters", response["note"])
    assert match, response["note"]
    assert int(match.group(1)) == 4000
    assert int(match.group(2)) > 4000
    assert "the event itself is whole" in response["note"]


def test_bytes_and_a_request_that_never_completed_are_not_drawn_as_empty_json(rendered):
    """``JSON.stringify`` over either gives ``{}``, which is a page inventing an answer.

    These are the transport's own documented response kinds — a binary operation resolves to a
    ``Blob``, and a request that never went out carries the ``Error`` itself with ``status`` 0
    (``client/transport.js``, the comment at its ``catch``). A log that rendered both as an
    empty object would be the same defect the capability table had: a thing that is absent
    drawn identically to a thing that is empty.
    """
    blob = _blocks(_calls(rendered["mounted"])[2])["Response"]
    assert blob["pre"] == "4 bytes of image/png"
    assert "bytes, not text" in blob["note"]

    never = _blocks(_calls(rendered["mounted"])[0])["Response"]
    assert never["pre"].endswith("fetch failed")
    assert "did not complete" in never["note"]
    assert _summary(_calls(rendered["mounted"])[0])["status"] == "no response"
    assert rendered["threw"] == "fetch failed"


# ------------------------------------------------------------------------- the credentials


def test_the_api_token_never_reaches_the_page(rendered):
    """IC-6 carries no headers, and this is the check that says so out loud.

    ``transport.js`` puts the credential in an ``Authorization`` header and the nine fields of a
    call event are ``{op_id, method, path, path_params, query, body, status, response, ms}`` —
    so there is no field for it to arrive in. That is a property of the contract rather than of
    this view's care, which is exactly why it is worth pinning from the page: a tenth field
    added to the event, or a view that passed a token as an argument, would land here.

    Scanned over the WHOLE rendered tree, the shell's own panel included, and over every text
    the three printers produced.
    """
    for label, tree in (("mounted", rendered["mounted"]), ("live", rendered["live"])):
        assert TOKEN not in json.dumps(tree), f"the credential is on the page ({label})"
    for call in _calls(rendered["mounted"]):
        for how, text in _printed(call).items():
            assert TOKEN not in text, f"{how} printed the credential"
    assert "$JMFTS_API_TOKEN" in _printed(_calls(rendered["mounted"])[-1])["curl"]


def test_a_minted_token_is_not_drawn_and_the_page_says_it_is_not(rendered):
    """The one response body in the contract package that IS a live credential.

    ``TokenMintResponse.token`` — "The raw bearer token. Shown once; store it now." A log is a
    surface people leave open and share a screen over; a credential that scrolls past in it
    outlives the moment anybody meant to look at it. So the view declines to draw that one
    value and says where it went, which is the opposite of hiding it.
    """
    mint = _calls(rendered["mounted"])[3]
    assert _summary(mint)["op_id"] == "AccessService.mint_token"
    response = _blocks(mint)["Response"]
    assert MINTED not in json.dumps(rendered["mounted"]), "a minted credential is on the page"
    assert "not drawn by the call log" in response["pre"]
    assert '"token_id": 1' in response["pre"], "everything else in the response is drawn"
    assert "token is a live credential and this page does not draw it" in response["note"]
    assert "the view that made the call received it" in response["note"]


def test_the_log_view_cannot_reach_a_credential():
    """Structural, and the precedent is ``test_copy_out_cannot_print_a_credential``.

    A module that can read the value is a module that can print it. This one never asks.
    """
    source = VIEW.read_text(encoding="utf-8")
    for forbidden in ("sessionStorage", "localStorage", "getCredential", "credentialFromSession"):
        # Named in prose is fine; a property access is not.
        assert not re.search(rf"(?<![.\w]){forbidden}\s*[.(\[]", source), f"log.js uses {forbidden}"


def _credential_fields() -> dict[type[BaseModel], list[str]]:
    """Every contract field whose own description says it is a raw credential.

    Found by reading the contracts rather than by listing names here, so an endpoint that grows
    a second one is discovered rather than remembered.
    """
    found: dict[type[BaseModel], list[str]] = {}
    for spec in REGISTRY:
        model = spec.response_model
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            continue
        names = [
            name
            for name, info in model.model_fields.items()
            if re.search(r"raw bearer token|shown once", info.description or "", re.IGNORECASE)
        ]
        if names:
            found[model] = names
    return found


def test_the_log_declares_every_response_field_that_is_a_credential():
    """The view's table, resolved against the live registry and the contract models.

    ``views/log.js`` names one operation and one field. This is what makes that a declaration
    rather than a guess: the same fact read out of ``REGISTRY`` and the Pydantic models, so an
    operation that starts answering with a credential fails here — on the day it is written,
    not on the day somebody screenshots the log.
    """
    credential_models = _credential_fields()
    assert (
        credential_models
    ), "no contract describes a raw credential any more; has the wording moved?"

    expected: dict[str, list[str]] = {}
    for spec in REGISTRY:
        if spec.response_model in credential_models:
            expected[spec.name] = sorted(credential_models[spec.response_model])

    source = VIEW.read_text(encoding="utf-8")
    table = source[source.index("export const REDACTED_RESPONSE_FIELDS") :]
    table = table[: table.index("});")]
    declared = {
        op_id: sorted(re.findall(r'"([a-z_]+)"', fields))
        for op_id, fields in re.findall(r'"([\w.]+)": Object\.freeze\(\[([^\]]*)\]\)', table)
    }
    assert declared == expected, (
        "views/log.js draws a response field that the contracts call a raw credential, or "
        "declares one that is no longer there"
    )


# ------------------------------------------------------------------------------ live, and not


def test_a_call_made_while_the_page_is_open_appears_at_the_top(rendered):
    """The log is the client's stream, not a snapshot taken at mount."""
    before = _calls(rendered["mounted"])
    after = _calls(rendered["live"])
    assert len(after) == len(before) + 1
    assert _summary(after[0])["line"] == "POST /search/hybrid"
    assert _summary(after[1])["op_id"] == _summary(before[0])["op_id"]

    tally = _find(rendered["live"], "dim log-tally")[0]["text"]
    assert tally.startswith("7 calls, 2 of them failed, ")
    assert tally.endswith(" ms spent waiting.")


def test_a_log_the_shell_has_replaced_stops_listening(rendered):
    """There is no unmount hook, so the subscription has to notice for itself.

    The shell mounts; it does not dispose. A listener that kept appending to a tree nobody can
    see would be a leak that grows for as long as the tab is open, and it would double every
    time a reader came back to this page. The subscription reads ``isConnected`` and lets go.
    """
    assert rendered["orphan"]["connected"] is False
    assert rendered["orphan"]["rows"] == len(
        _calls(rendered["live"])
    ), "a call made after the shell replaced this view was drawn into the orphaned tree"


@needs_node
def test_the_empty_log_says_why_it_can_be_empty(tmp_path):
    """Landing here first is not an error and not an empty box with no explanation.

    The shell makes no call of its own — ``test_web_views`` asserts that of the shell — so a
    reader who opens this page first sees a page with genuinely nothing on it. The one thing it
    must not do is look broken.
    """
    tree = _node(tmp_path, EMPTY_HARNESS)
    assert _calls(tree) == []
    assert _find(tree, "dim log-tally")[0]["text"] == "No calls yet."
    empty = _find(tree, "dim log-empty")[0]
    assert empty["hidden"] is False
    assert "This page makes no call of its own" in empty["text"]
