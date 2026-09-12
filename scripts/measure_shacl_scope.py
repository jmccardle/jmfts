"""How large a scope a full SHACL validation run can hold. ``docs/SPRINT_0_5_0.md`` Block A step 5.

**A measurement, not a benchmark.** Its result decides something outside this sprint: open
question 6.3 says the bound past which ``validate:shape`` refuses is "whatever step 5 measures
as comfortable — a bound the operator can move, not a constant nobody chose", and incremental
validation is worth building only when a full run is measured too slow. Both answers need a
number, and ``pyshacl`` has no streaming mode, so the number is a memory number before it is a
time number.

**It drives the REAL handler.** ``OntologyService.validate_binding`` enqueues, a real
:class:`~jmfts_core.ingest_worker.IngestWorker` claims and runs
``jmfts_core.validate_tasks.run_validate_shape`` with the real
:class:`~jmfts_core.rollup_tasks.IngestRollupPlanner`, and the numbers come off the report node
the run wrote. Nothing here re-implements a validation run.

**Three of the handler's callees are wrapped in timers, and nothing else is changed.**
``build_data_graph``, ``bound_shape_graph`` and the ``pyshacl`` module ``require_pyshacl()`` returns
are replaced, in ``jmfts_core.validate_tasks``'s namespace, by objects that call the real ones
and record the elapsed time. That is instrumentation of the shipped path, not a second path:
delete the three wrappers and the same run happens with no timings. The split matters because
building the graph and running the validator scale differently and a single total hides which
one is the wall.

What each memory number counts, because they count different things:

``rss_peak_kib``
    ``VmHWM`` from ``/proc/self/status`` — the kernel's high-water resident set for this
    process, in KiB. Counts every resident page: the interpreter, imported modules, ``rdflib``'s
    Python objects, the SQLAlchemy rows the build holds, ``psycopg2``'s C-side result buffers,
    the allocator's unreturned arenas. Does NOT count anything swapped out, anything the
    allocator freed to the OS after the peak (the number never falls), or memory in another
    process — Postgres's own usage is out of frame. It is the number a container memory limit is
    enforced against, which is why it is the one the bound is derived from. See
    :func:`_peak_rss_kib` for why it is not ``ru_maxrss``.

``tracemalloc_peak_kib``
    Python's allocator only, from :mod:`tracemalloc`. Counts objects allocated through
    ``PyMem``/``PyObject`` — which is most of ``rdflib`` and ``pyshacl``, both pure Python — and
    counts NOTHING allocated by a C extension outside that allocator, nor the interpreter's own
    baseline. It is measured in its own process because tracing costs memory and time; mixing it
    into a peak-RSS run would inflate the number the bound is derived from.

**Each measured run is a fresh subprocess**, because a peak is a high-water mark that never
falls: a second run in the same interpreter would report the first one's peak. The corpus is built once per size in the parent and left committed; the child
connects to it and measures only the run.

The corpus is GENERATED, and :func:`generate_corpus` says exactly how. There is no real one
here to measure — see ``docs/MEASURE_SHACL_SCOPE.md`` for what was available and what it would
take to answer the question against real data.

    ./.venv/bin/python -m scripts.measure_shacl_scope --sizes 100,1000,10000 --repeats 3

Research code: outside the black/ruff gate's three paths by the same rule as the rest of
``scripts/`` (``CLAUDE.md``, "The gate"), and formatted to it anyway.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import Optional

# The scratch database has to be named before `jmfts_core.config` is imported anywhere, because
# `get_settings` is cached and `jmfts_core.database` builds its engine from the cached copy. The
# parent sets it from `--database`; the child inherits it through the environment.
_DEFAULT_DATABASE = "jmfts_shacl_measure"

#: The scratch database's name must end in this. The same rail ``tests/conftest.py`` puts on
#: ``*_test``: this script DROPS the database it is pointed at, and a typo in ``JMFTS_DB_NAME``
#: should not be able to drop an appliance.
_DATABASE_SUFFIX = "_measure"

ONTOLOGY_NAME = "measure-vendor-shapes"
SHAPE_IRI = "http://example.org/VendorShape"
SCOPE_USETYPE = "measure:vendor"

#: One ``sh:NodeShape`` with two ``sh:minCount 1`` property constraints, one of them a NAMED
#: property shape. Named rather than inline on purpose: a shapes graph built by any kind of
#: reachability walk drops it (``rdflib``'s CBD stops at a named node), so a shape that did not
#: carry one would measure a validator doing less work than a bound shape really does.
#: ``rdf.shacl.bound_shape_graph`` keeps the whole vocabulary and disarms the other shapes,
#: which is why it runs.
TURTLE = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.org/> .
@prefix prop: <urn:jmfts:predicate:> .

ex:VendorShape a sh:NodeShape ;
    sh:property [ sh:path prop:name ; sh:minCount 1 ] ;
    sh:property ex:CountryProperty .

ex:CountryProperty
    sh:path prop:country ;
    sh:minCount 1 .
"""


