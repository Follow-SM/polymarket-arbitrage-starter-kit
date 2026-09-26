"""Runtime configuration: environment variables -> BotConfig + custom FollowSM RiskConfig."""

from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv
from followsm_sdk import RiskConfig
from pydantic import BaseModel, Field

load_dotenv()


class BotConfig(BaseModel):
    followsm_api_key: Optional[str] = None
    polymarket_private_key: Optional[str] = None
    polymarket_funder: Optional[str] = None
    polymarket_signature_type: int = 0
    polymarket_host: str = "https://clob.polymarket.com"
    live_trading: bool = False

    symbol: str = "BTCUSDT"
    poll_interval_secs: float = Field(default=5.0, gt=0)
    quote_size: float = Field(default=10.0, gt=0)
    base_half_spread: float = Field(default=0.02, gt=0, lt=0.5)
    tick_size: float = 0.01
    # How far (in probability points) a 1% spot move shifts the quote centre toward fair value.
    momentum_skew_per_pct: float = 0.02

    risk: RiskConfig = Field(default_factory=RiskConfig)


def _env(name: str) -> Optional[str]:
    value = os.getenv(name, "").strip()
    return value or None


def load_config() -> BotConfig:
    risk = RiskConfig(
        vpin_percentile_widen_threshold=float(os.getenv("VPIN_PERCENTILE_WIDEN_THRESHOLD", "0.90")),
        vpin_percentile_halt_threshold=float(os.getenv("VPIN_PERCENTILE_HALT_THRESHOLD", "0.95")),
        vpin_widen_threshold=float(os.getenv("VPIN_WIDEN_THRESHOLD", "0.80")),
        vpin_halt_threshold=float(os.getenv("VPIN_HALT_THRESHOLD", "0.90")),
        min_semantic_confidence=float(os.getenv("MIN_SEMANTIC_CONFIDENCE", "0.65")),
    )
    config = BotConfig(
        followsm_api_key=_env("FOLLOWSM_API_KEY"),
        polymarket_private_key=_env("POLYMARKET_PRIVATE_KEY"),
        polymarket_funder=_env("POLYMARKET_FUNDER"),
        polymarket_signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
        live_trading=os.getenv("LIVE_TRADING", "false").lower() in ("1", "true", "yes"),
        symbol=os.getenv("SYMBOL", "BTCUSDT").upper(),
        poll_interval_secs=float(os.getenv("POLL_INTERVAL_SECS", "5")),
        quote_size=float(os.getenv("QUOTE_SIZE", "10")),
        base_half_spread=float(os.getenv("BASE_HALF_SPREAD", "0.02")),
        risk=risk,
    )
    if config.live_trading and not config.polymarket_private_key:
        raise ValueError("LIVE_TRADING=true requires POLYMARKET_PRIVATE_KEY")
    return config
