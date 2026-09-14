"""The search view: seven methods side by side, and the anchor it must not drop.

``docs/SPRINT_0_6_0.md`` Block F step 27. Four properties are pinned here, and each one is a
sentence from the plan turned into something that can go red:

* **"the seven methods side by side"** — the count is not a number this page chose. It is every
  ``/search/`` operation that answers with a ranking, read back out of the GENERATED operation
  table, so an operation added to the search surface fails this file until the view draws it.
* **"a result count of zero with a method named is a fact worth rendering"** — a column that
  returned nothing renders its count, its applied block and, where ``/capabilities`` explains
  it, the reason. Asserted on the rendered tree rather than on the absence of an exception.
* **IC-4's anchor is carried, not discarded** — a hit with an anchor, a hit with an unresolved
  row and a hit with neither are three states, they render differently, and the link each one
  offers the detail view carries exactly what that hit had.
* **the page reads fields its contracts carry** — the same check ``views/capabilities.js``
  earned the hard way, applied to the five contracts this page reads. Every path in ``READS``
  resolves against the Pydantic model named on its left.

The harness is ``tests/test_web_views.py``'s: one DOM stub, one ``_node`` runner, one
``CapabilitiesResponse`` fixture built by Pydantic rather than typed by hand. Borrowed rather
than copied, because a second stub is a second thing to keep level with the bundle, and the
capability fixture in particular exists BECAUSE a hand-typed one reproduces the defect it is
testing for.
"""

from __future__ import annotations

import json
import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Union, get_args, get_origin

from pydantic import BaseModel

import jmfts_client.contracts.document as contracts_document
import jmfts_client.contracts.meta as contracts_meta
import jmfts_client.contracts.search as contracts_search
import jmfts_client.contracts.search_context as contracts_search_context
from jmfts_client.contracts.document import DocumentResponse
from jmfts_client.contracts.search import (
    SEARCH_METHODS,
    AppliedFilters,
    AutoSearchResponse,
    RoutingMetadata,
    SearchResponse,
    SearchResultItem,
    SourceReference,
    SynthesizeResponse,
)
from jmfts_client.contracts.search_context import SearchContextResponse
from tests.test_ts_client_codegen import operation_table
from tests.test_web_views import (  # the one harness this repository has for browser code
    _capabilities_fixture,
    _find,
    _node,
    needs_node,
)

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "jmfts-web" / "jmfts_web" / "static"
VIEW = STATIC / "views" / "search.js"
MANIFEST = STATIC / "shell" / "manifest.js"

CONTRACT_MODULES = {
    "meta": contracts_meta,
    "search": contracts_search,
    "search_context": contracts_search_context,
    "document": contracts_document,
}


def _source() -> str:
    return VIEW.read_text(encoding="utf-8")


def _declared(name: str) -> list[str]:
    """One ``export const NAME = Object.freeze([...])`` list of strings, read out of the view."""
    source = _source()
    opener = f"export const {name} = Object.freeze(["
    start = source.index(opener) + len(opener)
    return re.findall(r'"([^"]+)"', source[start : source.index("]);", start)])


# ------------------------------------------------------- the seven, counted from the surface


def _search_operations() -> dict[str, dict]:
    """Every mounted operation under ``/search/``, from the generated table."""
    return {
        op_id: op
        for op_id, op in operation_table().items()
        if op["path"].startswith("/search/") or op["path"] == "/search/"
    }


def test_the_side_by_side_draws_every_search_operation_that_returns_a_ranking():
    """Seven is a reading of the route table, not a number this page picked.

    ``METHODS`` is checked in both directions against the generated ``client/operations.js``: a
    ``/search/`` operation with no column fails here, and a column naming an operation that does
    not exist fails here too. The one operation that is deliberately not a column is
    ``synthesize``, which answers with prose rather than a ranking — so it is named as the
    panel's operation instead, and the two together must account for the whole surface.
    """
    source = _source()
    columns = re.findall(r'op_id: "([^"]+)"', source)
    synthesis = re.search(r'export const SYNTHESIS_OP = "([^"]+)"', source).group(1)

    mounted = set(_search_operations())
    assert len(columns) == 7, f"the plan says seven columns; this view draws {len(columns)}"
    assert set(columns) | {synthesis} == mounted, (
        f"columns {sorted(columns)} plus {synthesis} against the mounted search surface "
        f"{sorted(mounted)}. An operation added under /search/ needs a column or a reason."
    )
    assert len(set(columns)) == 7, "a method is drawn twice"


