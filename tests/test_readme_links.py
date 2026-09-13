"""The README is the PyPI project page, so it may not point at files nobody can see.

``readme = "README.md"`` in pyproject.toml means this file is uploaded verbatim and
rendered as the project page. It is also the landing page of the public repository,
which is a **subset** of this tree: ``docs/``, ``benchmarks/``, ``ROADMAP.md`` and
``jmfts-needle/`` are not published. Until 0.1.1 the README linked into all four, so a
reader arriving from either place followed seven dead links.

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

from tests.conftest import INTERNAL_TREE_MARKER, internal_tree_only

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
#: definition of "what the world sees" that four tests in this file and
#: ``conftest.INTERNAL_TREE_MARKER`` are written against, and because the held-back half of
#: ``docs/`` is still held back. Adding a path here remains a decision about what the world
#: sees; what changed is that the decision now takes effect by committing the file rather
#: than by a copy.
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
    # Published 2026-09-13, and it took `conftest.INTERNAL_TREE_MARKER` with it — the marker
    # WAS this file. `CHANGELOG.md` is still the account of what shipped and this is still
    # the account of what has not; what changed is that the second one is no longer a reason
    # to keep a reader out. The dated status narrative and the archive index did not come
    # with it, because `CHANGELOG.md` carries the first and the second points at a file that
    # is still internal.
    "ROADMAP.md",
    "bump-version.sh",
    "deploy/",
    "docker-compose.yml",
    # The ONLY part of docs/ that ships, and it ships because none of it is written by
    # hand: `scripts/generate_reference.py` renders all three pages from the registries the
    # appliance reads at runtime, and `tests/test_reference_docs.py` refuses a stale or
    # hand-written one. The working record in `docs/` explains WHY the appliance is shaped
    # the way it is and stays internal; these say WHAT it accepts, which is what an
    # integrator needs and what the held-back citations currently deny them.
    "docs/reference/",
    # The CURRENT sprint plan, and only the current one. `ROADMAP.md` above says what is
    # open and this says what is being done about it in this release; a reader who can see
    # the first and not the second can see that a decision was made and not what it was.
    # The plans for releases already cut stay internal until they have had the review pass
    # the README's note describes — so this file cites `docs/SPRINT_0_4_0.md` and
    # `docs/SPRINT_0_5_0.md` the way every other citation to a held-back document works,
    # and says so where it does it.
    "docs/SPRINT_0_6_0.md",
    "jmfts-client/",
    "jmfts_batch/",
    "jmfts_core/",
    "plugin/",
    "pyproject.toml",
    "scripts/",
    "tests/",
    "uv.lock",
)

#: Carved back OUT of a published directory. A path here is inside something ``PUBLISHED``
#: names, and is still not copied.
#:
#: The list exists because ``PUBLISHED`` is directory-granular and one file needed to be
#: finer than that. Keep it short: an exclusion is a thing the copy step has to remember,
#: and the reason ``PUBLISHED`` is a constant rather than a habit is that the copy step
#: should not have to remember anything.
#:
#: ``scripts/deadcode_scan.py`` is an occasional internal audit. It shells out to
#: ``vulture``, which is deliberately not a project dependency, and it imports the app to
#: read its registries — so on a public clone it is a script that fails with an install
#: hint for a tool the project never asks for. ``docs/`` is where its findings are acted
#: on, and ``docs/`` is internal.
NOT_PUBLISHED = ("scripts/deadcode_scan.py",)

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


def test_the_internal_tree_marker_is_not_published():
    """``conftest.IS_INTERNAL_TREE`` is a claim about this list, so hold it against it.

    The marker is the path whose presence means "development tree". If a release adds that
    path to ``PUBLISHED``, the marker reads True in the public tree and every test
    ``internal_tree_only`` skips runs there — which is how 0.5.0 broke it: the marker was
    ``docs/`` and this release published ``docs/reference/``.

    This test runs in BOTH trees, deliberately. Nothing in the old arrangement failed at
    the moment the premise stopped being true; the failure arrived a release later, in
    public CI, as four tests nobody had changed.
    """
    covered = [
        p for p in PUBLISHED if INTERNAL_TREE_MARKER == p or INTERNAL_TREE_MARKER.startswith(p)
    ]
    assert not covered, (
        f"conftest.INTERNAL_TREE_MARKER is {INTERNAL_TREE_MARKER}, which PUBLISHED now "
        f"carries via {covered}. The public tree will read IS_INTERNAL_TREE as True and "
        "run every internal_tree_only test. Point the marker at something the release "
        "does not copy — that is the decision, not this assertion."
    )


@internal_tree_only
def test_excluded_paths_still_exist_and_sit_inside_a_published_one():
    """An exclusion that stopped matching is an exclusion that silently stopped working.

    Rename or delete the file and this list keeps carving out a path that is not there,
    so the next release publishes whatever took its place. Both halves are asserted: the
    path is real, and it is genuinely inside something ``PUBLISHED`` copies — because an
    entry that excludes nothing is a note pretending to be a rule.

    Internal tree only, and the reason is the rule working. In a public checkout every
    ``NOT_PUBLISHED`` path is absent BY CONSTRUCTION, so asserting it exists there asks
    the release to prove it did not do the thing it was told to do. The first public CI
    run failed on exactly that, which is a better demonstration of the mechanism than the
    test was — and so did the first public CI run of 0.5.0, because the marker itself had
    stopped distinguishing the two trees. See
    ``test_the_internal_tree_marker_is_not_published`` above.
    """
    for path in NOT_PUBLISHED:
        assert (REPO / path).exists(), (
            f"NOT_PUBLISHED names {path}, which is not in this tree. Drop the entry, or "
            "point it at wherever that file went."
        )
        assert any(path.startswith(p) for p in PUBLISHED if p.endswith("/")), (
            f"NOT_PUBLISHED names {path}, which no PUBLISHED directory contains. It was "
            "never going to be copied, so excluding it says nothing."
        )
