# Office Formats: Ingest, Render, Modify, Generate

Companion to `INGEST_SPEC.md`. Written 2026-08-21.

**Reviewed for publication 2026-09-15** (`SPRINT_0_6_0.md` Block E step 4). This document
was written alongside code that has since moved, and the review pass corrected the claims
about the tree that had gone false rather than rewriting the design. Every correction is
marked where it sits, in the form the rest of this document already uses — the original
sentence stays, because roughly 161 source comments cite this file by part and section (read
2026-10-12; 154 on 2026-09-15) and
a deleted paragraph is a citation that resolves to nothing.

**Part 6's `file.rendition_ref` is the one correction here that is not merely stale — it was
wrong when it was written**, because it contradicts `INGEST_SPEC.md` 3.3. The rest are
things the tree has since done: Part 0's broken sentence and its "Still open" list
(worksheets and office `citation` both shipped, ordered lists and unread parts did not),
Part 0's corpus-coverage count, Part 1's base-install figure, Part 6's "off by default",
Part 7's three verbs (all shipped), Part 11's phasing table (steps 0-9 shipped, 10-12 did
not), and Part 12's questions 1 and 3, both since answered.

`INGEST_SPEC.md` Part 10 lists step 6 as "Office formats. `.docx`, `.pptx`,
`.xlsx`. Part 8." That step is half of what this document plans. The other half
— rendering a page, addressing a region, modifying a document in the tree, and
generating a new document from a template — is not in that spec at all, because
until now JMFTS has only ever *read* files.

That asymmetry is the reason for a separate document. Reading a format is one
task row and one handler. Writing one is a different contract with the user: a
file JMFTS emits carries our name, opens in somebody's Word, and is wrong in
ways no test of ours would notice unless we build for it deliberately.

---

## Part 0 — What already exists

Four facts about the current tree that this plan is built on. Each was verified
in the code, not assumed.

**Format detection is done.** `probe.detect_format` already identifies `docx`,
`pptx`, `xlsx` and `epub` from the ZIP member list, and `ole2` from magic bytes
(`jmfts_core/probe.py`, `_ZIP_MEMBERS` and `_MAGIC`). It reports
`detected_by="zip_manifest"` so the evidence is auditable. Nothing here needs
changing.

**The enqueue conditions are already written.** `DECLARED_STRUCTURE_PATTERN` in
`jmfts_core/ingest_tasks.py` already maps `docx → has_heading_styles`,
`pptx → has_slides`, `xlsx → has_sheets`. Those patterns are simply never
reported, because `PROBERS_AVAILABLE` is `("pdf", "text")`. The scheduling
decision for office formats exists and evaluates to false. Making it true is a
prober, not a scheduler change.

**The original bytes are retained.** `BlobRepository` stores every uploaded file
as a Postgres large object, with `file.blob_ref` holding the OID
(`INGEST_SPEC.md` Part 9). Every write-side feature in this document depends on
that, and it is already shipped.

**No positional anchor survives extraction.** A chunk node's evidence carries `rung`,
`section_title`, `section_level`,
`chunk_index` and `source_line` (`structure_tasks.py`). `source_line` is a line
offset **into the extracted markdown**, not into the source document.
`pdf_extraction.py` computes bounding boxes and uses them to order blocks and
own tables, then discards them.

> *Sentence repaired 2026-09-15.* Phase 2b (`SPRINT_JOBS.md` 14) rewrote this paragraph's
> reference to `structured_content` into "Its evidence carries" and left the first half of
> the previous sentence orphaned, so it read "A chunk node's" followed by a new sentence.
> Nothing about the fact changed; only the sentence was broken.

**`.docx` and `.pptx` now ingest.** `PROBERS_AVAILABLE` is
`("pdf", "text", "docx", "pptx", "xlsx", "ole2")`, and the probers report real patterns
from the ZIP directory alone — `has_slides` with a `slide_count`, `has_sheets` with
`sheet_names`, `has_macros`, plus `unknown_parts`, which names a zip-slip member verbatim.
They need no office library and sit in the base install.

`TEXT_EXTRACTORS` now holds `{"pdf", "text", "docx", "pptx"}`. `jmfts_core/office/extract.py`
is the reader — tier 2, behind the `require_*` guards — and it converts to markdown, which
11.3 already made the intermediate format. Both probers also report `has_text_layer`, which
is what `extract:text` is gated on. Measured, so:

```
=== real.docx   has_text_layer=True
   extract:text        enqueued
   structure:declared  enqueued
=== empty.docx  has_text_layer=False
   extract:text        not_applicable  patterns.has_text_layer is false
```

A document with no text run still creates a file node with no content, but now it says so
before running rather than after, which is the difference this closes.

The declared rung is genuinely declared for both. `DECLARED_STRUCTURE_PATTERN` maps `docx`
to `has_heading_styles` and `pptx` to `has_slides`, and the reader writes ATX headings FROM
those same heading styles and one `#` section per slide — so the pattern that lets the rung
run and the structure it splits on are two readings of one fact.

**Still open.** `xlsx` probes but does not extract: a grid is not prose, and what a chunk
of a spreadsheet should be is a question this document has not answered. Ordered lists
extract as `-`, because telling them from bullets means resolving `w:numId` through
`word/numbering.xml`. Images, comments, footnotes and tracked changes are each reported by
`probe` and each read by nobody. `citation` stays `impossible` for every office format
until step 7's `render:pdf` gives them a page geometry.

