import { fileURLToPath, URL } from "node:url";
import { defineConfig, loadEnv, searchForWorkspaceRoot } from "vite";
import vue from "@vitejs/plugin-vue";

// The appliance this console talks to.
//
// In development the browser talks to Vite and Vite talks to the appliance, so the page is
// same-origin and no CORS header has to exist on the server side. In a build the base URL is
// read at RUNTIME from `window.__JMFTS_BASE__` (see `index.html`), not baked in — one build
// should be servable next to any appliance, which is the whole point of this being a separate
// project rather than a bundle inside the wheel.
// The generated client. ONE copy exists, the one `scripts/generate_ts_client.py` writes and
// `tests/test_ts_client_codegen.py` holds against the live surface; this console reads it in
// place through the `@jmfts/client` alias rather than carrying a second copy that nothing
// checks. `tsconfig.json` maps the same name, so `vue-tsc` reads the generated `.d.ts` files.
// `tests/test_ts_client_codegen.py` fails if a copy reappears under `jmfts-ui/`.
const generatedClient = fileURLToPath(
  new URL("../jmfts-web/jmfts_web/static/client", import.meta.url)
);

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  // The compose stack's published API port (`../docker-compose.yml`), which is what the README
  // and `CLAUDE.md` tell a developer to start. Another instance is what `JMFTS_URL` is for.
  // `tests/test_ui_dev_proxy.py` fails if this and the compose file disagree.
  const appliance = env.JMFTS_URL || "http://127.0.0.1:8100";

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        "@": fileURLToPath(new URL("./src", import.meta.url)),
        "@jmfts/client": generatedClient,
      },
    },
    server: {
      port: 5183,
      // The client lives outside this project's root, and Vite's dev server refuses to serve
      // a file outside its allow list.
      fs: { allow: [searchForWorkspaceRoot(process.cwd()), generatedClient] },
      proxy: Object.fromEntries(
        // Every prefix the appliance answers on. Listed rather than globbed, because a
        // catch-all would swallow Vite's own `/@vite` and `/src` paths. Listed by hand, so
        // `tests/test_ui_dev_proxy.py` holds it against the first segment of every path in the
        // live OpenAPI document plus the three doc routes; a prefix missing here is a page that
        // works against a built bundle and 404s from Vite in development.
        [
          "/access",
          "/capabilities",
          "/config",
          "/conversations",
          "/docs",
          "/documents",
          "/graph",
          "/health",
          "/indexes",
          "/ingest",
          "/ontologies",
          "/openapi.json",
          "/rdf",
          "/redoc",
          "/runner",
          "/search",
          "/search-contexts",
          "/shape-bindings",
          "/templates",
          "/triples",
          "/usetype-presentations",
          "/view",
        ].map((prefix) => [prefix, { target: appliance, changeOrigin: true }])
      ),
    },
  };
});
