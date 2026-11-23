"""Model resolution — ported verbatim from day-orchestrator (proven wiring).

Local-only per project decision: research-plane LlmAgents resolve through
here; the trading plane never constructs a model.
"""

from __future__ import annotations

from .config import Config


def resolve_model(cfg: Config, spec: str | None = None):
    """Returns the ADK model object for a configured model string.

    spec is a bare model id (e.g. "ds41") when model_api_base is set, or a
    Gemini model string when it is not.
    """
    spec = spec or cfg.research_model
    if not cfg.model_api_base:
        return spec
    from google.adk.models.lite_llm import LiteLlm
    from google.genai import types as gtypes

    return LiteLlm(
        model=f"openai/{spec}",
        api_base=cfg.model_api_base,
        api_key=cfg.model_api_key,
        timeout=120,  # hung local endpoint must fail in minutes, not 600s x3
        retry_options=gtypes.HttpRetryOptions(attempts=3),
    )
