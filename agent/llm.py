"""LLM configuration for OpenRouter via LangChain's ChatOpenAI."""

from __future__ import annotations

import os
from typing import Any


def load_openrouter_llm(**overrides: Any):
    """Create a ChatOpenAI client configured for OpenRouter.

    Requires ``langchain_openai``. Example usage::

        from agent.llm import load_openrouter_llm
        llm = load_openrouter_llm()
    """
    try:
        from langchain_openai import ChatOpenAI
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ModuleNotFoundError(
            "langchain_openai is required. Install with: pip install langchain-openai"
        ) from exc

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY environment variable is required when using OpenRouter. "
            "Get your key from https://openrouter.ai/"
        )

    model_name = os.getenv("OPENROUTER_MODEL", "z-ai/glm-4.5-air:free")

    return ChatOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        model=model_name,
        temperature=0,
        max_retries=5,
        **overrides,
    )
