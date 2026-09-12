"""The generated client must match the surface it was generated from.

``jmfts-client/jmfts_client/_verbs.py`` is checked in, so building the client distribution
needs nothing from the server. The cost of checking a generated file in is that it can go
stale, and a stale client is the drift this whole design exists to prevent — a verb that
exists on the appliance and not in the client, or worse, one whose parameters moved.

These tests are the ratchet. They need no database: they build the app's route table and
render the file in memory.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from jmfts_core.registry import REGISTRY
from jmfts_core.rest.main import app
from jmfts_core.rest.wiring import iter_mounted_api_routes
from scripts.generate_client import TARGET, render

#: Operations whose route declares no response model, so the client returns parsed JSON
#: instead of a validated type. Named rather than counted, so adding one is a decision
#: someone writes down. ``test_untyped_verbs_carve_out_only_shrinks`` fails if a name here
#: gains a response model and is left on the list.
UNTYPED_OPERATIONS = frozenset(
    {
        "AccessService.delete_principal",
        "AccessService.revoke_grant",
        "AccessService.revoke_token",
        "DocumentService.delete_document",
        "DocumentService.delete_link",
        "DocumentService.embed_document",
        "IndexService.add_root_to_index",
        "IndexService.delete_index",
        "IndexService.get_index_roots",
        "IndexService.index_single_document",
        "IndexService.refresh_index",
        "IndexService.remove_root_from_index",
        # The two 0.3.0 deletes, on the same terms as every other delete above: the body is
        # ``{"deleted": <key>}`` echoing what was named, and a contract class per delete
        # would be twelve classes saying one thing.
        "OntologyService.delete_binding",
        "OntologyService.delete_ontology",
        "SearchContextService.delete_context",
        "TripleService.delete_predicate",
        "TripleService.delete_triple",
        "UsetypePresentationService.delete_presentation",
    }
)


def test_generated_client_is_current():
    """``python -m scripts.generate_client`` produces exactly the checked-in file."""
    on_disk = TARGET.read_text(encoding="utf-8")
    assert on_disk == render(), (
        "jmfts-client/jmfts_client/_verbs.py is stale. The exposed surface changed and the "
        "client was not regenerated. Run: python -m scripts.generate_client"
    )


def test_every_exposed_operation_has_a_client_method():
    """One method per registry entry — the client cannot be a subset of the appliance."""
    tree = ast.parse(TARGET.read_text(encoding="utf-8"))
    (cls,) = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    generated = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
    expected = {spec.func.__name__ for spec in REGISTRY}
    assert generated == expected, f"missing: {expected - generated}, extra: {generated - expected}"


def test_operation_method_names_are_unique():
    """The client is one flat namespace; two services sharing a method name would collide.

    This is the assumption ``_emit_method`` rests on. It holds today (94 distinct names).
    If it ever breaks, the generator must qualify the name rather than silently emit the
    second method over the first.
    """
    names = [spec.func.__name__ for spec in REGISTRY]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"two operations share a method name: {duplicates}"


def test_untyped_verbs_are_exactly_the_carve_out():
    """The set of operations returning unvalidated JSON is the one written down above."""
    actual = {spec.name for spec in REGISTRY if spec.response_model is None}
    assert actual == UNTYPED_OPERATIONS, (
        f"untyped operations changed. Gained: {actual - UNTYPED_OPERATIONS}. "
        f"Lost: {UNTYPED_OPERATIONS - actual}. Declare a response_model, or update the list."
    )


def test_untyped_carve_out_only_shrinks():
    """A named operation that gained a response model must leave the list."""
    typed = {spec.name for spec in REGISTRY if spec.response_model is not None}
    still_listed = UNTYPED_OPERATIONS & typed
    assert not still_listed, (
        f"{still_listed} now declare a response model. Remove them from UNTYPED_OPERATIONS "
        "so the carve-out keeps shrinking."
    )


def test_generator_reads_resolved_routes_not_the_registry_alone():
    """Guard the reason this generator exists.

    ``ExposeSpec`` does not record body-vs-query-vs-path binding; ``rest/wiring.py`` leaves
    that to FastAPI. If a future edit made the generator read only ``REGISTRY``, it would
    have to reimplement that inference and could disagree with the server about the wire.
    This asserts the route table is still the thing carrying the answer.
    """
    routes = {r.name: r for r in iter_mounted_api_routes(app)}
    spec = next(s for s in REGISTRY if s.name == "DocumentService.create_document")
    route = routes[spec.name]
    assert route.dependant.body_params, "the create route's body binding lives on the route"
    assert not hasattr(spec, "body_params"), "ExposeSpec must not grow a binding field"


def test_client_package_does_not_import_the_server():
    """The point of the thin distribution: nothing in it may IMPORT ``jmfts_core``.

    This reads import statements, not file text. Docstrings in the client refer to
    ``jmfts_core`` on purpose — a contract explaining which service enforces it is useful
    documentation, and a text search would forbid writing that down. What must not exist
    is a runtime dependency: ``pip install jmfts-client`` brings no server, so an import
    of one is an ImportError on a consumer's machine.
    """
    root = Path(TARGET).resolve().parent  # the package, not the distribution directory
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "jmfts_core" or name.startswith("jmfts_core.") for name in names):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, f"client modules import jmfts_core: {offenders}"


def _client_pyproject() -> dict:
    dist = Path(TARGET).resolve().parents[1]  # jmfts-client/, which holds the pyproject
    return tomllib.loads((dist / "pyproject.toml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["httpx", "pydantic"])
def test_client_declares_its_only_two_dependencies(name):
    """A third REQUIRED dependency is paid for by every consumer, so it is a deliberate act.

    Extras do not count against this and are the right home for anything narrower — see
    ``vectors``, which exists because numpy is ~20 MB and only a caller that moves raw
    embeddings ever needs it.
    """
    declared = _client_pyproject()["project"]["dependencies"]
    assert any(d.startswith(name) for d in declared), f"{name} missing from {declared}"
    assert len(declared) == 2, f"jmfts-client grew a required dependency: {declared}"


def test_no_client_module_imports_an_undeclared_package_at_module_level():
    """Importing any client module must work with only the two required dependencies.

    This is the failure a clean-room install finds and a source checkout hides: the repo has
    numpy sitting in the same environment, so ``import jmfts_client.contracts.runner``
    succeeds here while failing for a consumer who installed the wheel. The rule is that
    module level may import only what the wheel declares; anything heavier is fetched inside
    the function that needs it, behind a guard naming its extra.
    """
    root = Path(TARGET).resolve().parent  # the package, not the distribution directory
    allowed = {"httpx", "pydantic", "jmfts_client"} | set(sys.stdlib_module_names)
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module level only — a nested import is the guarded case
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] not in allowed:
                    offenders.append(f"{path.relative_to(root)}:{node.lineno} imports {name}")
    assert not offenders, "module-level imports outside the declared dependencies: " + str(
        offenders
    )


def test_nothing_imports_the_contracts_package_at_its_old_name():
    """``jmfts_core.contracts`` is gone. An import of it must fail HERE, not in CI.

    This one is worth a test because a developer machine cannot see it. The contracts moved
    to ``jmfts_client``, but an editable install of an older checkout leaves a finder that
    still resolves ``jmfts_core.contracts`` — to the OTHER working tree, whose files are not
    the ones under test. So the import succeeds locally, passes review, and fails on the
    first machine that has only this commit.

    A branch that was written before the move and merged after it is exactly how such an
    import arrives, and the merge itself will not conflict: nothing textually collides.

    **The enumeration is the INDEX and the reading is the WORKING TREE, and the two disagree
    in exactly one way that matters.** ``git ls-files`` lists a file the working tree no
    longer has until the deletion is staged, so opening every path it names died with
    ``FileNotFoundError`` mid-delete — a crash in place of a verdict, which reads as a broken
    test to whoever meets it next rather than as "you have not staged that yet".
    ``--deleted`` names precisely that set and it is subtracted up front, so a working-tree
    deletion is a clean pass: a file that is not there imports nothing.

    Subtracted rather than caught, because ``except FileNotFoundError: continue`` would
    excuse any missing path for any reason. And the working tree is read rather than the
    blobs (``git show :path``) deliberately: an import a developer has just written and not
    yet staged is the case this test exists to catch, and the index does not have it.
    """
    root = Path(__file__).resolve().parents[1]

    def _ls_files(*flags: str) -> set[str]:
        listed = subprocess.run(
            ["git", "ls-files", "-z", *flags, "*.py"],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout.decode()
        return set(filter(None, listed.split("\0")))

    tracked = _ls_files() - _ls_files("--deleted")
    offenders = []
    for relative in sorted(tracked):
        path = root / relative
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # a fixture that is deliberately not valid Python
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                name = node.module or ""
            elif isinstance(node, ast.Import):
                name = " ".join(alias.name for alias in node.names)
            else:
                continue
            if "jmfts_core.contracts" in name:
                offenders.append(f"{relative}:{node.lineno}")
    assert not offenders, (
        "these import jmfts_core.contracts, which no longer exists; the contracts are "
        "jmfts_client.contracts: " + str(offenders)
    )