> **Two of those four closed; read this list as of 2026-08-22.** Corrected 2026-09-15.
>
> * **`xlsx` extracts.** Not through `extract:text` — `TEXT_EXTRACTORS`
>   (`structure_tasks.py`) still holds `{"pdf", "text", "docx", "pptx"}` and a workbook has
>   no text layer — but through the tier this document's own Part 4 points at:
>   `structure:sheets` writes the sheet containers and `profile:sheet` / `extract:sheet`
>   run per sheet (`jmfts_core/sheet_tasks.py`). "What a chunk of a spreadsheet should be"
>   is answered for one of `INGEST_SPEC.md` 8.4's four shapes, `records`, on a rule that is
>   not a threshold; `matrix` and `unstructured` are still unbuilt and still waiting on
>   8.8's calibration. `jmfts_core/sheet_records.py` says so at its own module docstring.
> * **`citation` is not `impossible` for office formats.** `render:pdf` shipped
>   (`jmfts_core/render_tasks.py`), and with it `anchor:source` and `anchor:render`
>   (`jmfts_core/citation_tasks.py`) — so a `.docx` hit points at a page of the rendition
>   and says whose pagination it is. Part 11's steps 7, 8 and 9 all ran.
> * **Ordered lists still extract as `-`**, and `jmfts_core/office/extract.py` records the
>   same `w:numId` reason at its module docstring. Unchanged.
> * **Images, comments, footnotes and tracked changes are still read by nobody.**
>   Unchanged.

Two coverage gaps the corpus reports about itself: 12 vocabulary tags are on no fixture,
including `is_encrypted` — so `_probe_ole2`'s deliberate refusal of an encrypted package
has nothing exercising it — and the container hazards (`has_unsafe_member_paths`,
`has_external_entity`) are `proposed`, so a traversal path is visible in `detail` but no
pattern lets the scheduler act on it. `has_heading_styles` reads `planned` for a third
reason: shipped code measures it, but no fixture carries `word/styles.xml`, so it is never
emitted over the corpus. `tests/test_office_extraction.py` covers it against packages
python-docx writes. Run `python -m scripts.corpus report` for the current numbers rather
than trusting these.
<!-- Read 2026-09-15: 23 records and THIRTEEN tags on no fixture, not twelve. Everything
     else in this paragraph still holds, including the `has_heading_styles` clause: eight
     manifest records tag it, but no fixture carries `word/styles.xml` (zero hits in
     `tests/corpus/fixtures.py`), so `probe_vocabulary()` does not report it over the corpus
     and its status is still `planned`. The manifest's tag count and what probe emits are
     two different readings and only the second decides the status. The sentence telling you
     to run the report rather than trust the numbers is why this is a note and not a
     rewrite. -->


That last fact is the pivot of this whole plan. Everything in Parts 5 through 9
needs to know where in the *source* a node came from, and today nothing does.

### What is deliberately not reopened

`INGEST_SPEC.md` 11.3 decided that markdown is the intermediate format, not a
peer format. This plan does not revisit that. Markdown remains what gets
embedded and retrieved. The anchor of Part 5 is carried **alongside** the
markdown, not instead of it — retrieval quality and positional addressing are
separate concerns and coupling them would cost both.

---

## Part 1 — Three dependency tiers, and why they are three

`pip install jmfts` is 585 MB and cannot embed by itself, because the model
stack is the `embed` extra.
<!-- Was "584 MB" until 2026-09-15. It predated the measurement two paragraphs below,
     which read 585, and a document that states two figures for one number in one Part is
     wrong about at least one of them. `CLAUDE.md` reads 586 for the same install; all
     three are readings of a moving dependency set months old, so treat the tier ordering
     as the claim and re-measure before quoting a byte count. -->
 The stated reason (`pyproject.toml`) is that the
only step needing an accelerator is producing vectors, and a worker can ask
another JMFTS for those over HTTP.

Office support splits the same way, but into three tiers rather than two,
because one of the dependencies is not a wheel at all.

| Tier | What | Weight | Where it goes |
|---|---|---|---|
| 1 | `zipfile`, `xml.etree`, `olefile` | stdlib + one pure-Python module | base install |
| 2 | `python-docx`, `python-pptx`, `openpyxl` | +44 MB, and NOT pure Python | `jmfts[office]` extra |
| 3 | LibreOffice, driven by `unoserver` | ~500 MB system package | a badged worker image; `jmfts[convert]` is only the client |

Tier 2's weight was measured 2026-08-22 in empty venvs against the 0.2.0 wheels: base
585 MB, `[office]` 629 MB. This document previously said "~10 MB pure Python", which is
what the three named packages weigh on their own (2.9 + 3.1 + 3.0) and is not what
installing them costs. `python-docx` and `python-pptx` both require `lxml` (12 MB), and
`python-pptx` also requires `Pillow` (7.1 MB) and `XlsxWriter` (1.9 MB). **`lxml` and
`Pillow` are C extensions.** That matters to an image build on a platform with no wheel
for either: tier 2 then needs a compiler, which is precisely the possibility a "pure
Python" label tells the reader to stop considering.

**Tier 1 must stay in base, and this is a hard constraint.** `probe` "depends on
nothing, calls no model, and always runs" (`INGEST_SPEC.md` Part 4). If probing
a `.docx` required the `office` extra, then a base install would accept the
upload, run probe, and report an empty pattern set — which is indistinguishable
from a `.docx` that genuinely declares no structure. Reading `word/styles.xml`
out of a ZIP and counting `<w:pStyle w:val="Heading1"/>` needs no library, so
this constraint costs nothing to honour.

**`olefile` is in base, and that is a decision, not an oversight.** An encrypted
`.docx` is not a ZIP. It is an OLE2 compound file holding `EncryptionInfo` and
`EncryptedPackage` streams, so it carries the same eight magic bytes as a legacy
`.doc`, and `detect_format` reports both as `ole2` today with nothing to
separate them. Probe has to separate them, because the two go opposite ways: a
legacy binary gets converted (tier 3), and an encrypted package must fail with a
named reason. Sending the second down the first's path means LibreOffice failing
on bytes it was never going to read, a long way from the cause.

Separating them is one directory listing. `olefile` is a single pure-Python
module with no dependencies of its own, so it costs a storage-side worker
nothing measurable and it turns `ole2` from a dead end into an answer.

`msoffcrypto-tool` is deliberately **not** carried. It decrypts a
password-protected package, and JMFTS has no path by which a caller could supply
a password. Detecting encryption is the requirement; decrypting is not.

