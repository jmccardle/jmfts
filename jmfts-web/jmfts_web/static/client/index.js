// @ts-self-types="./index.d.ts"
//
// The one import a view needs. Hand-written.
//
// docs/SPRINT_0_6_0.md Block F step 23. Every view calls ONE client, and this is the door to
// it — `import { JmftsClient } from "./client/index.js"`. Nothing here has behaviour; the
// request path is transport.js, the operation table and the verbs are generated, and the
// three printers are copyout.js.
//
// No build step and no bundler: this is an ES module the browser executes as it stands, and
// it loads nothing from anywhere but this origin. The appliance is expected to run
// air-gapped, and a page that renders blank without internet is worse than no page.

export { JmftsClient } from "./verbs.js";
export { OPERATIONS, OP_IDS } from "./operations.js";
export {
  CALL_EVENT_FIELDS,
  CREDENTIAL_KEYS,
  TOKEN_KEY,
  JmftsHttpError,
  JmftsTransport,
  JmftsTransportError,
  detailOf,
} from "./transport.js";
export { asCurl, asFetch, asPython } from "./copyout.js";
