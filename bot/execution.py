"""Order management for Polymarket CLOB: paper (mock) and live (py-clob-client) modes.

Both modes read real midpoints from the public CLOB REST API; only the live manager
signs and posts orders. Blocking py-clob-client calls run in worker threads so the
asyncio loop keeps polling FollowSM while orders are in flight.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Dict, List, Optional

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY, SELL

from bot.risk_engine import QuotePlan

log = logging.getLogger(__name__)


class OrderManager:
    """Tracks this bot's resting maker quotes and replaces them atomically per plan."""

    mode = "base"

    def __init__(self, host: str) -> None:
        self._http = httpx.AsyncClient(base_url=host, timeout=5.0)
        self.open_orders: Dict[str, str] = {}  # order_id -> side
        self.current_plan: Optional[QuotePlan] = None

    async def get_midpoint(self, token_id: str) -> float:
        response = await self._http.get("/midpoint", params={"token_id": token_id})
        response.raise_for_status()
        return float(response.json()["mid"])

    async def replace_quotes(self, plan: QuotePlan) -> None:
        if plan == self.current_plan and self.open_orders:
            return
        await self.cancel_all(reason="requote")
        for side, price in ((BUY, plan.bid), (SELL, plan.ask)):
            try:
                order_id = await self._place(plan.token_id, side, price, plan.size)
            except Exception as exc:  # one rejected side must not leave the loop dead
                log.error("[%s] %s %.2f @ %.2f rejected: %s", self.mode, side, plan.size, price, exc)
                continue
            self.open_orders[order_id] = side
        self.current_plan = plan

    async def cancel_all(self, reason: str) -> None:
        if not self.open_orders:
            return
        order_ids = list(self.open_orders)
        await self._cancel(order_ids)
        log.info("[%s] cancelled %d quote(s): %s", self.mode, len(order_ids), reason)
        self.open_orders.clear()
        self.current_plan = None

    async def close(self) -> None:
        await self._http.aclose()

    async def _place(self, token_id: str, side: str, price: float, size: float) -> str:
        raise NotImplementedError

    async def _cancel(self, order_ids: List[str]) -> None:
        raise NotImplementedError


class MockOrderManager(OrderManager):
    """Paper trading: logs orders against live midpoints, never touches the exchange."""

    mode = "PAPER"

    def __init__(self, host: str) -> None:
        super().__init__(host)
        self._ids = itertools.count(1)

    async def _place(self, token_id: str, side: str, price: float, size: float) -> str:
        order_id = f"paper-{next(self._ids)}"
        log.info("[PAPER] %s %.2f YES @ %.2f (token %s…)", side, size, price, token_id[:10])
        return order_id

    async def _cancel(self, order_ids: List[str]) -> None:
        return None


class LiveOrderManager(OrderManager):
    """Signs and posts post-only GTC maker orders to the Polymarket CLOB.

    The ask side is a SELL of YES tokens, so it requires YES inventory in the funder
    wallet; without it the CLOB rejects that side and only the bid is quoted.
    """

    mode = "LIVE"

    def __init__(
        self,
        host: str,
        private_key: str,
        funder: Optional[str] = None,
        signature_type: int = 0,
    ) -> None:
        super().__init__(host)
        self._clob = ClobClient(
            host,
            chain_id=POLYGON,
            key=private_key,
            signature_type=signature_type,
            funder=funder,
        )
        self._clob.set_api_creds(self._clob.create_or_derive_api_creds())

    async def _place(self, token_id: str, side: str, price: float, size: float) -> str:
        args = OrderArgs(token_id=token_id, price=price, size=size, side=side)
        signed = await asyncio.to_thread(self._clob.create_order, args)
        response = await asyncio.to_thread(self._clob.post_order, signed, OrderType.GTC, True)
        if not response.get("success"):
            raise RuntimeError(response.get("errorMsg") or response)
        log.info("[LIVE] %s %.2f YES @ %.2f -> %s", side, size, price, response["orderID"])
        return response["orderID"]

    async def _cancel(self, order_ids: List[str]) -> None:
        await asyncio.to_thread(self._clob.cancel_orders, order_ids)
