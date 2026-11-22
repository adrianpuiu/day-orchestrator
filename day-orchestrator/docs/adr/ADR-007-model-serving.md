# ADR-007: Model serving — config-driven Gemini strings or local OpenAI-compatible endpoint

Date: 2026-11-22 · Status: accepted (live on the local endpoint) · Owner: agp

## Context

architecture.md evolution step 2 anticipated a local-model option. The owner
runs a tensorfold OpenAI-compatible server at `192.168.0.206:8000/v1`
(models `deepseek-v4.1-flash`, alias `ds41`). ADK has no native
OpenAI-compatible model class; the documented path is LiteLLM
(`google-adk[extensions]`).

Probing found the engine's capability split: **chat completions ✓, function
calling ✓, `response_format`/guided JSON ✗** ("structured output … is not
supported by this engine yet").

## Decision

`core/models.py::resolve_model` — one choke point:

- `model_api_base` empty → model strings are Gemini (as originally planned).
- `model_api_base` set (current `config.toml`) → every agent model string is a
  bare id from the server's `GET /v1/models`, served as
  `LiteLlm(model="openai/<id>", api_base=…, api_key="EMPTY")` with
  `HttpRetryOptions(attempts=3)` → litellm `num_retries` (a server blip no
  longer kills a turn).

**Structured output is instruction-driven JSON** ("output only a JSON object,
no markdown fences") with a fence-tolerant extractor (`_extract_json`) — never
`output_schema`, which would emit `response_format` and 400 on this engine.
Unparseable triage output degrades to "no items" (logged via the pipeline).

The coordinator receives the **current local datetime injected each turn**
(`before_agent_callback` → `current_datetime` state; instruction: resolve
relative dates against it). Live evals caught the failure this prevents:
without a clock, "tomorrow" resolved to the wrong date.

## Consequences

Positive: provider is a config value, not a code path; any engine works
(structured output + tool calling are the only requirements, both verified by
raw probes before wiring); nightly evals (03:30, `app.cli eval-nightly`) catch
server-side drift — the alias `ds41` can change under us.

Negative (accepted): self-judged readability eval shares the model's biases
(value = drift detection, not absolute scoring); triage JSON validity depends
on the model — malformed output degrades to an empty triage (visible as a
thin brief, not as an error); content still transits the LAN host.
