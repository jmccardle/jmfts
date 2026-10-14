// The Ingest form's judgements: whether a request is well formed before it is sent, and whether
// a file's name agrees with what its bytes measured as.
//
// FAIL EARLY, AND ONLY ON WHAT THE APPLIANCE TOLD US. The option fields are not a list kept here:
// they are the RESOLVED options the appliance returned — `ExplainIngestResponse.options` for a
// file, `PipelineInfo.options` for an entry point — so an option the appliance does not have
// cannot be typed into a field, and an override pasted as JSON that names one is refused here
// with the names that do exist. The appliance refuses the same thing with a 400; this is the
// same answer one round trip sooner, and the 400 still shows verbatim if the two ever disagree.

/** option group -> option -> value, as `options` is sent and as it is resolved. */
export type OptionGroups = Record<string, Record<string, unknown>>;

/** What the user typed into one option field, keyed `group.option`. Only edited fields. */
export type OptionEdits = Record<string, string>;

/**
 * Options that were renamed, old dotted name -> new. A refusal naming the new one is worth a
 * table this small: `structure.max_tokens` became `structure.max_words` because the number was
 * always a word count, and the appliance refuses the old name rather than translating it.
 *
 * The hint is offered only when the resolved options carry the NEW name and not the old one,
 * so pointed at an older appliance that still has `max_tokens` this table says nothing.
 */
export const RENAMED: Record<string, string> = {
  "structure.max_tokens": "structure.max_words",
};

/** What kind of field an option gets, decided by the type of its resolved value. */
export type FieldKind = "boolean" | "integer" | "number" | "text" | "json";

export function fieldKind(resolved: unknown): FieldKind {
  if (typeof resolved === "boolean") return "boolean";
  if (typeof resolved === "number") return Number.isInteger(resolved) ? "integer" : "number";
  if (typeof resolved === "string") return "text";
  // null, a list, an object: there is no type to hold the edit to except "valid JSON".
  return "json";
}

/** A resolved value as the text its field starts with. */
export function asFieldText(resolved: unknown): string {
  if (typeof resolved === "string") return resolved;
  return JSON.stringify(resolved);
}

export interface OptionField {
  group: string;
  option: string;
  key: string;
  kind: FieldKind;
  resolved: unknown;
}

/** The fields an options form shows, group by group, from what the appliance resolved. */
export function optionFields(resolved: OptionGroups): OptionField[] {
  const fields: OptionField[] = [];
  for (const group of Object.keys(resolved).sort()) {
    const options = resolved[group];
    if (options === null || typeof options !== "object" || Array.isArray(options)) continue;
    for (const option of Object.keys(options).sort()) {
      fields.push({
        group,
        option,
        key: `${group}.${option}`,
        kind: fieldKind(options[option]),
        resolved: options[option],
      });
    }
  }
  return fields;
}

function knownNames(resolved: OptionGroups, group: string): string {
  const options = resolved[group];
  return options ? Object.keys(options).sort().join(", ") : "";
}

/** Why `group.option` is not an option here, with the rename or the names that do exist. */
function unknownOption(key: string, resolved: OptionGroups): string {
  const [group, option] = splitKey(key);
  if (!(group in resolved)) {
    return (
      `"${group}" is not an option group this appliance resolves. ` +
      `It has: ${Object.keys(resolved).sort().join(", ")}.`
    );
  }
  const renamedTo = RENAMED[key];
  if (renamedTo) {
    const [newGroup, newOption] = splitKey(renamedTo);
    if (resolved[newGroup] && newOption in resolved[newGroup]) {
      return `"${key}" was renamed to "${renamedTo}", and the old name is refused. Use ${renamedTo}.`;
    }
  }
  return `"${option}" is not an option of "${group}". It has: ${knownNames(resolved, group)}.`;
}

function splitKey(key: string): [string, string] {
  const dot = key.indexOf(".");
  return dot < 0 ? [key, ""] : [key.slice(0, dot), key.slice(dot + 1)];
}

/** One typed field's text, parsed to the type its resolved value has. Throws the reason. */
export function parseField(kind: FieldKind, text: string): unknown {
  const trimmed = text.trim();
  switch (kind) {
    case "boolean":
      if (trimmed === "true") return true;
      if (trimmed === "false") return false;
      throw new Error("must be true or false");
    case "integer": {
      if (!/^-?\d+$/.test(trimmed)) throw new Error("must be a whole number");
      return Number(trimmed);
    }
    case "number": {
      const n = Number(trimmed);
      if (trimmed === "" || !Number.isFinite(n)) throw new Error("must be a number");
      return n;
    }
    case "text":
      return text;
    case "json":
      try {
        return JSON.parse(trimmed);
      } catch {
        throw new Error("must be JSON (null, a list, an object, or a quoted string)");
      }
  }
}

export interface ParsedOptions {
  /** The overrides to send, or null when there are none. Only values that differ. */
  options: OptionGroups | null;
  /** One sentence per problem. A non-empty list means the form must not submit. */
  problems: string[];
}

/**
 * Turn the fields that were edited into the `options` a request carries.
 *
 * An edit equal to the resolved value is dropped rather than sent, so a form that was touched
 * and put back sends what an untouched one sends.
 */
