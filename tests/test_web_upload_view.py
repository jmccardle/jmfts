"""The ingest view: what it promises before it stores anything, and what it claims afterwards.

``docs/SPRINT_0_6_0.md`` Block F step 26. The view composes six operations — ``/ingest``,
``/ingest/file``, ``/ingest/analyze``, ``/ingest/explain``, ``/ingest/pipelines`` and
``/ingest/file/{id}/frontier`` — and two of its properties are worth a suite rather than a
read-through:

* **Nothing is stored until somebody asks for it to be.** Dropping a file calls
  ``/ingest/analyze``, which ``INGEST_SPEC.md`` 11.2 defines as "runs probe and stops". The
  test below asserts the call list, because "it analyses first" is otherwise a claim about
  code rather than about behaviour. Editing the options takes the plan back off the page: a
  forecast for options the request no longer carries is indistinguishable from a fresh one.
* **A page that shows "done" while the queue drains is a page that lies.** ``POST
  /ingest/file`` returns before extraction (spec 5.7), so the upload response can only ever
  mean ``accepted``. ``ingestionState`` is walked through a draining workbook here — accepted,
  in flight, a worker that stopped, and settled — and the assertion is on which of the five
  words reaches the page at each step.

The fixtures are built from the Pydantic contracts and dumped to JSON, for the reason
``tests/test_web_views.py`` gives about its own: a hand-typed dict can carry the same
misspelling the page carries, and then the suite agrees with the defect. ``READS`` is
resolved against those same models, which is the check whose absence let the capability
table read three field names that do not exist.

The DOM stub is imported from ``tests/test_web_views.py`` rather than copied. It is the
shell lane's harness, it is the only one this repository has, and five F3 views copying it
would be five copies to fix the day a view touches an API it does not model.
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

from jmfts_client.contracts.attempt import AttemptRecord
from jmfts_client.contracts.explain import (
    AlreadyStored,
    AnalyzedFile,
    AnalyzeIngestResponse,
    CountResponse,
    ExplainedTaskResponse,
    ExplainIngestResponse,
    PlanCostResponse,
    ProbeFailure,
    UnmeasuredResponse,
)
from jmfts_client.contracts.ingest import IngestResponse, PipelineInfo
from jmfts_client.contracts.upload import FileUploadResponse, IngestFrontierResponse
from tests.test_web_views import DOM_STUB

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "jmfts-web" / "jmfts_web" / "static"
VIEW = STATIC / "views" / "upload.js"
MANIFEST = STATIC / "shell" / "manifest.js"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="no JavaScript runtime on PATH")

#: Every contract this view declares a read against, by the name ``READS`` keys it under.
CONTRACTS: dict[str, type[BaseModel]] = {
    "AnalyzeIngestResponse": AnalyzeIngestResponse,
    "AnalyzedFile": AnalyzedFile,
    "ProbeFailure": ProbeFailure,
    "AlreadyStored": AlreadyStored,
    "ExplainIngestResponse": ExplainIngestResponse,
    "ExplainedTaskResponse": ExplainedTaskResponse,
    "FileUploadResponse": FileUploadResponse,
    "AttemptRecord": AttemptRecord,
    "IngestFrontierResponse": IngestFrontierResponse,
    "PipelineInfo": PipelineInfo,
    "IngestResponse": IngestResponse,
}


# ------------------------------------------------------------------------------- fixtures


#: The two counts every plan row carries since the cost fold (``SPRINT_JOBS.md`` Part 7).
#: STATED HERE RATHER THAN DEFAULTED ON THE CONTRACT: `[1, 1]` is right for a root-scoped
#: row and silently wrong for a `profile:sheet`, which runs once per sheet — a default on
#: the wire model would be a plausible number for a question the server had not answered.
ONCE = CountResponse(low=1, high=1)
NEVER = CountResponse(low=0, high=0)


def _row(**fields) -> ExplainedTaskResponse:
    """One plan row, with counts that suit its outcome unless the caller states others."""
    fields.setdefault("multiplicity", ONCE if fields["outcome"] == "enqueued" else NEVER)
    fields.setdefault("writes", NEVER)
    return ExplainedTaskResponse(**fields)


def _plan(*, known: bool = True, source: str = "probed") -> ExplainIngestResponse:
    """A forecast with one row of every outcome that decides, plus one that does not."""
    return ExplainIngestResponse(
        format="xlsx",
        prober_available=True,
        # A misspelling, not a measurement: `sheet_count` stopped being an ignored key when
        # `structure:sheets`' fan-out bound started dividing by it (Part 7), so a fixture
        # naming it here would describe an answer the appliance no longer gives.
        patterns_ignored=["has_text_lyer"],
        patterns_known=known,
        patterns_source=source,
        options={"structure": {"max_tokens": 120}},
        tasks=[
            _row(
                task="probe",
                outcome="enqueued",
                after=[],
                requires=[],
                forbids=[],
                params={},
                write_mode="self",
            ),
            _row(
                task="structure:sheets",
                outcome="enqueued",
                after=["probe"],
                requires=["has_sheets"],
                forbids=[],
                params={"max_tokens": 120},
                write_mode="children",
                writes=CountResponse(low=3, high=3),
                writes_usetype="sheet",
                writes_basis="one node per sheet",
            ),
            _row(
                task="profile:sheet",
                outcome="enqueued",
                scope="@children_of(structure:sheets):sheet",
                after=["structure:sheets"],
                requires=[],
                forbids=[],
                params={},
                multiplicity=CountResponse(low=3, high=3),
                writes=CountResponse(low=3, high=3),
                writes_usetype="profile",
                writes_basis="always 1",
            ),
            _row(
                task="extract:text",
                outcome="impossible",
                reason="xlsx declares no text layer, whatever the bytes are",
                after=["probe"],
                requires=["has_text_layer"],
                forbids=[],
                params={},
            ),
            _row(
                task="extract:facts",
                outcome="skipped",
                reason="no LLM is configured",
                after=["structure:sheets"],
                requires=[],
                forbids=[],
                params={},
            ),
            _row(
                task="citation",
                outcome="conditional" if not known else "not_applicable",
                if_condition_holds="enqueued" if not known else None,
                reason="needs has_page_geometry",
                after=["probe"],
                requires=["has_page_geometry"],
                forbids=[],
                params={},
                multiplicity=CountResponse(low=0, high=None) if not known else NEVER,
            ),
        ],
        cost=PlanCostResponse(
            tasks=CountResponse(low=8, high=None),
            nodes=CountResponse(low=6, high=None),
            by_cost_class={
                "cpu": CountResponse(low=7, high=None),
                "model": CountResponse(low=0, high=None),
                "llm": NEVER,
            },
            unmeasured=[
                UnmeasuredResponse(
                    evidence="sheet.measurements.rows",
                    needed_by="extract:sheet",
                    written_by="profile:sheet writes it (measurements)",
                )
            ],
            not_counted=["these totals are Part 4's table only"],
        ),
    )


def _analysis(**over) -> AnalyzeIngestResponse:
    fields = {
        "file": AnalyzedFile(
            filename="pipeline.xlsx",
            byte_size=41_234,
            content_hash="sha256:" + "ab" * 32,
            declared_mime="application/vnd.ms-excel",
            detected_mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            detected_by="zip_manifest",
            mime_agrees=False,
        ),
        "format": "xlsx",
        "patterns": {"has_sheets": True, "sheet_count": 3},
        "probe_detail": {"sheets": ["Q3", "Q4", "Notes"]},
        "plan": _plan(),
    }
    fields.update(over)
    return AnalyzeIngestResponse(**fields)


def _upload(**over) -> FileUploadResponse:
    fields = {
        "document_id": 4021,
        "filename": "pipeline.xlsx",
        "usetype": "file",
        "settled": "in_flight",
        "byte_size": 41_234,
        "content_hash": "sha256:" + "ab" * 32,
        "blob_ref": "lob:918273",
        "declared_mime": "application/vnd.ms-excel",
        "detected_mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "detected_by": "zip_manifest",
        "attempts": [
            AttemptRecord(
                task="probe",
                status="pending",
                scope_document_id=4021,
                param_fingerprint="sha256:" + "cd" * 32,
            )
        ],
    }
    fields.update(over)
    return FileUploadResponse(**fields)


def _frontier(settled_root: str, settled: int, in_flight: int, failed: int, tasks: int):
    return IngestFrontierResponse(
        document_id=4021,
        settled=settled_root,
        nodes_settled=settled,
        nodes_in_flight=in_flight,
        nodes_failed=failed,
        nodes_total=settled + in_flight + failed,
        tasks_unfinished=tasks,
    )


PIPELINES = [
    PipelineInfo(
        name="markdown",
        description="Markdown ingestion",
        source="content",
        options={"structure": {"chunk_strategy": "paragraph"}},
    ),
    PipelineInfo(name="wiki:arxiv", description="Fetch an arXiv paper", source="arxiv", options={}),
]

TEXT_RESULT = IngestResponse(
    source_document_id=77,
    title="A note",
    usetype="markdown",
    message_count=0,
    segment_count=4,
    summary_count=1,
    triple_count=0,
    tree_depth=2,
    stages=[],
)


def _fixtures() -> str:
    """Everything the harness serves, as one JSON literal built by the contracts."""
    return json.dumps(
        {
            "pipelines": [p.model_dump(mode="json") for p in PIPELINES],
            "analysis": _analysis().model_dump(mode="json"),
            "analysis_failed": _analysis(
                plan=None,
                probe_failed=ProbeFailure(
                    error="FileDataError: cannot open broken document",
                    error_type="permanent",
                ),
            ).model_dump(mode="json"),
            "analysis_stored": _analysis(
                already_stored=AlreadyStored(document_id=17, settled="settled", options={})
            ).model_dump(mode="json"),
            "upload": _upload().model_dump(mode="json"),
            "upload_existing": _upload(was_existing=True, settled="settled").model_dump(
                mode="json"
            ),
            "plan_unknown": _plan(known=False, source="unknown").model_dump(mode="json"),
            "frontiers": [
                _frontier("in_flight", 1, 3, 0, 5).model_dump(mode="json"),
                # The root settled and four of its children have not. The queue still owes
                # work, so this is ordinary progress rather than the stalled case below.
                _frontier("settled", 31, 4, 0, 2).model_dump(mode="json"),
                _frontier("settled", 35, 0, 0, 0).model_dump(mode="json"),
                _frontier("settled", 30, 0, 5, 0).model_dump(mode="json"),
            ],
            "text_result": TEXT_RESULT.model_dump(mode="json"),
        }
    )


# -------------------------------------------------------------------------- the node harness

PROLOGUE = """
    import { install, serialize, Element } from "./dom.mjs";
    install();
    // The bundle's page has a <head>; the stub does not model one, and `linkStylesheet`
    // refuses a document without one rather than rendering unstyled.
    globalThis.document.head = new Element("head");

    const F = %(fixtures)s;
    const { views } = await import("%(static)s/shell/registry.js");
    const view = await import("%(static)s/views/upload.js");

    const calls = [];
    const scheduled = [];
    const schedule = (work, ms) => { scheduled.push(ms); };
    const clientFor = (answers) => ({
      operations: {},
      list_registered_pipelines: async (args) => { calls.push(["pipelines", args]); return F.pipelines; },
      analyze_ingest: async (args) => {
        calls.push(["analyze", { ...args, file: args.file.name }]);
        return answers.analysis;
      },
      upload_file: async (args) => {
        calls.push(["upload", { ...args, file: args.file.name }]);
        return answers.upload;
      },
      file_frontier: async (args) => {
        calls.push(["frontier", args]);
        return answers.frontiers.shift();
      },
      explain_ingest: async (args) => { calls.push(["explain", args]); return answers.plan; },
      ingest_content: async (args) => { calls.push(["text", args]); return F.text_result; },
    });
    const FILE = { name: "pipeline.xlsx", size: 41234 };
