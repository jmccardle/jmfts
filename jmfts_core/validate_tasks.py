"""Running a bound shape against the data it is bound to. ``SPRINT_0_5_0.md`` Block A steps 2 and 3.

``pyproject.toml:208`` declares ``pyshacl`` "although NOTHING CALLS IT YET". This module is
that caller, and it reaches the library the only way this appliance permits: through
:func:`~jmfts_core.rdf.require_pyshacl`, at the point of use, never at module scope.

**A task, not a request handler**, and Block A's reason is the one that decides it: a
validation run over a bound scope is the same shape of work as ``profile:sheet`` — bounded,
restartable, worth retrying — which is what :func:`register_task_handler` covers here. The
request that asks for one mints the node the report will land on and enqueues; nothing
validates inside a request.

**NOT A ROW OF** ``TASK_ROWS``. See :data:`~jmfts_core.ingest_tasks.TASK_VALIDATE_SHAPE`:
that table is evaluated from what ``probe`` measured, and nothing probe measures decides
whether somebody asked for a validation run.

**VALIDATION NEVER MUTATES, and everything here is arranged around keeping that true.**
``services/ontology_service.py:11`` already says an uploaded shape "constrains nothing, and
even bound it changes no existing row"; Block A keeps it. A violation is reported and the row
stays. Concretely:

* no ``Triple``, ``DocumentLink`` or source ``Document`` is written — the only row this task
  changes is the report node it is scoped to;
* ``pyshacl`` is called with ``inference="none"`` and ``advanced=False``, so no triple is
  inferred into the data graph and no ``sh:rule`` fires. Rules are Block B, on purpose:
  ``sh:rule`` WRITES, and it writes under a column (``derived_by``) built for the purpose;
* **the report node has no parent, and that is a correctness decision rather than a filing
  one.** The worker walks up from every completed task's scope node
  (``ingest_worker.py:240``) and asks the rollup planner at each level
  (``settling.py:225``); a report node filed under a shared root would therefore offer that
  root a ``summarize``, and a validation run would end by spending an LLM call on a container
  of unrelated reports. A node with no parent ends the walk at itself. What that costs, and
  what would buy it back, is in :data:`REPORT_PARENT_REASON`.

**Access follows the DATA, not the vocabulary.** The report is a derived artefact of the
documents in the binding's scope, so it inherits their access —
``SPRINT_0_5_0.md`` 3.4's rule, and the second reason ``014_entity_roots.sql`` keys a derived
root by access. The request is what enforces it (``OntologyService.validate_binding``): it
resolves the scope under the caller's filter, refuses a scope whose documents are not
governed identically, and gives the report node grants matching that one access key. This
module inherits the consequence rather than re-deriving it: **the document ids it validates
are on the task row**, pinned when the request ran, because a worker holds no principal and
re-resolving the scope here would widen it to everything the appliance can see.

**Open question 6.5, taken as recorded: the report node carries the RUN'S TIMESTAMP and the
caller compares.** No expiry, no background revalidation, no invalidation when a triple
changes. ``validated_at`` is in the report block and in the rendered text, so "is this report
still about the data I have?" is a question a caller can ask and this appliance does not
answer for them.

**Open question 6.2, taken as recorded: a scope artefact is MARKED, never suppressed.** A
shape whose target class sits outside the bound reports a violation that is an artefact of
the bound; :meth:`~jmfts_core.rdf.shacl.ScopeBoundGraph.is_scope_artefact` is what marks it,
and every violation carries ``scope_artefact`` plus the properties the focus node lost at the
boundary. A marked artefact is recoverable and a suppressed violation is not.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from jmfts_core.atoms import COST_CPU, EV_TEXT
from jmfts_core.ingest_tasks import TASK_VALIDATE_SHAPE, TaskOutcome, register_task_handler
from jmfts_core.models.document import Document
from jmfts_core.models.ontology import Ontology, ShapeBinding
from jmfts_core.models.task_queue import TaskQueue, WRITE_SELF
from jmfts_core.rdf import require_pyshacl, require_rdflib
from jmfts_core.rdf.names import STANDARD_PREFIXES, document_iri
from jmfts_core.rdf.shacl import bound_shape_graph, build_data_graph

logger = logging.getLogger(__name__)

#: The usetype a violation report carries. A node kind of its own, because a report is
#: neither a summary of a document nor a piece of one: it is a statement ABOUT a set of them,
#: at a time.
#:
#: DECLARED HERE AND NOT ON THE MODEL, following ``jmfts_core/derived_roots.py:60``
#: (``DERIVED_ROOT_USETYPE``). ``models/document.py:36`` says usetypes live on the model
#: because thirteen modules each declaring ``USETYPE_SHEET = "sheet"`` is copy-drift; that
#: argument is about a name SHARED by several modules, and this one is minted, read and
#: filtered in exactly one place. The day a second module needs it, it moves.
VALIDATION_REPORT_USETYPE = "validation:report"

#: Where the request record and the report both live on that node: one key in
#: ``structured_content``, holding what was asked for and — after the run — what was found.
#:
#: **``structured_content`` and not an evidence row, and the cost is real.** The protected
#: store is ``document_evidence``, whose names are closed by ``jmfts_core.atoms.EVIDENCE``
#: and typed by ``jmfts_core.evidence.REGISTRY``; a ``validation`` row there is the right
#: home and it needs an entry in BOTH, which is a change to a module this work did not own.
#: What that costs until it happens is the defect ``EV_RECORD`` was added for
#: (``atoms.py:145``): a metadata ``PATCH`` REPLACES this column
#: (``repositories/document.py:374``), so a writer can delete the machine-readable half of a
#: report. The half that survives is the one a reader searches — the rendered text is
#: ``Document.content``, which is the registered evidence name ``text`` and is what this atom
#: declares it produces.
VALIDATION_BLOCK = "validation"

#: Why a report node is top-level, stated where somebody who finds a parentless node will
#: look for it. The alternative is a derived root keyed by access
#: (``jmfts_core/derived_roots.py``, migration ``018``), which is the right home and is
#: unreachable until the rollup planner stops offering ``summarize`` to nodes that are not
#: ingest trees — see the module docstring and ``rollup_tasks.IngestRollupPlanner``.
REPORT_PARENT_REASON = (
    "a validation report has no parent so that the settling walk stops at it: the walk asks "
    "the rollup planner at every level above a completed task's scope node, and a report "
    "filed under a shared root would offer that root a summarize. The report's access is "
    "carried by grants on the node itself, matching the access of the scope it reports on "
    "(SPRINT_0_5_0.md 3.4)"
)

#: What the task row carries. The document ids are the whole reason this is a list of ids
#: rather than the binding's scope columns: the request resolved them under the CALLER'S
#: access filter, and a worker holds no principal
#: (``jmfts_core/access.py`` — an unbound caller bypasses every check), so re-resolving here
#: would validate documents the caller cannot read and report violations naming them.
PARAM_BINDING_ID = "binding_id"
PARAM_DOCUMENT_IDS = "document_ids"
PARAM_PROVENANCE = "provenance"
PARAM_BASE_IRI = "base_iri"

_SHACL = STANDARD_PREFIXES["sh"]

#: How many violations the RENDERED report lists. The machine-readable half
#: (``structured_content``) carries every one of them and is not capped.
#:
#: **The cap is about an index, not about a reader's patience.** MEASURED at the memory bound
#: (138,000 documents, 69,000 violations, ``docs/MEASURE_SHACL_SCOPE.md``): the rendered
#: report was **11,688,300 characters**, and ``idx_documents_content_fts``
#: (``sql/schema.sql:473``) is a partial GIN index over ``to_tsvector('english', title || ' '
#: || content)``. Its lexeme total measured 358,814 bytes there against Postgres's
#: 1,048,575-byte ceiling, which INFERRED at 2.724 bytes/document is reached at roughly
#: 190,000 documents on a fully-violating scope — and `string is too long for tsvector` is a
#: ``DataError``, classified PERMANENT, arriving on the LAST write of a run that already spent
#: a minute. ``SPRINT_0_5_0.md`` Block A finding 6.
#:
#: 512 violations is two orders of magnitude inside that ceiling at the measured ~170
#: characters per rendered violation. Open question 6.3's 512-document default makes this
#: unreachable in ordinary operation — a 512-document scope cannot easily produce 512
#: violations — and the cap stays because an operator raises that bound and this one does not
#: move with it.
RENDERED_VIOLATION_CAP = 512


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _term(value) -> Optional[str]:
    return None if value is None else str(value)


def _named(rdflib, value) -> Optional[str]:
    """``str(value)`` for an IRI, ``None`` for a blank node or a missing term."""
    return str(value) if isinstance(value, rdflib.URIRef) else None


def _violations(rdflib, results_graph, bound) -> list[dict]:
    """Every ``sh:ValidationResult`` as a plain dict, marked for open question 6.2.

    ``scope_artefact`` is over-inclusive by design — a focus node that lost an UNRELATED
    triple at the boundary answers true — because 6.2's recorded default is to mark rather
    than suppress, and an under-inclusive mark destroys the evidence a reader needs.
    ``cut_paths`` is the sharper answer beside it: when a violation's ``result_path`` is one
    of them, the violation is an artefact of the bound with near-certainty.
    """
    sh = rdflib.Namespace(_SHACL)
    out: list[dict] = []
    for result in results_graph.subjects(rdflib.RDF.type, sh.ValidationResult):
        focus = _term(results_graph.value(result, sh.focusNode))
        value = _term(results_graph.value(result, sh.value))
        cut_paths = list(bound.cuts_at(focus))
        out.append(
            {
                "focus_node": focus,
                "result_path": _term(results_graph.value(result, sh.resultPath)),
                "value": value,
                "severity": _term(results_graph.value(result, sh.resultSeverity)),
                "constraint": _term(results_graph.value(result, sh.sourceConstraintComponent)),
                # Named shapes only. A property shape written inline is a blank node, and a
                # blank node's id is "an artefact of this parse rather than a name the
                # document gave it" (``rdf/parse.py``, refusing a blank-node NodeShape for the
                # same reason) — reporting it would put a value in the report that changes
                # between two runs over identical data.
                "source_shape": _named(rdflib, results_graph.value(result, sh.sourceShape)),
                "message": _term(results_graph.value(result, sh.resultMessage)),
                "scope_artefact": bound.is_scope_artefact(focus, value),
                "cut_paths": cut_paths,
            }
        )
    # Deterministic: two runs over the same data must produce the same document, or a
    # caller diffing two reports reads rdflib's set ordering as a change in the data.
    out.sort(
        key=lambda v: tuple(
            "" if v[k] is None else v[k]
            for k in ("focus_node", "result_path", "constraint", "message")
        )
    )
    return out


def _render(report: dict, request: dict) -> str:
    """The report as markdown — the node's ``content``, and what a full-text search finds.

    Text and not JSON because ``Document.content`` is what ``ts_vector`` reads: "which
    reports mention this document, or this constraint" is a search, and a JSON blob in the
    same column would answer it by accident at best.

    **The list is capped at :data:`RENDERED_VIOLATION_CAP` and the text SAYS SO.** The full
    list is in ``structured_content``, which no index reads; what is capped is the half that
    goes through ``to_tsvector`` (Block A finding 6). A truncated report that did not say it
    was truncated would be worse than the row it prevents — it would read as "these are the
    violations" when it is "these are the first 512" — so the header counts them all and the
    cut is a line of its own naming where the rest is.
    """
    lines = [
        f"# SHACL validation — {request['shape_iri']}",
        "",
        f"- ontology: `{request['ontology_name']}`",
        f"- binding: {request['binding_id']} ({request['scope_type']} scope)",
        f"- validated at: {report['validated_at']}",
        f"- documents in scope: {report['documents_resolved']} "
        f"of {report['documents_requested']} pinned when the run was requested",
        f"- data graph: {report['triple_count']} triples over {report['node_count']} nodes "
        f"({request['provenance']} provenance)",
        f"- boundary cuts: {report['boundary_cuts']}",
        f"- conforms: {report['conforms']}",
        f"- violations: {report['violation_count']} "
        f"({report['scope_artefact_count']} possibly artefacts of the scope bound)",
        "",
    ]
    if not report["violations"]:
        lines.append("No violation was reported.")
        return "\n".join(lines)
    total = len(report["violations"])
    shown = report["violations"][:RENDERED_VIOLATION_CAP]
    lines.append("## Violations")
    lines.append("")
    if len(shown) < total:
        lines.append(
            f"**Listing the first {len(shown)} of {total} violations.** The remaining "
            f"{total - len(shown)} are not shown here and are not lost: every one of them is "
            f"in this node's `structured_content['{VALIDATION_BLOCK}']['report']['violations']`, "
            "which is what a caller reads programmatically. The rendered list is capped "
            "because this text is full-text indexed and a large enough report exceeds "
            "Postgres's tsvector ceiling, which would fail the run on its last write."
        )
        lines.append("")
    for violation in shown:
        mark = " *(possible scope artefact)*" if violation["scope_artefact"] else ""
        lines.append(
            f"- `{violation['focus_node']}` — {violation['constraint']}"
            f" on `{violation['result_path']}`{mark}"
        )
        if violation["message"]:
            lines.append(f"  - {violation['message']}")
        if violation["value"] is not None:
            lines.append(f"  - value: `{violation['value']}`")
        if violation["cut_paths"]:
            lines.append(f"  - lost at the scope boundary: {', '.join(violation['cut_paths'])}")
    return "\n".join(lines)


@register_task_handler(
    TASK_VALIDATE_SHAPE,
    # Nothing. The inputs are the shape binding, the ontology's Turtle and the triples the
    # scope resolves to — a table, a column and a table. None of them is evidence on a node,
    # and naming one that is not would put this task in the audit's derivation for a fact
    # nobody writes. `index:bm25` declares `produces=()` for the mirror-image reason.
    consumes=(),
    # The rendered report, in `Document.content` — which is what the evidence name `text`
    # IS (`jmfts_core/evidence.py`: store column `content`). The machine-readable half goes
    # to `structured_content`, which no evidence name covers; see `VALIDATION_BLOCK`.
    produces=(f"{EV_TEXT}@self",),
    # `self`: the only row this task writes is the report node it is scoped to. It creates
    # no child and touches nothing in any tree it validates — which is the property the
    # module docstring is about.
    write_mode=WRITE_SELF,
    # `cpu`. pyshacl is a graph walk: no weights, no GPU, no LLM. The report node is
    # deliberately not embedded (there is no `embed` task on it), so no model runs anywhere
    # in this task's reach and a badge sized on this class is sized correctly.
    cost_class=COST_CPU,
)
def run_validate_shape(session: Session, task: TaskQueue) -> TaskOutcome:
    """Validate one bound shape against its scope and write the report onto this node.

    The node is the report — minted by the request, in flight until this runs, and settled by
    the walk afterwards. Everything the run needs is on the task row: which binding, which
    documents (pinned under the requesting principal's access filter), which provenance layer,
    and which base IRI the document names were minted under.

    Raises:
        RdfStackNotInstalled: this install has no ``pyshacl``. PERMANENT, so the node ends
            ``failed`` with the reason and the extra named, rather than the request being
            refused by an API process that may not be the one that would have run it.
        LookupError: the report node, the binding or its ontology is gone.
        ShapeNotInOntologyError: the vocabulary was replaced and no longer declares the bound
            shape. A ``ValueError``, so it classifies PERMANENT — a shape does not reappear on
            the third attempt (``SPRINT_0_5_0.md`` Block B finding 6).
        ValueError: the task row does not carry what a run needs.
    """
    node = session.get(Document, task.scope_document_id)
    if node is None:
        raise LookupError(
            f"{TASK_VALIDATE_SHAPE} is scoped to document {task.scope_document_id}, which "
            "does not exist; the report node was deleted while the task was queued"
        )
    if node.usetype != VALIDATION_REPORT_USETYPE:
        raise ValueError(
            f"{TASK_VALIDATE_SHAPE} is scoped to document {node.id}, whose usetype is "
            f"{node.usetype!r} and not {VALIDATION_REPORT_USETYPE!r}; this task writes a "
            "violation report and has no meaning on any other node"
        )

    params = task.params or {}
    for key in (PARAM_BINDING_ID, PARAM_DOCUMENT_IDS, PARAM_PROVENANCE, PARAM_BASE_IRI):
        if key not in params:
            raise ValueError(
                f"{TASK_VALIDATE_SHAPE} task {task.id} carries no {key!r}; the request is "
                "what resolves a binding's scope, under the caller's access filter, and a "
                "worker cannot re-resolve it because it holds no principal"
            )
    binding_id = int(params[PARAM_BINDING_ID])
    document_ids = [int(i) for i in params[PARAM_DOCUMENT_IDS]]

    binding = session.get(ShapeBinding, binding_id)
    if binding is None:
        raise LookupError(
            f"shape binding {binding_id} no longer exists; it was deleted between the "
            "request and this run"
        )
    ontology = session.get(Ontology, binding.ontology_name)
    if ontology is None:
        raise LookupError(
            f"ontology {binding.ontology_name!r} no longer exists; binding {binding_id} "
            "names a vocabulary that has been deleted"
        )

    # The first caller `pyproject.toml:208` was waiting for. Before the graph is built, so an
    # install with no validator says so without reading a triple.
    pyshacl = require_pyshacl()
    rdflib = require_rdflib()

    bound = build_data_graph(
        session,
        scope_type="documents",
        scope={"document_ids": document_ids},
        # None, and it is not a default this module chose: the request already applied
        # whatever bound the operator configured, and re-applying one here would refuse a
        # run the request accepted. Open question 6.3's setting is Block A step 5's.
        max_scope_documents=None,
        provenance=params[PARAM_PROVENANCE],
        base_iri=params[PARAM_BASE_IRI],
    )
    # The shapes graph is built in `rdf/shacl.py`, by the same function `derive:rule` calls:
    # "only the bound shape runs" is one computation, and this module and `shacl_rules.py`
    # each had their own until Block B finding 4. Every OTHER shape's targets are stripped, so
    # a second shape in the vocabulary selects no focus node and reports nothing, while
    # remaining reachable as a named property shape, an `sh:node` or an `sh:condition`.
    shapes, _stripped = bound_shape_graph(
        rdflib,
        source_turtle=ontology.source_turtle,
        ontology_name=ontology.name,
        shape_iri=binding.shape_iri,
        target_iris=[
            document_iri(doc_id, params[PARAM_BASE_IRI]) for doc_id in bound.scope_document_ids
        ],
    )

    conforms, results_graph, _results_text = pyshacl.validate(
        data_graph=bound.graph,
        shacl_graph=shapes,
        # No inference and no rules: a validation run adds no triple to the graph it read,
        # and `sh:rule` is Block B, which writes under `Triple.derived_by`.
        inference="none",
        advanced=False,
        abort_on_first=False,
        meta_shacl=False,
        do_owl_imports=False,
    )

    violations = _violations(rdflib, results_graph, bound)
    request = dict((node.structured_content or {}).get(VALIDATION_BLOCK) or {})
    request.update(
        {
            "binding_id": binding_id,
            "ontology_name": binding.ontology_name,
            "shape_iri": binding.shape_iri,
            "scope_type": binding.scope_type,
            "provenance": params[PARAM_PROVENANCE],
            "base_iri": params[PARAM_BASE_IRI],
        }
    )
    report = {
        # Open question 6.5, as recorded: the run's timestamp, and the caller compares.
        # Nothing here expires a report and nothing revalidates one in the background.
        "validated_at": _utc_now_iso(),
        "conforms": bool(conforms),
        "violation_count": len(violations),
        "scope_artefact_count": sum(1 for v in violations if v["scope_artefact"]),
        "documents_requested": len(document_ids),
        # Not the same number when a document was deleted between the request and the run.
        # Reported rather than reconciled: a report over a scope that shrank is still a
        # report, and a reader is owed the difference.
        "documents_resolved": len(bound.scope_document_ids),
        "triple_count": bound.triple_count,
        "node_count": bound.node_count,
        "boundary_cuts": len(bound.boundary_cuts),
        "violations": violations,
    }
    node.structured_content = {
        **(node.structured_content or {}),
        VALIDATION_BLOCK: {**request, "report": report},
    }
    node.content = _render(report, request)
    session.flush()

    return TaskOutcome(
        detail={
            "binding_id": binding_id,
            "shape_iri": binding.shape_iri,
            "conforms": report["conforms"],
            "violations": report["violation_count"],
            "scope_artefacts": report["scope_artefact_count"],
            "documents": report["documents_resolved"],
            "triples": report["triple_count"],
            "nodes": report["node_count"],
            "boundary_cuts": report["boundary_cuts"],
            "validated_at": report["validated_at"],
        },
    )


__all__ = [
    "PARAM_BASE_IRI",
    "PARAM_BINDING_ID",
    "PARAM_DOCUMENT_IDS",
    "PARAM_PROVENANCE",
    "RENDERED_VIOLATION_CAP",
    "REPORT_PARENT_REASON",
    "VALIDATION_BLOCK",
    "VALIDATION_REPORT_USETYPE",
    "run_validate_shape",
]
