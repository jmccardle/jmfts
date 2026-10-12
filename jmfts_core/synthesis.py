"""LLM Synthesis Service — calls an OpenAI-compatible endpoint to synthesize search results."""

import asyncio
import logging
from dataclasses import dataclass

from jmfts_core.config import get_settings
from jmfts_core.llm_client import LlmCallError, complete

logger = logging.getLogger(__name__)

#: The statuses that say "not now" rather than "not this". See :func:`synthesize`.
RETRYABLE_STATUSES = frozenset({502, 503, 504})

#: The first wait before a retry; each later one doubles. The budget is a setting
#: (``JMFTS_SYNTHESIS_RETRY_BUDGET``); this is the schedule's shape.
RETRY_FIRST_DELAY_S = 0.5

SYSTEM_PROMPT = (
    "You are a research assistant. Given a user query and a set of retrieved documents, "
    "synthesize a clear, accurate answer grounded in the provided sources. "
    "First, think through your reasoning step by step. Then provide your synthesized answer. "
    "Cite sources by their document title or ID when making claims. "
    "If the documents do not contain enough information to answer, say so."
)


@dataclass
class SynthesisResult:
    text: str
    model: str
    #: τ's ``Usage``, as a dict — see :class:`jmfts_core.llm_client.LlmCompletion`. Still
    #: optional because a caller may construct a result without one; the synthesis path
    #: always fills it.
    usage: dict | None = None


def _format_context(documents: list[dict]) -> str:
    """Format every search result into one context string, in rank order.

    Nothing is dropped. This used to pack to ``max_context_tokens * 4`` characters, dropping
    results from the tail and trimming one mid-block, so an answer could cite four of twelve
    retrieved documents and read as though all twelve had been considered. JMFTS has no
    tokenizer for the synthesising model, so any budget here would be a guess; the caller
    chooses how many documents to send (``top_k``) and the endpoint says whether they fit.
    """
    parts = []
    for i, doc in enumerate(documents, 1):
        title = doc.get("title") or f"Document {doc['id']}"
        content = doc.get("content") or ""
        header = f"[Source {i}: {title} (id={doc['id']}, score={doc['score']:.3f})]"
        parts.append(f"{header}\n{content}\n")

    return "\n".join(parts)


async def synthesize(
    query: str,
    documents: list[dict],
    llm_model: str | None = None,
) -> SynthesisResult:
    """Call the LLM to synthesize a response from search results.

    Every document is sent. A set too large for the model's context is refused by the
    endpoint, and that refusal propagates like any other non-200 (below).

    Args:
        query: The user's original search query.
        documents: List of dicts with keys: id, title, content, score.
        llm_model: Override the default LLM model name.

    Returns:
        SynthesisResult with the generated text.

    Raises:
        LlmNotConfiguredError: If no LLM endpoint is configured. Raised by
            ``require_llm`` before any transport is built.
        Exception: For every other failure — a refused connection, a timeout, a non-200.
            It used to be the httpx type (``ConnectError``, ``TimeoutException``,
            ``HTTPStatusError`` from ``raise_for_status()``); τ 0.9.3 catches the
            transport's exception inside its own event generator and reports it as an
            error event, so what reaches here is a bare ``Exception`` whose message names
            the model, the endpoint and the status or fault. See ``llm_client``'s module
            docstring for what that costs ``task_errors.classify_exception``.

            *2026-10-12, 0.6.2 (#32):* that bare ``Exception`` now arrives as
            :class:`~jmfts_core.llm_client.LlmCallError`, with ``status`` (``None`` when
            nothing answered) and ``attempts``. A 502, 503 or 504 is retried first, with
            waits doubling from :data:`RETRY_FIRST_DELAY_S` for as long as their sum stays
            within ``Settings.synthesis_retry_budget``. Those three say "not now" about a
            request that may succeed unchanged — EnsoNet answers 503 while a model loads.
            Nothing else is retried: a 400 for an over-context set is as large the second
            time, a 429 on a metered API wants longer than a request should wait, and a
            refused connection or a timeout has no status to judge by.
    """
    settings = get_settings()
    base_url, model = settings.require_llm("Synthesis", llm_model)

    context = _format_context(documents)

    user_message = f"Query: {query}\n\nSources:\n{context}\n\nSynthesize an answer to the query based on the sources above."

    attempts = 0
    waited = 0.0
    delay = RETRY_FIRST_DELAY_S
    while True:
        attempts += 1
        try:
            result = await complete(
                settings=settings,
                base_url=base_url,
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=settings.synthesis_max_tokens,
                temperature=0.3,
            )
        except LlmCallError as exc:
            if exc.status in RETRYABLE_STATUSES and waited + delay <= (
                settings.synthesis_retry_budget
            ):
                logger.info(
                    "Synthesis attempt %d refused (%s); retrying in %.2fs", attempts, exc, delay
                )
                await asyncio.sleep(delay)
                waited += delay
                delay *= 2
                continue
            raise LlmCallError(str(exc), status=exc.status, attempts=attempts) from exc
        return SynthesisResult(text=result.text, model=model, usage=result.usage)
