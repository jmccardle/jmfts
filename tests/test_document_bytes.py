"""The three byte routes: ``/blob``, ``/image`` and ``/region``. ``OFFICE_SPEC.md`` Part 7.

``docs/SPRINT_0_6_0.md`` Block F steps 20, 21 and 22, which are ``OFFICE_SPEC.md`` Part 11
step 3 — the step that document scheduled *before* any office format is read, and which had
no code until now. Steps 1 and 2 of that table shipped in 0.4.0 and wrote the anchors these
routes read back (``tests/test_citation_task.py`` is where those are proved right); this file
is the other half, where a stored anchor becomes a picture.

Four things are under test and they are different in kind.

The **bounds**: :mod:`jmfts_core.rendering` refuses a density and an allocation, and it
refuses rather than clamping. No database and no tree — the decision is a function of a page
size and a number, and a clamp would pass every "does it return a PNG" test while handing a
caller an image at a scale it did not ask for and cannot detect.

The **address**: which page and which rectangle, from the node's own ``source_anchor``. The
four ways that fails are four different facts and each gets its own test, because the whole
point of ``source_anchor.unresolved`` being a second row is that a consumer can tell them
apart (``evidence.py:453``).

The **wrong kind of node**: a ``cells`` anchor is answered with ``/cells`` and a ``span``
anchor with "there is no rectangle", not with a picture. ``docs/SPRINT_0_6_0.md`` step 22 is
explicit that a spreadsheet region is served as cells, and a route that rendered one anyway
would be a second, worse answer to a question that already has one.

The **verb**, end to end on the real tree: upload a PDF, drain the queue, and read bytes,
pages and rectangles back off the nodes the ingest wrote — including through the mounted
route, so the query-parameter binding, the binary response and the statuses are the ones a
client actually sees.

**THE FIXTURE IS BUILT, NOT COMMITTED**, which is ``tests/corpus/fixtures.py``'s rule and
``tests/test_citation_task.py``'s practice: a PDF in git is a binary blob nobody reviews.
``pymupdf`` writes it here because a PDF rich enough to have several rectangles on several
pages needs a writer, and ``corpus/fixtures.py``'s hand-assembled ``minimal_pdf`` is one page
with one line — deliberately, since its bytes are hash-pinned and a writer's are not.
"""

from __future__ import annotations

import struct
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import jmfts_core.ingest_tasks  # noqa: F401  (import order; see the module cycle)
from jmfts_client.contracts.anchor import ANCHOR_ROW, ANCHOR_UNRESOLVED_ROW
from jmfts_client.contracts.binary import BinaryPayload
from jmfts_client.contracts.upload import UploadedFile
from jmfts_core import rendering
from jmfts_core.citation_tasks import UNRESOLVED_NO_SPAN, UNRESOLVED_REASON
from jmfts_core.database import get_db
from jmfts_core.models.document import Document, USETYPE_CHUNK
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import CurrentPrincipal, reset_principal, set_principal
from jmfts_core.rendering import (
    DPI_MAX,
    PIXELS_MAX,
    BadRenderRequest,
    RenderTooLarge,
    UnreadablePdf,
    parse_bbox,
    render_page,
)
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.rest.main import app
from jmfts_core.services.document_service import (
    BlobUnavailable,
    DocumentService,
    NotAPdfSource,
    RegionNotAddressable,
)
from jmfts_core.services.ingest_service import IngestService
from tests.conftest import AUTH_HEADERS, drain_ingest_queue

pymupdf = pytest.importorskip("pymupdf")

