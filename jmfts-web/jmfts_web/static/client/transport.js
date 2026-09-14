// @ts-self-types="./transport.d.ts"
//
// The ONE request path the front end has. Hand-written; everything beside it is generated.
//
// docs/SPRINT_0_6_0.md Block F step 23, IC-6. The same split jmfts-client has between
// `_verbs.py` (generated, no behaviour) and `jmfts_client/transport.py` (hand-written, all
// of it), and for the same reason: regenerating the verb table must not be able to lose
// anything. `operations.js` says where each argument goes; this file puts it there.
//
// EVERY call emits exactly one event, and that is the property the whole step exists for.
// A view that shows something the call log has no event for computed it in the browser, and
// that is readable from the log rather than arguable from the source.

import { OPERATIONS } from "./operations.js";

/**
 * The fields of a call event, in the order IC-6 writes them.
 *
 * Named here rather than only implied by the object literal below, because
 * `tests/test_ts_client_codegen.py` pins this list against the contract in the sprint plan.
 * The event is built from `_event()` and nothing else builds one.
 */
export const CALL_EVENT_FIELDS = Object.freeze([
  "op_id",
  "method",
  "path",
  "path_params",
  "query",
  "body",
  "status",
  "response",
  "ms",
]);

/**
 * Where each credential is kept in this tab.
 *
 * sessionStorage, never localStorage and never the URL: a closed tab has forgotten the
 * credential, and a pasted address does not carry one. The two keys are disjoint because the
 * credentials are — `jmfts_core/rest/auth.py`, "Two credentials, kept disjoint". A browser
 * normally holds only the first; the second is here because `/runner/*` is part of the
 * mounted surface and a client that silently sent the wrong one would earn a 401 with
 * nothing on the page to explain it.
 */
export const CREDENTIAL_KEYS = Object.freeze({
  JMFTSToken: "jmfts.token",
  JMFTSRunnerKey: "jmfts.runner_key",
});

/** The API token's sessionStorage key, which is what almost every caller wants. */
export const TOKEN_KEY = CREDENTIAL_KEYS.JMFTSToken;

/** The request never went out, or what came back could not be read as a response. */
export class JmftsTransportError extends Error {
  constructor(message) {
    super(message);
    this.name = "JmftsTransportError";
  }
}

/** The appliance answered, and it answered 4xx or 5xx. */
export class JmftsHttpError extends Error {
  constructor(event) {
    super(`${event.method} ${event.path} — HTTP ${event.status}: ${detailOf(event.response)}`);
    this.name = "JmftsHttpError";
    /** The call event this error came out of, so a handler can replay or print it. */
    this.event = event;
    this.status = event.status;
    this.op_id = event.op_id;
  }
}

/** The server's `detail`, or the body as it arrived when the error was not JMFTS-shaped. */
export function detailOf(response) {
  if (response && typeof response === "object" && "detail" in response) {
    const detail = response.detail;
    return typeof detail === "string" ? detail : JSON.stringify(detail);
  }
  if (typeof response === "string") return response;
  if (response instanceof Error) return response.message;
  return JSON.stringify(response);
}

/** Read one credential out of this tab. Returns null when the tab does not hold it. */
export function credentialFromSession(scheme) {
  const key = CREDENTIAL_KEYS[scheme];
  if (!key) {
    throw new JmftsTransportError(
      `the document names security scheme "${scheme}", which this client has no key for. ` +
        "Add it to CREDENTIAL_KEYS, or the appliance has grown a credential the browser " +
        "was never told how to hold."
    );
  }
  return window.sessionStorage.getItem(key);
}