def test_the_view_calls_the_search_contexts_it_reads_and_none_of_the_ones_that_write():
    """Step 27 composes ``/search-contexts/*``, and composing a preset is reading one.

    ``list_contexts`` returns whole rows, ``config`` included, so calling ``get_context`` for the
    selected preset would fetch a row the page is already holding — a call whose answer it has.
    The three write verbs are absent on purpose: a preset editor is CRUD with a surface of its
    own, and ``docs/SPRINT_0_6_0.md`` Part 3 has the preset question open on 4.6. This asserts
    the absence rather than leaving it to be noticed, because "the view does not do that" is a
    decision and decisions are what go stale.
    """
    source = _source()
    context_op = re.search(r'export const CONTEXT_OP = "([^"]+)"', source).group(1)
    assert context_op == "SearchContextService.list_contexts"

    contexts = {op_id for op_id in operation_table() if op_id.startswith("SearchContextService.")}
    writes = contexts - {context_op, "SearchContextService.get_context"}
    assert writes, "the search-context surface has no write verbs; this test is about nothing"
    for op_id in writes | {"SearchContextService.get_context"}:
        assert op_id not in source, f"{op_id} is called from the search view"


def test_the_get_column_runs_only_what_the_appliance_accepts():
    """``GET /search/`` takes the four retrieval methods and their fusion, and no other name.

    ``quick_search`` used to fall through its ``elif`` ladder and run a hybrid search for an
    unrecognised name; it now raises. The select this page draws is therefore pinned to
    ``SEARCH_METHODS`` plus ``hybrid`` rather than restated, so a fifth retrieval method reaches
    the control by moving the contract.
    """
    assert set(_declared("GET_METHODS")) == {"hybrid", *SEARCH_METHODS}

    retrieval = re.findall(r'id: "([a-z0-9]+)",\n    kind: "retrieval"', _source())
    assert retrieval == list(SEARCH_METHODS), (
        f"the retrieval columns are {retrieval} and SEARCH_METHODS is {list(SEARCH_METHODS)}; "
        "they are the same four and this page must not have its own list of them."
    )


def test_the_manifest_imports_the_search_view():
    """A view is a file plus one appended line, and this is the line."""
    assert 'import "../views/search.js";' in MANIFEST.read_text(encoding="utf-8")


def _stylesheet_href() -> str:
    """Where the view's own stylesheet resolves to, as a file URL under node."""
    return (STATIC / "views" / "search.css").as_uri()


def test_the_view_brings_its_own_stylesheet_and_index_html_is_untouched():
    """``shell/shell.css``'s rule, and the reason it is a rule.

    A second ``<link>`` in ``index.html`` is a second edit to the one file all five F3 worktrees
    would otherwise share, which is the conflict IC-7 exists to remove. So the sheet is a file
    beside the view and the view's own module puts it on the page. ``views/log.js`` established
    the shape; this is the same three lines.
    """
    assert (STATIC / "views" / "search.css").is_file()
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "search.css" not in page, "index.html links a view's stylesheet"
    assert 'new URL("./search.css", import.meta.url)' in _source()


# ---------------------------------------------------------- the fields the contracts carry


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


def _reads() -> dict[str, list[str]]:
    """``READS`` as ``{"module.Class": [path, ...]}``."""
    source = _source()
    start = source.index("export const READS = Object.freeze({")
    block = source[start : source.index("\n});", start)]
    out: dict[str, list[str]] = {}
    for model, body in re.findall(r'"([\w.]+)": Object\.freeze\(\[(.*?)\]\)', block, re.S):
        out[model] = re.findall(r'"([\w.]+)"', body)
    return out


def test_the_search_view_reads_only_fields_the_contracts_carry():
    """Five contracts, every path resolved against the model that defines it.

    This is the check whose absence let the capability table read three field names that do not
    exist and render all three as the word "none". The failure mode is the same on this page and
    worse: a search result that quietly shows nothing for ``source_anchor`` is a citation feature
    that looks switched off. So the page reaches into no response with ``.`` — every read goes
    through ``reader(model)``, and ``READS`` is what is checked here.
    """
    reads = _reads()
    assert set(reads) == {
        "meta.CapabilitiesResponse",
        "search_context.SearchContextResponse",
        "search.SearchResponse",
        "search.AutoSearchResponse",
        "search.SearchResultItem",
        "document.DocumentResponse",
        "search.SynthesizeResponse",
        "search.SourceReference",
    }, sorted(reads)

    for model, paths in reads.items():
        module, name = model.split(".")
        cls = getattr(CONTRACT_MODULES[module], name)
        assert paths, f"{model} declares no paths"
        for path in paths:
            _walk_contract(cls, path)  # KeyError is the failure

    # And nothing bypasses the reader. Comment lines go first: this module's prose names the
    # response fields it is about, and a rule written against prose is a rule about prose.
    code = "\n".join(
        line for line in _source().splitlines() if not line.lstrip().startswith(("//", "*", "/*"))
    )
    bypass = re.findall(r"\b(?:hit|response|cap|capabilities)\.[a-z_]+", code)
    assert not bypass, f"the view reaches into a response directly: {bypass}"


