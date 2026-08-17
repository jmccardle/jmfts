"""`DocumentResponse.from_document` must not silently drop columns.

It enumerates fields by hand, and DocumentResponse gives most of them a default, so a
newly-added column serialises as `null` with no error anywhere — the value is in the
database, the API just refuses to say so. That is exactly how `event_time` failed its
first end-to-end check. These tests fail the next time it happens.

`from_document` is the single ORM→response converter (the documents router's local
`doc_to_response` was folded into it during the @expose conversion); this guards it.
"""

from datetime import datetime, timezone

from api.schemas import DocumentResponse
from jmfts_core.models.document import Document


def doc_to_response(doc):
    """Adapter kept so these guards read against the single-source converter."""
    return DocumentResponse.from_document(doc)


def _doc():
    doc = Document(
        id=7,
        parent_id=None,
        title="t",
        content="c",
        structured_content={"importance": 5},
        path=[],
        usetype="note",
        position=None,
        content_hash="abc",
    )
    doc.created_at = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    doc.updated_at = doc.created_at
    doc.event_time = datetime(2023, 1, 15, 8, 30, tzinfo=timezone.utc)
    # Set for the same reason created_at is: `settled`'s defaults fire at INSERT, and
    # this Document is never inserted. A non-default value so the "declared but never
    # populated" guard below actually bites — 'settled' would be indistinguishable from
    # a field that was silently dropped and fell back to its response default.
    doc.settled = "in_flight"
    return doc


def test_event_time_survives_serialisation():
    assert doc_to_response(_doc()).event_time == datetime(2023, 1, 15, 8, 30, tzinfo=timezone.utc)


def test_response_carries_every_field_to_dict_exposes():
    """Structural guard: to_dict is the model's own view of itself, so anything it
    exposes should be reachable through the API surface too."""
    missing = set(_doc().to_dict()) - set(DocumentResponse.model_fields)
    assert not missing, f"DocumentResponse is missing field(s) exposed by to_dict: {missing}"


def test_response_populates_every_field_it_declares():
    """The tighter guard: declaring the field is not enough — doc_to_response must
    actually pass it, or it silently serialises as the default."""
    response = doc_to_response(_doc())
    doc_view = _doc().to_dict()
    unpopulated = {
        name
        for name in DocumentResponse.model_fields
        if name in doc_view and doc_view[name] is not None and getattr(response, name) is None
    }
    assert not unpopulated, f"doc_to_response declared but never populated: {unpopulated}"
