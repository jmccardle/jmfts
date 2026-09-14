# Sprint 0.6.0: eyes and hands on the appliance

Written 2026-09-13, against `master` at `4349f59`, which is `v0.5.1`.

**The headline is a web front end, and it was added to this plan on 2026-09-13 after the
other six blocks were written.** The document is kept in the order it was written rather than
reorganised around the new block, because the six blocks are the argument for why the seventh
is worth building: an appliance whose access gates do not close and whose retrieval paths
answer with nothing is an appliance that nobody has looked at directly. Part 2's opening
paragraph is the schedule; Part 5 is how it runs.

**This is the first sprint plan written in the public repository.** Development moved here at
0.5.1, so this document is not a copy of an internal one and there is no internal version of
it to diverge from. It cites `docs/SPRINT_0_4_0.md` and `docs/SPRINT_0_5_0.md`, which are
**not** published and, as of 2026-09-13, **never will be** — question 4.5 is answered and the
answer is to hold them. A reader outside the project cannot open them and should not wait for
them; what is still open out of both plans is a numbered step in this one, which is the point
of collecting it here. Block E publishes the specifications and the measurement records, which
is a different set and a larger one.

**Where the scope came from.** `docs/SPRINT_0_4_0.md` and `docs/SPRINT_0_5_0.md` were read end
to end and every item neither finished is collected here: 0.4.0's Block C, deferred whole;
0.4.0's step 14, which diverged and left a residue; 0.5.0's stretch, which neither trigger
fired for; and the numbered gaps in `ROADMAP.md` that acquired no owner. Nothing was dropped
for being inconvenient — what is not a step is in Part 3 or Part 4 with the reason.

**Theme: what this appliance does not stop, what it does not return, and that nobody can see
either.** Two access gates are open and a principal can act through both. Three retrieval
paths answer with nothing and say nothing about why. Both halves are the same shape — a check
that is absent, and an absence that looks like an answer — and Fail Early is the rule both
violate. Block F is the third half and the reason the first two are listed together: every
defect in this plan was found by reading the source, because reading the source is the only
way anybody has ever looked at this appliance.

---

## Part 0 — How a defect enters this sprint

Unchanged from `docs/SPRINT_0_4_0.md` Part 0, restated because this is the first plan a public
reader will meet:

**An open defect is a numbered step in the current sprint plan, and the entry condition is a
failing test.** Not an argument that something could go wrong, and not a measurement of how
often it does.

There is no standing defect file. `docs/archive/KNOWN-DEFECTS.md` holds D1–D7, all resolved,
and records history. `ROADMAP.md`'s numbered gaps are not defects: a gap there is something
somebody has to decide or measure before it could be a step, and the gaps that became steps
here say so in their rows.

The rule decides three live cases in this sprint's own scope, and it decides them
differently:

* **Block A's two gates are steps.** Each is a missing check at a named line, and the test
  that fails is one a reader can write from the row without a measurement: a principal acts
  on a document it may not act on, and the call succeeds.
* **Block B's step 7 is a step.** `docs/STRESS_CORPUS.md` 4.4 measured zero `index:bm25` tasks
  across a 21-workbook subtree, and a fixture that ingests one workbook and searches BM25 for
  a string in it fails today.
* **`ROADMAP.md` gaps 7, 10 and 11 are NOT steps.** Each is a decision with a measurement
  behind it and no failing test — the appliance does what it was built to do in all three,
  and what is wrong is the thing it was built to do. They are Part 4 open questions. Gap 7 in
  particular is not a defect against `jmfts_core` at all: `CHANGELOG.md`'s 0.5.1 entry
  records that the dead-node behaviour under it is pgvector deferring graph repair, which is
  a documented design choice of that extension.

---

## Part 1 — What verified this scope

Every row is a `grep` or a read over this tree at `4349f59`, on 2026-09-13.

| Claim | Where |
|---|---|
| `DocumentRepository.create_link` constructs and flushes a `DocumentLink` with no principal check | `jmfts_core/repositories/document.py:1119`, flush at `:1136` |
| `DocumentService.create_link` checks only that `request.source_id` equals the id in the URL | `jmfts_core/services/document_service.py:1287`, check at `:1289` |
| `get_centrality`, `get_subtree_authority`, `get_spines` and `get_communities` take no principal | `jmfts_core/services/graph_service.py:137`, `:179`, `:234`, `:381` |
| `can_read` is called ONCE in that file, in `get_neighbors` | import at `:58`, call at `:335` |
| `index:bm25` names three prose rungs in `after_any` and not `structure:sheets`, and requires `HAS_TEXT_LAYER` | `jmfts_core/ingest_tasks.py:1173`–`:1182` |
| That row's own comment says "exactly one of the two rungs is ever eligible for a document" | `jmfts_core/ingest_tasks.py:1161`–`:1162` |
| `HEADER_ROW_NUMBER = 1` is a module constant, not a measurement | `jmfts_core/sheet_records.py:60`, skipped at `:161` |
| `_header_evidence` requires the whole used extent to be text | `jmfts_core/office/sheets.py:391` and its docstring |
| `rendered_markdown` is built, counted, reported as a boolean and discarded | `jmfts_core/sheet_tasks.py:437`, `:442`, `:499` |
| `MINIMUM_RECALL = 0.90` and `RECALL_FLOOR` is 13 ULP under it | `tests/test_maxsim_recall.py:80`, `:98` |
| `HNSW_ITERATIVE_SCAN = "strict_order"`, set with `SET LOCAL` on two paths | `jmfts_core/repositories/search.py:62`, `:455`, `:1136` |
| `search_exclude_usetypes` defaults to four names, one of which is what hides the derived tree | `jmfts_core/config.py:219` |
| `shacl_max_scope_documents` exists and defaults to 512 | `jmfts_core/config.py:307` |
| No `POST /documents/batch` anywhere | `git grep documents/batch` over `jmfts_core jmfts-client tests` returns nothing |
| 695 citations from shipped files name a document this repository does not carry | the `git grep -hoE` command in `CLAUDE.md`'s Documentation section, summed over the held-back names |

**One number above is much larger than the reading `CLAUDE.md` carries.** That file says
"around four hundred source comments cite a `docs/` file", read 2026-09-10. Re-running its own
command on 2026-09-13 gives 695 over the held-back names, the three largest being
`SPRINT_JOBS.md` at 158, `INGEST_SPEC.md` at 149 and `SPRINT_0_5_0.md` at 87. The command is
in `CLAUDE.md` precisely because the number moves; Block E is what the number argues for.

---

## Part 2 — The scope

**Seven blocks, thirty-five steps.** The blocks are a schedule and the order is argued here.
Step 35 was added on 2026-09-13 by Block A step 3's audit, on its first run — see that
block. The count moved because the sprint found a defect, which is the plan working.

**Block F is the headline and the rest ride along with it.** 0.6.0 ships a web front end: drag
a file in, search it, click a result, see the page and the rectangle the answer came from.
That is the beeline, and everything below is placed by whether the beeline runs through it.

* **F runs first and runs throughout.** Its opening steps are contracts, which are small and
  which nothing else may fork before. Part 5 is the schedule at worktree granularity.
* **A rides along with F's first parallel slot** because every view is principal-scoped. A
  document-detail page renders a "create link" control, and step 1 is what makes that control
  honest rather than a button that succeeds when it should not. Step 3's audit is the same
  list the front end needs in order to know which verbs are safe to put on a page.
* **C rides along** because header detection is what the spreadsheet-region renderer displays.
  A region view over a sheet whose header row was guessed wrong shows the wrong thing, and
  shows it to somebody looking straight at it.
