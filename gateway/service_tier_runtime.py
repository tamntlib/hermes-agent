"""Exact named runtime identity for gateway service-tier controls and persistence."""
from __future__ import annotations

import json
import logging
from contextlib import suppress
from typing import Any, Optional

from gateway.session import SessionSource
from gateway.service_tiers import INHERIT_TIER

logger = logging.getLogger("gateway.run")


class GatewayServiceTierRuntimeMixin:
    def _resolve_session_agent_runtime(
        self, *, source: Optional[SessionSource] = None, session_key: Optional[str] = None,
        user_config: Optional[dict] = None, fallback: bool = True,
    ) -> tuple[str, dict]:
        """Keep the current session resolver and preserve its explicit route identity."""
        from gateway.run_turn_prepare import GatewayTurnPrepareMixin
        model, runtime_kwargs = GatewayTurnPrepareMixin._resolve_session_agent_runtime(
            self, source=source, session_key=session_key, user_config=user_config, fallback=fallback,
        )
        runtime_kwargs["requested_provider"] = runtime_kwargs.get("requested_provider") or runtime_kwargs.get("provider")
        return model, runtime_kwargs

    def _resolve_turn_agent_config(
        self, user_message: str, model: str, runtime_kwargs: dict, *, tier=INHERIT_TIER,
    ) -> dict:
        """Build one turn's route without losing provider overrides."""
        from gateway.service_tiers import turn_agent_config
        return turn_agent_config(self, user_message, model, runtime_kwargs, tier=tier)

    def _sync_session_model_from_agent(self, session_id: str, agent: Any) -> None:
        """Persist the runtime model/provider a gateway turn actually used (provider fallback can
        switch them after the row was created). Runs in the ``run_sync`` executor thread, so it
        uses the sync ``SessionDB`` (``_db``), not the AsyncSessionDB forwarder."""
        if not session_id or agent is None or self._session_db is None:
            return
        model = getattr(agent, "model", None)
        if not model:
            return
        runtime = {k: getattr(agent, k, None) for k in ("provider", "base_url", "api_mode")}
        from hermes_cli.runtime_provider_custom import session_provider_identity
        runtime["provider"] = session_provider_identity(
            runtime["provider"], getattr(agent, "requested_provider", None), runtime["base_url"], model)
        runtime["fallback_active"] = bool(getattr(agent, "_fallback_activated", False))
        runtime = {k: v for k, v in runtime.items() if v not in (None, "")}
        try:
            db = self._session_db._db
            row = db.get_session(session_id)
            if not row:
                return
            # Legacy backfill: canonical Bot Chats created BEFORE the follow_profile_config contract existed
            # carry no marker, yet they are still the plugin-owned forever-DM. The plugin's own identity
            # rule is "the profile's session titled exactly 'Bot Chat'" (UNIQUE(title) makes that an exact
            # registry, and pre-policy rows may be visible OR hidden), so mirror that rule here. Without
            # this, every Bot Chat that already exists in the field stays pinned to its stale stored
            # provider until the user deletes it — the exact live-report shape (#89497 / #94818).
            raw_config = row.get("model_config")
            config = {}
            with suppress(Exception):
                config = json.loads(raw_config) if raw_config else {}
            if not isinstance(config, dict):
                config = {}
            gateway_runtime = dict(config.get("gateway_runtime") or {})
            if row.get("model") == model and all(gateway_runtime.get(k) == v for k, v in runtime.items()):
                return
            config["gateway_runtime"] = runtime
            db.update_session_meta(session_id, json.dumps(config), model=model)
        except Exception:
            logger.debug("Failed to sync gateway session model metadata", exc_info=True)
