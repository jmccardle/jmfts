"""The tree browser: ``jmfts-web/jmfts_web/static/views/tree.js``.

``docs/SPRINT_0_6_0.md`` Block F step 28, the first half. Three properties are worth a test
here and the rest of the file is the ordinary business of a page drawing rows:

* **One parent, several child shapes.** Block C landed at M1 and a worksheet emits BOTH a
  ``record`` shape and a ``table`` shape for the same sheet (``models/document.py`` at
  ``USETYPE_TABLE``: "two renderings of one sheet... a query matching both returns both"). A
  browser that drew one flat list would show all of it and communicate half of it, so the
  children are grouped by usetype and every group reaches the page.
* **It reads the DOCUMENT surface, not the presentation-filtered one.**
  ``view_renderer.build_children_stubs`` returns ``[]`` for ``child_handling: "hidden"``,
  which is correct for a reading view and would delete a subtree from a browser.
* **A route argument that cannot be honoured refuses.** ``?root=fortytwo`` is a pasted link
  that is wrong, and answering it with the roots would be answering a different question.

Same arrangement as ``tests/test_web_views.py``: no database, no browser, and where ``node``
is on PATH the real ES modules run against a stubbed DOM. The stub here models exactly what
this view touches, which is that file's rule — anything beyond it throws rather than quietly
returning ``undefined``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from jmfts_client.contracts.document import DocumentResponse

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "jmfts-web" / "jmfts_web" / "static"
VIEW = STATIC / "views" / "tree.js"
MANIFEST = STATIC / "shell" / "manifest.js"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="no JavaScript runtime on PATH")

DOM_STUB = textwrap.dedent("""
    export class Element {
      constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.childNodes = [];
        this.attributes = {};
        this.className = "";
        this.dataset = {};
        this.hidden = false;
        this.href = null;
        this.disabled = false;
        this.listeners = {};
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
      set textContent(value) {
        this.childNodes = [];
        this._text = value === null || value === undefined ? "" : String(value);
      }
      get textContent() {
        return this._text + this.childNodes.map((n) => n.textContent).join("");
      }
      addEventListener(name, handler) { this.listeners[name] = handler; }
    }

    const head = new Element("head");
    export const document = { createElement: (tag) => new Element(tag), head };

    /** The rendered tree as plain data, so the assertions live in Python. */
    export function serialize(element) {
      return {
        tag: element.tagName.toLowerCase(),
        class: element.className || null,
        text: element.textContent,
        href: element.href,
        hidden: element.hidden,
        id: element.dataset.id ?? null,
        children: element.childNodes.map(serialize),
      };
    }

    export function install() {
      globalThis.__head = head;
      globalThis.Element = Element;
      globalThis.document = document;
    }
    """)


def _node(tmp_path: Path, harness: str, **substitutions: str) -> object:
    """Run one harness module and parse the single JSON document it prints."""
    (tmp_path / "dom.mjs").write_text(DOM_STUB, encoding="utf-8")
    # `str.replace` and not `%`-formatting: a fixture is JSON and JSON is full of `%`-free
    # punctuation right up until the day a document title carries a percent sign.
    source = harness.replace("%(static)s", STATIC.as_uri())
    for name, value in substitutions.items():
        source = source.replace(f"%({name})s", value)
    script = tmp_path / "harness.mjs"
    script.write_text(source, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, cwd=tmp_path, timeout=60
    )
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout)


def _document(**overrides) -> dict:
    """A real ``DocumentResponse``, dumped, so a fixture cannot drift from the contract.

    The same reasoning ``tests/test_web_views.py::_capabilities_fixture`` gives: a hand-typed
    dict is a second copy of the contract and can carry the very misspelling a page under test
    is supposed to be unable to have.
    """
    fields = {
        "id": 1,
        "parent_id": None,
        "title": "A document",
        "content": None,
        "structured_content": {},
        "path": [],
        "depth": 0,
        "usetype": "file",
        "created_at": None,
        "updated_at": None,
        "content_hash": None,
    }
    fields.update(overrides)
    return json.loads(DocumentResponse(**fields).model_dump_json())


def _find(node: dict, className: str) -> list[dict]:
    found = [node] if node["class"] == className else []
    for child in node["children"]:
        found.extend(_find(child, className))
    return found


# --------------------------------------------------------------------------- the declaration


def test_the_tree_is_a_registered_view_and_the_manifest_imports_it():
    """IC-7, from the outside: a view is a file plus one appended import line."""
    source = VIEW.read_text(encoding="utf-8")
    assert re.search(r'registerView\(\{\s*\n\s*id: "tree"', source), "tree.js registers no view"
    assert 'path: "/documents"' in source
    assert 'import "../views/tree.js";' in MANIFEST.read_text(encoding="utf-8")


def _code(path: Path) -> str:
    """The module with its comments dropped — this file's prose names what it must not call."""
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith(("//", "*", "/*"))
    )


