"""The preset editor: saving a search configuration from the browser, and running it.

``docs/SPRINT_0_6_0.md`` Block F over question 4.6 — "Absent an answer, nothing is seeded", and
the reason that question stays open is that a preset nobody can write is a mechanism with no
examples. ``views/search.js`` reads ``/search-contexts/`` and writes none of it; step 27 said why
at its ``CONTEXT_OP`` and ``tests/test_web_search_view.py`` asserts the absence. This file is the
other half, and four properties are pinned here rather than argued:

* **a preset is SHARED, and the page says so before any control.** All five verbs are
  ``CONFIG_OBJECT`` in ``tests/test_expose_principal_audit.py``: "shared mutable state any token
  holder may change". Asserted on the rendered tree — the banner, and the words on every button
  that writes or deletes.
* **a key this editor cannot set survives a save.** Demonstrated on a config carrying all four
  non-editable keys of the contract's vocabulary plus one the contract does not name, put through
  the merge the save actually calls, and asserted value by value. Not a claim in a docstring.
* **a 409 is an answer.** The conflict panel draws what the name already holds, what an overwrite
  would write, and offers the overwrite as a separate act that names who it affects.
* **the loop closes.** The search page links to this one carrying its own fragment, and this page
  links back with ``?context=NAME``, which ``requestFromParams`` in the search view reads.

And one claim the page makes that a reader would not guess — a saved ``limit`` reaches
``GET /search/`` and never a POST — is checked against the real resolver rather than trusted.

The harness is ``tests/test_web_views.py``'s: one DOM stub, one ``_node`` runner, one
``CapabilitiesResponse`` built by Pydantic. Borrowed rather than copied, because a second stub is
a second answer to "what does a document have".
"""

from __future__ import annotations

import inspect
import json
import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Union, get_args, get_origin

from pydantic import BaseModel

import jmfts_client.contracts.meta as contracts_meta
import jmfts_client.contracts.search_context as contracts_search_context
from jmfts_client.contracts.search import SearchRequest
from jmfts_client.contracts.search_context import SearchContextCreate, SearchContextResponse
from jmfts_core.repositories.search_context import SearchContextRepository
from jmfts_core.services.search_service import SearchService
from tests.test_ts_client_codegen import operation_table
from tests.test_web_views import (  # the one harness this repository has for browser code
    _capabilities_fixture,
    _node,
    needs_node,
)

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "jmfts-web" / "jmfts_web" / "static"
VIEW = STATIC / "views" / "presets.js"
SEARCH_VIEW = STATIC / "views" / "search.js"
MANIFEST = STATIC / "shell" / "manifest.js"

CONTRACT_MODULES = {
    "meta": contracts_meta,
    "search_context": contracts_search_context,
}


def _source() -> str:
    return VIEW.read_text(encoding="utf-8")


def _by_class(node: dict, token: str) -> list[dict]:
    """Every node carrying ``token`` among its classes, depth first.

    ``tests/test_web_views.py::_find`` matches the whole class string, and this page's nodes carry
    compound classes ("note bad preset-shared") because the shell's vocabulary and the view's own
    are both in use. One token is what an assertion here wants to name.
    """
    classes = (node["class"] or "").split()
    found = [node] if token in classes else []
    for child in node["children"]:
        found.extend(_by_class(child, token))
    return found


def _texts(node: dict, token: str) -> list[str]:
    return [found["text"] for found in _by_class(node, token)]


# ------------------------------------------------------------- the surface, and the split


def test_the_editor_is_the_whole_write_surface_the_search_view_refuses():
    """Five verbs, one page, and the split between the two pages is the point.

    ``views/search.js`` calls ``list_contexts`` and nothing else, which
    ``tests/test_web_search_view.py`` asserts from the other side. What is asserted here is the
    complement: every ``SearchContextService`` operation the appliance mounts is named by this
    view, so a verb added to that service has a page rather than becoming a surface with no way in.
    """
    source = _source()
    mounted = {op_id for op_id in operation_table() if op_id.startswith("SearchContextService.")}
    assert mounted, "the search-context surface has no operations; this test is about nothing"
    named = set(re.findall(r'"(SearchContextService\.[a-z_]+)"', source))
    assert named == mounted, (
        f"this view names {sorted(named)} and the appliance mounts {sorted(mounted)}. An "
        "operation added to SearchContextService needs a control here or a reason."
    )

    # And it calls them through the operation table rather than by a verb name, so every call this
    # page makes is one the generated table has to carry.
    for op_id in mounted:
        assert op_id in operation_table()


