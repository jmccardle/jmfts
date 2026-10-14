"""The standing audit: every ``@expose``d verb either consults the principal, or is listed.

``docs/SPRINT_0_6_0.md`` Block A step 3. Steps 1 and 2 close two access gates that were
found by an audit, re-confirmed twice, and found by NO TEST — which is the reason this file
exists. ``tests/test_api_parity.py::test_registry_and_generated_routes_are_in_bijection``
(``:43``, with ``REGISTRY`` imported at ``:23``) already reads the registry from a test and
asks whether every operation is mounted; this reads the same registry and asks whether every
operation can reach ``jmfts_core.access`` at all.

WHAT THIS PROVES, AND WHAT IT DOES NOT. The analysis below is a resolved call-graph
reachability search: it starts at the exposed method and follows calls it can RESOLVE —
module-level functions through the importing module's own import table, ``self.method``
within the defining class, and ``obj.method`` where ``obj`` was bound by a visible
``obj = SomeClass(...)`` — until it either reaches a gate function defined in
``jmfts_core/access.py`` or exhausts the graph.

* A verb with NO path to ``jmfts_core.access`` provably does not consult the principal.
  That direction is sound and it is the one the assertion is written on.
* A verb WITH a path is not thereby proved to be gated. ``TemplateService.list_templates``
  once reached ``require_add_child`` only because ``_get_container_id`` may create the
  container document — a real call, on a gate, that scoped nothing about the listing — and
  so read as scoped while returning every template. Since 2026-10-13 it reaches
  ``readable_filter`` directly, which does scope the listing
  (``tests/test_access_template_reads.py``). The residual of the same kind today is
  ``IngestService.file_frontier``: it takes ``can_read`` on the named node, but
  ``nodes_total`` and the per-state counts still include descendants the caller cannot
  read, so a governed caller learns a hidden subtree's size (ROADMAP known gap 12). It
  cannot be listed below, because the list is held in both directions and the verb does
  reach a gate. Reachability is a necessary condition, not a sufficient one.

So this file is a FLOOR: the list below is the set of verbs that are certainly unscoped, and
it can only be under-inclusive. It still does the job step 3 asked of it — the next verb
added with no principal in sight lands in the analysis's ungated set, is not in the list,
and turns this test red on the commit that adds it.

The list is a decision with a reason attached, the same shape as
``tests/test_readme_links.py::PUBLISHED``. A reason is one of three things: this verb has
no document to scope (fine), this verb names documents and is an OPEN GAP (said so, in
those words), or Block A scoped past it deliberately. **The front end reads this list to know
which verbs are safe to put on a page** — Part 2's placement of Block A alongside Block F is
exactly that.
"""

from __future__ import annotations

import ast
from pathlib import Path

import jmfts_core.rest.main  # noqa: F401  — imports every service, populating REGISTRY
from jmfts_core.registry import REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "jmfts_core"
ACCESS_MODULE = "jmfts_core.access"


# --- the "why not" list --------------------------------------------------------------
#
# Reasons repeat, so they are named once. A repeated string that drifts in one copy is the
# thing a list like this is worst at.

NO_DOCUMENT = (
    "No document is involved. The answer is read out of a declaration table in this "
    "process; there is nothing for an access-control root to be above."
)

APPLIANCE = (
    "Describes the appliance, not the corpus: installed extras, formats, retrieval methods. "
    "Deliberately answerable by any token holder — it is how an integrator finds out what "
    "this process can do before sending it anything."
)

CONFIG_OBJECT = (
    "A named configuration row, not a document. Subtree RBAC is defined over the document "
    "tree (an ACR is a document with grants, access.py:5), so there is no root above this "
    "row to consult. That leaves it shared mutable state any token holder may change, "
    "which is a governance question the access model does not express today — not a "
    "subtree-RBAC gap that step 1 or step 2 left open."
)

VOCABULARY = (
    "Corpus-wide vocabulary — an ontology, a shape, a predicate. Same shape as "
    "CONFIG_OBJECT: not a document, no ACR above it, shared by everyone with a token."
)

AGGREGATE_COUNTS = (
    "Returns counts and no document identity — no id, title, or content crosses the "
    "boundary. A governed caller still learns the corpus size from it. Block A step 2 "
    "scopes to the four analytics verbs that return ids; this one is listed rather than "
    "changed."
)

# --- and the entries that are gaps, said in those words -------------------------------

