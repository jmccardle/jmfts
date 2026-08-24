"""The office readers are an extra, and the base install has to stay able to say so.

``docs/OFFICE_SPEC.md`` Part 1 splits office support into three tiers. Two claims hold
that split up, and they are different in kind — the same two ``tests/test_thin_worker.py``
makes about the model stack, at a smaller scale.

The **structural** claim is that nothing on the app's import path reaches for a tier-2
reader. It needs a subprocess: by the time this test runs, pytest has imported most of
the tree, so asserting on this process's ``sys.modules`` would prove nothing about what
a fresh one loads.

The **packaging** claim is that a missing reader is a NAMED state rather than a
traceback — because an install with no ``python-docx`` is usually correct. Base JMFTS
detects office formats from the ZIP manifest and probes what they declare using nothing
but the standard library, so a storage-side worker that never ingests one is properly
installed and properly has no reader.

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

#: The tier-2 readers. Named here so the two probes below and the base-install script
#: agree on what "an office library" means.
TIER_TWO = ("docx", "pptx", "openpyxl")


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


def test_the_office_extra_carries_the_three_readers():
    declared = _requirement_names(EXTRAS["office"])
    assert declared == {"python-docx", "python-pptx", "openpyxl"}, declared


def test_the_convert_extra_is_the_libreoffice_client_only():
    """Tier 3 is a worker, not a wheel. The extra is only what talks to it.

    A reader appearing here would mean the tiers had collapsed: the point of the split
    is that a fleet runs ONE LibreOffice-badged worker, while any number of installs
    read OOXML directly.
    """
    declared = _requirement_names(EXTRAS["convert"])
    assert declared == {"unoserver"}, declared


def test_the_sketch_extra_carries_the_one_package():
    """``INGEST_SPEC.md`` 8.6's containment search, and nothing else.

    numpy is deliberately absent: it is already a base dependency, and a second
    declaration would be free to pin a version the embeddings were never tested against.
    """
    assert _requirement_names(EXTRAS["sketch"]) == {"datasketch"}


def test_dev_implies_office_but_not_convert():
    """The suite opens real packages, so it must install the readers it tests.

    It must NOT install ``unoserver``: that wheel installs cleanly with no LibreOffice
    underneath it and is useless without one, so a suite carrying it would be asserting
    against a configuration nobody deploys.
    """
    dev = _requirement_names(EXTRAS["dev"])
    assert "jmfts[office]" in [s.strip() for s in EXTRAS["dev"]]
    # `sketch` too: `profile:sheet` sketches every column by default, so a suite without
    # it would only ever exercise the way out of that default.
    assert "jmfts[sketch]" in [s.strip() for s in EXTRAS["dev"]]
    assert "unoserver" not in dev


def test_olefile_is_a_base_dependency_and_not_an_extra():
    """Probe always runs, and probe is what needs it.

    An encrypted ``.docx`` is not a ZIP — it is an OLE2 file holding ``EncryptionInfo``
    and ``EncryptedPackage`` streams, so it carries the same magic bytes as a legacy
    ``.doc``. Probe has to tell those two apart, because one gets converted and the other
    must fail with a named reason, and ``INGEST_SPEC.md`` Part 11.2 requires that
    decision to be a pure function of ``(format, patterns, options)``. Behind an extra,
    a base install could not make it.
    """
    base = _requirement_names(PYPROJECT["project"]["dependencies"])
    assert "olefile" in base
    for name, specifiers in EXTRAS.items():
        assert "olefile" not in _requirement_names(specifiers), (
            f"olefile is declared in the {name!r} extra as well as in base; the second "
            "copy is free to pin a different version than probe was tested against"
        )


def test_the_office_package_is_shipped():
    """``packages.find`` uses a glob, so a new subpackage is easy to add and forget."""
    include = PYPROJECT["tool"]["setuptools"]["packages"]["find"]["include"]
    assert any(pattern.startswith("jmfts_core") and pattern.endswith("*") for pattern in include)
    assert (REPO / "jmfts_core" / "office" / "__init__.py").is_file()


# ---------------------------------------------------------------------------
# The structural claim
# ---------------------------------------------------------------------------


PROBE = """
import sys