def test_the_manifest_imports_the_presets_view():
    """A view is a file plus one appended line, and this is the line."""
    assert 'import "../views/presets.js";' in MANIFEST.read_text(encoding="utf-8")


def test_the_view_brings_its_own_stylesheet_and_index_html_is_untouched():
    """``shell/shell.css``'s rule: a view's sheet is linked from the view's own module.

    ``index.html`` is the file every parallel worktree would otherwise edit, which is the conflict
    IC-7 exists to remove. Linked at MOUNT behind a module flag, not at module scope, because
    ``shell/shell.js`` imports every view transitively and a module-scope ``document.head`` would
    take the shell's own tests down.
    """
    assert (STATIC / "views" / "presets.css").is_file()
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "presets.css" not in page, "index.html links a view's stylesheet"
    source = _source()
    assert 'new URL("./presets.css", import.meta.url)' in source
    assert "let stylesheetLinked = false;" in source
    assert "document.head.append" in source.split("function linkStylesheet")[1][:800]


# ------------------------------------------------------- the vocabulary and the contracts


def _declared_config_keys() -> list[str]:
    """The keys of ``CONFIG_KEYS``, in the order the view declares them."""
    source = _source()
    start = source.index("export const CONFIG_KEYS = Object.freeze({")
    block = source[start : source.index("\n});", start)]
    return re.findall(r"^  ([a-z_]+): Object\.freeze\(\{", block, re.M)


def test_the_config_vocabulary_is_the_one_the_contract_names():
    """Eight keys, read out of the contract field's own description rather than copied.

    ``config`` is free-form JSONB, so there is no schema to resolve against — the description on
    ``SearchContextCreate.config`` IS the vocabulary, and it is what a reader of the API sees. A
    key added there without a row in ``CONFIG_KEYS`` would be a key this page silently calls
    unknown, which is the class of defect the whole page is written against.
    """
    description = SearchContextCreate.model_fields["config"].description
    named = [key.strip() for key in description.split(":", 1)[1].split(",")]
    assert (
        named == _declared_config_keys()
    ), f"the contract names {named} and this view declares {_declared_config_keys()}"


def _walk_contract(model: type[BaseModel], path: str) -> object:
    current: object = model
    for key in path.split("."):
        if not (isinstance(current, type) and issubclass(current, BaseModel)):
            raise KeyError(f"{path}: {current} has no fields to look {key} up in")
        info = current.model_fields.get(key)
        if info is None:
            raise KeyError(f"{path}: no field {key!r} on {current.__name__}")
        annotation = info.annotation
        if get_origin(annotation) is Union:
            members = [a for a in get_args(annotation) if a is not type(None)]
            annotation = members[0] if len(members) == 1 else annotation
        current = annotation
    return current


def test_the_presets_view_reads_only_fields_the_contracts_carry():
    """Two contracts, every path resolved against the model that defines it.

    The check ``views/capabilities.js`` earned the hard way: three rows read field names
    ``CapabilitiesResponse`` does not carry and all three rendered as the word "none". Nothing on
    this page reaches into a response with ``.``; every read goes through ``reader(model)``, and
    ``READS`` is what is checked here.
    """
    source = _source()
    start = source.index("export const READS = Object.freeze({")
    block = source[start : source.index("\n});", start)]
    reads = {
        model: re.findall(r'"([\w.]+)"', body)
        for model, body in re.findall(r'"([\w.]+)": Object\.freeze\(\[(.*?)\]\)', block, re.S)
    }
    assert set(reads) == {"meta.CapabilitiesResponse", "search_context.SearchContextResponse"}
    for model, paths in reads.items():
        module, name = model.split(".")
        cls = getattr(CONTRACT_MODULES[module], name)
        assert paths, f"{model} declares no paths"
        for path in paths:
            _walk_contract(cls, path)  # KeyError is the failure


# -------------------------------------------------------------- the node harness fixtures


