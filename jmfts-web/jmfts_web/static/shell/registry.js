// IC-7: a view registration, and the registry that collects them.
//
// Hand-written, and unlike `client/` there is no `.d.ts` beside it: these modules are not
// generated, so JSDoc on the declaration is the one place the types live rather than the
// second place. `client/*.d.ts` exists because a generator wrote both halves from one source.
//
// docs/SPRINT_0_6_0.md Block F step 24. This mirrors `jmfts_core/registry.py` on the client
// side and for the reason that file's own header gives: there is no hand-written second
// definition to drift. A view declares itself once — id, title, path, component, tags — and
// the shell builds the navigation, the routing table and the mount point from that one
// declaration. Nothing anywhere else names a view.
//
// THE SECOND REASON IS THE ONE THIS STEP EXISTS FOR, and it is not about drift. Phase F3
// forks five worktrees (steps 26 to 30) that each add a view. A router file three worktrees
// edit is a merge conflict scheduled in advance — Part 5's rule 2 — and Part 5.3 lists "the
// client router / every view step" as a collision "removed as a conflict by IC-7. This is the
// whole reason F2 is a sequential phase rather than a file three worktrees share."
//
// So: adding a view is adding a FILE, plus one import line in `manifest.js`, and that file
// says why one line in one shared file is still safe to append from two branches at once.
//
// The pin, from Part 5.1: two views added in two worktrees merge without conflict.
// `tests/test_web_views.py::test_two_views_added_in_two_worktrees_merge_without_conflict`
// performs the merge rather than asserting the property in prose.

/** A registration this registry will not accept, with the reason in the message. */
export class ViewRegistrationError extends Error {
  constructor(message) {
    super(message);
    this.name = "ViewRegistrationError";
  }
}

/**
 * The fields of a view registration, in the order IC-7 writes them.
 *
 * Named here rather than only implied by the checks below, because `tests/test_web_views.py`
 * pins this list against the contract in the sprint plan — the same arrangement
 * `CALL_EVENT_FIELDS` has in `client/transport.js` for IC-6.
 */
export const VIEW_FIELDS = Object.freeze(["id", "title", "path", "component", "tags"]);

const REGISTERED = [];
const BY_ID = new Map();
const BY_PATH = new Map();

const ID_SHAPE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const PATH_SHAPE = /^\/[a-z0-9]+(?:-[a-z0-9]+)*(?:\/[a-z0-9]+(?:-[a-z0-9]+)*)*$/;

/**
 * Declare one view. Called at module scope by the view's own module; nothing else calls it.
 *
 * Every check below REFUSES rather than repairing, and that is deliberate in a file whose
 * whole job is to be appended to by branches that never saw each other. A registration that
 * was quietly normalised — a missing tag defaulted, a duplicate id suffixed — would produce a
 * navigation nobody wrote, and the author of the second view would be the one who found out.
 *
 * @param {{id: string, title: string, path: string,
 *          component: (context: object) => (Element|Promise<Element>),
 *          tags: string[]}} registration
 * @returns {Readonly<object>} the frozen registration, so a view can keep a handle on it.
 */
export function registerView(registration) {
  if (registration === null || typeof registration !== "object") {
    throw new ViewRegistrationError(
      `a view registration is an object with ${VIEW_FIELDS.join(", ")}; got ` +
        `${registration === null ? "null" : typeof registration}`
    );
  }

  // Exactly the five fields — an unknown one is refused rather than ignored. This is the
  // defect of part 1 of this lane turned into a rule: a field name the consumer does not read
  // renders identically to a field that is legitimately empty, and the way to stop that is to
  // make the consumer say what it accepts and reject the rest at the point of declaration.
  const given = Object.keys(registration);
  const unknown = given.filter((name) => !VIEW_FIELDS.includes(name));
  const missing = VIEW_FIELDS.filter((name) => !given.includes(name));
  if (unknown.length || missing.length) {
    throw new ViewRegistrationError(
      `a view registration is exactly {${VIEW_FIELDS.join(", ")}}. ` +
        (unknown.length ? `Unknown: ${unknown.join(", ")}. ` : "") +
        (missing.length ? `Missing: ${missing.join(", ")}.` : "")
    );
  }

  const { id, title, path, component, tags } = registration;

  if (typeof id !== "string" || !ID_SHAPE.test(id)) {
    throw new ViewRegistrationError(
      `view id ${JSON.stringify(id)} must be lowercase words joined by hyphens — it appears ` +
        "in the URL fragment and in the DOM, so it is not free text."
    );
  }
  if (typeof title !== "string" || title.trim() === "") {
    throw new ViewRegistrationError(`view "${id}" needs a title; it is the navigation label.`);
  }
  if (typeof path !== "string" || !PATH_SHAPE.test(path)) {
    throw new ViewRegistrationError(
      `view "${id}" declares path ${JSON.stringify(path)}, which is not a route. A route is ` +
        '"/" followed by lowercase hyphenated segments, e.g. "/capabilities".'
    );
  }
  if (typeof component !== "function") {
    throw new ViewRegistrationError(
      `view "${id}" declares a component that is not a function. A component takes the ` +
        "shell's context and returns an element, or a promise of one."
    );
  }
  if (!Array.isArray(tags) || tags.length === 0 || tags.some((t) => typeof t !== "string" || !t)) {
    throw new ViewRegistrationError(
      `view "${id}" needs at least one non-empty tag. The FIRST tag is the navigation group ` +
        "this view appears under, so a view with no tags is a view with nowhere to live."
    );
  }

  // Two views cannot share an id or a route. The union merge that lets two worktrees append
  // to `manifest.js` without a conflict cannot notice that both appended the same name — so
  // this is where that collision surfaces, loudly, on the first page load after the merge.
  const clashingId = BY_ID.get(id);
  if (clashingId) {
    throw new ViewRegistrationError(
      `two views claim the id "${id}" (${clashingId.title} and ${title}). Two branches added ` +
        "the same view, or two views were given one name."
    );
  }
  const clashingPath = BY_PATH.get(path);
  if (clashingPath) {
    throw new ViewRegistrationError(
      `views "${clashingPath.id}" and "${id}" both claim the route "${path}".`
    );
  }

  const frozen = Object.freeze({ id, title, path, component, tags: Object.freeze([...tags]) });
  REGISTERED.push(frozen);
  BY_ID.set(id, frozen);
  BY_PATH.set(path, frozen);
  return frozen;
}

/** Every registered view, in the order `manifest.js` imports them. */
export function views() {
  return Object.freeze([...REGISTERED]);
}

/** One view by id, or null. */
export function viewById(id) {
  return BY_ID.get(id) ?? null;
}

/** One view by exact route, or null. The shell turns null into "no such view", not a guess. */
export function viewForPath(path) {
  return BY_PATH.get(path) ?? null;
}

/**
 * The navigation groups, in first-appearance order, each with its views.
 *
 * The FIRST tag is the group. The rest are metadata a view can be found by; nothing in the
 * shell reads them yet, and a view is free to carry them for the search this front end does
 * not have. Returned as `[[tag, views], ...]` rather than an object so the order is the one
 * the manifest produced rather than whatever key order a JS engine happens to give.
 */
export function navigationGroups() {
  const groups = new Map();
  for (const view of REGISTERED) {
    const group = view.tags[0];
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(view);
  }
  return [...groups.entries()].map(([tag, members]) => [tag, Object.freeze([...members])]);
}
