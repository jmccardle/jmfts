"""The shell, the view registry, and the two interfaces the views plug into.

``docs/SPRINT_0_6_0.md`` Block F steps 24 and 25 — IC-7, IC-8 and IC-9. Three contracts, and
each is pinned here by the property Part 5.1's table names for it rather than by prose:

* **IC-7** — "two views added in two worktrees merge without conflict". Performed, not
  asserted: two branches of a scratch repository each append a view to the real manifest and
  the merge is run. The same merge is run again without the ``.gitattributes`` line, where it
  must conflict, because a clean merge that nothing forced is a clean merge by luck.
The two interfaces of step 25 are pinned in the same file as they land; what is here at this
commit is IC-7 and the shell the views are mounted into.

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

import pytest

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
