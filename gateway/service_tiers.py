"""Messaging turn request overrides for session processing tiers."""

import logging

logger = logging.getLogger(__name__)


INHERIT_TIER = object()


def turn_agent_config(self, user_message: str, model: str, runtime_kwargs: dict, *, tier=INHERIT_TIER) -> dict:
    """Effective model/runtime config for one turn. With `/fast` priority on, fast-mode
    ``request_overrides`` are deep-merged OVER the per-provider ones so both reach the model."""
    from gateway.run import _deep_merge_request_overrides
    from agent.fast_mode import STATIC_TIERS
    from hermes_cli.models import resolve_fast_mode_overrides
    # Tests bind this method onto bare namespaces, so no class-level tables here.
    runtime = {
        k: runtime_kwargs.get(k) for k in (
            "api_key", "base_url", "provider", "requested_provider", "api_mode", "command", "args",
            "credential_pool", "max_tokens", "capabilities",
        )
    }
    runtime["args"] = list(runtime["args"] or [])
    runtime["capabilities"] = dict(runtime["capabilities"] or {})
    base_request_overrides = dict(runtime_kwargs.get("request_overrides") or {})
    route = {
        "model": model,
        "runtime": runtime,
        "signature": (
            model, runtime["provider"], runtime["requested_provider"], runtime["base_url"],
            runtime["api_mode"], runtime["command"], tuple(runtime["args"]),
        ),
    }
    if tier is INHERIT_TIER:
        tier = getattr(self, "_service_tier", None)
    if tier not in STATIC_TIERS:
        # None / auto / cold: the bounded window is applied per request by agent.fast_mode.
        route["request_overrides"] = base_request_overrides
        return route
    try:
        overrides = resolve_fast_mode_overrides(
            route["model"], provider=runtime["provider"], base_url=runtime["base_url"],
            requested_provider=runtime.get("requested_provider"), api_mode=runtime.get("api_mode"), tier=tier,
        )
    except ValueError as exc:
        logger.warning("Invalid Fast service tier configuration: %s", exc)
        overrides = None
    except Exception:
        logger.debug("Fast tier projection unavailable", exc_info=True)
        overrides = None
    # Fast-mode keys (service_tier / speed) are top-level and don't collide with extra_body.
    route["request_overrides"] = _deep_merge_request_overrides(base_request_overrides, overrides or {})
    return route