* **B step 7 rides along** because a search page over a workbook returns nothing today, and
  searching an uploaded spreadsheet is the first thing anyone will try.
* **D rides along in the second parallel slot.** `render:pdf` is a new task type that needs a
  badge, which is the fleet-routing case phase 5 is about; step 14's cost fold is also what
  the upload view's `EXPLAIN` panel would display.
* **E and B step 8 are pre-tag work.** Neither is on the beeline. Both are real.
* **G is the cut line.** Office parity widens the citation feature from PDF to `.docx` and
  `.xlsx`. If 0.6.0 runs long, G moves to 0.7.0 and the release still delivers its headline.

**This sprint moves the client wheel, adds a migration, and adds a third distribution.** That
is a correction to this document rather than a change of mind: the sentence here previously
claimed zero contract changes and no DDL, scoped against the fifteen steps that existed before
Block F. Block F changes `jmfts-client/jmfts_client/contracts/view.py` and `search.py`, adds
`RENDERERS` values behind a migration, and adds `jmfts-web`. The claim was true when written
and Part 2 said a step needing either "has to come back here and say so"; this is that.

### Block A — the two access gates that do not close

`ROADMAP.md`'s "Subtree RBAC — remaining follow-ups" is the source, and both items are marked
CONFIRMED OPEN there against two separate re-checks, 2026-08-28 and 2026-09-13.

| Step | What | Entry condition | Size |
|---|---|---|---|
| 1 | Gate edge WRITE on the principal, at the repository and the service | A principal with read-only access to document *B* calls `POST /documents/{A}/links` with `target_id = B` and it succeeds | medium |
| 2 | Gate the four graph-analytics verbs on the principal | A principal with no grant calls `GET /graph/centrality` and reads back ids and titles of documents it cannot read | medium |
| 3 | A standing audit: every `@expose`d method either takes a principal or is on a list that says why not | The audit does not exist, so the next ungated verb is added the same way these two were | small |

**Step 1 is a write gate and the question of which document it checks was answered on
2026-09-13: write on the source, READ on the target.** Part 4 question 4.1 carries the
argument and the cost of the two answers not taken.

**What is there now, read 2026-09-13.** `DocumentService.create_link`
(`services/document_service.py:1287`–`:1310`) performs no access check of any kind. Its whole
validation is `if request.source_id != document_id: raise ValueError(...)`, and then
`DocumentRepository.create_link` (`repositories/document.py:1119`–`:1137`) constructs a
`DocumentLink`, adds it and flushes. Neither calls `can_read`, `can_write` or `require_write`.
`get_links`, the method directly below it (`:1320`–`:1334`), does call `can_read`, and its
comment records that `repo.get_links` "additionally hides edges pointing to unreadable other
endpoints". Reads are gated and writes are not, in adjacent methods of one class.

**`delete_link` has the same hole and joins this step.** `services/document_service.py:1355`
calls `repo.delete_link(link_id, incident_to=document_id)` and raises `LookupError` only when
nothing matched. The entry condition in the table names `POST` because that is where the gap
was found; the step closes both verbs.

**The machinery is already written and this step calls it.** `jmfts_core/access.py` has
`can_read` (`:153`), `can_write` (`:165`, "may modify `doc` / add children under it / reparent
it"), `require_write` (`:313`) and `require_add_child` (`:331`). The last is the closest
analogue and sets the house style: read failure is spelled as "does not exist" so the gate
leaks no existence, write failure is `AccessDeniedError` → 403.

**Ungoverned stays open and that is not a gap.** `can_write` returns True when no ACR governs
the node (`access.py:172`, "ungoverned → open"). Access control in JMFTS is opt-in; a gate
that closed by default would be a different product.

**The residue this step also decides: who may delete an asserted edge.** `DocumentLink.derived_by`
(migration 019, cited at `repositories/document.py:1143`) distinguishes an edge a rule produced
from one a principal asserted. Write on the asserting end where there is one; write on either
end where `derived_by IS NULL` and nothing recorded who asserted it.

**Step 1 also lands `ViewResponse.can_write`, and Block F is why.** No response reports the
caller's own access level today (`ran` 2026-09-13: grepped `contracts/view.py` and
`contracts/document.py` for `can_write`, `writable`, `permission` — no match). A document
detail page therefore cannot decide whether to render a link-creation control without firing
the call and reading a 403 back. The field is computed from the same `can_write` the gate
calls, so the page and the gate cannot disagree.

**Step 2's cost is not the check, it is where the check goes.** `get_neighbors` is gated
because `walk_neighbors` hides unreadable nodes *during* the walk, which is a filter inside
the traversal. The four analytics verbs call `build_graph` and then compute over the whole
result, so gating them means either filtering the graph before the metric runs — which
changes every score, because centrality is a property of the graph you computed it on — or
filtering the returned ids afterwards, which leaks nothing but reports numbers derived from
rows the caller cannot see. **These are different products and the plan does not get to
pretend otherwise.** Part 4 question 4.2.

**Step 3 is the reason the first two are not enough.** Both gaps were found by an audit, both
were re-confirmed twice, and neither was found by a test.
`tests/test_api_parity.py::test_registry_and_generated_routes_are_in_bijection` (`:43`, with
`REGISTRY` imported at `:23`) already reads the registry from a test and asserts a bijection
against the mounted routes; the audit this step adds reads the same registry and asks a
different question of it.
An entry on the "why not" list is a decision with a reason attached, which is the same shape
as `tests/test_readme_links.py::NOT_PUBLISHED`.

**Step 3 ran on 2026-09-13 and found eleven more gaps.** That is the step justifying itself
on its first execution, and it is also a problem: eleven is more than Block A can absorb
without becoming a different sprint. They are recorded in `tests/test_expose_principal_audit.py`
as `OPEN GAP` reasons, which is the honest holding position — the audit reports them, the list
says why each is unclosed, and the test does not go green by pretending otherwise.

**One of the eleven is step 1's defect on a second table and is promoted to a step.** Five
`TripleService` write verbs take a subject document and an object document and gate neither,
while the triple READ side is gated. That is the same shape as `create_link`, found the same
way, and its entry condition is writable without a measurement — so by Part 0's rule it is a
step rather than a gap. It becomes **step 35**.

| Step | What | Entry condition | Size |
|---|---|---|---|
| 35 | Gate triple WRITE on the principal, subject and object | A principal with read-only access to document *B* asserts a triple naming *B* and it succeeds | medium |

Same answer as 4.1, for the same reason: **write on the subject, read on the object.** A
triple is a directed edge between two documents and the argument does not change because the
table does.

**The other ten stay gaps.** `DocumentService.embed_document` misses the `require_write` at
`repositories/document.py:401`; `IngestService.file_frontier`; `TemplateService.get_template`
and `render_template`, where a template IS a document read by `session.get(Document, id)` with
no `can_read`; and five `IndexService` verbs, in a service that reaches `jmfts_core.access`
nowhere at all. Each is a candidate step for 0.7.0 and none is scope creep into step 3.

**The audit is a floor and not a proof, and the file says so.** Reachability into
`jmfts_core.access` is necessary, not sufficient: `TemplateService.list_templates` reaches
`require_add_child` because `_get_container_id` may create a container document — a real call
on a real gate that scopes nothing. The assertion is written only on the sound direction, that
no path implies certainly unscoped.

### Block E — the working record, republished

Runs second. The block letter is E because this document keeps the letters in the order the
blocks were written, and the schedule is the paragraph above rather than the alphabet.

