"""jmfts-web — the browser front end for a JMFTS appliance, as its own distribution.

IC-5, ``docs/SPRINT_0_6_0.md`` Block F step 19. This package holds one thing: a directory of
built static files, and the function that says where it is. There is no Python logic here and
there is not meant to be — the appliance mounts :func:`static_dir` and serves it, and
everything the front end does it does by calling the same REST API any other client calls.

**Why this is a third distribution rather than a directory inside ``jmfts_core``.** An extra
guards dependencies. Static files inside the server package ship with the appliance whether or
not an extra names them, so ``jmfts[web]`` over a bundle in the main wheel would have been a
flag that guards nothing. ``Dockerfile.worker`` builds a worker that drains the ingest queue
and never serves a page; it should not carry a UI it cannot use. Question 4.8 of that sprint
plan records the cost that was accepted for this: a third version in ``bump-version.sh``, a
step in ``docs/RELEASING.md``, and a third case in ``.github/workflows/publish.yml``.

**The version tracks the other two exactly.** One number, three wheels, one tag. The front end
is generated against the appliance's own route table, so a client one release behind its server
is a client that renders controls for operations that have moved.

``jmfts_core.rest.main`` imports this package inside a ``try``: a base install has no
``jmfts_web`` and must start anyway, with no ``/app`` route rather than a broken one.
"""

from __future__ import annotations

from pathlib import Path

__version__ = "0.5.1"

__all__ = ["static_dir", "index_path", "__version__"]

#: The bundle, relative to this file. ``tests/test_web_distribution.py`` refuses a
#: distribution whose bundle is missing.
#:
#: **There is no build step yet and that is not an omission.** At step 19 the bundle is one
#: hand-written document, because one document is what it takes to prove the mount, the
#: extra and the release plumbing all work. Block F step 24 adds the source tree and the
#: build that writes here; until there is something to compile, a build script would be a
#: file that runs and does nothing.
_STATIC = Path(__file__).resolve().parent / "static"


def static_dir() -> Path:
    """The directory to mount, as an absolute path.

    Raises ``FileNotFoundError`` when the bundle is absent rather than returning a path that
    does not exist. A ``StaticFiles`` mount over a missing directory raises at mount time in
    some Starlette versions and answers 404 for every asset in others; neither is a state the
    appliance should boot into quietly, because both look exactly like "the front end is
    broken" to whoever opens it.
    """
    if not _STATIC.is_dir():
        raise FileNotFoundError(
            f"jmfts-web is installed but its bundle is missing at {_STATIC}. "
            "This is a packaging fault, not a configuration one: run jmfts-web/build.sh "
            "in a source checkout, or reinstall the wheel."
        )
    return _STATIC


def index_path() -> Path:
    """The single-page entry document.

    Named separately from :func:`static_dir` because the mount serves assets by path and the
    application needs one document served for every unmatched route — a front end with client
    -side routing answers ``/app/search`` from the same file as ``/app/``.
    """
    index = static_dir() / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"jmfts-web's bundle has no index.html at {index}.")
    return index
