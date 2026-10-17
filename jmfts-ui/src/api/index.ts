// The one appliance connection, and the call log every page can read.
//
// The client itself is GENERATED — `@jmfts/client` is `jmfts-web/jmfts_web/static/client/`,
// read in place (see `vite.config.ts`): what `scripts/generate_ts_client.py` renders from the
// appliance's `/openapi.json`, a typed method and a TypeScript interface for every operation.
// Nothing here re-declares a request or a response shape, and there is no copy to replace:
// when the appliance grows an operation, the regenerated files are what this console builds.
//
// WHAT THIS FILE ADDS is the three things a standalone console needs and the bundled one did
// not: a base URL that is chosen at runtime rather than assumed to be this origin, a token
// that survives a reload of a page served from somewhere else entirely, and a reactive view of
// the call stream so the console can show its own traffic.

import { reactive, ref, shallowRef, watch } from "vue";
import { JmftsClient } from "@jmfts/client/index.js";
import type { CallEvent } from "@jmfts/client/transport.js";

export type { CallEvent };
// Every contract shape, as the generator declared it. Types only — the one value this module
// re-exports is the client instance below, so nothing else can construct a second one.
export type * from "@jmfts/client/verbs.js";

/**
 * An appliance address as the transport needs it: empty for this origin, otherwise an absolute
 * http(s) URL with no trailing slash.
 *
 * The transport builds `baseUrl + path`, so an address without a scheme is not an address, it
 * is a relative path. `127.0.0.1:8100` becomes `/127.0.0.1:8100/health` on the page's own
 * origin, and the console reports the wrong thing — a server that answers HTML — instead of
 * the right one, which is that the address is malformed. So it is refused here, once, where
 * the address enters, rather than discovered at every call.
 */
export function parseApplianceUrl(raw: string): string {
  const trimmed = raw.trim().replace(/\/+$/, "");
  if (!trimmed) return "";
  let url: URL | null = null;
  try {
    url = new URL(trimmed);
  } catch {
    // Falls through to the same refusal as a parseable address with the wrong scheme.
  }
  if (url === null || (url.protocol !== "http:" && url.protocol !== "https:")) {
    const hint = trimmed.includes("://") ? "" : `, for example http://${trimmed}`;
    throw new Error(`"${raw.trim()}" is not an appliance address. Include http:// or https://${hint}`);
  }
  return trimmed;
}

/**
 * Set when the address remembered for this tab is not one `parseApplianceUrl` accepts — which
 * is what an earlier build of this console would store for a schemeless address. The stored
 * value is removed so it cannot break the next load as well, and the reason is kept here for
 * the frame to show; it is not dropped silently.
 */
export const storedAddressProblem = ref<string | null>(null);

/** Where the appliance is. */
export const baseUrl = ref<string>(resolveBaseUrl());

function resolveBaseUrl(): string {
  // A built bundle reads a global the host page sets, so one build serves next to any
  // appliance. In development this is empty and Vite proxies, which keeps the page
  // same-origin and means the appliance needs no CORS header to develop against.
  const declared = (window as { __JMFTS_BASE__?: string }).__JMFTS_BASE__;
  const remembered = safeRead("jmfts.base");
  if (remembered !== null) {
    try {
      return parseApplianceUrl(remembered);
    } catch (failure) {
      safeWrite("jmfts.base", null);
      storedAddressProblem.value = `Ignored the remembered address. ${(failure as Error).message}`;
    }
  }
  return declared ?? "";
}

/**
 * The token, kept for the tab and not for the browser.
 *
 * `sessionStorage`, never `localStorage` and never the URL: a closed tab has forgotten the
 * credential and a pasted address does not carry it. Reads are wrapped because a private
 * window can throw on the accessor rather than return null.
 */
const TOKEN_KEY = "jmfts.token";

function safeRead(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeWrite(key: string, value: string | null): void {
  try {
    if (value === null) window.sessionStorage.removeItem(key);
    else window.sessionStorage.setItem(key, value);
  } catch {
    // A browser that refuses storage still gets a working page for this tab; the token
    // lives in `token` below either way.
  }
}

export const token = ref<string | null>(safeRead(TOKEN_KEY));

export function connect(value: string): void {
  const trimmed = value.trim();
  if (!trimmed) throw new Error("Enter a token, or choose Forget token to clear it.");
  token.value = trimmed;
  safeWrite(TOKEN_KEY, trimmed);
}

export function forget(): void {
  token.value = null;
  safeWrite(TOKEN_KEY, null);
}

export function pointAt(url: string): void {
  baseUrl.value = parseApplianceUrl(url);
  safeWrite("jmfts.base", baseUrl.value || null);
}

export const client = new JmftsClient({
  baseUrl: baseUrl.value,
  getCredential: (scheme: string) => (scheme === "JMFTSToken" ? token.value : null),
});

// `baseUrl` is a plain property the transport reads per request, so pointing the console at a
// different appliance is an assignment rather than a new client — which matters because the
// call log is the client's own stream and a new instance would start an empty one.
watch(baseUrl, (next) => {
  client.baseUrl = next;
});

/**
 * Every call this console has made, newest first.
 *
 * The console adds nothing and hides nothing, which is the property worth having: a figure on
 * a page with no call behind it was computed in the browser, and you can see that from here.
 */
export const calls = shallowRef<CallEvent[]>([]);

client.onCall((event: CallEvent) => {
  calls.value = [event, ...calls.value].slice(0, 200);
});

/** Whether the appliance answered at all, and what it said about itself. */
export const reachability = reactive<{
  state: "unknown" | "reaching" | "ok" | "unauthorized" | "unreachable";
  detail: string;
}>({ state: "unknown", detail: "" });