def _row(name: str, config: dict, description: str | None = None) -> dict:
    """One ``SearchContextResponse``, built by Pydantic so the fixture cannot drift."""
    when = datetime(2026, 9, 14, 11, 30, tzinfo=timezone.utc)
    return json.loads(
        SearchContextResponse(
            id=1,
            name=name,
            description=description,
            config=config,
            created_at=when,
            updated_at=when,
        ).model_dump_json()
    )


#: A config carrying every key this editor does NOT set, plus one the contract does not name.
#:
#: Four of these are the vocabulary's non-editable keys and the fifth is a key nothing reads —
#: which is the harder case, because a page that only preserved keys it had heard of would pass a
#: test built from the vocabulary alone and still lose whatever wrote ``rerank``.
RICH_CONFIG = {
    "usetype": "chunk,section",
    "limit": 5,
    "methods": ["vector", "bm25"],
    "weights": {"vector": 0.7, "bm25": 0.3},
    "index_name": "notes",
    "threshold": 0.42,
    "rerank": True,
}

PURE_HARNESS = textwrap.dedent("""
    import { install, serialize } from "./dom.mjs";
    install();
    const view = await import("%(static)s/views/presets.js");

    const config = %(config)s;
    const split = view.splitConfig(config);
    const refused = [];
    for (const [label, fields] of %(refusals)s) {
      try {
        view.mergeConfig({}, { method: "", usetype: null, parent_id: "", limit: "", ...fields });
        refused.push([label, "ACCEPTED"]);
      } catch (error) {
        refused.push([label, `${error.name}: ${error.message}`]);
      }
    }
    const names = [];
    for (const candidate of %(names)s) {
      try { names.push([candidate, view.presetName(candidate)]); }
      catch (error) { names.push([candidate, error.name]); }
    }

    // A comma inside a glob is the one usetype shape the text control cannot hold.
    const commaInAGlob = view.splitConfig({ usetype: ["obsidian:*", "a,b"] });
    const plainList = view.splitConfig({ usetype: ["obsidian:*", "chunk"] });

    console.log(JSON.stringify({
      editable: view.EDITABLE,
      fields: split.fields,
      carried: split.carried,
      // The save the form actually performs: two controls changed, two cleared.
      saved: view.mergeConfig(config, split.fields, split),
      edited: view.mergeConfig(
        config, { ...split.fields, method: "bm25", limit: "25" }, split
      ),
      cleared: view.mergeConfig(
        config, { method: "", usetype: "", parent_id: "", limit: "" }, split
      ),
      commaInAGlob: {
        field: commaInAGlob.fields.usetype,
        carried: commaInAGlob.carried.map((one) => one.key),
        // The control is blank and untouched, so a save must leave the stored value alone.
        saved: view.mergeConfig(
          { usetype: ["obsidian:*", "a,b"] },
          { method: "", usetype: null, parent_id: "", limit: "" },
          commaInAGlob
        ),
      },
      // An EDITED string filter stays a string: the glob count does not decide the shape.
      editedString: view.mergeConfig(
        { usetype: "chunk,section" },
        { method: "", usetype: "chunk, section, file", parent_id: "", limit: "" },
        { usetypeIsList: false }
      ),
      plainList: {
        field: plainList.fields.usetype,
        carried: plainList.carried.map((one) => one.key),
        saved: view.mergeConfig(
          { usetype: ["obsidian:*", "chunk"] }, plainList.fields, plainList
        ),
      },
      refused,
      names,
      runHref: view.runHref("my notes"),
      unsaved: view.unsavedFromSearch(
        { q: "late interaction", m: "vector,maxsim", x: "1", limit: "7" }
      ),
      fromSearch: view.fieldsFromSearchParams(
        { q: "late interaction", get: "bm25", usetype: "chunk", parent: "42", limit: "7" }
      ),
      atDefaults: view.fieldsFromSearchParams({ q: "late interaction" }),
      carriedPanel: serialize(view.renderCarried(split.carried)),
      emptyCarriedPanel: serialize(view.renderCarried([])),
    }));
    """)

REFUSALS = [
    ["an empty glob", {"usetype": "chunk,,section"}],
    ["a duplicate glob", {"usetype": "chunk,chunk"}],
    ["a limit above the contract's range", {"limit": "101"}],
    ["a limit below it", {"limit": "0"}],
    ["a limit that is not a number", {"limit": "ten"}],
    ["a subtree that is not a document id", {"parent_id": "0"}],
    ["a subtree that is not a number", {"parent_id": "root"}],
]

