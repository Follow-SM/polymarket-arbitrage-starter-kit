"""Asynchronous FollowSM confluence polling and event parsing."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

import requests
from followsm_sdk import (
    ConfluenceSnapshot,
    FollowSMClient,
    PolymarketEventMetrics,
    RateLimitExceededException,
)
from pydantic import BaseModel

log = logging.getLogger(__name__)

PRICING_URL = "https://follow-sm.com/pricing"
RATE_LIMIT_BACKOFF_SECS = 60.0
RATE_LIMIT_CTA = (
    "\n  Free tier exhausted. Upgrade to DEVELOPER ($199/mo: 300 req/min, 50+ Binance pairs, "
    "sub-10ms snapshots) or ENTERPRISE ($499/mo: + WebSocket streams)\n"
    f"  -> {PRICING_URL}\n"
    "  Then set FOLLOWSM_API_KEY in .env and restart the bot.\n"
)


class MarketSignal(BaseModel):
    """The subset of a ConfluenceSnapshot the quoting loop acts on."""

    symbol: str
    timestamp_ms: int
    spot_price: float
    price_delta_15m_pct: float
    vpin: float
    vpin_percentile: Optional[float]
    ob_toxicity_1pct: float
    prob_delta_15m: float
    event: Optional[PolymarketEventMetrics]
    backend_action: str
    snapshot: ConfluenceSnapshot


def parse_snapshot(snapshot: ConfluenceSnapshot) -> MarketSignal:
    """Extract VPIN, 1% depth toxicity and the most-moved Polymarket event's prob_delta_15m."""
    micro = snapshot.binance_microstructure
    event = max(
        snapshot.polymarket_confluence.active_events,
        key=lambda e: abs(e.prob_delta_15m),
        default=None,
    )
    return MarketSignal(
        symbol=snapshot.symbol,
        timestamp_ms=snapshot.timestamp_ms,
        spot_price=micro.price,
        price_delta_15m_pct=micro.price_delta_15m_pct,
        vpin=micro.vpin,
        vpin_percentile=micro.vpin_percentile,
        ob_toxicity_1pct=micro.ob_toxicity_1pct,
        prob_delta_15m=event.prob_delta_15m if event else 0.0,
        event=event,
        backend_action=snapshot.composite_signals.recommended_action,
        snapshot=snapshot,
    )


class MarketMonitor:
    """Polls `get_confluence_snapshot` without blocking the event loop."""

    def __init__(self, client: FollowSMClient, symbol: str, poll_interval_secs: float) -> None:
        self.client = client
        self.symbol = symbol
        self.poll_interval_secs = poll_interval_secs

    async def fetch(self) -> ConfluenceSnapshot:
        return await asyncio.to_thread(self.client.get_confluence_snapshot, self.symbol)

    async def stream(self) -> AsyncIterator[Optional[MarketSignal]]:
        """Yield one MarketSignal per poll, or None whenever the feed is unavailable.

        Consumers should treat None as "fail closed" and pull their quotes.
        AuthenticationError (bad key / wrong plan) is not caught: it is fatal.
        """
        while True:
            delay = self.poll_interval_secs
            try:
                signal: Optional[MarketSignal] = parse_snapshot(await self.fetch())
            except RateLimitExceededException as exc:
                log.warning("%s%s", exc, RATE_LIMIT_CTA)
                signal, delay = None, RATE_LIMIT_BACKOFF_SECS
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status == 404:
                    log.info("No fresh confluence data for %s yet", self.symbol)
                else:
                    log.warning("FollowSM API error (%s): %s", status, exc)
                signal = None
            except requests.RequestException as exc:
                log.warning("FollowSM network error: %s", exc)
                signal = None
            yield signal
            await asyncio.sleep(delay)
