"""Offline safety checks for signal execution and position management.

Run with: python -m unittest discover -s tests -v
All exchange responses are mocked; these tests never place a real order.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config

_test_root = tempfile.TemporaryDirectory(prefix="binance-bot-tests-")
config.DB_PATH = Path(_test_root.name) / "test.db"
config.LOG_PATH = Path(_test_root.name) / "test.log"

import database
import trade_engine
from trade_engine import TradeEngine


class FakeClient:
    def __init__(self) -> None:
        self.mark = 0.0121
        self.position_amount = 0.0
        self.position_entry = 0.0
        self.position_pnl = 0.0
        self.leverage = 3
        self.next_order_id = 100
        self.orders: dict[str, dict] = {}
        self.order_requests: list[dict] = []
        self.algo_orders: dict[str, dict] = {}
        self.algo_requests: list[dict] = []
        self.fills: list[dict] = []

    async def futures_mark_price(self, symbol: str) -> dict:
        return {"symbol": symbol, "markPrice": str(self.mark)}

    async def futures_position_information(self, symbol: str | None = None) -> list[dict]:
        if not self.position_amount:
            return []
        return [{
            "symbol": symbol or "ARPAUSDT",
            "positionAmt": str(self.position_amount),
            "entryPrice": str(self.position_entry),
            "markPrice": str(self.mark),
            "unRealizedProfit": str(self.position_pnl),
            "leverage": str(self.leverage),
        }]

    async def futures_exchange_info(self) -> dict:
        return {"symbols": [{
            "symbol": "ARPAUSDT",
            "status": "TRADING",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "1", "minQty": "1"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }]}

    async def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
        self.leverage = leverage
        return {"symbol": symbol, "leverage": leverage}

    async def futures_create_order(self, **params: object) -> dict:
        self.next_order_id += 1
        order_id = str(self.next_order_id)
        request = dict(params)
        self.order_requests.append(request)
        if request.get("type") == "MARKET":
            quantity = float(request["quantity"])
            if request.get("reduceOnly"):
                if self.position_amount > 0 and request["side"] == "SELL":
                    self.position_amount = max(0.0, self.position_amount - quantity)
                elif self.position_amount < 0 and request["side"] == "BUY":
                    self.position_amount = min(0.0, self.position_amount + quantity)
                self.fills.append({
                    "orderId": order_id,
                    "qty": str(quantity),
                    "price": str(self.mark),
                    "realizedPnl": "3",
                    "commission": "0",
                    "commissionAsset": "USDT",
                })
            else:
                self.position_amount = quantity if request["side"] == "BUY" else -quantity
                self.position_entry = self.mark
            order = {
                **request,
                "orderId": order_id,
                "status": "FILLED",
                "executedQty": str(quantity),
                "cumQuote": str(quantity * self.mark),
                "avgPrice": str(self.mark),
            }
        else:
            order = {**request, "orderId": order_id, "status": "NEW"}
        self.orders[order_id] = order
        return order.copy()

    async def futures_get_order(
        self,
        symbol: str,
        orderId: str | int | None = None,
        origClientOrderId: str | None = None,
    ) -> dict:
        if orderId is not None:
            return self.orders[str(orderId)].copy()
        for order in self.orders.values():
            if order.get("newClientOrderId") == origClientOrderId:
                return order.copy()
        raise LookupError("mock order not found")

    async def futures_cancel_order(self, symbol: str, orderId: str | int) -> dict:
        self.orders[str(orderId)]["status"] = "CANCELED"
        return self.orders[str(orderId)].copy()

    async def futures_create_algo_order(self, **params: object) -> dict:
        self.next_order_id += 1
        algo_id = str(self.next_order_id)
        request = dict(params)
        self.algo_requests.append(request)
        response = {
            **request,
            "algoId": algo_id,
            "algoStatus": "NEW",
            "orderType": request["type"],
            "triggerPrice": str(request["triggerPrice"]),
        }
        if request.get("quantity") is not None:
            response["quantity"] = str(request["quantity"])
        self.algo_orders[algo_id] = response
        return response.copy()

    async def futures_get_open_algo_orders(
        self, symbol: str | None = None
    ) -> list[dict]:
        return [
            order.copy()
            for order in self.algo_orders.values()
            if (symbol is None or order.get("symbol") == symbol)
            and order.get("algoStatus") in {"NEW", "WORKING", "ACCEPTED", "PARTIALLY_FILLED"}
        ]

    async def futures_get_algo_order(self, symbol: str, algoId: str) -> dict:
        order = self.algo_orders[str(algoId)]
        if order.get("symbol") != symbol:
            raise LookupError("mock algo order symbol mismatch")
        return order.copy()

    async def futures_cancel_algo_order(self, symbol: str, algoId: str) -> dict:
        order = self.algo_orders[str(algoId)]
        if order.get("symbol") != symbol:
            raise LookupError("mock algo order symbol mismatch")
        order["algoStatus"] = "CANCELED"
        return order.copy()

    def fill_algo(self, algo_id: str) -> str:
        algo = self.algo_orders[str(algo_id)]
        self.next_order_id += 1
        actual_id = str(self.next_order_id)
        quantity = (
            abs(self.position_amount)
            if str(algo.get("closePosition", "")).lower() == "true"
            else float(algo.get("quantity") or 0)
        )
        if self.position_amount > 0 and algo["side"] == "SELL":
            self.position_amount = max(0, self.position_amount - quantity)
        elif self.position_amount < 0 and algo["side"] == "BUY":
            self.position_amount = min(0, self.position_amount + quantity)
        actual = {
            "symbol": algo["symbol"],
            "orderId": actual_id,
            "status": "FILLED",
            "executedQty": str(quantity),
            "avgPrice": str(self.mark),
        }
        self.orders[actual_id] = actual
        self.fills.append({
            "orderId": actual_id,
            "qty": str(quantity),
            "price": str(self.mark),
            "realizedPnl": "3",
            "commission": "0.01",
            "commissionAsset": "USDT",
        })
        algo["actualOrderId"] = actual_id
        algo["algoStatus"] = "FINISHED"
        return actual_id

    async def futures_account_trades(self, **_params: object) -> list[dict]:
        return list(self.fills)


class FakeSession:
    def __init__(self, client: FakeClient, **_params: object) -> None:
        self.client = client

    async def connect(self) -> FakeClient:
        return self.client

    async def verify_trading_access(self) -> dict:
        return {"can_trade": True, "one_way_mode": True}

    async def usdt_equity(self) -> tuple[float, float]:
        return 10_000.0, 10_000.0

    async def close(self) -> None:
        return None


class FakeVault:
    def load_for_user(self, _user_id: int, _environment: str) -> tuple[str, str]:
        return "offline-test-key", "offline-test-secret"


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self._message_id = 0

    async def send_message(self, **params: object) -> SimpleNamespace:
        self._message_id += 1
        self.sent.append(dict(params))
        return SimpleNamespace(message_id=self._message_id)

    async def edit_message_text(self, **params: object) -> None:
        self.edited.append(dict(params))


class TradeSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._db_dir = tempfile.TemporaryDirectory(prefix="binance-bot-case-")
        database.DB_PATH = Path(self._db_dir.name) / "case.db"
        database.init_db()
        self.client = FakeClient()
        self.bot = FakeBot()
        self.app = SimpleNamespace(bot=self.bot)
        self.engine = TradeEngine(self.app, FakeVault())
        self.user_id = 912345
        database.upsert_user(self.user_id, "offline-test", "Offline test")
        ok, _message = database.grant_subscription(
            self.user_id, "lifetime", admin_id=self.user_id
        )
        self.assertTrue(ok)
        database.set_preference(self.user_id, "auto_trading", 1)
        database.set_preference(self.user_id, "risk_pct", 0.5)
        database.set_setting("global_auto_enabled", 1)
        database.set_setting("max_signal_age_minutes", 90)
        self._session_patch = patch.object(
            trade_engine,
            "FuturesSession",
            side_effect=lambda **params: FakeSession(self.client, **params),
        )
        self._premium_patch = patch.object(trade_engine, "is_premium", return_value=True)
        self._session_patch.start()
        self._premium_patch.start()

    def tearDown(self) -> None:
        self._premium_patch.stop()
        self._session_patch.stop()
        self._db_dir.cleanup()

    def add_signal(self, direction: str = "LONG") -> int:
        if direction == "LONG":
            stop, tp1, tp2, tp3 = 0.0116, 0.0129, 0.0132, 0.0136
        else:
            stop, tp1, tp2, tp3 = 0.0126, 0.0113, 0.0110, 0.0106
        signal_id = database.store_signal({
            "symbol": "ARPAUSDT",
            "direction": direction,
            "setup_hash": f"offline-{self._db_dir.name[-8:]}-{self._testMethodName}",
            "entry": 0.0121,
            "entry_low": 0.0120,
            "entry_high": 0.0122,
            "zone": {"low": 0.0120, "high": 0.0122},
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "confidence": 87,
            "strategy": "SMC",
            "timeframe": "4H / 1H / 5m",
            "structure": "4H HH/HL; 1H sweep + BOS; 5m FVG retest",
            "sweep_time": 1,
            "reason": "offline safety fixture",
        })
        self.assertIsNotNone(signal_id)
        return int(signal_id)

    def execute(self, signal_id: int) -> tuple[bool, str]:
        return asyncio.run(self.engine.execute_for_user(self.user_id, signal_id))

    def test_old_sweep_time_does_not_expire_fresh_signal_and_executes(self) -> None:
        signal_id = self.add_signal()
        signal = database.get_signal(signal_id)
        detected = datetime.fromisoformat(signal["created_at"])
        expires = datetime.fromisoformat(signal["expires_at"])
        self.assertEqual(signal["sweep_time"], 1)
        self.assertEqual(signal["side"], "LONG")
        self.assertEqual(signal["entry_zone"], {"low": 0.012, "high": 0.0122})
        self.assertGreaterEqual((expires - detected).total_seconds(), 90 * 60 - 1)

        ok, reason = self.execute(signal_id)
        self.assertTrue(ok, reason)
        self.assertEqual(database.get_signal(signal_id)["status"], "EXECUTED")
        self.assertTrue(any(order.get("type") == "MARKET" for order in self.client.order_requests))
        self.assertEqual(len(self.client.algo_requests), 4)
        self.assertTrue(any(order.get("type") == "STOP_MARKET" for order in self.client.algo_requests))
        self.assertEqual(
            len([o for o in self.client.algo_requests if o.get("type") == "TAKE_PROFIT_MARKET"]),
            3,
        )
        stored = database.active_trades(self.user_id)[0]
        self.assertEqual(stored["status"], "PROTECTED")
        self.assertTrue(stored["sl_order_id"])
        self.assertTrue(stored["tp1_order_id"])
        self.assertTrue(stored["tp2_order_id"])
        self.assertTrue(stored["tp3_order_id"])
        open_algo = asyncio.run(
            self.client.futures_get_open_algo_orders(symbol="ARPAUSDT")
        )
        self.assertEqual(len(open_algo), 4)
        stop_order = next(o for o in open_algo if o["type"] == "STOP_MARKET")
        self.assertEqual(stop_order.get("closePosition"), "true")
        self.assertNotIn("quantity", stop_order)
        for tp in (o for o in open_algo if o["type"] == "TAKE_PROFIT_MARKET"):
            self.assertEqual(tp.get("reduceOnly"), "true")
            self.assertGreater(float(tp["quantity"]), 0)

    def test_live_price_outside_entry_is_rejected_with_explicit_reason(self) -> None:
        signal_id = self.add_signal()
        self.client.mark = 0.0123
        ok, reason = self.execute(signal_id)
        self.assertFalse(ok)
        self.assertEqual(reason, "LIVE_PRICE_OUTSIDE_ENTRY")
        self.assertFalse(self.client.order_requests)
        self.assertEqual(database.get_signal(signal_id)["reason"], reason)
        history = database.recent_trades(self.user_id, 5)
        self.assertEqual(history[0]["close_reason"], reason)

    def test_only_created_at_controls_ninety_minute_expiry(self) -> None:
        signal_id = self.add_signal()
        stale = (datetime.now(timezone.utc) - timedelta(minutes=91)).isoformat()
        with database.connection() as conn:
            conn.execute(
                "UPDATE signals SET created_at=?, expires_at=? WHERE signal_id=?",
                (stale, stale, signal_id),
            )
        ok, reason = self.execute(signal_id)
        self.assertFalse(ok)
        self.assertEqual(reason, "SIGNAL_EXPIRED")
        self.assertEqual(database.get_signal(signal_id)["status"], "EXPIRED")
        self.assertFalse(self.client.order_requests)

    def test_existing_exchange_position_prevents_duplicate_order(self) -> None:
        signal_id = self.add_signal()
        self.client.position_amount = 20
        self.client.position_entry = 0.012
        ok, reason = self.execute(signal_id)
        self.assertFalse(ok)
        self.assertEqual(reason, "EXISTING_POSITION")
        self.assertFalse(self.client.order_requests)

    def test_live_pnl_tp1_breakeven_and_confirmed_stop_close(self) -> None:
        signal_id = self.add_signal()
        ok, reason = self.execute(signal_id)
        self.assertTrue(ok, reason)
        trade = database.active_trades(self.user_id)[0]
        self.client.position_pnl = 4.82

        asyncio.run(self.engine._manage_trade(trade))
        self.assertTrue(self.bot.edited)
        self.assertIn("+4.8200 USDT", self.bot.edited[-1]["text"])
        self.assertIn("Mark Price: 0.0121", self.bot.edited[-1]["text"])

        tp_ids = json.loads(trade["tp_order_ids"])
        self.client.fill_algo(tp_ids[0])
        self.client.mark = 0.0122
        asyncio.run(self.engine._manage_trade(database.get_trade(trade["trade_id"])))
        updated = database.get_trade(trade["trade_id"])
        self.assertEqual(updated["tp1_hit"], 1)
        self.assertAlmostEqual(updated["stop_price"], updated["entry_price"], places=4)
        self.assertIn("TP1 ✅", self.bot.edited[-1]["text"])
        self.assertIn("SL: 0.0121", self.bot.edited[-1]["text"])

        stop_id = updated["stop_order_id"]
        self.client.fill_algo(stop_id)
        asyncio.run(self.engine._manage_trade(updated))
        closed = database.get_trade(trade["trade_id"])
        self.assertEqual(closed["status"], "SL_HIT")
        self.assertEqual(closed["close_reason"], "STOP_LOSS_CONFIRMED")
        self.assertEqual(closed["pnl_verified"], 1)
        self.assertIn("STOP LOSS HIT", self.bot.edited[-1]["text"])

    def test_confirmed_tp3_closes_exchange_residual_and_completes_trade(self) -> None:
        signal_id = self.add_signal()
        ok, reason = self.execute(signal_id)
        self.assertTrue(ok, reason)
        trade = database.active_trades(self.user_id)[0]
        self.client.mark = 0.0137
        self.client.position_pnl = 9.5
        tp_ids = json.loads(trade["tp_order_ids"])
        self.client.fill_algo(tp_ids[2])

        asyncio.run(self.engine._manage_trade(trade))

        closed = database.get_trade(trade["trade_id"])
        self.assertEqual(self.client.position_amount, 0)
        self.assertEqual(closed["status"], "COMPLETED")
        self.assertEqual(closed["close_reason"], "TP3_CONFIRMED")
        self.assertEqual(closed["tp3_hit"], 1)
        self.assertTrue(any(
            order.get("type") == "MARKET" and order.get("reduceOnly")
            for order in self.client.order_requests
        ))
        self.assertIn("TRADE COMPLETED", self.bot.edited[-1]["text"])

    def test_short_live_pnl_uses_exchange_value_and_red_presentation(self) -> None:
        signal_id = self.add_signal("SHORT")
        ok, reason = self.execute(signal_id)
        self.assertTrue(ok, reason)
        trade = database.active_trades(self.user_id)[0]
        self.assertLess(self.client.position_amount, 0)
        protective = asyncio.run(
            self.client.futures_get_open_algo_orders(symbol="ARPAUSDT")
        )
        stop = next(order for order in protective if order["type"] == "STOP_MARKET")
        targets = [
            order for order in protective
            if order["type"] == "TAKE_PROFIT_MARKET"
        ]
        self.assertGreater(float(stop["triggerPrice"]), trade["entry_price"])
        self.assertTrue(
            all(float(order["triggerPrice"]) < trade["entry_price"] for order in targets)
        )
        self.client.mark = 0.0122
        self.client.position_pnl = -2.3

        asyncio.run(self.engine._manage_trade(trade))

        message = self.bot.edited[-1]["text"]
        self.assertIn("🔴 SHORT ARPAUSDT", message)
        self.assertIn("-2.3000 USDT", message)
        self.assertIn("PnL %: -0.58%", message)

    def test_missing_algo_stop_is_recovered_without_duplicate_tps(self) -> None:
        signal_id = self.add_signal()
        ok, reason = self.execute(signal_id)
        self.assertTrue(ok, reason)
        trade = database.active_trades(self.user_id)[0]
        tp_ids = json.loads(trade["tp_order_ids"])
        asyncio.run(
            self.client.futures_cancel_algo_order(
                symbol="ARPAUSDT", algoId=trade["sl_order_id"]
            )
        )

        asyncio.run(self.engine._manage_trade(trade))

        updated = database.get_trade(trade["trade_id"])
        open_orders = asyncio.run(
            self.client.futures_get_open_algo_orders(symbol="ARPAUSDT")
        )
        open_ids = {str(order["algoId"]) for order in open_orders}
        self.assertIn(updated["sl_order_id"], open_ids)
        self.assertEqual(
            {str(order_id) for order_id in tp_ids},
            {order_id for order_id in open_ids if order_id in tp_ids},
        )
        self.assertEqual(len(open_orders), 4)
        self.assertEqual(updated["status"], "PROTECTED")

    def test_manual_tp_is_reduce_only_verified_and_duplicate_is_blocked(self) -> None:
        signal_id = self.add_signal()
        ok, reason = self.execute(signal_id)
        self.assertTrue(ok, reason)
        trade = database.active_trades(self.user_id)[0]
        starting = abs(self.client.position_amount)

        ok, message = asyncio.run(
            self.engine.execute_tp_stage(
                trade["trade_id"], owner_id=self.user_id, stage=1
            )
        )
        self.assertTrue(ok, message)
        self.assertAlmostEqual(abs(self.client.position_amount), starting * 0.6, delta=1)
        close = [
            order for order in self.client.order_requests
            if order.get("type") == "MARKET" and order.get("reduceOnly")
        ][-1]
        self.assertEqual(close.get("side"), "SELL")
        self.assertEqual(database.get_trade(trade["trade_id"])["tp1_hit"], 1)
        count = len(self.client.order_requests)
        ok, message = asyncio.run(
            self.engine.execute_tp_stage(
                trade["trade_id"], owner_id=self.user_id, stage=1
            )
        )
        self.assertFalse(ok)
        self.assertIn("already recorded", message)
        self.assertEqual(len(self.client.order_requests), count)

    def test_manual_tp3_closes_position_and_cancels_remaining_protection(self) -> None:
        signal_id = self.add_signal()
        ok, reason = self.execute(signal_id)
        self.assertTrue(ok, reason)
        trade = database.active_trades(self.user_id)[0]
        for stage in (1, 2):
            ok, message = asyncio.run(
                self.engine.execute_tp_stage(
                    trade["trade_id"], owner_id=self.user_id, stage=stage
                )
            )
            self.assertTrue(ok, message)
        ok, message = asyncio.run(
            self.engine.execute_tp_stage(
                trade["trade_id"], owner_id=self.user_id, stage=3
            )
        )
        self.assertTrue(ok, message)
        self.assertEqual(self.client.position_amount, 0)
        updated = database.get_trade(trade["trade_id"])
        self.assertEqual(updated["status"], "COMPLETED")
        self.assertEqual(updated["tp3_hit"], 1)
        self.assertEqual(
            asyncio.run(
                self.client.futures_get_open_algo_orders(symbol="ARPAUSDT")
            ),
            [],
        )

    def test_breakeven_replaces_real_algo_stop_and_manual_close_verifies_flat(self) -> None:
        signal_id = self.add_signal()
        ok, reason = self.execute(signal_id)
        self.assertTrue(ok, reason)
        trade = database.active_trades(self.user_id)[0]
        self.client.mark = 0.0124
        ok, message = asyncio.run(
            self.engine.move_stop_to_breakeven(
                trade["trade_id"], owner_id=self.user_id
            )
        )
        self.assertTrue(ok, message)
        updated = database.get_trade(trade["trade_id"])
        self.assertAlmostEqual(updated["stop_price"], updated["entry_price"])
        open_orders = asyncio.run(
            self.client.futures_get_open_algo_orders(symbol="ARPAUSDT")
        )
        stop = next(order for order in open_orders if order["type"] == "STOP_MARKET")
        self.assertEqual(str(stop["algoId"]), str(updated["sl_order_id"]))
        self.assertEqual(float(stop["triggerPrice"]), updated["entry_price"])

        ok, message = asyncio.run(
            self.engine.close_trade(
                trade["trade_id"], owner_id=self.user_id, actor_id=self.user_id
            )
        )
        self.assertTrue(ok, message)
        self.assertEqual(self.client.position_amount, 0)
        self.assertEqual(
            database.get_trade(trade["trade_id"])["status"], "MANUALLY_CLOSED"
        )
        self.assertEqual(
            asyncio.run(
                self.client.futures_get_open_algo_orders(symbol="ARPAUSDT")
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
