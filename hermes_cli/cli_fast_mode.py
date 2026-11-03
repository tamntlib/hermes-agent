"""Classic CLI session processing-tier command."""

def handle_fast_command(self, cmd: str):
    """Handle /fast — toggle fast mode (OpenAI Priority Processing / Anthropic Fast Mode).
    Session-scoped by default; ``--global`` persists agent.service_tier to config.yaml
    (parity with /model and /reasoning)."""
    from hermes_cli import cli_commands_mixin as commands
    from hermes_cli.cli_info_mixin import _fast_route_context
    from hermes_cli.models import resolve_fast_mode_overrides

    model, runtime = _fast_route_context(self)
    # Determine the branding for the current model
    anthropic = commands._probe("hermes_cli.models", "_is_anthropic_fast_model", None, model)
    feature_name = commands._t("fast.feature_generic" if anthropic is None
                      else "fast.feature_anthropic" if anthropic else "fast.feature_openai")
    raw = commands._command_arg(cmd)
    usage = commands._dim_line(commands._t("fast.usage"))
    if not raw or raw.lower() == "status":
        from agent.fast_mode import service_tier_word
        tier = self.service_tier
        status = service_tier_word(tier)
        if tier is not None:
            try:
                supported = resolve_fast_mode_overrides(
                    model, **runtime, tier="ultrafast" if tier == "ultrafast" else "priority")
            except ValueError as exc:
                return commands._cp(commands._dim_line(str(exc)), usage)
            if supported is None:
                status = "normal"
        return commands._cp(commands._accent_line(commands._t("fast.status", feature=feature_name, status=status)), usage)
    arg, explicit_global = commands._split_scope_flags(raw)
    if arg not in commands._FAST_TIERS:
        return commands._cp(commands._dim_line(commands._t("shared.unknown_argument", arg=arg)), usage)
    tier, saved_value = commands._FAST_TIERS[arg]
    if tier is not None:
        try:
            supported = resolve_fast_mode_overrides(
                model, **runtime, tier="ultrafast" if tier == "ultrafast" else "priority")
        except ValueError as exc:
            return commands._cp(commands._dim_line(str(exc)))
        if supported is None:
            key = "fast.ultrafast_not_supported" if tier == "ultrafast" else "fast.not_supported"
            return commands._cp(commands._dim_line(commands._t(key, model=model or "?")), usage)
    if tier == "ultrafast":
        if not commands._probe("agent.codex_service_tiers", "ultrafast_offered_for", False, self, model):
            return commands._cp(commands._dim_line(commands._t("fast.ultrafast_not_supported", model=model or "?")), usage)
        feature_name = commands._t("fast.feature_ultrafast")
    self.service_tier = tier
    commands._retire_agent(self)  # Force agent re-init with new service-tier config
    saved = explicit_global and commands._save("agent.service_tier", saved_value)
    outcome = commands._scope_outcome(explicit_global, saved)
    commands._cp(commands._accent_line(commands._t("fast.set_to", feature=feature_name, value=saved_value.upper(), scope=outcome)))
