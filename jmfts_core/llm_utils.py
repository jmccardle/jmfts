"""LLM response utilities for reasoning models."""

from typing import Any


def extract_llm_text(choice: dict) -> str:
    """Extract text from an LLM choice, handling reasoning models.

    If the model returns reasoning_content separately (e.g. Qwen3), use content
    if present, otherwise fall back to reasoning_content. Handles both standard
    and reasoning-capable models transparently.
    """
    message: Any = choice.get("message", {})
    if not isinstance(message, dict):
        return ""

    content: str = (message.get("content") or "").strip()
    if content:
        return content

    reasoning: str = (message.get("reasoning_content") or "").strip()
    return reasoning
