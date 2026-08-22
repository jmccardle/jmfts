"""Nothing that ships names one machine.

JMFTS is meant to be forked, installed, and run by someone else, so a LAN address, a
home directory, or this developer's data mount baked into a shipped file is a defect. It
makes the package work in exactly one place and fail quietly everywhere else. Commit
3b43cec fixed two such details by hand; this test is what stops the third.

Scope
-----
``SHIPPED`` is what a user installs from or copies configuration out of. It is
deliberately narrower than the repository: ``docs/`` and ``benchmarks/`` are dated
records of runs against a specific machine, and the address there is the *data* —
rewriting it would be falsifying a measurement.

``scripts/`` is in scope, because the README and CLAUDE.md tell a reader to run things
from it. Sixteen research scripts in it do hardcode this machine's dataset mount, and
they are listed in ``KNOWN_UNFIXED`` rather than quietly excluded. That list is held
against the tree by ``test_known_unfixed_is_current``: fix a script and the test fails
until you delete its line, so the carve-out can only ever shrink.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: A LAN address, this developer's home directory, the internal git host, or the data
#: mount only this machine has.
LEAKS = re.compile(r"192\.168\.\d+\.\d+|/home/john\b|dev\.ffwf\.net|/storage/")

#: Trees a user installs from or copies out of. Paths are matched as prefixes of the
#: repository-relative path, so a bare filename matches that one file.
SHIPPED = (
    # The workflows. They are published (test_readme_links.py::PUBLISHED) and they are
    # the files most likely to grow a hostname, because CI is where addresses live. This
    # tree's `origin` is the internal git remote; a workflow naming it would ship that
    # name to the public repository and to anyone who forks it.
    ".github/",
    "jmfts_core/",
    "jmfts_batch/",
    "plugin/",
    "deploy/",
    "scripts/",
    "pyproject.toml",
    "README.md",
    "CLAUDE.md",
    ".env.example",
    "Dockerfile",
    "Dockerfile.worker",
    "docker-compose.yml",
)

#: This file names the patterns it forbids, so it cannot hold itself.
ALLOWED = {"tests/test_no_host_addresses.py"}

#: Research scripts that still hardcode this machine's dataset mount. Each one needs the
#: location to come from the environment (a ``JMFTS_DATA_DIR``) instead of a literal.
#: Until then they are named here, not excluded — a reader can see exactly what is
#: unfixed, and ``test_known_unfixed_is_current`` forces this list to shrink.
KNOWN_UNFIXED = {
    "scripts/batch_summarize_xsum.py",
    "scripts/benchmark_centroid_recall.py",
    "scripts/benchmark_multihop.py",
    "scripts/benchmark_overnight.py",
    "scripts/benchmark_twostage.py",
    "scripts/benchmark_write.py",
    "scripts/create_ivf_index.py",
    "scripts/embedding_similarity.py",
    "scripts/ingest_missing_steelman.py",
    "scripts/maxsim_token_demo.py",
    "scripts/ops_check.py",
    "scripts/reembed_corpus.py",
    "scripts/test_all_gguf.sh",
    "scripts/test_summarization.py",
    "scripts/token_selection_experiment.py",
    "scripts/wiki_ingest_pdf.py",
}


def _tracked() -> list[str]:
    """Every tracked path. Needs a work tree — a tarball is not enough."""
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [p for p in out.split("\0") if p]


def _shipped_paths() -> list[str]:
    return [
        p
        for p in _tracked()
        if any(p.startswith(prefix) for prefix in SHIPPED) and p not in ALLOWED
    ]


def _leaks_in(rel: str) -> list[str]:
    """Every offending line in one file, as ``path:lineno: text``."""
    try:
        text = (REPO / rel).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        return []
    return [
        f"{rel}:{n}: {line.strip()}"
        for n, line in enumerate(text.splitlines(), start=1)
        if LEAKS.search(line)
    ]


def test_no_shipped_file_names_one_machine():
    found: list[str] = []
    for rel in _shipped_paths():
        if rel in KNOWN_UNFIXED:
            continue
        found += _leaks_in(rel)
    assert not found, (
        "shipped files naming one machine:\n"
        + "\n".join(found)
        + "\n\nRewrite the path or address so it is a placeholder or comes from the "
        "environment. Do NOT add the file to KNOWN_UNFIXED — that list is for the "
        "sixteen research scripts already there, and it only shrinks."
    )


def test_known_unfixed_is_current():
    """A fixed script must leave the list, and a listed script must still exist.

    Without this the carve-out would outlive the defect it names, and the next reader
    would believe sixteen scripts are broken when three of them are fine.
    """
    tracked = set(_tracked())
    stale: list[str] = []
    for rel in sorted(KNOWN_UNFIXED):
        if rel not in tracked:
            stale.append(f"{rel} — no longer tracked; delete this line")
        elif not _leaks_in(rel):
            stale.append(f"{rel} — no longer names one machine; delete this line")
    assert not stale, "KNOWN_UNFIXED is out of date:\n" + "\n".join(stale)