**Tier 3 is not an extra, because `pip install libreoffice` is not a thing.**
It follows the `JMFTS_RUNNER_URL` shape instead: a task type, a worker badge,
and an optional URL that makes it another process's problem. The proposed
setting is `JMFTS_CONVERT_URL`.

Tier 3 earns its keep twice, which is what makes a whole worker image
proportionate:

1. **Legacy formats.** `.doc`, `.xls` and `.ppt` are converted to OOXML, and the
   tier-2 readers open the result. The alternative was three more binary-format
   readers in the `office` extra (`olefile`, `xlrd<2.0`, `pyxlsb`), each worse
   at its format than LibreOffice is.
2. **Renditions.** Any document converts to PDF once, at ingest, so that
   serving a citation image is `pymupdf` over a stored PDF — and `pymupdf` is
   already a base dependency.

Both are conversions, which is why the extra is named `convert` rather than
`render`.

The important consequence, and the simplification the whole render half rests
on: **LibreOffice is needed by exactly one ingest task and by nothing on the
query path.** Converting `.docx` to PDF happens once, at ingest, and the result
is stored. Rendering a page image from that PDF is `pymupdf`, which is already a
base dependency. So a fleet needs one LibreOffice-badged worker for ingestion,
and every search node can serve citation images with no new dependency at all.

### Distribution shape

Extras on `jmfts`, not a separate `jmfts-office` distribution. The rule this
follows is the one `embed` already sets: an extra is right when the code lives
in the same package and only the dependency is optional. A separate
distribution is right when the code is separately useful, which is why
`jmfts-client` is being split out and `jmfts_batch` is a top-level package.
<!-- `jmfts-client` shipped as a separate distribution in 0.2.0, and `jmfts-web` became a
     third in 0.6.0. The rule the sentence states is unchanged; only its tense is. -->

Office extraction is a set of task handlers inside `jmfts_core`; it is not
useful without the queue that runs them.

Two extras, and no more. The three-extra version of this section had a `legacy`
extra of binary-format readers and a `render` extra for rasterizing; routing
legacy formats through tier 3 deleted the first, and `pymupdf` already being a
base dependency deleted the second.

```
jmfts[office]    python-docx, python-pptx, openpyxl
jmfts[convert]   unoserver                          # needs LibreOffice underneath
```

`dev` implies `office`, because the suite opens real packages. It does **not**
imply `convert`: that wheel installs cleanly with no LibreOffice present and is
useless without one, so a suite carrying it would assert against a configuration
nobody deploys. The conversion path gets its own CI job on an image that has the
system package.

**Shipped.** `jmfts_core/office/__init__.py` is the only module permitted to
import a tier-2 reader, and only through its `require_docx` / `require_pptx` /
`require_openpyxl` guards, which turn a bare `ModuleNotFoundError` into
`OfficeStackNotInstalled` naming the extra. `tests/test_office_packaging.py`
fails if an office library reaches the app's import path, and
`scripts/check_base_install.sh` asserts the same thing against a real base venv.

---

## Part 2 — Probe: what an office file declares

New probers for `docx`, `pptx`, `xlsx`, added to `PROBERS_AVAILABLE`. Tier 1
only: `zipfile` plus `xml.etree.ElementTree`.

### Patterns already named by the scheduler

| Format | Pattern | How it is measured |
|---|---|---|
| `docx` | `has_heading_styles` | a `w:pStyle` referencing a style whose `w:styleId` or `w:name` is an outline heading, in `word/document.xml` |
| `pptx` | `has_slides` | `ppt/slides/slide*.xml` member count > 0 |
| `xlsx` | `has_sheets` | `<sheet>` elements in `xl/workbook.xml` |

Measuring `has_heading_styles` through `word/styles.xml` rather than by name
matters: a document whose headings use a renamed or localized style still
declares an outline, and matching the literal string `"Heading 1"` would miss
every non-English document.

### New patterns this capability needs

| Pattern | Formats | Why it is measured |
|---|---|---|
| `is_encrypted` | `ole2` | an encrypted OOXML package is an OLE2 file, **not** a ZIP — see Part 1. Without this it is indistinguishable from a legacy `.doc` and gets sent to a converter that cannot read it. |
| `is_legacy_binary` | `ole2` | the other half of the same directory listing: a `WordDocument`, `Workbook` or `PowerPoint Document` stream names the application. This is what enqueues `convert:ooxml`. |
| `has_macros` | all OOXML | `vbaProject.bin` present. Not a hazard to us — we never execute — but a fact the corpus and the operator both want. |
| `has_tracked_changes` | `docx` | `w:ins`/`w:del` present. Decides whether extracted text is the original or the revision, which is a real ambiguity that must be a recorded parameter, not a silent choice. |
| `has_comments` | `docx`, `xlsx` | comments are content the default reader drops. |
| `has_tables` | `docx`, `pptx` | already a scheduling input for `extract:tables`. |
| `has_images` | all | already a scheduling input for `extract:images`. |
| `has_smartart` | `pptx` | `ppt/diagrams/` present. `python-pptx` cannot read it, so the honest report is that content exists which extraction will not carry. |
| `has_speaker_notes` | `pptx` | notes are often the prose of a deck; a slide-only extraction silently loses the argument. |
| `part_count`, `unknown_parts` | all | corpus instrumentation, Part 10. |

`is_encrypted` is the one that changes behaviour rather than just recording it.
An encrypted package makes probe **fail** with a named reason. It does not
extract to empty and settle. That is the Fail Early rule applied to the one
input that would otherwise produce a confidently empty document.

### Cost

Probe currently reads the blob once. These probers read the ZIP central
directory plus at most three small parts. Measured on the existing corpus is
required before merge, but the expected cost is under 10 ms per file, and it
does not decompress `word/document.xml` in full — the heading scan can stream.

---

## Part 3 — `extract:text` for office formats

Markdown out, per 11.3. Tier 2.