export function parseOptions(edits: OptionEdits, resolved: OptionGroups): ParsedOptions {
  const options: OptionGroups = {};
  const problems: string[] = [];
  for (const [key, text] of Object.entries(edits)) {
    const [group, option] = splitKey(key);
    if (!(group in resolved) || !(option in (resolved[group] ?? {}))) {
      problems.push(unknownOption(key, resolved));
      continue;
    }
    const current = resolved[group][option];
    let value: unknown;
    try {
      value = parseField(fieldKind(current), text);
    } catch (failure) {
      problems.push(`${key} ${(failure as Error).message}.`);
      continue;
    }
    if (JSON.stringify(value) === JSON.stringify(current)) continue;
    (options[group] ??= {})[option] = value;
  }
  return { options: Object.keys(options).length ? options : null, problems };
}

/**
 * A pasted `{"group": {"option": value}}` object as field edits, so it is validated by exactly
 * the rules a typed field is. Throws when the text is not that shape at all.
 */
export function editsFromJson(text: string): OptionEdits {
  const trimmed = text.trim();
  if (!trimmed) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch (failure) {
    throw new Error(`options is not JSON: ${(failure as Error).message}`);
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error('options is an object of option groups, e.g. {"structure": {"max_words": 60}}');
  }
  const edits: OptionEdits = {};
  for (const [group, options] of Object.entries(parsed as Record<string, unknown>)) {
    if (options === null || typeof options !== "object" || Array.isArray(options)) {
      throw new Error(`options.${group} must be an object of options, not ${JSON.stringify(options)}`);
    }
    for (const [option, value] of Object.entries(options as Record<string, unknown>)) {
      edits[`${group}.${option}`] = typeof value === "string" ? value : JSON.stringify(value);
    }
  }
  return edits;
}

/** A parent field: empty for none, a positive whole number otherwise. Throws the reason. */
export function parseParentId(text: string): number | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  if (!/^\d+$/.test(trimmed) || Number(trimmed) === 0) {
    throw new Error(`a parent is a document id; "${trimmed}" is not one`);
  }
  return Number(trimmed);
}

/**
 * What an entry point's `content` field must hold, from `PipelineInfo.source`.
 *
 * Only what is certain is checked: that there is something, and that a `url` is an http(s)
 * address. Whether an arXiv id or a server path names anything is the appliance's to answer.
 */
export function sourceProblem(source: string, content: string): string | null {
  const trimmed = content.trim();
  const noun = SOURCE_NOUNS[source] ?? "content";
  if (!trimmed) return `there is no ${noun} to ingest`;
  if (source === "url") {
    let url: URL | null = null;
    try {
      url = new URL(trimmed);
    } catch {
      // Same refusal as a parseable URL with the wrong scheme.
    }
    if (url === null || (url.protocol !== "http:" && url.protocol !== "https:")) {
      return `"${trimmed}" is not an http(s) URL`;
    }
  }
  return null;
}

/** What `content` holds for each `PipelineInfo.source`, in the reader's words. */
export const SOURCE_NOUNS: Record<string, string> = {
  content: "text",
  url: "URL",
  arxiv: "arXiv id",
  path: "server path",
};

// ------------------------------------------------------------- the name and the bytes

/**
 * Extensions a measured format answers to, beyond its own name. Only where the format's name
 * is not itself the usual extension — `jpeg` is `.jpg`, and `ole2` is the container the three
 * pre-2007 Office formats share, which probe names honestly rather than guessing among them.
 */
const ALSO_ANSWERS_TO: Record<string, string[]> = {
  jpeg: ["jpg", "jpe", "jfif"],
  ole2: ["doc", "dot", "xls", "xlt", "ppt", "pot", "msg"],
  postscript: ["ps", "eps"],
};

export type NameVerdict = "agrees" | "disagrees" | "unmeasured" | "no_extension";

export interface NameCheck {
  verdict: NameVerdict;
  extension: string | null;
  message: string;
}

/** The filename's extension, lowercased, no dot; null when it has none. */
export function extensionOf(filename: string): string | null {
  const dot = filename.lastIndexOf(".");
  if (dot <= 0 || dot === filename.length - 1) return null;
  return filename.slice(dot + 1).trim().toLowerCase() || null;
}

/**
 * Does the name agree with the bytes?
 *
 * `detectedBy` null means nothing recognised the bytes and the format IS the extension, so
 * there is nothing to compare — that is `unmeasured`, not `agrees`. `text` is what any UTF-8
 * file sniffs as, so it agrees with every extension except one that names a format the
 * appliance identifies from bytes (`detectable`, from `GET /capabilities`): a `.pdf` that is
 * plain text is a disagreement, a `.csv` is not.
 */
export function checkName(
  filename: string,
  format: string,
  detectedBy: string | null,
  detectable: string[]
): NameCheck {
  const extension = extensionOf(filename);
  if (detectedBy === null) {
    return {
      verdict: "unmeasured",
      extension,
      message:
        "Nothing recognised these bytes, so the format is read from the file name — a hint, " +
        "not evidence.",
    };
  }
  if (extension === null) {
    return {
      verdict: "no_extension",
      extension,
      message: `The name has no extension; the bytes measured as ${format}.`,
    };
  }
  const names = (f: string) => [f, ...(ALSO_ANSWERS_TO[f] ?? [])];
  let agrees: boolean;
  if (format === "text") {
    const claimed = new Set(detectable.filter((f) => f !== "text").flatMap(names));
    agrees = !claimed.has(extension);
  } else {
    agrees = names(format).includes(extension);
  }
  return agrees
    ? { verdict: "agrees", extension, message: `.${extension} and the bytes agree: ${format}.` }
    : {
        verdict: "disagrees",
        extension,
        message:
          `The name says .${extension} but the bytes measured as ${format} (by ${detectedBy}). ` +
          `The appliance plans from the bytes, so this will be ingested as ${format}.`,
      };
}
