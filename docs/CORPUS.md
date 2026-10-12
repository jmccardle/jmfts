# The fidelity corpus

**Reviewed for publication 2026-09-15** (`SPRINT_0_6_0.md` Block E step 5). Four things were
corrected: the tag-on-no-fixture count (twelve → thirteen) and the list under it, which read
as exhaustive over seven names; the two files tagged `is_damaged`, which are three; a
proposed environment variable marked as proposed; and python-pptx's repository URL, which
moved to the `python-openxml` organisation. Everything else was checked against the tree and
holds. **The "fetch step" this document specifies has not been built** — the corpus is 23
generated fixtures, exactly as *What exists now* says.

Companion to `docs/OFFICE_SPEC.md` Part 10. Part 10 says *why* the corpus exists and what
it must contain; this document is the operational half — what is built, what would have to
be fetched, under which licence, and where it goes.

It is a separate file rather than an edit to Part 10 on purpose: Part 10 is the design, and
design belongs in one document that several people are editing. This is the runbook.

---

## What exists now

| Piece | Where | What it is |
|---|---|---|
| Vocabulary | `tests/corpus/vocabulary.py` | One table of pattern names, each with a status. Probe is imported and interrogated; nothing is restated. |
| Manifest | `tests/corpus/manifest.toml` + `manifest.py` | One record per file: bytes, provenance, licence, feature tags, expected behaviour. |
| Generator | `tests/corpus/fixtures.py` | 23 fixtures, built from `zipfile` and string literals. Deterministic. Nothing binary is committed. |
| Safe XML | `tests/corpus/xmlsafe.py` | `expat` with every declaration handler bound to a refusal, plus a depth limit. |
| Fidelity tiers 1–2 | `tests/corpus/roundtrip.py` + `test_roundtrip.py` | Part-set, content, order, compression and member-metadata diff, and the tier-2 assertion built on it. |
| CLI | `scripts/corpus.py` | `report`, `write <dir>`, `check`. |
| Office defects | `tests/corpus/test_office_defects.py` | The must-reject half, held against the probers. |

<!-- The table is by piece, not by file. `tests/corpus/` also holds `test_fixtures.py` and
     `test_manifest.py`, which are the audits of the two rows above them. 2026-09-15. -->

```
python -m scripts.corpus report          # coverage, and what shipped code can measure
python -m scripts.corpus write /tmp/c    # materialise the fixtures to look at them
python -m scripts.corpus check           # manifest against the bytes; non-zero on drift
pytest tests/corpus -q                   # the suite. No database, no optional dependency.
```

### The number that matters

