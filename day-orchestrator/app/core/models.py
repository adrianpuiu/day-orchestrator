"""Model resolution (architecture.md evolution step 2: local-model option).

Config drives it: `model_api_base` set → every agent model string is served
OpenAI-compatible via LiteLLM; unset → Gemini strings pass through unchanged.
"""

from __future__ import annotations

from .config import Config


def resolve_model(cfg: Config, spec: str):
    """Returns the ADK model object for a configured model string.

    spec is a bare model id (e.g. "ds41") when model_api_base is set, or a
    Gemini model string (e.g. "gemini-3.8-flash") when it is not.
    """
    if not cfg.model_api_base:
        return spec
    from google.adk.models.lite_llm import LiteLlm

    return LiteLlm(
        model=f"openai/{spec}",
        api_base=cfg.model_api_base,
        api_key=cfg.model_api_key,
    )
