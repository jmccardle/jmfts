"""Running one binding's ``sh:rule`` set and storing what it concluded. Block B steps 6–8.

The task half of :mod:`jmfts_core.shacl_rules`, and the mirror of
:mod:`jmfts_core.validate_tasks`: same request-mints-a-node-and-enqueues shape, same pinned
scope, same top-level report node — and the opposite relationship to the store, because this
one WRITES.

**A task and not a request handler**, for Block A step 2's reason unchanged: a rule pass over
a bound scope is bounded, restartable work, which is what ``register_task_handler`` covers
here. It is also the reason the writing half is safe to retry — :func:`apply_derivation`
deletes this rule's whole output before rebuilding it, so a second attempt after a crashed
first one converges on the same rows rather than doubling them.

**NOT A ROW OF** ``TASK_ROWS``. See :data:`~jmfts_core.ingest_tasks.TASK_DERIVE_RULE`; the
argument is ``validate:shape``'s, and it is the same argument because it is the same
situation: Part 4's table is evaluated from what ``probe`` measured, and nothing probe
measures says whether somebody bound a shape carrying rules.

**THE INPUT IS THE ASSERTED LAYER AND THE CALLER CANNOT WIDEN IT.** ``provenance`` is fixed
at ``"asserted"`` here and there is no request field for it. Block B's restriction — one pass,
no chaining — is not a property of how many times this module calls ``pyshacl``; it is a
property of what the graph is built from. A caller who could pass ``"any"`` would be asking a
rule to read another rule's output, which is the fixpoint the block declined.

**WHAT THIS TASK MAY WRITE.** Rows in ``triples``, all carrying this rule's identity in
``derived_by``, plus the report node it is scoped to. Nothing else: no document in the scope
is touched, no link is written, and no predicate is minted (see
:class:`~jmfts_core.shacl_rules.PredicateNotRegisteredError`). A run that cannot map every
derived triple onto a storable row writes NOTHING — the mapping is complete before the delete
begins.

**Open question 6.4, taken as recorded: derivation requires WRITE access to the binding's
scope.** ``docs/SPRINT_0_5_0.md`` 6.4 calls it "the narrowest defensible rule and the easiest
to widen later", and it needs no strengthening argument here the way it did for validation:
this run writes facts about the scope's documents.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from jmfts_core.atoms import COST_CPU, EV_TEXT
from jmfts_core.ingest_tasks import TASK_DERIVE_RULE, TaskOutcome, register_task_handler
from jmfts_core.models.document import Document
from jmfts_core.models.ontology import Ontology, ShapeBinding
from jmfts_core.models.task_queue import TaskQueue, WRITE_SELF
from jmfts_core.rdf import require_pyshacl, require_rdflib
from jmfts_core.rdf.names import document_iri
from jmfts_core.rdf.shacl import bound_shape_graph, build_data_graph
from jmfts_core.shacl_rules import (
    ShapeDeclaresNoRuleError,
    apply_derivation,
    declares_a_rule,
    expand_rules,
    map_to_rows,
    rule_identity,
)

logger = logging.getLogger(__name__)

#: The usetype a derivation report carries. Its own kind, for the reason
#: ``validation:report`` is: a report is a statement ABOUT a set of documents at a time,
#: rather than a summary of one or a piece of one. Declared here and not on the model,
#: following ``jmfts_core/derived_roots.py:60``; the day a second module reads it, it moves.
DERIVATION_REPORT_USETYPE = "derivation:report"

#: The one key in ``structured_content`` holding what was asked for and what was concluded.
#:
#: ``structured_content`` and not an evidence row, and it carries the cost
#: ``validate_tasks.VALIDATION_BLOCK`` names: a metadata ``PATCH`` REPLACES this column
#: (``repositories/document.py:374``), so a writer can delete the machine-readable half of a
#: report. **What that costs HERE is less than it costs there**, and the difference is worth
#: stating: the durable record of what this run did is the ``triples`` rows themselves, which
#: carry ``derived_by`` and are reachable by ``WHERE derived_by = :rule`` with no report at
#: all. The report is how a reader finds out what the identity MEANS.
DERIVATION_BLOCK = "derivation"

#: Why a report node is top-level. The same reason ``validate_tasks.REPORT_PARENT_REASON``
#: gives, restated rather than imported: ``jmfts_core.validate_tasks`` is a task module that
#: imports ``ingest_tasks``, which imports both task modules from its own foot, so a task
#: module that imported its sibling would make the import order load-bearing.
REPORT_PARENT_REASON = (
    "a derivation report has no parent so that the settling walk stops at it: the walk asks "
    "the rollup planner at every level above a completed task's scope node, and a report "
    "filed under a shared root would offer that root a summarize — an LLM call over a "
    "container of unrelated reports. The report's access is carried by grants on the node "
    "itself, matching the access of the scope it derived from (SPRINT_0_5_0.md 3.4)"
)

#: What the task row carries. ``document_ids`` is pinned by the request under the CALLER'S
#: access filter, for ``validate:shape``'s reason: a worker holds no principal and an unbound
#: caller bypasses every check, so a handler that re-resolved the binding's scope would derive
#: over every document in the appliance. ``rule`` is pinned for a second reason on top of
#: that — it is the delete set of step 8, and the response already told the caller what it is.
PARAM_BINDING_ID = "binding_id"
PARAM_DOCUMENT_IDS = "document_ids"
PARAM_BASE_IRI = "base_iri"
PARAM_RULE = "rule"

#: The layer a rule reads. Fixed, and see the module docstring.
DERIVATION_PROVENANCE = "asserted"

#: How many derived triples the RENDERED report lists. The machine-readable half
#: (``structured_content``) carries every one of them and is not capped.
#:
#: **Its twin is ``validate_tasks.RENDERED_VIOLATION_CAP`` and it is deliberately not the
#: same constant.** The two modules do not import each other — ``REPORT_PARENT_REASON``
#: above restates its sibling's wording rather than importing it, because both are loaded
#: from ``ingest_tasks``'s foot and a sibling import would make that order load-bearing —
#: and a shared knob would be wrong in one of the two places the moment either is tuned,
#: because the two lists do not grow the same way against the same 512-document scope bound.
#: A violation list is roughly linear in the scope: one focus node can only fail a shape's
#: constraints so many times. A derived list is bounded by the RULE'S query, not by the
#: scope — ``shacl_rules.map_to_rows`` requires both endpoints in scope, so a ``sh:SPARQLRule``
#: relating every pair of in-scope documents produces 512 × 512 = 262,144 rows from the
#: same bound that yields a few hundred violations.
#:
#: **And unlike the validation cap, this one is reachable at the default scope bound.**
#: MEASURED 2026-09-10 against ``pgvector/pgvector:pg16`` — the same ceiling
#: ``SPRINT_0_5_0.md`` Block A finding 6 measured, ``idx_documents_content_fts``
#: (``sql/schema.sql``) being a partial GIN over ``to_tsvector('english', title || ' ' ||
#: content)``, whose lexeme string is capped at 1,048,575 bytes — rendering derived rows in
#: this function's format:
#:
#: * objects that are DOCUMENTS: the lexeme vocabulary is bounded by the scope, so the
#:   tsvector plateaus at 15,998 bytes and stays there through all 262,144 rows. It cannot
#:   reach the ceiling. The ``content`` string is still 11,534,335 characters.
#: * objects that are LITERALS: a rule can ``CONCAT``, so every row can carry a lexeme
#:   nothing else carries. 105,000 rows indexed at 1,457,226 bytes; 110,000 raised `string
#:   is too long for tsvector` at 1,087,198 bytes of lexeme string. The crossing is near
#:   106,000 rows and moves with how much distinct text each literal holds.
#:
#: So the answer finding 6 gave for violations — "the 512-document default makes the cap
#: unreachable, and it is correct anyway for the operator who raises that bound" — does NOT
#: transfer. 106,000 literal-object rows is a rule an operator can write today, at the
#: shipped default, and it buys a ``DataError`` classified PERMANENT on the LAST write of a
#: run that already spent a minute.
#:
#: 512 and not another number because it is what an ordinary run produces: a ``sh:TripleRule``
#: concludes one triple per focus node, so a full scope at the default bound is 512 rows and
#: is listed COMPLETE. What gets cut is the rule that went quadratic — which is exactly the
#: run whose reader needs to be told the list is partial.
RENDERED_TRIPLE_CAP = 512


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _render(report: dict, request: dict) -> str:
    """The report as markdown — the node's ``content``, and what a full-text search finds.

    Text and not JSON for ``validate_tasks._render``'s reason: ``Document.content`` is what
    ``ts_vector`` reads, and "which run wrote this fact" is a search.

    **The list is capped at :data:`RENDERED_TRIPLE_CAP` and the text SAYS SO.** The full list
    is in ``structured_content``, which no index reads; what is capped is the half that goes
    through ``to_tsvector`` (``SPRINT_0_5_0.md`` Block A finding 6, and the constant for the
    measurement that says a derivation reaches that ceiling where a validation does not). A
    truncated report that did not say it was truncated would read as "these are the triples
    the rule concluded" when it is "these are the first 512" — and here that misreading has a
    second edge the validation side does not have, because the report is also how a reader
    finds out what an opaque ``derived_by`` digest MEANS. So the header counts them all and
    the cut is a line of its own naming where the rest is.
    """
    lines = [
        f"# SHACL derivation — {request['shape_iri']}",
        "",
        f"- ontology: `{request['ontology_name']}`",
        f"- binding: {request['binding_id']} ({request['scope_type']} scope)",
        f"- rule: `{report['rule']}`",
        f"- derived at: {report['derived_at']}",
        f"- documents in scope: {report['documents_resolved']} "
        f"of {report['documents_requested']} pinned when the run was requested",
        f"- data graph: {report['triple_count']} asserted triples over "
        f"{report['node_count']} nodes",
        f"- boundary cuts: {report['boundary_cuts']}",
        f"- shapes disarmed so that only the bound one ran: {report['targets_stripped']}",
        f"- concluded: {report['candidates']} triples "
        f"({report['inserted']} written, {report['already_present']} already held)",
        f"- rows this rule had written before and that were replaced: {report['deleted']}",
        "",
    ]
    if not report["triples"]:
        lines.append("The rules ran against the scope and concluded nothing.")
        return "\n".join(lines)
    total = len(report["triples"])
    shown = report["triples"][:RENDERED_TRIPLE_CAP]
    lines.append("## Derived")
    lines.append("")
    if len(shown) < total:
        lines.append(
            f"**Listing the first {len(shown)} of {total} derived triples.** The remaining "
            f"{total - len(shown)} are not shown here and are not lost: every one of them is "
            f"in this node's `structured_content['{DERIVATION_BLOCK}']['report']['triples']`, "
            f"and every one of them is a row in `triples` carrying `derived_by = "
            f"{report['rule']}`, which is what a caller reads programmatically. The rendered "
            "list is capped because this text is full-text indexed and a large enough report "
            "exceeds Postgres's tsvector ceiling, which would fail the run on its last write."
        )
        lines.append("")
    for row in shown:
        obj = (
            f"document {row['object_id']}"
            if row["object_id"] is not None
            else f"`{row['object_literal']}`"
        )
        lines.append(f"- document {row['subject_id']} —predicate {row['predicate_id']}→ {obj}")
    return "\n".join(lines)


@register_task_handler(
    TASK_DERIVE_RULE,
    # Nothing, in the sense the atom vocabulary means. The inputs are a binding row, an
    # ontology's Turtle and the asserted triples the scope resolves to — a table, a column
    # and a table, none of which is evidence on a node.
    consumes=(),
    # The rendered report, in `Document.content`, which is what the evidence name `text` IS.
    #
    # THE `triples` ROWS ARE NOT DECLARED, and that is not an oversight: `extract:facts`
    # declares `produces=()` for the identical reason (`fact_tasks.py`) — rows in `triples`
    # are not evidence on a node of any tree, and naming an evidence key that cannot be read
    # back would put this task in the audit's derivation for a fact nobody writes. What it
    # costs, and the exact entry that would fix it, is in the lane report: `jmfts_core.atoms`
    # would need a name for it AND `jmfts_core.evidence` a matching `register()` with
    # `Store(STORE_ROWS, "triples")`, and the two must land together or every atom
    # declaration fails.
    produces=(f"{EV_TEXT}@self",),
    # `self`: the only DOCUMENT this task writes is the report node it is scoped to. It
    # creates no child and touches no node of any tree it derives from.
    write_mode=WRITE_SELF,
    # `cpu`. pyshacl is a graph walk and a SPARQL evaluation: no weights, no GPU, no LLM.
    # The report node is deliberately not embedded, so no model runs anywhere in this
    # task's reach and a badge sized on this class is sized correctly.
    cost_class=COST_CPU,
)
def run_derive_rule(session: Session, task: TaskQueue) -> TaskOutcome:
    """Run one binding's rules over its scope and replace what that rule had derived.

    Raises:
        RdfStackNotInstalled: this install has no ``rdflib`` or no ``pyshacl``. PERMANENT, so
            the node ends ``failed`` with the extra named rather than the request being
            refused by an API process that may not be the one that would have run it.
        LookupError: the report node, the binding or its ontology is gone.
        ShapeNotInOntologyError: the vocabulary was replaced and no longer declares the bound
            shape. A ``ValueError``, so it classifies PERMANENT (Block B finding 6).
        ShapeDeclaresNoRuleError: the bound shape carries no ``sh:rule``.
        UnstorableTermError, TermOutOfScopeError, PredicateNotRegisteredError: a derived
            triple this store cannot hold. Nothing is written, including nothing deleted.
        ValueError: the task row does not carry what a run needs, or the binding moved under
            it.
    """
    node = session.get(Document, task.scope_document_id)
    if node is None:
        raise LookupError(
            f"{TASK_DERIVE_RULE} is scoped to document {task.scope_document_id}, which does "
            "not exist; the report node was deleted while the task was queued"
        )
    if node.usetype != DERIVATION_REPORT_USETYPE:
        raise ValueError(
            f"{TASK_DERIVE_RULE} is scoped to document {node.id}, whose usetype is "
            f"{node.usetype!r} and not {DERIVATION_REPORT_USETYPE!r}; this task writes a "
            "derivation report and has no meaning on any other node"
        )

    params = task.params or {}
    for key in (PARAM_BINDING_ID, PARAM_DOCUMENT_IDS, PARAM_BASE_IRI, PARAM_RULE):
        if key not in params:
            raise ValueError(
                f"{TASK_DERIVE_RULE} task {task.id} carries no {key!r}; the request is what "
                "resolves a binding's scope, under the caller's access filter, and a worker "
                "cannot re-resolve it because it holds no principal"
            )
    binding_id = int(params[PARAM_BINDING_ID])
    document_ids = [int(i) for i in params[PARAM_DOCUMENT_IDS]]
    base_iri = params[PARAM_BASE_IRI]
    rule = params[PARAM_RULE]

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
    # The identity is pinned on the row AND recomputed, because it is the DELETE set. A
    # binding has no update endpoint, so the two can only disagree if one appears — and the
    # day it does, this refuses rather than deleting a set the request never announced.
    recomputed = rule_identity(
        ontology_name=binding.ontology_name,
        shape_iri=binding.shape_iri,
        scope_type=binding.scope_type,
        scope=binding.scope or {},
    )
    if recomputed != rule:
        raise ValueError(
            f"{TASK_DERIVE_RULE} task {task.id} was enqueued for rule {rule!r} and binding "
            f"{binding_id} now identifies {recomputed!r}; the binding changed under the "
            "request, and deleting a set the caller was not told about is not a re-derivation"
        )

    pyshacl = require_pyshacl()
    rdflib = require_rdflib()

    bound = build_data_graph(
        session,
        scope_type="documents",
        scope={"document_ids": document_ids},
        # None: the request already applied whatever bound the operator configured, and
        # re-applying one here would refuse a run the request accepted. Open question 6.3's
        # setting is Block A step 5's to measure.
        max_scope_documents=None,
        # Fixed. See the module docstring: this is where "no chaining" lives.
        provenance=DERIVATION_PROVENANCE,
        base_iri=base_iri,
    )
    # The same function `validate:shape` calls: "only the bound shape runs" is one
    # computation and lives in `rdf/shacl.py` (Block B finding 4).
    shapes, targets_stripped = bound_shape_graph(
        rdflib,
        source_turtle=ontology.source_turtle,
        ontology_name=ontology.name,
        shape_iri=binding.shape_iri,
        target_iris=[document_iri(doc_id, base_iri) for doc_id in bound.scope_document_ids],
    )
    if not declares_a_rule(rdflib, shapes, binding.shape_iri):
        raise ShapeDeclaresNoRuleError(
            f"shape {binding.shape_iri!r} in ontology {binding.ontology_name!r} declares no "
            "sh:rule, so a derivation run over it would conclude nothing — which reads "
            "exactly like rules that ran and matched nothing. Bind a shape that carries "
            "one, or validate this one instead (POST /ontologies/{name}/validate)"
        )

    derived = expand_rules(pyshacl, bound.graph, shapes)
    # Every term mapped before anything is deleted: a rule that produces one unstorable
    # triple leaves the store exactly as it was, including the rows its own previous run
    # wrote.
    candidates = map_to_rows(
        session,
        rdflib,
        derived,
        base_iri=base_iri,
        scope_ids=set(bound.scope_document_ids),
    )
    outcome = apply_derivation(session, rule=rule, candidates=candidates)

    request = dict((node.structured_content or {}).get(DERIVATION_BLOCK) or {})
    request.update(
        {
            "binding_id": binding_id,
            "ontology_name": binding.ontology_name,
            "shape_iri": binding.shape_iri,
            "scope_type": binding.scope_type,
            "provenance": DERIVATION_PROVENANCE,
            "base_iri": base_iri,
            "rule": rule,
        }
    )
    report = {
        "rule": rule,
        # Open question 6.5's answer, applied to a derivation rather than to a violation: the
        # RUN'S timestamp, and the caller compares. Nothing here expires a derived row and
        # nothing revalidates one in the background — a re-run is what refreshes it.
        "derived_at": _utc_now_iso(),
        "documents_requested": len(document_ids),
        "documents_resolved": len(bound.scope_document_ids),
        "triple_count": bound.triple_count,
        "node_count": bound.node_count,
        "boundary_cuts": len(bound.boundary_cuts),
        "targets_stripped": targets_stripped,
        "candidates": len(candidates),
        "deleted": outcome.deleted,
        "inserted": outcome.inserted,
        "already_present": len(outcome.already_present),
        "already_present_rows": list(outcome.already_present),
        "triples": [candidate.as_dict() for candidate in candidates],
    }
    node.structured_content = {
        **(node.structured_content or {}),
        DERIVATION_BLOCK: {**request, "report": report},
    }
    node.content = _render(report, request)
    session.flush()

    return TaskOutcome(
        detail={
            "binding_id": binding_id,
            "shape_iri": binding.shape_iri,
            "rule": rule,
            "candidates": report["candidates"],
            "inserted": report["inserted"],
            "deleted": report["deleted"],
            "already_present": report["already_present"],
            "documents": report["documents_resolved"],
            "triples": report["triple_count"],
            "derived_at": report["derived_at"],
        },
    )


__all__ = [
    "DERIVATION_BLOCK",
    "DERIVATION_PROVENANCE",
    "DERIVATION_REPORT_USETYPE",
    "PARAM_BASE_IRI",
    "PARAM_BINDING_ID",
    "PARAM_DOCUMENT_IDS",
    "PARAM_RULE",
    "RENDERED_TRIPLE_CAP",
    "REPORT_PARENT_REASON",
    "run_derive_rule",
]