# ------------------------------------------------------------------------------- fixtures


def _doc(
    id_: int, title: str | None, usetype: str, content: str | None, **extra
) -> DocumentResponse:
    """A ``DocumentResponse`` built by Pydantic, so the fixture cannot drift from the contract."""
    when = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    return DocumentResponse(
        id=id_,
        parent_id=None,
        title=title,
        content=content,
        structured_content={},
        path=[],
        depth=0,
        usetype=usetype,
        created_at=when,
        updated_at=when,
        content_hash=None,
        **extra,
    )


PDF_ANCHOR = {"kind": "pdf", "page": 3, "bbox": [72.0, 118.4, 540.0, 262.9]}
UNRESOLVED = {"code": "no_source_span", "reason": "the chunk predates source spans"}


def _hits() -> list[SearchResultItem]:
    """Three hits in the three anchor states IC-4 can produce, plus a workbook record."""
    return [
        SearchResultItem(
            document=_doc(11, "Q3 board pack", "chunk", "late interaction over stored tokens"),
            score=0.913,
            method="vector",
            source_anchor=PDF_ANCHOR,
        ),
        SearchResultItem(
            document=_doc(12, "An older chunk", "chunk", "no span was ever recorded here"),
            score=0.712,
            method="bm25",
            source_anchor_unresolved=UNRESOLVED,
        ),
        SearchResultItem(
            document=_doc(13, None, "record", "row 4 of the pipeline sheet", produced_by="sheets"),
            score=0.501,
            method="bm25",
        ),
    ]


LONG = (
    "A container node's content is filled by projection at read time, which on a real corpus is "
    "usually the best retrieval target there is. " * 6
)


def _long_hit() -> SearchResultItem:
    """One hit whose content is longer than a column draws, for the disclosure that follows."""
    return SearchResultItem(
        document=_doc(21, "A long container", "section", LONG, content_source="effective"),
        score=0.88,
        method="fulltext",
    )


def _response(results: list[SearchResultItem], **applied) -> SearchResponse:
    return SearchResponse(
        results=results,
        total=len(results),
        latency_ms=12.5,
        applied=AppliedFilters(
            limit=10, exclude_types=["entity", "entities", "summary"], **applied
        ),
    )


def _fixture(*, params: dict, answers: dict, capabilities=None) -> str:
    """The whole harness input: the operation table this page may call, and what each answers."""
    operations = {
        op_id: {"op_id": op_id}
        for op_id in [
            *_search_operations(),
            "MetaService.capabilities",
            "SearchContextService.list_contexts",
        ]
    }
    payload = {
        "params": params,
        "operations": operations,
        "answers": {
            "MetaService.capabilities": json.loads(
                (capabilities or _capabilities_fixture()).model_dump_json()
            ),
            "SearchContextService.list_contexts": [
                json.loads(
                    SearchContextResponse(
                        id=1,
                        name="my-notes",
                        description="everything I said",
                        config={"usetype": "transcript:*,obsidian:*", "limit": 5},
                        created_at=None,
                        updated_at=None,
                    ).model_dump_json()
                )
            ],
            **answers,
        },
    }
    return json.dumps(payload)


