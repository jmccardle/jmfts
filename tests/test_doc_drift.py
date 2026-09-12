"""Guards for the facts the README and ``.env.example`` state about the code.

Three claims in this tree are numbers or names copied out of the source by hand, and each
one had drifted by 0.3.0:

* ``.env.example``'s header says "This file lists EVERY setting ``jmfts_core.config.Settings``
  reads ... Nothing is omitted", and it omitted ``JMFTS_INGEST_SYNC_TIMEOUT_SECONDS``.
* the README said the API has 109 operations; the route table had 114.
* the README said ``jmfts`` pins ``jmfts-client==0.2.1``; ``pyproject.toml`` said 0.3.0.

None of the three is a bug in the appliance and all three are read by somebody deciding
whether to use it. A prose promise about the code is a claim the code can check, so these
tests are where the claims are checked rather than a review habit.

Everything here reads files and registries. No database, no optional dependency.

Every file these tests read — ``README.md``, ``.env.example``, ``pyproject.toml`` — is in
``tests/test_readme_links.py::PUBLISHED``, so none of them is marked ``internal_tree_only``
and all of them run in the public repository too. They were marked until 0.5.0, and the
mark was wrong by ``conftest``'s own rule: it is for a test whose SUBJECT is internal, not
a way to excuse a test from the public gate. The public gate is where these claims matter
most, because the reader who checks them is reading the public README.
"""

from __future__ import annotations

import re
from pathlib import Path

from jmfts_core.config import Settings

REPO = Path(__file__).resolve().parents[1]

#: Settings fields deliberately absent from ``.env.example``, and why. Empty, and it is a
#: field for the exemption rather than a habit: an entry here has to state who reads the
#: name instead, the way ``PATTERNS_NOT_PROBED`` does for guard patterns.
SETTINGS_NOT_IN_ENV_EXAMPLE: dict[str, str] = {}

#: Names in ``.env.example`` that are NOT ``Settings`` fields. Both are read straight from
#: the environment by ``jmfts_core/worker.py``, which is what the file's own header says
#: about them.
ENV_EXAMPLE_NOT_SETTINGS: dict[str, str] = {
    "JMFTS_WORKER_ID": "read from the environment by jmfts_core/worker.py, not via Settings",
    "JMFTS_WORKER_BADGE": "read from the environment by jmfts_core/worker.py, not via Settings",
}


def _env_example_names() -> set[str]:
    """Every ``JMFTS_*`` name ``.env.example`` assigns, commented-out lines included.

    A commented assignment is how the file shows a tuning knob's real default, so it counts
    as naming the setting — see the file's header.
    """
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^#?\s*(JMFTS_[A-Z0-9_]+)\s*=", text, re.M))


def _settings_env_names() -> set[str]:
    """Every ``Settings`` field as its ``JMFTS_``-prefixed environment name."""
    return {f"JMFTS_{name.upper()}" for name in Settings.model_fields}


def test_env_example_names_every_setting():
    """``.env.example`` promises it omits nothing. Hold it to that."""
    missing = _settings_env_names() - _env_example_names() - set(SETTINGS_NOT_IN_ENV_EXAMPLE)
    assert not missing, (
        f".env.example omits {sorted(missing)}. Its header says it lists EVERY setting "
        "Settings reads. Add each one with its real default, or add it to "
        "SETTINGS_NOT_IN_ENV_EXAMPLE with the reason."
    )


def test_env_example_names_no_setting_that_does_not_exist():
    """A name in the template that ``Settings`` does not read teaches a setting that is not there."""
    extra = _env_example_names() - _settings_env_names() - set(ENV_EXAMPLE_NOT_SETTINGS)
    assert not extra, (
        f".env.example names {sorted(extra)}, which no Settings field reads. Remove them, "
        "or record them in ENV_EXAMPLE_NOT_SETTINGS with what reads them instead."
    )


def test_readme_operation_count_matches_the_route_table():
    """The README's "N operations" is the count ``/docs`` will show.

    Counted off the live app rather than off ``REGISTRY``: ``/``, ``/health``,
    ``/health/llm`` and ``/config`` are mounted infra that is not in the registry, and the
    number a reader checks is the one in the browser.

    Through ``iter_mounted_api_routes`` and never ``app.routes``, which that function's
    docstring forbids: since fastapi 0.141 ``include_router`` leaves an ``_IncludedRouter``
    wrapper in ``app.routes`` and keeps the real routes behind it. This test open-coded the
    traversal and read 8 under 0.141.1 against 119 under 0.135.3 — the pinned dev
    environment gave the right answer and the first public CI run of 0.5.0 did not.

    The number also stopped counting ``/openapi.json``, ``/docs``,
    ``/docs/oauth2-redirect`` and ``/redoc``. Those four are ``Route`` rather than
    ``APIRoute``, they are absent from the OpenAPI document, and Swagger UI lists none of
    them — so counting them made the README's figure four higher than what the reader sees
    on the page the sentence is about. 115 through this traversal and 115 operations across
    91 paths in ``app.openapi()``, on both fastapi versions.
    """
    from jmfts_core.rest.main import app
    from jmfts_core.rest.wiring import iter_mounted_api_routes

    readme = (REPO / "README.md").read_text(encoding="utf-8")
    claimed = re.search(r"(\d+)\s+operations", readme)
    assert claimed, "README no longer states an operation count; this guard expects one"

    verbs = {"GET", "POST", "PUT", "DELETE", "PATCH"}
    actual = sum(1 for route in iter_mounted_api_routes(app) for m in route.methods if m in verbs)
    assert (
        int(claimed.group(1)) == actual
    ), f"README says {claimed.group(1)} operations; the mounted route table has {actual}."


def test_readme_client_pin_matches_pyproject():
    """The README quotes the ``jmfts-client==X`` pin as the reason to install it first."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")

    declared = re.search(r'"jmfts-client==([^"]+)"', pyproject)
    assert declared, "pyproject.toml no longer pins jmfts-client with =="
    quoted = re.search(r"jmfts-client==([0-9][^`\s]*)", readme)
    assert quoted, "README no longer quotes the jmfts-client pin; this guard expects it"
    assert quoted.group(1) == declared.group(1), (
        f"README quotes jmfts-client=={quoted.group(1)}; pyproject.toml pins "
        f"{declared.group(1)}. ./bump-version.sh sets the pin, so the README is what "
        "fell behind."
    )