| Step | What | Entry condition | Size |
|---|---|---|---|
| 4 | Review and publish the specifications shipped code cites: `INGEST_SPEC.md`, `SPRINT_JOBS.md`, `OFFICE_SPEC.md` | 387 citations from shipped files point at these three and resolve to nothing | medium |
| 5 | Review and publish the measurement records: `ANN_INDEX_HEALTH.md`, `STRESS_CORPUS.md`, `MEASURE_*.md`, `CORPUS.md` | 79 citations, and three of this sprint's own Part 4 questions cite them for their numbers | small |
| 6 | Decide, once, what happens to the sprint plans and the archive | 201 citations name `SPRINT_0_3_0.md`, `SPRINT_0_4_0.md` or `SPRINT_0_5_0.md`; this document adds more | small, decision-shaped |

**The counts are the argument and they are not stable.** 695 citations from files this
repository publishes name a document it does not carry. A reader who follows the explanation
layer of this code — which is where the *why* lives, deliberately, rather than in the code —
arrives nowhere 695 times. The README says the held-back list "is meant to shrink" and that a
document joins "when it has had a review pass rather than when a release runs"; from 0.5.1
there is no release-shaped copy step to hide behind, so the pass is the only thing left
between these files and the repository they are already cited from.

**What a review pass is, so the step is not open-ended.** Read for three things and only
three: a private host name or path, a named third party who has not agreed to be named, and a
claim about the tree that has gone false. It is not a rewrite and not a tidy-up. The documents
are the working record and they are allowed to read like one — the value in `INGEST_SPEC.md`
is that a comment can cite 5.3 and mean it.

**Step 6 was a decision, it was taken on 2026-09-13, and the answer is no.** The sprint plans
and `docs/archive/` stay internal. The reasoning is not that publishing them is uncomfortable:
it is that this plan already went past them. Everything still open out of `docs/SPRINT_0_4_0.md`
and `docs/SPRINT_0_5_0.md` is a numbered step here, which is what Part 1 verified and what the
"Where the scope came from" paragraph at the top of this document describes. A reader who could
open those two would find the same work, planned earlier and less well.

**The consequence, stated rather than discovered later: 201 citations stay dangling
permanently**, and the README's held-back list keeps three names it will never lose. That is a
real cost and it is now a decided one. `CLAUDE.md`'s rule still holds — **do not treat those
citations as broken links to fix, and do not delete them** — but the reason changes from "the
document is not published yet" to "the document is not published". Step 6 therefore becomes a
documentation step rather than a review pass: say so in the README's note and in `CLAUDE.md`,
once, so the next person does not re-open the question.

### Block B — retrieval that answers with nothing

| Step | What | Entry condition | Size |
|---|---|---|---|
| 7 | `index:bm25` reaches a workbook's `record` nodes | Ingest one workbook, search BM25 for a string in a cell, get nothing | medium |
| 8 | The margin on the MaxSim recall threshold | Public CI read `21.5/24` on 0.5.0 — one document genuinely short, with the ungated control at `0.9333` | medium |

**Step 7 has two independent exclusions and both have to move.** `after_any` on the
`index:bm25` row names `structure:declared`, `structure:inferred` and `structure:conversation`
and not `structure:sheets`; `requires=(HAS_TEXT_LAYER,)` is a PDF property no workbook can
satisfy. Either alone keeps the tasks from being created. The row's own comment says "exactly
one of the two rungs is ever eligible for a document" (`ingest_tasks.py:1160`–`:1162`), which is true
of the prose formats it was written about and false for a workbook, so the comment is part of
the fix.

**Step 7's real cost is what "the content" means for a sheet.** A `record` node's text is
labelled prose built by `sheet_tasks.py`, and a `cell` node's is one value. Indexing every
`cell` would put a corpus of bare numbers into the inverted index and move every IDF in the
index it joins. The narrow version — `record` nodes only — is what the measurement in
`docs/STRESS_CORPUS.md` 4.4 is about (4,601 `record` nodes averaging 457 characters) and is
what this step scopes. Part 4 question 4.3.

**Step 8 is the one step here whose entry condition is a run rather than a fixture, and that
is stated rather than hidden.** `tests/test_maxsim_recall.py` measures `recall@10` against an
exact scan over 24 queries and asserts the mean clears 0.90. Public CI read `21.5/24 = 0.8958`
on 0.5.0 with the ungated control at `0.9333`, which is one document short and genuinely
short — not the floating-point tie that 0.5.1 fixed. Twelve local runs at 0.5.1 read
`217/240` to `224/240`, two to eight documents above the failing reading. So the threshold is
inside the spread of the thing it measures, and the step is to find out which of three
accounts is true: the corpus is too small for a 1/240-resolution threshold, the ANN index is
genuinely losing a neighbour under some condition CI has and this machine does not, or 0.90 is
simply the wrong number for a 24-query fixture. **The step is not "raise the threshold until
it passes"**, and it is not "add retries": a test that is green because it was asked twice
measures nothing. Part 4 question 4.4.

### Block C — the office read path, resumed

**These are `docs/SPRINT_0_4_0.md` Block C steps 8 through 12, renumbered here and otherwise
unchanged.** That block was deferred out of 0.4.0 on 2026-09-05 with its measurements intact,
and its text is still the specification for these steps. The mapping, so nothing is ambiguous:

| Here | There | What |
|---|---|---|
| 9 | 0.4.0 step 8 | Scan rows 1–8 for the header; carry the header's row index as a measurement. Amends `INGEST_SPEC.md` 8.3 |
| 10 | 0.4.0 step 9 | A second pass allowing numeric headers: non-empty and distinct, any type. 8.3 |
| 11 | 0.4.0 step 10 | Emit a `table` node — the whole sheet as one markdown table — at the 8192 window. 8.4, 8.8 |
| 12 | 0.4.0 step 11 | Emit both shapes where both match, instead of first-match-wins. 8.4 |
| 13 | 0.4.0 step 12 | A column label for a headerless table, read from the banner row step 9 finds. 8.5 |

**Verified not started, 2026-09-13, by reading the tree rather than by trusting the
deferral.** `HEADER_ROW_NUMBER = 1` is still a module constant (`sheet_records.py:60`);
`_header_evidence` still requires the whole used extent to be text (`office/sheets.py:391`);
`rendered_markdown` is still built, counted, reported as a boolean and discarded
(`sheet_tasks.py:437`, `:442`, `:499`).

**What this block is worth, measured over 30,448 FUSE and 8,652 git-corpus sheets in
2026-09-03:**

| | FUSE (open web) | git corpora |
|---|---|---|
| today: 8.3's rule at row 1 only | 23.9% | 35.5% |
| step 9: the same rule, rows 1–8 | **45.5%** | **44.7%** |
| step 10: plus the numeric second pass | **54.4%** | **54.8%** |

`docs/SPRINT_0_4_0.md` Block C carries the rest: why three cheaper relaxations were measured
and rejected on ordering rather than on rate, why the window is 8192 and not 512, the
contested-population table that argues for emitting both shapes, and why the absolute
percentages are optimistic while the relative gains are not.

**This block's entry condition is a fire rate and not a failing test, which is why it left
0.4.0 and why it is fourth here.** Nothing is broken at 23.9% — the shipped rule does exactly
what `INGEST_SPEC.md` 8.3 says it does. One reading has arrived since the deferral that points
the same way from the appliance side: on a real 21-workbook ingest, 18 of 33 worksheets
produced zero `record` nodes (`docs/STRESS_CORPUS.md` 4.4b).

**Open question 4.3 of `docs/SPRINT_0_4_0.md` comes back with this block** — what rows 1 to
N−1 are, when the header is at row N. It is asked by step 9 and spent by step 13, and it stays
inside the block that pays for it.

### Block D — the job-system chain, the first two links