HARNESS = textwrap.dedent("""
    import { install, serialize, document as dom } from "./dom.mjs";
    install();
    // The stylesheet this view links at mount lands on `dom.head`, which the SHARED stub now
    // models — the second view that linked a sheet arrived (the call log, step 30) and this
    // harness's line 4 said that was when `head` belonged in DOM_STUB. It does; the local
    // `dom.head = new Element("head")` that used to stand here is gone.
    // Imported for its side effect: the module registers itself, exactly as `manifest.js` does.
    await import("%(static)s/views/search.js");
    const { viewById } = await import("%(static)s/shell/registry.js");

    const FIXTURE = %(fixture)s;
    const calls = [];
    const navigated = [];
    const client = {
      operations: FIXTURE.operations,
      call: async (opId, args) => {
        calls.push([opId, args]);
        const answer = FIXTURE.answers[opId];
        if (answer === undefined) throw new Error(`the fixture has no answer for ${opId}`);
        if (answer && answer.__error) throw new Error(answer.__error);
        return answer;
      },
    };

    const registration = viewById("search");
    const element = await registration.component({
      client, params: FIXTURE.params, navigate: (path, params) => navigated.push([path, params]),
    });

    // The DOM stub's `serialize` reports what a rendering assertion needs and `href` is not in
    // it — it is a property on the element rather than an attribute it was given. This walk
    // collects the links in document order, which is what the hand-off to the detail view has
    // to be read out of.
    const links = [];
    const walk = (node) => {
      if (node.tagName === "A") links.push({ href: node.href, text: node.textContent });
      for (const child of node.childNodes) walk(child);
    };
    walk(element);

    console.log(JSON.stringify({
      registration: {
        id: registration.id, title: registration.title, path: registration.path,
        tags: [...registration.tags],
      },
      calls, navigated, links, tree: serialize(element),
      stylesheets: dom.head.childNodes.map((node) => ({ rel: node.rel, href: node.href })),
    }));
    """)


def _render(tmp_path, *, params, answers, capabilities=None) -> dict:
    return _node(
        tmp_path,
        HARNESS.replace(
            "%(fixture)s", _fixture(params=params, answers=answers, capabilities=capabilities)
        ),
    )


def _columns(tree: dict) -> dict[str, dict]:
    """The side-by-side as ``{column heading: the column}``, in the order it was drawn."""
    return {column["children"][0]["text"]: column for column in _find(tree, "search-column")}


# ------------------------------------------------------------------ the page, end to end


@needs_node
def test_seven_columns_are_drawn_for_one_query_and_each_says_what_it_ran(tmp_path):
    """The demonstration, rendered: one query, seven calls, seven columns that do not move.

    The plan calls this "the shortest explanation of what this appliance is that anybody has
    written", so the assertions are about the layout being the answer — every selected method
    gets a column, the columns are in the declared order whatever order the calls came back in,
    and each column names the operation behind it so the call log and the page can be read
    against each other.
    """
    answers = {
        "SearchService.vector_search": json.loads(_response(_hits()).model_dump_json()),
        "SearchService.fulltext_search": json.loads(_response([_long_hit()]).model_dump_json()),
        "SearchService.bm25_search": json.loads(_response(_hits()[1:]).model_dump_json()),
        # MaxSim returns nothing, and the capability fixture says this appliance has no token
        # vectors — which is the pair that turns a blank column into an explained one.
        "SearchService.maxsim_search": json.loads(_response([]).model_dump_json()),
        "SearchService.hybrid_search": json.loads(
            _response(
                _hits(),
                methods=["vector", "bm25"],
                weights={"vector": 0.86, "bm25": 0.14},
                truncated=True,
            ).model_dump_json()
        ),
        "SearchService.auto_search": json.loads(
            AutoSearchResponse(
                results=_hits()[:1],
                total=1,
                latency_ms=4.0,
                routing=RoutingMetadata(
                    method="bm25", reason="the query looks lexical", signals={"quoted": True}
                ),
                applied=AppliedFilters(limit=10, exclude_types=["entity"]),
            ).model_dump_json()
        ),
        # One method fails. The other six must still be on the page.
        "SearchService.quick_search": {"__error": "GET /search/ — HTTP 503: no index named x"},
    }
    result = _render(tmp_path, params={"q": "late interaction"}, answers=answers)

    assert result["registration"] == {
        "id": "search",
        "title": "Search",
        "path": "/search",
        "tags": ["retrieval"],
    }

    # Two preflight calls, then the seven, and nothing else. The preflights are what let a zero
    # be explained and a context be named; everything after them is one method each.
    assert [op for op, _ in result["calls"]][:2] == [
        "MetaService.capabilities",
        "SearchContextService.list_contexts",
    ]
    assert sorted(op for op, _ in result["calls"][2:]) == sorted(
        _search_operations().keys() - {"SearchService.synthesize_search"}
    )

    columns = _columns(result["tree"])
    assert list(columns) == [
        "vector",
        "fulltext",
        "bm25",
        "maxsim",
        "hybrid",
        "auto",
        "quick (GET)",
    ]
    assert "POST /search/vector" in columns["vector"]["text"]
    assert "GET /search/" in columns["quick (GET)"]["text"]

    # What each column ran and what it returned.
    assert "3 results in 12.5 ms" in columns["vector"]["text"]
    assert "1 result in 12.5 ms" in columns["fulltext"]["text"], "one result is not '1 results'"
    assert "held out: entity, entities, summary" in columns["vector"]["text"]
    assert "fused vector×0.86 + bm25×0.14" in columns["hybrid"]["text"]
    assert "SHORT PAGE" in columns["hybrid"]["text"], "an ANN scan that came back short says so"
    assert "routed to bm25: the query looks lexical" in columns["auto"]["text"]

    # A column that failed keeps its place and shows what failed, with the error's CLASS on the
    # front — never the object, which `JSON.stringify` flattens to `{}`. Its six neighbours are
    # untouched, which is the whole reason the seven are run with allSettled.
    assert "Error: GET /search/ — HTTP 503" in columns["quick (GET)"]["text"]
    assert "3 results" in columns["vector"]["text"]

    # A hit whose text was shortened says how much of it is on screen. The cut is in the
    # DRAWING and never in the data: `1 result` is still `1 result`, and a reader can tell a
    # short document from a shortened one.
    assert f"showing the first 180 of {len(LONG)} characters" in columns["fulltext"]["text"]
    assert "effective content" in columns["fulltext"]["text"]

    # The view's stylesheet is linked by the view's own module, not by index.html.
    assert result["stylesheets"] == [{"rel": "stylesheet", "href": _stylesheet_href()}]


