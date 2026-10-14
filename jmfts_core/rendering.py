"""Pages and rectangles of a PDF, as PNG. ``OFFICE_SPEC.md`` Part 7, Part 11 step 3.

``jmfts_core/pdf_extraction.py`` reads a PDF for its text and throws the geometry away;
``jmfts_core/citation_tasks.py`` re-derives that geometry and writes the rectangle a chunk
came from onto the chunk. Both of those run at ingest. This module is the other direction and
it runs on the query path: given the stored bytes and a place on a page, hand back the picture
of it.

**It holds no session, no repository and no principal.** Everything here is a function of
bytes and numbers, which is what makes the bounds below testable without a database and
without a route — and the bounds are the part with a decision in them. Access, blob lookup and
"which node's anchor is this" all live in ``services/document_service.py``, which is where the
tree is.

### Why there are two bounds and not one

``dpi`` alone does not bound anything. A page's size is the document's to choose — PDF's own
limit is 200 inches a side — so one number multiplied by an attacker-chosen page is still
unbounded. The pixel count is what the allocation actually is, so that is bounded too:

* :data:`DPI_MAX` refuses a density no screen and no printer consumes. It is about the
  request being nonsense rather than about memory.
* :data:`PIXELS_MAX` refuses the allocation. A pixmap here is 3 bytes a pixel (RGB, no
  alpha), so the limit is roughly 75 MB of pixmap plus whatever the PNG encoder wants beside
  it, and a handful of concurrent requests is still a number this appliance survives.

**Both refuse rather than clamp**, and that is the Fail Early rule rather than a preference.
A clamped render answers 200 with a picture at a density the caller did not ask for, and a
caller measuring a rectangle off that image in order to overlay something on it gets a
silently wrong scale. The refusal names the largest ``dpi`` that would fit, so a caller that
just wants a picture has the number to retry with rather than a bisection to run.
"""

from __future__ import annotations

import math
from typing import Optional

import pymupdf

#: 150 dpi is roughly twice a browser's CSS pixel density at 100% zoom, so a page rendered at
#: it is legible on screen and still crops usefully. It is also ``OFFICE_SPEC.md`` Part 7's
#: stated default for ``/image``, which is where the number came from.
DPI_DEFAULT = 150

#: The highest density this appliance will render at. 600 dpi is photographic-plate print
#: density — above it nothing consumes the extra detail, and a caller asking for it is either
#: mistaken or probing. US Letter at 600 dpi is 5100x6600, which is 33.7 Mpx and is therefore
#: already refused by :data:`PIXELS_MAX`; the two bounds overlap on purpose, because neither
#: one covers the other's case.
DPI_MAX = 600

#: The largest pixmap this appliance will allocate, in pixels. 25 Mpx is US Letter at 520 dpi
#: and A0 at 126 dpi, so every ordinary page renders at every sensible density and no single
#: request can ask for more than about 75 MB of RGB.
PIXELS_MAX = 25_000_000

#: What ``@expose`` declares for the two rendering routes, and what the pixmap is encoded as.
#: PNG and not JPEG: a page of text is line art, where JPEG's ringing lands exactly on the
#: glyph edges a reader is trying to read.
PNG_MEDIA_TYPE = "image/png"

#: What a document's ``file.detected_mime`` has to say before anything here is asked to open
#: it. Spelled beside the renderer because "what this module can read" is this module's fact;
#: ``probe.py:59`` is where the magic bytes that produce it live, and a real PDF always
#: detects by them, so an absent or different value is evidence and not an omission.
PDF_MEDIA_TYPE = "application/pdf"


class BadRenderRequest(ValueError):
    """The caller named a page, a density or a rectangle that this document has no answer for.

    A ``ValueError`` so that every service ``@expose``'ing a render already maps it to 400
    through the mapping it has for every other malformed argument. The distinction from
    :class:`RenderTooLarge` is who has to change: a bad request is wrong about THIS document
    (page 40 of a 3-page file), and a too-large one is right about the document and over a
    limit this appliance set.
    """


class RenderTooLarge(Exception):
    """The render would exceed :data:`DPI_MAX` or :data:`PIXELS_MAX` (→ HTTP 413).

    413 for the reason ``document_service.TooManyCells`` is 413: it is the RESPONSE that
    would be too large, and that is worth more to a caller than the literal reading of the
    request half of RFC 9110's definition. Every message names the limit and the ``dpi`` that
    would fit under it.
    """


class UnreadablePdf(Exception):
    """The stored bytes are a PDF this appliance cannot open (→ HTTP 409).

    Encrypted, truncated, or damaged in a way ``pymupdf`` refuses. 409 and not 500: the row
    is fine, the request is fine, and what is wrong is the state of the bytes the tree is
    holding — which is the same shape as ``SheetSourceUnavailable`` and gets the same status.
    """


