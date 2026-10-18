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

#: The OTHER anchor's two rows — ``OFFICE_SPEC.md`` Part 5's "Two anchors, not one", Block G
#: step 33. A ``.docx`` carries an exact ``ooxml`` address in ``source_anchor`` AND a
#: rectangle RECOVERED from its rendition in ``render_anchor``, and they are two rows because
#: they are two facts with different truth conditions. A client reads them the same way it
#: reads the pair above, with :func:`parse_anchor`; what it gets back from the second is a
#: :class:`PdfAnchor` whose ``of`` is ``"rendition"``.
RENDER_ANCHOR_ROW = "render_anchor"
RENDER_ANCHOR_UNRESOLVED_ROW = "render_anchor.unresolved"


class PdfAnchor(BaseModel):
    """A page and a rectangle in a PDF.

    Written by ``citation_tasks.anchor_for_span`` (a source PDF's own text layer) and by
    ``citation_tasks.anchor_for_rendition_span`` (an office document's rendition). The shape
    is ``{"kind": "pdf", "page": 3, "bbox": [72.0, 118.4, 540.0, 262.9]}``, with ``of``
    saying which of the two.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["pdf"] = "pdf"

    #: WHICH PDF, and therefore how much the rectangle can be trusted. ``"source"`` — the
    #: default, and what every anchor written before Block G step 33 means — is the
    #: document's own text layer, where the coordinates are the ones extraction computed and
    #: are exact. ``"rendition"`` is a page LibreOffice paginated (``OFFICE_SPEC.md`` Part 6),
    #: where the rectangle was RECOVERED by matching the passage's text and is approximate:
    #: two LibreOffice versions paginate differently, and the recovery can fail outright, in
    #: which case there is a ``render_anchor.unresolved`` row and no anchor at all.
    #:
    #: **It defaults rather than being required, and that is not laziness.** Making it
    #: required would change what every stored ``source_anchor`` row means — they were all
    #: written before the field existed, they are all a source PDF's own geometry, and a
    #: default that says so is the only reading under which an old row and a new one parse
    #: alike. The server writes the key only for a rendition anchor, for the same reason.
    #:
    #: A viewer that draws a rectangle should say which it has. "Page 3" and "page 3 of our
    #: rendering of this document" are different claims, and the second one stops being
    #: surprising the moment a reader opens the ``.docx`` in Word and finds the passage on
    #: page 4.
    of: Literal["source", "rendition"] = "source"

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

    #: The sha256 of the rendition bytes the rectangle was measured on, for
    #: ``of="rendition"`` only (0.7.1). A re-render replaces the rendition and keeps the
    #: chunks, so this is what tells a current rectangle from last pagination's: the server
    #: refuses (409) a rendition anchor whose hash is not the stored rendition's, and one with
    #: no hash at all, which was written before 0.7.1 and cannot be checked. A viewer drawing
    #: the row client-side should compare it with the ``rendition`` evidence's
    #: ``content_hash`` the same way.
    rendition_hash: Optional[str] = None


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


class OoxmlAnchor(BaseModel):
    """An element of an OOXML package, by part and XPath.

    ``{"kind": "ooxml", "part": "word/document.xml", "path": "/w:document/w:body/w:p[42]"}``,
    written by ``anchor:source`` (``jmfts_core/citation_tasks.py:424``). ``OFFICE_SPEC.md``
    Part 5's SOURCE anchor: exact, permanent, and computable from the uploaded bytes, which is
    what makes it a different task from ``citation`` rather than another format in its reader
    table. A rectangle on a rendered page is the OTHER anchor and is recorded separately.

    **This has no rectangle, and that is not a gap.** A ``.docx`` has no pages until something
    paginates it, so nothing here can be drawn on a page image on its own. The rectangle is
    the OTHER anchor: once ``render:pdf`` (Block G step 32) has produced a rendition and
    ``anchor:render`` (step 33) has recovered geometry from it, the same chunk also carries a
    :class:`PdfAnchor` with ``of="rendition"`` in its :data:`RENDER_ANCHOR_ROW`.

    **When a node has both, this one is what it IS and that one is where to LOOK.** They do
    not compete: this address cannot be wrong and cannot be drawn, the rectangle can be drawn
    and can be wrong. So an overlay draws the rectangle and names this as the thing the
    rectangle is a picture of — and when the rectangle is absent, this is still the answer to
    "where did that come from", which is the same answer a span highlight gives a format with
    no pages.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["ooxml"] = "ooxml"
    #: The package part, e.g. ``word/document.xml`` or ``ppt/slides/slide1.xml``.
    part: str
    #: An XPath into that part, resolvable with the three-prefix namespace table
    #: ``anchor:source`` binds. Not parsed here, for ``CellsAnchor.ref``'s reason: a second
    #: grammar in the client would be a second grammar.
    path: str
    #: The slide number, for a ``pptx`` part that has one. Absent for ``docx``.
    slide: Optional[int] = None
    #: The last element of the SAME part the passage covers, when it spans more than one.
    #: Absent when the passage is one element.
    through: Optional[str] = None
    #: Further PARTS the passage runs into, in order — a chunk running from a slide into its
    #: notes. Absent, not empty, when it stays in one part.
    #:
    #: Note the type: :attr:`PdfAnchor.continues` is a list of page NUMBERS and this is a list
    #: of part NAMES. Both answer "where else does this passage go", and neither can be read
    #: as the other, which is why they are fields on two models rather than one shared field.
    continues: Optional[list[str]] = None


SourceAnchor = Annotated[
    Union[PdfAnchor, CellsAnchor, SpanAnchor, OoxmlAnchor], Field(discriminator="kind")
]


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
    #: For a ``*_failed`` code (0.7.1): the error the anchor task's last attempt failed
    #: with, so "this passage was never placed because the task died" is not read as "this
    #: passage was never anchored". The task's attempt log holds the same text.
    error: Optional[str] = None
    #: For a ``render_anchor.unresolved`` row: the rendition the alignment looked in.
    rendition_hash: Optional[str] = None


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
            f"anchor kind {kind!r} is not one of pdf, cells, span, ooxml. This client is "
            "older than the appliance that wrote it."
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
    for model in (PdfAnchor, CellsAnchor, SpanAnchor, OoxmlAnchor):
        if kind == model.model_fields["kind"].default:
            return model.model_validate(row)
    raise UnknownAnchorKind(kind)