@needs_node
def test_a_method_that_returned_nothing_says_so_and_says_why(tmp_path):
    """Zero is an answer, and this appliance can explain two of the ways to get one.

    The corpus fixture holds 1284 documents, 1200 with vectors and NONE with token vectors. So
    an empty ``maxsim`` column is not a fact about the query at all — it is a fact about the
    appliance, and reading it off ``/capabilities?corpus=true`` is what lets the page say which
    of the two a reader is looking at. A blank area under a method name says neither.
    """
    empty = json.loads(_response([]).model_dump_json())
    answers = {op_id: empty for op_id in _search_operations()}
    answers.pop("SearchService.synthesize_search")
    answers["SearchService.auto_search"] = json.loads(
        AutoSearchResponse(
            results=[],
            total=0,
            latency_ms=1.0,
            routing=RoutingMetadata(method="vector", reason="no lexical signal", signals={}),
            applied=AppliedFilters(limit=10),
        ).model_dump_json()
    )
    result = _render(tmp_path, params={"q": "nothing matches this"}, answers=answers)

    columns = _columns(result["tree"])
    assert "0 results" in columns["maxsim"]["text"]
    assert "no document here has token vectors" in columns["maxsim"]["text"]
    # A method the corpus cannot explain away says the plain thing rather than inventing one.
    assert "nothing matched this query" in columns["fulltext"]["text"]
    assert "no document here has" not in columns["fulltext"]["text"]

    # And the page says what is held out of every result set before a reader concludes the
    # corpus is empty.
    page = result["tree"]["text"]
    assert "Held out of every result set" in page
    assert "entity, entities, summary, derived" in page


@needs_node
def test_a_hit_carries_its_anchor_to_the_detail_view_and_says_which_state_it_is_in(tmp_path):
    """IC-4, through the page and out the other side.

    Step 31 draws the box; this view's job is to not lose the thing it draws it from. Three hits,
    three states — an anchor, an unresolved row, and neither — and the link each one offers must
    carry exactly what that hit had. A hit with no anchor must not gain one, and a hit whose
    rectangle could not be recovered must carry the reason rather than arriving at the detail
    view indistinguishable from a hit nothing ever tried.
    """
    answers = {
        op_id: json.loads(_response(_hits()).model_dump_json()) for op_id in _search_operations()
    }
    answers.pop("SearchService.synthesize_search")
    answers["SearchService.auto_search"] = json.loads(
        AutoSearchResponse(
            results=_hits(),
            total=3,
            latency_ms=1.0,
            routing=RoutingMetadata(method="bm25", reason="lexical", signals={}),
            applied=AppliedFilters(limit=10),
        ).model_dump_json()
    )
    result = _render(tmp_path, params={"q": "anchors", "m": "vector"}, answers=answers)

    column = _columns(result["tree"])["vector"]
    hits = _find(column, "search-hit")
    assert len(hits) == 3

    def badge_classes(hit):
        return [
            child["class"]
            for child in hit["children"][1]["children"]
            if child["class"].startswith("search-badge")
        ]

    assert "search-badge anchored" in badge_classes(hits[0])
    assert "0-based page 3" in hits[0]["text"]
    assert "search-badge unresolved" in badge_classes(hits[1])
    assert "no_source_span" in hits[1]["text"]
    assert "search-badge none" in badge_classes(hits[2])

    # The workbook record that BM25 can now reach renders as what it is, and the rule that
    # wrote it is on the hit. Step 7 of this sprint is what put it in a result set at all.
    assert "record" in hits[2]["text"]
    assert "sheets" in hits[2]["text"]

    # And the hand-off. The anchored hit's link carries the anchor verbatim; the unresolved
    # hit's carries the reason; the third carries neither and gains nothing.
    hrefs = [link["href"] for link in result["links"]]
    assert len(hrefs) == 3, "one link per hit, and nothing else on this page links anywhere"
    assert hrefs[0].startswith("#/document?id=11&anchor=")
    assert json.loads(_query(hrefs[0])["anchor"]) == PDF_ANCHOR
    assert json.loads(_query(hrefs[1])["unresolved"]) == UNRESOLVED
    assert "anchor" not in _query(hrefs[1])
    assert _query(hrefs[2]) == {"id": "13"}