# ---------------------------------------------------------------------------
# The budget the word "comfortable" is spent against
# ---------------------------------------------------------------------------

#: The cpu-badged worker pod's memory REQUEST, ``deploy/k8s/20-worker-cpu.yaml:67``. A process
#: past its request is running on a node's burst capacity and is the first thing evicted under
#: node pressure, so this is where a run stops being comfortable.
WORKER_MEMORY_REQUEST_KIB = 2 * 1024 * 1024

#: The same pod's memory LIMIT, ``deploy/k8s/20-worker-cpu.yaml:73``. The manifest calls an OOM
#: kill "a clean death the lease already handles", and for most tasks it is — but a validation
#: run has no partial progress, so the reaped task retries into the same allocation and dies the
#: same way. Past this line a scope is not slow, it is impossible.
WORKER_MEMORY_LIMIT_KIB = 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# One measured run
# ---------------------------------------------------------------------------


@dataclass
class Measurement:
    """One full validation run over one scope, as the child process saw it."""

    scope_documents: int
    triples_per_document: int
    violation_fraction: float
    #: Documents the request resolved and pinned onto the task row.
    documents_resolved: int
    #: ``len(graph)`` as the handler recorded it on the report node.
    triple_count: int
    #: Distinct document nodes those triples name — what ``pyshacl`` actually holds.
    node_count: int
    violation_count: int
    conforms: bool
    #: ``OntologyService.validate_binding``: resolve the scope, mint the node, enqueue.
    request_seconds: float
    #: ``rdf/shacl.build_data_graph`` — the SQL and the RDF-term minting.
    build_graph_seconds: float
    #: ``rdf.shacl.bound_shape_graph`` — the vocabulary, disarmed, plus one ``sh:targetNode`` per
    #: in-scope document. Reported separately because it is the one cost that is linear in the
    #: scope while looking like a constant.
    shape_graph_seconds: float
    #: ``pyshacl.validate``.
    validate_seconds: float
    #: The whole drain: claim, handler, settle, terminal write.
    drain_seconds: float
    rss_baseline_kib: int
    #: The peak resident set the moment ``build_data_graph`` returned. Together with
    #: :attr:`rss_peak_kib` it says WHICH PHASE is the memory wall — the split the wall-time
    #: columns give for time, which a single peak cannot.
    rss_after_build_kib: int
    #: The same, the moment ``bound_shape_graph`` returned; the difference from the line above is
    #: what one ``sh:targetNode`` per in-scope document costs.
    rss_after_shape_kib: int
    rss_peak_kib: int
    rss_after_kib: int
    tracemalloc_peak_kib: Optional[int]


