<script setup lang="ts">
// Every request this page has made, newest first.
//
// The console adds nothing to this list and hides nothing from it, which is the property worth
// having: a figure on a page with no call behind it was computed in the browser, and you can
// see that from here. The stream is the generated client's own — one event per call, nine
// fields — not something a page keeps alongside.

import { calls } from "@/api";
import { asCurl, asPython } from "@/api/client/copyout.js";
import Panel from "@/components/Panel.vue";
import Tag from "@/components/Tag.vue";
import { ref } from "vue";

const shown = ref<number | null>(null);
const form = ref<"curl" | "python">("curl");

function printed(index: number) {
  const event = calls.value[index];
  const options = { baseUrl: window.location.origin };
  return form.value === "curl" ? asCurl(event, options) : asPython(event, options);
}

const tone = (status: number) => (status === 0 || status >= 400 ? "alarm" : "plain");
</script>

<template>
  <div class="calls scroll-pane">
    <Panel title="Calls this page made" :note="`${calls.length} kept`">
      <p v-if="calls.length === 0" class="empty">
        Nothing yet. Open the corpus or a node and the requests will appear here.
      </p>

      <ol v-else class="list">
        <li v-for="(event, i) in calls" :key="i">
          <div class="head">
            <span class="method value">{{ event.method }}</span>
            <span class="path value">{{ event.path }}</span>
            <Tag :value="String(event.status)" :tone="tone(event.status)" />
            <span class="ms value">{{ event.ms }} ms</span>
            <button type="button" @click="shown = shown === i ? null : i">
              {{ shown === i ? "Hide" : "Copy out" }}
            </button>
          </div>
          <div v-if="shown === i" class="out">
            <div class="forms">
              <button
                v-for="f in (['curl', 'python'] as const)"
                :key="f"
                type="button"
                class="ghost"
                :class="{ on: form === f }"
                @click="form = f"
              >
                {{ f }}
              </button>
            </div>
            <pre class="value">{{ printed(i) }}</pre>
            <p class="apparatus-voice">
              The token is never in this text. Each form names the environment variable to read
              it from instead.
            </p>
          </div>
        </li>
      </ol>
    </Panel>
  </div>
</template>

<style scoped>
.calls {
  padding: var(--s6);
  max-width: 62rem;
}

.list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.list li {
  padding: var(--s2) 0;
  border-bottom: 1px solid var(--rule-soft);
}

.head {
  display: flex;
  align-items: baseline;
  gap: var(--s3);
  flex-wrap: wrap;
}

.method {
  color: var(--ink-soft);
  font-size: var(--t-micro);
  min-width: 3.2rem;
}

.path {
  flex: 1;
  min-width: 12rem;
  overflow-wrap: anywhere;
  font-size: var(--t-small);
}

.ms {
  color: var(--ink-faint);
  font-size: var(--t-micro);
}

button {
  padding: 2px var(--s2);
  border: 1px solid var(--rule);
  border-radius: var(--r);
  background: transparent;
  color: var(--ink-soft);
  font-size: var(--t-micro);
  cursor: pointer;
}

button.on {
  border-color: var(--accent);
  color: var(--accent);
}

.out {
  margin-top: var(--s2);
}

.forms {
  display: flex;
  gap: var(--s2);
  margin-bottom: var(--s2);
}

pre {
  margin: 0;
  padding: var(--s3);
  background: var(--sunk);
  overflow-x: auto;
  font-size: var(--t-micro);
  line-height: 1.5;
  white-space: pre-wrap;
}

.empty {
  color: var(--ink-soft);
  font-style: italic;
}
</style>