import jmfts_core.rest.main       # the whole API, every router
import jmfts_core.worker          # the CLI and the loop
import jmfts_core.ingest_tasks    # every registered task handler
import jmfts_core.probe           # format detection, which must stay tier 1
import jmfts_core.office          # the seam itself: importing it imports no reader
import jmfts_core.sketch          # the other seam, same rule
import jmfts_core.office.sheets   # 8.3's measurer, which reaches for BOTH at call time

loaded = sorted(m for m in ("docx", "pptx", "openpyxl", "datasketch") if m in sys.modules)
print(",".join(loaded))
"""


def test_starting_the_app_imports_no_office_reader():
    """The claim that keeps the base install able to probe without being able to read.

    In a SUBPROCESS, for the reason the module docstring gives.

    Note what this proves in each environment. Where the readers ARE installed (CI, which
    installs ``dev``), it proves nothing on the import path reaches for them. Where they
    are NOT, it proves the app imports cleanly without them. Both are the claim; neither
    alone is.
    """
    result = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        f"starting the app imported {result.stdout.strip()}; something on the import "
        "path has grown an eager optional import. Move it behind a require_* guard in "
        "jmfts_core/office/__init__.py or jmfts_core/sketch.py."
    )


# ---------------------------------------------------------------------------
# The packaging claim
# ---------------------------------------------------------------------------


#: Simulate the absent readers by poisoning ``sys.modules``, which makes ``import docx``
#: raise ``ImportError: import of docx halted; None in sys.modules`` — the same exception
#: class the guards catch. ``scripts/check_base_install.sh`` is the version with no
#: simulation in it: it builds a clean venv, installs base, and asserts the same things.
MISSING_STACK_PROBE = """
import sys

from jmfts_core.office import (
    OfficeStackNotInstalled,
    require_docx,
    require_openpyxl,
    require_pptx,
)
from jmfts_core.task_errors import ErrorType, classify_exception

sys.modules["docx"] = None
sys.modules["pptx"] = None
sys.modules["openpyxl"] = None

for label, guard in (
    ("docx", require_docx),
    ("pptx", require_pptx),
    ("openpyxl", require_openpyxl),
):
    try:
        guard()
    except OfficeStackNotInstalled as exc:
        assert "jmfts[office]" in str(exc), f"{label}: message does not name the extra"
        assert classify_exception(exc) is ErrorType.PERMANENT, f"{label}: not PERMANENT"
    else:
        raise AssertionError(f"{label}: the guard returned a module that is not there")

# Detection keeps working with every reader gone. This is the whole reason the split is
# possible: a base install can still say WHAT a file is and what it declares.
from jmfts_core.probe import detect_format

assert detect_format(b"%PDF-1.7 ...", filename="x.pdf").format == "pdf"
print("ok")
"""


def test_a_missing_office_stack_is_a_named_permanent_error():
    result = subprocess.run(
        [sys.executable, "-c", MISSING_STACK_PROBE], capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok", result.stdout


MISSING_SKETCH_PROBE = """
import sys

from jmfts_core.sketch import SketchStackNotInstalled, require_datasketch
from jmfts_core.task_errors import ErrorType, classify_exception

sys.modules["datasketch"] = None

try:
    require_datasketch()
except SketchStackNotInstalled as exc:
    assert "jmfts[sketch]" in str(exc), "the message does not name the extra"
    assert "sketch_columns" in str(exc), "the message does not name the other way out"
    assert classify_exception(exc) is ErrorType.PERMANENT, "not PERMANENT"
else:
    raise AssertionError("the guard returned a module that is not there")

print("ok")
"""


def test_a_missing_sketch_stack_is_a_named_permanent_error():
    """The same contract the office guards have, for the same reason.

    An install with no ``datasketch`` can still probe, extract, chunk, embed and search;
    what it cannot do is make a high-cardinality column findable by ``propose:links``. The
    message has to say which of those two deployments you are in, and it names BOTH ways
    out — the extra, and the ``sketch_columns`` parameter that measures the sheet without
    sketching it.
    """
    result = subprocess.run(
        [sys.executable, "-c", MISSING_SKETCH_PROBE], capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok", result.stdout


def test_the_guards_return_the_real_module_when_it_is_installed():
    """The other half of the guard's contract, which the poisoned probe cannot check."""
    pytest.importorskip("docx", reason="the office extra is not installed")
    from jmfts_core.office import require_docx

    assert require_docx().__name__ == "docx"