NAMES = ["", "   ", " leading", "trailing ", "with/slash", "personal-notes"]


def _pure(tmp_path, config: dict = RICH_CONFIG) -> dict:
    harness = (
        PURE_HARNESS.replace("%(config)s", json.dumps(config))
        .replace("%(refusals)s", json.dumps(REFUSALS))
        .replace("%(names)s", json.dumps(NAMES))
    )
    return _node(tmp_path, harness)


@needs_node
def test_a_key_this_editor_cannot_set_survives_a_save(tmp_path):
    """The property the whole page turns on, demonstrated value by value.

    ``config`` is free-form JSONB and this editor sets four keys of it. A preset written by
    something else — the API, a script, a later version of this page — can carry keys with no
    control here, and an editor that sent back only what its form knew about would delete them
    silently. So the merge starts from the config as it arrived and touches four names.

    The fifth key in the fixture is the one that matters most: ``rerank`` is not in the contract's
    vocabulary at all, so a page that preserved only keys it had heard of would still lose it.
    """
    result = _pure(tmp_path)

    assert result["editable"] == ["method", "usetype", "parent_id", "limit"]

    # What the form shows, and what it will not show.
    assert result["fields"] == {
        "method": "",
        "usetype": "chunk,section",
        "parent_id": "",
        "limit": "5",
    }
    assert [one["key"] for one in result["carried"]] == [
        "methods",
        "weights",
        "index_name",
        "threshold",
        "rerank",
    ], "the carried list is the vocabulary's non-editable keys in order, then the unknown ones"
    for one in result["carried"]:
        assert one["why"], f"{one['key']} is listed with no reason"
        assert one["value"] == RICH_CONFIG[one["key"]]

    # Saving with nothing changed is the identity, key for key — including the unknown one.
    assert result["saved"] == RICH_CONFIG

    # Saving with two controls changed changes exactly those two.
    edited = dict(RICH_CONFIG, method="bm25", limit=25)
    assert result["edited"] == edited

    # And CLEARING the four controls removes those four and keeps everything else. An empty
    # control removes its key rather than storing "" — `resolve_params` overrides only what the
    # request left as None, so a stored `usetype: ""` is a filter matching nothing where an absent
    # one is no filter at all.
    assert result["cleared"] == {
        "methods": ["vector", "bm25"],
        "weights": {"vector": 0.7, "bm25": 0.3},
        "index_name": "notes",
        "threshold": 0.42,
        "rerank": True,
    }


@needs_node
def test_the_carried_keys_are_listed_rather_than_hidden(tmp_path):
    """Preserving a key silently is half the job; the page has to say which ones.

    And the panel is drawn even when there is nothing in it, because "this editor preserves what
    it does not show" is a claim, and a panel that appears only when there is something to
    preserve leaves the claim unmade in the one case a reader meets first — their own new preset.
    """
    result = _pure(tmp_path)

    panel = result["carriedPanel"]
    keys = _texts(panel, "preset-key")
    assert keys == ["methods", "weights", "index_name", "threshold", "rerank"]
    values = _texts(panel, "preset-value")
    assert json.loads(values[keys.index("weights")]) == {"vector": 0.7, "bm25": 0.3}
    assert json.loads(values[keys.index("rerank")]) is True
    assert "written back exactly as it arrived" in panel["text"]
    for why in _texts(panel, "preset-why"):
        assert why.strip(), "a carried key with no reason beside it"

    empty = result["emptyCarriedPanel"]
    assert "Carried through untouched" in empty["text"]
    assert "it would be listed here and saved back exactly as it arrived" in empty["text"]


