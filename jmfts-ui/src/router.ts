import { createRouter, createWebHashHistory, type RouteRecordRaw } from "vue-router";
import Corpus from "@/views/Corpus.vue";
import Calls from "@/views/Calls.vue";
import NodeInspector from "@/inspector/NodeInspector.vue";

// Hash routing, so the built bundle can be served from any static host — including one that
// answers 404 for a path it has no file for, which is what `StaticFiles` does and what most
// object stores do. A console that only works behind a catch-all route is a console with a
// deployment prerequisite nobody wrote down.
const routes: RouteRecordRaw[] = [
  { path: "/", name: "corpus", component: Corpus },
  {
    path: "/node/:id(\\d+)",
    name: "node",
    component: NodeInspector,
    props: (route) => ({ id: Number(route.params.id) }),
  },
  { path: "/search", name: "search", component: () => import("@/views/Search.vue") },
  { path: "/calls", name: "calls", component: Calls },
  { path: "/ingest", name: "ingest", component: () => import("@/views/Ingest.vue") },
  { path: "/:rest(.*)", redirect: "/" },
];

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
});
