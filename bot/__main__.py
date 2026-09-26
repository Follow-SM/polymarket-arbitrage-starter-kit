"""Entry point: `python -m bot` (run from the repository root)."""

from __future__ import annotations

import asyncio
import logging

import httpx
from followsm_sdk import FollowSMClient

from bot.execution import LiveOrderManager, MockOrderManager, OrderManager
from bot.market_monitor import MarketMonitor
from bot.risk_engine import RiskEngine, build_quote_plan
from config import BotConfig, load_config

log = logging.getLogger("bot")


def build_order_manager(config: BotConfig) -> OrderManager:
    if not config.live_trading:
        return MockOrderManager(config.polymarket_host)
    return LiveOrderManager(
        config.polymarket_host,
        private_key=config.polymarket_private_key,
        funder=config.polymarket_funder,
        signature_type=config.polymarket_signature_type,
    )


async def run(config: BotConfig) -> None:
    client = FollowSMClient(api_key=config.followsm_api_key, risk_config=config.risk)
    monitor = MarketMonitor(client, config.symbol, config.poll_interval_secs)
    risk = RiskEngine(config.risk)
    orders = build_order_manager(config)
    log.info(
        "Starting %s mode on %s (tier: %s)",
        orders.mode,
        config.symbol,
        "DEVELOPER/ENTERPRISE" if config.followsm_api_key else "free",
    )

    try:
        async for signal in monitor.stream():
            if signal is None:
                await orders.cancel_all(reason="FollowSM feed unavailable (fail closed)")
                continue

            decision = risk.evaluate(signal)
            log.info(
                "%s vpin=%.3f (pctl %s) ob_tox_1pct=%.2f prob_delta_15m=%+.3f spot_15m=%+.3f%% -> %s (backend: %s)",
                signal.symbol,
                signal.vpin,
                "warming up" if signal.vpin_percentile is None else f"{signal.vpin_percentile:.2f}",
                signal.ob_toxicity_1pct,
                signal.prob_delta_15m,
                signal.price_delta_15m_pct * 100,
                decision.action,
                decision.backend_action,
            )

            if decision.halt:
                await orders.cancel_all(reason="HALT_MAKER_QUOTES")
                continue
            if signal.event is None:
                await orders.cancel_all(reason="no mapped Polymarket event")
                continue

            try:
                mid = await orders.get_midpoint(signal.event.yes_token_id)
            except httpx.HTTPError as exc:
                log.warning("Polymarket midpoint unavailable: %s", exc)
                await orders.cancel_all(reason="no Polymarket midpoint")
                continue

            plan = build_quote_plan(
                signal,
                decision,
                mid,
                base_half_spread=config.base_half_spread,
                tick_size=config.tick_size,
                momentum_skew_per_pct=config.momentum_skew_per_pct,
                size=config.quote_size,
            )
            if plan is None:
                await orders.cancel_all(reason="quote collapsed after rounding")
                continue
            log.info(
                "Quoting %s: mid=%.3f bid=%.2f ask=%.2f (%.1fx spread)",
                signal.event.market_slug,
                mid,
                plan.bid,
                plan.ask,
                decision.spread_multiplier,
            )
            await orders.replace_quotes(plan)
    finally:
        await orders.cancel_all(reason="shutdown")
        await orders.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(load_config()))
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
