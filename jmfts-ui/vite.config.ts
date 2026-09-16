import { fileURLToPath, URL } from "node:url";
import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";

// The appliance this console talks to.
//
// In development the browser talks to Vite and Vite talks to the appliance, so the page is
// same-origin and no CORS header has to exist on the server side. In a build the base URL is
// read at RUNTIME from `window.__JMFTS_BASE__` (see `index.html`), not baked in — one build
// should be servable next to any appliance, which is the whole point of this being a separate
// project rather than a bundle inside the wheel.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const appliance = env.JMFTS_URL || "http://127.0.0.1:8110";

  return {
    plugins: [vue()],
    resolve: {
      alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
    },
    server: {
      port: 5183,
      proxy: Object.fromEntries(
        // Every prefix the appliance answers on. Listed rather than globbed, because a
        // catch-all would swallow Vite's own `/@vite` and `/src` paths.
        [
          "/access",
          "/capabilities",
          "/conversations",
          "/documents",
          "/graph",
          "/health",
          "/indexes",
          "/ingest",
          "/ontologies",
          "/openapi.json",
          "/rdf",
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