def _query(href: str) -> dict[str, str]:
    from urllib.parse import parse_qs, urlparse

    return {k: v[0] for k, v in parse_qs(urlparse(href.lstrip("#")).query).items()}


ANCHOR_HARNESS = textwrap.dedent("""
    import { install } from "./dom.mjs";
    install();
    const { anchorState, readDetailParams, detailParams, ANCHOR_STATE, SearchRequestError } =
      await import("%(static)s/views/search.js");

    const hit = (anchor, unresolved) => ({
      document: { id: 1, title: "t", usetype: "chunk", content: "c",
                  content_source: "stored", produced_by: null },
      score: 1, method: "vector",
      source_anchor: anchor, source_anchor_unresolved: unresolved,
    });
    const ANCHORS = %(anchors)s;

    const out = { states: {}, vocabulary: ANCHOR_STATE, roundTrip: {}, refusals: {} };
    const state = (label, anchor, unresolved) => {
      const answer = anchorState(hit(anchor, unresolved));
      out.states[label] = { state: answer.state, where: answer.where, detail: answer.detail };
    };
    state("anchored", ANCHORS.pdf, null);
    state("cells", ANCHORS.cells, null);
    state("span", ANCHORS.span, null);
    state("unresolved", null, ANCHORS.unresolved);
    state("none", null, null);
    state("contradictory", ANCHORS.pdf, ANCHORS.unresolved);
    state("unknown kind", { kind: "waveform" }, null);
    state("malformed pdf", { kind: "pdf", bbox: [1, 2, 3, 4] }, null);

    out.roundTrip.anchored = readDetailParams(detailParams(hit(ANCHORS.pdf, null)));
    out.roundTrip.bare = readDetailParams(detailParams(hit(null, null)));

    const refuse = (label, fn) => {
      try { fn(); out.refusals[label] = null; }
      catch (error) {
        out.refusals[label] = [
          error instanceof SearchRequestError ? "SearchRequestError" : error.name, error.message,
        ];
      }
    };
    refuse("torn json", () => readDetailParams({ id: "4", anchor: '{"kind":' }));
    refuse("no id", () => readDetailParams({ anchor: "null" }));
    console.log(JSON.stringify(out));
    """)


@needs_node
def test_the_three_anchor_absences_are_three_different_answers(tmp_path):
    """ "No anchor" and "no anchor could be recovered" are not the same fact.

    ``jmfts_core/evidence.py:453`` says the unresolved row is present exactly when the anchor is
    not, so a page that drew nothing for both would be hiding the second — the same rule
    ``render/overlay.js`` is built on, and the reason this view imports its two absence words
    rather than spelling them again. Both rows at once is a contradiction the appliance should
    not be able to produce; it is SHOWN in the bad colour rather than thrown, because a hit with
    a broken evidence row is still a hit and the six beside it are fine.
    """
    anchors = json.dumps(
        {
            "pdf": PDF_ANCHOR,
            "cells": {"kind": "cells", "sheet": "Q3 Pipeline", "ref": "B4:H120"},
            "span": {"kind": "span", "char_start": 100, "char_end": 240},
            "unresolved": UNRESOLVED,
        }
    )
    result = _node(tmp_path, ANCHOR_HARNESS.replace("%(anchors)s", anchors))

    # The two words IC-9 owns are the same two words here, because they were imported.
    assert result["vocabulary"]["NONE"] == "none"
    assert result["vocabulary"]["UNRESOLVED"] == "unresolved"

    states = result["states"]
    assert states["anchored"]["state"] == "anchored"
    assert states["anchored"]["where"] == "0-based page 3"
    assert states["cells"]["where"] == "Q3 Pipeline!B4:H120"
    assert states["span"]["where"] == "chars 100–240"
    assert states["unresolved"]["state"] == "unresolved"
    assert (
        states["unresolved"]["detail"] == UNRESOLVED["reason"]
    ), "the reason is carried, not re-written"
    assert states["none"]["state"] == "none"
    assert states["contradictory"]["state"] == "contradictory"
    assert "453" in states["contradictory"]["detail"]
    assert states["unknown kind"]["state"] == "unreadable"
    assert "older than the appliance" in states["unknown kind"]["detail"]
    assert states["malformed pdf"]["state"] == "unreadable"

    # The hand-off round-trips, and BOTH keys come back even when both are null — the shape
    # `highlight()` refuses to be called without.
    assert result["roundTrip"]["anchored"] == {
        "documentId": 1,
        "anchor": PDF_ANCHOR,
        "unresolved": None,
    }
    assert result["roundTrip"]["bare"] == {"documentId": 1, "anchor": None, "unresolved": None}

    # A torn fragment is not "no anchor". Reading it as one is how a highlight silently stops
    # appearing on a link somebody edited by hand.
    assert result["refusals"]["torn json"][0] == "SearchRequestError"
    assert result["refusals"]["no id"][0] == "SearchRequestError"