def _status_kib(field: str) -> int:
    """One ``/proc/self/status`` size field, in KiB. Raises rather than defaulting."""
    prefix = f"{field}:"
    with open("/proc/self/status", "r", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(prefix):
                return int(line.split()[1])
    raise RuntimeError(f"/proc/self/status carries no {field}; this measurement needs Linux")


def _rss_now_kib() -> int:
    """Current resident set. What is still held, which a high-water mark cannot say."""
    return _status_kib("VmRSS")


def _peak_rss_kib() -> int:
    """This process's OWN peak resident set, ``VmHWM`` from ``/proc/self/status``.

    **Not** ``resource.getrusage(RUSAGE_SELF).ru_maxrss``, and the difference is not academic:
    Linux keeps ``ru_maxrss`` in ``signal_struct`` and **does not reset it across ``exec``**, so
    a child spawned by a large parent reports the PARENT's high-water mark as its own. Measured
    on this machine, a 900 MiB parent spawning a trivial child:

        child ru_maxrss 933320   VmHWM 11464
        parent ru_maxrss 933320  VmHWM 933320

    ``VmHWM`` is read from the ``mm_struct``, which ``exec`` replaces, so it is the child's own
    peak. The first ladder run of this script used ``ru_maxrss`` and its four smallest rungs
    reported the parent's corpus-generation footprint instead of the run's; they were re-taken.

    Both count the same thing once the process is right: every resident page — interpreter,
    modules, ``rdflib`` objects, the SQLAlchemy rows the build holds, ``psycopg2``'s buffers,
    allocator arenas never returned. Neither counts swapped-out pages, memory freed after the
    peak, or any other process — Postgres's own usage is out of frame.
    """
    return _status_kib("VmHWM")


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


def generate_corpus(
    session, *, first: int, count: int, triples_per_document: int, violation_fraction: float
) -> None:
    """Add ``count`` documents and ``count * triples_per_document`` triples to the scope.

    **This corpus is generated and it is not a sample of anything.** No appliance in this tree
    holds a triple store large enough to answer step 5's question; the largest real scope
    available is recorded in ``docs/MEASURE_SHACL_SCOPE.md``. What is measured here is therefore
    the SHAPE of the curve — how the graph, the shape graph and ``pyshacl`` each scale with the
    scope — against a ratio taken from the appliance's own configuration rather than invented:
    ``Settings.extraction_max_facts`` is 5 (``config.py:256``), the maximum triples fact
    extraction writes per segment, and a segment is a document row.

    Every object endpoint is inside the scope, so the graph has no boundary cuts. That is the
    cheap case for ``BoundaryCut`` accounting and it is chosen deliberately: a cut costs a tuple
    and an index entry per cut, which would add a term to the memory curve that has nothing to
    do with what ``pyshacl`` holds.

    ``violation_fraction`` of the documents get no ``prop:country`` triple and so fail the
    shape. The rest get one. The triple count per document is exactly
    ``triples_per_document`` either way — a conforming document spends one of its triples on
    ``country``, a violating one spends it on a second ``related`` edge — so the two differ in
    what the validator REPORTS and not in what it reads.
    """
    from sqlalchemy import insert, select

    from jmfts_core.models.document import Document
    from jmfts_core.models.triple import Predicate, Triple

    if triples_per_document < 2:
        raise ValueError(
            f"triples_per_document must be at least 2 (one 'name', one 'country' or "
            f"'related'); got {triples_per_document}"
        )

    # Looked up by IRI and not by name, because `import_ontology` has already registered a
    # predicate for every `sh:path` the vocabulary names (`prop:name`, `prop:country`) and the
    # IRI is what the data graph is keyed on. Creating a second row for the same IRI is what
    # `predicates_iri_key` refuses, and rightly: two predicate rows with one IRI would put two
    # names on one property.
    predicates = {}
    for name in ("name", "country", "related"):
        iri = f"urn:jmfts:predicate:{name}"
        existing = session.execute(select(Predicate).where(Predicate.iri == iri)).scalar()
        if existing is None:
            existing = Predicate(name=name, iri=iri)
            session.add(existing)
            session.flush()
        predicates[name] = existing.id

    session.execute(
        insert(Document),
        [
            {
                "title": f"vendor {first + i}",
                "content": f"vendor {first + i} body",
                "usetype": SCOPE_USETYPE,
                "structured_content": {},
                "path": [],
            }
            for i in range(count)
        ],
    )
    session.flush()

    ids = list(
        session.execute(
            select(Document.id)
            .where(Document.usetype == SCOPE_USETYPE)
            .order_by(Document.id.desc())
            .limit(count)
        ).scalars()
    )
    ids.reverse()
    if len(ids) != count:
        raise RuntimeError(f"expected {count} new scope documents, found {len(ids)}")

    # Deterministic rather than random: the violating documents are the ones whose index falls
    # under the fraction, so two runs of this script build the same corpus and a difference
    # between them is a difference in the machine.
    violating = round(count * violation_fraction)
    rows: list[dict] = []
    for i, doc_id in enumerate(ids):
        rows.append(
            {
                "subject_id": doc_id,
                "predicate_id": predicates["name"],
                "object_literal": f"Vendor {first + i}",
            }
        )
        fillers = triples_per_document - 1
        if i >= violating:
            rows.append(
                {
                    "subject_id": doc_id,
                    "predicate_id": predicates["country"],
                    "object_id": ids[(i + 1) % count],
                }
            )
            fillers -= 1
        for k in range(fillers):
            rows.append(
                {
                    "subject_id": doc_id,
                    "predicate_id": predicates["related"],
                    "object_id": ids[(i + k + 2) % count],
                }
            )

    # Chunked because the whole list is one executemany otherwise, and a million-row parameter
    # list is a memory cost in the PARENT that has nothing to do with what is being measured.
    for start in range(0, len(rows), 20_000):
        session.execute(insert(Triple), rows[start : start + 20_000])
    session.commit()


# ---------------------------------------------------------------------------
# The child: one run, instrumented
# ---------------------------------------------------------------------------


def _instrument(timings: dict[str, float], marks: dict[str, int]):
    """Wrap the handler's three costly callees in timers, in place, and return nothing.

    ``jmfts_core.validate_tasks`` binds ``build_data_graph`` and ``require_pyshacl`` at module
    scope, so replacing the module attribute is what the handler will call. The real functions
    are what actually run; the wrapper adds a ``perf_counter`` around each, and reads the peak
    resident set after the first two so the peak can be attributed to a phase.
    """
    import jmfts_core.validate_tasks as vt

    real_build = vt.build_data_graph
    real_shape = vt.bound_shape_graph
    real_require = vt.require_pyshacl

    def timed_build(*args, **kwargs):
        started = time.perf_counter()
        try:
            return real_build(*args, **kwargs)
        finally:
            timings["build_graph_seconds"] = time.perf_counter() - started
            marks["rss_after_build_kib"] = _peak_rss_kib()

    def timed_shape(*args, **kwargs):
        started = time.perf_counter()
        try:
            return real_shape(*args, **kwargs)
        finally:
            timings["shape_graph_seconds"] = time.perf_counter() - started
            marks["rss_after_shape_kib"] = _peak_rss_kib()

    class _TimedPyshacl:
        """Delegates every attribute to the real module; times ``validate``."""

        def __init__(self, module):
            self._module = module

        def __getattr__(self, name):
            return getattr(self._module, name)

        def validate(self, *args, **kwargs):
            started = time.perf_counter()
            try:
                return self._module.validate(*args, **kwargs)
            finally:
                timings["validate_seconds"] = time.perf_counter() - started

    vt.build_data_graph = timed_build
    vt.bound_shape_graph = timed_shape
    vt.require_pyshacl = lambda: _TimedPyshacl(real_require())


def measure_once(
    *,
    binding_id: int,
    scope_documents: int,
    triples_per_document: int,
    violation_fraction: float,
    trace: bool,
) -> Measurement:
    """Enqueue one validation run, drain it with a real worker, and report what it cost."""
    import tracemalloc

    from jmfts_client.contracts.rdf import ShapeValidationRequest
    from jmfts_core.database import get_session
    from jmfts_core.ingest_worker import IngestWorker
    from jmfts_core.models.document import Document
    from jmfts_core.principal_context import OWNER, reset_principal, set_principal
    from jmfts_core.rollup_tasks import IngestRollupPlanner
    from jmfts_core.services.ontology_service import OntologyService
    from jmfts_core.validate_tasks import VALIDATION_BLOCK

    timings: dict[str, float] = {}
    marks: dict[str, int] = {}
    _instrument(timings, marks)

    # Taken after every import and after the first connection, so what it excludes is the cost
    # of being a Python process with this appliance loaded, and what the deltas below measure is
    # the run.
    baseline = _peak_rss_kib()
    if trace:
        tracemalloc.start()

    with get_session() as session:
        token = set_principal(OWNER)
        try:
            started = time.perf_counter()
            response = OntologyService(session).validate_binding(
                ONTOLOGY_NAME, ShapeValidationRequest(binding_id=binding_id)
            )
            request_seconds = time.perf_counter() - started
        finally:
            reset_principal(token)

    # UNBOUND, which is production: a worker holds no principal. What it validates is what the
    # request pinned onto the task row.
    worker = IngestWorker(worker_id="measure-shacl-scope", planner=IngestRollupPlanner())
    started = time.perf_counter()
    ran = worker.drain(max_tasks=8)
    drain_seconds = time.perf_counter() - started
    if ran != 1:
        raise RuntimeError(
            f"expected exactly one task (the validation run) to drain, {ran} ran; a second "
            "task means something in the settling walk enqueued work and the split below is "
            "not a validation run's split"
        )

    peak = _peak_rss_kib()
    after = _rss_now_kib()
    traced = None
    if trace:
        traced = round(tracemalloc.get_traced_memory()[1] / 1024)
        tracemalloc.stop()

    with get_session() as session:
        node = session.get(Document, response.report_document_id)
        if node is None:
            raise RuntimeError(f"report node {response.report_document_id} is gone")
        block = (node.structured_content or {}).get(VALIDATION_BLOCK) or {}
        report = block.get("report")
        if report is None:
            raise RuntimeError(
                f"report node {node.id} carries no report; the task ended {node.settled!r} "
                "and the run did not complete"
            )

    missing = {"build_graph_seconds", "shape_graph_seconds", "validate_seconds"} - set(timings)
    if missing:
        raise RuntimeError(
            f"the handler did not call {sorted(missing)}; the instrumentation names a callee "
            "the shipped path no longer has, so the split would be a fiction"
        )

    return Measurement(
        scope_documents=scope_documents,
        triples_per_document=triples_per_document,
        violation_fraction=violation_fraction,
        documents_resolved=report["documents_resolved"],
        triple_count=report["triple_count"],
        node_count=report["node_count"],
        violation_count=report["violation_count"],
        conforms=report["conforms"],
        request_seconds=request_seconds,
        build_graph_seconds=timings["build_graph_seconds"],
        shape_graph_seconds=timings["shape_graph_seconds"],
        validate_seconds=timings["validate_seconds"],
        drain_seconds=drain_seconds,
        rss_baseline_kib=baseline,
        rss_after_build_kib=marks["rss_after_build_kib"],
        rss_after_shape_kib=marks["rss_after_shape_kib"],
        rss_peak_kib=peak,
        rss_after_kib=after,
        tracemalloc_peak_kib=traced,
    )


# ---------------------------------------------------------------------------
# The parent: provision, grow the corpus, spawn one child per run
# ---------------------------------------------------------------------------


def provision(database: str) -> None:
    """Drop and recreate the scratch database, and load ``schema.sql`` into it."""
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    from jmfts_core.config import get_settings
    from jmfts_core.db_setup import apply_schema

    settings = get_settings()
    if settings.db_name != database:
        raise RuntimeError(
            f"JMFTS_DB_NAME is {settings.db_name!r} but this run wants {database!r}; the name "
            "has to be set before jmfts_core.config is first imported"
        )
    connection = psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database="postgres",
    )
    connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        cursor = connection.cursor()
        cursor.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        cursor.execute(f'CREATE DATABASE "{database}"')
    finally:
        connection.close()
    apply_schema()