| Format | Reader | Produces |
|---|---|---|
| `docx` | `python-docx` for body, raw `lxml` for the rest | ATX headings from outline styles, paragraphs, tables as GFM, list nesting |
| `pptx` | `python-pptx` | one `##` per slide (title or `Slide N`), body text frames, tables, speaker notes under a `### Notes` subheading |
| `xlsx` | see Part 4 | not a markdown document; the sheet is the unit |

Three decisions inside `docx` extraction that are easy to get silently wrong:

**Tracked changes.** `python-docx` returns the text with insertions included and
deletions excluded — the "final" view — and never says so. When
`has_tracked_changes` is true, the chosen view becomes a recorded parameter
(`INGEST_SPEC.md` Part 7.2), defaulting to `final`, and the attempt detail
records which view produced the text.

**Headers, footers, footnotes, comments.** `python-docx` reads none of them.
`docx2python` does. The plan uses `python-docx` for the body and reads the
remaining parts directly, rather than adding a second whole-document reader that
would disagree with the first about the body. Footnotes are appended as a
`### Notes` section so they are retrievable; headers and footers are recorded on
the file node but **not** put in the chunk stream, because a running header
repeated on 200 pages is 200 copies of the same sentence in the index.

**`altChunk`.** A `docx` can embed another document. `python-docx` ignores it.
Probe reports it as an unknown part; extraction that meets one fails rather than
silently dropping the embedded content.

### What is not extracted, and is said so

OMML equations, drawings, SmartArt, embedded OLE. Each is reported on the file
node as content that exists and was not carried. A count in the attempt record
is the difference between a lossy extraction and an unknown one.

---

## Part 4 — Worksheets

`INGEST_SPEC.md` Part 8 already specifies this in full: the tree stops at the
sheet, two tasks per sheet (`profile:sheet` and `extract:sheet`), eight
measurements, four shapes, and a profile node. Neither task calls a model.

This plan adds nothing to that design and does not restate it. Three notes on
implementing it:

1. **8.8 leaves the thresholds unset on purpose.** They need calibration against
   real workbooks before defaults are written. That calibration is a corpus
   task (Part 10), and it blocks `profile:sheet` from shipping its shape
   decision — not from shipping its measurements. Measurements first, shape
   second, is a shippable split.
2. **`openpyxl` in read-only mode is required, not preferred.** A 1M-row sheet
   loads its entire cell grid into Python objects otherwise.
   `python-calamine` is roughly an order of magnitude faster for the read path
   and is worth measuring, but it carries no formatting and `profile:sheet`
   measures merged cells, so it cannot be the only reader.
3. **A cell range is already an anchor.** Part 5 needs no new work for `xlsx`;
   `{"sheet": name, "ref": "B4:H120"}` is the address, and it is the same string
   the retrieval verb of Part 7 accepts.

---

## Part 5 — The anchor

One concept, and every feature in Parts 6 through 9 is a consumer of it.

**An anchor is a stable address of a region of the source document, recorded on
the node that region produced.** It lives at
the `source_anchor` evidence row on chunk, section and sheet nodes.

```json
{"kind": "pdf",   "page": 3, "bbox": [72.0, 118.4, 540.0, 262.9]}
{"kind": "ooxml", "part": "word/document.xml", "path": "/w:document/w:body/w:p[42]"}
{"kind": "ooxml", "part": "ppt/slides/slide7.xml", "path": "./p:cSld/p:spTree/p:sp[3]", "slide": 7}
{"kind": "cells", "sheet": "Q3 Pipeline", "ref": "B4:H120"}
```

`kind` is what says how to interpret the rest. It is an open string for the same
reason `usetype` is.

### Two anchors, not one

The addresses above locate a region **in the source**. They do not say where it
lands on a rendered page, and for `ooxml` they cannot: the layout that decides
which page paragraph 42 falls on is performed by Word or LibreOffice, not by
anything readable in `word/document.xml`. A `.docx` has no pages until something
paginates it.

So a second, derived anchor is recorded separately, once a rendition exists
(Part 6):

```json
{"kind": "pdf", "page": 3, "bbox": [...], "of": "rendition"}
```

Keeping them apart matters because they have different truth conditions. The
source anchor is exact and permanent. The rendition anchor is approximate,
depends on which LibreOffice version paginated the file, and **can fail to be
computed at all**. Storing both under one key would make a recovered rectangle
indistinguishable from an exact one.

### `citation` — one task, every format, best-effort

Matching chunks to the pages and rectangles they came from is its own task, and
it is the same task for every format. **`citation` does not care what the source
was.** By the time it runs there is a PDF — either the uploaded file itself, or
the rendition of Part 6 — and a set of chunk nodes with text. Its job is to put a
page and a rectangle on each of them.

| | |
|---|---|
| Task name | `citation` |
| Depends on | the structure rung that wrote the chunks, and (for office) `render:pdf` |
| Write mode | `children` — it annotates the chunks it was scoped to |
| Uses a model | no |
| Blocking | **no** |

Three properties, and each is load-bearing.

**It is separate.** Extraction's job is text, and it is already the most
format-specific code in the tree. Folding position recovery into it would mean
four extractors each growing a second responsibility, and a change to how
rectangles are found touching all four. As its own task it is one handler, and it
gets its own retry budget, its own routing badge, and its own line in the
attempt log.

**It is best-effort, and this is what "non-blocking" has to mean concretely.**
A permanently failed task today sets its node to `settled='failed'`
(`repositories/task_queue.py:727`), and a failed node is terminal — the settle
walk refuses to build on top of a hole (`settling.py:203`). That is correct for
`extract:text`. It is wrong for `citation`: a document whose rectangles could not
be recovered is a perfectly good searchable document, and failing it would throw
away the extraction, the chunks and the vectors over a missing convenience.

The change is small and exactly localized, because a failed task is **already**
not counted as unfinished — `_unfinished_criterion` treats a permanent failure
as finished, so it never blocks the walk by itself. The only thing that blocks is
that one assignment. So:

