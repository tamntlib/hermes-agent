"""Destination-local compression tier policy (independent of chat fast mode)."""
from __future__ import annotations

from typing import Any


def compression_main_runtime(compressor: Any) -> dict[str, Any]:
    """Keep explicit named authority from this session's scoped runtime only."""
    from agent.auxiliary_client import _RUNTIME_MAIN_CONTEXT

    runtime = {key: getattr(compressor, key, "") for key in (
        "model", "provider", "base_url", "api_key", "api_mode",
    )}
    scoped = _RUNTIME_MAIN_CONTEXT.get()
    session_id = getattr(compressor, "_session_id", "")
    if (session_id and isinstance(scoped, dict) and scoped.get("session_id") == session_id
            and all(scoped.get(key, "") == value for key, value in runtime.items())):
        if scoped.get("requested_provider"):
            runtime["requested_provider"] = scoped["requested_provider"]
    return runtime


def native_compression_fast(context: dict, model: str, base_url: str) -> bool:
    """Raw speed extras do not authorize native compression's Fast beta."""
    from agent import auxiliary_client as aux

    if context.get("task") != "compression":
        return False
    projection = apply_compression_service_tier(
        {}, aux._get_auxiliary_task_config("compression"),
        provider=aux._fallback_provider_from_label(
            context.get("stream_provider") or context.get("provider") or "unknown"),
        model=model, base_url=base_url,
        api_mode="anthropic_messages",
    )
    return projection.get("extra_body", {}).get("speed") == "fast"


def apply_compression_service_tier(
    kwargs: dict[str, Any], task_config: dict[str, Any], *, provider: str,
    model: str | None, base_url: str | None, api_mode: str | None,
) -> dict[str, Any]:
    """Project explicit compression selection through the shared destination gate."""
    selected = task_config.get("service_tier")
    if selected is None:
        return kwargs
    if not isinstance(selected, str) or selected.strip().lower() not in {
        "priority", "fast", "ultrafast", "normal", "off", "",
    }:
        raise ValueError("auxiliary.compression.service_tier must be priority, fast, ultrafast, normal or off")
    from agent.fast_mode import parse_exact_service_tier
    from hermes_cli.models import resolve_fast_mode_overrides

    selected = parse_exact_service_tier(selected)
    # This explicit policy owns only paid-tier fields, after all legacy extras/profile
    # merges. Missing/None deliberately leaves the historical behavior untouched.
    kwargs = dict(kwargs)
    kwargs.pop("service_tier", None)
    kwargs.pop("speed", None)
    body = dict(kwargs.get("extra_body") or {})
    body.pop("service_tier", None)
    body.pop("speed", None)
    if selected:
        # Native eligibility is the sole authority: named proxy declarations cannot
        # grant a paid tier, and identity/transport metadata is not part of this API.
        projection = resolve_fast_mode_overrides(
            model, provider=provider, base_url=base_url, tier=selected,
        ) or {}
        if ("speed" in projection) == (api_mode == "anthropic_messages"):
            body.update(projection)
    if body:
        kwargs["extra_body"] = body
    else:
        kwargs.pop("extra_body", None)
    return kwargs