@needs_node
def test_a_usetype_the_text_box_cannot_express_is_carried_and_not_flattened(tmp_path):
    """The carry-through decision is per VALUE, not only per key.

    ``usetype_globs`` in ``jmfts-client/jmfts_client/contracts/search.py`` accepts a comma-joined
    string and a list, and it says why the list exists: it "is the only way to name a usetype that
    contains a comma". So a list whose members are comma-free means the same thing as the joined
    string and the text control can hold it — but a list with a comma INSIDE a member does not,
    and joining it would produce a different filter that still looks right.

    That value therefore leaves the editable set: the control is blank and disabled, the value is
    listed among the carried keys, and a save leaves it exactly as it was. The shape is preserved
    too — a filter that arrived as a list goes back as a list, because the two shapes are not
    interchangeable for the very value this is about.
    """
    result = _pure(tmp_path)

    unexpressible = result["commaInAGlob"]
    assert unexpressible["field"] is None, "a value the control cannot hold must not reach it"
    assert unexpressible["carried"] == ["usetype"]
    assert unexpressible["saved"] == {"usetype": ["obsidian:*", "a,b"]}

    # And the shape a filter arrived in is the shape it goes back in, whatever the glob COUNT is.
    # The two shapes mean the same thing to `usetype_globs`, so rewriting one into the other is a
    # change this page made that nobody asked for — and it shows up as a diff on a row somebody
    # else is also editing. This was a real defect: the first draft turned a one-glob string into
    # a list as soon as a second glob was typed into it.
    assert result["editedString"] == {"usetype": "chunk,section,file"}

    # The comma-free list IS expressible, and it goes back as a list rather than as a string.
    plain = result["plainList"]
    assert plain["field"] == "obsidian:*,chunk"
    assert plain["carried"] == []
    assert plain["saved"] == {"usetype": ["obsidian:*", "chunk"]}


@needs_node
def test_the_form_refuses_what_the_appliance_would_refuse(tmp_path):
    """Fail Early applies to a form: an invalid config is not submitted.

    The three usetype refusals are ``usetype_globs``'s three and carry its reason — an empty glob
    "names nothing" and is refused rather than read as "match everything". The limit range is
    ``SearchRequest.limit``'s own ``ge=1, le=100``: a preset outside it is stored happily by the
    JSONB column and refused by every search that resolves it, which is a failure arriving a long
    way from its cause.
    """
    result = _pure(tmp_path)
    for label, outcome in result["refused"]:
        assert outcome.startswith("PresetError: "), f"{label} was {outcome}"
    reasons = dict(result["refused"])
    assert "match everything" in reasons["an empty glob"]
    assert "ge=1 le=100" in reasons["a limit above the contract's range"]

    assert SearchRequest.model_fields["limit"].metadata, "SearchRequest.limit declares no bounds"
    bounds = {
        type(one).__name__.lower(): getattr(one, "ge", None) or getattr(one, "le", None)
        for one in SearchRequest.model_fields["limit"].metadata
    }
    assert set(bounds.values()) == {1, 100}, f"the page says 1..100 and the contract says {bounds}"

    # A name is a path segment of /search-contexts/{name}, and whitespace makes two rows of one.
    names = dict(result["names"])
    assert names[""] == "PresetError"
    assert names["   "] == "PresetError"
    assert names[" leading"] == "PresetError"
    assert names["trailing "] == "PresetError"
    assert names["with/slash"] == "PresetError"
    assert names["personal-notes"] == "personal-notes"


# --------------------------------------------------------------- the page, mounted

MOUNT_HARNESS = textwrap.dedent("""
    import { install, serialize } from "./dom.mjs";
    install();
    const { viewById } = await import("%(static)s/shell/registry.js");
    await import("%(static)s/views/presets.js");

    const answers = %(answers)s;
    const calls = [];
    const client = {
      operations: {},
      async call(opId, args) {
        calls.push([opId, args]);
        if (!(opId in answers)) throw new Error(`the fixture has no answer for ${opId}`);
        return answers[opId];
      },
    };

    const view = viewById("presets");
    const element = await view.component({
      client, params: %(params)s, navigate: () => {},
    });

    // The controls as a reader would find them, which `serialize` does not report: what a form
    // HOLDS is the whole of "this link prefilled the editor", and a tree walk is the only way to
    // ask a stubbed DOM for it.
    const controls = {};
    const walk = (node) => {
      if (node.tagName === "INPUT" || node.tagName === "SELECT") {
        controls[node.name] = { value: node.value, disabled: node.disabled === true };
      }
      for (const child of node.childNodes) walk(child);
    };
    walk(element);

    console.log(JSON.stringify({
      registration: { id: view.id, title: view.title, path: view.path, tags: [...view.tags] },
      calls,
      controls,
      tree: serialize(element),
    }));
    """)