def test_the_tree_reads_the_document_surface_and_never_the_presentation_filtered_one():
    """The decision this view turns on, asserted against the code rather than the comment.

    ``GET /view/{id}``'s ``children_stubs`` are the wrong rows for a browser: a usetype whose
    presentation says ``child_handling: "hidden"`` gets ``[]`` (``view_renderer.py:107``), and
    a tree built on them would silently omit whole subtrees because of a display rule. So this
    view calls ``get_children`` and must not reach for the view surface at all.
    """
    code = _code(VIEW)
    assert "client.get_children(" in code
    assert "client.get_root_documents(" in code
    for forbidden in ("expand_children", "view_document", "children_stubs"):
        assert forbidden not in code, (
            f"tree.js calls {forbidden}, which is the /view/* surface. That surface is "
            "presentation-filtered and a tree browser must not be."
        )


def test_the_view_reads_only_fields_the_document_contract_carries():
    """Every ``node.x``, ``child.x`` and ``ancestor.x`` in the module is a real field.

    The defect this is written against is the one that opened step 24's lane: a page read three
    field names the response does not carry and all three rendered as the word "none", because
    ``undefined`` is a perfectly good empty. Here the check is mechanical — the three variables
    that hold a ``DocumentResponse`` are named consistently, so every property access through
    them can be resolved against the Pydantic model itself rather than against a second list
    somebody has to keep current.
    """
    reads = sorted(set(re.findall(r"(?<![\w./\"])(?:node|child|ancestor)\.([a-z_]+)", _code(VIEW))))
    assert reads, "nothing in tree.js reads a document; this test is asserting nothing"
    unknown = [name for name in reads if name not in DocumentResponse.model_fields]
    assert not unknown, (
        f"tree.js reads {unknown} off a DocumentResponse, which carries "
        f"{sorted(DocumentResponse.model_fields)}. jmfts-client is the authority."
    )


# ------------------------------------------------------------------------------ the grouping

EXPAND_HARNESS = textwrap.dedent("""
    import { install, serialize } from "./dom.mjs";
    install();
    const { expandInto, byUsetype, CHILD_LIMIT } = await import("%(static)s/views/tree.js");
    const { document } = await import("./dom.mjs");

    const children = %(children)s;
    const calls = [];
    const client = {
      get_children: async (args) => { calls.push(args); return children; },
    };

    const container = document.createElement("div");
    const groups = await expandInto(container, 9, { client });
    console.log(JSON.stringify({
      calls,
      limit: CHILD_LIMIT,
      groups: groups.map(([usetype, members]) => [usetype, members.map((m) => m.id)]),
      shape: byUsetype(children).map(([usetype, members]) => [usetype, members.length]),
      tree: serialize(container),
    }));
    """)


@needs_node
def test_one_sheet_with_two_child_shapes_shows_both_of_them(tmp_path):
    """Block C's consequence, on the page. ``record`` AND ``table`` AND ``profile``.

    ``models/document.py`` at ``USETYPE_TABLE``: a ``table`` node and the ``record`` nodes of
    the same sheet "routinely BOTH exist, and neither contains the other". A tree that listed
    them in one undifferentiated run would be truthful and unreadable — the one ``table`` is
    lost among four hundred rows — so the children are grouped by usetype and every group is
    headed by its count.

    The groups are in FIRST-APPEARANCE order, which is the server's row order rather than
    whatever order a JS engine gives a set of keys.
    """
    children = [
        _document(id=11, usetype="record", title="row 2"),
        _document(id=12, usetype="record", title="row 3"),
        _document(id=13, usetype="table", title="Q3 Pipeline as a table"),
        _document(id=14, usetype="profile", title="Q3 Pipeline, measured"),
        _document(id=15, usetype="record", title="row 4"),
    ]
    result = _node(tmp_path, EXPAND_HARNESS, children=json.dumps(children))

    assert result["calls"] == [{"document_id": 9, "limit": result["limit"]}]
    assert result["groups"] == [
        ["record", [11, 12, 15]],
        ["table", [13]],
        ["profile", [14]],
    ]
    assert result["shape"] == [["record", 3], ["table", 1], ["profile", 1]]

    headings = [g["text"] for g in _find(result["tree"], "tree-group")]
    assert headings == ["3 × record", "1 × table", "1 × profile"]

    # And every child is a row, whatever group it fell into: five children in, five rows out.
    rows = _find(result["tree"], "tree-node")
    assert [row["id"] for row in rows] == ["11", "12", "15", "13", "14"]
    assert "Q3 Pipeline as a table" in result["tree"]["text"]


