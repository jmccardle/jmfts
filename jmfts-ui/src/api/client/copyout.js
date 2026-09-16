// @ts-self-types="./copyout.d.ts"
//
// A call event, printed three ways. Hand-written.
//
// docs/SPRINT_0_6_0.md Block F step 23, third property: "an operation id maps to a
// `_verbs.py` method name, so an event prints as curl with the token elided, as a
// RemoteJmftsClient call, and as fetch. Worked examples stop being a thing anybody writes
// by hand."
//
// That is the point. Every example in this appliance's documentation of how to call it is a
// thing that can go stale; a printed event cannot, because it is a transcript of a request
// that was actually made a moment ago, rendered from the same table the request was built
// from.
//
// THE CREDENTIAL IS NEVER PRINTED. Each renderer names the environment variable or the
// variable the reader is expected to supply. A copy button that put the appliance's token on
// the clipboard, and from there into a chat window or a bug report, would be a credential
// leak with a convenience feature around it.

import { OPERATIONS } from "./operations.js";
import { queryString } from "./transport.js";

/** The shell/environment variable each credential is named by, never its value. */
const CREDENTIAL_ENV = Object.freeze({
  JMFTSToken: "JMFTS_API_TOKEN",
  JMFTSRunnerKey: "JMFTS_RUNNER_KEY",
});

function operationFor(event) {
  const op = OPERATIONS[event.op_id];
  if (!op) throw new Error(`no such operation: ${event.op_id}`);
  return op;
}

/** `'...'` for POSIX sh, with embedded single quotes closed and reopened. */
function shellQuote(text) {
  return `'${String(text).replace(/'/g, `'\\''`)}'`;
}

/** Continue a multi-line literal from the column it was written at. */
function reindent(text, spaces) {
  const pad = " ".repeat(spaces);
  return text.split("\n").join(`\n${pad}`);
}

/** What a file part is, without its bytes: curl and fetch both need the name, not the data. */
function partName(value) {
  if (value && typeof value === "object" && typeof value.name === "string") return value.name;
  return "FILE";
}

/**
 * The event as a `curl` invocation.
 *
 * @param {object} event a call event
 * @param {{baseUrl?: string}} [options] the appliance's origin; "" keeps the path relative
 */
export function asCurl(event, options = {}) {
  const op = operationFor(event);
  const base = (options.baseUrl ?? "").replace(/\/$/, "");
  const url = base + event.path + queryString(event.query);
  const lines = [`curl -sS -X ${op.method}`];

  if (op.security_scheme) {
    // Double quotes so the shell expands the variable; the value itself is never here.
    lines.push(`  -H "Authorization: Bearer $${CREDENTIAL_ENV[op.security_scheme]}"`);
  }
  if (op.body_media_type === "multipart/form-data") {
    for (const name of op.file_params) {
      if (event.body?.[name] !== undefined) {
        lines.push(`  -F ${shellQuote(`${name}=@${partName(event.body[name])}`)}`);
      }
    }
    for (const name of op.form_params) {
      if (event.body?.[name] !== undefined && event.body[name] !== null) {
        lines.push(`  -F ${shellQuote(`${name}=${JSON.stringify(event.body[name])}`)}`);
      }
    }
  } else if (op.body_param && event.body !== undefined) {
    if (op.body_media_type === "application/json") {
      lines.push(`  -H 'Content-Type: application/json'`);
      lines.push(`  -d ${shellQuote(JSON.stringify(event.body))}`);
    } else {
      lines.push(`  -H ${shellQuote(`Content-Type: ${op.body_media_type}; charset=utf-8`)}`);
      lines.push(`  -d ${shellQuote(event.body)}`);
    }
  }
  if (op.binary) {
    lines.push(`  -o out.${(op.response_media_type || "bin").split("/").pop()}`);
  }
  lines.push(`  ${shellQuote(url)}`);
  return lines.join(" \\\n");
}

/** A JSON value as Python source. */
function asPythonLiteral(value, indent = "") {
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    const inner = value.map((v) => `${indent}    ${asPythonLiteral(v, `${indent}    `)}`);
    return `[\n${inner.join(",\n")},\n${indent}]`;
  }
  if (value instanceof Blob) return `open(${JSON.stringify(partName(value))}, "rb").read()`;
  const entries = Object.entries(value);
  if (entries.length === 0) return "{}";
  const inner = entries.map(
    ([k, v]) => `${indent}    ${JSON.stringify(k)}: ${asPythonLiteral(v, `${indent}    `)}`
  );
  return `{\n${inner.join(",\n")},\n${indent}}`;
}

/**
 * The event as a `jmfts_client.RemoteJmftsClient` call.
 *
 * Seven of the appliance's routes are hand-written FastAPI routes rather than `@expose`'d
 * service methods, so they have no generated verb. For those this prints the `httpx` call
 * that is the honest equivalent, rather than a client method that does not exist.
 */
export function asPython(event, options = {}) {
  const op = operationFor(event);
  const base = options.baseUrl || "http://localhost:8100";
  const env = op.security_scheme ? CREDENTIAL_ENV[op.security_scheme] : null;

  if (!op.python_verb) {
    const header = env ? `, headers={"Authorization": f"Bearer {os.environ['${env}']}"}` : "";
    const body =
      op.body_param && event.body !== undefined ? `, json=${asPythonLiteral(event.body)}` : "";
    const url = `${base}${event.path}${queryString(event.query)}`;
    return [
      ...(env ? ["import os", ""] : []),
      "import httpx",
      "",
      `# ${op.op_id} is a hand-written route: jmfts-client has no verb for it.`,
      `httpx.request(${JSON.stringify(op.method)}, ${JSON.stringify(url)}${header}${body})`,
    ].join("\n");
  }

  // Arguments are printed one per line at eight spaces, so nested literals continue from
  // there: a pasted call has to be valid Python, not merely recognisable as one.
  const inner = "        ";
  const args = [];
  for (const name of op.path_params) {
    args.push(`${name}=${asPythonLiteral(event.path_params[name], inner)}`);
  }
  if (op.body_param && event.body !== undefined) {
    args.push(`${op.body_param}=${asPythonLiteral(event.body, inner)}`);
  }
  for (const name of [...op.file_params, ...op.form_params]) {
    if (event.body?.[name] !== undefined) {
      args.push(`${name}=${asPythonLiteral(event.body[name], inner)}`);
    }
  }
  for (const [name, value] of Object.entries(event.query)) {
    args.push(`${name}=${asPythonLiteral(value, inner)}`);
  }

  const token = env ? `, token=os.environ[${JSON.stringify(env)}]` : "";
  const imports = env ? ["import os", ""] : [];
  return [
    ...imports,
    "from jmfts_client import RemoteJmftsClient",
    "",
    `with RemoteJmftsClient(${JSON.stringify(base)}${token}) as client:`,
    `    result = client.${op.python_verb}(`,
    ...args.map((arg) => `        ${arg},`),
    "    )",
  ].join("\n");
}

