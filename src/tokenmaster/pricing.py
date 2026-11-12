"""Token pricing table for local JSONL usage & cost fallback calculations."""

from __future__ import annotations

from typing import Optional

# Prices in USD per 1M tokens: (input, cached_input, cache_write, output)
MODEL_PRICING_PER_1M: dict[str, tuple[float, float, float, float]] = {
    # Claude models
    "claude-opus-4": (15.0, 1.50, 18.75, 75.0),
    "claude-sonnet-4": (3.0, 0.30, 3.75, 15.0),
    "claude-3-7-sonnet": (3.0, 0.30, 3.75, 15.0),
    "claude-3-5-sonnet": (3.0, 0.30, 3.75, 15.0),
    "claude-3-5-haiku": (0.80, 0.08, 1.00, 4.0),
    "claude-haiku-4": (0.80, 0.08, 1.00, 4.0),
    # OpenAI / Codex models
    "gpt-5": (2.50, 0.25, 2.50, 10.0),
    "codex-mini": (1.50, 0.375, 1.50, 6.0),
    "o3": (2.0, 0.50, 2.0, 8.0),
    "o4-mini": (1.10, 0.275, 1.10, 4.40),
    "gpt-4.1": (2.0, 0.50, 2.0, 8.0),
    "gpt-4o": (2.50, 1.25, 2.50, 10.0),
    # Gemini / Antigravity models
    "gemini-2.5-pro": (1.25, 0.31, 1.25, 10.0),
    "gemini-2.5-flash": (0.15, 0.0375, 0.15, 0.60),
    "gemini-4": (1.50, 0.375, 1.50, 10.0),
    # Grok / xAI models
    "grok-4": (3.0, 0.75, 3.0, 15.0),
    "grok-3": (3.0, 0.75, 3.0, 15.0),
    "grok-3-mini": (0.30, 0.075, 0.30, 0.50),
}


def estimate_token_cost_usd(
    model: Optional[str],
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int = 0,
    default_rates: tuple[float, float, float, float] = (3.0, 0.30, 3.75, 15.0),
) -> float:
    """Estimate USD cost from token counts."""
    rates = default_rates
    if model:
        m_lower = model.lower()
        for prefix, r in MODEL_PRICING_PER_1M.items():
            if prefix in m_lower:
                rates = r
                break

    in_rate, cache_read_rate, cache_write_rate, out_rate = rates
    cost = (
        (max(0, input_tokens) * in_rate)
        + (max(0, cached_input_tokens) * cache_read_rate)
        + (max(0, cache_write_tokens) * cache_write_rate)
        + (max(0, output_tokens) * out_rate)
    ) / 1_000_000.0
    return cost
