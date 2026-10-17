# JMFTS plugin

Skills that let Claude treat a JMFTS instance as durable knowledgebase
memory: search, ingest, read, explore, analyze.

## Layout

```
plugin/jmfts/
├── plugin.json
├── README.md          (this file)
└── skills/
    ├── jmfts/         orientation — explains the substrate, routes
    ├── jmfts-search/  retrieval (5 methods + auto, LLM synthesis)
    ├── jmfts-ingest/  7 ingest usetypes, upload, options, dedup rule
    ├── jmfts-read/    /view/{id} composite reader
    ├── jmfts-explore/ tree + link + triple navigation
    └── jmfts-analyze/ centrality, communities, lint
```

The orientation skill is auto-discovered whenever the user references the
knowledgebase, prior memory, or accumulated knowledge. The five
capability skills are auto-discovered when their specific intents fire
("look up", "save this", "read with context", "what cites this", "lint").

## Install

Symlink or copy into the user's Claude plugin directory. The skill files
are self-contained — they do not import from the JMFTS package.

The skills assume:

- `JMFTS_API_BASE_URL` env var (default `http://localhost:8100`) points
  to a running JMFTS instance.
- `JMFTS_API_TOKEN` holds that instance's API token. The scripts send it as
  `Authorization: Bearer <token>`, and the `curl` examples in the skills pass
  it explicitly. Every route except `/health` and the API docs needs it; with
  it unset, the scripts exit with a 401 that names the variable.
- The agent can `cd` to the JMFTS install directory and run
  `python -m scripts.<name>`. If the JMFTS install lives somewhere
  non-default, set `JMFTS_HOME` (advisory; not currently consumed by
  the scripts themselves but referenced in the skills).

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
each agent run sets `JMFTS_API_BASE_URL` (and that instance's
`JMFTS_API_TOKEN`) for the instance it should hit.

## Underlying system

The plugin describes the agent surface. The underlying server lives at
`https://github.com/jmccardle/jmfts` (or wherever installed). The design
document that motivated this skill set, `docs/AGENTIC_KNOWLEDGEBASE.md`
(architecture and the server-vs-client decision rule), is **held back** and
not in the public repository. The repository's `README.md` and
`docs/reference/` describe the server.
