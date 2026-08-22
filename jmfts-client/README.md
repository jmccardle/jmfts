# jmfts-client

The thin half of [JMFTS](https://github.com/jmccardle/jmfts): the wire contracts, and an
HTTP client generated from them.

Install this to **call** a JMFTS appliance. Install `jmfts` to **run** one.

```bash
pip install jmfts-client
```

Two dependencies — `httpx` and `pydantic`. A consumer that only makes requests does not
install sqlalchemy, psycopg2, pgvector, transformers or pymupdf to do it.

## Use

```python
from jmfts_client import RemoteJmftsClient
from jmfts_client.contracts import DocumentCreate, HybridSearchRequest

with RemoteJmftsClient("http://localhost:8100", token="...") as jmfts:
    doc = jmfts.create_document(DocumentCreate(title="Ada", content="Ada Lovelace"))
    hits = jmfts.hybrid_search(HybridSearchRequest(query="Ada", limit=10))
    for hit in hits.results:
        print(hit.document_id, hit.score)
```

Errors arrive as exceptions carrying the status: `JmftsNotFound`, `JmftsBadRequest`,
`JmftsConflict`, `JmftsUnprocessable`, `JmftsServerError`. All descend from `JmftsError`.
A request that never reached the server raises `JmftsTransportError`.

## Where the methods come from

Nobody writes them. A JMFTS service method marked `@expose` becomes a REST route, an entry
in the OpenAPI document, a method on the in-process `LocalJmftsClient`, and a method here —
four views of one definition.

`jmfts_client/_verbs.py` is generated from the route table the server actually builds, not
from the decorator's declaration, because the decorator does not record which parameters
are body, query or path — FastAPI's inference decides that, and reading the built routes is
what keeps this client and the server agreeing about the wire. Regeneration lives in the
`jmfts` repository (`python -m scripts.generate_client`), and a test there fails if this
file falls behind the surface.

## What this client does not offer

`LocalJmftsClient.unit_of_work()` groups several writes into one transaction. There is no
equivalent here, deliberately. Each HTTP request is its own transaction on the server, so a
`unit_of_work` over HTTP could group the calls but could not roll back the earlier ones when
a later one failed. If you need that guarantee, run in-process with `jmfts` installed.

## Licence

MIT. Copyright (c) 2026 Fight Fire with Fire Robotics, LLC.