REQUEST_HARNESS = textwrap.dedent("""
    import { install } from "./dom.mjs";
    install();
    const { requestFromParams, paramsFromRequest, SearchRequestError } =
      await import("%(static)s/views/search.js");

    const out = { parsed: {}, roundTrip: {}, refusals: {} };
    out.parsed.bare = requestFromParams({ q: "hello" });
    out.parsed.full = requestFromParams({
      q: " spaced ", limit: "25", usetype: "transcript:*,obsidian:*", parent: "17",
      context: "my-notes", m: "vector,maxsim", get: "bm25", x: "1",
    });
    out.roundTrip.bare = paramsFromRequest(out.parsed.bare);
    out.roundTrip.full = paramsFromRequest(out.parsed.full);
    out.roundTrip.again = requestFromParams(out.roundTrip.full);

    const refuse = (label, params) => {
      try { requestFromParams(params); out.refusals[label] = null; }
      catch (error) {
        out.refusals[label] = [
          error instanceof SearchRequestError ? "SearchRequestError" : error.name, error.message,
        ];
      }
    };
    refuse("unknown method", { q: "x", m: "vetcor" });
    refuse("duplicate method", { q: "x", m: "bm25,bm25" });
    refuse("limit over the contract", { q: "x", limit: "500" });
    refuse("limit not a number", { q: "x", limit: "ten" });
    refuse("get method the route refuses", { q: "x", get: "auto" });
    console.log(JSON.stringify(out));
    """)


@needs_node
def test_a_search_is_a_url_and_one_it_cannot_run_is_refused(tmp_path):
    """The fragment IS the search, so it round-trips and it is validated like a request.

    The form does not run a search; it navigates, and the mount runs what the fragment says. That
    is what makes ``#/search?q=…&m=vector,maxsim`` a link somebody can send. It also means the
    query string is untrusted input, and the three refusals here are the three
    ``validate_search_methods`` makes in the contract — unknown, duplicate, empty — for its
    reason: an unrecognised name "contributes nothing and raises nothing", so the reader gets a
    side-by-side with a column missing and no way to tell that from a column that came back
    empty.
    """
    result = _node(tmp_path, REQUEST_HARNESS)

    assert result["parsed"]["bare"]["limit"] == 10
    assert result["parsed"]["bare"]["methods"] == [
        "vector",
        "fulltext",
        "bm25",
        "maxsim",
        "hybrid",
        "auto",
        "quick",
    ], "an unspecified method list is all seven, which is what the page is for"
    assert result["parsed"]["bare"]["getMethod"] == "hybrid"
    assert result["parsed"]["full"]["query"] == "spaced"
    assert result["parsed"]["full"]["parentId"] == 17
    assert result["parsed"]["full"]["includeExcluded"] is True

    # Defaults stay out of the URL, and everything else survives the round trip.
    assert result["roundTrip"]["bare"] == {"q": "hello"}
    assert result["roundTrip"]["full"] == {
        "q": "spaced",
        "limit": "25",
        "usetype": "transcript:*,obsidian:*",
        "parent": "17",
        "context": "my-notes",
        "m": "vector,maxsim",
        "get": "bm25",
        "x": "1",
    }
    assert result["roundTrip"]["again"] == result["parsed"]["full"]

    for label, refusal in result["refusals"].items():
        assert refusal, f"{label} was accepted"
        assert refusal[0] == "SearchRequestError", (label, refusal)
    assert "vetcor" in result["refusals"]["unknown method"][1]
    assert "1..100" in result["refusals"]["limit over the contract"][1]