def _mount(tmp_path, *, params: dict, rows: list[dict]) -> dict:
    answers = {
        "MetaService.capabilities": json.loads(_capabilities_fixture().model_dump_json()),
        "SearchContextService.list_contexts": rows,
    }
    harness = MOUNT_HARNESS.replace("%(answers)s", json.dumps(answers)).replace(
        "%(params)s", json.dumps(params)
    )
    return _node(tmp_path, harness)


@needs_node
def test_the_page_says_a_preset_is_shared_before_it_offers_a_button(tmp_path):
    """``CONFIG_OBJECT``, rendered.

    ``tests/test_expose_principal_audit.py`` records the reason all five verbs consult no
    principal: "A named configuration row, not a document. Subtree RBAC is defined over the
    document tree… there is no root above this row to consult. That leaves it shared mutable state
    any token holder may change." That is a decision and not a defect — but a button labelled
    "Save" that silently writes state shared with everybody is a surprise, and a surprise is a bug
    here. So the banner is above the form and every writing control names who it affects, which is
    the rule ``views/upload.js`` applies to its Ingest button.
    """
    rows = [_row("personal-notes", {"usetype": "chunk"}, "everything I wrote")]
    result = _mount(tmp_path, params={}, rows=rows)

    assert result["registration"] == {
        "id": "presets",
        "title": "Presets",
        "path": "/presets",
        "tags": ["retrieval"],
    }

    tree = result["tree"]
    banner = _by_class(tree, "preset-shared")
    assert len(banner) == 1, "the page draws no shared-state banner"
    said = banner[0]["text"]
    assert "every holder of this appliance's API token" in said
    assert "overwrite any of them and can delete any of them" in said
    assert "does not record who did" in said

    # Above the form, not below it: a warning a reader meets after pressing the button is a
    # warning that arrived too late. The banner is the second paragraph of the page.
    order = [child["class"] for child in tree["children"]]
    assert order.index("note bad preset-shared") < order.index("preset-panel")

    # And every control that writes says so on its face, which is the rule `views/upload.js`
    # applies to its Ingest button: the control names what it will do before it is pressed.
    def buttons(node: dict) -> list[str]:
        found = [node["text"]] if node["tag"] == "button" else []
        for child in node["children"]:
            found.extend(buttons(child))
        return found

    assert buttons(_by_class(tree, "preset-form")[0]) == [
        "Create — visible to every token holder on this appliance"
    ]
    row = buttons(_by_class(tree, "preset-actions")[0])
    assert row == [
        "Edit a copy of this into the form",
        'Delete "personal-notes"…',
    ], row
    # The first press of Delete ARMS and sends nothing; the second says what it destroys and for
    # whom. There is no undo — the row and its config are gone for everybody — and a single
    # button over state shared with every token holder is one slip away from taking somebody
    # else's preset away.
    assert "It goes for every token holder." in _source()


@needs_node
def test_the_loop_closes_every_preset_is_a_link_to_a_search_that_runs_it(tmp_path):
    """Question 4.6's point: a preset you can create and not use is a mechanism with no examples.

    So the two halves are both links and both are checked. This page puts ``?context=NAME`` on
    every row, which is the parameter ``requestFromParams`` in ``views/search.js`` reads; and that
    page puts its own fragment on a link to ``/presets``, which is what brings a search here to be
    saved. Neither of the two calls the other's operations — the coupling is a URL, which is how
    the detail hand-off between steps 27 and 28 already works.
    """
    rows = [_row("personal-notes", {"usetype": "chunk"}), _row("hardware", {"limit": 3})]
    result = _mount(tmp_path, params={}, rows=rows)

    hrefs = {node["text"]: node for node in _by_class(result["tree"], "preset-run")}
    assert len(hrefs) == 1 and "Run it →" in hrefs

    # Two rows, two run links, each carrying its own name.
    runs = _by_class(result["tree"], "preset-run")
    assert len(runs) == 2

    # The other half: the search view links here with its own fragment vocabulary, and it names
    # this view's route rather than writing a preset itself.
    search = SEARCH_VIEW.read_text(encoding="utf-8")
    assert 'export const PRESETS_PATH = "/presets";' in search
    assert "Save this search as a preset" in search
    assert "paramsFromRequest(request)" in search.split("PRESETS_PATH}?")[1][:80]
    # And still none of the three write verbs, which is step 27's own assertion from this side.
    for verb in ("create_context", "update_context", "delete_context"):
        assert f"SearchContextService.{verb}" not in search


