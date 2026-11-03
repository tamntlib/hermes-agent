"""Auxiliary request assembly, late-bound to the router for credential/recovery seams."""
from __future__ import annotations

from typing import Any, Optional


def build_call_kwargs(
    provider: str, model: str, messages: list, temperature: Optional[float] = None,
    max_tokens: Optional[int] = None, tools: Optional[list] = None, timeout: float = 30.0,
    extra_body: Optional[dict] = None, reasoning_config: Optional[dict] = None,
    base_url: Optional[str] = None, task: Optional[str] = None,
    no_progress_timeout: Optional[float] = None,
    requested_provider: Optional[str] = None, api_mode: Optional[str] = None,
) -> dict:
    """Build kwargs for .chat.completions.create() with model/provider adjustments.
    ``no_progress_timeout`` is a Codex-Responses-only extra (consumed by
    ``_CodexCompletionsAdapter.create``'s ``**kwargs`` catch-all); callers must only pass it
    when the resolved client is a ``CodexAuxiliaryClient`` — real SDK clients don't accept it."""
    from agent.auxiliary_client import (
        _current_custom_base_url, _with_custom_endpoint_extra_body, _fixed_temperature_for_model, OMIT_TEMPERATURE, _forwards_max_tokens,
        auxiliary_max_tokens_param, _dedupe_tool_names, _project_provider_profile, data_use_requirement, _merge_aux_extra_body,
        _nous_on_messages_wire, _endpoint_speaks_anthropic_messages, _is_anthropic_compat_endpoint, _get_auxiliary_task_config, _runtime_main_value,
    )

    kwargs: dict[str, Any] = {"model": model, "messages": messages, "timeout": timeout}
    if no_progress_timeout is not None:
        kwargs["no_progress_timeout"] = no_progress_timeout
    effective_base = base_url or (_current_custom_base_url() if provider == "custom" else "")
    extra_body = _with_custom_endpoint_extra_body(extra_body, provider, model, effective_base)
    # Per-model fixed/omitted temperature, then Opus 4.7+ sampling bans: it rejects any
    # non-default temperature/top_p/top_k, so drop silently rather than 400 when the aux model flips.
    fixed_temperature = _fixed_temperature_for_model(model, effective_base, provider)
    if fixed_temperature is OMIT_TEMPERATURE:
        temperature = None  # strip — let server choose
    elif fixed_temperature is not None:
        temperature = fixed_temperature
    if temperature is not None:
        from agent.anthropic_adapter import _forbids_sampling_params
        if not _forbids_sampling_params(model):
            kwargs["temperature"] = temperature
    provider_norm = str(provider or "").strip().lower()
    if max_tokens is not None and _forwards_max_tokens(provider, provider_norm, model, effective_base, task):
        kwargs.update(auxiliary_max_tokens_param(max_tokens, model=model))  # picks max_completion_tokens where needed
    if tools:
        kwargs["tools"] = _dedupe_tool_names(tools, provider, model)
    # Provider profiles are the source of truth for reasoning wire shapes (top-level, nested body,
    # or extra_body.reasoning); providers without a reasoning-aware profile keep the generic
    # ``extra_body.reasoning`` fallback. Clamp Hermes-internal levels (``ultra``) to the
    # OpenAI-compat wire ONCE here, before either path sees the config — the same entry clamp the
    # main transport applies (#89503); MoA aggregator/reference and aux calls 400'd without it (#112010).
    from agent.reasoning_effort import clamp_reasoning_config
    from agent.auxiliary_reasoning_floor import known_reasoning_floor
    if isinstance(extra_body, dict):
        task_reasoning = extra_body.get("reasoning")
        if isinstance(task_reasoning, dict) and "enabled" in task_reasoning:
            extra_body = dict(extra_body)
            extra_body.pop("reasoning")
            if reasoning_config is None:
                reasoning_config = task_reasoning
    reasoning_config = clamp_reasoning_config(known_reasoning_floor(reasoning_config, provider_norm, effective_base, model, task))
    projection = _project_provider_profile(provider, provider_norm, model, effective_base, reasoning_config, data_use_requirement(messages))
    kwargs.update(projection.top_level)
    merged_extra = _merge_aux_extra_body(extra_body, projection, reasoning_config, provider_norm)
    if "response_format" in merged_extra:
        from agent.auxiliary_structured_output import without_unsupported_response_format
        merged_extra = without_unsupported_response_format(merged_extra, provider_norm, effective_base, model, task)
    if merged_extra:
        kwargs["extra_body"] = merged_extra
    # Anthropic Messages adapters take reasoning via a private kwarg that plain OpenAI SDK clients
    # would reject; Portal Claude is dual-wire, so include it only when the catalog id selects
    # /v1/messages. A profile declaring api_mode=anthropic_messages (commandcode-anthropic) is on
    # that wire regardless of URL shape — once it overrides build_api_kwargs_extras the generic
    # ``extra_body.reasoning`` fallback the adapter used to read is gone, so this is the adapter's
    # only path. _wrap_transport wraps such providers on the same declaration.
    if reasoning_config and isinstance(reasoning_config, dict):
        raw_base = base_url or ""
        if (
            provider_norm == "anthropic" or projection.messages_wire or _nous_on_messages_wire(provider_norm, model)
            or _endpoint_speaks_anthropic_messages(raw_base) or _is_anthropic_compat_endpoint(provider_norm, raw_base)
        ):
            kwargs["_reasoning_config"] = dict(reasoning_config)
    if task == "compression":
        from agent.auxiliary_service_tiers import apply_compression_service_tier
        kwargs = apply_compression_service_tier(
            kwargs, _get_auxiliary_task_config(task), provider=provider, model=model,
            base_url=effective_base, api_mode=api_mode or "chat_completions",
            requested_provider=provider if requested_provider is None else requested_provider,
        )
    # Conversation affinity (OpenCode relay, opt-in custom-provider header) — same key as the main
    # turn so compression/title/vision calls stay on the conversation's warm backend.
    from agent.opencode_affinity import merge_session_affinity_headers
    return merge_session_affinity_headers(kwargs, provider, base_url, _runtime_main_value("session_id") or None)
