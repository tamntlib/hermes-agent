"""Offline session route selection and paid-tier validation for TUI/Desktop controls."""
from __future__ import annotations


def live_route(agent) -> dict:
    """The actual live wire route, including the named identity before resolution."""
    mode = getattr(agent, "api_mode", None)
    base = getattr(agent, "_anthropic_base_url", None) if mode == "anthropic_messages" else None
    return {
        "provider": getattr(agent, "provider", None),
        "requested_provider": getattr(agent, "requested_provider", None),
        "base_url": base or getattr(agent, "base_url", None), "api_mode": mode,
    }


def selected_route(override: dict | None = None) -> tuple[str, dict]:
    """Resolve a draft/pending pick using config only; never credentials or discovery."""
    from tui_gateway import server
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.runtime_provider_custom import _get_named_custom_provider

    config = load_user_config_effective()
    model_config = config.get("model")
    model_config = model_config if isinstance(model_config, dict) else {}
    override = override or {}
    default_model, startup_provider = server._resolve_startup_runtime()
    model = override.get("model") or default_model
    if override.get("model") and not override.get("provider"):
        # The builder's dict-override branch resolves requested=None config-first,
        # unlike a fresh unpicked draft, which follows the launch route.
        from hermes_cli.runtime_provider import resolve_requested_provider
        requested = resolve_requested_provider()
    else:
        requested = override.get("provider") or startup_provider or model_config.get("provider")
    named = _get_named_custom_provider(requested, config=config, resolve_credentials=False) if requested else None
    if named:
        return model, {"provider": "custom", "requested_provider": requested,
                       "base_url": override.get("base_url") or named["base_url"],
                       "api_mode": override.get("api_mode") or named.get("api_mode") or "chat_completions"}
    same_route = not override.get("provider") or requested == model_config.get("provider")
    return model, {"provider": requested, "requested_provider": requested,
                   "base_url": override.get("base_url") or (model_config.get("base_url") if same_route else None),
                   "api_mode": override.get("api_mode") or (model_config.get("api_mode") if same_route else None)}


def validate_selection(tier: str, *, agent=None, override: dict | None = None) -> dict:
    """Return static wire overrides or raise before any session/config mutation.

    Auto/cold are windows over priority, not provider tier names. Draft validation is
    strictly offline; a built subscription agent retains its existing account check.
    """
    from hermes_cli.models import resolve_fast_mode_overrides

    if not tier or tier == "normal":
        return {}
    model, route = (getattr(agent, "model", None), live_route(agent)) if agent is not None else selected_route(override)
    if not model:
        raise ValueError("fast mode is not available without a selected model")
    selected = "ultrafast" if tier == "ultrafast" else "priority"
    overrides = resolve_fast_mode_overrides(model, **route, tier=selected)
    if overrides is not None and selected == "ultrafast" and agent is not None:
        from agent.codex_service_tiers import ultrafast_offered
        if not ultrafast_offered(model, **route, access_token=getattr(agent, "api_key", None)):
            overrides = None
    if overrides is None:
        raise ValueError(f"{tier} mode is not available for this model")
    return overrides if tier not in {"auto", "cold"} else {}


def tier_applies(agent, model: str, provider: str, *, route_known: bool,
                 tier: str | None = None, profile_home=None) -> bool:
    """Profile-bound Fast display policy; pending picks resolve their own offline route."""
    from hermes_cli.models import resolve_fast_mode_overrides
    from tui_gateway import server

    try:
        with server._profile_build_scope(profile_home):
            if route_known and agent is not None:
                route = live_route(agent)
            else:
                _, route = selected_route({"model": model, "provider": provider})
            return resolve_fast_mode_overrides(model, **route, tier=tier) is not None
    except ValueError:
        # Bad declarations refuse controls and must never light up the Fast indicator.
        return False