"""


def _node(tmp_path: Path, harness: str) -> object:
    """Run one harness module and parse the single JSON document it prints."""
    (tmp_path / "dom.mjs").write_text(DOM_STUB, encoding="utf-8")
    script = tmp_path / "harness.mjs"
    body = (PROLOGUE + harness) % {"static": STATIC.as_uri(), "fixtures": _fixtures()}
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    done = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, cwd=tmp_path, timeout=60
    )
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout)


def _find(node: dict, className: str) -> list[dict]:
    found = [node] if node["class"] == className else []
    for child in node["children"]:
        found.extend(_find(child, className))
    return found


def _find_prefix(node: dict, prefix: str) -> list[dict]:
    found = [node] if node["class"] and node["class"].startswith(prefix) else []
    for child in node["children"]:
        found.extend(_find_prefix(child, prefix))
    return found


def _status(tree: dict) -> tuple[str, str]:
    """The state banner: the word it is showing, and everything it says."""
    banners = _find_prefix(tree, "upload-status upload-status-")
    assert len(banners) == 1, f"expected one state banner, found {len(banners)}"
    word = banners[0]["class"].rsplit("-", 1)[-1]
    return word, banners[0]["text"]


def _text(tree: dict) -> str:
    return tree["text"]


# ------------------------------------------------------------- the contracts this view reads


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
        if get_origin(annotation) is Union:
            members = [a for a in get_args(annotation) if a is not type(None)]
            annotation = members[0] if len(members) == 1 else annotation
        current = annotation
    return current


def _declared_reads() -> dict[str, list[str]]:
    source = VIEW.read_text(encoding="utf-8")
    block = source[source.index("export const READS = Object.freeze({") :]
    block = block[: block.index("\n});")]
    reads: dict[str, list[str]] = {}
    for contract, body in re.findall(r"(\w+): Object\.freeze\(\[(.*?)\]\)", block, re.S):
        reads[contract] = re.findall(r'"([a-z_.]+)"', body)
    return reads


def test_the_upload_view_reads_only_fields_the_contracts_carry():
    """Every path in ``READS`` resolves against the contract that owns it.

    This is the check whose absence produced the defect that opened the shell lane: three rows
    of the capability table read field names ``CapabilitiesResponse`` does not have, and all
    three rendered as the word "none" because an absent field and an empty list are the same
    thing to ``undefined || []``. This view reads eleven contracts, so it is checked against
    eleven models rather than against a reading of the appliance's JSON.
    """
    reads = _declared_reads()
    assert set(reads) == set(CONTRACTS), (
        f"READS names {sorted(set(reads) - set(CONTRACTS))} which this test does not import, "
        f"and omits {sorted(set(CONTRACTS) - set(reads))}"
    )
    for contract, paths in reads.items():
        assert paths, f"READS.{contract} is empty"
        for path in paths:
            _walk_contract(CONTRACTS[contract], path)  # KeyError is the failure


def test_nothing_in_the_view_reaches_into_a_response_directly():
    """``field()`` is not a convention here; it is the only way in.

    Correcting a misspelt name would leave the next one to render as "none" again. What stops
    that is that no response is ever reached into with ``.`` — so what is checked is that
    nothing bypasses the reader. Comments are dropped first, because they discuss these names.
    """
    code = "\n".join(
        line
        for line in VIEW.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith(("//", "*", "/*"))
    )
    names = "analysis|frontier|plan|stored|failure|pipeline|attempt|result|file|upload"
    # ``state.file.name`` is a browser ``File`` and not a contract, so a dotted prefix is
    # excluded: what this hunts is a bare response object being read field by field. A
    # filename ending in one of the bundle's suffixes is prose about a file, not a read —
    # ``upload.css`` is the stylesheet this view links.
    bypass = re.findall(rf"(?<![.\w])(?:{names})\.(?!css\b|js\b|json\b|md\b|py\b)[a-z_]+", code)
    assert not bypass, f"the view reaches into a response directly: {sorted(set(bypass))}"


def test_every_outcome_the_contract_names_has_a_word_on_this_page():
    """The plan's vocabulary, read out of the contract rather than restated in JavaScript.

    ``ExplainedTaskResponse.outcome``'s own description is the list, and the distinctions in
    it are load-bearing — ``impossible`` is stronger than ``not_applicable``, ``deferred``
    means the condition holds and nothing is registered to act on it. An appliance that grows
    a seventh outcome must fail here rather than render a word with no gloss beside it, which
    is also what ``planSummary`` refuses at runtime.
    """
    description = ExplainedTaskResponse.model_fields["outcome"].description
    declared = [word.strip() for word in description.split(".", 1)[0].split("|")]
    assert len(declared) >= 6, description

    source = VIEW.read_text(encoding="utf-8")
    block = source[source.index("export const OUTCOMES = Object.freeze({") :]
    rendered = re.findall(r"^  ([a-z_]+):", block[: block.index("\n});")], re.M)
    assert sorted(rendered) == sorted(
        declared
    ), f"the page glosses {sorted(rendered)}; the contract names {sorted(declared)}"


def test_the_view_is_a_file_and_one_appended_line():
    """IC-7, from the other end: this lane added a file and one import.

    ``tests/test_web_views.py`` performs the merge that makes the appended line safe. What is
    checked here is that this lane took the deal — no second registration anywhere, and no
    edit to the shell.
    """
    manifest = MANIFEST.read_text(encoding="utf-8")
    assert 'import "../views/upload.js";' in manifest
    assert manifest.count("../views/upload.js") == 1
    assert VIEW.is_file() and (STATIC / "views" / "upload.css").is_file()
    shell = (STATIC / "shell" / "shell.js").read_text(encoding="utf-8")
    assert "upload" not in shell, "the shell knows this view exists; it must not"


def test_the_stylesheet_is_linked_from_the_module_and_not_from_the_page():
    """``shell/shell.css``'s rule: a view styles itself from its own module.

    A second ``<link>`` in ``index.html`` would put the five F3 worktrees back in one shared
    file, which is the conflict IC-7 exists to remove.
    """
    assert "upload.css" not in (STATIC / "index.html").read_text(encoding="utf-8")
    source = VIEW.read_text(encoding="utf-8")
    # Resolved against the module rather than against the page: the page is /app/index.html
    # and this module is /app/views/upload.js, and a bare relative string picks the wrong one.
    assert 'new URL("./upload.css", import.meta.url)' in source


# ---------------------------------------------------------------- analyse before you store

FLOW_HARNESS = """
    const answers = { analysis: F.analysis, upload: F.upload, frontiers: [...F.frontiers] };
    const client = clientFor(answers);
    const mounted = view.mountUpload({ client, params: {}, navigate: () => {} }, { schedule });
    await mounted.actions.loadPipelines();

    const snapshots = {};
    const snap = (name) => { snapshots[name] = serialize(mounted.element); };

    await mounted.actions.analyze(FILE);
    snap("analysed");
    const afterAnalyse = calls.length;

    // The options changed, so the plan on the page describes a request nobody is making.
    // Instrumented: what a keystroke handler must NOT do is rebuild the field it was typed
    // into, so every element created while handling one is recorded.
    const created = [];
    const build = globalThis.document.createElement;
    globalThis.document.createElement = (tag) => { created.push(tag); return build(tag); };
    mounted.actions.setOptions('{"structure": {"max_tokens": 60}}');
    const madeWhileTyping = [...created];
    snap("invalidated");

    mounted.actions.setOptions("{not json");
    snap("badOptions");
    await mounted.actions.analyze(FILE);
    const refusedAnalyse = calls.length;
    snap("refused");

    mounted.actions.setOptions('{"structure": {"max_tokens": 60}}');
    await mounted.actions.analyze(FILE);
    await mounted.actions.ingest();
    snap("accepted");

    await mounted.actions.refreshFrontier();
    snap("draining");
    await mounted.actions.refreshFrontier();
    snap("rootSettledChildrenNot");
    await mounted.actions.refreshFrontier();
    snap("settled");

    console.log(JSON.stringify({
      snapshots, calls, scheduled, madeWhileTyping,
      analysedWithoutStoring: afterAnalyse,
      refusedAnalyse,
    }));