> A set of **advisory** task types, declared once in
> `jmfts_core/models/task_queue.py` next to the status constants. When an
> advisory task fails permanently, `fail_task` records the attempt exactly as it
> does now and **does not touch the node's `settled`**.

Declared as data, in one place, for the reason `TASK_ROWS` is: "advisory" is a
property of the task type, not of the call site, and a boolean argument on
`fail_task` would let two callers disagree about the same task.

The rule for what may be advisory is worth stating, because the mechanism is
easy to misuse and it is exactly the kind of thing this project's Fail Early rule
exists to prevent: **a task is advisory only if a node without its output is
still correct and still says so.** `citation` qualifies — a chunk with no anchor
is a chunk, and the absence is visible. `extract:text` does not — a file node
with no text is not a document, it is a lie. Adding a type to that set is a
decision about what a settled node means.

**It runs for PDFs too, and that is where it starts.** `pdf_extraction.py`
already computes the bounding box of every block, uses it to order text and to
own tables, and then discards it. For a source PDF the two anchors of the
previous section are the same object, so `citation` on a PDF is retaining what is
already computed. It needs no office library, no LibreOffice, and no rendition.

### How a rectangle is recovered, and when it is not

For office formats there is nothing to retain, because the layout happened inside
LibreOffice. The rectangle is recovered by searching the rendition for the node's
text (`pymupdf`'s `page.search_for`). That is approximate, and it has three
failure modes:

- the text appears more than once on the page (a repeated table header),
- the text does not appear at all (LibreOffice hyphenated or reflowed it),
- the chunk spans a page break.

**When the search does not resolve to exactly one rectangle, no anchor is
written for that chunk, and the reason is recorded on it.** The task still
completes: partial recovery is the expected outcome, not a failure. A citation
feature that returns a plausible rectangle from the wrong paragraph is worse than
one that says it cannot locate the passage, because the reader cannot tell.

Recording the reason per chunk is what makes the recovery rate measurable, which
is what Part 10 needs to decide whether the text search is good enough. The
higher-fidelity alternative is to have LibreOffice emit a **tagged PDF** and map
its structure tree back to the OOXML elements, removing the search entirely. It
is substantially more work and it is not the first step; it is the answer if the
measured rate comes back poor.

### Why this is a retrieval feature and not an ingest detail

Every JMFTS answer today is text a caller has to trust. An answer that can point
at the page it came from, and at the words on it, is a different product: the
reader verifies in one glance instead of re-finding the source.

That makes `citation` an extension of retrieval that happens to be computed at
ingest, and it is the reason the anchor is a first-class field on the node rather
than something derived on demand. Deriving it at query time would mean opening
the source document on the search path, which is the one place the heavy
dependencies must not be.

---

## Part 6 — Renditions

**A rendition is a PDF of the source document, stored as a derived blob.**

New task `render:pdf`. Badged for a worker with LibreOffice. Off by default.

| | |
|---|---|
| Depends on | `probe` |
| Write mode | `self` |
| Enqueued when | format is an office format **and** the `render` option is on |
| Produces | a blob OID under `file.rendition_ref`, plus page count and page geometry |

> **Two rows of that table are superseded. Corrected 2026-09-15; the table is left standing
> because source comments cite Part 6 by name.** Both were settled by
> `SPRINT_0_6_0.md` Block G step 32, which is what built the task.
>
> **"a blob OID under `file.rendition_ref`" was WRONG WHEN IT WAS WRITTEN**, and it is the
> one error in this document that is not merely stale. It contradicts `INGEST_SPEC.md` 3.3:
> "**`file`** is what we received. It never changes after upload." A task writing into that
> block would be editing the record of what was RECEIVED in order to say something about
> what was DERIVED. The second reason is the value — an OID is a physical address the
> repository maintains, in a namespace a caller may `PATCH`, and what a reader actually
> needs is the page count and the per-page geometry a box is scaled against, which a ref
> cannot carry.
>
> What shipped instead: **a `rendition` evidence row on the node, and the bytes in
> `document_renditions`** (migration 023). `jmfts_core/repositories/rendition.py`'s
> `EVIDENCE_NAME` is where the name is spelled once, and it carries this argument at its
> own definition; `render_tasks.py` carries it again at the handler. `convert:ooxml`'s
> artefact is the same shape under the name `conversion`. **A lane in 0.6.0 declined an
> instruction derived from this sentence and was right to.**
>
> **"Enqueued when … the `render` option is on", and "off by default", are both
> superseded.** There is no `render` option and the row is unconditional
> (`TaskRow(TASK_RENDER_PDF, write_mode=WRITE_SELF, requires=(RENDITION_SOURCE,))`). Block G
> answered Part 12 question 1 with a measurement rather than carrying it: original bytes
> are 144 MB of a 2988 MB reference database — 4.8%, not the doubling the question assumed
> — and a rendition is DERIVED, so the policy is cache eviction rather than retention. What
> makes an unconditional row safe on an appliance with no LibreOffice is that `render:pdf`
> is in `ADVISORY_TASK_TYPES`: it fails PERMANENTLY naming `JMFTS_CONVERT_URL` and the
> image, and the document settles anyway with its text, its chunks and its vectors. An
> option would also have been the wrong mechanism for a second reason — a row's condition
> is a pure function of `(format, patterns, options)` so that `EXPLAIN` can answer without
> bytes, and "is there a converter configured" is a fact about the DEPLOYMENT, which is in
> neither.
>
> **`RenditionRepository.delete` is the eviction half and nothing schedules a sweeper**;
> nothing regenerates on read either. `pdf_for` raising `NoRenditionError` is what a read
> gets until somebody writes both.

Three reasons this is a stored artifact rather than a conversion performed on
demand:

1. **The query path loses its heaviest dependency.** Serving a citation image
   becomes `pymupdf` over a stored PDF, and every search node can do it.
2. **It is stable.** Two LibreOffice versions paginate differently. A rendition
   computed once means a citation rectangle stored in June still points at the
   same words in December.
3. **PDF and office unify.** After this task, every renderable document in the
   system is a PDF, and Parts 7's implementation has one code path.

The cost is honest and should be stated to the operator: a rendition is roughly
the size of the source, so enabling it approximately doubles blob storage for
office documents. That is why it is off by default and why it is a per-ingest
option rather than a global setting.
<!-- Superseded 2026-09-15; see the note under the table above. The denominator was
     wrong — original bytes are 4.8% of a reference database, so the upper bound is
     +4.8% and not a doubling — and the row shipped unconditional with eviction as the
     policy. There is no `render` option. -->


**Driving LibreOffice.** `unoserver`, not `unoconv` (unmaintained) and not
`soffice --headless --convert-to` per file (a cold start per document, and
concurrent invocations fight over the user profile directory). `unoserver`
keeps one process warm and serializes conversions. The worker owns it; the
`JMFTS_CONVERT_URL` form is a JMFTS worker in front of it, not raw unoserver,
so the task contract stays a JMFTS task contract.

Failure is a normal task failure with the converter's stderr in the attempt
record. A document LibreOffice cannot convert has no rendition, keeps its source
anchors, and remains fully searchable. Rendering degrades; retrieval does not.

---

## Part 7 — Addressing a region

Three read verbs, all on the query path, all served from the rendition or the
source blob.

> **All three shipped, under these names and these paths. Read 2026-09-15.**
> `GET /documents/{document_id}/image`, `/region` and `/cells` are `@expose`d on
> `jmfts_core/services/document_service.py`, which is where this Part says they belong.
> `/cells` grew a 409 for a node that is not a worksheet, and `/image` and `/region` grew
> one for a document with no PDF to draw on — `NoRenditionError` and `UnreadablePdf`. Read
> the code for the current parameters; the tables below are the design.

### `GET /documents/{id}/image`

The page or slide a node came from, as PNG.

| Parameter | Meaning |
|---|---|
| `dpi` | raster density, default 150 |
| `highlight` | when true, draw the node's rendition anchor |
| `page` | override; defaults to the node's own anchor page |

A node with no rendition anchor and `highlight=true` is a 409, naming which of
the three Part 5 failure modes applied. It is not a silently unhighlighted
image.

### `GET /documents/{id}/region`

The pixel region containing a citation. This is the verb the user story is
about: a search result comes back, and the caller wants the picture of the words
that matched.

Input is a document id and optionally a text span within that node's content.
Output is a cropped PNG plus the rectangle in PDF points, so a caller doing its
own rendering can use the coordinates instead.

Resolving a span *within* a chunk is the same `search_for` operation as Part 5,
run at query time against the stored rendition and scoped to the node's own
rectangle. Scoping it to the node's rectangle is what makes it reliable — the
ambiguity that defeats a whole-page search usually does not exist inside one
paragraph.

### `GET /documents/{id}/cells`

A spreadsheet region. `?ref=B4:H120`, or the sheet node's own anchor by default.
Returns values, formulas and the rendered markdown table. Served from the source
blob with `openpyxl` — there is no rendition involved, because a page image of a
spreadsheet answers a different question and is rarely the one asked.

These are read verbs on documents, so they belong in
`jmfts_core/services/document_service.py` behind `@expose`, which puts them in
the generated client and the OpenAPI document with no separate work.

---

## Part 8 — Modifying a document

The first write-side capability, and the one with the largest gap between the
obvious implementation and the correct one.

### The rule

**The source package is the truth. The tree is a projection of it. A
modification rewrites named parts of a copy of the package, and never
re-authors the document from the tree.**

The obvious implementation is to parse the `.docx` into nodes, let the caller
edit them, and write a new `.docx` with `python-docx`. That is a
format-converting round trip, and everything `python-docx` does not model is
destroyed: comments, tracked changes, content controls, fields, equations,
drawings, section geometry, theme, styles it did not read. The output opens, and
looks wrong. It is precisely the "finished task that relies on placeholders"
that this project's Fail Early rule forbids, with the additional problem that
the placeholder is invisible.

Editing in place inverts the risk. Copy the ZIP, replace the text of exactly the
elements the anchor names, leave every other byte untouched. What we do not
understand, we do not parse, and therefore cannot break.

### The verb

`POST /documents/{id}/edit` taking a list of `(node_id, new_text)`.

1. Load the source blob.
2. For each node, require an `ooxml` anchor. **A node without one is a 422** that
   names the node — not a skipped edit.
3. Resolve each anchor's part and XPath. An anchor that does not resolve is a
   409: the package changed under the tree, and re-ingestion is the answer.
4. Rewrite the text of the runs under each element, preserving the first run's
   properties and dropping the remainder. (Word splits a sentence across runs at
   arbitrary points for spell-check and revision state; preserving all of them
   means deciding which run each new character belongs to, which is not
   answerable.)
5. Repack: same ZIP, same member order, same compression, changed parts swapped.
6. **Verify before returning.** Reopen the result, re-extract, and confirm the
   edited nodes now read as intended and no other node's text changed. A
   round trip that does not reproduce the intent fails the request and returns no
   file.

Step 6 is cheap — it is the extraction we already have, run once — and it is
what makes this a fail-early write path rather than a hopeful one.

### Scope limit

This verb changes the text of existing elements. It does not insert or delete
paragraphs, slides, rows or sections. Structural editing is a separate,
later verb with its own contract, because insertion has to answer questions
about numbering, cross-references, bookmarks and section breaks that
replacement does not.

Saying so is not a limitation to apologize for. A replace-only verb is
completely correct within its scope, and a structural verb that is 90% correct
produces files that are wrong in ways the caller cannot see.

---

## Part 9 — Generating from a template

"Take a target file with office metadata and a different subtree of data, and
use that target's structure to create a new file."

This is Part 8 with the slots named in advance.

### Slots must be native, not textual

The tempting design is `{{customer_name}}` in the document and a string
replacement. It fails constantly, and for a structural reason worth stating:
**Word splits a text run at arbitrary points**, so `{{customer_name}}` is
routinely stored as `{{cus`, `tom`, `er_name}}` across three runs with different
revision ids. A string search of the XML does not find it. This is the single
most common failure of naive `.docx` templating.

OOXML has native mechanisms for exactly this, and each is a single element that
cannot be split:

| Format | Slot mechanism |
|---|---|
| `docx` | content control (`w:sdt`) with `w:tag` / `w:alias` |
| `docx` | `docProps/custom.xml` document properties, referenced by `DOCPROPERTY` fields |
| `pptx` | layout placeholders, addressed by `idx` and `type` |
| `xlsx` | defined names |

A template declares its slots by using one of these. `GET /templates/{id}/slots`
reads them out of the package and returns the list, so a caller can discover
what a template wants without opening Word.

**A template that declares no slots is a 422** naming the mechanisms above. It
is not a fallback to string replacement.

There is a genuine need for text substitution in templates authored by people
who will not use content controls. That is a separate opt-in verb —
run-normalizing replacement, which merges adjacent runs sharing formatting
before matching — with a different name and a documented failure mode. Nobody
gets it by accident, and nobody gets silent partial substitution.

### Filling

`POST /generate` taking a template document id, a source subtree id, and a
binding from slot name to either a literal or a node selector.

The subtree half is what makes this a JMFTS feature rather than a templating
library: the values come from the tree, so a filled document can carry
retrieved content, a summary node, a table extracted from a different workbook,
or the result of a search — and the generated file records the node ids it drew
from, which makes it auditable back to its sources.

Output is a new blob, and by default a new `file` node whose provenance names
the template and the binding. The generated document is then itself ingestible,
which closes the loop: JMFTS can search what JMFTS wrote.

### Naming collision to avoid

`jmfts_core/services/template_service.py` already exists and is about **prompt**
templates (`adjutant:template` usetype). It is unrelated. This capability needs
a different name — `document_generation` — and must not be added to that
service.

---

## Part 10 — The corpus

None of the above is testable against files we author ourselves, because every
file we author is one our own reader already handles.

### Probe is the feature probe

The corpus needs a per-file feature vector to be a dataset rather than a pile.
JMFTS already computes one: `matched.patterns`. The pattern vocabulary of Part 2
**is** the corpus's tag vocabulary, and extending one extends the other. This
means coverage is measurable with shipped code, and a corpus file whose features
no test exercises is a query, not an audit.

### Sources, in the order they are worth the effort

1. **Apache POI `test-data/` and LibreOffice `sw/qa`, `sc/qa`, `sd/qa`.**
   Thousands of adversarial OOXML files, each attached to a bug, already
   licensed. This is the highest-value source by a wide margin, and it is where
   to start.
2. **The reader libraries' own fixtures** (`python-docx`, `python-pptx`,
   `openpyxl`). Small and minimal — good for pinning a single behaviour.