export class JmftsTransport {
  /**
   * @param {{baseUrl?: string, getCredential?: (scheme: string) => string | null,
   *          fetchImpl?: typeof fetch, now?: () => number}} [options]
   */
  constructor(options = {}) {
    // Same origin by default: the appliance serves this bundle at /app, so "" reaches the
    // API it was generated from without a CORS entry and without a configured hostname.
    this.baseUrl = (options.baseUrl ?? "").replace(/\/$/, "");
    this.getCredential = options.getCredential ?? credentialFromSession;
    this._fetch = options.fetchImpl ?? ((...args) => fetch(...args));
    this._now = options.now ?? (() => performance.now());
    this._listeners = new Set();
  }

  /** Subscribe to the call event stream. Returns the unsubscribe function. */
  onCall(listener) {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  }

  /** The operation table, so a caller can ask what exists without importing it separately. */
  get operations() {
    return OPERATIONS;
  }

  /**
   * Issue one operation and return its parsed result.
   *
   * `args` is one flat object keyed by parameter name — path, query and body alike — so a
   * call is a value, which is what makes an event replayable. An argument the operation does
   * not declare throws rather than being dropped: a misspelt parameter that silently does
   * nothing is the failure this refusal exists to make loud.
   */
  async call(opId, args = {}) {
    const op = OPERATIONS[opId];
    if (!op) {
      throw new JmftsTransportError(`no such operation: ${opId}`);
    }
    const placed = place(op, args);
    const path = fillPath(op, placed.path_params);
    const url = this.baseUrl + path + queryString(placed.query);

    const headers = {};
    if (op.security_scheme) {
      const credential = this.getCredential(op.security_scheme);
      if (!credential) {
        throw new JmftsTransportError(
          `${op.method} ${op.path} needs the ${op.security_scheme} credential and this tab ` +
            `holds none. Set sessionStorage["${CREDENTIAL_KEYS[op.security_scheme]}"].`
        );
      }
      headers.Authorization = `Bearer ${credential}`;
    }

    const request = { method: op.method, headers };
    if (op.body_media_type === "multipart/form-data") {
      // No Content-Type header: the boundary is the browser's to choose, and setting the
      // header by hand drops it, which makes the server fail to parse a body that is fine.
      const form = new FormData();
      for (const name of op.file_params) {
        if (placed.body[name] !== undefined) form.append(name, placed.body[name]);
      }
      for (const name of op.form_params) {
        const value = placed.body[name];
        if (value !== undefined && value !== null) form.append(name, JSON.stringify(value));
      }
      request.body = form;
    } else if (op.body_param && placed.body !== undefined) {
      if (op.body_media_type === "application/json") {
        headers["Content-Type"] = "application/json";
        request.body = JSON.stringify(placed.body);
      } else {
        // A body the route declared under a media type of its own goes out verbatim. The
        // charset is explicit because a missing Content-Type reads as JSON on the server.
        headers["Content-Type"] = `${op.body_media_type}; charset=utf-8`;
        request.body = placed.body;
      }
    }

    const started = this._now();
    let status = 0;
    let response = null;
    let failure = null;
    try {
      const reply = await this._fetch(url, request);
      status = reply.status;
      response = await readBody(op, reply);
    } catch (error) {
      // The Error itself becomes the event's `response`, and `status` stays at whatever the
      // exchange reached — 0 when the request never went out. IC-6 fixes the nine fields, so
      // a tenth for "this one failed" would be a field every consumer has to learn; 0 is
      // already the platform's value for "no HTTP response", and the Error says why.
      failure = error instanceof Error ? error : new JmftsTransportError(String(error));
      response = failure;
    }

    const event = this._event(op, path, placed, status, response, this._now() - started);
    if (failure) throw failure;
    if (status >= 400) throw new JmftsHttpError(event);
    return response;
  }

  /**
   * Re-send a call event, optionally with edited arguments.
   *
   * The reason the event carries its three argument buckets separately: an event is a call
   * plus what it was called with, so it is re-sendable — `/docs`' "Try it out" with the
   * result half already written.
   */
  replay(event, overrides = {}) {
    const op = OPERATIONS[event.op_id];
    if (!op) {
      throw new JmftsTransportError(`no such operation: ${event.op_id}`);
    }
    const args = { ...event.path_params, ...event.query };
    if (op.body_param) args[op.body_param] = event.body;
    else if (event.body) Object.assign(args, event.body);
    return this.call(event.op_id, { ...args, ...overrides });
  }