"""


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    if NODE is None:
        pytest.skip("no JavaScript runtime on PATH")
    return _node(tmp_path_factory.mktemp("upload-flow"), FLOW_HARNESS)


def test_dropping_a_file_analyses_it_and_stores_nothing(flow):
    """The first half of the step: the truth BEFORE the appliance acts on it.

    ``/ingest/analyze`` "runs probe and stops" — no document, no blob, no queue row — and this
    page is built so that the only thing a dropped file can do is ask that question. The
    assertion is on the call list, because a page that stored first and described afterwards
    would render identically.
    """
    made = [call[0] for call in flow["calls"][: flow["analysedWithoutStoring"]]]
    assert made == ["pipelines", "analyze"], made
    assert "upload" not in made

    tree = flow["snapshots"]["analysed"]
    facts = _text(_find(tree, "upload-facts")[0])
    assert "pipeline.xlsx" in facts and "41.2 kB" in facts
    # The declared mime and the detected mime disagree, which is exactly the case the
    # ANALYZE contract says a caller cannot otherwise see until two tasks later.
    assert "the bytes are" in facts and "the client said" in facts

    plan = _find(tree, "upload-plan")[0]
    assert "patterns probed" in _text(plan)
    assert "Every task below is decided" in _text(plan)
    rows = [row for row in plan["children"] if row["class"] == "upload-summary"][0]
    assert [pill["text"] for pill in rows["children"]] == [
        "3 enqueued",
        "1 skipped",
        "1 not_applicable",
        "1 impossible",
    ]


def test_the_button_says_what_pressing_it_will_do(flow):
    """The forecast and the button are the same sentence, or the forecast is decoration."""
    button = _find(flow["snapshots"]["analysed"], "upload-go")[0]
    assert button["text"] == "Ingest — stores 41.2 kB, enqueues 3 tasks"


def test_changing_the_options_takes_the_stale_plan_off_the_page(flow):
    """A forecast for options the request no longer carries is worse than no forecast.

    It is indistinguishable from a fresh one, and the options are resolved against the
    detected format before anything is created — so the plan really does change with them.
    """
    invalidated = flow["snapshots"]["invalidated"]
    assert _find(invalidated, "upload-plan") == []
    assert _find(invalidated, "upload-go") == []
    assert "no longer describes it" in _text(invalidated)


def test_typing_in_the_options_box_does_not_rebuild_the_options_box(flow):
    """A keystroke redraws what the value changed, and not the field it was typed into.

    This page redraws a panel by clearing it and building it again, which is the right shape
    for a page whose every fact comes from a response — and it is exactly wrong for the
    element holding the caret. The handler would replace the textarea mid-word and typing
    would drop every character after the first.

    Asserted on what was constructed while handling one keystroke: the analysis panel is
    rebuilt (its plan no longer describes the request), and no form control is.
    """
    made = flow["madeWhileTyping"]
    assert made, "setOptions did nothing at all"
    assert "textarea" not in made and "input" not in made and "select" not in made, made


def test_options_that_are_not_json_stop_the_request(flow):
    """Fail Early on the form: the request does not go out, and the page says why.

    Not a default of ``{}``. An upload resolves its options before anything is created, and a
    silently emptied options box would ingest under defaults the caller did not ask for.
    """
    assert "options is not JSON" in _text(flow["snapshots"]["badOptions"])
    # And the analyse that followed it made no call at all.
    assert flow["refusedAnalyse"] == flow["analysedWithoutStoring"]
    assert "options is not JSON" in _text(flow["snapshots"]["refused"])


# ------------------------------------------------------- accepted, in flight, settled


def test_an_upload_is_accepted_and_that_is_all_it_is(flow):
    """``POST /ingest/file`` returns before extraction, so the response cannot mean "done".

    This is the state the demo turns on. A workbook's rows do not exist when the upload
    answers; a page that read 201 as completion would show an empty tree and call it finished.
    """
    word, said = _status(flow["snapshots"]["accepted"])
    assert word == "accepted"
    assert "stored, and probe is queued" in said
    assert "Nothing has been extracted yet" in said
    assert "settled" not in said.split("Nothing has been extracted")[0]

    facts = _text(_find(flow["snapshots"]["accepted"], "upload-facts")[1])
    assert "4021" in facts
    assert "probe: pending" in facts, "the attempt log is what says nothing has run yet"


def test_the_frontier_moves_the_page_through_in_flight_to_settled(flow):
    """Counts, never a percentage, and the three words the step asks for.

    The middle reading is the one that matters: the ROOT node is settled and four nodes under
    it are not. A page that read the root's own state would say "settled" there and be wrong
    about the thing somebody is watching for.
    """
    draining, _ = _status(flow["snapshots"]["draining"])
    middle, middle_said = _status(flow["snapshots"]["rootSettledChildrenNot"])
    settled, settled_said = _status(flow["snapshots"]["settled"])

    assert draining == "in_flight"
    assert middle == "in_flight", "a settled root with children in flight is not settled"
    assert "31 settled, 4 in flight" in middle_said
    assert settled == "settled"
    assert "35 settled, 0 in flight, 0 failed, of 35" in settled_said


def test_polling_stops_when_nothing_more_will_happen(flow):
    """The poll is not a timer that runs forever; it is a question with an end.

    Two non-terminal readings schedule another; the settled one does not. A page that kept
    asking would keep a demo appliance busy for as long as the tab was open.
    """
    assert flow["scheduled"] == [1500, 1500], flow["scheduled"]
    frontier_calls = [call for call in flow["calls"] if call[0] == "frontier"]
    assert len(frontier_calls) == 3
    assert frontier_calls[0][1] == {"document_id": 4021}


# --------------------------------------------------------------------- the awkward answers

EDGE_HARNESS = """
    const out = {};

    const broken = clientFor({ analysis: F.analysis_failed, upload: F.upload, frontiers: [] });
    const one = view.mountUpload({ client: broken, params: {}, navigate: () => {} }, { schedule });
    await one.actions.analyze(FILE);
    out.probeFailed = serialize(one.element);

    const dedup = clientFor({
      analysis: F.analysis_stored,
      upload: F.upload_existing,
      frontiers: [F.frontiers[2]],
    });
    const two = view.mountUpload({ client: dedup, params: {}, navigate: () => {} }, { schedule });
    await two.actions.analyze(FILE);
    out.alreadyStored = serialize(two.element);
    await two.actions.ingest();
    out.acceptedExisting = serialize(two.element);

    const failing = clientFor({ analysis: F.analysis, upload: F.upload, frontiers: [F.frontiers[3]] });
    const three = view.mountUpload({ client: failing, params: {}, navigate: () => {} }, { schedule });
    await three.actions.analyze(FILE);
    await three.actions.ingest();
    await three.actions.refreshFrontier();
    out.failed = serialize(three.element);

    // A worker that is not running: nodes in flight, and a queue that owes nothing.
    out.stalled = view.ingestionState({
      upload: F.upload,
      frontier: { ...F.frontiers[0], nodes_in_flight: 3, tasks_unfinished: 0 },
    });

    const guessing = clientFor({ analysis: F.analysis, upload: F.upload, frontiers: [] });
    const four = view.mountUpload({ client: guessing, params: {}, navigate: () => {} }, { schedule });
    four.actions.setExplainFormat("pdf");
    guessing.explain_ingest = async (args) => { calls.push(["explain", args]); return F.plan_unknown; };
    await four.actions.explain();
    out.unknownPatterns = serialize(four.element);

    const texting = clientFor({ analysis: F.analysis, upload: F.upload, frontiers: [] });
    const five = view.mountUpload({ client: texting, params: {}, navigate: () => {} }, { schedule });
    five.actions.setText({ textContent: "# A note", textUsetype: "markdown", textTitle: "A note" });
    await five.actions.ingestText();
    out.textIngested = serialize(five.element);
    out.textCall = calls[calls.length - 1];

    const refusals = {};
    const refuse = async (label, work) => {
      try { await work(); refusals[label] = null; }
      catch (error) { refusals[label] = error.message; }
    };
    await refuse("no anchor at all", () => view.ingestionState({}));
    await refuse("an outcome with no word", () => view.planSummary({
      ...F.analysis.plan,
      tasks: [{ ...F.analysis.plan.tasks[0], outcome: "rescheduled" }],
    }));
    await refuse("a field the contract lacks", () =>
      view.field({ nodes_settled: 1 }, "IngestFrontierResponse", "nodes_failed"));
    await refuse("a path nothing declares", () =>
      view.field({}, "IngestFrontierResponse", "nodes_pending"));
    out.refusals = refusals;
    out.calls = calls;
    console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def edges(tmp_path_factory):
    if NODE is None:
        pytest.skip("no JavaScript runtime on PATH")
    return _node(tmp_path_factory.mktemp("upload-edges"), EDGE_HARNESS)


