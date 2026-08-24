"""The RDF stack is an extra, and the base install has to stay able to say so.

``docs/SPRINT_0_3_0.md`` Part 9. The same two claims ``tests/test_office_packaging.py``
makes about the office readers, about ``rdflib`` and ``pyshacl`` — and they are different
in kind.

The **structural** claim is that nothing on the app's import path reaches for either
library. It needs a subprocess: by the time this test runs, pytest has imported most of the
tree, so asserting on this process's ``sys.modules`` would prove nothing about what a fresh
one loads.

The **packaging** claim is that a missing RDF stack is a NAMED state rather than a
traceback — because an install with no ``rdflib`` is usually correct. The triple store is
an RDF store with integers for names and always was: facts are asserted, queried,
superseded and invalidated with no RDF library anywhere near them. A worker that neither
imports a vocabulary nor exports one is properly installed and properly has no ``rdflib``.

``scripts/check_base_install.sh`` makes both claims against a real base venv, with no
simulation in it at all. This file makes them where CI runs them on every commit.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
EXTRAS = PYPROJECT["project"]["optional-dependencies"]


def _requirement_names(specifiers: list[str]) -> set[str]:
    """Distribution names from requirement strings, lowercased, extras markers dropped."""
    names = set()
    for spec in specifiers:
        head = spec.split(";")[0].strip()
        for separator in (">=", "==", "<=", "~=", ">", "<", "["):
            head = head.split(separator)[0]
        names.add(head.strip().lower())
    return names


# ---------------------------------------------------------------------------
# The declarations
# ---------------------------------------------------------------------------


def test_the_rdf_extra_carries_both_libraries():
    """``pyshacl`` is declared although nothing calls it yet, and that is the point.

    Which TIER a library belongs to is the packaging decision, and it is cheap to make now
    and expensive to discover later — a validator landing in a release where the extra does
    not carry ``pyshacl`` arrives with a quiet top-level import, which is the exact failure
    the seam exists to prevent. ``olefile`` and ``require_openpyxl`` were both declared
    ahead of their first caller for this reason.
    """
    declared = _requirement_names(EXTRAS["rdf"])
    assert declared == {"rdflib", "pyshacl"}, declared


def test_dev_implies_rdf():
    """The suite parses and serialises real Turtle, so it must install what does that."""
    assert "jmfts[rdf]" in [s.strip() for s in EXTRAS["dev"]]


def test_neither_library_is_a_base_dependency():
    base = _requirement_names(PYPROJECT["project"]["dependencies"])
    assert "rdflib" not in base
    assert "pyshacl" not in base


def test_the_rdf_package_is_shipped():
    """``packages.find`` uses a glob, so a new subpackage is easy to add and forget."""
    include = PYPROJECT["tool"]["setuptools"]["packages"]["find"]["include"]
    assert any(pattern.startswith("jmfts_core") and pattern.endswith("*") for pattern in include)
    assert (REPO / "jmfts_core" / "rdf" / "__init__.py").is_file()


# ---------------------------------------------------------------------------
# The structural claim
# ---------------------------------------------------------------------------


PROBE = """
import sys

import jmfts_core.rest.main       # the whole API, every router, RdfService included
import jmfts_core.worker          # the CLI and the loop
import jmfts_core.ingest_tasks    # every registered task handler
import jmfts_core.rdf             # the seam itself: importing it imports no library
import jmfts_core.rdf.names       # and neither does the naming half, which is string work

loaded = sorted(m for m in ("rdflib", "pyshacl") if m in sys.modules)
print(",".join(loaded))
"""


def test_starting_the_app_imports_no_rdf_library():
    """The claim that keeps a triple store usable without being able to speak Turtle.

    In a SUBPROCESS, for the reason the module docstring gives.

    Note what this proves in each environment. Where the libraries ARE installed (CI, which
    installs ``dev``), it proves nothing on the import path reaches for them. Where they are
    NOT, it proves the app imports cleanly without them. Both are the claim; neither alone
    is.
    """
    result = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        f"starting the app imported {result.stdout.strip()}; something on the import path "
        "has grown an eager RDF import. Move it behind a require_* guard in "
        "jmfts_core/rdf/__init__.py."
    )


# ---------------------------------------------------------------------------
# The packaging claim
# ---------------------------------------------------------------------------


#: Simulate the absent libraries by poisoning ``sys.modules``, which makes ``import rdflib``
#: raise ``ImportError: import of rdflib halted; None in sys.modules`` — the same exception
#: class the guards catch. ``scripts/check_base_install.sh`` is the version with no
#: simulation in it: it builds a clean venv, installs base, and asserts the same things.
MISSING_STACK_PROBE = """
import sys

from jmfts_core.rdf import RdfStackNotInstalled, require_pyshacl, require_rdflib
from jmfts_core.task_errors import ErrorType, classify_exception

sys.modules["rdflib"] = None
sys.modules["pyshacl"] = None

for label, guard in (("rdflib", require_rdflib), ("pyshacl", require_pyshacl)):
    try:
        guard()
    except RdfStackNotInstalled as exc:
        assert "jmfts[rdf]" in str(exc), f"{label}: message does not name the extra"
        assert classify_exception(exc) is ErrorType.PERMANENT, f"{label}: not PERMANENT"
    else:
        raise AssertionError(f"{label}: the guard returned a module that is not there")

# The triple store keeps working with both libraries gone. This is the whole reason the
# split is possible: an appliance can hold, query and supersede facts without them.
from jmfts_core.models.triple import Triple  # noqa: F401
from jmfts_core.repositories.triple import TripleRepository  # noqa: F401

print("ok")
"""


def test_a_missing_rdf_stack_is_a_named_permanent_error():
    result = subprocess.run(
        [sys.executable, "-c", MISSING_STACK_PROBE], capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok", result.stdout


def test_the_guards_return_the_real_module_when_it_is_installed():
    """The other half of the guard's contract, which the poisoned probe cannot check."""
    pytest.importorskip("rdflib", reason="the rdf extra is not installed")
    from jmfts_core.rdf import require_rdflib

    assert require_rdflib().__name__ == "rdflib"
