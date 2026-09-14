"""The shell, the view registry, and the two interfaces the views plug into.

``docs/SPRINT_0_6_0.md`` Block F steps 24 and 25 — IC-7, IC-8 and IC-9. Three contracts, and
each is pinned here by the property Part 5.1's table names for it rather than by prose:

* **IC-7** — "two views added in two worktrees merge without conflict". Performed, not
  asserted: two branches of a scratch repository each append a view to the real manifest and
  the merge is run. The same merge is run again without the ``.gitattributes`` line, where it
  must conflict, because a clean merge that nothing forced is a clean merge by luck.
* **IC-8** — "every value in ``RENDERERS`` has exactly one renderer". The tuple is read out of
  ``jmfts_core.models.usetype_presentation``; restating it in JavaScript would make this a
  comparison of two copies of a list rather than a check of one.
* **IC-9** — "one anchor renders identically on all three surfaces". Three surfaces of three
  kinds, one anchor, one box, asserted equal.

And the defect that opened this lane: the capability table read three field names that
``CapabilitiesResponse`` does not carry, and all three rendered as the word "none" because
``pills(items)`` could not tell a field that is absent from a field that is empty. The
fixture below is BUILT FROM THE CONTRACT — a real ``CapabilitiesResponse``, dumped to JSON —
so a field renamed in ``jmfts-client`` fails here rather than on somebody's screen.

These need no database and no browser. Where ``node`` is on PATH they execute the bundle's ES
modules against a stubbed DOM, which is the same arrangement
``tests/test_ts_client_codegen.py`` uses to execute the generated client against a stubbed
``fetch``; that file is the only JavaScript harness this repository has and this is the
second.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from jmfts_client.contracts.meta import (
    CapabilitiesResponse,
    CorpusFacts,
    EmbeddingCapability,
    ExtraStatus,
    IngestCapability,
    LlmCapability,
    RetrievalCapability,
)
from jmfts_core.models.usetype_presentation import RENDERERS

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "jmfts-web" / "jmfts_web" / "static"
MANIFEST = STATIC / "shell" / "manifest.js"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="no JavaScript runtime on PATH")

#: The five fields of a view registration, verbatim from IC-7 in Part 5.1's table. Written out
#: here rather than imported for the reason ``IC6_CALL_EVENT`` gives in the codegen test: a
#: contract pinned by reading the implementation is pinned to whatever the implementation says.
IC7_VIEW_FIELDS = ("id", "title", "path", "component", "tags")


# --------------------------------------------------------------------------- the node harness


#: Enough DOM to run the bundle's rendering, and nothing beyond it.
#:
#: A real DOM would be a dependency, and the bundle has none by design. What the modules under
#: test actually touch is four methods and three properties; anything else throws here rather
#: than quietly returning undefined, so a renderer that starts using an API this stub does not
#: model fails loudly instead of being tested against a shrug.
DOM_STUB = textwrap.dedent("""
    export class Element {
      constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.childNodes = [];
        this.attributes = {};
        this.className = "";
        this.dataset = {};
        this._text = "";
      }
      append(...nodes) {
        for (const node of nodes) {
          if (!(node instanceof Element)) throw new TypeError("append() takes elements");
          this.childNodes.push(node);
        }
      }
      setAttribute(name, value) { this.attributes[name] = String(value); }
      getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
      get classList() {
        return {
          add: (name) => { this.className = this.className ? `${this.className} ${name}` : name; },
        };
      }
      set textContent(value) {
        this.childNodes = [];
        this._text = value === null || value === undefined ? "" : String(value);
      }
      get textContent() {
        return this._text + this.childNodes.map((n) => n.textContent).join("");
      }
      // The call-log panel writes markup in one go. Kept OUT of textContent so an assertion
      // about rendered text cannot accidentally match a tag; `serialize` reports it separately.
      set innerHTML(value) { this.childNodes = []; this._html = String(value); }
      get innerHTML() { return this._html ?? ""; }
      addEventListener() {}
      querySelector() { return null; }
      closest() { return null; }
    }

    export const document = {
      createElement: (tag) => new Element(tag),
      getElementById: () => null,
    };

    /** The rendered tree as plain data, so the assertions can live in Python. */
    export function serialize(element) {
      return {
        tag: element.tagName.toLowerCase(),
        class: element.className || null,
        title: element.getAttribute("title"),
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


# ------------------------------------------------------------------ IC-7: a view registration


def test_the_view_registration_is_exactly_the_five_fields_of_ic7():
    """IC-7 fixes the shape, and the registry names it in one place."""
    source = (STATIC / "shell" / "registry.js").read_text(encoding="utf-8")
    match = re.search(r"export const VIEW_FIELDS = Object\.freeze\(\[([^\]]*)\]\)", source)
    assert match, "registry.js no longer names VIEW_FIELDS"
    declared = tuple(re.findall(r'"([a-z_]+)"', match.group(1)))
    assert declared == IC7_VIEW_FIELDS


def test_the_manifest_holds_nothing_but_import_lines():
    """The property that makes a union merge of this file safe.

    ``merge=union`` keeps both sides of a hunk. That is correct for a file of independent
    append-only lines and wrong for anything else — a declaration two branches both edit would
    come back doubled. So the rule is not "be careful": it is that this file has no content a
    branch could edit in the first place.
    """
    offenders = [
        f"{number}: {line}"
        for number, line in enumerate(MANIFEST.read_text(encoding="utf-8").splitlines(), 1)
        if line.strip() and not line.startswith("//") and not re.fullmatch(r'import ".+";', line)
    ]
    assert not offenders, (
        "shell/manifest.js carries something that is not a comment or an import: "
        f"{offenders}. Anything else belongs in shell/shell.js; see that file's header."
    )


def test_git_gives_the_manifest_the_union_merge_driver():
    """Asked of git itself, in this repository, rather than by reading ``.gitattributes``.

    A pattern that does not match the path is a pattern that does nothing, and reading the
    file back would not notice. ``check-attr`` resolves it the way a merge would.
    """
    done = subprocess.run(
        ["git", "check-attr", "merge", "--", str(MANIFEST.relative_to(REPO))],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip().endswith(": merge: union"), done.stdout


def _scratch_repo(work: Path, *, with_attributes: bool) -> Path:
    """A repository holding the real manifest, ready to be branched."""

    def git(*args, check=True):
        return subprocess.run(["git", *args], cwd=work, capture_output=True, text=True, check=check)

    work.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "trunk", ".")
    git("config", "user.email", "lane@example.invalid")
    git("config", "user.name", "lane")
    (work / "manifest.js").write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    if with_attributes:
        # The real rule, with only the path rewritten to where the file sits here.
        line = next(
            row
            for row in (REPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
            if row.strip() and not row.startswith("#")
        )
        assert line.endswith("merge=union"), line
        (work / ".gitattributes").write_text("manifest.js merge=union\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "trunk")
    return work


def _add_a_view(work: Path, branch: str, name: str) -> None:
    """What an F3 worktree does: append one import line, on its own branch."""

    def git(*args):
        return subprocess.run(["git", *args], cwd=work, capture_output=True, text=True, check=True)

    git("checkout", "-q", "-B", branch, "trunk")
    manifest = work / "manifest.js"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + f'import "../views/{name}.js";\n', encoding="utf-8"
    )
    git("commit", "-qam", f"add the {name} view")


def _merge(work: Path, into: str, other: str) -> subprocess.CompletedProcess:
    subprocess.run(["git", "checkout", "-q", into], cwd=work, capture_output=True, check=True)
    return subprocess.run(
        ["git", "merge", "--no-edit", other], cwd=work, capture_output=True, text=True
    )


def test_two_views_added_in_two_worktrees_merge_without_conflict(tmp_path):
    """IC-7's pin, performed rather than argued.

    This is the whole reason step 24 is a sequential phase's deliverable. Part 5.3 lists "the
    client router / every view step" as a collision "removed as a conflict by IC-7", and M3
    forks five worktrees against that promise. Here it is kept: two branches that never saw
    each other each add a view, and the merge is clean with both views present and in order.
    """
    work = _scratch_repo(tmp_path / "with", with_attributes=True)
    _add_a_view(work, "wt-view-search", "search")
    _add_a_view(work, "wt-view-upload", "upload")

    done = _merge(work, "wt-view-search", "wt-view-upload")
    assert done.returncode == 0, f"the manifest conflicted:\n{done.stdout}{done.stderr}"

    merged = (work / "manifest.js").read_text(encoding="utf-8")
    assert "<<<<<<<" not in merged
    imports = re.findall(r'^import "(.+)";$', merged, re.M)
    assert imports == [
        "../views/capabilities.js",
        "../views/search.js",
        "../views/upload.js",
    ], imports


def test_without_the_union_driver_those_same_two_views_conflict(tmp_path):
    """What stops the test above being a clean merge by luck.

    Two appends at one position are an ordinary conflict, and the only reason the manifest
    survives them is the ``.gitattributes`` line. Remove it and the same two commits collide —
    so the attribute is load-bearing rather than decoration, and deleting it breaks a test
    instead of breaking M3.
    """
    work = _scratch_repo(tmp_path / "without", with_attributes=False)
    _add_a_view(work, "wt-view-search", "search")
    _add_a_view(work, "wt-view-upload", "upload")

    done = _merge(work, "wt-view-search", "wt-view-upload")
    assert done.returncode != 0, (
        "two appends to one file merged cleanly with no merge driver, so "
        "test_two_views_added_in_two_worktrees_merge_without_conflict proves nothing."
    )
    assert "<<<<<<<" in (work / "manifest.js").read_text(encoding="utf-8")


REGISTRY_HARNESS = textwrap.dedent("""
    import { install } from "./dom.mjs";
    install();
    const { registerView, views, viewForPath, navigationGroups, ViewRegistrationError } =
      await import("%(static)s/shell/registry.js");

    const refusals = {};
    const refuse = (label, registration) => {
      try {
        registerView(registration);
        refusals[label] = null;
      } catch (error) {
        refusals[label] = error instanceof ViewRegistrationError ? error.message : String(error);
      }
    };

    const ok = registerView({
      id: "alpha", title: "Alpha", path: "/alpha", component: () => null, tags: ["group-a"],
    });
    registerView({
      id: "beta", title: "Beta", path: "/beta", component: () => null, tags: ["group-a", "x"],
    });
    registerView({
      id: "gamma", title: "Gamma", path: "/gamma", component: () => null, tags: ["group-b"],
    });

    refuse("unknown field", {
      id: "d", title: "D", path: "/d", component: () => null, tags: ["g"], icon: "x",
    });
    refuse("missing field", { id: "e", title: "E", path: "/e", component: () => null });
    refuse("bad id", {
      id: "Not An Id", title: "F", path: "/f", component: () => null, tags: ["g"],
    });
    refuse("bad path", {
      id: "g", title: "G", path: "g", component: () => null, tags: ["g"],
    });
    refuse("no tags", { id: "h", title: "H", path: "/h", component: () => null, tags: [] });
    refuse("component not a function", {
      id: "i", title: "I", path: "/i", component: {}, tags: ["g"],
    });
    refuse("duplicate id", {
      id: "alpha", title: "Again", path: "/again", component: () => null, tags: ["g"],
    });
    refuse("duplicate path", {
      id: "delta", title: "Delta", path: "/alpha", component: () => null, tags: ["g"],
    });

    console.log(JSON.stringify({
      frozen: Object.isFrozen(ok),
      order: views().map((v) => v.id),
      by_path: viewForPath("/beta")?.id ?? null,
      unrouted: viewForPath("/nowhere"),
      groups: navigationGroups().map(([tag, group]) => [tag, group.map((v) => v.id)]),
      refusals,
    }));
    """)


@needs_node
def test_the_registry_refuses_a_registration_it_cannot_honour(tmp_path):
    """Every check in ``registerView`` refuses rather than repairing.

    A registry appended to by branches that never saw each other cannot afford to normalise: a
    defaulted tag or a suffixed duplicate id produces a navigation nobody wrote, and the author
    of the second view is the one who finds out.
    """
    result = _node(tmp_path, REGISTRY_HARNESS)

    assert result["frozen"] is True
    assert result["order"] == ["alpha", "beta", "gamma"]
    assert result["by_path"] == "beta"
    assert result["unrouted"] is None, "an unrouted path must be null, never a guess"
    assert result["groups"] == [["group-a", ["alpha", "beta"]], ["group-b", ["gamma"]]]

    for label, message in result["refusals"].items():
        assert message, f"{label} was accepted; every one of these must be refused"
    assert "icon" in result["refusals"]["unknown field"]
    assert "alpha" in result["refusals"]["duplicate id"]
    assert "/alpha" in result["refusals"]["duplicate path"]


def test_the_capability_landing_is_a_registered_view_and_not_the_page():
    """Step 24's move: ``index.html`` is the shell, and the old page is a view like any other.

    Asserted structurally rather than by reading the HTML for absences. What makes the five F3
    worktrees able to work in parallel is that a view is a FILE plus one appended line — so the
    check is that the document has no view logic in it at all, not that it has less than it did.
    """
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    body = page[page.index("<body>") :]
    assert "<script" in body and 'src="./shell/shell.js"' in body
    assert re.search(r"<script[^>]*>\s*[^<\s]", body) is None, (
        "index.html carries inline script again. Everything it would do belongs to the shell "
        "or to a view; this file is a mount point."
    )
    assert (STATIC / "views" / "capabilities.js").is_file()
    assert 'import "../views/capabilities.js";' in MANIFEST.read_text(encoding="utf-8")


SHELL_HARNESS = textwrap.dedent("""
    import { install } from "./dom.mjs";
    install();
    const shell = await import("%(static)s/shell/shell.js");
    const { views } = await import("%(static)s/shell/registry.js");

    const out = { routes: {}, navigated: [], registered: views().map((v) => v.id) };
    out.routes.empty = shell.currentRoute("");
    out.routes.plain = shell.currentRoute("#/capabilities");
    out.routes.withParams = shell.currentRoute("#/document?id=42&highlight=on");
    out.routes.unknown = shell.currentRoute("#/nowhere");

    for (const [path, params] of [["/capabilities", {}], ["/document", { id: 42 }]]) {
      shell.navigate(path, params);
      out.navigated.push(window.location.hash);
    }
    out.roundTrip = shell.currentRoute(window.location.hash);
    out.log = shell.callLog();
    console.log(JSON.stringify(out));
    """)


@needs_node
def test_the_shell_routes_on_the_fragment_and_carries_a_views_arguments(tmp_path):
    """Routing, and why it is on the fragment rather than on the path.

    ``StaticFiles(html=True)`` mounted at ``/app`` (``jmfts_core/rest/main.py:210``) answers
    ``/app/`` from ``index.html`` and answers ``/app/search`` with 404, because no such file
    exists. Real paths need a catch-all route; ``jmfts_web.index_path()`` is written for exactly
    that route and nothing mounts one. So ``#/search`` is the form that survives a reload, and a
    view's arguments ride in the fragment's own query string — which keeps IC-7's ``path`` a
    literal string rather than growing a pattern grammar this step was not asked for.

    An empty fragment resolves to the first registered view rather than to an error: arriving at
    ``/app`` without saying where you were going is ambivalence, not a failure, and the manifest
    is append-only so "first" does not move.
    """
    result = _node(tmp_path, SHELL_HARNESS)

    assert result["registered"] == ["capabilities"]
    assert result["routes"]["empty"] == {"path": "/capabilities", "params": {}}
    assert result["routes"]["plain"] == {"path": "/capabilities", "params": {}}
    assert result["routes"]["withParams"] == {
        "path": "/document",
        "params": {"id": "42", "highlight": "on"},
    }
    # An unrouted fragment is carried through as itself. The shell renders "no such view" and
    # lists what exists; it does not redirect, because a redirect hides the bad link.
    assert result["routes"]["unknown"] == {"path": "/nowhere", "params": {}}

    assert result["navigated"] == ["#/capabilities", "#/document?id=42"]
    assert result["roundTrip"] == {"path": "/document", "params": {"id": "42"}}
    assert result["log"] == [], "the shell made no call of its own; the views make the calls"


START_HARNESS = textwrap.dedent("""
    import { install, serialize, Element } from "./dom.mjs";
    install();
    const shell = await import("%(static)s/shell/shell.js");

    // No fetch reaches anywhere: the default base URL is "" and "/health" is not an absolute
    // URL, so the request never goes out and the view's first call fails. That is the case
    // worth smoke-testing — the frame has to survive a view that throws.
    const root = new Element("div");
    window.location.hash = "#/capabilities";
    await shell.start(root);
    console.log(JSON.stringify(serialize(root)));
    """)


def _find(node: dict, className: str) -> list[dict]:
    found = [node] if node["class"] == className else []
    for child in node["children"]:
        found.extend(_find(child, className))
    return found


@needs_node
def test_the_shell_draws_its_frame_and_shows_a_view_that_fails(tmp_path):
    """The frame, end to end, with the one thing most likely to break it.

    Nothing else in this file runs ``start()``, and a shell that throws while building its own
    chrome would take every view down with it. So this builds the whole frame and then mounts a
    view whose first call cannot succeed.

    What it must NOT do is render an empty page. The shell catches what a component throws and
    shows the message, which is the Fail Early rule done once on every view's behalf: a view is
    free to let an error propagate, and what it may never do is render something that looks
    like an answer.
    """
    tree = _node(tmp_path, START_HARNESS)

    assert [c["tag"] for c in tree["children"]] == ["header", "form", "main", "section"]

    # Navigation built from the registry, with the current route marked.
    links = _find(tree, "shell-nav-link on")
    assert [link["text"] for link in links] == ["This appliance"]
    assert _find(tree, "shell-nav-tag")[0]["text"] == "appliance"

    # Token entry, and the status line saying what a reader has to do next.
    form = tree["children"][1]
    assert [c["tag"] for c in form["children"]] == ["input", "button", "button", "span"]
    assert "a token is needed" in _find(tree, "dim shell-status")[0]["text"]

    # The view failed and the page says so, with the message the failure carried.
    errors = _find(tree, "shell-error")
    assert len(errors) == 1, "the view's failure must reach the page"
    assert errors[0]["children"][0]["text"] == "This appliance"
    message = errors[0]["children"][1]
    assert message["class"] == "bad"
    assert "/health" in message["text"], message["text"]

    # And the call that failed is IN the log, which is the property step 23 built it for: a
    # request that never went out is still a request this page made, and IC-6 records it with
    # status 0 rather than dropping it. A log that showed only successes would be the same
    # class of omission as a table rendering a missing field as "none".
    log = _find(tree, "shell-log")[0]["html"]
    assert "Nothing yet." not in log
    assert "GET /health" in log


# ---------------------------------------------------- the defect: reading a field that is there


def _capabilities_fixture() -> CapabilitiesResponse:
    """A real ``CapabilitiesResponse``, so the fixture cannot drift from the contract.

    Hand-typing a dict here would reproduce the defect under test in the test itself: the page
    read three field names that do not exist, and a hand-typed fixture could have carried the
    same three. Every value below is placed by Pydantic into the model the appliance returns.
    """
    return CapabilitiesResponse(
        version="0.5.1",
        extras=[
            ExtraStatus(
                name="embed", installed=True, provides="produce embeddings", install="pip install"
            ),
            ExtraStatus(
                name="office", installed=False, provides="open .docx", install="pip install"
            ),
            ExtraStatus(name="rdf", installed=True, provides="Turtle", install="pip install"),
            ExtraStatus(name="sketch", installed=True, provides="MinHash", install="pip install"),
            ExtraStatus(
                name="convert", installed=False, provides="LibreOffice", install="pip install"
            ),
        ],
        embedding=EmbeddingCapability(
            model="nomic-ai/modernbert-embed-base",
            device="cpu",
            document_dims=768,
            token_dims=256,
            local_model_available=True,
            runner_url=None,
            can_embed=True,
        ),
        retrieval=RetrievalCapability(
            methods=["vector", "fulltext", "bm25", "maxsim"],
            default_methods=["vector", "bm25"],
            default_weights={"vector": 0.86, "bm25": 0.14},
            search_exclude_usetypes=["entity", "entities", "summary", "derived"],
            bm25_exclude_usetypes=["entity"],
        ),
        ingest=IngestCapability(
            usetypes=["document", "wiki:arxiv"],
            detectable_formats=["pdf", "docx", "xlsx", "pptx", "markdown", "html"],
            probeable_formats=["pdf", "docx", "xlsx"],
            task_types=["embed", "index:bm25"],
        ),
        llm=LlmCapability(configured=False, base_url="", model="", requires_llm=["synthesis"]),
        corpus=CorpusFacts(
            documents=1284,
            documents_with_vectors=1200,
            documents_with_token_vectors=0,
            bm25_indexes=["default"],
        ),
    )


CAPABILITY_HARNESS = textwrap.dedent("""
    import { install, serialize } from "./dom.mjs";
    install();
    const { views } = await import("%(static)s/shell/registry.js");
    await import("%(static)s/views/capabilities.js");

    const capabilities = %(capabilities)s;
    const calls = [];
    const client = {
      operations: { a: 1, b: 2, c: 3 },
      health_check_liveness: async () => { calls.push("health"); return { status: "ok" }; },
      capabilities: async (args) => { calls.push(["capabilities", args]); return capabilities; },
    };

    const [view] = views();
    const element = await view.component({ client, params: {}, navigate: () => {} });
    console.log(JSON.stringify({
      registration: { id: view.id, title: view.title, path: view.path, tags: [...view.tags] },
      calls,
      tree: serialize(element),
    }));
    """)


def _rows(tree: dict) -> dict[str, dict]:
    """The capability table as ``{row label: the cell}``."""

    def walk(node):
        if node["tag"] == "tr":
            head, cell = (c for c in node["children"] if c["tag"] in ("th", "td"))
            yield head["text"], cell
        for child in node["children"]:
            yield from walk(child)

    return dict(walk(tree))


def _pills(cell: dict) -> list[str]:
    """The pill labels in one cell, and the pills alone — a "none" is not a pill."""
    return [
        c["text"]
        for c in cell["children"][0]["children"]
        if c["class"] and c["class"].startswith("pill")
    ]


@needs_node
def test_the_capability_table_renders_the_fields_the_response_actually_carries(tmp_path):
    """The three defects of 2026-09-14, each asserted on what reaches the page.

    Written against the RENDERED OUTPUT and not against the absence of a console error, which
    is the point. ``read_console_messages`` with ``onlyErrors`` reported nothing when all three
    rows were wrong: ``(items || []).length === 0`` rendered "none", and an empty list is not
    an error. A test that only checked "it did not throw" would have passed before the fix too.
    """
    fixture = _capabilities_fixture()
    result = _node(
        tmp_path,
        CAPABILITY_HARNESS.replace("%(capabilities)s", fixture.model_dump_json()),
    )

    assert result["registration"] == {
        "id": "capabilities",
        "title": "This appliance",
        "path": "/capabilities",
        "tags": ["appliance"],
    }
    assert result["calls"] == ["health", ["capabilities", {"corpus": True}]]

    rows = _rows(result["tree"])

    # Defect 1: `extras` is list[ExtraStatus], and reading it as an object rendered its
    # indices — "0 1 2 3 4". Five named pills, three of them marked installed.
    assert _pills(rows["Extras"]) == ["embed", "office", "rdf", "sketch", "convert"]
    marked = [
        c["text"] for c in rows["Extras"]["children"][0]["children"] if c["class"] == "pill on"
    ]
    assert marked == ["embed", "rdf", "sketch"]
    assert "not installed" in _titles(rows["Extras"])["office"]

    # Defect 2: there is no `cap.formats`. Both real lists reach the page, under labels that
    # say which question each answers.
    assert _pills(rows["Formats identified from the bytes"]) == [
        "pdf",
        "docx",
        "xlsx",
        "pptx",
        "markdown",
        "html",
    ]
    assert _pills(rows["Formats something can look inside"]) == ["pdf", "docx", "xlsx"]

    # Defect 3: the field is `search_exclude_usetypes`, and it rendered as "none".
    assert _pills(rows["Held out of every result"]) == [
        "entity",
        "entities",
        "summary",
        "derived",
    ]
    assert _pills(rows["Never written to a BM25 index"]) == ["entity"]

    # The fourth finding, which was a decision: the page asked for corpus counts and dropped
    # them. It renders them, and it says out loud which method returns nothing here.
    assert rows["Documents"]["text"] == "1284"
    assert rows["Documents a vector search can reach"]["text"].startswith("1200")
    maxsim = rows["Documents MaxSim can rank"]
    assert maxsim["children"][0]["text"] == "0"
    assert maxsim["children"][0]["class"] == "bad"
    assert "returns nothing here" in maxsim["text"]
    assert _pills(rows["BM25 indexes"]) == ["default"]

    assert rows["Version"]["text"] == "0.5.1"
    assert rows["Can produce a vector"]["text"].startswith("yes, in this process")
    assert rows["Operations this client knows"]["text"].startswith("3")


def _titles(cell: dict) -> dict[str, str]:
    return {c["text"]: c["title"] or "" for c in cell["children"][0]["children"]}


MISSPELT_FIELD_HARNESS = textwrap.dedent("""
    import { install } from "./dom.mjs";
    install();
    const { field, ContractError } =
      await import("%(static)s/views/capabilities.js");

    const out = {};
    const attempt = (label, response, path) => {
      try {
        out[label] = { value: field(response, path), error: null };
      } catch (error) {
        out[label] = {
          value: null,
          error: error instanceof ContractError ? error.message : `WRONG TYPE: ${error}`,
        };
      }
    };

    attempt("present", { version: "1" }, "version");
    attempt("present but null", { embedding: { runner_url: null } }, "embedding.runner_url");
    attempt("absent leaf", { retrieval: { methods: [] } }, "retrieval.search_exclude_usetypes");
    attempt("absent branch", {}, "ingest.detectable_formats");
    attempt("undeclared path", { formats: [] }, "formats");
    console.log(JSON.stringify(out));
    """)


@needs_node
def test_a_field_the_response_does_not_carry_is_an_error_and_not_the_word_none(tmp_path):
    """The structural half of the fix, and the half that generalises.

    Correcting three names would have left the next misspelling to render as "none" again. What
    stops that is that the view no longer reaches into the response with ``.``: every read goes
    through ``field()``, an ABSENT key raises, and a key that is present and null comes back as
    the value it is. ``embedding.runner_url`` is null on an appliance that embeds locally, and
    that is an answer rather than a gap.
    """
    result = _node(tmp_path, MISSPELT_FIELD_HARNESS)

    assert result["present"] == {"value": "1", "error": None}
    assert result["present but null"] == {"value": None, "error": None}

    for label in ("absent leaf", "absent branch", "undeclared path"):
        assert result[label]["error"], f"{label} was read as a value instead of refused"
    assert "search_exclude_usetypes" in result["absent leaf"]["error"]
    assert "READS" in result["undeclared path"]["error"]


def _walk_contract(model: type[BaseModel], path: str) -> object:
    """Resolve one dotted path against a Pydantic model, or raise ``KeyError``."""
    current: object = model
    for key in path.split("."):
        if not (isinstance(current, type) and issubclass(current, BaseModel)):
            raise KeyError(f"{path}: {current} has no fields to look {key} up in")
        info = current.model_fields.get(key)
        if info is None:
            raise KeyError(f"{path}: no field {key!r} on {current.__name__}")
        annotation = info.annotation
        # ``Optional[CorpusFacts]`` is the one wrapper that appears here; step into it so a
        # path through an optional sub-model resolves the way a page reading it would.
        if get_origin(annotation) is Union:
            members = [a for a in get_args(annotation) if a is not type(None)]
            annotation = members[0] if len(members) == 1 else annotation
        current = annotation
    return current


def test_the_capability_view_reads_only_fields_the_contract_carries():
    """Every path in the view's ``READS`` resolves against ``CapabilitiesResponse``.

    This is the check whose absence caused the defect. The generated ``client/verbs.d.ts``
    already carried the right shape and nothing compared the page to it; comparing the page to
    the Pydantic model instead is stronger, because the model is what the appliance serialises.
    """
    source = (STATIC / "views" / "capabilities.js").read_text(encoding="utf-8")
    block = source[source.index("export const READS = Object.freeze([") :]
    reads = re.findall(r'"([a-z_.]+)"', block[: block.index("]);")])
    assert reads, "capabilities.js no longer declares READS"

    for path in reads:
        _walk_contract(CapabilitiesResponse, path)  # KeyError is the failure

    # And the declaration is not allowed to go stale in the other direction: a path used in the
    # module and missing from READS is refused at runtime by ``field()`` itself, so what is
    # checked here is that nothing bypasses it. Comment lines are dropped first — this module
    # quotes the three wrong names on purpose, so that a reader meets the defect at the fix.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("//", "*", "/*"))
    )
    bypass = re.findall(r"\bcap\.[a-z_]+", code)
    assert not bypass, f"the view reaches into the response directly: {bypass}"


# ---------------------------------------------------------------------------- IC-8: renderers


def test_every_value_in_the_renderers_tuple_has_exactly_one_renderer():
    """IC-8's pin, with the tuple read out of Python rather than restated in JavaScript.

    ``RENDERERS`` lives at ``jmfts_core/models/usetype_presentation.py:14`` and a copy of it in
    the bundle would make this a comparison between two lists rather than a check of one. Both
    directions matter: a value with no renderer is a document that cannot be shown, and a
    renderer for a value the tuple does not carry is dead code with a plausible name.
    """
    source = (STATIC / "render" / "renderers.js").read_text(encoding="utf-8")
    registered = re.findall(r'^registerRenderer\("([a-z-]+)", \{', source, re.M)
    assert sorted(registered) == sorted(RENDERERS), (
        f"registered: {sorted(registered)}, RENDERERS: {sorted(RENDERERS)}. Adding a renderer "
        "is a tuple edit plus a migration plus one registration here."
    )
    assert len(set(registered)) == len(registered), "a renderer is registered twice"


RENDERER_HARNESS = textwrap.dedent("""
    import { install, serialize } from "./dom.mjs";
    install();
    const { render, rendererNames, rendererFor, UnknownRendererError, RenderError } =
      await import("%(static)s/render/renderers.js");

    const presentation = (renderer, config = {}) => ({
      renderer, child_handling: "collapsed", link_handling: "footnotes", renderer_config: config,
    });
    const doc = { id: 7, title: "t", usetype: "document" };

    const out = { names: rendererNames(), showing: {}, rendered: {}, refusals: {} };
    for (const name of rendererNames()) out.showing[name] = rendererFor(name).showing;

    out.rendered.plain = serialize(render("a\\nb", presentation("plain"), doc));
    out.rendered.code = serialize(render("x=1", presentation("code", { language: "py" }), doc));
    out.rendered.markdown = serialize(render("# H", presentation("markdown"), doc));
    out.rendered.table = serialize(render(
      JSON.stringify([{ a: 1, b: null }, { a: 2, c: 3 }]), presentation("json-table"), doc));

    const refuse = (label, fn) => {
      try { fn(); out.refusals[label] = null; }
      catch (error) { out.refusals[label] = [error.name, error.message]; }
    };
    refuse("unknown renderer", () => render("x", presentation("mermaid"), doc));
    refuse("json-table on prose", () => render("not json", presentation("json-table"), doc));
    console.log(JSON.stringify(out));
    """)


@needs_node
def test_a_renderer_that_is_not_finished_says_so_on_the_page(tmp_path):
    """Step 25 ships the interface; step 29 ships the renderers, and the gap is visible.

    Three of the five are complete because what they claim to do is all there is to do.
    ``markdown`` and ``transcript`` are not, and rather than let them look finished they
    declare ``showing`` and ``render()`` puts that sentence on the page. The notice is not the
    renderer's to remove: a step-29 author sets ``showing: null`` and it goes, so finishing a
    renderer and forgetting to take the notice down is not a state that exists.
    """
    result = _node(tmp_path, RENDERER_HARNESS)

    assert sorted(result["names"]) == sorted(RENDERERS)
    complete = {name for name, showing in result["showing"].items() if showing is None}
    assert complete == {"plain", "code", "json-table"}

    for name in ("markdown", "transcript"):
        assert "step 29" in result["showing"][name], (
            f"the {name} renderer must say what it is actually showing and where the real one "
            "is scheduled"
        )

    markdown = result["rendered"]["markdown"]
    assert markdown["class"] == "render-partial"
    assert markdown["children"][0]["class"] == "render-notice"
    assert "SOURCE" in markdown["children"][0]["text"]
    assert markdown["children"][1]["text"] == "# H"

    # The complete three are not wrapped, because there is nothing to warn about.
    assert result["rendered"]["plain"]["class"] == "render-plain"
    assert result["rendered"]["plain"]["text"] == "a\nb"
    code = result["rendered"]["code"]
    assert code["class"] == "render-code"
    assert code["children"][0]["class"] == "language-py"

    table = result["rendered"]["table"]
    assert table["class"] == "render-json-table"
    header = [c["text"] for c in table["children"][0]["children"]]
    assert header == ["a", "b", "c"]
    # A column this row does not carry and a column whose value is null are different facts.
    first = table["children"][1]["children"]
    assert [c["text"] for c in first] == ["1", "null", "—"]
    assert first[2]["class"] == "dim"

    assert result["refusals"]["unknown renderer"][0] == "UnknownRendererError"
    assert "mermaid" in result["refusals"]["unknown renderer"][1]
    assert result["refusals"]["json-table on prose"][0] == "RenderError"


# ------------------------------------------------------------------ IC-9: the highlight overlay

#: The three anchor kinds, verbatim from the shapes the sprint plan cites and the models in
#: ``jmfts-client/jmfts_client/contracts/anchor.py`` parse.
PDF_ANCHOR = {"kind": "pdf", "page": 3, "bbox": [72.0, 118.4, 540.0, 262.9]}
CELLS_ANCHOR = {"kind": "cells", "sheet": "Q3 Pipeline", "ref": "B4:H120"}
SPAN_ANCHOR = {"kind": "span", "char_start": 100, "char_end": 240}

OVERLAY_HARNESS = textwrap.dedent("""
    import { install } from "./dom.mjs";
    install();
    const { highlight, canPlace, HIGHLIGHT_STATUS, OverlayError, UnknownAnchorKind } =
      await import("%(static)s/render/overlay.js");

    const ANCHORS = %(anchors)s;
    const geometry = {
      source: { width: 612, height: 792 },
      pixels: { width: 1224, height: 1584 },
      originCorner: "top-left",
      page: 3,
    };

    const out = { same: {}, results: {}, refusals: {}, canPlace: {} };

    // IC-9's pin: one anchor, three surfaces of three kinds, one box.
    for (const kind of ["pdf-page", "image", "sheet-region"]) {
      out.same[kind] = highlight({ anchor: ANCHORS.pdf, unresolved: null }, { kind, ...geometry });
    }

    const on = (extra) => ({ kind: "pdf-page", ...geometry, ...extra });
    out.results.box = out.same["pdf-page"];
    out.results.flipped = highlight(
      { anchor: ANCHORS.pdf, unresolved: null }, on({ originCorner: "bottom-left" }));
    out.results.elsewhere = highlight({ anchor: ANCHORS.pdf, unresolved: null }, on({ page: 9 }));
    out.results.continued = highlight(
      { anchor: { ...ANCHORS.pdf, continues: [4, 5] }, unresolved: null }, on({ page: 4 }));
    out.results.spread = highlight(
      { anchor: { ...ANCHORS.pdf, continues: [4] }, unresolved: null }, on({}));
    out.results.none = highlight({ anchor: null, unresolved: null }, on({}));
    out.results.unresolved = highlight(
      { anchor: null, unresolved: { code: "no_source_span", reason: "the chunk predates it" } },
      on({}));

    // A cells anchor over a sheet region, whose surface resolves its own A1 grammar. The rect
    // it returns is the SAME source rectangle the pdf anchor names, so the two must agree.
    const sheet = {
      kind: "sheet-region", ...geometry, sheet: "Q3 Pipeline",
      cellRect: (ref) => (ref === "B4:H120" ? [72.0, 118.4, 540.0, 262.9] : null),
    };
    out.results.cells = highlight({ anchor: ANCHORS.cells, unresolved: null }, sheet);
    out.results.otherSheet = highlight(
      { anchor: { ...ANCHORS.cells, sheet: "Q4" }, unresolved: null }, sheet);
    out.results.offRegion = highlight(
      { anchor: { ...ANCHORS.cells, ref: "AA900" }, unresolved: null }, sheet);

    const refuse = (label, fn) => {
      try { fn(); out.refusals[label] = null; }
      catch (error) {
        out.refusals[label] = [
          error instanceof UnknownAnchorKind ? "UnknownAnchorKind"
            : error instanceof OverlayError ? "OverlayError" : error.name,
          error.message,
        ];
      }
    };
    refuse("span on a page", () => highlight({ anchor: ANCHORS.span, unresolved: null }, on({})));
    refuse("unknown kind", () =>
      highlight({ anchor: { kind: "waveform" }, unresolved: null }, on({})));
    refuse("both rows", () =>
      highlight({ anchor: ANCHORS.pdf, unresolved: { code: "c", reason: "r" } }, on({})));
    refuse("only one key", () => highlight({ anchor: null }, on({})));
    refuse("no origin corner", () => highlight(
      { anchor: ANCHORS.pdf, unresolved: null },
      { kind: "image", source: geometry.source, pixels: geometry.pixels, page: 3 }));
    refuse("no page declared", () => highlight(
      { anchor: ANCHORS.pdf, unresolved: null },
      { kind: "image", source: geometry.source, pixels: geometry.pixels,
        originCorner: "top-left" }));
    refuse("cells with no grid", () =>
      highlight({ anchor: ANCHORS.cells, unresolved: null }, on({})));

    out.canPlace.pdf = canPlace(ANCHORS.pdf, on({}));
    out.canPlace.span = canPlace(ANCHORS.span, on({}));
    out.canPlace.cells = canPlace(ANCHORS.cells, sheet);
    out.status = HIGHLIGHT_STATUS;
    console.log(JSON.stringify(out));
    """)


@pytest.fixture(scope="module")
def overlay(tmp_path_factory):
    if NODE is None:
        pytest.skip("no JavaScript runtime on PATH")
    anchors = json.dumps({"pdf": PDF_ANCHOR, "cells": CELLS_ANCHOR, "span": SPAN_ANCHOR})
    return _node(
        tmp_path_factory.mktemp("overlay"),
        OVERLAY_HARNESS.replace("%(anchors)s", anchors),
    )


def test_one_anchor_renders_identically_on_all_three_surfaces(overlay):
    """IC-9's pin, and the reason the overlay is one component rather than three.

    The sprint plan's argument is that "writing it three times is how the three end up
    disagreeing about which corner the origin is in". So the three surfaces here differ ONLY in
    their ``kind`` — same source extent, same pixel size, same declared origin — and the box
    must be the same box. It is the same box because ``toPixels`` never reads ``kind``; the day
    it does, this goes red.
    """
    boxes = overlay["same"]
    assert set(boxes) == {"pdf-page", "image", "sheet-region"}
    assert len({json.dumps(box, sort_keys=True) for box in boxes.values()}) == 1, boxes
    assert boxes["pdf-page"]["status"] == "box"


def test_the_box_is_the_source_rectangle_scaled_and_the_origin_is_declared(overlay):
    """The geometry itself, at 2x, and the flip a bottom-left surface needs.

    Points, not pixels, are what the anchor stores — the anchor contract is explicit that a
    stored pixel value would bake one viewer's zoom into the record — so the scale is the
    surface's and this is where it is applied.
    """
    # ``approx`` because these are the products of binary floating point, not because the
    # numbers are uncertain: 262.9 - 118.4 is 144.49999999999997 and doubling it keeps the tail.
    box = overlay["results"]["box"]["box"]
    assert box == pytest.approx({"left": 144.0, "top": 236.8, "width": 936.0, "height": 289.0})

    # Same rectangle, a surface whose origin is the other corner: the box is the same size and
    # the same distance from the OTHER edge. 792 - 262.9 = 529.1 points, doubled.
    flipped = overlay["results"]["flipped"]["box"]
    assert flipped["left"] == box["left"]
    assert flipped["width"] == box["width"]
    assert flipped["height"] == box["height"]
    assert flipped["top"] == pytest.approx(1058.2)


def test_a_passage_on_another_page_is_not_a_box_drawn_on_this_one(overlay):
    """Three answers where a viewer might expect one, and they are different answers.

    A rectangle from page 3 drawn on page 9 is confidently wrong, which is worse than absent
    and indistinguishable from right. ``continues`` is the case the anchor contract added the
    field for: the box covers only the page the passage BEGINS on, so a page it merely runs
    onto gets "continued" and never the starting page's rectangle.
    """
    assert overlay["results"]["elsewhere"] == {"status": "elsewhere", "page": 3}
    assert overlay["results"]["continued"] == {"status": "continued", "page": 4, "from": 3}
    assert overlay["results"]["spread"]["status"] == "box"
    assert overlay["results"]["spread"]["continues"] == [4]
    assert overlay["results"]["box"]["continues"] is None, "absent, not empty"


def test_no_highlight_and_an_unrecoverable_highlight_are_different_answers(overlay):
    """The Fail Early rule applied to a surface, and the failure this sprint is written against.

    ``source_anchor.unresolved`` is its own evidence row, "present exactly when ``anchor`` is
    not" (``jmfts_core/evidence.py:453``). Collapsing the two into a blank surface is how the
    second one stays invisible, so the result is TAGGED and a caller cannot handle one without
    having seen the other. The reason is carried through verbatim: it is a sentence written for
    a human at ``citation_tasks.py:284``, and rewriting it here would replace an explanation
    with a shrug.
    """
    assert overlay["results"]["none"] == {"status": "none"}
    assert overlay["results"]["unresolved"] == {
        "status": "unresolved",
        "code": "no_source_span",
        "reason": "the chunk predates it",
    }
    assert overlay["refusals"]["both rows"][0] == "OverlayError"
    assert "453" in overlay["refusals"]["both rows"][1]
    assert overlay["refusals"]["only one key"][0] == "OverlayError"


def test_a_cells_anchor_lands_where_the_same_rectangle_does(overlay):
    """The sheet surface owns the A1 grammar; the overlay owns the transform.

    ``CellsAnchor``'s docstring refuses a second A1 parser in the client, and a third in
    JavaScript would be worse again — so the surface resolves the ref against the grid it was
    built from and hands back its own source coordinates. What must then be true is that those
    coordinates go through the SAME transform, which is what this asserts: the same rectangle,
    reached two ways, is the same box.
    """
    assert overlay["results"]["cells"]["box"] == overlay["results"]["box"]["box"]
    assert overlay["results"]["otherSheet"] == {
        "status": "elsewhere",
        "sheet": "Q4",
        "showing": "Q3 Pipeline",
    }
    assert overlay["results"]["offRegion"]["status"] == "elsewhere"
    assert overlay["results"]["offRegion"]["ref"] == "AA900"


def test_the_overlay_refuses_what_it_cannot_honestly_draw(overlay):
    """Five refusals, and each is a silently-wrong box that does not get drawn.

    A span anchor is a VALID anchor that has no geometry, and saying so points the caller at
    the text renderer instead of leaving them to conclude the highlight is broken. An unknown
    kind means this front end is older than the appliance — the same answer
    ``UnknownAnchorKind`` gives in the Python contract, for the same reason. A surface with no
    declared origin corner is a coin flip on every box's vertical position.
    """
    refusals = overlay["refusals"]
    assert refusals["span on a page"][0] == "OverlayError"
    assert "anchor.py" in refusals["span on a page"][1]
    assert refusals["unknown kind"][0] == "UnknownAnchorKind"
    assert "waveform" in refusals["unknown kind"][1]
    assert "originCorner" in refusals["no origin corner"][1]
    assert "page" in refusals["no page declared"][1]
    assert "cellRect" in refusals["cells with no grid"][1]

    assert overlay["canPlace"] == {"pdf": True, "span": False, "cells": True}
    assert overlay["status"] == {
        "BOX": "box",
        "CONTINUED": "continued",
        "ELSEWHERE": "elsewhere",
        "NONE": "none",
        "UNRESOLVED": "unresolved",
    }