/** The event as a browser `fetch`, which is what this client itself did. */
export function asFetch(event, options = {}) {
  const op = operationFor(event);
  const base = (options.baseUrl ?? "").replace(/\/$/, "");
  const url = base + event.path + queryString(event.query);
  const headers = [];
  if (op.security_scheme) headers.push(`      "Authorization": \`Bearer \${token}\`,`);

  const init = [`    method: ${JSON.stringify(op.method)},`];
  let prelude = [];
  if (op.body_media_type === "multipart/form-data") {
    prelude = ["const form = new FormData();"];
    for (const name of op.file_params) {
      if (event.body?.[name] !== undefined) {
        // The bytes are not printable, so this names where they come from and says which
        // file was actually sent. Nothing here pretends to reproduce the upload.
        prelude.push(
          `form.append(${JSON.stringify(name)}, fileInput.files[0]); ` +
            `// was: ${partName(event.body[name])}`
        );
      }
    }
    for (const name of op.form_params) {
      if (event.body?.[name] !== undefined && event.body[name] !== null) {
        prelude.push(
          `form.append(${JSON.stringify(name)}, ${JSON.stringify(
            JSON.stringify(event.body[name])
          )});`
        );
      }
    }
    init.push("    body: form,");
  } else if (op.body_param && event.body !== undefined) {
    if (op.body_media_type === "application/json") {
      headers.push(`      "Content-Type": "application/json",`);
      init.push(`    body: JSON.stringify(${reindent(JSON.stringify(event.body, null, 2), 4)}),`);
    } else {
      const declared = JSON.stringify(`${op.body_media_type}; charset=utf-8`);
      headers.push(`      "Content-Type": ${declared},`);
      init.push(`    body: ${JSON.stringify(event.body)},`);
    }
  }
  if (headers.length) init.splice(1, 0, "    headers: {", ...headers, "    },");

  const read = op.binary ? "await response.blob()" : "await response.json()";
  return [
    ...prelude,
    `const response = await fetch(${JSON.stringify(url)}, {`,
    ...init,
    "});",
    `const result = ${read};`,
  ].join("\n");
}