def test_a_probe_that_would_fail_is_reported_and_no_plan_is_invented(edges):
    """``ANALYZE`` returns ``probe_failed`` with a null plan, and the page keeps both facts.

    The contract's reasoning, which this page is the consumer of: a file probe cannot open has
    no downstream schedule, and emitting one built on an empty pattern set would say "nothing
    is applicable to this file" when the truth is "we could not look". So there is no plan on
    the page, the classification that decides whether the real task would retry is on it, and
    the button says what pressing it would leave behind.
    """
    tree = edges["probeFailed"]
    assert _find(tree, "upload-plan") == []
    notice = _find(tree, "upload-notice bad")[0]["text"]
    assert "probe would FAIL" in notice
    assert "cannot open broken document" in notice
    assert "permanent" in notice
    assert _find(tree, "upload-go")[0]["text"] == "Ingest anyway — probe will fail"


def test_a_deduplicated_upload_does_not_claim_this_request_ingested_anything(edges):
    """``already_stored`` before, ``was_existing`` after, and neither is silent.

    An upload of bytes that are already here resolves to the existing node and runs no plan at
    all, so a page that reported it as an ingestion would be describing a run that is not
    going to happen — and the frontier it then shows belongs to somebody else's earlier one.
    """
    before = _find(edges["alreadyStored"], "upload-notice")[0]["text"]
    assert "already stored, as document 17" in before
    assert "runs no plan at all" in before
    assert _find(edges["alreadyStored"], "upload-go")[0]["text"].endswith("the existing node")

    word, said = _status(edges["acceptedExisting"])
    assert word == "accepted"
    assert "already here" in said
    assert "created nothing and enqueued nothing" in said
    assert "the earlier run" in said