@needs_node
def test_a_search_link_prefills_the_editor_and_says_what_a_preset_cannot_hold(tmp_path):
    """ "Save this search" carries the search's own fragment, and three of its parameters have no
    home in a config.

    Two of the three a reader would reasonably expect to be saved, which is why they are
    paragraphs on the page rather than a silent omission: ``q`` is the query, and ``m`` is which
    COLUMNS the side-by-side draws — a different thing entirely from the config key ``methods``,
    which is the list a hybrid search fuses. Conflating those two would save a fusion nobody asked
    for.
    """
    result = _pure(tmp_path)

    assert [one["param"] for one in result["unsaved"]] == ["q", "m", "x"]
    said = {one["param"]: one["what"] for one in result["unsaved"]}
    assert "not the search" in said["q"]
    assert "which columns that page draws" in said["m"]
    assert "not one of the eight keys" in said["x"]

    # The four it CAN hold, under the names the search page writes them with.
    assert result["fromSearch"] == {
        "method": "bm25",
        "usetype": "chunk",
        "parent_id": "42",
        "limit": "7",
    }
    # `views/search.js` leaves its defaults out of the URL, and a search run at those defaults
    # must therefore prefill NOTHING — writing limit 10 into a preset would save a decision
    # nobody made.
    assert result["atDefaults"] == {}

    # And the form actually holds them when the link carries them.
    mounted = _mount(
        tmp_path,
        params={"q": "late interaction", "get": "bm25", "usetype": "chunk", "limit": "7"},
        rows=[],
    )
    text = mounted["tree"]["text"]
    assert 'The link carried q="late interaction", and it is NOT saved' in text
    held = {name: control["value"] for name, control in mounted["controls"].items()}
    assert held == {
        "name": "",
        "description": "",
        "method": "bm25",
        "usetype": "chunk",
        "parent_id": "",
        "limit": "7",
    }


@needs_node
def test_a_link_onto_an_existing_preset_starts_from_that_row_and_the_search_wins(tmp_path):
    """Two sources for one form, and the precedence is stated rather than emergent.

    "Save this search as a preset" while a context is selected carries ``?context=NAME``, which is
    the row the search RAN under — so the form starts from that row, and the search's own
    parameters are then laid over it. The point of the link is to save THIS search, so it wins
    where it says anything; where it says nothing the row's value stands, and the keys the editor
    cannot set stand whatever either of them says.
    """
    rows = [_row("tuned", RICH_CONFIG, "somebody tuned this")]
    mounted = _mount(
        tmp_path, params={"context": "tuned", "q": "anything", "get": "bm25"}, rows=rows
    )

    held = {name: control["value"] for name, control in mounted["controls"].items()}
    assert held == {
        "name": "tuned",  # the row the link named
        "description": "somebody tuned this",  # from the row
        "method": "bm25",  # from the search: it says something, so it wins
        "usetype": "chunk,section",  # from the row: the search said nothing
        "parent_id": "",
        "limit": "5",  # from the row, and the search was at its default
    }
    # And the four keys no control here can set are listed, not lost.
    assert _texts(mounted["tree"], "preset-key") == [
        "methods",
        "weights",
        "index_name",
        "threshold",
        "rerank",
    ]


@needs_node
def test_a_control_that_cannot_hold_its_value_is_disabled_rather_than_wrong(tmp_path):
    """A text box showing "a,b" over a value that is not "a,b" is a control that lies.

    So the control is blank and disabled, it carries the reason in its title, and the value is in
    the carried list below it verbatim. The alternative — flattening the list into the box — would
    look right and save a different filter.
    """
    rows = [_row("unexpressible", {"usetype": ["obsidian:*", "a,b"]})]
    mounted = _mount(tmp_path, params={"name": "unexpressible"}, rows=rows)

    assert mounted["controls"]["usetype"] == {"value": "", "disabled": True}
    assert mounted["controls"]["limit"] == {"value": "", "disabled": False}
    assert _texts(mounted["tree"], "preset-key") == ["usetype"]


# ------------------------------------------------------------- a 409 is an answer

