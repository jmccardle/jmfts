"""``jmfts-ui``'s dev server proxies to the appliance the repository starts, on every prefix.

``npm run dev`` in ``jmfts-ui/`` serves the console from Vite and proxies the appliance's
route prefixes to it, so the page is same-origin in development. Two things in
``vite.config.ts`` decide whether that works, and neither is checked by anything that runs
JavaScript:

- **The default target.** It was ``127.0.0.1:8110`` while ``docker-compose.yml``, ``CLAUDE.md``
  and the console's own ``index.html`` all said 8100, so following the README against the
  documented stack produced a console that could reach nothing. Nothing failed; every call
  was a proxy error.
- **The prefix list.** It is written out by hand, because a catch-all would swallow Vite's
  own ``/@vite`` and ``/src`` paths. A prefix missing from it is a page that works against a
  built bundle and 404s from Vite — ``/runner``, ``/config``, ``/docs`` and ``/redoc`` were.

Pure Python, in the manner of ``tests/test_ui_client_single_source.py``: it reads
``vite.config.ts`` and ``docker-compose.yml`` as text and imports the app for its OpenAPI
document, which needs no database.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "jmfts-ui"
VITE_CONFIG = UI / "vite.config.ts"
COMPOSE = REPO / "docker-compose.yml"

#: First path segments the appliance answers on that the dev proxy must NOT forward, and why.
#: Written here rather than inferred, so excluding one is a decision with a reason beside it.
EXCLUDED = {
    # The gated health check at the root. Proxying `/` would forward every path, Vite's own
    # module requests and the console's page included.
    "/": "a prefix of every path",
    # `jmfts-web`, mounted as static files. A different console; this one has no use for it,
    # and it is a Mount, so it is absent from the OpenAPI document anyway.
    "/app": "the bundled jmfts-web console",
}

pytestmark = pytest.mark.skipif(not UI.is_dir(), reason="no jmfts-ui/ in this tree")


def _default_target() -> str:
    match = re.search(r'env\.JMFTS_URL\s*\|\|\s*"([^"]+)"', VITE_CONFIG.read_text())
    assert match, 'vite.config.ts no longer reads `env.JMFTS_URL || "<default>"`'
    return match.group(1)


def _proxy_prefixes() -> set[str]:
    config = VITE_CONFIG.read_text()
    match = re.search(r"proxy:\s*Object\.fromEntries\(.*?\[(.*?)\]\.map\(", config, re.DOTALL)
    assert match, "vite.config.ts no longer builds its proxy from a literal list of prefixes"
    return set(re.findall(r'"(/[^"]*)"', match.group(1)))


def _compose_api_service() -> str:
    """The text of the ``api`` service in ``docker-compose.yml``, up to the next service."""
    text = COMPOSE.read_text()
    match = re.search(r"^  api:\n(.*?)(?=^  \S|^\S|\Z)", text, re.DOTALL | re.MULTILINE)
    assert match, "docker-compose.yml has no `api` service"
    return match.group(1)


def _compose_published_api_port() -> int:
    service = _compose_api_service()
    container = re.search(r"--port\s+(\d+)", service)
    assert container, "the compose `api` service no longer names the port uvicorn listens on"
    published = re.findall(rf'"(?:[\d.]+:)?(\d+):{container.group(1)}"', service)
    assert len(published) == 1, f"expected one published port for the API, found {published}"
    return int(published[0])


def _appliance_first_segments() -> set[str]:
    from jmfts_core.rest.main import app

    paths = set(app.openapi()["paths"])
    # The doc routes are plain Starlette routes, excluded from the document they serve.
    paths |= {p for p in (app.openapi_url, app.docs_url, app.redoc_url) if p}
    return {"/" + path.split("/")[1] for path in paths}


def test_the_default_target_is_the_compose_stacks_api():
    target = urlsplit(_default_target())
    assert target.hostname in {"127.0.0.1", "localhost"}, target
    assert target.port == _compose_published_api_port(), (
        f"vite.config.ts defaults to port {target.port}; docker-compose.yml publishes the API "
        f"on {_compose_published_api_port()}. Another instance is what JMFTS_URL is for."
    )


def test_the_readme_example_does_not_name_another_port():
    readme = (UI / "README.md").read_text()
    port = _compose_published_api_port()
    stray = sorted(set(re.findall(r"127\.0\.0\.1:(\d+)", readme)) - {str(port), "5183"})
    assert not stray, f"jmfts-ui/README.md names appliance port(s) {stray}; the stack is {port}"


def test_the_proxy_covers_every_prefix_the_appliance_answers_on():
    missing = sorted(_appliance_first_segments() - _proxy_prefixes() - set(EXCLUDED))
    assert not missing, (
        "vite.config.ts does not proxy these appliance prefixes, so the dev server answers them "
        "itself (404, or the console's page):\n  " + "\n  ".join(missing)
    )


def test_no_excluded_prefix_is_proxied():
    proxied = sorted(_proxy_prefixes() & set(EXCLUDED))
    assert not proxied, f"proxied despite EXCLUDED: {[(p, EXCLUDED[p]) for p in proxied]}"
