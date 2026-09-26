"""Custom risk ladder and quote construction on top of FollowSM's `evaluate_risk_action`.

    NONE              -> quote at 1.0x base spread
    WIDEN_SPREAD_1_5X -> quote at 1.5x base spread
    WIDEN_SPREAD_2X   -> quote at 2.0x base spread
    HALT_MAKER_QUOTES -> cancel every resting maker quote
"""

from __future__ import annotations

import logging
import math
from typing import Literal, Optional

from followsm_sdk import RiskConfig, evaluate_risk_action
from pydantic import BaseModel

from bot.market_monitor import MarketSignal

log = logging.getLogger(__name__)

RiskAction = Literal["NONE", "WIDEN_SPREAD_1_5X", "WIDEN_SPREAD_2X", "HALT_MAKER_QUOTES"]

SPREAD_MULTIPLIERS = {"NONE": 1.0, "WIDEN_SPREAD_1_5X": 1.5, "WIDEN_SPREAD_2X": 2.0}
MAX_SKEW = 0.05
DIRECTION_SIGN = {"bullish_if_yes": 1, "bearish_if_yes": -1, "neutral": 0}


class RiskDecision(BaseModel):
    action: RiskAction
    backend_action: str
    spread_multiplier: Optional[float]

    @property
    def halt(self) -> bool:
        return self.action == "HALT_MAKER_QUOTES"


class QuotePlan(BaseModel):
    token_id: str
    bid: float
    ask: float
    size: float


class RiskEngine:
    """Re-derives the recommended action locally with the bot's own RiskConfig thresholds."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def evaluate(self, signal: MarketSignal) -> RiskDecision:
        action = evaluate_risk_action(signal.snapshot, self.config)
        if action != signal.backend_action:
            log.debug("Local ladder %s overrides backend %s", action, signal.backend_action)
        return RiskDecision(
            action=action,
            backend_action=signal.backend_action,
            spread_multiplier=SPREAD_MULTIPLIERS.get(action),
        )


def _floor_to_tick(price: float, tick: float) -> float:
    return round(math.floor(price / tick + 1e-9) * tick, 6)


def _ceil_to_tick(price: float, tick: float) -> float:
    return round(math.ceil(price / tick - 1e-9) * tick, 6)


def build_quote_plan(
    signal: MarketSignal,
    decision: RiskDecision,
    mid: float,
    *,
    base_half_spread: float,
    tick_size: float,
    momentum_skew_per_pct: float,
    size: float,
) -> Optional[QuotePlan]:
    """Two-sided YES quote around the Polymarket mid, skewed toward the spot-implied fair value.

    Lead-lag arbitrage: Binance spot usually reprices before the prediction market.
    The move Polymarket *should* have made is `k * Δspot% * direction`; whatever part of it
    `prob_delta_15m` has not yet absorbed shifts the quote centre (capped at ±MAX_SKEW).
    Returns None when the ladder says HALT or no directional event is mapped.
    """
    if decision.halt or decision.spread_multiplier is None or signal.event is None:
        return None

    direction = DIRECTION_SIGN[signal.event.direction]
    expected_prob_move = momentum_skew_per_pct * signal.price_delta_15m_pct * 100 * direction
    unabsorbed = expected_prob_move - signal.prob_delta_15m if direction else 0.0
    center = mid + max(-MAX_SKEW, min(MAX_SKEW, unabsorbed))

    half_spread = base_half_spread * decision.spread_multiplier
    bid = max(tick_size, _floor_to_tick(center - half_spread, tick_size))
    ask = min(1.0 - tick_size, _ceil_to_tick(center + half_spread, tick_size))
    if bid >= ask:
        return None
    return QuotePlan(token_id=signal.event.yes_token_id, bid=bid, ask=ask, size=size)
