"""Explicit paid-tier declarations for named provider routes."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any


_SESSION_DECLARATIONS: ContextVar = ContextVar("session_tier_declarations", default=None)


@contextmanager
def service_tier_config_scope(config):
    """Bind paid declarations for one owning session, not general CLI/profile config reads."""
    from hermes_constants import hermes_home_key

    token = _SESSION_DECLARATIONS.set((hermes_home_key(), config))
    try:
        yield
    finally:
        _SESSION_DECLARATIONS.reset(token)


def _declaration_config():
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_constants import hermes_home_key

    scoped = _SESSION_DECLARATIONS.get()
    if scoped is not None and scoped[0] == hermes_home_key() and scoped[1] is not None:
        return scoped[1]
    return load_user_config_effective()


def _declared_tiers(entry: Any, path: str) -> tuple[str, ...] | None:
    if not isinstance(entry, dict) or "service_tiers" not in entry:
        return None
    value = entry["service_tiers"]
    if not isinstance(value, list):
        raise ValueError(f"{path}.service_tiers must be a list of 'priority' and/or 'ultrafast' (or [])")
    for index, tier in enumerate(value):
        if not isinstance(tier, str) or tier not in {"priority", "ultrafast"}:
            raise ValueError(f"{path}.service_tiers[{index}] must be 'priority' or 'ultrafast'")
    return tuple(value)


def _named_provider_entry(config: dict[str, Any], key: str) -> tuple[dict[str, Any] | None, str]:
    from hermes_cli.config_providers import coerce_provider_id, find_provider_entry

    _, entry = find_provider_entry(config.get("providers"), key)
    path = f"providers.{key}"
    if entry is not None:
        return entry, path
    legacy = config.get("custom_providers")
    if isinstance(legacy, list):
        for index, candidate in enumerate(legacy):
            if isinstance(candidate, dict) and coerce_provider_id(candidate.get("name")) == key:
                return candidate, f"custom_providers[{index}]"
    return None, path


def get_configured_service_tiers(
    model_id: str | None, *, provider: str | None = None,
    requested_provider: str | None = None, base_url: str | None = None,
    api_mode: str | None = None, config: dict[str, Any] | None = None,
) -> tuple[str, ...] | None:
    """Return declared tiers; () denies mismatched routes, None keeps builtin policy."""
    from hermes_cli.config_providers import (
        _normalize_custom_provider_entry, _normalize_provider_models, is_provider_enabled,
    )
    from hermes_cli.route_identity import normalize_route_base_url

    identity = str(requested_provider or "")
    if identity in {"", "custom", "auto"}:
        return None
    key = identity.removeprefix("custom:")
    if config is None:
        config = _declaration_config()
    raw_entry, path = _named_provider_entry(config, key)
    if raw_entry is None:
        return () if key and (provider == "custom" or identity.startswith("custom:")) else None
    if not is_provider_enabled(raw_entry):
        return ()
    provider_tiers = _declared_tiers(raw_entry, path)
    model_metadata, _ = _normalize_provider_models(raw_entry.get("models"))
    model_tiers = _declared_tiers(model_metadata.get(model_id) if model_id is not None else None,
                                  f"{path}.models.{model_id}")
    declared = model_tiers if model_tiers is not None else provider_tiers
    if declared is None:
        return None
    entry = _normalize_custom_provider_entry(raw_entry, provider_key=key)
    if not entry or not base_url or not api_mode:
        return ()
    if normalize_route_base_url(entry["base_url"]) != normalize_route_base_url(base_url):
        return ()
    if entry.get("api_mode") != api_mode:
        return ()
    return model_tiers if model_tiers is not None else provider_tiers


def resolve_service_tier_overrides(
    model_id: str | None, *, provider: str | None = None,
    requested_provider: str | None = None, base_url: str | None = None,
    api_mode: str | None = None, tier: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Map explicit route declarations before consulting the builtin model allowlists."""
    from hermes_cli import models

    declared = get_configured_service_tiers(
        model_id, provider=provider, requested_provider=requested_provider,
        base_url=base_url, api_mode=api_mode, config=config,
    )
    if declared is not None:
        selected = "priority" if tier is None else tier
        if selected not in declared:
            return None
        if api_mode in {"codex_responses", "chat_completions"}:
            return {"service_tier": selected}
        if api_mode == "anthropic_messages" and selected == "priority":
            return {"speed": "fast"}
        return None
    if not models.model_supports_fast_mode(model_id):
        return None
    route_provider = provider if requested_provider in {None, "", "auto"} else requested_provider
    if (route_provider or base_url) and not models._fast_mode_route_supported(model_id, route_provider, base_url):
        return None
    if tier == "ultrafast":
        return {"service_tier": "ultrafast"} if models.model_supports_ultrafast(model_id) else None
    return {"speed": "fast"} if models._is_anthropic_fast_model(model_id) else {"service_tier": "priority"}