# None remain. The ten verbs ROADMAP known gap 12 listed — the five IndexService verbs
# and the template, frontier and binding families — each reach a gate as of 2026-10-13,
# and their GAP_* constants were deleted with them. Two residual counts are recorded
# in that gap rather than here, because both verbs do reach a gate.


#: Every ``@expose``d operation that provably does not consult the current principal, and
#: why that is so. Keys are ``Service.method`` — the ``name`` on the registry entry.
NOT_PRINCIPAL_SCOPED: dict[str, str] = {
    # DocumentService.embed_document was here, as an OPEN GAP. It left this list on
    # 2026-10-12: DocumentRepository.embed_document now takes require_write, so a hidden
    # document is a 404 and a read-only one a 403. `tests/test_access_embed_write.py`.
    "GraphService.get_diff": AGGREGATE_COUNTS,
    "GraphService.get_stats": AGGREGATE_COUNTS,
    "IndexService.list_indexes": CONFIG_OBJECT,
    "IndexService.create_index": CONFIG_OBJECT,
    "IndexService.get_index": CONFIG_OBJECT,
    "IndexService.delete_index": CONFIG_OBJECT,
    # The five IndexService verbs that name documents were here, as two OPEN GAP entries.
    # They left this list on 2026-10-12, and every gate lives in SearchRepository:
    # add/remove root and index_document take require_write (a hidden or missing document
    # is a 404 — a 400 for index_document, matching its "not found" — read-only a 403),
    # get_index_roots drops roots the caller cannot read, and refresh_index takes write on
    # every root before it clears anything. `tests/test_access_index_verbs.py`.
    "IngestService.explain_ingest": NO_DOCUMENT,
    "IngestService.list_registered_pipelines": NO_DOCUMENT,
    # IngestService.file_frontier was here, as an OPEN GAP. It left this list on
    # 2026-10-13: it takes can_read on the node, so a hidden node is the same 404 as a
    # missing one. `tests/test_access_file_frontier.py`. RESIDUAL, and not listed because
    # the verb does reach a gate: the counts still include descendants the caller cannot
    # read (the AGGREGATE_COUNTS shape), recorded in ROADMAP known gap 12.
    "MetaService.capabilities": APPLIANCE,
    "OntologyService.import_ontology": VOCABULARY,
    "OntologyService.list_ontologies": VOCABULARY,
    "OntologyService.get_ontology": VOCABULARY,
    "OntologyService.delete_ontology": VOCABULARY,
    "OntologyService.list_bindings": VOCABULARY,
    "OntologyService.delete_binding": VOCABULARY,
    # OntologyService.create_binding was here, listed for its document-reference scope. It
    # left this list on 2026-10-13: a governed caller may name only documents it can read
    # in a `parent_id` or `document_ids` scope (readable_id_subset), and a hidden id is the
    # same 404 as a missing one. `tests/test_access_binding_scope.py`.
    "SearchContextService.create_context": CONFIG_OBJECT,
    "SearchContextService.list_contexts": CONFIG_OBJECT,
    "SearchContextService.get_context": CONFIG_OBJECT,
    "SearchContextService.update_context": CONFIG_OBJECT,
    "SearchContextService.delete_context": CONFIG_OBJECT,
    # TemplateService.get_template and render_template were here, as an OPEN GAP. They
    # left this list on 2026-10-13: both read the template through can_read, so a template
    # under a root the caller holds no grant on is "Template not found".
    # `tests/test_access_template_reads.py`. list_templates is scoped too, by
    # readable_filter on its own query (it only reached require_add_child through
    # _get_container_id, which scoped nothing); the same file holds it over HTTP.
    "TripleService.create_predicate": VOCABULARY,
    "TripleService.get_predicate": VOCABULARY,
    "TripleService.list_predicates": VOCABULARY,
    "TripleService.delete_predicate": VOCABULARY,
    # The five TripleService WRITE verbs were here, as the two OPEN GAP entries step 3
    # found and step 35 promoted to a step. They left this list on 2026-09-15: assert takes
    # write on the subject and read on the object, retract takes require_edge_delete's rule
    # read off `triples.derived_by`, and both gates live in TripleRepository so a new caller
    # cannot walk around them. `tests/test_access_triple_writes.py` is the measurement.
    "UsetypePresentationService.create_presentation": CONFIG_OBJECT,
    "UsetypePresentationService.list_presentations": CONFIG_OBJECT,
    "UsetypePresentationService.get_presentation": CONFIG_OBJECT,
    "UsetypePresentationService.update_presentation": CONFIG_OBJECT,
    "UsetypePresentationService.delete_presentation": CONFIG_OBJECT,
}


