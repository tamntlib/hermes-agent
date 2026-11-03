"""Exact next-turn route metadata for off-turn paid-tier controls (no URL inference)."""


def tier_route(authority, policy, agent=None, live=None):
    """Use a pending/frozen local selection before a resident agent; scope cold resolution to its owner."""
    from gateway.session_authorities import owner_scope
    with owner_scope(authority):
        if policy is not None:
            from gateway.session_route_identity import _named_entry, resolved_route
            config, model = policy.effective(authority, secrets=False)
            block = config.get('model') or {}
            requested, base_url = resolved_route(block, config)
            entry = _named_entry(requested, config) or {}
            return dict(model=model, provider='custom' if entry else requested,
                        requested_provider=requested, base_url=base_url,
                        api_mode=block.get('api_mode') or entry.get('api_mode'), config=config)
        if agent is not None:
            from agent.voice_turn_route import session_runtime_view
            view = session_runtime_view(agent)
            return {key: getattr(view, key, None) for key in
                    ('model', 'provider', 'requested_provider', 'base_url', 'api_mode')}
        model, runtime = authority.runner._resolve_session_agent_runtime(
            source=live.source, session_key=live.route, fallback=False)
        return dict(model=model, **{key: runtime.get(key) for key in
                                   ('provider', 'requested_provider', 'base_url', 'api_mode')})