CONFLICT_HARNESS = textwrap.dedent("""
    import { install, serialize } from "./dom.mjs";
    install();
    const view = await import("%(static)s/views/presets.js");

    const existing = %(existing)s;
    const split = view.splitConfig(existing.config);
    const willWrite = view.mergeConfig(
      existing.config, { ...split.fields, limit: "25" }, split
    );
    console.log(JSON.stringify({
      willWrite,
      panel: serialize(view.renderConflict(existing, {
        willWrite,
        description: "mine",
        onOverwrite: () => {},
      })),
    }));
    """)


@needs_node
def test_a_taken_name_is_an_answer_and_the_overwrite_is_a_separate_act(tmp_path):
    """A duplicate name renders as what that name holds, not as a red failure.

    And the overwrite is composed against the config the appliance answered with, not against the
    one the form started from — the row may have been written by somebody else while this page was
    open, and it is the fresh one that is about to be replaced. The panel shows both what it holds
    and what would be written, so the keys this editor does not set can be seen surviving rather
    than promised.
    """
    existing = _row("personal-notes", RICH_CONFIG, "somebody else's")
    harness = CONFLICT_HARNESS.replace("%(existing)s", json.dumps(existing))
    result = _node(tmp_path, harness)

    # The overwrite keeps every key this editor does not set, and changes the one that was edited.
    assert result["willWrite"] == dict(RICH_CONFIG, limit=25)

    panel = result["panel"]
    said = panel["text"]
    assert 'A preset called "personal-notes" already exists' in said
    assert "Nothing was written" in said
    assert "read back just now rather than from the list above" in said
    assert "somebody else's" in said
    assert "Kept from the existing preset, unchanged" in said
    for kept in ("methods", "weights", "index_name", "threshold", "rerank"):
        assert kept in said.split("Kept from the existing preset, unchanged:")[1]

    # The overwrite is one button, it is the destructive one, and it says for whom.
    buttons = [node for node in panel["children"] if node["tag"] == "button"]
    assert len(buttons) == 1
    assert buttons[0]["text"] == 'Overwrite "personal-notes" for every token holder (PUT)'
    assert buttons[0]["class"] == "bad"


# ------------------------------------------------ the one claim a reader would not guess


def test_a_saved_limit_reaches_a_get_search_and_never_a_post():
    """``LIMIT_IS_ALMOST_ALWAYS_OVERRIDDEN``, checked against the real resolver.

    ``SearchContextRepository.resolve_params`` applies every override whose value is not None over
    the stored config. ``SearchRequest.limit`` is ``Field(default=10, ge=1, le=100)`` — an int
    that is never None — and every POST search passes ``"limit": request.limit`` into that
    override map, so a preset's ``limit`` never survives a POST. ``quick_search`` is the exception
    and it is deliberate: it passes ``limit if limit != 10 else None``, so the preset's value is
    used exactly when the caller did not ask for one.

    Run against the function itself rather than asserted in prose, with the row supplied directly:
    what is under test is the override arithmetic, and a database would only be storing the dict
    this test already has.
    """
    repo = SearchContextRepository.__new__(SearchContextRepository)
    repo.get_by_name = lambda name: SimpleNamespace(config={"limit": 3, "usetype": "chunk"})

    # The POST shape: `limit` is always present, so the preset's value is always replaced.
    assert repo.resolve_params("preset", {"limit": 10, "usetype": None})["limit"] == 10
    assert repo.resolve_params("preset", {"limit": 10, "usetype": None})["usetype"] == "chunk"

    # The GET shape: `quick_search` sends None when the caller left the default alone.
    assert repo.resolve_params("preset", {"limit": None, "usetype": None})["limit"] == 3

    # And the two call sites are what this claims they are.
    post = inspect.getsource(SearchService.hybrid_search)
    assert '"limit": request.limit' in post, "a POST search no longer always overrides the limit"
    quick = inspect.getsource(SearchService.quick_search)
    assert '"limit": limit if limit != 10 else None' in quick

    # The page says so where a reader will meet it, rather than only here.
    source = _source()
    assert "export const LIMIT_IS_ALMOST_ALWAYS_OVERRIDDEN" in source
    assert "LIMIT_IS_ALMOST_ALWAYS_OVERRIDDEN)" in source, "the note is declared and never drawn"