def bind_shape(session) -> int:
    """Import the vocabulary and bind its shape to the scope. Returns the binding id."""
    from jmfts_client.contracts.rdf import ShapeBindingCreate
    from jmfts_core.rdf.names import DEFAULT_BASE_IRI
    from jmfts_core.services.ontology_service import OntologyService

    service = OntologyService(session)
    service.import_ontology(TURTLE, name=ONTOLOGY_NAME, base_iri=DEFAULT_BASE_IRI)
    binding = service.create_binding(
        ONTOLOGY_NAME,
        ShapeBindingCreate(shape_iri=SHAPE_IRI, usetype_pattern=SCOPE_USETYPE),
    )
    session.commit()
    return binding.id


def spawn_child(
    args: argparse.Namespace,
    *,
    binding_id: int,
    size: int,
    trace: bool,
    consecutive: int = 1,
) -> list[dict]:
    """Measured runs in a fresh interpreter. Returns one dict per run, in order.

    A new interpreter because a high-water mark never falls: a second run in this one would
    report the first one's peak and every size after the largest would look identical to it.
    ``VmHWM`` rather than ``ru_maxrss`` is what makes the child's number the child's own — see
    :func:`_peak_rss_kib`.

    ``consecutive`` is the deliberate exception: more than one run in ONE child, because a
    cpu worker is a long-lived process that claims the next task in the same interpreter, and
    a bound derived from a single run is only right if the allocator gives the graph back.
    Each run's ``rss_peak_kib`` is then the CUMULATIVE high-water mark of that process, which
    is the number the pod's memory limit is enforced against.
    """
    command = [
        sys.executable,
        "-m",
        "scripts.measure_shacl_scope",
        "--child",
        "--binding-id",
        str(binding_id),
        "--scope-documents",
        str(size),
        "--triples-per-document",
        str(args.triples_per_document),
        "--violation-fraction",
        str(args.violation_fraction),
        "--database",
        args.database,
    ]
    if trace:
        command.append("--trace")
    if consecutive != 1:
        command += ["--consecutive", str(consecutive)]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, "JMFTS_DB_NAME": args.database},
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"the measuring child failed at {size} documents (exit {completed.returncode}):\n"
            f"{completed.stderr[-4000:]}"
        )
    measured = [
        json.loads(line) for line in completed.stdout.strip().splitlines() if line.startswith("{")
    ]
    if len(measured) != consecutive:
        raise RuntimeError(
            f"the child at {size} documents printed {len(measured)} measurements, expected "
            f"{consecutive}:\n{completed.stdout[-2000:]}"
        )
    return measured