def test_a_failed_node_is_not_a_finished_one(edges):
    """Five nodes failed and thirty settled, and the word for that is not "settled"."""
    word, said = _status(edges["failed"])
    assert word == "failed"
    assert "30 settled, 0 in flight, 5 failed, of 35" in said


def test_an_empty_queue_with_nodes_in_flight_is_its_own_answer(edges):
    """The one state the contract names in prose, because it is the one nobody expects.

    "Zero with nodes_in_flight above zero means the frontier is waiting on something other
    than the queue." On a demo machine that is a worker that is not running, and rendering it
    as ordinary progress is how somebody waits ten minutes for nothing to happen.
    """
    assert edges["stalled"]["status"] == "stalled"
    assert "empty queue" in edges["stalled"]["headline"]
    assert "jmfts-worker" in edges["stalled"]["note"]


def test_a_plan_with_unmeasured_patterns_does_not_read_as_a_forecast(edges):
    """``EXPLAIN`` with no patterns answers conditionally, and the page says so twice.

    ``patterns_source`` is load-bearing rather than metadata — the contract's own word — and
    an answer that did not say where its patterns came from would be indistinguishable from
    one that made them up.
    """
    basis = _find(edges["unknownPatterns"], "upload-basis upload-basis-partial")[0]["text"]
    assert "Some tasks below are undecided" in basis
    assert "patterns unknown" in basis
    summary = _find(edges["unknownPatterns"], "upload-summary")[0]
    assert "1 conditional" in [pill["text"] for pill in summary["children"]]
    assert "becomes" in _text(_find(edges["unknownPatterns"], "upload-plan")[0])


