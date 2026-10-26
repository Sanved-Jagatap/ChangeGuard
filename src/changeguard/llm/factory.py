from __future__ import annotations

from ..config import Settings
from ..errors import LLMError
from .base import LLMClient
from .mock import GullibleMockLLM, MockPolicyLLM


def make_llm(settings: Settings, provider: str | None = None, model: str | None = None) -> LLMClient:
    provider = (provider or settings.llm_provider).lower()
    model = model or settings.llm_model
    if provider == "mock":
        return MockPolicyLLM()
    if provider == "mock_gullible":
        return GullibleMockLLM()
    if provider == "gemini":
        from .gemini import GeminiClient
        if not model:
            raise LLMError("set CG_LLM_MODEL to a Gemini model name")
        return GeminiClient(model)
    if provider == "openai":
        from .openai_client import OpenAIClient
        if not model:
            raise LLMError("set CG_LLM_MODEL to an OpenAI-compatible model name")
        return OpenAIClient(model)
    raise LLMError(f"unknown provider '{provider}'")
