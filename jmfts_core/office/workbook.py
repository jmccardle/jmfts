"""The sheet list a workbook declares. ``docs/INGEST_SPEC.md`` 8.1.

8.1: *"A workbook declares exactly one structural fact: it contains named sheets. That is
the whole ``declared`` rung for ``.xlsx``."* This module reads that one fact and nothing
else. There is no cell here, no used range and no measurement — 8.3's signals belong to
``profile:sheet``, which is a per-sheet task with its own claim and its own failure.

TIER 2, and the reason this file is in :mod:`jmfts_core.office` rather than beside the PDF
reader. ``openpyxl`` is the ``office`` extra; the import below happens inside the function,
behind :func:`~jmfts_core.office.require_openpyxl`, and
``tests/test_office_packaging.py::test_starting_the_app_imports_no_office_reader`` fails if
it ever climbs to module scope.

**``read_only=True`` is not a preference.** Without it openpyxl materialises every cell of
every sheet as a Python object before ``sheetnames`` can be read, and Part 8 is written for
workbooks of a million rows. The whole cost of this function should be the workbook part.

**This is not a second implementation of ``has_sheets``.** ``jmfts_core.probe._probe_xlsx``
reads the same ``xl/workbook.xml`` with the standard library and reports whether the list
is non-empty; that is the SCHEDULING input, it runs in a base install, and it decides
whether this function is ever called. What this adds is a reader — and the two lists are
not guaranteed to be equal, which is a fact about the format rather than a defect: see
:func:`read_sheets`.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

# The guard, not the reader. Importing `jmfts_core.office` reaches no office library.
from jmfts_core.office import require_openpyxl


@dataclass(frozen=True)
class Sheet:
    """One entry of the workbook's own sheet list.

    ``index`` is the position in ``xl/workbook.xml``, which is the order the tabs appear
    in and the order a caller addressing "the third sheet" means. It is carried rather
    than left to the node's ``position`` because position is the tree's business and can
    be changed by a later reparent; this is what the file said.

    ``state`` is ``visible``, ``hidden`` or ``veryHidden``. Declared, not measured — it is
    in the workbook part beside the name — so it belongs to this rung. A hidden sheet is
    still a sheet and still gets a node: hiding a lookup table is how workbooks are built,
    and dropping it here would silently remove the half of the workbook that the visible
    half references.
    """

    index: int
    name: str
    state: str


def read_sheets(data: bytes) -> list[Sheet]:
    """Every sheet the workbook names, in workbook order.

    **Openpyxl's list and probe's list can disagree, and neither is wrong.** ``<sheets>``
    in ``xl/workbook.xml`` also holds chartsheets, dialog sheets and legacy macro sheets;
    probe counts them, because at tier 1 a ``<sheet>`` element is all there is to count.
    openpyxl resolves each entry to a part, so it may raise on a workbook probe measured
    happily. That is left to raise here rather than caught: a workbook this appliance
    cannot open is a permanent, named failure on one file, and skipping the entries that
    did not resolve would produce a partial tree that reads as a complete one.
    """
    openpyxl = require_openpyxl()
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
    try:
        return [
            Sheet(index=index, name=name, state=workbook[name].sheet_state)
            for index, name in enumerate(workbook.sheetnames)
        ]
    finally:
        # A read-only workbook holds the ZIP open, and the worker that called this is about
        # to do database work in the same transaction.
        workbook.close()
