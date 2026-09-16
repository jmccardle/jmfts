# jmfts-ui

A standalone browser console for a JMFTS appliance. It builds on its own, it is not packaged
with `jmfts`, and it points at an API base URL you choose at runtime.

```bash
npm install
JMFTS_URL=http://127.0.0.1:8110 npm run dev     # http://localhost:5183
npm run build                                   # dist/, static files
npm run typecheck
```

`JMFTS_URL` only affects the dev server, which proxies the appliance's route prefixes so the
page stays same-origin and the appliance needs no CORS header to develop against. A built
bundle reads `window.__JMFTS_BASE__` from the host page, and whatever address you type into the
bar wins over it for that tab.

The token lives in `sessionStorage`, never in `localStorage` and never in the URL. A closed tab
has forgotten it; a pasted address does not carry it.

## What is here

| Path | What |
|---|---|
| `src/api/client/` | **Generated.** A copy of what `scripts/generate_ts_client.py` renders from the appliance's `/openapi.json` — a typed method and a TypeScript interface for all 115 operations. Replace the directory to update; do not edit it. |
| `src/api/index.ts` | The one client instance, the base URL, the token, and the call stream. |
| `src/design/` | The tokens and the element defaults. `tokens.css` states the design language. |
| `src/components/` | The shared vocabulary: `Tag`, `Fact`, `HandMark`, `NodeLine`, `Panel`, `ScopeTrail`, `Apparatus`, and the renderers. |
| `src/inspector/` | The node inspector. |
| `src/views/` | The corpus census and the call log. |
| `src/lib/` | `vocab.ts` glosses the appliance's own words; `markdown.ts` parses to tokens, never to HTML. |

## The design language, in one paragraph

A JMFTS node is rarely legible on its own — a search hit is a chunk five levels down whose title
belongs to its grandparent, whose text was cut by a named rule, and whose meaning needs the page
it came from. That is what a scholarly edition is for: a text in the main column and an
apparatus beside it recording where each reading came from. So content is set in a text face and
derivation in the same family a size down, the apparatus is permanently on the page rather than
behind a disclosure triangle, and the one thing that would otherwise shout — whether a rule or a
person wrote this node — is an editorial bracket in the margin instead of a colour. Serif is
prose; mono is a value you could copy. Hue is rationed to two jobs: one accent for what you can
act on, one alarm for what failed.

## Screenshots, for looking at the design

```bash
node scripts/shot.mjs <token> <out-dir> "/" "/node/7" "/node/7@1440@980@dark"
```

Needs a Chrome in `/usr/bin` and the dev server up. Route, then optional `@width@height@theme`.
