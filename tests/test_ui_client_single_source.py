"""``jmfts-ui`` reads the ONE generated browser client; it does not carry a copy.

The standalone console (``jmfts-ui/``) and the bundled demonstration (``jmfts-web``) call the
appliance through the same client: the files ``scripts/generate_ts_client.py`` writes into
``jmfts-web/jmfts_web/static/client/``, which ``tests/test_ts_client_codegen.py`` holds
against the live route surface. ``jmfts-ui`` reaches them in place through an alias,
``@jmfts/client``, declared twice — once in ``vite.config.ts`` for the dev server and the
build, once in ``tsconfig.json`` for ``vue-tsc`` — so both read the same directory.

This exists because the first cut of ``jmfts-ui`` did not: it carried a hand copy of the ten
files, about nine thousand lines, taken before the token moved from ``sessionStorage`` to
``localStorage`` and before ``get_seeded_rank`` existed. Nothing checked the copy, so it had
drifted from the generator in eight of its ten files by the time anyone diffed it. A
generated file with two homes is one checked file and one that only looks checked.

Pure Python: it reads the tree and two config files and needs no JavaScript runtime. Whether
the alias actually resolves under ``vite`` and ``vue-tsc`` is what ``npm run build`` in
``jmfts-ui/`` answers; these tests answer whether the arrangement is still the arrangement.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from scripts.generate_ts_client import BANNER_JS, TARGET_DIR

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "jmfts-ui"

#: What the alias says, verbatim. Relative to ``jmfts-ui/``, because both config files are.
ALIAS = "@jmfts/client"
ALIAS_TARGET = "../jmfts-web/jmfts_web/static/client"

#: Installed or built output, not source. ``dist/`` legitimately holds the client — bundled
#: into one chunk, which is the build doing its job rather than a copy on disk.
_NOT_SOURCE = frozenset({"node_modules", "dist", ".vite"})

#: A module specifier: what follows ``from`` or sits inside ``import(…)``.
_IMPORT = re.compile(r"""(?:\bfrom\s+|\bimport\s*\(\s*|^\s*import\s+)["']([^"']+)["']""")

pytestmark = pytest.mark.skipif(not UI.is_dir(), reason="no jmfts-ui/ in this tree")


def _ui_source_files():
    for root, dirs, files in os.walk(UI):
        dirs[:] = [d for d in dirs if d not in _NOT_SOURCE]
        for name in files:
            yield Path(root) / name


def _client_files() -> dict[str, bytes]:
    """The live client directory, read rather than listed, so a file added to it is covered
    the day it lands. ``test_the_client_directory_holds_nothing_else`` bounds what that is."""
    return {p.name: p.read_bytes() for p in TARGET_DIR.iterdir() if p.is_file()}


def test_the_alias_target_is_the_generated_client():
    assert (UI / ALIAS_TARGET).resolve() == TARGET_DIR.resolve()
    assert TARGET_DIR.is_dir(), f"{TARGET_DIR} is missing; jmfts-ui cannot build without it"


def test_vite_resolves_the_alias_to_the_generated_client():
    config = (UI / "vite.config.ts").read_text()
    assert f'"{ALIAS}"' in config, f"vite.config.ts declares no {ALIAS} alias"
    assert f'"{ALIAS_TARGET}"' in config, f"vite.config.ts does not point {ALIAS} at {ALIAS_TARGET}"
    # Outside the project root, so the dev server refuses it unless it is allowed by name.
    assert re.search(r"fs:\s*\{\s*allow:", config), "the dev server's fs.allow is not set"


def test_the_type_checker_resolves_the_alias_to_the_generated_client():
    paths = json.loads((UI / "tsconfig.json").read_text())["compilerOptions"]["paths"]
    assert paths.get(f"{ALIAS}/*") == [f"{ALIAS_TARGET}/*"]


def test_jmfts_ui_carries_no_copy_of_the_generated_client():
    client = _client_files()
    by_content = {content: name for name, content in client.items()}
    copies = []
    for path in _ui_source_files():
        content = path.read_bytes()
        rel = path.relative_to(REPO)
        if content in by_content:
            copies.append(f"{rel} is byte-equal to the generated {by_content[content]}")
        elif BANNER_JS.encode() in content:
            copies.append(f"{rel} carries the generator's banner")
    # A STALE copy is equal to nothing and, for the hand-written runtime half, has no banner.
    # What it still has is the client's file names, together, in one directory.
    for root, dirs, files in os.walk(UI):
        dirs[:] = [d for d in dirs if d not in _NOT_SOURCE]
        shared = sorted(set(files) & set(client))
        if len(shared) >= 2:
            copies.append(f"{Path(root).relative_to(REPO)}/ holds the client's files {shared}")
    assert not copies, (
        "jmfts-ui must import the generated client through the @jmfts/client alias, not keep "
        "a copy that no check covers:\n  " + "\n  ".join(copies)
    )


def test_every_client_import_in_jmfts_ui_goes_through_the_alias():
    # A relative path into jmfts-web works too, and is a second, unchecked way to say the
    # same thing — the alias is the one place that knows where the client lives.
    stray = []
    for path in _ui_source_files():
        if path.suffix not in {".ts", ".vue", ".js", ".mjs"} or path.name == "vite.config.ts":
            continue
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            for spec in _IMPORT.findall(line):
                if spec.startswith(f"{ALIAS}/") or not spec.startswith((".", "@/")):
                    continue
                base = UI / "src" if spec.startswith("@/") else path.parent
                target = (base / spec.removeprefix("@/")).resolve()
                if not target.is_relative_to(TARGET_DIR.resolve()):
                    continue
                stray.append(f"{path.relative_to(REPO)}:{line_no}: {line.strip()}")
    assert not stray, "import the client as @jmfts/client/…:\n  " + "\n  ".join(stray)
