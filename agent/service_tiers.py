"""Project explicit service-tier declarations onto provider-specific request builders."""


def apply_anthropic_service_tier_overrides(agent, kwargs: dict, request_overrides: dict) -> None:
    """Retain native defaults unless this exact named route declares priority support."""
    if request_overrides.get("speed") != "fast":
        return
    from agent.anthropic_adapter import apply_anthropic_service_tier_override
    from hermes_cli.service_tiers import get_configured_service_tiers

    base_url = getattr(agent, "_anthropic_base_url", None) or getattr(agent, "base_url", None)
    declared = get_configured_service_tiers(
        agent.model, provider=getattr(agent, "provider", None),
        requested_provider=getattr(agent, "requested_provider", None),
        base_url=base_url, api_mode="anthropic_messages",
    )
    apply_anthropic_service_tier_override(
        kwargs, speed=request_overrides["speed"], service_tiers=declared,
        base_url=base_url, is_oauth=agent._is_anthropic_oauth,
        drop_context_1m_beta=bool(getattr(agent, "_oauth_1m_beta_disabled", False)),
    )
