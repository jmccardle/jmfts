"""``anchor:render``'s alignment against what a REAL LibreOffice draws — audit item #8.

``tests/test_render_anchors.py`` builds its rendition with PyMuPDF out of the package's own
strings, so it proves the rows and the refusals and cannot measure the recovery rate. Here
each package in :mod:`tests.office_renditions` is rendered by ``soffice`` and pushed through
the shipped ``_located_blocks``, and the rate ``located / (source - not_rendered)`` has a
floor per case.

**THE RATES, MEASURED 2026-10-18 against LibreOffice 7.4.7 and PyMuPDF 1.28.2.** Before the
0.7.1 matcher (exact/prefix, one-to-one) and after it:

    case                    before   after
    docx_bullets             3/7      7/7
    docx_numbered            2/6      6/6
    docx_table               3/4      4/4
    docx_table_borderless    2/3      3/3
    docx_merged              2/6      6/6
    docx_hyphenation         3/4      4/4
    pptx_deck                7/11     8/8   (+3 not_rendered: notes, its label, `Slide 3`)

The floor asserted below is ALL of them for every case, because every case is a thing a
renderer does on ordinary documents and none of them is a corner. A LibreOffice upgrade that
moves a number is a finding to read, not a floor to lower.

**WHEN THIS RUNS.** ``soffice`` on ``PATH``, or the module skips — LOUDLY, naming what is
missing. CI does not leave it to that: the ``renditions`` job in ``.github/workflows/ci.yml``
installs LibreOffice and sets ``JMFTS_REQUIRE_SOFFICE=1``, under which a missing ``soffice``
is a FAILURE. A gate that skipped what it exists to run would be a green check over nothing.
No database is used.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("docx", reason="the office extra is not installed")
pytest.importorskip("pptx", reason="the office extra is not installed")
pymupdf = pytest.importorskip("pymupdf")

from jmfts_core.citation_tasks import (  # noqa: E402
    UNRESOLVED_NOT_RENDERED,
    _located_blocks,
    anchor_for_rendition_span,
    normalize_for_match,
)
from jmfts_core.office.extract import (  # noqa: E402
    NOT_RENDERED,
    docx_to_markdown,
    pptx_to_markdown,
)
from jmfts_core.pdf_extraction import pdf_to_markdown  # noqa: E402
from tests.office_renditions import CASES, SOFFICE, render_with_soffice  # noqa: E402

if SOFFICE is None:
    if os.environ.get("JMFTS_REQUIRE_SOFFICE") == "1":
        raise RuntimeError(
            "JMFTS_REQUIRE_SOFFICE=1 and there is no `soffice` on PATH: the real-LibreOffice "
            "rendition tests would have been skipped by the job that exists to run them"
        )
    pytest.skip(
        "NO `soffice` ON PATH: the real-LibreOffice recovery-rate tests (audit #8) did NOT "
        "run. Install LibreOffice to run them; CI's `renditions` job does.",
        allow_module_level=True,
    )

READERS = {"docx": docx_to_markdown, "pptx": pptx_to_markdown}

#: located / renderable source blocks, per case. See the module docstring.
MINIMUM_RATE = {name: 1.0 for name in CASES}


@pytest.fixture(scope="module")
def rendered() -> dict:
    """Every case, rendered once. ~1.5 s a document with a cold profile."""
    out = {}
    for name, (build, suffix) in CASES.items():
        data = build()
        markdown, meta = READERS[suffix](data)
        pdf = render_with_soffice(data, suffix)
        rendered_markdown, rendered_meta = pdf_to_markdown(pdf)
        blocks = _located_blocks(
            (markdown, meta["source_blocks"]), (rendered_markdown, rendered_meta["text_blocks"])
        )
        out[name] = (markdown, blocks, pdf)
    return out


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_recovery_rate_meets_its_floor(rendered, name):
    markdown, blocks, _ = rendered[name]
    renderable = [block for block in blocks if not block.get(NOT_RENDERED)]
    located = [block for block in renderable if "bbox" in block]
    missing = [markdown[b["start"] : b["end"]][:80] for b in renderable if "bbox" not in b]
    rate = len(located) / len(renderable)
    assert rate >= MINIMUM_RATE[name], (
        f"{name}: located {len(located)}/{len(renderable)} renderable source blocks; "
        f"not located: {missing}"
    )


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_located_rectangle_holds_its_own_text(rendered, name):
    """A rate is worth nothing if the rectangles are wrong. The text LibreOffice printed
    inside each recorded rectangle, normalized the way the matcher normalizes, has to
    contain the source block's own text — so a block merged with its neighbours passes
    (the rectangle is the whole merged block, which is block-granular and audit item #6),
    and a block located in the wrong place does not."""
    markdown, blocks, pdf = rendered[name]
    document = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        for block in blocks:
            if "bbox" not in block:
                continue
            page = document[block["page"]]
            inside = page.get_textbox(pymupdf.Rect(block["bbox"]) + (-1, -1, 1, 1))
            own = normalize_for_match(markdown[block["start"] : block["end"]])
            assert own in normalize_for_match(inside), (
                f"{name}: {markdown[block['start']:block['end']][:60]!r} is not inside its "
                f"rectangle {block['bbox']} on page {block['page']}, which holds {inside[:120]!r}"
            )
    finally:
        document.close()


def test_speaker_notes_are_reported_as_never_rendered(rendered):
    """Audit item #21. A chunk of speaker notes is not a passage the renderer lost — a slide
    export never draws notes — and the code has to say which, or someone hunts a conversion
    defect that is not there."""
    markdown, blocks, _ = rendered["pptx_deck"]
    notes = next(b for b in blocks if markdown[b["start"] : b["end"]].startswith("Speaker notes"))
    assert notes.get(NOT_RENDERED)
    starts = [b["start"] for b in blocks]
    anchor, code = anchor_for_rendition_span(blocks, starts, notes["start"], notes["end"])
    assert anchor is None
    assert code == UNRESOLVED_NOT_RENDERED
