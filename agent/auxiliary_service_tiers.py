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


def apply_compression_service_tier(
    kwargs: dict[str, Any], task_config: dict[str, Any], *, provider: str,
    model: str | None, base_url: str | None, api_mode: str | None,
    requested_provider: str | None,
) -> dict[str, Any]:
    """Apply only explicitly declared capabilities; never use builtin fast allowlists."""
    selected = task_config.get("service_tier")
    if selected is None:
        return kwargs
    if not isinstance(selected, str) or selected.strip().lower() not in {
        "priority", "fast", "ultrafast", "normal", "off", "",
    }:
        raise ValueError("auxiliary.compression.service_tier must be priority, fast, ultrafast, normal or off")
    from agent.fast_mode import parse_exact_service_tier
    from hermes_cli.service_tiers import get_configured_service_tiers, resolve_service_tier_overrides

    selected = parse_exact_service_tier(selected)
    # This explicit policy owns only paid-tier fields, after all legacy extras/profile
    # merges. Missing/None deliberately leaves the historical behavior untouched.
    kwargs = dict(kwargs)
    kwargs.pop("service_tier", None)
    kwargs.pop("speed", None)
    kwargs.pop("_compression_service_tiers", None)
    body = dict(kwargs.get("extra_body") or {})
    body.pop("service_tier", None)
    body.pop("speed", None)
    route: dict[str, Any] = dict(provider=provider, requested_provider=requested_provider,
                                 base_url=base_url, api_mode=api_mode)
    if selected:
        declared = get_configured_service_tiers(model, **route)
        if declared is not None and selected in declared:
            overrides = resolve_service_tier_overrides(model, tier=selected, **route) or {}
            body.update(overrides)
            if overrides.get("speed") == "fast":
                # Private authorization receipt, consumed only by the Messages adapter.
                # A legacy raw extra_body.speed must not gain these new semantics.
                kwargs["_compression_service_tiers"] = declared
    if body:
        kwargs["extra_body"] = body
    else:
        kwargs.pop("extra_body", None)
    return kwargs