def test_text_goes_in_through_the_other_door_and_the_page_says_so(edges):
    """``POST /ingest`` is synchronous and ``POST /ingest/file`` is not, and they look alike.

    The text path "drains THAT DOCUMENT'S tasks before returning" — the request acts as the
    worker — so there is no frontier to watch and the tree really is finished when it answers.
    A page that offered both entry points without saying which is which would teach a reader
    that the file path finishes on return too, which is the one thing this view is for.
    """
    kind, args = edges["textCall"]
    assert kind == "text"
    assert args["request"]["usetype"] == "markdown"
    assert args["request"]["title"] == "A note"
    assert args["request"]["content"] == "# A note"

    said = _text(edges["textIngested"])
    assert "SYNCHRONOUS" in said
    assert "drained the document's tasks before it answered" in said
    # And the frontier panel is not on the page at all: there is nothing to watch.
    assert _find_prefix(edges["textIngested"], "upload-status upload-status-") == []


def test_the_view_refuses_what_it_cannot_honestly_show(edges):
    """Four refusals, each of which would otherwise be a plausible-looking page."""
    refusals = edges["refusals"]
    for label, message in refusals.items():
        assert message, f"{label} was accepted"
    assert "neither an upload nor a frontier" in refusals["no anchor at all"]
    assert "rescheduled" in refusals["an outcome with no word"]
    assert "nodes_failed" in refusals["a field the contract lacks"]
    assert "READS" in refusals["a path nothing declares"]


