"""The one door from JMFTS to an LLM endpoint. ``SPRINT_0_3_0.md`` Part 2.

Four call sites used to each open their own ``httpx`` client, post to
``{base_url}/v1/chat/completions`` and parse the reply: ``synthesis.synthesize``,
``summarization._llm_summarize``, ``fact_extraction._llm_extract`` and
``rollup_tasks.summarize_span``. Four copies of one wire format is four places for a
header, a timeout or a response shape to drift, and it is also four places that would
have to learn constrained decoding when Part 8 needs it. They all come through here now,
and here calls ``tau_llm`` (``ffwf-tau-llm``, a first-party package).

**What this module owns and what it does not.** It owns the transport: which endpoint,
which credential, which timeout, and how an ``AssistantMessage`` becomes the string the
caller wanted. It does NOT own the decision of whether an LLM is configured at all —
that stays :meth:`jmfts_core.config.Settings.require_llm`, which raises
``LlmNotConfiguredError`` naming the environment variable. A blank ``JMFTS_LLM_*`` still
means every non-LLM path works and the four LLM paths say why they cannot.

Three things about τ's ``Model`` are worth stating here, because each one looks like a
value this module invented and none of them is:

* ``provider`` is ``"jmfts"``. τ's provider registry attaches a default endpoint and a
  credential environment variable to a registered vendor — ``"openai"`` would make τ read
  ``OPENAI_API_KEY`` and refuse the call when it is unset. JMFTS names its endpoint and
  its key itself, from ``Settings``, so the vendor is deliberately an unregistered name.
* ``context_window`` is 0. JMFTS has no setting that knows an endpoint's context window,
  nothing on τ's request path reads the field, and it is required. Zero is the honest
  answer to "how big is this window"; a plausible-looking 4096 would be a fabricated one
  that a future reader would trust.
* ``stream`` is False. Every one of the four callers wants a whole answer and has no
  streaming UI, and a buffered completion is byte-for-byte the request the four
  hand-written call sites were already sending.

**What a failure looks like now, and what that costs.** τ 0.9.3's OpenAI provider catches
every transport exception inside its event generator and reports it as an error event
carrying a message; ``complete_simple`` then raises a bare ``Exception`` with that text.
So an ``httpx.ConnectError``, an ``httpx.TimeoutException`` and a non-200 status all reach
JMFTS as ``Exception``, where they used to reach it as their own httpx types.

:func:`jmfts_core.task_errors.classify_exception` branches on those types, so an LLM
failure inside a queued task now takes the module's documented default — ``RETRYABLE`` —
instead of being classified from the status code. Three of the four outcomes are
unchanged in policy (a 429, a 5xx and a refused connection were all RETRYABLE already); a
timeout keeps its retries but is now logged as ``retryable`` rather than ``timeout``; and
a 4xx that was ``PERMANENT`` now burns the retry budget before failing terminally.

That is recorded rather than patched. The only fix available on this side would be to
read the status back out of τ's message text, and ``task_errors``'s own docstring exists
to say that retry policy is decided by classification and never by a string match on a
message. The fix belongs in τ: preserve the original exception, and raise a typed error
carrying ``status_code`` for a non-200. One arm in ``classify_exception`` then restores
every distinction, and this paragraph goes away.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from tau_llm import Model, TextContent, ThinkingContent, aclose_providers, complete_simple
from tau_llm.types import AssistantMessage

from jmfts_core.config import Settings

#: τ's wire protocol for OpenAI-compatible ``/chat/completions`` servers. The only one
#: JMFTS speaks; ``anthropic-messages`` and ``google-generative-ai`` are implemented in
#: τ and are not wired here.
OPENAI_COMPLETIONS_API = "openai-completions"

#: The vendor name JMFTS calls itself on τ's side. Unregistered on purpose — see the
#: module docstring.
JMFTS_PROVIDER = "jmfts"

#: What to send as the bearer when no ``JMFTS_LLM_API_KEY`` is configured.
#:
#: τ refuses to send a request with no credential at all (Fail-Early: a blank key is far
#: more often a misconfiguration than a keyless server), and its documented spelling for
#: "this endpoint genuinely wants none" is a truthy sentinel. This is the ONE wire
#: difference from the hand-written call sites, which sent no ``Authorization`` header in
#: that case: a local llama-server that requires no auth ignores the header, and a
#: metered API that requires one was never going to be reached without a key anyway.
NO_KEY_SENTINEL = "not-needed"


@dataclass(frozen=True)
class LlmCompletion:
    """One completed call: the text the caller asked for, and what it cost."""

    #: The assistant's text. Empty only when the model genuinely said nothing.
    text: str
    #: The model id that answered — the resolved name, not the caller's override.
    model: str
    #: τ's :class:`tau_llm.Usage`, as a dict. τ's own vocabulary (``input_tokens`` /
    #: ``output_tokens`` / ``cache_*``), not OpenAI's ``prompt_tokens`` spelling: the raw
    #: provider block is no longer in reach, and re-spelling τ's numbers into OpenAI's
    #: field names would be a translation nobody asked for and nobody could check.
    usage: dict[str, Any]


def build_model(*, base_url: str, model: str, max_tokens: int, settings: Settings) -> Model:
    """Describe one JMFTS LLM endpoint to τ.

    ``base_url`` is what :meth:`Settings.require_llm` returned — the server root, with no
    ``/v1``. τ's OpenAI provider posts to ``/chat/completions`` relative to the base URL
    it is given, so the ``/v1`` the four call sites used to write into the path literal is
    appended here instead. One place, one spelling.

    ``max_tokens`` reaches the wire through τ rather than through the request body, which
    is what lets ``tau_llm.compat`` choose between ``max_tokens`` and
    ``max_completion_tokens`` per endpoint. llama.cpp, vLLM and the classic Chat
    Completions API keep ``max_tokens``; OpenAI's o-series and gpt-5 family, which reject
    it, get ``max_completion_tokens``.
    """
    return Model(
        id=model,
        name=model,
        api=OPENAI_COMPLETIONS_API,
        provider=JMFTS_PROVIDER,
        base_url=f"{base_url.rstrip('/')}/v1",
        # Not known, and not consulted on the request path. See the module docstring.
        context_window=0,
        max_tokens=max_tokens,
        # A buffered completion, not SSE — the shape all four callers already used.
        stream=False,
        request_timeout=settings.effective_llm_timeout,
    )


def _options(
    *, settings: Settings, temperature: float, extra_body: dict[str, Any] | None
) -> dict[str, Any]:
    """Per-call options for τ: the credential, the sampler, and any server knobs.

    ``extra_body`` is for endpoint-specific request fields the callers already send —
    today only ``chat_template_kwargs``, which is how summarization suppresses a
    reasoning model's thinking. τ merges these into the request body below its own
    reserved keys, so one cannot silently replace ``model``, ``messages`` or ``stream``.
    """
    options: dict[str, Any] = {
        "api_key": settings.llm_api_key or NO_KEY_SENTINEL,
        "temperature": temperature,
    }
    if extra_body:
        options.update(extra_body)
    return options


def _text_of(message: AssistantMessage) -> str:
    """The assistant's answer, with a reasoning model's fallback.

    Same rule ``llm_utils.extract_llm_text`` applies to a raw choice, restated over τ's
    typed content blocks: prefer the text channel; when it is empty, fall back to the
    reasoning channel. A reasoning model given a small token budget spends it thinking
    and returns empty content, and a summary of the reasoning trace is a poorer answer
    than a real summary but a far better one than "".
    ``Settings.summarization_disable_thinking`` exists to stop that happening at all.
    """
    text = "".join(b.text for b in message.content if isinstance(b, TextContent)).strip()
    if text:
        return text
    return "".join(b.thinking for b in message.content if isinstance(b, ThinkingContent)).strip()


async def complete(
    *,
    settings: Settings,
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    extra_body: dict[str, Any] | None = None,
) -> LlmCompletion:
    """One whole-message completion against the configured endpoint.

    ``messages`` are OpenAI-shaped dicts — ``{"role": ..., "content": ...}`` — which is
    what the four callers already build and what τ passes through unchanged.
    """
    tau_model = build_model(
        base_url=base_url, model=model, max_tokens=max_tokens, settings=settings
    )
    message = await complete_simple(
        tau_model,
        {"messages": messages},
        _options(settings=settings, temperature=temperature, extra_body=extra_body),
    )
    return LlmCompletion(text=_text_of(message), model=model, usage=message.usage.model_dump())


def complete_sync(
    *,
    settings: Settings,
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    extra_body: dict[str, Any] | None = None,
) -> LlmCompletion:
    """:func:`complete`, for a caller that has no event loop.

    ``rollup_tasks.summarize_span`` is the one. It runs inside the ingest worker, which is
    a plain thread driving synchronous task handlers, and τ's client is async-only —
    ``complete_simple`` is a coroutine and there is no sync spelling of it.

    So this owns a loop for the duration of one call: ``asyncio.run`` creates it, and the
    ``finally`` closes τ's provider pool ON that loop before it is torn down. Both halves
    matter. τ pools providers per event loop and closes them explicitly rather than by
    GC, so skipping the ``aclose_providers`` would leak an ``httpx.AsyncClient`` — and its
    socket — every call. Closing it means no HTTP keep-alive between calls, which is
    exactly what ``with httpx.Client(...) as client:`` gave this call site before, so
    nothing regresses.

    A running loop in this thread is refused rather than worked around: the caller is an
    async context that should be awaiting :func:`complete`, and ``asyncio.run`` inside one
    is a ``RuntimeError`` whose message says nothing about which function to call instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "llm_client.complete_sync() was called from a thread that already runs an "
            "event loop; it owns one for the duration of the call and cannot nest. Await "
            "llm_client.complete() instead."
        )

    async def _once() -> LlmCompletion:
        try:
            return await complete(
                settings=settings,
                base_url=base_url,
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                extra_body=extra_body,
            )
        finally:
            await aclose_providers()

    return asyncio.run(_once())
