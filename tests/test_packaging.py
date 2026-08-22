"""What ships has to be what runs.

An installed wheel is not the tree it was built from. Two things go missing between
them, and both install cleanly and die on first use — the worst kind of release:

1. **Non-``.py`` files the code reads at runtime.** ``jmfts-init-db`` loads
   ``jmfts_core/sql/schema.sql`` and every file in ``jmfts_core/sql/migrations/``. They
   sat at the repository root until 0.1.1, outside every ``packages.find`` include, so
   the wheel carried the ORM models and nothing that could create their tables.
2. **Metadata that only matters once the wheel is on an index.** A missing ``readme``
   is a blank PyPI page. A ``license-files`` key under setuptools < 77.0.3 is ignored,
   and the wheel ships with no licence text at all.

So the declarations are held against the tree.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

#: The importable trees this distribution ships.
PACKAGE_ROOTS = ("jmfts_core", "jmfts_batch")

#: PEP 440, restricted to the forms this project releases.
VERSION = re.compile(r"^\d+\.\d+\.\d+((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")


def _runtime_data_files() -> list[Path]:
    """Every non-.py file under a shipped package tree."""
    found: list[Path] = []
    for root in PACKAGE_ROOTS:
        found += [
            p
            for p in (REPO / root).rglob("*")
            if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts
            # ``*.egg-info/`` is build metadata setuptools writes on any editable
            # install — the very install CLAUDE.md tells you to do. It is generated,
            # gitignored, and never shipped, so holding it against package-data would
            # assert something this test does not mean.
            and not any(part.endswith(".egg-info") for part in p.parts)
        ]
    return found


def _owning_package(path: Path) -> tuple[str, str]:
    """Return ``(dotted package name, path relative to that package's directory)``.

    ``package-data`` keys are dotted package names and their patterns are relative to
    the package directory, so ``jmfts_core/sql/migrations/011_x.sql`` is declared by
    package ``jmfts_core.sql`` with pattern ``migrations/*.sql``. The owning package is
    the nearest ancestor directory holding an ``__init__.py``.
    """
    directory = path.parent
    while not (directory / "__init__.py").is_file():
        directory = directory.parent
        if directory == REPO:  # pragma: no cover - a data file outside any package
            raise AssertionError(f"{path} is under no package")
    dotted = ".".join(directory.relative_to(REPO).parts)
    return dotted, path.relative_to(directory).as_posix()


def _matches(pattern: str, relative: str) -> bool:
    """setuptools glob semantics: ``*`` matches within one path segment only."""
    pattern_parts = pattern.split("/")
    relative_parts = relative.split("/")
    if len(pattern_parts) != len(relative_parts):
        return False
    return all(fnmatch.fnmatch(r, p) for p, r in zip(pattern_parts, relative_parts))


def test_every_runtime_data_file_is_declared_as_package_data():
    declared = PYPROJECT["tool"]["setuptools"]["package-data"]
    missing: list[str] = []
    for path in _runtime_data_files():
        package, relative = _owning_package(path)
        patterns = declared.get(package, []) + declared.get("*", [])
        if not any(_matches(pattern, relative) for pattern in patterns):
            missing.append(f"{path.relative_to(REPO)} (no pattern for {package!r})")
    assert not missing, (
        "runtime data files a wheel would not contain:\n"
        + "\n".join(missing)
        + "\n\nAdd a pattern under [tool.setuptools.package-data]."
    )


def test_the_version_has_exactly_one_home():
    project = PYPROJECT["project"]
    assert "version" not in project, (
        "pyproject.toml declares a literal version. It is read from "
        "jmfts_core.__version__ instead, so a literal here is a second copy that will "
        "disagree with the code. Delete it; bump jmfts_core/__init__.py."
    )
    assert "version" in project.get(
        "dynamic", []
    ), "project.dynamic must list 'version', or setuptools has nowhere to read it from."
    attr = PYPROJECT["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "jmfts_core.__version__", attr


def test_the_version_attribute_exists_and_is_pep440():
    import jmfts_core

    assert VERSION.match(jmfts_core.__version__), (
        f"jmfts_core.__version__ is {jmfts_core.__version__!r}, which is not a version "
        "this project releases (N.N.N, optionally aN/bN/rcN, .postN, .devN)."
    )


def test_the_metadata_files_exist():
    project = PYPROJECT["project"]
    missing = [
        name
        for name in [project["readme"], *project["license-files"]]
        if not (REPO / name).is_file()
    ]
    assert not missing, f"declared in pyproject.toml but not on disk: {missing}"


# --------------------------------------------------------------------------------------
# The client distribution. ``jmfts-client/`` is a SECOND distribution in this tree, with
# its own pyproject, its own version and its own wheel. Everything the server distribution
# is held to above, it is held to here — a release cuts both, so a defect in either is a
# defect in the release.

CLIENT = REPO / "jmfts-client"
CLIENT_PYPROJECT = tomllib.loads((CLIENT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_client_version_has_exactly_one_home():
    project = CLIENT_PYPROJECT["project"]
    assert "version" not in project, (
        "jmfts-client/pyproject.toml declares a literal version. It is read from "
        "jmfts_client.__version__ instead. Delete it; bump jmfts_client/__init__.py."
    )
    assert "version" in project.get("dynamic", []), "project.dynamic must list 'version'."
    attr = CLIENT_PYPROJECT["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "jmfts_client.__version__", attr


def test_the_client_version_is_pep440():
    import jmfts_client

    assert VERSION.match(jmfts_client.__version__), jmfts_client.__version__


def test_the_client_metadata_files_exist():
    project = CLIENT_PYPROJECT["project"]
    missing = [
        name
        for name in [project["readme"], *project["license-files"]]
        if not (CLIENT / name).is_file()
    ]
    assert not missing, f"declared in jmfts-client/pyproject.toml but not on disk: {missing}"


def test_the_client_setuptools_is_new_enough_for_license_files():
    """Same PEP 639 floor as the server. Below 77.0.3 the wheel ships with no licence."""
    requires = CLIENT_PYPROJECT["build-system"]["requires"]
    floor = next(r for r in requires if r.startswith("setuptools"))
    assert floor == "setuptools>=77.0.3", floor


def test_the_server_depends_on_the_client():
    """The dependency runs server → client, and the release depends on it staying that way.

    ``jmfts_core`` imports ``jmfts_client.contracts`` in dozens of modules. If this
    dependency were dropped, ``pip install jmfts`` would build a wheel that fails to import
    on a clean machine, and no test in this repository would notice — the source tree has
    the client sitting right beside it.
    """
    declared = PYPROJECT["project"]["dependencies"]
    assert any(
        d.replace("_", "-").startswith("jmfts-client") for d in declared
    ), f"jmfts must depend on jmfts-client; got {declared}"


def _client_pin() -> str:
    """The ``jmfts-client…`` requirement string from the server's dependencies."""
    declared = PYPROJECT["project"]["dependencies"]
    return next(d for d in declared if d.replace("_", "-").startswith("jmfts-client"))


def test_the_two_distributions_release_in_lockstep():
    """One number, two wheels — and the pin says the same number.

    The versions are deliberately NOT independent. ``jmfts-client/jmfts_client/_verbs.py``
    is generated from this server's resolved route table, so a client built from a
    different release was generated from a different surface. That mismatch does not fail
    at install time; it fails at call time, as a verb that is missing or whose parameters
    moved, which is the drift the whole generated-client design exists to prevent.

    ``./bump-version.sh`` moves all three locations at once. This test is what makes
    forgetting one of them fail here rather than on PyPI, where a version cannot be reused.
    """
    import jmfts_client
    import jmfts_core

    pin = _client_pin()
    assert "==" in pin, (
        f"the client pin is {pin!r}. Lockstep needs '==': a range would let pip resolve a "
        "client generated from a different server surface, and the mismatch appears on the "
        "wire rather than at install time."
    )
    pinned = pin.split("==", 1)[1].strip().strip('"')
    assert jmfts_core.__version__ == jmfts_client.__version__ == pinned, (
        "the three version locations disagree: "
        f"jmfts_core {jmfts_core.__version__}, jmfts_client {jmfts_client.__version__}, "
        f"pin {pinned}. Run ./bump-version.sh <version> rather than editing one of them."
    )


def test_setuptools_is_new_enough_for_license_files():
    """PEP 639 ``license-files`` is silently ignored below setuptools 77.0.3.

    Silently is the problem: the build succeeds and the wheel carries no licence.
    """
    requires = PYPROJECT["build-system"]["requires"]
    floor = next(r for r in requires if r.startswith("setuptools"))
    version = tuple(int(part) for part in floor.split(">=")[1].split("."))
    assert version >= (77, 0, 3), f"{floor} ignores license-files; need >=77.0.3"


def test_every_console_script_resolves():
    """A renamed module turns an entry point into a traceback on first run.

    The wheel records the string; nothing checks it until a user types the command.
    """
    import importlib

    broken: list[str] = []
    for name, target in PYPROJECT["project"]["scripts"].items():
        module_name, _, attribute = target.partition(":")
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - report, do not classify
            broken.append(f"{name} = {target!r}: importing {module_name} raised {exc!r}")
            continue
        if not callable(getattr(module, attribute, None)):
            broken.append(f"{name} = {target!r}: {module_name} has no callable {attribute}")
    assert not broken, "console scripts that would fail on first run:\n" + "\n".join(broken)


def test_the_server_entry_point_answers_help_without_binding():
    """``jmfts-server --help`` must print help and exit, not start a server.

    ``docs/RELEASING.md`` step 4 runs this as a smoke check of the built wheel. Before
    ``run()`` parsed arguments it ignored the flag and called ``uvicorn.run``, so that step
    either blocked on a serving process or "failed" because the port was already taken —
    neither of which says anything about whether the wheel is sound.

    Binding a port from a test would be flaky and rude, so this asserts the property that
    matters: argparse handles the flag and raises SystemExit(0) before uvicorn is reached.
    """
    from jmfts_core.rest.main import run

    with pytest.raises(SystemExit) as caught:
        run(["--help"])
    assert caught.value.code == 0