3. **Government open data and SEC EDGAR** for real `.xlsx`: multi-row headers,
   units inside header cells, footnote markers in data cells, merged
   presentation rows. This is what Part 4's thresholds must be calibrated
   against, and it is a blocker for `profile:sheet`'s shape decision.
4. **Govdocs1** for legacy `.doc`/`.xls`/`.ppt` and format confusion.

Malware corpora (VirusShare, MalwareBazaar) are deliberately excluded. They
change the legal and operational posture of the repository, and the structural
attacks they would cover — zip bombs, XXE, entity expansion, duplicate ZIP
entries, central directory mismatch — can be synthesized in a fixture generator
without carrying anyone's payloads.

XML parsing at ingest uses `defusedxml`, or `lxml` with `resolve_entities=False`
and `huge_tree=False`, from the first commit rather than after the first
finding.

### Fidelity tests, in four tiers

| Tier | What it compares | Needs | Catches |
|---|---|---|---|
| 1 | no-op repack, part set and bytes | stdlib | packaging bugs, member ordering, compression drift |
| 2 | one-element edit: every other part byte-identical | stdlib | over-broad rewrites, normalizing readers |
| 3 | reopen with an independent reader (`pandoc`, LibreOffice) | tier 3 dep | our reader agreeing only with itself |
| 4 | render before/after, SSIM | LibreOffice | silent visual loss the first three miss |