@needs_node
def test_a_child_with_no_usetype_is_labelled_and_not_smoothed_over(tmp_path):
    """A node no rule claims is worth seeing as one.

    ``usetype`` is ``Optional[str]`` on the contract, so null is a value the page can meet. It
    groups under its own heading and says what it is rather than being folded into a plausible
    name — which is the same distinction the capability table had to learn between a field that
    is empty and a field that is not there.
    """
    children = [_document(id=21, usetype=None, title="orphaned")]
    result = _node(tmp_path, EXPAND_HARNESS, children=json.dumps(children))

    assert result["groups"] == [[None, [21]]]
    assert [g["text"] for g in _find(result["tree"], "tree-group")] == ["1 × no usetype"]
    assert "no usetype" in _find(result["tree"], "pill")[0]["text"]


@needs_node
def test_a_full_page_of_children_says_it_may_be_short(tmp_path):
    """``/documents/{id}/children`` has a ``limit`` and no ``offset``, so there is no page 2.

    A browser that asked for 200 and drew 200 with nothing said would be claiming a subtree is
    200 wide when what it knows is that it asked for 200. The notice names the route and the
    argument, and the call itself is in the shell's log where step 30's view replays it with a
    bigger one.
    """
    limit = 200
    children = [_document(id=100 + n, usetype="chunk", title=f"chunk {n}") for n in range(limit)]
    result = _node(tmp_path, EXPAND_HARNESS, children=json.dumps(children))

    assert result["limit"] == limit
    text = result["tree"]["text"]
    assert "there may be more" in text
    assert "/documents/{id}/children" in text


@needs_node
def test_a_node_with_no_children_says_so(tmp_path):
    """Expanded and empty is an answer, and a blank box is not."""
    result = _node(tmp_path, EXPAND_HARNESS, children="[]")
    assert result["groups"] == []
    assert result["tree"]["text"] == "no children"


# ---------------------------------------------------------------------------------- the page

PAGE_HARNESS = textwrap.dedent("""
    import { install, serialize } from "./dom.mjs";
    install();
    const view = await import("%(static)s/views/tree.js");
    const { views } = await import("%(static)s/shell/registry.js");

    const fixtures = %(fixtures)s;
    const calls = [];
    const client = {
      get_root_documents: async () => { calls.push(["roots", null]); return fixtures.roots; },
      get_document: async (args) => { calls.push(["document", args]); return fixtures.node; },
      get_ancestors: async (args) => { calls.push(["ancestors", args]); return fixtures.ancestors; },
      get_children: async (args) => { calls.push(["children", args]); return fixtures.children; },
    };

    const registration = views().find((v) => v.id === "tree");
    const out = { registration: {
      id: registration.id, title: registration.title, path: registration.path,
      tags: [...registration.tags],
    }, refusals: {} };

    const roots = await registration.component({ client, params: {}, navigate: () => {} });
    out.roots = serialize(roots);
    out.rootCalls = [...calls];

    calls.length = 0;
    const focused = await registration.component(
      { client, params: { root: "9" }, navigate: () => {} });
    out.focused = serialize(focused);
    out.focusedCalls = [...calls];

    try {
      await registration.component({ client, params: { root: "fortytwo" }, navigate: () => {} });
      out.refusals.badRoot = null;
    } catch (error) {
      out.refusals.badRoot = [error.name, error.message];
    }

    // An expansion that fails: the appliance answers and the answer is a refusal.
    const failing = {
      get_children: async () => { throw new Error("Document 9 not found"); },
    };
    const row = view.nodeRow(fixtures.node, { client: failing });
    await row.toggle();
    out.failed = serialize(row.element);
    // Three mounts above, and one stylesheet: the view's own, appended to <head> by the
    // view's own module. index.html is a file five F3 worktrees would otherwise all edit.
    out.styles = globalThis.__head.childNodes.map((n) => [n.rel, n.href]);
    console.log(JSON.stringify(out));
    """)


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    if NODE is None:
        pytest.skip("no JavaScript runtime on PATH")
    fixtures = {
        "roots": [
            _document(id=1, usetype="file", title="Q3 Pipeline.xlsx", produced_by=None),
            _document(
                id=2,
                usetype="file",
                title="still ingesting.pdf",
                settled="in_flight",
                produced_by="ingest:file",
            ),
            _document(
                id=3,
                usetype="section",
                title=None,
                content="a container's text, computed from its children",
                content_source="effective",
            ),
        ],
        "node": _document(id=9, usetype="sheet", title="Q3 Pipeline", produced_by="extract:sheet"),
        "ancestors": [_document(id=1, usetype="file", title="Q3 Pipeline.xlsx")],
        "children": [
            _document(id=11, usetype="record", title="row 2"),
            _document(id=13, usetype="table", title="the whole sheet"),
        ],
    }
    return _node(tmp_path_factory.mktemp("tree"), PAGE_HARNESS, fixtures=json.dumps(fixtures))


