"""Native-route session fixtures independent of optional custom Fast policy."""
import json
from types import SimpleNamespace

NATIVE_MODEL = "gpt-6-astra"
NATIVE_URL = "https://api.openai.com/v1"


def native_config(tier="priority", provider="openai"):
    return {
        "model": {"default": NATIVE_MODEL, "provider": provider,
                  "base_url": NATIVE_URL, "api_mode": "codex_responses"},
        "agent": {"service_tier": tier}, "compression": {"enabled": False},
    }


def native_parent(parent, provider="openai"):
    parent.model = NATIVE_MODEL
    parent.provider = parent.requested_provider = provider
    parent.base_url, parent.api_mode = NATIVE_URL, "codex_responses"
    return parent


def native_route(provider="openai"):
    return dict(provider=provider, model=NATIVE_MODEL, base_url=NATIVE_URL,
                api_mode="codex_responses", api_key="offline-synthetic", fallback_providers=[])


def authority_for(home):
    return SimpleNamespace(profile_id=str(home), runner=SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True)),
                           db=SimpleNamespace(db_path=home / "state.db"))


def policy_for(home, config, *, follows=False):
    from gateway.session_policy import LocalSessionPolicy
    return LocalSessionPolicy("gui", "desktop", str(home), NATIVE_MODEL, (),
                              json.dumps(config), json.dumps({"follow_profile_config": follows}), "{}")