`docs/SPRINT_JOBS.md` Part 14 is the phase table. Six of seven phases have landed; these are
the next two, and they are the two that are unblocked.

| Step | What | Source | Size |
|---|---|---|---|
| 14 | Phase 5: multiplicity and the cost fold, so `EXPLAIN` says how much rather than only what | `SPRINT_JOBS.md` Part 7 | medium |
| 15 | Phase 4's leftover: the route and the ancestor walk for per-collection configuration | `SPRINT_JOBS.md` 5.1 | medium |

**Neither is blocked and both were deferred for the same reason twice.** `docs/SPRINT_0_5_0.md`
put them in a stretch section that would run only if Block A's step 5 came back cheap; it came
back at 57.5–59.0 s of drain at the bound, which is cheap by its own criterion, and the slot
went elsewhere. They compete on their own merits now rather than on a trigger.

**Step 15 is a route and a walk, not a scoping mechanism.** Phase 4's design moved
per-collection configuration out of phase 6 rather than into itself: a collection is a
subtree, so a setting on a node reaches the documents beneath it, and the ancestor walk that
does it already runs one level down in `rollup_tasks._options_owner`. RBAC, which is already
subtree-scoped, answers who may set it.

**One debt step 14 inherits and does not pay.** `Fact.locus` has no term for a write into
another tree and three shipped tasks write there. It is recorded at `docs/INGEST_SPEC.md` 5.3,
in `jmfts_core/atoms.py` and in the handlers' own declarations, so the phase that takes
`EXPLAIN` inherits a written statement of what it is not reporting. Step 14 should state what
it does not report rather than quietly report a number that is wrong by three tasks.

### Block F — the web front end

**The beeline: drag a PDF in, search it, click a result, see the page with the answer boxed on
it.** Sixteen steps in four phases. The phases are a dependency ordering, not a size ordering;
Part 5 turns them into worktrees and merge gates.

**A count, so the shape of the problem is not guessed at.** The appliance mounts 115
operations across 15 tags (`ran` 2026-09-13: imported `jmfts_core.rest.main:app` and walked
`app.routes`; 119 rows including `/docs`, `/redoc`, `/openapi.json` and the OAuth2 redirect).
One view per operation is 115 views. One view per tag is 15, and half of those are CRUD
tables. **Neither is the unit.** A capability page composes several tags into one workflow,
and there are twelve of them plus nine renderers and six shared components.

#### Phase F0 — the contracts

Nothing forks before these land. They are small, they are almost all declaration, and every
later step is written against them.

| Step | What | Contract | Size |
|---|---|---|---|
| 16 | `ExposeSpec` grows a response media type; `rest/wiring.py` honours it | IC-1 | small |
| 17 | Contract shapes for the three byte routes: path, params, media type | IC-2 | small |
| 18 | `ViewResponse.can_write`; a search result carries its `source_anchor` | IC-3, IC-4 | small |
| 19 | The `jmfts-web` distribution: layout, build, `[web]` extra, mount, release plumbing | IC-5 | medium |

**Step 16 is the one thing everything else waits on.** `ExposeSpec` carries `method`, `path`,
`response_model`, `errors`, `tags`, `summary` and `status_code` (`registry.py:109`–`:118`) and
has no way to say a response is not JSON. `rest/wiring.py:200` passes `response_model` and
nothing else. The tree has hit this once already and declined it: `GET /rdf/turtle` returns
Turtle inside JSON, and `services/rdf_service.py:19` gives the reason, which was a good reason
for that route and is not a reason for a PNG.

**Step 18's second half removes a round trip from the beeline.** A search result whose
`source_anchor` is absent forces the front end to call `GET /documents/{id}/evidence` per hit
before it can offer "show me where". The anchor shapes already exist and already reach the
API: `{"kind": "pdf", "page": 3, "bbox": [x0, y0, x1, y1]}` in PDF points
(`citation_tasks.py:6`, `:9`), `{"kind": "cells", "sheet": ..., "ref": "B4:H120"}`
(`services/document_service.py:141`), and `source_span` as a character range
(`atoms.py:191`). `source_anchor.unresolved` is its own evidence row, "present exactly when
`anchor` is not" (`evidence.py:453`), so a front end can tell "no highlight" from "the
highlight could not be recovered, and here is the reason" without inventing either.

**Step 19's distribution is a third wheel and the alternative was a flag that guards
nothing.** Static files inside `jmfts_core/web/` ship with the appliance whether or not an
extra names them, so `jmfts[web]` would be decoration. `jmfts-web` holds the bundle,
`jmfts[web]` depends on it, and the mount is conditional on the import succeeding.
`Dockerfile.worker` builds a worker that never serves a page and should not carry one.
Cost, stated: a third version in `bump-version.sh`, a step in `docs/RELEASING.md`, a third
case in `.github/workflows/publish.yml`. Part 4 question 4.8.

**One of those three could not be paid, and it is worth recording where it stopped.**
`bump-version.sh` and `publish.yml` are both in this repository and both were updated on
2026-09-13. `docs/RELEASING.md` is **not in this tree** — it is held back, and it is not in
Block E step 4's publish list either, which names `INGEST_SPEC.md`, `SPRINT_JOBS.md` and
`OFFICE_SPEC.md`. So the release runbook still describes two distributions and still carries
a step 3, "Replace the public tree", for a copy that stopped running at 0.5.1.

That is a defect in the runbook and it predates this sprint; step 19 only made it visible by
being the first change that needed to edit it. It is not a step here because the file cannot
be edited from the repository the work happens in, which is the held-back-documents cost
arriving in a concrete place rather than as a count of dangling citations. Part 4
question 4.10.

#### Phase F1 — bytes out, and the transport in

| Step | What | Contract | Size |
|---|---|---|---|
| 20 | `GET /documents/{id}/blob` — the original bytes, for re-hosting and download | IC-2 | small |
| 21 | `GET /documents/{id}/image` — one rendered page | IC-2 | medium |
| 22 | `GET /documents/{id}/region` — a rectangle, from an anchor or from explicit bounds | IC-2 | medium |
| 23 | The generated TypeScript client, and the call event every view emits | IC-6 | medium |

