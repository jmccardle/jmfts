"""`JMFTS_LLM_BASE_URL` is used as given — JMFTS appends `/chat/completions` and `/models`.

Until 0.6.1 JMFTS appended `/v1` to a "server root". The advertised `/v1` URL then 404'd at
`/v1/v1`, and a server that versions under any other segment — EnsoNet serves `/v1d` —
could not be reached by any value of the setting. Found by the 0.6.0 cutover on midlife.

No database.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from jmfts_core.config import Settings
from jmfts_core.rest import main

ADVERTISED = ["http://gpu-box:8080/v1", "http://host:8853/v1d", "https://api.example.com/v1/"]


class _Answer:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self._body = body
        self.text = "" if body is None else str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _probe(base_url: str, answer: _Answer | Exception) -> tuple[list[str], object]:
    seen: list[str] = []

    def fake_get(url, timeout):
        seen.append(url)
        if isinstance(answer, Exception):
            raise answer
        return answer

    settings = Settings(llm_base_url=base_url, llm_model="m")
    with (
        patch.object(main, "get_settings", return_value=settings),
        patch.object(main.httpx, "get", fake_get),
    ):
        status = main._probe_llm()
    return seen, status


@pytest.mark.parametrize("given", ADVERTISED)
def test_the_probe_asks_models_on_the_base_url_as_given_and_only_that(given):
    seen, status = _probe(given, _Answer(200, {"data": [{"id": "m"}]}))
    assert seen == [f"{given.rstrip('/')}/models"]
    assert status.reachable and status.openai_compatible


def test_a_root_url_404s_and_the_detail_names_the_fix():
    """The likeliest misconfiguration after 0.6.1 is a pre-0.6.1 root URL. Say so; do not
    quietly retry with `/v1` appended — that would be the old guess back again."""
    seen, status = _probe("http://gpu-box:8080", _Answer(404))
    assert seen == ["http://gpu-box:8080/models"]
    assert status.reachable and not status.openai_compatible
    assert "http://gpu-box:8080/models" in status.detail
    assert "http://gpu-box:8080/v1" in status.detail


def test_an_unreachable_host_is_reported_as_unreachable():
    seen, status = _probe("http://gpu-box:8080/v1", httpx.ConnectError("refused"))
    assert seen == ["http://gpu-box:8080/v1/models"]
    assert not status.reachable
    assert status.detail.startswith("unreachable:")
