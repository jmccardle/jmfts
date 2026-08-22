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
PUBLISHED = (
    ".dockerignore",
    ".env.example",
    ".github/",
    ".githooks/",
    ".gitignore",
    "CLAUDE.md",
    "Dockerfile",
    "Dockerfile.worker",
    "LICENSE",
    "README.md",
    "bump-version.sh",
    "deploy/",
    "docker-compose.yml",
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


def test_excluded_paths_still_exist_and_sit_inside_a_published_one():
    """An exclusion that stopped matching is an exclusion that silently stopped working.

    Rename or delete the file and this list keeps carving out a path that is not there,
    so the next release publishes whatever took its place. Both halves are asserted: the
    path is real, and it is genuinely inside something ``PUBLISHED`` copies — because an
    entry that excludes nothing is a note pretending to be a rule.
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
