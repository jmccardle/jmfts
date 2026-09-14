# jmfts-web

The browser front end for a [JMFTS](https://github.com/jmccardle/jmfts) retrieval appliance.

```bash
pip install 'jmfts[web]'
uvicorn jmfts_core.rest.main:app --host 0.0.0.0 --port 8100
# then open http://localhost:8100/app/
```

This wheel carries built static files and one module that says where they are. It has **no
dependencies**: everything the front end does it does in the browser, over the same REST API
any other client calls. Installing it adds no Python package to the appliance.

It is not useful on its own — there is nothing to import and nothing to run. `jmfts[web]`
depends on it, and `jmfts_core.rest.main` mounts it at `/app` when it is present. An
appliance without it starts normally and serves no `/app`.

## Why a separate distribution

An extra guards dependencies. Static files inside the server package would ship with the
appliance whether or not an extra named them, so `jmfts[web]` over a bundle in the main wheel
would have been a flag that guards nothing. `Dockerfile.worker` builds a worker that drains
the ingest queue and never serves a page.

## Versioning

One number, three wheels, one tag — `jmfts`, `jmfts-client` and `jmfts-web` release in
lockstep. The front end is written against the appliance's own route table, so a UI one
release behind its server renders controls for operations that have moved.

## Licence

MIT. See `LICENSE`.
