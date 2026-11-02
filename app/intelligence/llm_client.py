"""Factory for the assistant's model client.

Kept separate from the extraction client so the two can run different models:
bulk extraction wants a cheap high-volume model, the assistant wants a stronger
one. Both read the key from the environment and neither writes it anywhere.
"""
from __future__ import annotations

from ..config import settings
from ..extraction.llm import LlmClient


def assistant_client(model: str | None = None) -> LlmClient:
    return LlmClient(model=model or settings.assistant_model)
