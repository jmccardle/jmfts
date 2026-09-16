// Types for the hand-written request path. Hand-written; `transport.js` is the code.

import type { Operation, OpId } from "./operations.js";

/**
 * One call, and what it was called with. IC-6, `docs/SPRINT_0_6_0.md` Part 5.1.
 *
 * Exactly nine fields, and the reason the argument buckets are kept apart rather than merged
 * back into one object is that an event has to be re-sendable with one of them edited.
 */
export interface CallEvent {
  /** `ServiceClass.method`, or the route name for the seven hand-written routes. */
  op_id: OpId;
  method: string;
  /** The RESOLVED path: placeholders filled and percent-encoded. No query string. */
  path: string;
  path_params: Record<string, unknown>;
  /** Query arguments that were actually sent — an unspecified one is absent, not null. */
  query: Record<string, unknown>;
  /**
   * The request body as the caller gave it: the JSON value, the raw text, or for a
   * multipart operation an object of its parts (a file part is the Blob itself, by
   * reference — the log holds no copy of the bytes).
   */
  body: unknown;
  /** The HTTP status, or 0 when the request never reached a response. */
  status: number;
  /**
   * The parsed response: JSON, text, a Blob for a binary operation, `null` for a 204, or
   * the `Error` when `status` is 0 or the body could not be read.
   */
  response: unknown;
  /** Wall time for the whole exchange, rounded to the millisecond. */
  ms: number;
}

export declare const CALL_EVENT_FIELDS: ReadonlyArray<keyof CallEvent>;
export declare const CREDENTIAL_KEYS: Readonly<Record<string, string>>;
export declare const TOKEN_KEY: string;

export declare class JmftsTransportError extends Error {}

export declare class JmftsHttpError extends Error {
  event: CallEvent;
  status: number;
  op_id: OpId;
}

export declare function detailOf(response: unknown): string;
export declare function credentialFromSession(scheme: string): string | null;
export declare function fillPath(op: Operation, values: Record<string, unknown>): string;
export declare function queryString(query: Record<string, unknown>): string;

export interface JmftsTransportOptions {
  /** Where the appliance is. Empty (the default) is same-origin, which is how /app serves. */
  baseUrl?: string;
  /** Where credentials come from. The default reads this tab's sessionStorage. */
  getCredential?: (scheme: string) => string | null;
  fetchImpl?: typeof fetch;
  now?: () => number;
}

export declare class JmftsTransport {
  constructor(options?: JmftsTransportOptions);
  baseUrl: string;
  readonly operations: Readonly<Record<OpId, Operation>>;
  /** Subscribe to the call event stream; returns the unsubscribe function. */
  onCall(listener: (event: CallEvent) => void): () => void;
  call(opId: OpId, args?: Record<string, unknown>): Promise<unknown>;
  replay(event: CallEvent, overrides?: Record<string, unknown>): Promise<unknown>;
}