def parse_bbox(raw: str) -> tuple[float, float, float, float]:
    """``"x0,y0,x1,y1"`` in PDF points, as four floats with the ends in order.

    Four numbers on one query parameter rather than four parameters, because a rectangle is
    one value: three of four arriving is not a partial rectangle, it is a malformed one, and
    a single parameter is what makes that a parse failure instead of a defaulting question.
    The order is ``pymupdf``'s own ``Rect`` order, which is what a ``source_anchor`` holds
    (``citation_tasks.py:6``) and therefore what a caller reading one off a search hit
    already has in hand.
    """
    parts = raw.split(",")
    if len(parts) != 4:
        raise BadRenderRequest(
            f"bbox {raw!r} has {len(parts)} comma-separated value(s); a rectangle is four, "
            "`x0,y0,x1,y1` in PDF points"
        )
    try:
        x0, y0, x1, y1 = (float(part) for part in parts)
    except ValueError:
        raise BadRenderRequest(
            f"bbox {raw!r} is not four numbers; a rectangle is `x0,y0,x1,y1` in PDF points"
        ) from None
    # `float()` accepts "nan" and "inf", and neither is a coordinate. Left in, a NaN
    # survives `min`/`max` (both just return whichever operand came first), reaches
    # `pymupdf.Rect`, and comes back as a PNG of nothing in particular rather than a 400.
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        raise BadRenderRequest(
            f"bbox {raw!r} has a non-finite value; a rectangle is four finite numbers, "
            "`x0,y0,x1,y1` in PDF points"
        )
    # Put the ends in order rather than refusing a reversed one: `x1 < x0` names the same
    # rectangle traversed the other way, and every reader of a PDF rect treats it so.
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _open(pdf: bytes) -> pymupdf.Document:
    """Open stored bytes, or say why they are not openable.

    ``needs_pass`` is checked here rather than left to the first page access, because an
    encrypted document opens successfully and then answers every question with nothing —
    which would render as a blank page rather than as a refusal.
    """
    try:
        doc = pymupdf.open(stream=pdf, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — pymupdf raises several unrelated types
        raise UnreadablePdf(f"these bytes cannot be opened as a PDF: {exc}") from exc
    if doc.needs_pass:
        doc.close()
        raise UnreadablePdf(
            "this PDF is encrypted and JMFTS holds no password for it, so there is no page "
            "to render"
        )
    return doc


def page_count(pdf: bytes) -> int:
    """How many pages the stored bytes hold. Used to name the range in a refusal."""
    doc = _open(pdf)
    try:
        return doc.page_count
    finally:
        doc.close()


def page_geometry(pdf: bytes) -> list[dict]:
    """Every page's size in PDF points, in order. ``[{"width": 595.3, "height": 841.9}, …]``

    Written for ``render:pdf``, which records it on the rendition (``OFFICE_SPEC.md`` Part 6:
    "a blob OID under ``file.rendition_ref``, plus page count and page geometry"), and it
    belongs here rather than in that handler for this module's stated reason — it is a
    function of bytes and numbers, testable without a database.

    **Per page rather than one size for the document.** A presentation is landscape where its
    handout pages are portrait, and a spreadsheet paginates into whatever its print area
    implies, so a caller converting a rectangle into a pixel box needs the page it is on. It
    also serves as the CHECK that a converter returned a PDF at all: bytes that are not one
    raise :class:`UnreadablePdf` here rather than being stored and discovered later.
    """
    doc = _open(pdf)
    try:
        return [
            {"width": round(page.rect.width, 2), "height": round(page.rect.height, 2)}
            for page in doc
        ]
    finally:
        doc.close()


def _check_bounds(width_pt: float, height_pt: float, dpi: int) -> None:
    """Refuse a render that is too dense or too large, naming the ``dpi`` that would fit.

    ``width_pt``/``height_pt`` are the area actually being rasterised — the page for
    ``render_page``, the clip for a crop — so cropping a big page is not refused for the
    page's size. Separated out because it is the decision, and a decision wants a test that
    does not open a PDF to reach it.
    """
    if dpi < 1:
        raise BadRenderRequest(f"dpi {dpi} is not a density; it is a positive integer")
    if dpi > DPI_MAX:
        raise RenderTooLarge(
            f"dpi {dpi} is above this appliance's maximum of {DPI_MAX}; nothing consumes a "
            f"higher density and the allocation is the caller's to choose. Retry at "
            f"{DPI_MAX} or below"
        )
    scale = dpi / 72.0
    pixels = width_pt * scale * height_pt * scale
    if pixels > PIXELS_MAX:
        # The largest dpi whose area fits: pixels scale with dpi squared, so this is the
        # square root. Floored, because the bound is `>` and a rounded-up answer would be
        # refused a second time — an error that suggests a value it would also reject is
        # worse than one that suggests nothing.
        fits = math.floor(72.0 * math.sqrt(PIXELS_MAX / (width_pt * height_pt)))
        raise RenderTooLarge(
            f"rendering {width_pt:.0f}x{height_pt:.0f} points at {dpi} dpi is "
            f"{pixels / 1e6:.1f} Mpx, above this appliance's maximum of "
            f"{PIXELS_MAX / 1e6:.0f} Mpx. Retry at {fits} dpi or below"
        )


def _is_blank_paper(pixmap: pymupdf.Pixmap) -> bool:
    """Whether every sample of ``pixmap`` is white — paper with nothing printed on it.

    White and not "one colour": a crop inside a filled box, a 1x1-point crop, or the inside
    of a glyph at 150 dpi is legitimately one colour and is a correct picture of what is
    there. Paper is what ``alpha=False`` renders the page background as.

    NOT ``Pixmap.is_unicolor``, which is a pure-Python loop over every sample: on a blank
    8.4 Mpx crop (Letter at 300 dpi) it took 40 seconds and turned a 0.13 s answer into an
    8.4 s one through REST. One ``bytes`` comparison is a ``memcmp``, against an allocation
    :data:`PIXELS_MAX` already prices.
    """
    samples = pixmap.samples
    return samples == b"\xff" * len(samples)


def render_page(
    pdf: bytes,
    *,
    page: int,
    dpi: int = DPI_DEFAULT,
    clip: Optional[tuple[float, float, float, float]] = None,
    anchor: bool = False,
) -> bytes:
    """One page, or one rectangle of it, as PNG bytes.

    ``page`` is **0-based**, matching ``PdfAnchor.page``, ``page_offsets`` and
    ``pages_with_tables``. Nothing here adds one for a human reader; a viewer that labels
    pages does that, because the record is not what a human reads
    (``jmfts_client/contracts/anchor.py``'s ``PdfAnchor.page``).

    ``clip`` is ``[x0, y0, x1, y1]`` in PDF points on that page, which is a ``source_anchor``
    bbox verbatim. It is INTERSECTED with the page: a rectangle that hangs over the edge is
    an ordinary anchor on a page whose media box the writer measured differently, and the
    part that is on the page is the answer. A rectangle that misses the page entirely is not
    — there is no picture of it — and that refuses.

    ``anchor`` says ``clip`` is a STORED ``source_anchor``, and only then is a crop of blank
    paper refused (#38). An anchor is written from where words are, so one that crops white
    paper edge to edge has missed them, and a 200 with a blank PNG is a confident picture of
    nothing. A rectangle a caller names is the caller's: blank paper is a correct picture of
    a margin, so that is answered.
    """
    doc = _open(pdf)
    try:
        if not 0 <= page < doc.page_count:
            raise BadRenderRequest(
                f"page {page} is outside this document, which holds {doc.page_count} page(s) "
                f"numbered 0 to {doc.page_count - 1} (0-based, as the anchors are)"
            )
        target = doc.load_page(page)
        rect = target.rect
        if clip is None:
            area = rect
        else:
            named = pymupdf.Rect(*clip)
            if named.is_empty:
                raise BadRenderRequest(
                    f"bbox {clip} has no area, so there is no region of page {page} to render"
                )
            area = named & rect
            if area.is_empty:
                raise BadRenderRequest(
                    f"bbox {clip} lies entirely outside page {page}, whose media box is "
                    f"{tuple(round(v, 1) for v in rect)} in points"
                )
        _check_bounds(area.width, area.height, dpi)
        # `alpha=False`: 3 bytes a pixel, which is what PIXELS_MAX is priced in, and a page
        # has nothing to be transparent against.
        pixmap = target.get_pixmap(dpi=dpi, clip=area, alpha=False)
        if anchor and clip is not None and _is_blank_paper(pixmap):
            hint = ""
            if target.rotation:
                hint = (
                    f" Page {page} is rotated {target.rotation} degrees. Anchors on rotated "
                    f"pages written before #38 was fixed were measured on the unrotated page "
                    f"and land on blank paper; re-derive them by re-running `citation` (text "
                    f"anchors) or `extract:text` then `extract:tables` (table spans)"
                )
            raise BadRenderRequest(
                f"this node's anchor, bbox {clip} on page {page}, crops blank paper: nothing "
                f"is printed inside it, so there is no picture of the passage to return.{hint}"
            )
        return pixmap.tobytes("png")
    finally:
        doc.close()