`report` prints it: **18 of 29 tags are measurable by shipped code today.** One more is an
`OFFICE_SPEC.md` Part 2 pattern waiting on Part 11 step 4, and ten are container-level
hazards that no spec section names yet — see [Proposed Part 2 rows](#proposed-part-2-rows).
Read 2026-09-05; run `report` rather than trusting the number here.

It also prints thirteen tags carried by no corpus file. Seven of them need a *real*
document: `has_tracked_changes`, `has_comments`, `has_tables`, `has_smartart`,
`has_speaker_notes`, `is_encrypted`, `is_legacy_binary`. The other six —
`has_headings`, `has_images`, `has_markup`, `has_outline`, `is_conversation`, `is_scanned`
— are a different gap: at least three of them are markdown or text patterns a *generated*
fixture could carry, so they are a fixture somebody has not written rather than a file
somebody has to fetch. A hand-built fixture for tracked
changes would be a fixture asserting what we already believe tracked changes look like,
which is the failure Part 10 opens with — *"none of the above is testable against files we
author ourselves, because every file we author is one our own reader already handles."*

The first list is the fetch step's specification. It is not a wish. The second is not
fetch's to answer.

<!-- Corrected 2026-09-15: the count was twelve and is thirteen, and the sentence "Every one
     of them needs a real document" was written over a list of seven while the count was
     twelve, so it read as exhaustive and was not. Run `report` for the current figures. -->

---

## What a fetch step would have to do

Deliberately **not** built here. Fetching is a decision about repository size, about
licence obligations that attach to redistribution, and about where a multi-gigabyte corpus
lives on a machine — three questions with no correct default. What follows is what the
answer has to cover.

### Sources

| Source | URL | Licence | Obligation if redistributed | Size |
|---|---|---|---|---|
| Apache POI `test-data/` | `https://github.com/apache/poi` (`test-data/`) | Apache-2.0 | NOTICE file preserved, changes stated | order of 10² MB for `test-data/` alone; the repo with history is larger |
| LibreOffice `sw/qa`, `sc/qa`, `sd/qa` | `https://git.libreoffice.org/core` | MPL-2.0 | file-level copyleft; per-file notices preserved | the `core` repo is order of 10⁰ GB; the three `qa` trees are a fraction of it |
| python-docx fixtures | `https://github.com/python-openxml/python-docx` (`tests/test_files/`) | MIT | attribution | order of 10⁰ MB |
| python-pptx fixtures | `https://github.com/python-openxml/python-pptx` (`tests/test_files/`) | MIT | attribution | order of 10⁰ MB |
| openpyxl fixtures | `https://foss.heptapod.net/openpyxl/openpyxl` (`openpyxl/tests/data`) | MIT | attribution | order of 10⁰ MB |
| SEC EDGAR | `https://www.sec.gov/Archives/edgar/full-index/` to enumerate, `https://www.sec.gov/Archives/edgar/data/<cik>/` to fetch | US Government work, public domain | none | per-filing; select, do not mirror |
| Govdocs1 | `https://digitalcorpora.org/corpora/file-corpora/files/` | freely redistributable (harvested from `.gov` servers) | none | ~1M files, ~500 GB total, published as ~1000 archives of ~330 MB |

**Every size in that table is an order of magnitude, not a measurement.** A fetch step must
measure what it actually downloaded and record it — which the manifest already requires
per file, because `size` is a required field.

### Operational details that are easy to get wrong

**SEC EDGAR requires a declared User-Agent** carrying a real contact address, and rate
limits to 10 requests per second. A fetch that ignores either gets the appliance's egress
IP blocked, which is a durable problem rather than a failed run. EDGAR is also the only
source here where the useful files must be *selected* — a mirror is both enormous and
mostly HTML.

**POI and LibreOffice need a sparse checkout.** `git clone --filter=blob:none --sparse`
followed by `git sparse-checkout set test-data` (POI) or `sw/qa sc/qa sd/qa`
(LibreOffice). A full clone of either is an order of magnitude more data than the part that
is wanted, and both have long histories.

**Govdocs1 is sampled, never mirrored.** The corpus is per-thousand-file archives; the
useful fetch takes a few of them for legacy `.doc`/`.xls`/`.ppt` and format confusion, and
records which. Which archives were taken is provenance and belongs in the record.

**Licences do not merge.** Apache-2.0 and MPL-2.0 files carry obligations to whoever
redistributes them. Keeping them out of this repository's history is what keeps JMFTS's
own MIT licence a complete statement about JMFTS. That is the strongest single argument for
the layout below, and it is the reason the manifest makes `licence` a required field with
no default rather than a nicety.

### Where the files go

`datasets/` is gitignored and is a symlink whose target is a per-machine choice — the same
arrangement the MultiHop-RAG benchmark data already uses (`CLAUDE.md`). Imported corpus
files follow it:

```
datasets/corpus/poi/<upstream path>
datasets/corpus/libreoffice/<upstream path>
datasets/corpus/edgar/<accession>/<file>
datasets/corpus/govdocs1/<archive>/<file>
```

The upstream path is kept, because it is half the provenance: an Apache POI file is
identified by its path in `test-data/`, and a record that renamed it could not be followed
back to the bug it was attached to.

`Corpus.bytes_for(record, root=...)` takes the root explicitly and **has no default**. A
default would be a hard-coded path on one machine, and this project's rule is that software
should be forkable without carrying somebody's directory layout. The suite's own
`root` would come from `JMFTS_CORPUS_ROOT` — **a name this section proposes and nothing in
the tree defines; do not go looking for it (2026-09-15)**; when it is unset, imported records are not
exercised and `report` says how many were not — *not* silently counted as passing.

### What a fetched file's record looks like

```toml
[[file]]
name = "poi/document/Bug53453Test.docx"
origin = "imported"
source = "apache/poi test-data/document/Bug53453Test.docx"
url = "https://github.com/apache/poi/blob/trunk/test-data/document/Bug53453Test.docx"
licence = "Apache-2.0"
sha256 = "…"
size = 12345
format = "docx"
tags = ["has_content_types", "has_tracked_changes"]
expect = "parses-lossy"
minimal = false
note = "…which bug, and what about this file makes it worth carrying"
```

The `sha256` is what makes a fetch reproducible: the fetch step verifies it, and a file
whose bytes moved upstream is a loud failure rather than a silently different dataset.
`minimal = false` is the common case for imported files and is worth stating — a bug
attachment is a real document that happens to contain the feature, which is a different
kind of evidence from a minimal reproduction and is not a substitute for one.

---

## Proposed Part 2 rows

`OFFICE_SPEC.md` Part 2's pattern list describes what a document **declares**. Half of Part
10's fixture generator produces attacks on the **container**, and Part 2 names no pattern
for any of them — so the corpus can label those files and no shipped or planned code can
measure them. `tests/corpus/vocabulary.py` carries them with status `PROPOSED`, and
`test_vocabulary.py` asserts they are still absent from both probe and Part 2, so the day
either learns one, the suite says to promote it.

These are the rows that would close the gap. Adding them to Part 2 is a spec decision, not
this lane's:

| Pattern | Formats | Why it would be measured |
|---|---|---|
| `has_unsafe_member_paths` | all ZIP | A member path that is absolute or escapes with `..`. Any reader that extracts before parsing writes outside its temporary directory, and every declared part is still where it expects. |
| `has_duplicate_members` | all ZIP | The same part twice with different content. `zipfile` resolves a name to the last entry, other readers take the first; a package whose text depends on the reader has no text. |
| `has_content_types` | OOXML | `[Content_Types].xml` present. Its absence makes the ZIP not an OOXML package, while `detect_format` still answers `docx` — it identifies from `word/document.xml`. |
| `has_entity_declaration` | XML-bearing | A `DOCTYPE` or entity declaration reached the parser. Measured: `xml.etree.ElementTree.fromstring` expands internal entities silently. |
| `has_external_entity` | XML-bearing | XXE. Refused by `expat` today by default, which makes it the half that is safe by accident. |
| `has_deep_nesting` | XML-bearing | Nesting past a reader's recursion limit, in valid entity-free XML — a cost a C parser pays as a stack overflow rather than an exception. |
| `has_byte_order_mark` | XML-bearing | Legal, and Word writes one. The case a reader gets wrong by being too strict. |
| `has_invalid_encoding` | all | Bytes that do not decode as declared. Already a `PERMANENT` classification for text (`probe._probe_text`); an office part deserves the same answer. |
| `has_extreme_compression` | all ZIP | The declared uncompressed size over the compressed size, read from the central directory *before* decompressing — which is the only place an extraction budget can be decided. |
| `declared_type_disagrees` | all | `FormatDetection.mime_agrees` is False. Probe already computes this and puts it on the file block rather than in `patterns`; promoting it would make it schedulable, which `INGEST_SPEC.md` 11.2 requires of anything that changes what runs. |
| `compression_methods`, `member_count` | all ZIP | Measurements, not flags. Corpus instrumentation, next to Part 2's existing `part_count` / `unknown_parts`. |

`is_damaged` is **not** in that list. Probe already emits it for PDF, with a meaning that
generalises exactly — *bytes arrived and they do not open as the thing they claim to be* —
and `truncated.docx`, `central-directory-mismatch.docx` and `corrupt-deflate.docx` are tagged
with it today. What
is missing is not a name; it is a ZIP prober that sets it.

---

## Tiers 3 and 4

Part 10's table has four tiers. Tiers 1 and 2 are here and run in the default suite. Tier 3
(reopen with an independent reader) and tier 4 (render before and after, compare SSIM) need
`pandoc` or LibreOffice and belong in the office-badged CI job described in
`OFFICE_SPEC.md` Part 1. They are not built, and the reason to note it here is that the
tier-1/2 diff is shaped for them: `PackageDiff` reports what changed between two packages,
and a tier-3 check is the same comparison with a different reader's output on one side.

## Property-based round trips

Part 10 also names `hypothesis` over generated edit sequences. Not built, and it has a
prerequisite that does not exist yet: the edit verb is Part 11 step 10. The place it slots
in is visible in `tests/corpus/test_roundtrip.py` — `edit_one_part` is a deliberately crude
stand-in, and the assertions around it are already the invariant a property test would
generalise. `hypothesis` is in no extra today; adding it is a dependency decision.