def _span(values: list[float]) -> str:
    """``min–median–max`` over the repeats, which is what a range means here."""
    if len(values) == 1:
        return f"{values[0]:.3g}"
    return f"{min(values):.3g}–{statistics.median(values):.3g}–{max(values):.3g}"


def report(rows: list[dict], repeats: list[list[Measurement]]) -> str:
    """The table, and where the run stops being comfortable."""
    lines = [
        "",
        f"budget: comfortable = peak RSS at or under the cpu worker's memory REQUEST "
        f"({WORKER_MEMORY_REQUEST_KIB // 1024} MiB, deploy/k8s/20-worker-cpu.yaml:67);",
        f"        impossible  = peak RSS past its LIMIT ({WORKER_MEMORY_LIMIT_KIB // 1024} "
        f"MiB, :73), where the retry dies the same way.",
        "",
        f"{'docs':>8} {'triples':>9} {'nodes':>8} {'viol':>7} "
        f"{'build s':>16} {'shape s':>16} {'pyshacl s':>16} "
        f"{'peak RSS MiB':>18} {'held MiB':>10}",
    ]
    for group in repeats:
        first = group[0]
        lines.append(
            f"{first.scope_documents:>8} {first.triple_count:>9} {first.node_count:>8} "
            f"{first.violation_count:>7} "
            f"{_span([m.build_graph_seconds for m in group]):>16} "
            f"{_span([m.shape_graph_seconds for m in group]):>16} "
            f"{_span([m.validate_seconds for m in group]):>16} "
            f"{_span([m.rss_peak_kib / 1024 for m in group]):>18} "
            f"{max(m.rss_after_kib for m in group) / 1024:>10.0f}"
        )
    comfortable = [
        g for g in repeats if max(m.rss_peak_kib for m in g) <= WORKER_MEMORY_REQUEST_KIB
    ]
    lines.append("")
    if not comfortable:
        lines.append(
            "NOTHING measured stayed inside the request. The bound is below the "
            "smallest size tried."
        )
    elif len(comfortable) == len(repeats):
        lines.append(
            f"every size tried stayed inside the request; the largest measured is "
            f"{comfortable[-1][0].scope_documents} documents at "
            f"{max(m.rss_peak_kib for m in comfortable[-1]) / 1024:.0f} MiB peak. The bound is "
            "ABOVE this ladder and the ladder is what needs extending, not the bound."
        )
    else:
        crossed = repeats[len(comfortable)][0]
        lines.append(
            f"comfortable up to {comfortable[-1][0].scope_documents} documents; "
            f"{crossed.scope_documents} crosses the request at "
            f"{max(m.rss_peak_kib for m in repeats[len(comfortable)]) / 1024:.0f} MiB peak."
        )
    return "\n".join(lines) + "\n"