Tiers 1 and 2 are cheap, need no system dependency, and catch most of what
matters — they belong in the default suite. Tiers 3 and 4 run in an
office-badged CI job.

Tier 2 is the specific test that holds Part 8's rule. If a one-word edit changes
any part other than the one the anchor named, the implementation has started
re-authoring, and the test says so before a user finds out.

### Property-based round trips

`hypothesis` over generated edit sequences, asserting that extract → edit →
repack → extract reproduces the intended text and leaves every unedited node
unchanged. This is the invariant Part 8 step 6 checks at runtime, exercised at
scale.

---

## Part 11 — Phasing

Each step is independently useful and independently shippable. The split
between step 3 and step 4 is the real fork: steps 1 to 3 are `INGEST_SPEC.md`
step 6 and need no new concepts, and everything from 4 on depends on the anchor.

| Step | What | Tier | Depends on | Rough size |
|---|---|---|---|---|
| 0 | The extras, the `office` seam, the import guards | — | — | **shipped** |
| 1 | Advisory task types; `fail_task` honours them | — | — | small |
| 2 | `citation` for PDF: retain the box `pdf_extraction` computes | 1 | 1 | small |
| 3 | `GET /documents/{id}/image` and `/region` | 1 | 2 | medium |
| 4 | Office probers; `is_encrypted` fails, `is_legacy_binary` schedules | 1 | — | small |
| 5 | `extract:text` for `docx` and `pptx` | 2 | 4 | medium |
| 6 | `profile:sheet` measurements; `extract:sheet`; `/cells` | 2 | 4 | medium |
| 7 | `render:pdf` and `convert:ooxml`; the LibreOffice worker | 3 | 4 | medium |
| 8 | `citation` for office: recovery against the rendition | 3 | 5, 7 | medium |
| 9 | Source anchors for office extraction | 2 | 5 | medium |
| 10 | `POST /documents/{id}/edit` | 2 | 9 | large |
| 11 | `/templates/{id}/slots`, `POST /generate` | 2 | 10 | large |
| 12 | Part 4 threshold calibration; shape decision | 2 | 6, corpus | medium |

