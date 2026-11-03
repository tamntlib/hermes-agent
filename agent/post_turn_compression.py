"""A post-reply trigger; execution stays on each surface's existing compress path."""
from __future__ import annotations

import math


def post_turn_compression_due(agent, history: list, config: dict | None = None) -> bool:
    """Start just before the effective hard trigger, without changing that trigger.

    ``threshold_buffer`` is a fraction of the context window (0.02 = two percentage
    points). None disables it; zero compresses at the hard trigger after delivery.
    Only the built-in local compressor participates; native/provider engines own
    their own automatic compaction policy.
    """
    if config is None:
        from hermes_cli.config import load_config
        config = load_config()
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    compression = config.get("compression") or {}
    buffer = compression.get("threshold_buffer", DEFAULT_CONFIG["compression"]["threshold_buffer"])
    if buffer is None or isinstance(buffer, bool):
        return False
    try:
        buffer = float(buffer)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(buffer) or not 0 <= buffer < 1:
        return False
    if not getattr(agent, "compression_enabled", False) or len(history) < 4:
        return False
    if getattr(agent, "api_mode", "") == "codex_app_server" or getattr(agent, "codex_responses_native_compaction", False) is True:
        return False
    from agent.context_compressor import ContextCompressor
    compressor = getattr(agent, "context_compressor", None)
    if not isinstance(compressor, ContextCompressor):
        return False
    soft_tokens = compressor.threshold_tokens - int(compressor.context_length * buffer)
    if soft_tokens <= 0:
        # A small absolute cap can be smaller than the window-relative buffer.
        # Fall back to its hard boundary, not "disabled" or "compress every turn".
        soft_tokens = compressor.threshold_tokens
    from agent.turn_context import _preflight_request_tokens
    return _preflight_request_tokens(agent, history, getattr(agent, "_cached_system_prompt", "") or "") >= soft_tokens