#: US Letter in points, which is what the fixture's pages are. Spelled once so the bound
#: arithmetic below reads as arithmetic rather than as two magic numbers.
LETTER_PT = (612.0, 792.0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_pdf() -> bytes:
    """Three US Letter pages, several distinctly-worded paragraphs on each.

    Several paragraphs because a page with one block cannot tell a rectangle that is right
    from one that is merely the whole page, and distinct wording because a chunk's anchor is
    checked by re-reading the text inside the rectangle it names. Short lines because
    ``insert_text`` does not wrap, and an overflowing line would put the rectangle under test
    outside the media box.
    """
    doc = pymupdf.open()
    for index in range(3):
        page = doc.new_page(width=LETTER_PT[0], height=LETTER_PT[1])
        page.insert_text((50, 60), f"Chapter {index}", fontsize=24)
        for paragraph in range(4):
            page.insert_text(
                (50, 140 + paragraph * 80),
                f"Sentence {paragraph} of chapter {index} reads distinctly.",
                fontsize=12,
            )
    return doc.tobytes()


@pytest.fixture(scope="module")
def pdf_bytes() -> bytes:
    return _make_pdf()


def _png_size(data: bytes) -> tuple[int, int]:
    """Width and height out of a PNG's IHDR, without decoding the image.

    Eight bytes of signature, then a four-byte length and the ``IHDR`` tag, then the two
    dimensions as big-endian uint32. Read rather than decoded so that the size assertions
    below do not depend on the same library that produced the bytes.
    """
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def _ingest(session, data: bytes, filename: str, content_type: str) -> Document:
    response = IngestService(session).upload_file(
        UploadedFile(data=data, filename=filename, content_type=content_type)
    )
    drain_ingest_queue(session, max_tasks=200)
    return DocumentRepository(session).get(response.document_id)


@pytest.fixture
def pdf_node(db_session, pdf_bytes) -> Document:
    return _ingest(db_session, pdf_bytes, "three-chapters.pdf", "application/pdf")


def _anchored_chunk(session, root_id: int) -> tuple[Document, dict]:
    """The first chunk under ``root_id`` that ``citation`` actually placed, and its anchor."""
    evidence = EvidenceRepository(session)
    for chunk in (
        session.execute(
            Document.__table__.select()
            .where(Document.path.contains([root_id]))
            .where(Document.usetype == USETYPE_CHUNK)
            .order_by(Document.id)
        )
        .mappings()
        .all()
    ):
        anchor = evidence.read(chunk["id"], ANCHOR_ROW)
        if anchor is not None:
            return session.get(Document, chunk["id"]), anchor
    raise AssertionError("citation placed no chunk; the fixture has nothing to address")


@contextmanager
def _as(principal):
    """Bind ``principal`` as the current request principal, as ``rest/auth.py`` does."""
    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


def _principal(session, name: str) -> CurrentPrincipal:
    row = PrincipalModel(name=name, is_owner=False)
    session.add(row)
    session.flush()
    return CurrentPrincipal(id=row.id, name=name, is_owner=False)


@pytest.fixture
def client_with_db(db_session):
    """TestClient bound to the savepoint-wrapped session (the house pattern)."""

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    client = TestClient(app, headers=AUTH_HEADERS)
    yield client
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# The bounds — no database, no tree
# ---------------------------------------------------------------------------


class TestParseBbox:
    def test_four_numbers_are_a_rectangle(self):
        assert parse_bbox("72,118.4,540,262.9") == (72.0, 118.4, 540.0, 262.9)

    def test_the_ends_are_put_in_order(self):
        """A reversed pair names the same rectangle traversed the other way."""
        assert parse_bbox("540,262.9,72,118.4") == (72.0, 118.4, 540.0, 262.9)

    def test_three_numbers_is_a_malformed_rectangle_and_not_a_partial_one(self):
        with pytest.raises(BadRenderRequest, match="3 comma-separated"):
            parse_bbox("1,2,3")

    def test_words_are_refused_rather_than_read_as_zero(self):
        with pytest.raises(BadRenderRequest, match="not four numbers"):
            parse_bbox("a,b,c,d")


class TestRenderBounds:
    def test_a_page_renders_at_the_density_asked_for(self, pdf_bytes):
        """72 dpi is one pixel per point, so the size is the media box."""
        assert _png_size(render_page(pdf_bytes, page=0, dpi=72)) == (612, 792)

    def test_doubling_the_density_doubles_the_pixels(self, pdf_bytes):
        assert _png_size(render_page(pdf_bytes, page=0, dpi=144)) == (1224, 1584)

    def test_a_page_outside_the_document_names_the_range(self, pdf_bytes):
        with pytest.raises(BadRenderRequest, match="holds 3 page"):
            render_page(pdf_bytes, page=3, dpi=72)

    def test_page_numbering_is_zero_based(self, pdf_bytes):
        """Page 2 is the third and last page; page 3 does not exist. If anything ever
        offsets for a human reader, exactly one of these two assertions breaks."""
        assert render_page(pdf_bytes, page=2, dpi=36)
        with pytest.raises(BadRenderRequest):
            render_page(pdf_bytes, page=3, dpi=36)

    def test_a_density_above_the_maximum_is_refused_and_not_clamped(self, pdf_bytes):
        with pytest.raises(RenderTooLarge, match=f"maximum of {DPI_MAX}"):
            render_page(pdf_bytes, page=0, dpi=DPI_MAX + 1)

    def test_a_render_larger_than_the_pixel_limit_is_refused(self, pdf_bytes):
        """The second bound, and the reason there are two: ``dpi`` alone bounds nothing,
        because the page's size is the document's to choose."""
        # The smallest dpi whose Letter page exceeds the limit, computed rather than
        # written down, so the test follows the constant instead of pinning a stale one.
        over = int((PIXELS_MAX / (LETTER_PT[0] * LETTER_PT[1])) ** 0.5 * 72) + 1
        assert over <= DPI_MAX, "the pixel bound must bite below the density bound"
        with pytest.raises(RenderTooLarge, match="Mpx"):
            render_page(pdf_bytes, page=0, dpi=over)

    def test_the_refusal_names_a_density_that_would_fit(self, pdf_bytes):
        """An error that suggests a value it would also reject is worse than one that
        suggests nothing, so the suggestion is tried."""
        over = int((PIXELS_MAX / (LETTER_PT[0] * LETTER_PT[1])) ** 0.5 * 72) + 1
        with pytest.raises(RenderTooLarge) as caught:
            render_page(pdf_bytes, page=0, dpi=over)
        suggested = int(str(caught.value).split("Retry at ")[1].split(" dpi")[0])
        assert _png_size(render_page(pdf_bytes, page=0, dpi=suggested))

    def test_a_crop_is_priced_on_the_crop_and_not_on_the_page(self, pdf_bytes):
        """A small rectangle of a big page renders at a density the whole page cannot.

        ``abs=2`` because a scaled rectangle lands between pixels and PyMuPDF snaps it to
        the ENCLOSING integer grid — floor on one edge, ceil on the other — so it can gain a
        pixel at each end depending on where the origin's fraction falls. That is the
        rasteriser's rule, and pinning the exact count would be pinning a library detail;
        what this asserts is that 600 dpi over 100x50 points is not refused and comes back
        the size those points are.
        """
        png = render_page(pdf_bytes, page=0, dpi=DPI_MAX, clip=(50, 50, 150, 100))
        assert _png_size(png) == pytest.approx((100 * DPI_MAX / 72, 50 * DPI_MAX / 72), abs=2)

    def test_a_rectangle_hanging_over_the_edge_is_intersected_with_the_page(self, pdf_bytes):
        """An ordinary anchor on a page whose media box a writer measured differently. The
        part that IS on the page is the answer."""
        assert _png_size(render_page(pdf_bytes, page=0, dpi=72, clip=(-100, -100, 200, 200))) == (
            200,
            200,
        )

    def test_a_rectangle_that_misses_the_page_is_refused(self, pdf_bytes):
        with pytest.raises(BadRenderRequest, match="entirely outside"):
            render_page(pdf_bytes, page=0, dpi=72, clip=(5000, 5000, 5100, 5100))

    def test_a_rectangle_with_no_area_is_refused(self, pdf_bytes):
        with pytest.raises(BadRenderRequest, match="no area"):
            render_page(pdf_bytes, page=0, dpi=72, clip=(10, 10, 10, 50))

    def test_a_density_of_zero_is_not_a_density(self, pdf_bytes):
        with pytest.raises(BadRenderRequest, match="positive integer"):
            render_page(pdf_bytes, page=0, dpi=0)

    def test_bytes_that_are_not_a_pdf_say_so(self):
        with pytest.raises(UnreadablePdf, match="cannot be opened"):
            render_page(b"this is not a PDF", page=0, dpi=72)

    def test_an_encrypted_pdf_is_refused_rather_than_rendered_blank(self):
        """An encrypted document OPENS and then answers every question with nothing, which
        would render as a blank page rather than as a refusal."""
        doc = pymupdf.open()
        doc.new_page()
        locked = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret")
        with pytest.raises(UnreadablePdf, match="encrypted"):
            render_page(locked, page=0, dpi=72)


# ---------------------------------------------------------------------------
# Step 20 — GET /documents/{id}/blob
# ---------------------------------------------------------------------------


class TestBlob:
    def test_the_bytes_come_back_exactly_as_they_were_uploaded(
        self, db_session, pdf_node, pdf_bytes
    ):
        payload = DocumentService(db_session).get_document_blob(pdf_node.id)
        assert isinstance(payload, BinaryPayload)
        assert payload.content == pdf_bytes
        assert len(payload) == len(pdf_bytes)

    def test_it_is_served_as_a_download_under_the_uploaders_own_filename(
        self, db_session, pdf_node
    ):
        """The reason this route exists is that somebody wants the file back."""
        payload = DocumentService(db_session).get_document_blob(pdf_node.id)
        assert payload.download is True
        assert payload.filename == "three-chapters.pdf"
        assert payload.media_type == "application/pdf"

    def test_the_route_sends_the_bytes_and_not_json(self, client_with_db, pdf_node, pdf_bytes):
        resp = client_with_db.get(f"/documents/{pdf_node.id}/blob")
        assert resp.status_code == 200
        assert resp.content == pdf_bytes
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["content-disposition"].startswith("attachment; ")
        assert "three-chapters.pdf" in resp.headers["content-disposition"]

    def test_the_openapi_document_declares_the_weaker_claim(self, client_with_db):
        """The route cannot promise more than octet-stream — what was uploaded is the row's
        property, not the route's — and the payload carries what actually went on the wire."""
        schema = client_with_db.get("/openapi.json").json()
        content = schema["paths"]["/documents/{document_id}/blob"]["get"]["responses"]["200"][
            "content"
        ]
        assert list(content) == ["application/octet-stream"]
        assert content["application/octet-stream"]["schema"]["format"] == "binary"

    def test_a_node_that_never_held_an_upload_is_a_named_refusal(self, db_session, pdf_node):
        """Not an empty 200. A zero-byte body is indistinguishable from an empty file that
        really was uploaded."""
        chunk, _ = _anchored_chunk(db_session, pdf_node.id)
        with pytest.raises(BlobUnavailable, match="holds no stored bytes"):
            DocumentService(db_session).get_document_blob(chunk.id)

    def test_the_refusal_names_the_usetype_it_found(self, db_session, pdf_node):
        chunk, _ = _anchored_chunk(db_session, pdf_node.id)
        with pytest.raises(BlobUnavailable, match=USETYPE_CHUNK):
            DocumentService(db_session).get_document_blob(chunk.id)

    def test_bytes_gone_from_under_the_row_is_a_named_refusal_too(self, db_session, pdf_node):
        """``find_blobless_documents`` exists because this state is real."""
        from jmfts_core.repositories.blob import BlobRepository

        assert BlobRepository(db_session).delete(pdf_node.id) is True
        with pytest.raises(BlobUnavailable):
            DocumentService(db_session).get_document_blob(pdf_node.id)

    def test_a_missing_document_is_a_404(self, db_session):
        with pytest.raises(LookupError):
            DocumentService(db_session).get_document_blob(10_000_000)

    def test_an_unreadable_document_is_indistinguishable_from_a_missing_one(
        self, db_session, pdf_node
    ):
        """This route serves document CONTENT, so it is gated on ``can_read``, and the
        existence-hiding convention is this codebase's: the same 404, with the same message,
        as a document that is not there."""
        outsider = _principal(db_session, "outsider")
        db_session.add(
            AccessGrant(
                document_id=pdf_node.id,
                principal_id=_principal(db_session, "insider").id,
                level="read",
            )
        )
        db_session.flush()
        with _as(outsider):
            with pytest.raises(LookupError) as governed:
                DocumentService(db_session).get_document_blob(pdf_node.id)
            with pytest.raises(LookupError) as absent:
                DocumentService(db_session).get_document_blob(10_000_000)
        assert str(governed.value).replace(str(pdf_node.id), "X") == str(absent.value).replace(
            "10000000", "X"
        )


# ---------------------------------------------------------------------------
# Step 21 — GET /documents/{id}/image
# ---------------------------------------------------------------------------


class TestImage:
    def test_a_file_node_with_no_page_named_opens_at_its_first(
        self, db_session, pdf_node, pdf_bytes
    ):
        """The file node IS the document, so page 0 is the only reading of "a picture of
        this document"."""
        payload = DocumentService(db_session).get_document_image(pdf_node.id)
        assert payload.media_type == "image/png"
        assert payload.content == render_page(pdf_bytes, page=0, dpi=rendering.DPI_DEFAULT)

    def test_a_rendered_page_is_not_offered_as_a_download(self, db_session, pdf_node):
        """It is derived from a document rather than being one, and naming it invites
        somebody to treat it as the source."""
        payload = DocumentService(db_session).get_document_image(pdf_node.id)
        assert payload.download is False
        assert payload.filename is None

    def test_the_page_number_is_zero_based_end_to_end(self, client_with_db, pdf_node):
        """Page 2 is the third page and page 3 does not exist. An endpoint that offset for a
        human reader would put every stored anchor one page out."""
        assert client_with_db.get(f"/documents/{pdf_node.id}/image?page=2").status_code == 200
        over = client_with_db.get(f"/documents/{pdf_node.id}/image?page=3")
        assert over.status_code == 400
        assert "3 page" in over.json()["detail"]

    def test_the_route_answers_with_a_png(self, client_with_db, pdf_node):
        resp = client_with_db.get(f"/documents/{pdf_node.id}/image?page=1&dpi=72")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.headers["content-disposition"] == "inline"
        assert _png_size(resp.content) == (612, 792)

    def test_a_density_above_the_maximum_is_a_413(self, client_with_db, pdf_node):
        resp = client_with_db.get(f"/documents/{pdf_node.id}/image?page=0&dpi={DPI_MAX + 1}")
        assert resp.status_code == 413
        assert str(DPI_MAX) in resp.json()["detail"]

    def test_a_chunk_is_rendered_at_the_page_its_anchor_names(
        self, db_session, pdf_node, pdf_bytes
    ):
        """Omitting ``page`` on a non-file node asks for the page it is ADDRESSED at."""
        chunk, anchor = _anchored_chunk(db_session, pdf_node.id)
        service = DocumentService(db_session)
        assert service.get_document_image(chunk.id).content == render_page(
            pdf_bytes, page=anchor["page"], dpi=rendering.DPI_DEFAULT
        )

    def test_a_chunk_reaches_the_bytes_of_the_file_node_above_it(self, db_session, pdf_node):
        """A chunk carries the anchor and a file node carries the bytes, so the verb has to
        walk from one to the other."""
        chunk, _ = _anchored_chunk(db_session, pdf_node.id)
        assert chunk.id != pdf_node.id
        assert _png_size(DocumentService(db_session).get_document_image(chunk.id).content)

    def test_a_node_with_no_anchor_is_refused_rather_than_shown_page_zero(
        self, db_session, pdf_node
    ):
        """A picture of the wrong page is the failure Part 5 calls worse than no picture."""
        chunk, _ = _anchored_chunk(db_session, pdf_node.id)
        EvidenceRepository(db_session).delete(chunk.id, ANCHOR_ROW)
        with pytest.raises(RegionNotAddressable, match="carries no 'source_anchor'"):
            DocumentService(db_session).get_document_image(chunk.id)

    def test_a_recorded_failure_to_place_the_passage_is_reported_by_reason(
        self, db_session, pdf_node
    ):
        """Two rows, never one with a null: "nothing has placed this" and "this could not be
        placed, because X" are different answers and the caller gets X."""
        chunk, _ = _anchored_chunk(db_session, pdf_node.id)
        evidence = EvidenceRepository(db_session)
        evidence.delete(chunk.id, ANCHOR_ROW)
        evidence.write(
            chunk.id,
            ANCHOR_UNRESOLVED_ROW,
            {"code": UNRESOLVED_NO_SPAN, "reason": UNRESOLVED_REASON[UNRESOLVED_NO_SPAN]},
        )
        with pytest.raises(RegionNotAddressable, match=UNRESOLVED_NO_SPAN):
            DocumentService(db_session).get_document_image(chunk.id)

    def test_something_that_is_not_a_pdf_is_refused_by_name(self, db_session):
        """Office renditions are Block G. A renderer that quietly extracted markdown instead
        would be the failure step 29 avoids, one route earlier."""
        node = _ingest(db_session, b"plain words, no pages\n", "notes.txt", "text/plain")
        with pytest.raises(NotAPdfSource, match="text/plain"):
            DocumentService(db_session).get_document_image(node.id, page=0)

    def test_an_unreadable_document_is_indistinguishable_from_a_missing_one(
        self, db_session, pdf_node
    ):
        outsider = _principal(db_session, "outsider")
        db_session.add(
            AccessGrant(
                document_id=pdf_node.id,
                principal_id=_principal(db_session, "insider").id,
                level="read",
            )
        )
        db_session.flush()
        with _as(outsider), pytest.raises(LookupError, match="not found"):
            DocumentService(db_session).get_document_image(pdf_node.id, page=0)


# ---------------------------------------------------------------------------
# Step 22 — GET /documents/{id}/region
# ---------------------------------------------------------------------------


class TestRegion:
    def test_an_explicit_rectangle_is_cropped_out_of_the_page(self, client_with_db, pdf_node):
        resp = client_with_db.get(
            f"/documents/{pdf_node.id}/region",
            params={"page": 0, "bbox": "50,50,250,150", "dpi": 72},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert _png_size(resp.content) == (200, 100)

    def test_the_nodes_own_anchor_crops_to_the_passage(self, db_session, pdf_node):
        """``anchor=true`` is the form a client has after a search hit: no coordinates to
        carry, because the node already knows where it is."""
        chunk, anchor = _anchored_chunk(db_session, pdf_node.id)
        payload = DocumentService(db_session).get_document_region(chunk.id, anchor=True, dpi=72)
        x0, y0, x1, y1 = anchor["bbox"]
        width, height = _png_size(payload.content)
        # 72 dpi is one pixel per point, so the image IS the rectangle — to within the pixel
        # the rasteriser snaps each edge to (see the crop test above).
        assert (width, height) == pytest.approx((x1 - x0, y1 - y0), abs=2)
        # Strictly inside the page, or the "rectangle" is the whole page and says nothing.
        assert width < LETTER_PT[0] and height < LETTER_PT[1]

    def test_the_cropped_pixels_are_the_passages_own_words(self, db_session, pdf_node, pdf_bytes):
        """A rectangle that is plausible and wrong is the specific failure Part 5 says is
        worse than no rectangle, and only re-reading the page can see it. The crop is checked
        against what PyMuPDF finds inside the same rectangle of the same page."""
        chunk, anchor = _anchored_chunk(db_session, pdf_node.id)
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        inside = doc.load_page(anchor["page"]).get_text("text", clip=pymupdf.Rect(*anchor["bbox"]))
        doc.close()
        assert inside.strip(), "the anchored rectangle holds no text at all"
        assert inside.split()[0] in (chunk.content or "")

    def test_naming_both_ways_is_refused_rather_than_given_a_precedence_rule(
        self, client_with_db, pdf_node
    ):
        """There is no way to tell which was meant."""
        resp = client_with_db.get(
            f"/documents/{pdf_node.id}/region",
            params={"anchor": "true", "page": 0, "bbox": "0,0,10,10"},
        )
        assert resp.status_code == 400
        assert "one or the other" in resp.json()["detail"]

    def test_naming_neither_way_says_what_both_are(self, client_with_db, pdf_node):
        resp = client_with_db.get(f"/documents/{pdf_node.id}/region")
        assert resp.status_code == 400
        assert "anchor=true" in resp.json()["detail"]

    def test_a_page_with_no_bbox_points_at_the_verb_that_serves_a_page(
        self, client_with_db, pdf_node
    ):
        resp = client_with_db.get(f"/documents/{pdf_node.id}/region", params={"page": 0})
        assert resp.status_code == 400
        assert "/image" in resp.json()["detail"]

    def test_a_malformed_rectangle_is_a_400(self, client_with_db, pdf_node):
        resp = client_with_db.get(
            f"/documents/{pdf_node.id}/region", params={"page": 0, "bbox": "1,2,3"}
        )
        assert resp.status_code == 400
        assert "four" in resp.json()["detail"]

    def test_a_rectangle_off_the_page_names_the_media_box(self, client_with_db, pdf_node):
        resp = client_with_db.get(
            f"/documents/{pdf_node.id}/region", params={"page": 0, "bbox": "5000,5000,5100,5100"}
        )
        assert resp.status_code == 400
        assert "792" in resp.json()["detail"]

    def test_a_spreadsheet_region_is_answered_with_cells_and_not_with_a_picture(
        self, db_session, client_with_db, pdf_node
    ):
        """``docs/SPRINT_0_6_0.md`` step 22: for a sheet, JSON cells are the better answer
        than a picture of cells, and ``GET /documents/{id}/cells`` already serves them. The
        409 carries that route, because an error that names the fix costs one call instead of
        a search."""
        chunk, _ = _anchored_chunk(db_session, pdf_node.id)
        evidence = EvidenceRepository(db_session)
        evidence.delete(chunk.id, ANCHOR_ROW)
        evidence.write(
            chunk.id, ANCHOR_ROW, {"kind": "cells", "sheet": "Q3 Pipeline", "ref": "B4:H120"}
        )
        resp = client_with_db.get(f"/documents/{chunk.id}/region", params={"anchor": "true"})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "B4:H120" in detail
        assert f"/documents/{chunk.id}/cells" in detail

    def test_a_character_span_has_no_geometry_and_says_so(self, db_session, pdf_node):
        """Same treatment, different absence: a range in text has no rectangle on any page
        until something paginates the document."""
        chunk, _ = _anchored_chunk(db_session, pdf_node.id)
        evidence = EvidenceRepository(db_session)
        evidence.delete(chunk.id, ANCHOR_ROW)
        evidence.write(chunk.id, ANCHOR_ROW, {"kind": "span", "char_start": 10, "char_end": 40})
        with pytest.raises(RegionNotAddressable, match="no rectangle on any page"):
            DocumentService(db_session).get_document_region(chunk.id, anchor=True)

    def test_an_anchor_kind_this_appliance_has_no_model_for_is_refused(self, db_session, pdf_node):
        """``parse_anchor`` raises rather than returning None, and this keeps that: an anchor
        nobody can interpret is not a highlight that is merely missing."""
        chunk, _ = _anchored_chunk(db_session, pdf_node.id)
        evidence = EvidenceRepository(db_session)
        evidence.delete(chunk.id, ANCHOR_ROW)
        evidence.write(chunk.id, ANCHOR_ROW, {"kind": "ooxml", "part": "word/document.xml"})
        with pytest.raises(RegionNotAddressable, match="ooxml"):
            DocumentService(db_session).get_document_region(chunk.id, anchor=True)

    def test_an_unreadable_document_is_indistinguishable_from_a_missing_one(
        self, db_session, pdf_node
    ):
        outsider = _principal(db_session, "outsider")
        db_session.add(
            AccessGrant(
                document_id=pdf_node.id,
                principal_id=_principal(db_session, "insider").id,
                level="read",
            )
        )
        db_session.flush()
        with _as(outsider), pytest.raises(LookupError, match="not found"):
            DocumentService(db_session).get_document_region(pdf_node.id, page=0, bbox="10,10,20,20")
