"""A local OpenAI-compatible endpoint, for tests of JMFTS's LLM call sites.

Why a real socket instead of a mock. Until 0.3.0 these tests replaced
``httpx.AsyncClient`` in each module and asserted on the dict handed to ``post()``. The
transport now lives in ``tau_llm`` (``jmfts_core/llm_client.py``), which builds its own
client inside its own provider, so there is nothing in the JMFTS module left to patch —
and patching τ's internals would be a test of τ's private shape rather than of JMFTS.

A loopback HTTP server answers the question the mock was really being asked: *does JMFTS
put the right request on the wire, and does it read the answer correctly?* It exercises
the URL, the ``Authorization`` header, the JSON body and the response parse end to end,
costs a few milliseconds, needs no database and no optional dependency, and it keeps
working when τ's internals move.

Usage::

    with llm_stub(content="a summary") as stub:
        settings = stub.settings()
        ...                                   # call the code under test
        assert stub.requests[0].body["model"] == "stub-model"
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator


@dataclass
class StubRequest:
    """One request the stub received, as it arrived."""

    path: str
    body: dict[str, Any]
    authorization: str | None

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.body["messages"]

    def role(self, role: str) -> str:
        """The content of the single message with this role."""
        matches = [m["content"] for m in self.messages if m["role"] == role]
        assert len(matches) == 1, f"expected one {role!r} message, got {len(matches)}"
        return matches[0]


@dataclass
class LlmStub:
    """A running stub endpoint and the record of what reached it."""

    base_url: str
    requests: list[StubRequest] = field(default_factory=list)

    def settings(self, **overrides: Any):
        """A real ``Settings`` pointed at this stub.

        A real one rather than a ``MagicMock``: ``llm_client`` reads
        ``effective_llm_timeout`` and ``llm_api_key`` off it, and a mock would answer
        both with a ``MagicMock`` that τ then rejects — which is the mock testing itself.
        """
        from jmfts_core.config import Settings

        values: dict[str, Any] = {
            "llm_base_url": self.base_url,
            "llm_model": "stub-model",
            "llm_timeout": 10.0,
        }
        values.update(overrides)
        return Settings(**values)


def _make_handler(stub: LlmStub, content: str | None, status: int, reasoning: str | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # keep pytest output clean
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            stub.requests.append(
                StubRequest(
                    path=self.path,
                    body=json.loads(self.rfile.read(length) or b"{}"),
                    authorization=self.headers.get("Authorization"),
                )
            )
            if status != 200:
                body = json.dumps({"error": {"message": "stub refuses"}}).encode()
            else:
                message: dict[str, Any] = {"role": "assistant", "content": content}
                if reasoning is not None:
                    # The shape a reasoning model returns when its whole token budget
                    # went into thinking: empty content, a populated reasoning channel.
                    message["reasoning_content"] = reasoning
                body = json.dumps(
                    {
                        "id": "stub-completion",
                        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 11,
                            "completion_tokens": 7,
                            "total_tokens": 18,
                        },
                    }
                ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@contextmanager
def llm_stub(
    *, content: str | None = "stub answer", status: int = 200, reasoning: str | None = None
) -> Iterator[LlmStub]:
    """Run a stub ``/v1/chat/completions`` on loopback for the duration of the block."""
    # The handler needs the stub to record into, and the stub needs the port the handler
    # will be bound to, so the base URL is filled in once the socket has a port.
    stub = LlmStub(base_url="")
    server = HTTPServer(("127.0.0.1", 0), _make_handler(stub, content, status, reasoning))
    stub.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
