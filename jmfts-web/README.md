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

## One generated client, and one event per call

`jmfts_web/static/client/` is the only way this front end talks to an appliance. Four of its
files are generated from the appliance's own `/openapi.json`, the way `jmfts-client`'s
`_verbs.py` is generated from the route table:

```bash
python -m scripts.generate_ts_client            # in a jmfts source checkout
python -m scripts.generate_ts_client --check    # exit 1 if the bundle is stale
```

The other six are the hand-written runtime — the request path, the three copy-out printers,
and the entry point. `tests/test_ts_client_codegen.py` refuses a stale generated file and
refuses a file that is in neither set.

Every call emits one event: `{op_id, method, path, path_params, query, body, status,
response, ms}`. Three things follow from that.

* **Replay.** An event is a call plus its arguments, so `client.replay(event, {…})` re-sends
  it with any of them edited.
* **Verifiable thinness.** A view showing something the call log has no event for computed it
  in the browser. That is readable from the log rather than arguable from the source.
* **Copy-out.** `asCurl`, `asPython` and `asFetch` print any event three ways — and none of
  them prints the credential. Each names the environment variable it lives in instead.

There is no build step and no bundler: these are ES modules the browser runs as they stand,
with `.d.ts` declarations beside them for TypeScript consumers. Nothing loads from a CDN, a
font host or anywhere but the appliance's own origin, because this appliance is expected to
run air-gapped.

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
