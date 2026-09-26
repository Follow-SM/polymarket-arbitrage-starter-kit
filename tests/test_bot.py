"""Offline tests: no network, no keys required. Run with `pytest -q`."""

import asyncio

import pytest
from followsm_sdk import ConfluenceSnapshot, RateLimitExceededException, RiskConfig

from bot.market_monitor import MarketMonitor, parse_snapshot
from bot.risk_engine import RiskEngine, build_quote_plan


def make_snapshot(vpin=0.30, divergence=False, direction="bullish_if_yes", prob_delta=0.0, price_delta=0.0, vpin_percentile=None, ob_tox=1.2):
    return ConfluenceSnapshot.model_validate(
        {
            "symbol": "BTCUSDT",
            "timestamp_ms": 1790212800000,
            "binance_microstructure": {
                "price": 68420.5,
                "vpin": vpin,
                "vpin_percentile": vpin_percentile,
                "ob_toxicity_1pct": ob_tox,
                "ob_imbalance_l1": 0.62,
                "depth_bands": {},
                "volume_z_score": 3.1,
                "natr_15m": 0.87,
                "taker_buy_ratio": 0.29,
                "price_delta_15m_pct": price_delta,
            },
            "polymarket_confluence": {
                "active_events": [
                    {
                        "market_slug": "will-btc-hit-70k",
                        "question": "Will Bitcoin hit $70k?",
                        "condition_id": "0xabc",
                        "yes_token_id": "123",
                        "direction": direction,
                        "direction_confidence": 0.91,
                        "implied_probability": 0.50,
                        "prob_delta_15m": prob_delta,
                        "clob_order_flow_imbalance": 0.74,
                        "smart_money_whale_sweeps_1h_usdt": 185000,
                    }
                ],
                "macro_event_risk_score": 0.85,
            },
            "composite_signals": {
                "is_toxic_alert": vpin > 0.7,
                "cross_market_divergence_flag": divergence,
                "recommended_action": "NONE",
            },
        }
    )


def quote(signal, decision, mid=0.50):
    return build_quote_plan(
        signal, decision, mid, base_half_spread=0.02, tick_size=0.01, momentum_skew_per_pct=0.02, size=10
    )


def test_parse_snapshot_extracts_core_fields():
    signal = parse_snapshot(make_snapshot(vpin=0.78, prob_delta=0.09, ob_tox=2.14))
    assert (signal.vpin, signal.ob_toxicity_1pct, signal.prob_delta_15m) == (0.78, 2.14, 0.09)
    assert signal.event.yes_token_id == "123"


def test_halt_cancels_quotes():
    signal = parse_snapshot(make_snapshot(vpin=0.92, divergence=True))
    decision = RiskEngine(RiskConfig()).evaluate(signal)
    assert decision.halt
    assert quote(signal, decision) is None


@pytest.mark.parametrize("vpin,divergence,width", [(0.30, False, 0.04), (0.30, True, 0.06), (0.85, False, 0.08)])
def test_spread_widens_with_risk_ladder(vpin, divergence, width):
    signal = parse_snapshot(make_snapshot(vpin=vpin, divergence=divergence))
    plan = quote(signal, RiskEngine(RiskConfig()).evaluate(signal))
    assert plan.ask - plan.bid == pytest.approx(width)


def test_custom_risk_config_changes_action():
    signal = parse_snapshot(make_snapshot(vpin=0.85))
    assert RiskEngine(RiskConfig(vpin_widen_threshold=0.90)).evaluate(signal).action == "NONE"


def test_vpin_percentile_drives_the_ladder_when_published():
    calm = parse_snapshot(make_snapshot(vpin=0.95, vpin_percentile=0.40))
    toxic = parse_snapshot(make_snapshot(vpin=0.20, vpin_percentile=0.93))
    assert RiskEngine(RiskConfig()).evaluate(calm).action == "NONE"
    assert RiskEngine(RiskConfig()).evaluate(toxic).action == "WIDEN_SPREAD_2X"


def test_toxic_1pct_book_widens_even_with_calm_vpin():
    signal = parse_snapshot(make_snapshot(vpin=0.19, vpin_percentile=0.30, ob_tox=6.7))
    assert RiskEngine(RiskConfig()).evaluate(signal).action == "WIDEN_SPREAD_2X"


def test_spot_lead_skews_quotes_toward_fair_value():
    # Spot +1% on a bullish-if-YES market that has not repriced yet -> quote centre moves up 2 points.
    signal = parse_snapshot(make_snapshot(price_delta=0.01, prob_delta=0.0))
    plan = quote(signal, RiskEngine(RiskConfig()).evaluate(signal))
    assert (plan.bid, plan.ask) == (0.50, 0.54)


def test_rate_limit_yields_none_and_prints_pricing_cta(caplog):
    class RateLimitedClient:
        def get_confluence_snapshot(self, symbol):
            raise RateLimitExceededException("Global rate limit exceeded", 0)

    async def first_signal():
        stream = MarketMonitor(RateLimitedClient(), "BTCUSDT", poll_interval_secs=0.01).stream()
        return await stream.__anext__()

    assert asyncio.run(first_signal()) is None
    assert "https://follow-sm.com/pricing" in caplog.text
