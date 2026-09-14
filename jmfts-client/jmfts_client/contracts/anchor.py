"""IC-2: where a passage came from, as a shape three consumers agree on.

``docs/SPRINT_0_6_0.md`` Block F step 17. An **anchor** is a stable address of a region of a
source document, recorded on the node that region produced. ``jmfts_core/citation_tasks.py``
has written them since 0.4.0 and ``docs/OFFICE_SPEC.md`` Part 5 specifies them; what did not
exist until now is a typed form, because the only consumer was a task handler reading back
its own dict.

Block F gives it three consumers that must agree, in three different worktrees:

* step 18 puts an anchor on a search hit, so a result can offer "show me where" without a
  second call per hit;
* step 22 takes one as ``GET /documents/{id}/region?anchor=true``, and crops to it;
* step 25's overlay draws it on a rendered surface.

**These models parse the evidence rows; they do not replace them.** The stored form stays a
JSONB dict written by the handler, because ``document_evidence`` is a general store and
narrowing it to a union of four shapes would be the wrong place to put the constraint. What
is here is the reader, and :func:`parse_anchor` is where an unrecognised ``kind`` becomes a
refusal rather than a silently dropped highlight.

**Coordinates are the source document's own, never the viewer's.** A PDF box is in points
with the origin where ``pymupdf`` puts it; a spreadsheet region is an A1 range. Converting to
screen pixels needs the rendered surface's scale and is the overlay's job (IC-9). Putting a
pixel value in here would bake one viewer's zoom level into the stored record.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

#: The evidence row an anchor lives in, and the row a failure to recover one lives in.
#: Spelled here so a client can read them out of ``DocumentEvidenceResponse`` without
#: importing the server. ``jmfts_core/evidence.py:342`` and ``:448`` are the declarations;
#: ``:453`` records that the second is "present exactly when `anchor` is not".
ANCHOR_ROW = "source_anchor"
ANCHOR_UNRESOLVED_ROW = "source_anchor.unresolved"


class PdfAnchor(BaseModel):
    """A page and a rectangle in a PDF.

    Written by ``citation_tasks.anchor_for_span``. The shape is
    ``{"kind": "pdf", "page": 3, "bbox": [72.0, 118.4, 540.0, 262.9]}``.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["pdf"] = "pdf"

    #: **0-based**, matching ``page_offsets`` and ``pages_with_tables`` in the same
    #: extraction record. A viewer that labels pages for a human adds one; nothing in the
    #: record does it for them, because the record is not what a human reads.
    page: int = Field(..., ge=0)

    #: ``[x0, y0, x1, y1]`` in PDF points, which is what ``pymupdf`` takes back.
    bbox: tuple[float, float, float, float]

    #: Further pages the passage runs onto, in order, when it crosses a page break.
    #: Absent — not empty — when it does not.
    #:
    #: The page above is the one the passage BEGINS on and the rectangle covers only that
    #: page, which ``anchor_for_span``'s docstring argues for at length: a reader clicking a
    #: citation wants to be taken to where the passage starts, and a rectangle that looked
    #: complete when it was not would be the real failure. This field is what keeps the two
    #: cases distinguishable, so an overlay can say "continues on p. 4" rather than
    #: presenting a partial box as a whole one.
    continues: Optional[list[int]] = None


class CellsAnchor(BaseModel):
    """A rectangular region of one worksheet.

    ``{"kind": "cells", "sheet": "Q3 Pipeline", "ref": "B4:H120"}``, per
    ``jmfts_core/services/document_service.py:141``. ``GET /documents/{id}/cells`` already
    serves this region; ``_cells_bounds`` (`:154`) resolves it in the spec's order — what the
    caller named, then the node's own anchor, then the measured used range.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["cells"] = "cells"
    sheet: str
    #: An A1 range, e.g. ``B4:H120``. Not parsed here: ``jmfts_core/office/cells.py`` owns
    #: that grammar and a second parser in the client would be a second grammar.
    ref: str


class SpanAnchor(BaseModel):
    """A character range in the extracted markdown, for a document with no geometry.

    The ``source_span`` row (``jmfts_core/atoms.py:191``) rather than ``source_anchor``, and
    it is included here because it is what a plain-text or converted document can offer. An
    overlay cannot draw it on a page, but a renderer can highlight it in the text it is
    already showing — which is the same feature for a format that has no pages.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["span"] = "span"
    char_start: int = Field(..., ge=0)
    char_end: int = Field(..., ge=0)


SourceAnchor = Annotated[Union[PdfAnchor, CellsAnchor, SpanAnchor], Field(discriminator="kind")]


class UnresolvedAnchor(BaseModel):
    """Why a passage got no anchor, by code and reason.

    The ``source_anchor.unresolved`` row. Written as
    ``{"code": ..., "reason": UNRESOLVED_REASON[code]}`` at
    ``jmfts_core/citation_tasks.py:284``.

    **This is why the front end can tell two different failures apart.** "This document has
    no highlights" and "this passage's rectangle could not be recovered, because the chunk
    predates ``source_span``" are different answers, and a viewer that showed nothing for
    both would be hiding the second. Partial recovery is a COMPLETED task in this appliance,
    not a failed one, so the unresolved rows are an ordinary outcome rather than an error.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    reason: str


class UnknownAnchorKind(ValueError):
    """An anchor row named a ``kind`` this client has no model for.

    Raised rather than returning ``None``, and the reason is the version skew this whole
    package exists to make visible: a client older than the appliance meets a kind that
    arrived with an office format, and the alternative is a highlight that silently never
    appears. The message names the kind so the answer is "upgrade the client", not "the
    citation feature is broken for some documents".
    """

    def __init__(self, kind: object) -> None:
        super().__init__(
            f"anchor kind {kind!r} is not one of pdf, cells, span. This client is older "
            "than the appliance that wrote it."
        )
        self.kind = kind


def parse_anchor(row: object) -> SourceAnchor:
    """Read one stored ``source_anchor`` dict into its model.

    The dict comes from ``DocumentEvidenceResponse.evidence`` or from a search hit. A row
    that is not a mapping, or whose ``kind`` is unknown, raises
    :class:`UnknownAnchorKind` — an anchor nobody can interpret is not a highlight that is
    merely missing.
    """
    if not isinstance(row, dict):
        raise UnknownAnchorKind(type(row).__name__)
    kind = row.get("kind")
    for model in (PdfAnchor, CellsAnchor, SpanAnchor):
        if kind == model.model_fields["kind"].default:
            return model.model_validate(row)
    raise UnknownAnchorKind(kind)