# --------------------------------------------------------------------- mounted by the shell

MOUNT_HARNESS = """
    const answers = { analysis: F.analysis, upload: F.upload, frontiers: [F.frontiers[2]] };
    const client = clientFor(answers);
    // By id, not by position: this harness imports the view module directly rather than
    // through the manifest, so the registry holds whatever that one import registered.
    const registered = views().find((v) => v.id === "ingest");
    const element = await registered.component({ client, params: { document: "4021" } });
    console.log(JSON.stringify({
      registration: {
        id: registered.id, title: registered.title, path: registered.path,
        tags: [...registered.tags],
      },
      calls,
      head: serialize(globalThis.document.head),
      tree: serialize(element),
    }));
"""


@needs_node
def test_the_shell_can_mount_this_view_and_it_can_watch_an_ingestion_it_did_not_start(tmp_path):
    """IC-7 end to end, plus the one route parameter this view takes.

    ``#/ingest?document=42`` hands an ingestion over from another view without either of them
    knowing about the other — the tree browser (step 28) can link to it and this page needs no
    change. Everything the page draws comes from a call: the entry-point table is
    ``/ingest/pipelines`` and the frontier is ``/ingest/file/{id}/frontier``.
    """
    result = _node(tmp_path, MOUNT_HARNESS)

    assert result["registration"] == {
        "id": "ingest",
        "title": "Ingest",
        "path": "/ingest",
        "tags": ["ingest"],
    }
    assert [call[0] for call in result["calls"]] == ["pipelines", "frontier"]
    assert result["calls"][1][1] == {"document_id": 4021}

    # The stylesheet is linked by the module, into the document's head.
    assert result["head"]["children"][0]["tag"] == "link"

    word, _said = _status(result["tree"])
    assert word == "settled"
    pipelines = _text(_find(result["tree"], "upload-pipelines")[0])
    assert "markdown" in pipelines and "wiki:arxiv" in pipelines
    assert "tunes: structure" in pipelines
