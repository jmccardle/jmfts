"""Reading and writing OOXML packages. The ``office`` extra, and the only door to it.

``docs/OFFICE_SPEC.md`` Part 1 splits office support into three tiers by what the
dependency actually is, and this package is the boundary between the first two.

**Tier 1 — stdlib, base install.** ``zipfile`` and ``xml.etree``, plus ``olefile`` for
the OLE2 container. That is enough to detect an office format, to read what it declares
about its own structure, and to tell an encrypted package from a legacy binary one. It
lives in :mod:`jmfts_core.probe`, not here, and it must never gain an import from this
module: ``probe`` "depends on nothing, calls no model, and always runs"
(``INGEST_SPEC.md`` Part 4), and an install that could accept a ``.docx`` but not probe
it would report an empty pattern set — which is indistinguishable from a ``.docx`` that
genuinely declares nothing.

**Tier 2 — this package.** ``python-docx``, ``python-pptx``, ``openpyxl``. Needed to
open a package and get its content out, or to write one back. Every import of them goes
through a ``require_*`` function below, at the point of use, never at module scope.

**Tier 3 — LibreOffice.** Not importable at all; it is a badged worker and a URL. See
the ``convert`` extra in pyproject.toml.

The shape of tiers 1 and 2 is the one :mod:`jmfts_core.embedding` already uses for the
model stack, and it is copied deliberately rather than reinvented. The claim being made
is the same one: **an install that cannot read office files is a supported deployment,
not a broken one.** It is what most of a fleet is. So the failure has to say which
deployment you are in, not read as a missing dependency.

``tests/test_office_packaging.py`` asserts that nothing on the app's import path reaches
for tier 2, and ``scripts/check_base_install.sh`` asserts it against a real base venv.
"""

from __future__ import annotations


class OfficeStackNotInstalled(ImportError):
    """This install can detect office files but not open them, and something asked it to.

    Raised instead of letting a bare ``ModuleNotFoundError: No module named 'docx'``
    reach the caller. That message describes a broken environment, and this one usually
    is not: base JMFTS detects ``.docx``/``.pptx``/``.xlsx`` from the ZIP manifest and
    probes what they declare, all without a reader, so a storage-side worker that never
    ingests an office file is correctly installed and correctly has no ``python-docx``.

    Classified PERMANENT by :mod:`jmfts_core.task_errors`, along with every other
    ``ImportError`` (``task_errors.py``): a package that is not installed does not appear
    on the third attempt, and spending the retry budget on it only delays the moment
    somebody reads this.
    """


#: What to do about it. One string, because the message is the whole value of the
#: exception and two copies of it would be free to drift.
#:
#: It names the extra and nothing else. There is no remote equivalent of
#: ``JMFTS_RUNNER_URL`` here — the office readers cost 44 MB installed (measured
#: 2026-08-22, base 585 MB against ``[office]`` 629 MB), so "install it" is the whole
#: answer, where for the model stack's 4.6 GB it is one of two.
_INSTALL_HINT = (
    "This JMFTS was installed without the office readers, so it can detect and probe "
    "office files but cannot open one. Install them:\n"
    "    pip install 'jmfts[office]'\n"
    "A worker that does not ingest office files does not need them; see "
    "jmfts_core/office/__init__.py."
)


def require_docx():
    """The ``docx`` module, or say what is missing and what to do."""
    try:
        import docx
    except ImportError as exc:
        raise OfficeStackNotInstalled(f"{exc}\n\n{_INSTALL_HINT}") from exc
    return docx


def require_pptx():
    """The ``pptx`` module, or say what is missing and what to do."""
    try:
        import pptx
    except ImportError as exc:
        raise OfficeStackNotInstalled(f"{exc}\n\n{_INSTALL_HINT}") from exc
    return pptx


def require_openpyxl():
    """The ``openpyxl`` module, or say what is missing and what to do.

    Callers open workbooks ``read_only=True``. That is not a preference: without it
    openpyxl materialises the entire cell grid as Python objects, and ``INGEST_SPEC.md``
    Part 8 profiles sheets of a million rows.
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise OfficeStackNotInstalled(f"{exc}\n\n{_INSTALL_HINT}") from exc
    return openpyxl


__all__ = [
    "OfficeStackNotInstalled",
    "require_docx",
    "require_openpyxl",
    "require_pptx",
]
