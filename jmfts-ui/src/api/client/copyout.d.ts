// Types for the three copy-out renderers. Hand-written; `copyout.js` is the code.

import type { CallEvent } from "./transport.js";

export interface CopyOutOptions {
  /**
   * The appliance's origin to print. Empty keeps the URL relative, which is right for
   * `fetch` from the same page and wrong for a `curl` somebody will paste into a terminal.
   */
  baseUrl?: string;
}

/** The event as a `curl` invocation, with the credential named and never printed. */
export declare function asCurl(event: CallEvent, options?: CopyOutOptions): string;

/**
 * The event as a `jmfts_client.RemoteJmftsClient` call, or as the `httpx` call that is its
 * honest equivalent for a route with no generated verb.
 */
export declare function asPython(event: CallEvent, options?: CopyOutOptions): string;

/** The event as the browser `fetch` this client itself issued. */
export declare function asFetch(event: CallEvent, options?: CopyOutOptions): string;