def _write_json(path: Optional[str], rows: list[dict]) -> None:
    """Dump every run recorded so far, with the budget they are read against."""
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "budget_kib": {
                    "request": WORKER_MEMORY_REQUEST_KIB,
                    "limit": WORKER_MEMORY_LIMIT_KIB,
                },
                "runs": rows,
            },
            handle,
            indent=2,
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--binding-id", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--scope-documents", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--trace", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--consecutive",
        type=int,
        default=1,
        help="after the last rung, run this many validations of it in ONE process, to measure "
        "what a long-lived worker keeps between tasks. 1 (the default) skips the check",
    )
    parser.add_argument(
        "--sizes",
        default="100,1000,10000,50000",
        help="comma-separated scope sizes in documents, ascending; the corpus grows to each",
    )
    parser.add_argument(
        "--triples-per-document",
        type=int,
        default=5,
        help="default 5 — Settings.extraction_max_facts (config.py:256), the appliance's own "
        "maximum triples per segment, and a segment is a document row",
    )
    parser.add_argument("--violation-fraction", type=float, default=0.5)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--database", default=_DEFAULT_DATABASE)
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="do not drop the scratch database at the end",
    )
    args = parser.parse_args(argv)

    if not args.database.endswith(_DATABASE_SUFFIX):
        raise SystemExit(
            f"--database must end in {_DATABASE_SUFFIX!r}; this script DROPS what it is "
            f"pointed at and {args.database!r} could be an appliance"
        )
    os.environ["JMFTS_DB_NAME"] = args.database
    # THE BOUND THIS SCRIPT MEASURED IS NOW ENFORCED, so the script has to lift it or refuse
    # itself at its second rung. `Settings.shacl_max_scope_documents` defaults to 512 documents
    # (open question 6.3, taken 2026-09-10) and `_pin_scope` raises `ScopeTooLargeError` past
    # it. Set rather than defaulted-past in code: a measurement run says out loud that it is
    # asking for scopes no appliance would accept unconfigured. `setdefault`, so an operator
    # measuring a particular bound can still name one.
    os.environ.setdefault("JMFTS_SHACL_MAX_SCOPE_DOCUMENTS", "1000000")

    if args.child:
        # The loop is what makes `--consecutive` mean anything: the runs share an interpreter,
        # so each `rss_peak_kib` is the high-water mark of everything this process has done.
        for index in range(args.consecutive):
            measurement = measure_once(
                binding_id=args.binding_id,
                scope_documents=args.scope_documents,
                triples_per_document=args.triples_per_document,
                violation_fraction=args.violation_fraction,
                trace=args.trace,
            )
            record = asdict(measurement)
            record["consecutive_index"] = index
            print(json.dumps(record), flush=True)
        return 0

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    if sizes != sorted(sizes):
        raise SystemExit("--sizes must be ascending; the corpus grows from one size to the next")

    provision(args.database)

    from jmfts_core.database import get_session

    with get_session() as session:
        binding_id = bind_shape(session)

    grown = 0
    repeats: list[list[Measurement]] = []
    rows: list[dict] = []
    for size in sizes:
        with get_session() as session:
            generate_corpus(
                session,
                first=grown,
                count=size - grown,
                triples_per_document=args.triples_per_document,
                violation_fraction=args.violation_fraction,
            )
        grown = size
        group: list[Measurement] = []
        for repeat in range(args.repeats):
            raw = spawn_child(args, binding_id=binding_id, size=size, trace=False)[0]
            raw["repeat"] = repeat
            rows.append(raw)
            group.append(
                Measurement(
                    **{k: v for k, v in raw.items() if k not in ("repeat", "consecutive_index")}
                )
            )
            print(
                f"  {size:>7} docs  repeat {repeat}: "
                f"build {group[-1].build_graph_seconds:.2f}s  "
                f"shape {group[-1].shape_graph_seconds:.2f}s  "
                f"pyshacl {group[-1].validate_seconds:.2f}s  "
                f"peak {group[-1].rss_peak_kib / 1024:.0f} MiB",
                flush=True,
            )
        traced = spawn_child(args, binding_id=binding_id, size=size, trace=True)[0]
        traced["repeat"] = "tracemalloc"
        rows.append(traced)
        print(
            f"  {size:>7} docs  tracemalloc: "
            f"{traced['tracemalloc_peak_kib'] / 1024:.0f} MiB of Python objects "
            f"(peak RSS in that run {traced['rss_peak_kib'] / 1024:.0f} MiB)",
            flush=True,
        )
        repeats.append(group)
        if size == sizes[-1] and args.consecutive > 1:
            # The long-lived-worker check, and only at the top of the ladder: what it is
            # asking is whether the allocator gives the largest graph back, and a small scope
            # cannot answer that.
            for run in spawn_child(
                args, binding_id=binding_id, size=size, trace=False, consecutive=args.consecutive
            ):
                run["repeat"] = f"consecutive-{run['consecutive_index']}"
                rows.append(run)
                print(
                    f"  {size:>7} docs  consecutive run {run['consecutive_index'] + 1} of "
                    f"{args.consecutive} in ONE process: cumulative peak "
                    f"{run['rss_peak_kib'] / 1024:.0f} MiB, held {run['rss_after_kib'] / 1024:.0f}"
                    " MiB",
                    flush=True,
                )
        # Written after EVERY rung, not once at the end. This runs against a shared working
        # tree: a rung that dies because another lane's half-saved module was on disk when a
        # child imported it must not take the rungs that already succeeded with it. What it
        # costs is a rewrite of a small file per rung.
        _write_json(args.json_path, rows)

    text = report(rows, repeats)
    print(text)
    _write_json(args.json_path, rows)
    if args.json_path:
        print(f"wrote {args.json_path}")

    if not args.keep:
        import psycopg2
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

        from jmfts_core.config import get_settings
        import jmfts_core.database as database_module

        if database_module._engine is not None:
            database_module._engine.dispose()
        settings = get_settings()
        connection = psycopg2.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            database="postgres",
        )
        connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        try:
            connection.cursor().execute(f'DROP DATABASE IF EXISTS "{args.database}" WITH (FORCE)')
        finally:
            connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
