"""Drive the GENERATED client against the real app, over a real database.

``test_client_codegen.py`` proves the client matches the surface in shape. This proves it
matches on the wire: that a generated method puts each argument where FastAPI expects to
read it, and parses what comes back into the declared model.

The transport is a ``TestClient``, which is an ``httpx.Client`` whose transport calls the
ASGI app in-process. ``_VerbTransport`` accepts an externally-supplied client for exactly
this reason, so nothing here is a stub: the request is routed, validated, executed against
PostgreSQL, serialised and parsed by the same code a network call would use.

A shape mismatch that the codegen tests cannot see — a body sent as a query param, a bool
rendered ``True`` where the parser wants ``true``, a path value not escaped — fails here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jmfts_client import JmftsNotFound, RemoteJmftsClient
from jmfts_client.contracts import DocumentCreate
from jmfts_client.contracts.upload import UploadedFile
from jmfts_core.database import get_db
from jmfts_core.rest.main import app


@pytest.fixture
def jmfts(db_session):
    """A generated client whose transport is the app itself, on the test session."""

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    from tests.conftest import AUTH_HEADERS

    transport = TestClient(app, headers=AUTH_HEADERS)
    client = RemoteJmftsClient("http://testserver", client=transport)
    yield client
    app.dependency_overrides.pop(get_db, None)


def test_a_json_body_verb_round_trips(jmfts):
    """``create_document`` sends a model as the body and parses a model back."""
    created = jmfts.create_document(
        DocumentCreate(title="Ada Lovelace", content="Wrote the first algorithm."),
    )
    assert created.id > 0
    assert created.title == "Ada Lovelace"
    # The return value is the declared contract class, not a dict.
    assert type(created).__name__ == "DocumentResponse"


def test_a_path_param_verb_round_trips(jmfts):
    """``get_document`` puts its argument in the path, not the query string."""
    created = jmfts.create_document(DocumentCreate(title="Grace", content="Compilers."))
    fetched = jmfts.get_document(created.id)
    assert fetched.id == created.id
    assert fetched.title == "Grace"


def test_a_missing_object_raises_the_typed_error(jmfts):
    """A 404 arrives as ``JmftsNotFound`` carrying the server's detail."""
    with pytest.raises(JmftsNotFound) as caught:
        jmfts.get_document(999_999_999)
    assert caught.value.status_code == 404
    assert caught.value.detail


def test_query_params_reach_the_server_as_the_parser_expects(jmfts):
    """Bools and ints in the query string, not the body.

    ``list_documents`` takes only query params. Python renders ``True`` with a capital T,
    which FastAPI's bool parser rejects, so ``_query_value`` lowercases it. If that ever
    regressed this call would 422 rather than return rows.
    """
    jmfts.create_document(DocumentCreate(title="Katherine", content="Orbital mechanics."))
    rows = jmfts.list_documents(limit=5, offset=0)
    assert isinstance(rows, list)
    assert all(type(row).__name__ == "DocumentResponse" for row in rows)


def test_a_list_response_is_validated_into_contract_objects(jmfts):
    """``list[DocumentResponse]`` is parsed element-wise, not returned as dicts."""
    jmfts.create_document(DocumentCreate(title="Radia", content="Spanning tree protocol."))
    rows = jmfts.list_documents(limit=50)
    assert rows, "expected at least the document just created"
    assert hasattr(rows[0], "title")


def test_the_multipart_verb_sends_a_file_and_its_options(jmfts):
    """``upload_file`` is the one route with a file part beside a JSON form field.

    The file travels as a multipart part; ``options`` travels beside it as JSON TEXT,
    because the server declares it ``Json[dict]``. Sending it as a normal JSON body — the
    obvious thing — would 422, and only a real request shows that.
    """
    uploaded = jmfts.upload_file(
        UploadedFile(
            data=b"# Title\n\nSome prose for the ingest queue.\n",
            filename="note.md",
            content_type="text/markdown",
        ),
        options={"structure": {"chunk_strategy": "paragraph_packed", "max_tokens": 100}},
    )
    assert uploaded.document_id > 0
    assert uploaded.filename == "note.md"
    assert uploaded.content_hash.startswith("sha256:")


def test_multipart_options_reach_the_server_as_options_not_as_a_body(jmfts):
    """An unknown option group comes back as the server's own 400, naming the groups.

    This is the proof that ``options`` arrived where the server reads options. Sent as a
    JSON body instead, FastAPI would reject the request as malformed multipart before
    ``resolve_options`` ever saw the key, and the message would name neither the group nor
    the alternatives.
    """
    from jmfts_client import JmftsBadRequest

    with pytest.raises(JmftsBadRequest) as caught:
        jmfts.upload_file(
            UploadedFile(data=b"prose\n", filename="x.md", content_type="text/markdown"),
            options={"nonesuch": {"k": 1}},
        )
    assert "unknown option group 'nonesuch'" in str(caught.value.detail)


def test_an_untyped_verb_returns_the_parsed_json(jmfts):
    """An operation whose route declares no response model returns JSON, not a model.

    This is the carve-out named in ``test_client_codegen.UNTYPED_OPERATIONS``. The client
    does not invent a shape the server never promised.
    """
    created = jmfts.create_document(DocumentCreate(title="Doomed", content="Delete me."))
    result = jmfts.delete_document(created.id)
    assert isinstance(result, (dict, list, type(None)))
    with pytest.raises(JmftsNotFound):
        jmfts.get_document(created.id)