# --- the analysis --------------------------------------------------------------------
#
# `jmfts_core/access.py` also holds identity and storage helpers that are not gates.
# `hash_token` / `resolve_principal_token` are the auth layer's token lookup, and
# `access_key` / `access_key_text` / `acr_ids` are how an entity root is KEYED by access —
# reading them scopes nothing. Excluded by name so that a new function added to access.py
# counts as a gate by default, which is the direction that fails safe.
NOT_A_GATE = frozenset(
    {"hash_token", "resolve_principal_token", "access_key", "access_key_text", "acr_ids"}
)


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class _CallGraph:
    """Functions in ``jmfts_core``, and the calls between them that can be resolved."""

    def __init__(self) -> None:
        self.trees: dict[str, ast.Module] = {}
        self.imports: dict[str, dict[str, tuple[str, str]]] = {}
        self.module_alias: dict[str, dict[str, str]] = {}
        self.defs: dict[str, set[str]] = {}
        self.classes: dict[str, str] = {}
        self.bodies: dict[tuple[str, str], ast.AST] = {}

        for path in sorted(PACKAGE.rglob("*.py")):
            self.trees[_module_name(path)] = ast.parse(path.read_text())
        for module, tree in self.trees.items():
            self._index(module, tree)

    def _index(self, module: str, tree: ast.Module) -> None:
        self.imports[module] = {}
        self.module_alias[module] = {}
        self.defs[module] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    self.imports[module][alias.asname or alias.name] = (node.module, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    key = alias.asname or alias.name.split(".")[0]
                    self.module_alias[module][key] = alias.name
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defs[module].add(node.name)
                self.bodies[(module, node.name)] = node
            elif isinstance(node, ast.ClassDef):
                self.classes.setdefault(node.name, module)
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        qual = f"{node.name}.{sub.name}"
                        self.defs[module].add(qual)
                        self.bodies[(module, qual)] = sub

    # -- edges ------------------------------------------------------------------------

    @staticmethod
    def _local_class_bindings(fn: ast.AST) -> dict[str, str]:
        """``var -> ClassName`` for every visible ``var = ClassName(...)`` inside ``fn``.

        The repository pattern is `repo = DocumentRepository(self.session)` followed by
        `repo.create(...)`, so without this the analysis stops at the service layer and
        every verb in the tree reads as unscoped.
        """
        bindings: dict[str, str] = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                func = node.value.func
                name = func.id if isinstance(func, ast.Name) else None
                if name and name[:1].isupper():
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            bindings[target.id] = name
        return bindings

    def _module_of_class(self, module: str, cls: str) -> str | None:
        if cls in self.imports.get(module, {}):
            target_module, _ = self.imports[module][cls]
            return target_module if target_module in self.trees else None
        if any(q == cls or q.startswith(cls + ".") for q in self.defs.get(module, ())):
            return module
        return self.classes.get(cls)

    def edges(self, node: tuple[str, str]) -> set[tuple[str, str]]:
        module, qual = node
        fn = self.bodies[node]
        enclosing = qual.split(".")[0] if "." in qual else None
        bindings = self._local_class_bindings(fn)
        out: set[tuple[str, str]] = set()

        def emit(mod: str, sym: str) -> None:
            if mod == ACCESS_MODULE or (mod in self.trees and sym in self.defs.get(mod, ())):
                out.add((mod, sym))

        def visit(func: ast.AST) -> None:
            if isinstance(func, ast.Name):
                if func.id in self.imports.get(module, {}):
                    emit(*self.imports[module][func.id])
                elif func.id in self.defs.get(module, ()):
                    emit(module, func.id)
                return
            if not isinstance(func, ast.Attribute):
                return
            attr, value = func.attr, func.value
            cls: str | None = None
            if isinstance(value, ast.Name):
                if value.id == "self" and enclosing:
                    emit(module, f"{enclosing}.{attr}")
                    return
                if value.id in self.module_alias.get(module, {}):
                    emit(self.module_alias[module][value.id], attr)
                    return
                if value.id in self.imports.get(module, {}):
                    target_module, target_symbol = self.imports[module][value.id]
                    if target_module in self.trees and target_symbol not in self.defs.get(
                        target_module, ()
                    ):
                        emit(f"{target_module}.{target_symbol}", attr)
                    cls = target_symbol
                cls = cls or bindings.get(value.id)
            elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                cls = value.func.id  # `SearchRepository(session).hybrid_search(...)`
            if cls:
                owner = self._module_of_class(module, cls)
                if owner:
                    emit(owner, f"{cls}.{attr}")

        for sub in ast.walk(fn):
            if not isinstance(sub, ast.Call):
                continue
            visit(sub.func)
            # A callable PASSED to a call, not called here: `asyncio.to_thread(self._drain,
            # ...)` is how IngestService.ingest_content reaches everything it does, and
            # without this edge that verb reads as unscoped when it is not.
            for arg in list(sub.args) + [kw.value for kw in sub.keywords]:
                if isinstance(arg, (ast.Name, ast.Attribute)):
                    visit(arg)
        return out

    def gate_names(self) -> set[str]:
        tree = self.trees[ACCESS_MODULE]
        return {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_")
        } - NOT_A_GATE

    def reaches_access(self, start: tuple[str, str]) -> bool:
        gates = self.gate_names()
        seen: set[tuple[str, str]] = set()
        frontier = {start}
        while frontier:
            node = frontier.pop()
            if node in seen:
                continue
            seen.add(node)
            if node[0] == ACCESS_MODULE and node[1] in gates:
                return True
            if node in self.bodies:
                frontier |= self.edges(node) - seen
        return False


def _unscoped_verbs() -> tuple[set[str], set[str]]:
    graph = _CallGraph()
    scoped, unscoped = set(), set()
    for spec in REGISTRY:
        key = (
            spec.service_cls.__module__,
            f"{spec.service_cls.__name__}.{spec.name.split('.')[-1]}",
        )
        assert key in graph.bodies, f"{spec.name}: no source found at {key}"
        (scoped if graph.reaches_access(key) else unscoped).add(spec.name)
    return scoped, unscoped


# --- the assertions ------------------------------------------------------------------


def test_every_exposed_verb_is_principal_scoped_or_listed():
    """The unscoped set and the list are the same set — in both directions.

    A new verb with no path to ``jmfts_core.access`` fails here and is fixed by gating it
    or by adding a line to ``NOT_PRINCIPAL_SCOPED`` that says why not. A verb that GAINS a
    gate fails here too, and is fixed by deleting its line — which is what keeps the list
    from turning into a record of things that used to be true.
    """
    scoped, unscoped = _unscoped_verbs()
    assert scoped, "no verb reached jmfts_core.access — the analysis, not the appliance"

    listed = set(NOT_PRINCIPAL_SCOPED)
    newly_unscoped = unscoped - listed
    assert not newly_unscoped, (
        "these @expose'd verbs cannot reach jmfts_core.access and are not on the list: "
        f"{sorted(newly_unscoped)}. Gate them, or add an entry saying why not."
    )
    now_scoped = listed - unscoped
    assert not now_scoped, (
        "these verbs are on the 'not principal-scoped' list but now reach a gate: "
        f"{sorted(now_scoped)}. Delete their entries."
    )


def test_listed_verbs_still_exist():
    """The list names live operations. A renamed verb takes its reason with it."""
    names = {spec.name for spec in REGISTRY}
    missing = set(NOT_PRINCIPAL_SCOPED) - names
    assert not missing, f"NOT_PRINCIPAL_SCOPED names verbs that are not exposed: {sorted(missing)}"


def test_every_listed_verb_carries_a_reason():
    """A bare name on this list is a note to nobody. Each entry says why."""
    thin = {name for name, why in NOT_PRINCIPAL_SCOPED.items() if len(why.strip()) < 40}
    assert not thin, f"these entries have no real reason attached: {sorted(thin)}"


def test_the_gates_block_a_closed_stay_closed():
    """The six verbs Block A steps 1 and 2 moved, named so a revert is a red test here too.

    The first test would also catch a revert — the reverted verb would reappear in the
    unscoped set and not be on the list — but it would report it as "add an entry saying
    why not", which is the wrong instruction for a gate somebody deleted. This says the
    right thing. Behaviour, as opposed to reachability, is asserted in
    ``tests/test_access_edge_writes.py`` and ``tests/test_access_graph_analytics.py``.
    """
    _, unscoped = _unscoped_verbs()
    for name in (
        "DocumentService.create_link",
        "DocumentService.delete_link",
        "GraphService.get_centrality",
        "GraphService.get_communities",
        "GraphService.get_spines",
        "GraphService.get_subtree_authority",
    ):
        assert name not in unscoped, f"{name} lost its access gate"
