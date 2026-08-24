"""LLM Synthesis Service — calls an OpenAI-compatible endpoint to synthesize search results."""

import logging
from dataclasses import dataclass

from jmfts_core.config import get_settings
from jmfts_core.llm_client import complete

logger = logging.getLogger(__name__)

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


def _format_context(documents: list[dict], max_context_tokens: int) -> str:
    """Format search results into a context string, respecting approximate token budget."""
    parts = []
    char_budget = max_context_tokens * 4  # rough chars-per-token estimate

    for i, doc in enumerate(documents, 1):
        title = doc.get("title") or f"Document {doc['id']}"
        content = doc.get("content") or ""
        header = f"[Source {i}: {title} (id={doc['id']}, score={doc['score']:.3f})]"
        block = f"{header}\n{content}\n"

        if len("\n".join(parts)) + len(block) > char_budget:
            remaining = char_budget - len("\n".join(parts))
            if remaining > len(header) + 40:
                parts.append(f"{header}\n{content[:remaining - len(header) - 10]}...\n")
            break

        parts.append(block)

    return "\n".join(parts)


async def synthesize(
    query: str,
    documents: list[dict],
    max_context_tokens: int = 4096,
    llm_model: str | None = None,
) -> SynthesisResult:
    """Call the LLM to synthesize a response from search results.

    Args:
        query: The user's original search query.
        documents: List of dicts with keys: id, title, content, score.
        max_context_tokens: Approximate token budget for the context window.
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
    """
    settings = get_settings()
    base_url, model = settings.require_llm("Synthesis", llm_model)

    context = _format_context(documents, max_context_tokens)

    user_message = f"Query: {query}\n\nSources:\n{context}\n\nSynthesize an answer to the query based on the sources above."

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

    return SynthesisResult(text=result.text, model=model, usage=result.usage)
