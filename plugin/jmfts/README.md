# JMFTS plugin

Skills that let Claude treat a JMFTS instance as durable knowledgebase
memory: search, ingest, explore.

## Layout

```
plugin/jmfts/
├── plugin.json
├── README.md          (this file)
└── skills/
    ├── jmfts/         orientation — explains the substrate, routes
    ├── jmfts-search/  retrieval (5 methods + LLM synthesis)
    ├── jmfts-ingest/  7 ingest pipelines + idempotency model
    └── jmfts-explore/ tree + link + triple navigation
```

The orientation skill is auto-discovered whenever the user references the
knowledgebase, prior memory, or accumulated knowledge. The three
capability skills are auto-discovered when their specific intents fire
("look up", "save this", "what cites this").

## Install

Symlink or copy into the user's Claude plugin directory. The skill files
are self-contained — they do not import from the JMFTS package.

The skills assume:

- `JMFTS_API_BASE_URL` env var (default `http://localhost:8100`) points
  to a running JMFTS instance.
- The agent can `cd` to the JMFTS checkout and run
  `python -m scripts.<name>`. The plugin does not assume a location for
  that checkout.

## Working scope (convention)

JMFTS does not enforce a per-agent root. Agents may:

- Operate at corpus scope (no `--parent-id`).
- Use a designated root they were told about (`--parent-id N`).
- Create a fresh root on first use and remember its ID.

Whichever style suits the agent — the skills describe the convention but
don't pick one for the user.

## Multi-instance

A given Claude installation can connect to different JMFTS instances on
different machines (or different ports). The plugin doesn't pin a URL;
each agent run sets `JMFTS_API_BASE_URL` for the instance it should hit.

## Underlying system

The plugin describes the agent surface only. The server it talks to is
the JMFTS appliance — see that repository's `README.md` and `CLAUDE.md`
for architecture.