# --------------------------------------------------------------------------- the synthesis


@needs_node
def test_an_appliance_with_no_llm_says_so_instead_of_offering_a_button_that_fails(tmp_path):
    """``JMFTS_LLM_*`` is blank by default and blank is supported.

    So "no LLM" is an ordinary state of a working appliance, not a broken one, and the panel says
    which four capabilities that state costs rather than presenting a control that can only
    produce a 5xx. The Fail Early rule as this repository draws it: a form that cannot succeed
    does not submit, and the reason is on the page.
    """
    answers = {
        op_id: json.loads(_response([]).model_dump_json())
        for op_id in _search_operations()
        if op_id not in ("SearchService.auto_search", "SearchService.synthesize_search")
    }
    answers["SearchService.auto_search"] = json.loads(
        AutoSearchResponse(
            results=[],
            total=0,
            latency_ms=1.0,
            routing=RoutingMetadata(method="vector", reason="r", signals={}),
            applied=AppliedFilters(limit=10),
        ).model_dump_json()
    )
    result = _render(tmp_path, params={"q": "anything"}, answers=answers)

    panel = _find(result["tree"], "search-panel")[0]["text"]
    assert "No LLM is configured" in panel
    assert "synthesis" in panel, "the panel names what the missing LLM costs"
    # And nothing was asked of the route that cannot answer.
    assert "SearchService.synthesize_search" not in [op for op, _ in result["calls"]]


SYNTHESIS_HARNESS = textwrap.dedent("""
    import { install, serialize } from "./dom.mjs";
    install();
    const { renderSynthesis } = await import("%(static)s/views/search.js");
    console.log(JSON.stringify({
      answered: serialize(renderSynthesis(%(answered)s)),
      unavailable: serialize(renderSynthesis(%(unavailable)s)),
    }));
    """)


@needs_node
def test_a_placeholder_answer_is_labelled_as_one(tmp_path):
    """``POST /search/synthesize`` answers 200 with a placeholder when the LLM did not.

    ``search_service.py`` catches the failure and returns "LLM synthesis unavailable…" with
    ``llm_available=False`` and REAL sources. A panel that rendered that text plainly would show
    a sentence the model never wrote, above citations for a reply that never happened. So the
    flag is read and the page says which half of what follows is an answer.
    """
    sources = [SourceReference(document_id=11, title="Q3 board pack", score=0.9, method="vector")]
    answered = SynthesizeResponse(
        synthesis="Late interaction scores each query token against the document's tokens.",
        sources=sources,
        llm_model="local/llama",
        search_latency_ms=12.0,
        total_latency_ms=980.0,
    )
    unavailable = SynthesizeResponse(
        synthesis="LLM synthesis unavailable. Search results are provided below as source references.",
        sources=sources,
        llm_model="local/llama",
        search_latency_ms=12.0,
        total_latency_ms=13.0,
        llm_available=False,
    )
    result = _node(
        tmp_path,
        SYNTHESIS_HARNESS.replace("%(answered)s", answered.model_dump_json()).replace(
            "%(unavailable)s", unavailable.model_dump_json()
        ),
    )

    assert "Late interaction scores" in result["answered"]["text"]
    assert "placeholder" not in result["answered"]["text"]
    assert "Q3 board pack" in result["answered"]["text"]
    assert "via vector" in result["answered"]["text"]

    assert "placeholder rather than a synthesis" in result["unavailable"]["text"]
    assert "real retrieval results" in result["unavailable"]["text"]


@needs_node
def test_the_landing_state_is_a_form_and_not_an_empty_side_by_side(tmp_path):
    """Arriving at ``#/search`` with no query is ambivalence, not a failure.

    Nothing is searched, no method is called, and the page says what to do. The two preflight
    calls still happen, because what they answer — what is held out of every result set, which
    contexts exist — is true before a query is typed and is part of what the reader needs in
    order to type a good one.
    """
    result = _render(tmp_path, params={}, answers={})

    assert [op for op, _ in result["calls"]] == [
        "MetaService.capabilities",
        "SearchContextService.list_contexts",
    ]
    assert _columns(result["tree"]) == {}
    assert "Enter a query above" in result["tree"]["text"]
    # The form is drawn from the same tables the calls are, so every method is offered.
    assert "quick (GET)" in result["tree"]["text"]