  _event(op, path, placed, status, response, ms) {
    const event = {
      op_id: op.op_id,
      method: op.method,
      path,
      path_params: placed.path_params,
      query: placed.query,
      body: placed.body,
      status,
      response,
      ms: Math.round(ms),
    };
    for (const listener of this._listeners) listener(event);
    return event;
  }
}

/** Split one flat argument object into the three buckets the wire has. */
function place(op, args) {
  const known = new Set([
    ...op.path_params,
    ...op.query_params,
    ...op.form_params,
    ...op.file_params,
  ]);
  if (op.body_param) known.add(op.body_param);
  const unknown = Object.keys(args).filter((name) => !known.has(name));
  if (unknown.length) {
    throw new JmftsTransportError(
      `${op.op_id} has no parameter ${unknown.map((n) => `"${n}"`).join(", ")}. ` +
        `It takes: ${[...known].join(", ") || "(nothing)"}.`
    );
  }

  const path_params = {};
  for (const name of op.path_params) {
    if (args[name] === undefined || args[name] === null) {
      throw new JmftsTransportError(`${op.op_id} needs a value for path parameter "${name}"`);
    }
    path_params[name] = args[name];
  }

  const query = {};
  for (const name of op.query_params) {
    // undefined and null are both "not specified". A server default and an explicitly sent
    // null are different things, and this client sends the first.
    if (args[name] !== undefined && args[name] !== null) query[name] = args[name];
  }

  let body;
  if (op.body_media_type === "multipart/form-data") {
    body = {};
    for (const name of [...op.file_params, ...op.form_params]) {
      if (args[name] !== undefined) body[name] = args[name];
    }
  } else if (op.body_param) {
    body = args[op.body_param];
  }
  return { path_params, query, body };
}

/** Substitute `{name}` placeholders, percent-encoding each value. */
export function fillPath(op, values) {
  let out = op.path;
  for (const [name, value] of Object.entries(values)) {
    // One path uses Starlette's `{usetype:path}` converter, whose whole point is that the
    // value may contain "/". The document cannot say so — `compile_path` strips converters
    // — which is why `operations.js` carries `path_slash_params`; see the generator.
    const keepSlashes = op.path_slash_params.includes(name);
    const text = String(value);
    const encoded = keepSlashes
      ? text.split("/").map(encodeURIComponent).join("/")
      : encodeURIComponent(text);
    out = out.replace(`{${name}}`, encoded);
  }
  return out;
}

/** Render the query string, the way the server's parser expects to read it. */
export function queryString(query) {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(query)) {
    for (const one of Array.isArray(value) ? value : [value]) {
      params.append(name, queryValue(one));
    }
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

function queryValue(value) {
  // FastAPI's bool parser wants lowercase, and `String(true)` already gives it; a Date is
  // sent as ISO-8601 because that is what the server's datetime parser reads.
  if (value instanceof Date) return value.toISOString();
  return String(value);
}

/** Read one response body: bytes for a binary operation, parsed JSON otherwise. */
async function readBody(op, reply) {
  if (reply.status === 204) return null;
  const contentType = reply.headers.get("content-type") || "";
  // `op.binary` is the DECLARED answer and it governs only the success case: an error on a
  // binary route answers `application/json` like every other error, and handing back a Blob
  // of the error text would hide the detail the page needs to show.
  if (op.binary && reply.ok) return await reply.blob();
  if (contentType.includes("json")) {
    const text = await reply.text();
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch (error) {
      throw new JmftsTransportError(
        `${op.method} ${op.path} answered ${reply.status} with a body declared JSON that is ` +
          `not JSON: ${error.message}`
      );
    }
  }
  return await reply.text();
}