def test_the_registration_is_what_the_shell_puts_in_its_navigation(page):
    assert page["registration"] == {
        "id": "tree",
        "title": "Tree",
        "path": "/documents",
        "tags": ["corpus", "documents"],
    }


def test_the_roots_page_says_which_nodes_no_search_can_reach(page):
    """One call, one row per root, and the two facts a row cannot be read without.

    ``settled`` is the one worth the test: every retrieval index is partial on it (CLAUDE.md,
    "Queued ingest"), so an in-flight node is perfectly visible here and invisible to every
    search. A browser that showed it like any other row would be the one page where the two
    surfaces disagree with no explanation on either.
    """
    assert page["rootCalls"] == [["roots", None]]
    rows = _find(page["roots"], "tree-node")
    assert [row["id"] for row in rows] == ["1", "2", "3"]

    text = page["roots"]["text"]
    assert "in_flight — not yet reachable by any search" in text
    assert "by ingest:file" in text

    # A node with no title is marked as having none rather than being given one.
    untitled = _find(page["roots"], "tree-title untitled")
    assert [link["text"] for link in untitled] == ["(untitled)"]

    # Both destinations are links, so they survive a reload and can be copied.
    hrefs = [row["children"][0]["children"][1]["href"] for row in rows]
    assert hrefs == ["#/document?id=1", "#/document?id=2", "#/document?id=3"]
    assert "#/documents?root=3" in json.dumps(page["roots"])

    # A container's text is its children's, and the row says which it is showing.
    assert "effective content, computed from the subtree" in text


def test_a_focused_subtree_arrives_open_with_its_ancestors(page):
    """Three calls, and the node you navigated to is already expanded.

    Asking a reader to click the one node they asked for is a step with no decision in it —
    "I tried something and it gave me an error when the solution didn't require me to make a
    real decision", one notch down.
    """
    assert page["focusedCalls"] == [
        ["document", {"document_id": 9}],
        ["ancestors", {"document_id": 9}],
        ["children", {"document_id": 9, "limit": 200}],
    ]
    crumbs = _find(page["focused"], "tree-crumbs")[0]
    assert "all roots" in crumbs["text"]
    assert "Q3 Pipeline.xlsx" in crumbs["text"]

    # Open: the children are drawn and the container is not hidden.
    groups = [g["text"] for g in _find(page["focused"], "tree-group")]
    assert groups == ["1 × record", "1 × table"]
    assert _find(page["focused"], "tree-children")[0]["hidden"] is False


def test_a_root_argument_that_is_not_a_document_id_refuses(page):
    """A pasted link that is wrong, answered as wrong.

    The alternative is showing the roots, which answers a question the reader did not ask and
    looks exactly like a corpus that happens not to contain the node they were sent to.
    """
    name, message = page["refusals"]["badRoot"]
    assert name == "TreeParamError"
    assert "fortytwo" in message
    assert "#/documents?root=42" in message


def test_the_stylesheet_comes_from_the_view_and_is_linked_once(page):
    """``shell/shell.css:4``'s rule: a view styles itself from its own module.

    At MOUNT and not at import — ``shell/manifest.js`` imports every view for its registration
    alone, and a module that reaches for ``document.head`` when it is merely imported cannot be
    imported that way. Three mounts here, one <link>, resolved against the module's own URL so
    there is no path for another origin to arrive through.
    """
    assert len(page["styles"]) == 1, page["styles"]
    rel, href = page["styles"][0]
    assert rel == "stylesheet"
    assert href.endswith("/views/tree.css")
    assert href.startswith("file:")


def test_an_expansion_that_fails_lands_where_the_children_would_have_been(page):
    """A click handler is outside the shell's catch, so this is where Fail Early is done.

    The shell catches what a COMPONENT throws; nothing catches what a click handler throws, and
    an expander that swallowed its error would leave a row that does nothing when clicked. The
    message the appliance sent goes where the children would have been, and the row stays
    usable.
    """
    text = page["failed"]["text"]
    assert "Document 9 not found" in text
    assert _find(page["failed"], "bad"), "the failure must be marked, not just printed"
    expander = _find(page["failed"], "tiny tree-expander")[0]
    assert expander["text"] == "!"
