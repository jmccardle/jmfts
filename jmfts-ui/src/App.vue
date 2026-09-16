<script setup lang="ts">
// The frame: which appliance, which credential, and which way in.
//
// The rail lists ways of working rather than endpoints — browsing the corpus, searching it,
// putting something into it, and watching what this console asked for. An entry point that is
// not built yet says so and is not clickable; a navigation item that opens an empty page is a
// control that lies.

import { computed, onMounted, ref } from "vue";
import { baseUrl, calls, client, connect, forget, pointAt, reachability, token } from "@/api";

const entry = ref("");
const applianceField = ref(baseUrl.value);
const problem = ref<string | null>(null);

const ways = [
  { to: "/", label: "Corpus", hint: "What this appliance holds, and the trees it holds it in." },
  { to: null, label: "Search", hint: "Not built yet. The seven methods, side by side." },
  { to: null, label: "Ingest", hint: "Not built yet. Put a document in and watch it settle." },
  { to: "/calls", label: "Calls", hint: "Every request this page has made." },
];

async function probe() {
  reachability.state = "reaching";
  try {
    const health = await client.health_check_liveness();
    reachability.state = "ok";
    reachability.detail = `${health.status} · ${health.version}`;
  } catch (failure) {
    reachability.state = "unreachable";
    reachability.detail = (failure as Error).message;
  }
}

function submit() {
  problem.value = null;
  try {
    if (applianceField.value.trim() !== baseUrl.value) pointAt(applianceField.value.trim());
    connect(entry.value);
    entry.value = "";
    void probe();
  } catch (failure) {
    problem.value = (failure as Error).message;
  }
}

onMounted(probe);

const connected = computed(() => Boolean(token.value));
const where = computed(() => baseUrl.value || "this origin");
</script>

<template>
  <div class="frame">
    <header class="bar">
      <RouterLink to="/" class="brand">JMFTS</RouterLink>

      <form class="connect" @submit.prevent="submit">
        <label class="sr-only" for="appliance">Appliance URL</label>
        <input
          id="appliance"
          v-model="applianceField"
          class="value url"
          type="text"
          placeholder="same origin"
          spellcheck="false"
        />
        <label class="sr-only" for="token">API token</label>
        <input
          id="token"
          v-model="entry"
          class="value"
          type="password"
          placeholder="API token"
          autocomplete="off"
        />
        <button type="submit">Connect</button>
        <button v-if="connected" type="button" class="ghost" @click="forget">Forget token</button>
      </form>

      <p class="state" :class="reachability.state">
        <span v-if="problem" class="bad">{{ problem }}</span>
        <template v-else-if="reachability.state === 'ok'">
          {{ where }} answered {{ reachability.detail
          }}<template v-if="!connected">, and needs a token for everything else</template>
        </template>
        <template v-else-if="reachability.state === 'unreachable'">
          <span class="bad">{{ where }} did not answer: {{ reachability.detail }}</span>
        </template>
        <template v-else>Reaching {{ where }}…</template>
      </p>
    </header>

    <div class="body">
      <nav class="rail">
        <ul>
          <li v-for="way in ways" :key="way.label">
            <RouterLink v-if="way.to" :to="way.to" class="way">
              <span class="label">{{ way.label }}</span>
              <span class="hint">{{ way.hint }}</span>
            </RouterLink>
            <span v-else class="way unbuilt">
              <span class="label">{{ way.label }}</span>
              <span class="hint">{{ way.hint }}</span>
            </span>
          </li>
        </ul>
        <p class="tally value">{{ calls.length }} calls this page</p>
      </nav>

      <main><RouterView /></main>
    </div>
  </div>
</template>

<style scoped>
.frame {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.bar {
  display: flex;
  align-items: center;
  gap: var(--s5);
  flex-wrap: wrap;
  padding: var(--s3) var(--s5);
  border-bottom: 1px solid var(--rule);
  background: var(--leaf);
}

.brand {
  font-size: var(--t-mid);
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--ink);
  text-decoration: none;
}

.connect {
  display: flex;
  gap: var(--s2);
  align-items: center;
}

input {
  padding: 5px var(--s2);
  border: 1px solid var(--rule);
  border-radius: var(--r);
  background: var(--paper);
  color: var(--ink);
  font-size: var(--t-small);
  width: 13rem;
}

input.url {
  width: 15rem;
}

input:focus {
  border-color: var(--accent);
}

button {
  padding: 5px var(--s3);
  border: 1px solid var(--accent);
  border-radius: var(--r);
  background: var(--accent);
  color: var(--leaf);
  font-size: var(--t-small);
  cursor: pointer;
}

button.ghost {
  background: transparent;
  border-color: var(--rule);
  color: var(--ink-soft);
}

button:hover {
  filter: brightness(1.12);
}

.state {
  margin: 0;
  max-width: none;
  font-size: var(--t-small);
  color: var(--ink-soft);
}

.bad {
  color: var(--alarm);
}

.body {
  display: grid;
  grid-template-columns: var(--rail) minmax(0, 1fr);
  flex: 1;
  min-height: 0;
}

.rail {
  display: flex;
  flex-direction: column;
  padding: var(--s5) var(--s4);
  border-right: 1px solid var(--rule);
  background: var(--leaf);
}

.rail ul {
  list-style: none;
  margin: 0;
  padding: 0;
  flex: 1;
}

.way {
  display: block;
  padding: var(--s2) var(--s3);
  margin-bottom: var(--s1);
  border-left: 2px solid transparent;
  color: inherit;
  text-decoration: none;
}

a.way:hover {
  background: var(--paper);
}

a.way.router-link-active {
  border-left-color: var(--accent);
  background: var(--paper);
}

.label {
  display: block;
  line-height: 1.3;
}

.hint {
  display: block;
  margin-top: 1px;
  font-size: var(--t-micro);
  line-height: 1.4;
  color: var(--ink-faint);
}

.unbuilt {
  cursor: not-allowed;
}

.unbuilt .label {
  color: var(--ink-faint);
}

.tally {
  margin: 0;
  padding-top: var(--s3);
  border-top: 1px solid var(--rule-soft);
  font-size: var(--t-micro);
  color: var(--ink-faint);
}

main {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

main > :deep(*) {
  flex: 1;
  min-height: 0;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

@media (max-width: 880px) {
  .body {
    grid-template-columns: minmax(0, 1fr);
  }
  .rail {
    border-right: 0;
    border-bottom: 1px solid var(--rule);
  }
  .rail ul {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
  }
  .hint {
    display: none;
  }
}
</style>
