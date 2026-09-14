"""IC-5: the front end is a third distribution, and the appliance runs without it.

``docs/SPRINT_0_6_0.md`` Block F step 19, and question 4.8 for why it is a distribution
rather than a directory under ``jmfts_core``. The short version: an extra guards
dependencies, so static files inside the server wheel would ship whether or not an extra
named them, and ``jmfts[web]`` would have been a flag that guards nothing.

**Every assertion here reads the TREE, not the installed environment.** ``jmfts-web`` is not
installed in the development venv and should not be — ``dev`` does not imply ``web``, because
the suite does not open a browser. What these tests hold is that the three distributions agree
about the version, that the bundle is real, and that the mount is conditional in both
directions.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "jmfts-web"


def _version_literal(path: Path) -> str:
    """The ``__version__`` a distribution's package declares."""
    match = re.search(r'^__version__ = "([^"]+)"$', path.read_text(encoding="utf-8"), re.M)
    assert match, f"{path} has no __version__ line; bump-version.sh names it structural."
    return match.group(1)


def test_the_three_distributions_carry_one_version():
    """One number, three wheels, one tag.

    ``bump-version.sh`` writes all three and refuses a partial bump, so a disagreement here
    means somebody edited a literal by hand — which is the failure that script exists to
    prevent and the one that is only otherwise visible after a wheel is on PyPI.
    """
    versions = {
        "jmfts": _version_literal(REPO / "jmfts_core" / "__init__.py"),
        "jmfts-client": _version_literal(REPO / "jmfts-client" / "jmfts_client" / "__init__.py"),
        "jmfts-web": _version_literal(WEB / "jmfts_web" / "__init__.py"),
    }
    assert len(set(versions.values())) == 1, f"the distributions disagree: {versions}"


def test_the_web_extra_pins_the_exact_version():
    """``jmfts[web]`` must not resolve a different release of the front end from PyPI."""
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    web = config["project"]["optional-dependencies"]["web"]
    expected = _version_literal(WEB / "jmfts_web" / "__init__.py")
    assert web == [f"jmfts-web=={expected}"], web


def test_bump_version_knows_about_the_third_distribution():
    """A release step that silently skips a wheel is the thing this repo keeps guarding.

    Asserted against the script's text rather than by running it: the script refuses to work
    on a dirty tree, which is correct behaviour and makes it awkward to exercise from a
    suite. What can be held here is that the file names the third version literal at all.
    """
    script = (REPO / "bump-version.sh").read_text(encoding="utf-8")
    assert "jmfts-web/jmfts_web/__init__.py" in script
    # The pin pattern has to match both names. A pattern that lost `web` would report a
    # successful bump having rewritten one pin of two.
    assert "jmfts-(client|web)" in script


def test_the_web_distribution_declares_no_dependencies():
    """The whole reason the extra is worth having.

    A dependency here is a dependency the appliance inherits from ``jmfts[web]``, and an
    ingest worker that will never serve a page would pay for it.
    """
    config = tomllib.loads((WEB / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["dependencies"] == []


def test_the_bundle_is_shipped_as_package_data():
    """``packages.find`` collects Python; the bundle is not Python.

    Without a ``package-data`` entry the wheel installs a module whose ``static_dir()``
    raises on every call — which that function reports clearly and which nobody should ever
    have to read.
    """
    config = tomllib.loads((WEB / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["jmfts_web"]
    assert any("static" in p for p in patterns), patterns
    assert (WEB / "jmfts_web" / "static" / "index.html").is_file()


def test_static_dir_refuses_to_return_a_path_that_is_not_there(tmp_path, monkeypatch):
    """A damaged install is a different case from an absent one, and raises.

    ``jmfts_core.rest.main`` swallows ``ImportError`` — a base install with no front end has
    no problem to report. It does NOT swallow this: a package that is installed and whose
    bundle is missing is a packaging fault, and a ``StaticFiles`` mount over a missing
    directory answers 404 for every asset, which looks exactly like a broken front end to
    whoever opens it.
    """
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "jmfts_web_under_test", WEB / "jmfts_web" / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["jmfts_web_under_test"] = module
    try:
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_STATIC", tmp_path / "not-built")
        with pytest.raises(FileNotFoundError, match="packaging fault"):
            module.static_dir()
    finally:
        del sys.modules["jmfts_web_under_test"]


def test_the_appliance_starts_with_no_front_end_installed():
    """The control case, and it is the environment this suite actually runs in.

    ``jmfts-web`` is not a development dependency, so importing the app here exercises the
    ``except ImportError: pass`` branch. No ``/app`` route, and no failure.
    """
    from jmfts_core.rest.main import app

    mounted = [getattr(r, "path", "") for r in app.routes]
    assert not any(p.startswith("/app") for p in mounted), (
        "jmfts-web appears to be installed in this environment. The mount is correct, but "
        "this test can no longer prove the appliance runs without it."
    )
