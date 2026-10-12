"""The README is the PyPI project page, so it may not point at files nobody can see.

``readme = "README.md"`` in pyproject.toml means this file is uploaded verbatim and
rendered as the project page. It is also the landing page of the public repository,
which is a **subset** of this tree. Until 0.1.1 the README linked into four paths that
subset did not carry, so a reader arriving from either place followed seven dead links.
Three of those four have since been published; ``jmfts-needle/`` and ``benchmarks/`` have
not, and neither has the held-back half of ``docs/`` — see ``PUBLISHED`` below.

``PUBLISHED`` below is the list of top-level paths the release copies into the public
repository. ``docs/RELEASING.md`` cites this constant rather than repeating it, so the
publish step and this test cannot disagree about what "published" means.

Scope: inline code spans only — the ```like this``` form. Prose slashes
("search/ingest/read skills") and fenced command blocks are not scanned, because
neither reads as a link and both are full of tokens that only look like paths.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = (REPO / "README.md").read_text(encoding="utf-8")

#: Top-level paths the release copies into the public repository. Everything else in
#: this tree stays internal. Adding a path here is a decision about what the world sees.
#:
#: Both of the paths this list was once waiting on have landed, and
#: ``test_published_paths_all_exist`` refuses a path that is gone.
#:
#: ``.github/`` is published because PyPI Trusted Publishing binds an OIDC identity to
#: (owner, repository, workflow filename, environment), so ``publish.yml`` has no identity
#: to exchange anywhere but the public repository. A publish workflow that stayed internal
#: would be a workflow that cannot publish.
#:
#: ``jmfts-client/`` is published because it is a SECOND distribution living in this tree,
#: not a package of the first: it has its own ``pyproject.toml``, its own version, and only
#: two dependencies. The release copies it because ``jmfts`` depends on it — a public
#: repository carrying the server without the contracts it imports would not build.
#:
#: ``.githooks/`` and ``bump-version.sh`` are published because the public tree is what a
#: contributor clones. A gate they cannot run, and a release step they would have to do by
#: hand, are both worse than the two small files.
#:
#: **This constant describes rather than drives, from 0.5.1 on.** It was the copy list for a
#: release step that built the public tree out of an internal one, and that step no longer
#: runs: development happens in the public repository. It is kept because it is still the
#: definition of "what the world sees" that the tests in this file are written against, and because the held-back half of
#: ``docs/`` is still held back. Adding a path here remains a decision about what the world
#: sees; what changed is that the decision now takes effect by committing the file rather
#: than by a copy.
#:
#: **And it is one-way for a ``docs/`` file.** ``docs/SPRINT_0_6_0.md`` question 4.5 says so
#: where it decided which of them to publish: publishing later is always available,
#: unpublishing is not. Ten landed on 2026-09-15 and three sprint plans are held
#: permanently, which is why the entries below carry dates and reasons rather than only
#: paths.
PUBLISHED = (
    ".dockerignore",
    ".env.example",
    ".github/",
    ".githooks/",
    ".gitignore",
    "CHANGELOG.md",
    "CLAUDE.md",
    "Dockerfile",
    "Dockerfile.worker",
    "LICENSE",
    "README.md",
    # Published 2026-09-13, and it took the then internal-tree marker with it — the marker
    # WAS this file. `CHANGELOG.md` is still the account of what shipped and this is still
    # the account of what has not; what changed is that the second one is no longer a reason
    # to keep a reader out. The dated status narrative and the archive index did not come
    # with it, because `CHANGELOG.md` carries the first and the second points at a file that
    # is still internal.
    "ROADMAP.md",
    "bump-version.sh",
    "deploy/",
    "docker-compose.yml",
    # The only GENERATED part of docs/ that ships, and it ships because none of it is
    # written by hand: `scripts/generate_reference.py` renders all three pages from the
    # registries the appliance reads at runtime, and `tests/test_reference_docs.py` refuses
    # a stale or hand-written one. It was the ONLY part of docs/ that shipped until
    # 2026-09-13; these say WHAT the appliance accepts, and the specifications below now say
    # WHY beside them.
    "docs/reference/",
    # The CURRENT sprint plan, and only the current one. `ROADMAP.md` above says what is
    # open and this says what is being done about it in this release; a reader who can see
    # the first and not the second can see that a decision was made and not what it was.
    # The plans for releases already cut stay internal PERMANENTLY, which stopped being
    # "until they have had a review pass" on 2026-09-13 — `docs/SPRINT_0_6_0.md` question
    # 4.5 is the decision and its reasoning. So this file cites `docs/SPRINT_0_4_0.md` and
    # `docs/SPRINT_0_5_0.md` the way every other citation to a held-back document works,
    # and says so where it does it.
    "docs/SPRINT_0_6_0.md",
    # The specifications shipped code cites, published 2026-09-15 — `SPRINT_0_6_0.md`
    # Block E step 4, deciding 4.5. 530 citations from files in this list name these three
    # (read 2026-10-12; 500 on 2026-09-15), and until then every one of them resolved to
    # nothing. Each carries a dated
    # review note at its head saying what in it had gone false and was corrected; the
    # review pass is the entry condition, not the citation count.
    #
    # They cite `docs/SPRINT_0_3_0.md`, `docs/SPRINT_0_4_0.md` and `docs/SPRINT_0_5_0.md`,
    # which stay held. That is expected and those citations must not be deleted — `CLAUDE.md`
    # calls them the anchors the documents would be republished against, and for these three
    # the anchor is now permanent rather than pending.
    "docs/INGEST_SPEC.md",
    "docs/OFFICE_SPEC.md",
    "docs/SPRINT_JOBS.md",
    # The measurement records, published 2026-09-15 — Block E step 5. Each one is the
    # evidence behind a decision the code carries, and three of 0.6.0's own Part 4 questions
    # cite them for their numbers. `docs/STRESS_CORPUS.md` was redacted in the same commit:
    # it measures a corpus of the author's own documents on a named machine, and neither the
    # machine's name nor two of the filenames survive the review pass.
    "docs/ANN_INDEX_HEALTH.md",
    "docs/CORPUS.md",
    "docs/MEASURE_BM25_BOUNDARY.md",
    # Committed 2026-09-15 by `b9fcfee` as a new record, outside the Block E pass and without
    # an entry here, and on the public remote ever since — so it was published already and
    # this line describes that rather than deciding it, which is what this list does from
    # 0.5.1 on. Its review note is dated 2026-10-12. Cited from `scripts/` and from
    # `docs/SPRINT_0_6_0.md` step 8.
    "docs/MEASURE_MAXSIM_MARGIN.md",
    "docs/MEASURE_SHACL_SCOPE.md",
    "docs/MEASURE_TYPED_WALK.md",
    "docs/STRESS_CORPUS.md",
    "jmfts-client/",
    # The THIRD distribution, added 2026-09-13 with `docs/SPRINT_0_6_0.md` Block F step 19.
    # Published for the reason `jmfts-client/` is: it is a distribution living in this tree,
    # with its own `pyproject.toml` and its own version, and `jmfts[web]` pins it with `==`.
    # A public repository carrying the extra without the thing it pins would not install.
    #
    # It was missed when step 19 ran — that step added the directory, the extra, the version
    # in `bump-version.sh` and the matrix entry in `publish.yml`, and not this constant. The
    # miss is exactly the one the `PUBLISHED` docstring warns about: the list is the
    # definition four tests are written against, so a distribution absent from it is
    # internal by that definition no matter how many other files name it.
    "jmfts-web/",
    "jmfts_batch/",
    "jmfts_core/",
    "plugin/",
    "pyproject.toml",
    "scripts/",
    "tests/",
)

#: There is no ``NOT_PUBLISHED`` list any more. Its one entry, ``scripts/deadcode_scan.py``,
#: was held back as an internal audit and was restored and published on 2026-10-12
#: (66778c0); publishing is one-way, so the list emptied and went, and with it
#: ``conftest``'s internal-tree marker and the two tests that held the pair together.

#: Suffixes that make a token without a slash still a file reference.
FILE_SUFFIXES = {
    ".md",
    ".py",
    ".sh",
    ".sql",
    ".yml",
    ".yaml",
    ".toml",
    ".json",
    ".example",
    ".lock",
}

#: Tokens that contain a slash and are not repository paths. Each one is here because
#: it would otherwise read as a dangling link.
NOT_PATHS = {
    "pgvector/pgvector",  # a Docker image name, cut at the ':' tag separator
    "and/or",
    # A Hugging Face Hub model id — the JMFTS_RERANKER_MODEL default. Same category as
    # the Docker image above: an identifier on somebody else's namespace that happens to
    # be spelled owner/name.
    "cross-encoder/ms-marco-MiniLM-L-6-v2",
}

#: Single-backtick spans. Double-backtick spans and fenced blocks are not matched.
INLINE_CODE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")


def _candidate_paths() -> list[str]:
    """Every inline code span that reads as a path into this repository."""
    found: list[str] = []
    for span in INLINE_CODE.findall(README):
        for raw in span.split():
            # Trailing punctuation only. A LEADING dot is part of the name —
            # stripping it turned `.env.example` into a dangling `env.example`.
            token = raw.lstrip("\"'(").rstrip("\"'(),;.").removeprefix("./")
            if not token or token.startswith("/") or ":" in token:
                # An absolute path here is an API route (/search, /openapi.json); a
                # colon is a Docker tag, a module target, or a pipeline name.
                continue
            if token in NOT_PATHS:
                continue
            has_suffix = any(token.endswith(suffix) for suffix in FILE_SUFFIXES)
            if "/" not in token and not has_suffix:
                continue
            if "." in token.split("/")[0] and "/" not in token and not has_suffix:
                continue  # a hostname, e.g. cdn.jsdelivr.net
            found.append(token)
    return sorted(set(found))


def test_every_path_the_readme_names_exists():
    dangling = [token for token in _candidate_paths() if not (REPO / token).exists()]
    assert not dangling, (
        "the README names paths that are not in this tree:\n"
        + "\n".join(dangling)
        + "\n\nFix the path, or add it to NOT_PATHS if it is not a repository path."
    )


def test_every_path_the_readme_names_is_published():
    """A link the PyPI reader cannot follow is worse than no link at all."""
    unpublished = [
        token
        for token in _candidate_paths()
        if (REPO / token).exists() and not any(token == p or token.startswith(p) for p in PUBLISHED)
    ]
    assert not unpublished, (
        "the README links into paths the public tree does not carry:\n"
        + "\n".join(unpublished)
        + "\n\nEither state the content inline instead of linking, or add the path to "
        "PUBLISHED — which is a decision about what the world sees, not a test fix."
    )


def test_published_paths_all_exist():
    """A publish step that copies a path which is gone would fail mid-release."""
    missing = [p for p in PUBLISHED if not (REPO / p.rstrip("/")).exists()]
    assert not missing, (
        "PUBLISHED names paths that are not in this tree: "
        + ", ".join(missing)
        + " — delete them, or create them before the next release."
    )