> **Steps 0 through 9 have shipped; 10, 11 and 12 have not. Read 2026-09-15.** The "Rough
> size" column is a plan and only step 0 carried a status, so the table as it stands reads
> as if nothing after it had run. What settles each:
>
> | Step | Shipped | Where |
> |---|---|---|
> | 1 | yes | `ADVISORY_TASK_TYPES` in `jmfts_core/models/task_queue.py` |
> | 2 | yes | `citation` in `jmfts_core/citation_tasks.py` |
> | 3 | yes | `GET /documents/{id}/image` and `/region`, `services/document_service.py` |
> | 4 | yes | `PROBERS_AVAILABLE` is `("pdf", "text", "docx", "pptx", "xlsx", "ole2")` |
> | 5 | yes | `TEXT_EXTRACTORS` holds `docx` and `pptx`; `jmfts_core/office/extract.py` |
> | 6 | yes | `profile:sheet`, `extract:sheet`, `GET /documents/{id}/cells` |
> | 7 | yes | `render:pdf` and `convert:ooxml` in `jmfts_core/render_tasks.py` |
> | 8 | yes | `anchor:render` in `jmfts_core/citation_tasks.py` |
> | 9 | yes | `anchor:source`, same module |
> | 10 | **no** | there is no `POST /documents/{id}/edit` |
> | 11 | **no** | there is no `/templates/{id}/slots` and no `POST /generate`; `services/template_service.py` is still the PROMPT-template service Part 9 warns about |
> | 12 | **no** | `matrix` and `unstructured` are unbuilt and 8.8's thresholds are unset; `jmfts_core/sheet_records.py` says so |
>
> Steps 10 and 11 are the write half, and Part 11's own "What to cut" paragraph is why
> they are the two still standing: they are the two with no partial-credit version.

The ordering changed from the first draft of this document, and the reason is
worth recording: **citation for PDF comes before any office format is read.**

Steps 1 to 3 deliver the headline feature end to end — a search result that can
show you the page and the rectangle it came from — using formats JMFTS already
ingests, with no new dependency of any tier. Step 2 is retaining a value the code
already computes and throws away. Nothing in steps 1 to 3 needs `python-docx`,
LibreOffice, or a rendition.

That is also the honest way to test the design. If the anchor shape, the advisory
mechanism and the two read verbs are wrong, they are wrong for PDF too, and
finding out costs three small steps instead of eight.

Steps 4 to 9 then widen the same feature to office formats, which is mostly a
matter of producing a PDF to run the existing `citation` handler against.

The corpus (Part 10) is not a step. Tiers 1 and 2 arrive with step 2 and grow
with each step after it. Step 11 cannot start before the spreadsheet half of
the corpus exists.

### What to cut, if this is too large

Steps 1 to 3 and 8 deliver "JMFTS ingests and searches office documents", which
is the capability with the broadest use. Steps 4, 6, 7 deliver citation images.
Steps 9 and 10 deliver writing, and they are the two large ones — they are also
the two with no partial-credit version, because a write path that is mostly
right emits files that are quietly wrong.

---

## Part 12 — Open questions

1. ~~**Rendition storage.**~~ **Answered 2026-09-14 by measurement** (`SPRINT_0_6_0.md`
   Block G). The premise was wrong: original bytes are 144 MB of a 2988 MB reference
   database, 4.8%, so a rendition per office document is an upper bound of +4.8% and not a
   doubling. A rendition is derived, so the policy is **on always, evictable by a sweeper,
   regenerated on read** — none of per-ingest, per-usetype or global. `RenditionRepository.delete`
   is the eviction half; nothing schedules a sweeper and nothing regenerates on read yet,
   and `pdf_for` raising `NoRenditionError` is what a read gets until they exist.

   The original wording: *"Roughly doubles blob storage for office documents. Off by default
   is proposed; whether it should be per-ingest, per-usetype, or a global policy is not
   decided."*
2. ~~**Legacy binary formats.**~~ **Decided 2026-08-21: convert through
   LibreOffice.** `.doc`/`.xls`/`.ppt` become tier 3, not a wheel extra. This
   deleted the proposed `legacy` extra.

   The consequence to carry: for these formats the converted OOXML is what gets
   read, so **the source of truth for editing is the conversion, not the upload**
   — which puts them outside Part 8's rule. A legacy file is therefore
   **read-only**. `POST /documents/{id}/edit` refuses one with a message naming
   the conversion, rather than editing a derived package the user never saw and
   handing back a `.docx` where they uploaded a `.doc`.
3. ~~**Which LibreOffice.**~~ **Answered 2026-09-13** (`SPRINT_0_6_0.md` 4.9): **26.8.0**,
   the Fresh branch, chosen over the conservative Still branch because 26.2 reaches end of
   life on 2026-11-30 and pinning it would ship a component with no security updates on its
   first day. Both halves of the rest are answered with it: **the version is recorded on the
   rendition**, as a `renderer_version` key in its evidence row, and **an upgrade re-renders
   on read** rather than invalidating in bulk — which is why `RenditionRepository.store`
   REPLACES where `BlobRepository.store` refuses a second write.

   The original wording: *"A pinned container image is proposed, because pagination
   differences between versions are what make a stored rendition stable. Whether the version
   is recorded on the rendition, and what happens on upgrade, is not decided."*
4. **Tracked-changes default.** `final` is proposed. `original` is defensible for
   an archive.
5. **Tagged-PDF structure mapping** as a replacement for text-search anchor
   recovery. Deferred until Part 10 measures the recovery rate.