**Steps 20 to 22 are `docs/OFFICE_SPEC.md` Part 11 step 3, which that document scheduled
before any office format is read and which has not been built.** Its phasing table reads
"steps 1 to 3 deliver the headline feature end to end — a search result that can show you the
page and the rectangle it came from — using formats JMFTS already ingests, with no new
dependency of any tier". Steps 1 and 2 of that table are shipped: `ADVISORY_TASK_TYPES =
frozenset({"citation"})` (`models/task_queue.py:136`) and the `citation` handler
(`citation_tasks.py`, `TASK_CITATION`). Step 3 is not: neither path is in the route table.

**These three need no new dependency.** `pymupdf>=1.24` is at `pyproject.toml:88`, inside base
`dependencies` (line 32), not an extra. The bytes are already stored — `BlobRepository` keeps
them as Postgres large objects (`repositories/blob.py:1`) and `read_bytes` has exactly three
callers today, all task handlers (`citation_tasks.py:221`, `conversation_tasks.py:81`,
`ingest_tasks.py:2522`). Nothing under `jmfts_core/rest/` reads a blob.

**What steps 20 to 22 did NOT build, recorded 2026-09-13 when they landed.** `OFFICE_SPEC.md`
Part 7 sketches three things beyond the brief these steps were given, and all three are
deliberate omissions rather than oversights:

* **a `highlight` parameter on `/image`.** The overlay is IC-9 and it is client-side, drawn
  over the same anchor IC-4 puts on the search hit. Burning a rectangle into a rendered PNG
  makes the highlight uncacheable and un-dismissable, and the page already has the geometry.
* **the rectangle in points returned beside `/region`'s PNG.** `BinaryPayload` is bytes and
  their type. The coordinates are the `source_anchor` row, which `/evidence` already serves
  and which the hit already carries.
* **a text span within a node's content** — a query-time `search_for` scoped to that node's
  rectangle. This one is a real capability and it is simply not in this sprint. It is the
  recovery mechanism office formats need (Part 5's "How a rectangle is recovered" searches a
  rendition), so it belongs with Block G step 33 rather than here.

**Step 22 serves a spreadsheet region as cells, not as an image.** `_cells_bounds`
(`services/document_service.py:154`) already resolves a rectangle three ways in the spec's
order — what the caller named, the node's own anchor, the measured used range — and
`GET /documents/{id}/cells` already serves it. For a sheet, JSON cells are the better answer
than a picture of cells, and this step is a renderer decision rather than a second route.

**Step 23 is the step that makes "thinly wraps the API" checkable rather than claimed.** Every
view calls one generated TypeScript client, generated from `/openapi.json` the way
`jmfts-client/jmfts_client/_verbs.py` is generated from the route table
(`scripts/generate_client.py`, with `tests/test_client_codegen.py` refusing a stale one). The
client emits one event per call — operation id, method, resolved path, path params, query,
body, status, response, elapsed ms. Three properties follow, and the third is the one that was
asked for:

1. **Replay.** An event is a call plus its arguments, so it is re-sendable with edited
   arguments and the response renders through the right visualisation instead of as a blob.
   That is `/docs`' "Try it out" with the result half fixed.
2. **Verifiable thinness.** A view that shows something the log has no call for computed it
   client-side. That is readable from the log rather than arguable from the source.
3. **Copy-out.** An operation id maps to a `_verbs.py` method name, so an event prints as
   `curl` with the token elided, as a `RemoteJmftsClient` call, and as `fetch`. Worked
   examples stop being a thing anybody writes by hand.

The generator runs against the 115 operations that exist. It picks up steps 20 to 22 when they
merge, which is why this step does not wait on them.

#### Phase F2 — the shell, and the two interfaces the views plug into

Sequential and small. It exists because three views written in three worktrees conflict in one
router file, and the fix is a registry rather than a merge policy.

| Step | What | Contract | Size |
|---|---|---|---|
| 24 | The view registry, the shell, navigation, token entry | IC-7 | medium |
| 25 | The renderer interface and the highlight-overlay interface | IC-8, IC-9 | small |

**Step 24 mirrors `registry.py` on the client side.** One module per view, each registering
itself; the manifest is append-only. `@expose` is the precedent and the reason is the same one
`registry.py:1` gives — there is no hand-written second definition to drift, and here there is
additionally no shared file for two parallel worktrees to fight over.

**Step 25's renderer interface is a dispatch table over server state, which is the strongest
thing in the tree for this.** `RENDERERS = ("markdown", "code", "json-table", "transcript",
"plain")` (`models/usetype_presentation.py:14`), with `CHILD_HANDLINGS` and `LINK_HANDLINGS`
beside it; `UsetypePresentation` rows are per-usetype and CRUD-able over
`/usetype-presentations/`; and `GET /view/{document_id}` returns a `ViewPresentation` with the
content (`services/view_service.py:1`). So the document viewer dispatches on a value the
database hands it, and adding a renderer is a tuple edit plus a migration that every client
picks up — not a branch in JavaScript that only this front end has.

**The overlay is one component and not three.** It takes a `source_anchor` and a rendered
surface and draws the box. `pdf-page`, `image` and `sheet-region` all use it. Writing it three
times is how the three end up disagreeing about which corner the origin is in.

#### Phase F3 — the views

Five worktrees, no shared file, because of step 24.

| Step | What | Composes | Size |
|---|---|---|---|
| 26 | The upload view: drag and drop, `EXPLAIN`, the settling frontier | `/ingest`, `/ingest/file`, `/ingest/analyze`, `/ingest/explain`, `/ingest/pipelines`, `/ingest/file/{id}/frontier` | medium |
| 27 | The search view: the seven methods side by side, contexts, synthesis | all `/search/*`, `/search-contexts/*` | medium |
| 28 | The tree browser and the document detail view | `/documents/*`, `/view/*` | large |
| 29 | The renderers: `pdf-page`, `image`, `sheet-region`, `office`, and the five that exist | `/documents/{id}/image`, `/region`, `/cells`, `/blob` | large |
| 30 | The call-log view | step 23's event stream | small |

**Step 27's side-by-side is the demonstration, not a debug affordance.** The same query run
through `vector`, `bm25`, `hybrid` and `maxsim` in four columns is the shortest explanation of
what this appliance is that anybody has written, and it is four calls the log shows.

**Step 29's `office` renderer falls back to extracted markdown when no rendition exists, and
says so on the page.** `jmfts_core/office/extract.py` converts `docx` and `pptx` to markdown
already. A page that silently shows markdown where it showed a rendered page for the document
next to it is the failure this step is written to avoid; the renderer states which of the two
it is showing.

#### Phase F4 — the beeline closes

| Step | What | Size |
|---|---|---|
| 31 | A search result carries its anchor, the detail view renders the page, the overlay draws the box | medium |

**This is one step because it is integration and it is where the contracts get tested against
each other.** Its entry condition is a demonstration rather than a failing test: upload a PDF,
search it, click a hit, and see the page with the rectangle on it. Nothing before this step
proves the four contracts agree, and nothing after it is worth doing if they do not.

### Block G — office parity, and the cut line

**Widens Block F's citation feature from PDF to `.docx`, `.xlsx` and the legacy binaries.**
`docs/OFFICE_SPEC.md` Part 11 steps 7, 8 and 9, unchanged.

| Step | What | OFFICE_SPEC | Size |
|---|---|---|---|
| 32 | `render:pdf` and `convert:ooxml`; the badged LibreOffice worker image | Part 11 step 7 | medium |
| 33 | `citation` for office: recovery against the rendition | Part 11 step 8 | medium |
| 34 | Source anchors for office extraction | Part 11 step 9 | medium |

**Verified not started, 2026-09-13, by reading the tree.** No `render:pdf` or `convert:ooxml`
task type exists (`ran`: grepped every `TASK_*` constant in `ingest_tasks.py` and both string
literals). `pyproject.toml:276` says "No code imports this yet" of the `convert` extra, and
names Part 11 step 7 as where it lands. `office/extract.py` writes no anchor.

**The design is already written down and this block executes it.** `pyproject.toml:268`:
"render any document to PDF once, at ingest, so that serving a citation image is `pymupdf`
over a stored rendition and needs NO tier-3 dependency on the query path." That is what makes
step 33 cheap — it is the `citation` handler that already exists, pointed at a rendition.

**`OFFICE_SPEC.md` Part 12 question 1 is answered here rather than carried.** That question
proposed renditions off by default because they "roughly double blob storage for office
documents". The claim is arithmetically true and measures the wrong denominator.
`docs/STRESS_CORPUS.md` 2.5, 877 files at commit `1440535`: 151 MB of source became 2988 MB of
database, of which the original bytes are **144 MB across 852 blobs — 4.8%**. `token_embeddings`
alone is 1672 MB, 56% of the total, half of that the ANN index. Upper bound if every stored
byte were an office document and every rendition matched its original: 2988 → 3132 MB, +4.8%.
A rendition is also **derived** and regenerable by re-running `render:pdf`, which the original
is not — so the policy is cache eviction, not retention. **Renditions on, always, evictable by
a sweeper, regenerated on read.**

**This block is the cut line.** If 0.6.0 runs long, G moves to 0.7.0. Block F delivers the
headline for PDF, which is the format the stress corpus is mostly made of, and G widens it.

---

## Part 3 — Not in this sprint

Named so that a reader who finds one of these elsewhere knows it was scoped out rather than
missed.

| What | Where it went | Why |
|---|---|---|
| **`SPRINT_JOBS.md` phases 6 and 7** | later | Sequential behind steps 14 and 15, and phase 7 deletes the rollup special case, which is a change to a path every ingest runs. Two links of a chain is a sprint; four is a rewrite. |
| **The component query: JSONB filter, census, projection** | unscheduled | The projection's shape is undecided — `docs/SPRINT_0_4_0.md` 4.1 — and shipping the filter without it delivers a capability with no consumer. `jmfts_core/effective_content.py` supplied a worked precedent for the cost side (4.9 ms for a 100-container page) and decided none of the shape. |
| **Entity resolution beyond string similarity** — `ROADMAP.md` gap 2 | blocked | Owned by `docs/SPRINT_0_3_0.md` step 10, which needs a calibration corpus that does not exist. |
| **Acquiring real Excel-saved workbooks** | **starts now, lands later** | Procurement, not engineering. It blocks the calibration chain and no engineering hour shortens it. Block C above does not wait on it: the corpus is only larger whenever it arrives. |
| **`POST /documents/batch`** — `ROADMAP.md` gap 4 | re-examine, do not build | The queued spine accepts a file and fans out from it, so the case a batch endpoint was for may have moved. Confirmed absent 2026-09-13; that is not the same as confirmed wanted. |
| **Seeding a named search-context preset** — `ROADMAP.md` gap 5 residue | gated on 4.6 | Two of the three presets name namespaces nothing writes. The third needs a filter key `SearchContext.config` does not have, and carrying any preset to an existing appliance needs a migration nobody has written. |
| **Does tree agreement carry information?** | unscheduled, and this is the uncomfortable one | `docs/SPRINT_0_5_0.md` 3.1 calls it the load-bearing empirical claim in that document, four of the eight reprojection trees are gated on it, the second tree it needs now exists, and nothing has been scheduled to take the reading. It is not in this sprint because it is a measurement whose result would reshape a later sprint, and taking it inside a sprint it cannot change is the wrong order. |
| OWL 2 RL, SPARQL via Ontop, FerretDB, a free path language, named graphs as a column, a corpus object | gated | Each gate is written in `ROADMAP.md`'s "Deferred, and what gates each" and none has opened. |

---

## Part 4 — Open questions

Each states what the answer changes and what happens absent an answer. **Four were answered on
2026-09-13 and are kept here with their answers rather than deleted**, because a question that
was live is part of the record of why the step is shaped the way it is: 4.1, 4.8, 4.9, and
`OFFICE_SPEC.md` Part 12 question 1, which Block G answers in place.

### 4.1 Which end of an edge does a write gate check — ANSWERED 2026-09-13

**Write on the source, read on the target.** The argument below is what was weighed; Block A
step 1 carries what the code does about it.

Step 1. An edge has a source and a target, and gating on the source alone closes nothing that
the URL does not already imply.

Three candidates. **Write on both ends** is the narrowest and refuses a legitimate citation of
a document you may read and not modify. **Write on source, read on target** refuses the leak —
attaching an edge to a document you cannot see — while permitting citation. **Write on source
only** is the status quo with a check bolted on and closes nothing.

What the answer changes: whether `DocumentLink` can express "this document of mine cites that
document of yours" across an access boundary, which is a product question about what the graph
is for.

**Answered: write on source and read on target**, because it is the one that makes the
existing READ gate on edges (stage 4 of subtree RBAC) coherent: hiding an edge from a reader
while letting that same reader create it is the inconsistency, not the permission.

**Write on source only was rejected on a concrete leak, not on principle.** A principal may
then create an edge to a document it cannot read at all, and that row reaches
`/graph/neighbors`, `/graph/centrality` and `/graph/spines` — edge injection into a graph the
injecting principal cannot see. The disclosure half in the other direction is already closed:
`repo.get_links` hides edges whose far endpoint the reader cannot read
(`services/document_service.py:1330`), so a target's readers do not learn the source exists.

### 4.2 Do the analytics verbs filter the graph or filter the result

Step 2. `get_neighbors` filters during the walk. The four analytics verbs compute a metric
over a graph and then return ids.

Filtering the graph before the metric runs is the honest one — a centrality score is a
property of the graph it was computed on, and a score computed over documents the caller
cannot see is a number about somebody else's corpus. It is also more expensive and it changes
every number every existing caller reads. Filtering the returned ids leaks nothing and reports
numbers derived from invisible rows.

What the answer changes: whether these verbs are analytics *about the caller's corpus* or
analytics about the appliance that the caller is shown a slice of. Those are different
products.

**Absent an answer, filter the graph**, and accept that every score moves — with the change
recorded in `CHANGELOG.md` as a behaviour break rather than a fix, because for an ungoverned
single-principal deployment nothing moves at all and for a governed one the old numbers were
never the caller's to read.

### 4.3 Does a `cell` node reach BM25, or only a `record` node

Step 7. `record` nodes carry labelled prose. `cell` nodes carry one value each, and `39d5f20`
made `cell` children of a `record` for rows too long to embed.

Indexing every `cell` puts a corpus of bare values into the inverted index and moves every IDF
in the index it joins — and this codebase has measured ranking changes going wrong twice.

What the answer changes: whether a search for a distinctive single value in a wide row finds
it. That is a real retrieval case for a spreadsheet and it is exactly the case `record`-only
does not reach.

**Absent an answer, `record` nodes only**, because that is the population
`docs/STRESS_CORPUS.md` 4.4 measured and the only one this step has a number for.

### 4.4 What is the MaxSim recall threshold a threshold on

Step 8. The fixture runs 24 queries, so every achievable mean is a multiple of 1/240 and the
threshold sits one or two of those steps above the reading that failed public CI.

Three accounts and they need different work. If the **corpus is too small**, the fix is more
queries and the threshold stays; that is a slower test and a better one. If the **ANN index
is genuinely losing a neighbour** under a condition CI has and this machine does not, the fix
is in `jmfts_core` and the test is doing its job. If **0.90 is the wrong number** for what
this fixture can measure, the fix is a threshold derived from the fixture rather than chosen.

What the answer changes: whether `v0.5.1` can go red on public CI on real grounds, which it
can today.

**Absent an answer, measure before changing anything** — run the fixture enough times on the
CI image to get a distribution rather than a reading, which is the one thing nobody has done.
**A retry is not an answer** and neither is a threshold moved to fit the last failure.

### 4.5 What happens to the sprint plans and the archive — ANSWERED 2026-09-13

**They stay internal. Publish the specifications and the measurement records; do not publish
the sprint plans or `docs/archive/`.** The argument below is what was weighed.

Step 6. 201 citations from shipped files name `SPRINT_0_3_0.md`, `SPRINT_0_4_0.md` or
`SPRINT_0_5_0.md`, and this document adds more.

A shipped sprint plan is a record of what was decided and why, including what was wrong when
written and corrected in place. `docs/SPRINT_0_4_0.md` Block C is the clearest case: it is the
only surviving record of a measurement over 39,100 sheets, and it is attached to a block that
was deferred.

What the answer changes: whether the 695 citations become links or stay as names, and whether
this repository is the working record or a publication of it.

**Answered: publish the specifications and the measurements (steps 4 and 5) and hold the
sprint plans**, because the first two are about the appliance and the third is about how the
work was run. The deciding argument was not the one above about discomfort. It is that 0.6.0
is already past both plans: what is unfinished in them is a numbered step in this document, so
publishing them would hand a reader an earlier, worse copy of a plan they can already read.
`docs/SPRINT_0_4_0.md` Block C's 39,100-sheet measurement is the one real loss, and it is
quoted in Block C above — the numbers survive in a published document even though the argument
around them does not.

**This decision is one-way and that was known when it was taken.** Publishing later is always
available; unpublishing is not.

### 4.6 Is a seeded preset worth a migration

`ROADMAP.md` gap 5. A seed in `schema.sql` reaches a freshly built database and no existing
one.

What the answer changes: whether "named search contexts" is a feature with examples or a
mechanism with none. It has been a mechanism with none since it shipped.

**Absent an answer, nothing is seeded**, which is where 0.4.0's step 14 left it deliberately,
and `tests/test_search_context_presets.py` keeps asserting the table is empty so the absence
stays a decision rather than an oversight.

### 4.7 The three gaps that are decisions and not steps

Carried here whole so they are not lost between plans. None is in Part 2.

* **Gap 7 — the dead-entry residue.** `strict_order` holds recall at 0.979 under 500 dead
  entries per query; at that dose 3 queries in 100 were still degraded and one returned
  nothing. `docs/ANN_INDEX_HEALTH.md` Part 2 lays out five options with their costs and picks
  none, because the choice depends on delete frequency and on whether a miss is a nuisance or
  a correctness failure. **Absent an answer, nothing changes**, and that is a real position:
  the appliance already runs the strongest mitigation measured.
* **Gap 10 — the task queue cannot say which resource a rung needs.** A per-rung concurrency
  cap, a resource class on `TaskRow` beside `write_mode`, or a narrower lock in
  `index_document` are three different answers with three different blast radii.
  **Absent an answer, an operator keeps writing `UPDATE task_queue SET priority` by hand**,
  which is what happened twice in one run.
* **Gap 11 — the summary tree stores 13,755 duplicate vectors.** Either a derived node carries
  no embedding and is reached through `source_node_id`, or the duplication stays and
  `search_exclude_usetypes` is documented as required rather than default. **Absent an answer,
  the second**, because it is what ships today and the exclusion being load-bearing is a
  documentation defect rather than a storage one.

### 4.8 Does the front end ship as a third distribution — ANSWERED 2026-09-13

**Yes: `jmfts-web`, with `jmfts[web]` depending on it.** Step 19.

An extra guards dependencies. Static files inside `jmfts_core/web/` ship with the appliance
whether or not an extra names them, so `jmfts[web]` over a bundle in the main wheel would be a
flag that guards nothing — which is the kind of thing this document exists to refuse.

What the answer changes: whether a headless worker carries a UI bundle it will never serve.
`Dockerfile.worker` is a real deployment, not a hypothetical, and `Dockerfile.worker:107`
records that an un-badged worker claims everything.

Cost, paid knowingly: a third version in `bump-version.sh` (which sets three copies today), a
step in `docs/RELEASING.md`, and a third case in `.github/workflows/publish.yml`. This tree
already builds two distributions from one source, so a third is a new entry rather than a new
concept.

### 4.9 Which LibreOffice does the badged image pin — ANSWERED 2026-09-13

**26.8.0.** `OFFICE_SPEC.md` Part 12 question 3 asked this and proposed "a pinned container
image" without a version, on the grounds that pagination differences between versions are what
make a stored rendition stable. Block G step 32 needs the number.

Two branches are live (`ran` 2026-09-13, endoflife.date):

| Branch | Latest | Released | Support ends |
|---|---|---|---|
| Fresh — 26.8 | 26.8.0 | 2026-08-26 | 2027-06-13 |
| Still — 26.2 | 26.2.5 | 2026-02-04 | 2026-11-30 |

**Fresh, despite Still being the conservative branch.** The reason to prefer Still is fewer
regressions in a deployment that takes updates, and this image takes none inside a release. The
deciding factor is the support window: 26.2 reaches end of life on 2026-11-30, plausibly before
0.6.0 tags, so pinning it means shipping a component with no security updates on its first day.

**The version is recorded on the rendition** — a `renderer_version` key in its evidence row —
and an upgrade re-renders on read rather than invalidating in bulk. That is the rest of Part 12
question 3, answered with it.

---

## Part 5 — The workflow

**Not a procedure to follow top to bottom.** It is a dependency graph with merge gates, written
so that parallel work has something to be parallel against. Four rules hold it together:

1. **A contract merges before anything forks against it.** Every interface below is a numbered
   task in Part 2 whose deliverable is the shape, not the implementation.
2. **A worktree owns files, not features.** Two worktrees that edit one file are a merge
   conflict scheduled in advance; where that is unavoidable the plan says so and orders them.
3. **At every gate, everything open merges.** Not just the branches the next phase needs. A
   branch held across two gates is a branch whose conflicts compound.
4. **Sequential phases are when the tree gets clean.** F0, F2 and F4 are narrow by
   construction, so they are where every outstanding worktree lands and conflicts get resolved
   at the first opportunity rather than the last.

The integration branch is `feature/web`. It merges to `master` at each gate, per the standing
practice that `master` is a working branch and version tags are what guarantee stability.

### 5.1 The interface contracts

Each is the deliverable of a Part 2 step, and each is pinned by a test so that "the contract
changed" is a red suite rather than a conversation.

| | Contract | Step | Shape | Pinned by |
|---|---|---|---|---|
| IC-1 | Response media type | 16 | `ExposeSpec.media_type: Optional[str]`; `wiring.py` sets `response_class` from it | a route declaring `image/png` returns non-JSON |
| IC-2 | The three byte routes | 17 | `GET /documents/{id}/blob`; `/image?page=N&dpi=D`; `/region?page=N&bbox=x0,y0,x1,y1` or `?anchor=true` | `test_api_parity` bijection; the generated client carries them |
| IC-3 | Caller access on a view | 18 | `ViewResponse.can_write: bool`, from `access.can_write` | a read-only principal reads `false` |
| IC-4 | Anchor on a search hit | 18 | `source_anchor: Optional[dict]` and `source_anchor_unresolved: Optional[dict]`, verbatim from the evidence rows | a hit on an anchored chunk carries the box |
| IC-5 | The mount | 19 | `jmfts-web` exports a package directory; the appliance mounts it when the import succeeds and runs without it when it does not | base install starts with no `jmfts-web` present |
| IC-6 | The call event | 23 | `{op_id, method, path, path_params, query, body, status, response, ms}` | the generator emits one event per operation |
| IC-7 | A view registration | 24 | `{id, title, path, component, tags}`, one module per view, append-only manifest | two views added in two worktrees merge without conflict |
| IC-8 | A renderer | 25 | `(content, presentation, document) → element`, keyed by `RENDERERS` | every value in `RENDERERS` has exactly one renderer |
| IC-9 | The highlight overlay | 25 | `(source_anchor, surface) → box`, one component for `pdf-page`, `image` and `sheet-region` | one anchor renders identically on all three |

**IC-2 and IC-4 are the two that carry the beeline**, and they are written in different steps
on purpose: the route can exist before a search hit knows to point at it, and a hit that
carries an anchor is useful in the log before there is a page to draw it on.

### 5.2 The gates

```
master
  │
  ├─ M0 ── F0: contracts and scaffolding ──────────────────── SEQUENTIAL
  │        ├─ wt/contracts   steps 16, 17, 18   registry, wiring, contracts, regen _verbs
  │        └─ wt/dist        step 19            jmfts-web, pyproject, bump, RELEASING, publish
  │        (two worktrees, disjoint file sets, both merge at M0)
  │
  ├─ M1 ── F1 + the first ride-alongs ─────────────────────── SEVEN WORKTREES
  │        ├─ wt/bytes       steps 20, 21, 22   document_service, new jmfts_core/rendering.py
  │        ├─ wt/tsclient    step 23            scripts/, jmfts-web/src/client/
  │        ├─ wt/access      steps 1, 2, 3      access.py, document_service, graph_service
  │        ├─ wt/sheets      steps 9–13         sheet_records, office/sheets, sheet_tasks
  │        ├─ wt/bm25        step 7             ingest_tasks rows, repositories/search
  │        └─ (wt/dist and wt/contracts are closed; they merged at M0)
  │
  ├─ M2 ── F2: the shell ──────────────────────────────────── SEQUENTIAL
  │        └─ wt/shell       steps 24, 25       jmfts-web only
  │        EVERYTHING OPEN MERGES HERE. This is the first opportunity and the rule is
  │        rule 3: wt/access, wt/sheets and wt/bm25 land now even if they are done early.
  │
  ├─ M3 ── F3 + the second ride-alongs ────────────────────── SEVEN WORKTREES
  │        ├─ wt/view-upload   step 26
  │        ├─ wt/view-search   step 27
  │        ├─ wt/view-tree     step 28
  │        ├─ wt/renderers     step 29
  │        ├─ wt/view-log      step 30
  │        ├─ wt/jobs          steps 14, 15     SPRINT_JOBS phases 5 and 4-residue
  │        └─ wt/docs          steps 4, 5, 6    the republication review pass
  │
  ├─ M4 ── F4: the beeline closes ─────────────────────────── SEQUENTIAL
  │        └─ wt/beeline       step 31
  │        EVERYTHING OPEN MERGES HERE, same rule.
  │
  ├─ M5 ── G: office parity ───────────────────────────────── THREE WORKTREES
  │        ├─ wt/render-pdf    step 32
  │        ├─ wt/cite-office   step 33  (forks after wt/render-pdf merges)
  │        └─ wt/anchor-office step 34
  │
  └─ M6 ── pre-tag: step 8, polish, CHANGELOG, bump, tag v0.6.0
```

### 5.3 The conflicts this schedule is shaped around

| Where | Who collides | How it is handled |
|---|---|---|
| `_verbs.py` | every step that adds a route | Regenerated, never edited. One worktree (`wt/contracts`) regenerates at M0; `wt/bytes` regenerates once at M1. Two worktrees regenerating in parallel is a guaranteed conflict in a 1500-line generated file |
| `jmfts-web/.../client/verbs.d.ts` | the same steps, from 2026-09-13 | **A branch that adds a route now regenerates TWO clients.** Step 23 landed a TypeScript client generated from the same surface, and `verbs.d.ts` is the largest generated file in the tree. The `_verbs.py` rule above extends to it unchanged: one worktree regenerates per gate |
| `services/document_service.py` | `wt/bytes` (steps 20–22) and `wt/access` (step 1) | Different regions of a 1400-line file — `wt/bytes` appends a Links-adjacent section, `wt/access` edits `create_link`/`delete_link` in place. Usually a clean merge; `wt/access` merges first at M1 because its change is smaller and its tests are cheaper to re-run |
| The client router | every view step | Removed as a conflict by IC-7. This is the whole reason F2 is a sequential phase rather than a file three worktrees share |
| `pyproject.toml` | `wt/dist` and anything adding a dependency | `wt/dist` owns it through M0 and nothing else in F0 touches it. After M0 the only step that adds a dependency is 32, in G |
| `docs/SPRINT_0_6_0.md` | any worktree correcting its own step | Corrections land at a gate, in the merge commit, not inside a feature worktree. This document is published, so a half-corrected step is visible to everybody |

### 5.4 What runs the suite, and when

**The full suite runs at every gate and not inside a worktree.** `JMFTS_CI_PG_PORT=<port>
./scripts/run_tests_docker.sh` reads `2480 passed, 40 skipped in 440.04s` on `master` at
`db30087` (`ran` 2026-09-13), and that is the baseline a gate compares against. Inside a
worktree, run the files the change touches. `JMFTS_CI_PG_PORT` moves both the port and the
container name, so parallel worktrees can each hold a database without colliding.

**`black --line-length 100 --check jmfts_core jmfts-client tests` and
`ruff check jmfts_core jmfts-client tests` run before every merge**, because `.githooks/pre-commit`
and `.github/workflows/ci.yml` run the same three paths and the three cannot be allowed to
disagree. `jmfts-web/` is outside that gate and carries its own.

### 4.10 `docs/RELEASING.md` describes a release that no longer happens — ANSWERED 2026-09-13

**Publish it under Block E step 4, and split it in two.** The rules that outlive any one
release live in a `release` skill; the commands, the version locations, the distribution list
and the remote names stay in `docs/RELEASING.md`. That split is the τ monorepo's
(`agent-harness-py`), it is the reason its runbook survived the same pivot, and the skill was
ported to this repository on 2026-09-13 — it carries the two-remote shape, the three
distributions, the pending-publisher trap, and a statement that until the runbook is fixed the
skill is the more current document and its step 3 does not happen.

Two things the port found, neither of which is this sprint's to fix and both of which are now
written down where a release will meet them:

* **The leakage scan had an unwalked surface.** `tests/test_no_host_addresses.py::SHIPPED` did
  not list `ROADMAP.md`, `CHANGELOG.md`, `docs/` or `jmfts-web/` — all four published, the
  first on 2026-09-13, and it had carried a private path and a host reference that were cut by
  hand on the way out. Nothing would have caught a third. Widened the same day; the scan still
  reads clean.
* **A test queries the repository it runs in.** `_tracked` shells out to `git ls-files` and
  says so in its own docstring. There is no `git archive`-based gate here yet, so it has not
  bitten; τ paid for that rule and this repository has the debt without the bill.

The argument that is being retired: "process, not specification" kept the runbook internal, and
it stopped being true when the process moved into the repository the work happens in.

Found while doing step 19, on 2026-09-13, and it is not that step's to fix.

The runbook is held back, so it is not in this repository and cannot be edited from it. Read
from the internal history at `oldmaster` it still says "**The two distributions release in
lockstep: one number, two wheels, one tag**" at its step 1, and still carries a step 3,
"Replace the public tree", for the squash-and-copy that stopped running when development moved
here at 0.5.1. Step 19 adds a third distribution, so both statements are now wrong in a way
that would mislead somebody cutting 0.6.0.

It is cited 11 times from shipped files and it is **not** in Block E step 4's list, which
names `INGEST_SPEC.md`, `SPRINT_JOBS.md` and `OFFICE_SPEC.md` — the three with 387 citations
between them. `RELEASING.md` was left out because it is process rather than specification, and
that reasoning held right up until the process itself became something a contributor in this
repository has to follow and cannot read.

What the answer changes: whether cutting 0.6.0 means following a document that describes a
different repository layout. Three candidates.

* **Publish it, as part of step 4.** One more name on the list, and it becomes editable by
  whoever is doing the release. It carries a "two repositories" section describing an internal
  remote, which is the review pass's job to catch — the pass is already defined as reading for
  exactly that.
* **Rewrite it here as a new file** and leave the old one internal. Cleanest content, and it
  splits a document that is cited by anchor from the anchors, which is the thing `CLAUDE.md`
  says not to do.
* **Leave it, and correct it in the internal tree by hand at release time.** Free, and it is
  how this defect got here.

**Absent an answer, the first**, because the argument that kept it internal — process, not
specification — stopped being true when the process moved. A runbook nobody following it can
open is not being held back, it is being lost.
