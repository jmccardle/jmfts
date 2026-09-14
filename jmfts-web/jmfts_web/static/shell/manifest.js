// The view manifest: one import line per view, and nothing else in this file, ever.
//
// docs/SPRINT_0_6_0.md Block F step 24, IC-7. Each line imports a module for its side effect
// — the module calls `registerView` at its own scope, the way an `@expose`'d service method
// is collected when `jmfts_core` imports the module that defines it. There is no list of view
// OBJECTS here, because a list of objects is a second definition of every view and the whole
// point of IC-7 is that there is only one.
//
// APPEND ONLY, AND APPEND AT THE END. Phase F3 forks five worktrees that each add a view, and
// each of them appends one line here. Two branches appending different lines to the same end
// of the same file is an ordinary git conflict — it is not one here, because
// `.gitattributes` gives this path `merge=union`, which takes both sides of the hunk instead
// of asking somebody to choose. That is the mechanism behind IC-7's pin ("two views added in
// two worktrees merge without conflict") and `tests/test_web_views.py` performs the merge both
// ways round: with the attribute it is clean, and without it the same two appends conflict —
// which is what stops the clean case being luck.
//
// What union merge CANNOT do is notice that two branches added the same view twice. That
// surfaces in `registry.js`, which refuses a duplicate id or route by name on the first page
// load after the merge.
//
// Anything other than an import line belongs in `shell.js`. An edit to an existing line here
// is the one change union merge gets wrong — it would keep both versions — so a view that
// moves is a line deleted and a line appended, in one commit, by one branch.

import "../views/capabilities.js";
