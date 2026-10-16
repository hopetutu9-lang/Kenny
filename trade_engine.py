"""User-isolated signal execution and protective-position reconciliation."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

from binance_client import CredentialVault, FuturesSession
from config import ADMIN_CHAT_ID
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database import (
    active_trades,
    add_trade,
    all_users_with_preferences,
    all_active_trades,
    daily_realized_pnl,
    get_preference,
    get_setting,
    get_signal,
    get_trade,
    is_premium,
    log_event,
    premium_users_with_preferences,
    record_trade_rejection,
    update_signal_status,
    update_trade,
)
from logger import logger, safe_error_name
from risk_manager import calculate_position, decimal_ceil, decimal_floor


class TradeEngine:
    def __init__(self, application: Any, vault: CredentialVault) -> None:
        self.application = application
        self.vault = vault
        self._monitor_lock = asyncio.Lock()

    async def on_signal(self, signal: dict[str, Any], signal_id: int) -> None:
        stored_signal = get_signal(signal_id) or signal
        users = premium_users_with_preferences()
        for user in users:
            user_id = int(user["user_id"])
            if bool(user["notifications"]):
                await self._notify(
                    user_id,
                    self._format_signal(stored_signal, bool(user["auto_trading"])),
                )
            if bool(user["auto_trading"]):
                await self.execute_for_user(user_id, signal_id)

    async def execute_for_user(self, user_id: int, signal_id: int) -> tuple[bool, str]:
        signal = get_signal(signal_id)
        if not signal:
            return False, "signal not found"
        created_at = signal.get("created_at")
        try:
            created = datetime.fromisoformat(str(created_at))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            max_age = max(
                1,
                min(1440, int(float(get_setting("max_signal_age_minutes", "90") or 90))),
            )
        except (TypeError, ValueError, OverflowError):
            return await self._reject(user_id, signal_id, "API_ERROR")
        if (datetime.now(timezone.utc) - created).total_seconds() > max_age * 60:
            return await self._reject(user_id, signal_id, "SIGNAL_EXPIRED", status="EXPIRED")
        update_signal_status(signal_id, "VALIDATING")
        reason = self._preflight(user_id)
        if reason:
            return await self._reject(user_id, signal_id, reason)

        preference = get_preference(user_id)
        environment = str(preference["environment"])
        if environment not in {"testnet", "real"}:
            return await self._reject(user_id, signal_id, "API_ERROR")
        if environment == "real" and get_setting("real_trading_enabled", "0") != "1":
            return await self._reject(user_id, signal_id, "REAL_TRADING_DISABLED")

        try:
            credentials = self.vault.load_for_user(user_id, environment)
        except Exception:
            return await self._reject(user_id, signal_id, "API_ERROR")
        if not credentials:
            return await self._reject(user_id, signal_id, "API_ERROR")

        api_key, api_secret = credentials
        session = FuturesSession(
            api_key=api_key,
            api_secret=api_secret,
            testnet=environment == "testnet",
        )
        trade_id: int | None = None
        try:
            client = await session.connect()
            await session.verify_trading_access()
            # Always fetch a fresh exchange mark immediately before any sizing
            # or order attempt; scanner candle caches are never execution prices.
            current = await client.futures_mark_price(symbol=signal["symbol"])
            mark_price = float(current.get("markPrice", 0))
            entry = float(signal["entry"])
            stop = float(signal["stop"])
            if not all(value > 0 for value in (mark_price, entry, stop)):
                return await self._reject(user_id, signal_id, "INVALID_PRICE")
            low = float(signal.get("entry_low") or (entry * 0.997))
            high = float(signal.get("entry_high") or (entry * 1.003))
            if low > high or not low <= mark_price <= high:
                return await self._reject(user_id, signal_id, "LIVE_PRICE_OUTSIDE_ENTRY")
            direction = str(signal["direction"])
            target_prices = [float(signal.get(f"tp{index}") or 0) for index in range(1, 4)]
            levels_valid = (
                direction == "LONG"
                and stop < mark_price < target_prices[0] < target_prices[1] < target_prices[2]
            ) or (
                direction == "SHORT"
                and stop > mark_price > target_prices[0] > target_prices[1] > target_prices[2]
            )
            if not levels_valid:
                return await self._reject(user_id, signal_id, "INVALID_PRICE")

            account_equity, available = await session.usdt_equity()
            if account_equity <= 0 or available <= 0:
                return await self._reject(user_id, signal_id, "INSUFFICIENT_BALANCE")
            positions = await client.futures_position_information()
            open_positions = [
                p for p in positions if abs(float(p.get("positionAmt", 0) or 0)) > 0
            ]
            if any(p.get("symbol") == signal["symbol"] for p in open_positions):
                return await self._reject(user_id, signal_id, "EXISTING_POSITION")
            if any(t["symbol"] == signal["symbol"] and t["environment"] == environment for t in active_trades(user_id)):
                return await self._reject(user_id, signal_id, "EXISTING_POSITION")

            daily_pnl = daily_realized_pnl(user_id, environment)
            daily_pnl += sum(
                float(p.get("unRealizedProfit", 0) or 0) for p in open_positions
            )
            info_all = await client.futures_exchange_info()
            symbol_info = next(
                (item for item in info_all.get("symbols", []) if item.get("symbol") == signal["symbol"]),
                None,
            )
            if not symbol_info or symbol_info.get("status") != "TRADING":
                return await self._reject(user_id, signal_id, "API_ERROR")
            min_rr = float(get_setting("min_reward_risk", "1.5") or 1.5)
            risk_distance = abs(mark_price - stop)
            reward = abs(target_prices[0] - mark_price)
            if risk_distance <= 0 or reward / risk_distance < min_rr:
                return await self._reject(user_id, signal_id, "RISK_CHECK_FAILED")
            settings_risk = float(get_setting("risk_per_trade_pct", "0.5") or 0.5)
            user_risk = min(float(preference["risk_pct"]), settings_risk)
            decision = calculate_position(
                equity=account_equity,
                available_balance=available,
                entry=mark_price,
                stop=stop,
                direction=direction,
                risk_pct=user_risk,
                symbol_info=symbol_info,
                daily_realized_pnl=daily_pnl,
                open_position_count=len(open_positions),
            )
            if not decision.approved:
                lower_reason = decision.reason.lower()
                if "quantity" in lower_reason or "notional" in lower_reason or "minimum" in lower_reason:
                    return await self._reject(user_id, signal_id, "INVALID_QUANTITY")
                if "balance" in lower_reason or "margin" in lower_reason:
                    return await self._reject(user_id, signal_id, "INSUFFICIENT_BALANCE")
                return await self._reject(user_id, signal_id, "RISK_CHECK_FAILED")

            trade_id = add_trade(
                user_id,
                signal_id,
                signal["setup_hash"],
                environment,
                signal["symbol"],
                direction,
                str(signal.get("reason") or ""),
            )
            if trade_id is None:
                return await self._reject(user_id, signal_id, "DUPLICATE_SIGNAL")

            leverage = min(3, int(float(get_setting("max_leverage", "3") or 3)))
            await client.futures_change_leverage(symbol=signal["symbol"], leverage=leverage)
            side = "BUY" if direction == "LONG" else "SELL"
            close_side = "SELL" if direction == "LONG" else "BUY"
            client_order_id = f"pro{signal['setup_hash'][:24]}"
            update_signal_status(signal_id, "EXECUTING")
            await self._notify(
                user_id,
                "⚡ EXECUTION\n"
                f"Symbol: {signal['symbol']}\nSide: {direction}\n"
                "🔌 API: CONNECTED\n"
                f"💰 Balance: ${account_equity:,.2f} · Available: ${available:,.2f}\n"
                f"📍 Live Price: {mark_price:g}\n"
                f"🎯 Entry: {low:g} – {high:g}\n"
                "🛡 Risk: PASS\n📊 Position: PASS",
            )
            try:
                entry_order = await client.futures_create_order(
                    symbol=signal["symbol"],
                    side=side,
                    type="MARKET",
                    quantity=decision.quantity,
                    newClientOrderId=client_order_id,
                )
            except Exception as exc:
                # A timeout can occur after Binance accepted the order. Query its
                # deterministic client order ID before deciding the outcome.
                try:
                    entry_order = await client.futures_get_order(
                        symbol=signal["symbol"], origClientOrderId=client_order_id
                    )
                except Exception:
                    update_trade(trade_id, status="UNKNOWN")
                    await self._alert_admin(
                        f"Order state unknown for user {user_id}, {signal['symbol']}. "
                        "Check Binance before retrying."
                    )
                    return await self._reject(user_id, signal_id, "ORDER_REJECTED")
            order_id = str(entry_order.get("orderId", ""))
            status = str(entry_order.get("status", ""))
            if status not in {"FILLED", "PARTIALLY_FILLED"} or not order_id:
                update_trade(trade_id, status="UNKNOWN", entry_order_id=order_id or None)
                return await self._reject(user_id, signal_id, "ORDER_REJECTED")
            if status == "PARTIALLY_FILLED":
                try:
                    await client.futures_cancel_order(symbol=signal["symbol"], orderId=order_id)
                    entry_order = await client.futures_get_order(symbol=signal["symbol"], orderId=order_id)
                except Exception:
                    logger.warning("Could not cancel unfilled remainder trade_id=%s", trade_id)
            filled_qty = float(entry_order.get("executedQty", 0) or 0)
            cumulative = float(entry_order.get("cumQuote", 0) or 0)
            actual_entry = cumulative / filled_qty if filled_qty > 0 and cumulative > 0 else float(entry_order.get("avgPrice", 0) or entry)
            if filled_qty <= 0:
                update_trade(trade_id, status="UNKNOWN", entry_order_id=order_id)
                return await self._reject(user_id, signal_id, "ORDER_REJECTED")

            step, tick = self._steps(symbol_info)
            if step <= 0 or tick <= 0:
                raise RuntimeError("Binance symbol step/tick filters are unavailable")

            # The market order may fill at a different quantity/average than
            # requested. Protection is always sized from Binance's live position.
            positions = await client.futures_position_information(
                symbol=signal["symbol"]
            )
            position = next(
                (item for item in positions if item.get("symbol") == signal["symbol"]),
                None,
            )
            position_amount = float(position.get("positionAmt", 0) or 0) if position else 0.0
            direction_matches = (
                position_amount > 0 if direction == "LONG" else position_amount < 0
            )
            if not position or not direction_matches or abs(position_amount) <= 0:
                update_trade(
                    trade_id,
                    entry_order_id=order_id,
                    status="PROTECTION_ERROR",
                    protection_error="Binance did not report the expected open position after entry.",
                )
                await self._alert_admin(
                    f"⚠️ PROTECTION ERROR\nSymbol: {signal['symbol']}\n"
                    "Reason: entry order filled but the expected live position could not be confirmed. "
                    "Check Binance Futures immediately."
                )
                return False, "entry fill did not produce a confirmable position"

            filled_qty = decimal_floor(abs(position_amount), step)
            actual_entry = float(position.get("entryPrice") or actual_entry or entry)
            rounded_stop = (
                decimal_floor(stop, tick)
                if direction == "LONG"
                else decimal_ceil(stop, tick)
            )
            rounded_targets = [
                decimal_ceil(price, tick) if direction == "LONG"
                else decimal_floor(price, tick)
                for price in target_prices
            ]
            valid_actual_levels = (
                direction == "LONG"
                and rounded_stop < actual_entry < rounded_targets[0] < rounded_targets[1] < rounded_targets[2]
            ) or (
                direction == "SHORT"
                and rounded_stop > actual_entry > rounded_targets[0] > rounded_targets[1] > rounded_targets[2]
            )
            if not valid_actual_levels or filled_qty <= 0:
                closed = await self._emergency_close(
                    client,
                    signal["symbol"],
                    close_side,
                    abs(position_amount),
                    signal["setup_hash"],
                )
                update_trade(
                    trade_id,
                    entry_order_id=order_id,
                    status="PROTECTION_ERROR",
                    protection_error="Filled position failed actual-entry/quantity validation.",
                )
                if closed:
                    refreshed = await client.futures_position_information(
                        symbol=signal["symbol"]
                    )
                    rem = next(
                        (item for item in refreshed if item.get("symbol") == signal["symbol"]),
                        None,
                    )
                    if not rem or abs(float(rem.get("positionAmt", 0) or 0)) <= 0:
                        await self._mark_closed(
                            client,
                            get_trade(trade_id) or {},
                            "COMPLETED",
                            "POSITION_CLOSED_AFTER_PROTECTION_VALIDATION_FAILURE",
                        )
                await self._alert_admin(
                    f"⚠️ PROTECTION ERROR\nSymbol: {signal['symbol']}\n"
                    "Reason: filled position failed actual entry/quantity validation. "
                    f"Emergency close {'confirmed' if closed else 'not confirmed'}; inspect Binance."
                )
                return False, "position validation failed; emergency close attempted"

            update_trade(
                trade_id,
                entry_order_id=order_id,
                exchange_order_ids=json.dumps([order_id]),
                quantity=filled_qty,
                remaining_quantity=filled_qty,
                entry_price=actual_entry,
                executed_at=datetime.now(timezone.utc).isoformat(),
                stop_price=rounded_stop,
                sl_order_id=None,
                stop_order_id=None,
                tp_order_ids="[]",
                tp_actual_order_ids="[]",
                tp1_order_id=None,
                tp2_order_id=None,
                tp3_order_id=None,
                protection_error=None,
                best_price=actual_entry,
            )
            try:
                active_trade = get_trade(trade_id) or {}
                protection_ids = await self._install_protection(
                    client,
                    active_trade,
                    signal,
                    position,
                    symbol_info,
                )
            except Exception as first_exc:
                # A timed-out order may already be live. Re-fetch the algo-order
                # book and reuse deterministic clientAlgoIds before any retry.
                try:
                    active_trade = get_trade(trade_id) or {}
                    protection_ids = await self._install_protection(
                        client,
                        active_trade,
                        signal,
                        position,
                        symbol_info,
                    )
                except Exception as retry_exc:
                    reason_text = safe_error_name(retry_exc)
                    update_trade(
                        trade_id,
                        status="PROTECTION_ERROR",
                        protection_error=reason_text,
                    )
                    closed = await self._emergency_close(
                        client,
                        signal["symbol"],
                        close_side,
                        abs(position_amount),
                        signal["setup_hash"],
                    )
                    flat = False
                    if closed:
                        try:
                            refreshed = await client.futures_position_information(
                                symbol=signal["symbol"]
                            )
                            rem = next(
                                (item for item in refreshed if item.get("symbol") == signal["symbol"]),
                                None,
                            )
                            flat = not rem or abs(float(rem.get("positionAmt", 0) or 0)) <= 0
                        except Exception:
                            flat = False
                    await self._cancel_tracked_orders(
                        client, get_trade(trade_id) or active_trade
                    )
                    if flat:
                        await self._mark_closed(
                            client,
                            get_trade(trade_id) or active_trade,
                            "COMPLETED",
                            "POSITION_CLOSED_AFTER_PROTECTION_FAILURE",
                        )
                    else:
                        await self._notify(
                            user_id,
                            f"⚠️ PROTECTION ERROR\nSymbol: {signal['symbol']}\n"
                            f"Reason: {reason_text}\n"
                            "Emergency close is not confirmed. Check Binance Futures immediately.",
                        )
                    await self._alert_admin(
                        f"⚠️ PROTECTION ERROR\nSymbol: {signal['symbol']}\n"
                        f"Reason: {reason_text}\n"
                        f"Emergency close {'confirmed flat' if flat else 'not confirmed'}."
                    )
                    logger.error(
                        "Protection failed after retry trade_id=%s initial=%s retry=%s",
                        trade_id,
                        safe_error_name(first_exc),
                        reason_text,
                    )
                    update_signal_status(signal_id, "REJECTED", "PROTECTION_ERROR")
                    return False, "protection could not be confirmed; emergency recovery attempted"

            tracked_ids = json.loads(
                (get_trade(trade_id) or {}).get("exchange_order_ids") or "[]"
            )
            tracked_ids.extend(
                value for value in (protection_ids.get("sl"), *protection_ids.get("tps", []))
                if value
            )
            update_trade(
                trade_id,
                stop_order_id=protection_ids["sl"],
                sl_order_id=protection_ids["sl"],
                tp_order_ids=json.dumps(protection_ids["tps"]),
                tp1_order_id=protection_ids["tps"][0],
                tp2_order_id=protection_ids["tps"][1],
                tp3_order_id=protection_ids["tps"][2],
                exchange_order_ids=json.dumps(list(dict.fromkeys(tracked_ids))),
                status="PROTECTED",
                protection_error=None,
            )
            update_signal_status(signal_id, "EXECUTED")
            log_event(
                "trade.opened",
                user_id,
                trade_id=trade_id,
                symbol=signal["symbol"],
                environment=environment,
                qty=filled_qty,
            )
            live_trade = get_trade(trade_id) or {}
            trade_message = await self._send_trade_manager(
                user_id,
                live_trade,
                signal,
                header=f"✅ TRADE EXECUTED · {environment.upper()}",
                extra=f"Entry order: {order_id}\nProtection: Binance-confirmed SL + TP1/TP2/TP3",
            )
            if trade_message is not None:
                update_trade(
                    trade_id,
                    telegram_chat_id=user_id,
                    telegram_message_id=getattr(trade_message, "message_id", None),
                )
            return True, "exchange confirmed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if trade_id is not None:
                current = active_trades(user_id)
                if any(t["trade_id"] == trade_id and t["status"] == "SUBMITTING" for t in current):
                    update_trade(trade_id, status="UNKNOWN")
            logger.warning(
                "Trade execution rejected user_id=%s signal_id=%s error=%s",
                user_id,
                signal_id,
                safe_error_name(exc),
            )
            return await self._reject(user_id, signal_id, "API_ERROR")
        finally:
            await session.close()

    async def admin_close_trade(self, trade_id: int, admin_id: int) -> tuple[bool, str]:
        """Compatibility wrapper for the admin-panel close flow."""
        return await self.close_trade(trade_id, owner_id=None, actor_id=admin_id)

    async def close_trade(
        self,
        trade_id: int,
        *,
        owner_id: int | None,
        actor_id: int,
    ) -> tuple[bool, str]:
        """Close exactly one verified, tracked Binance position with reduce-only."""
        async with self._monitor_lock:
            trade = get_trade(trade_id)
            if not trade or trade["status"] not in {
                "SUBMITTING",
                "OPEN",
                "PROTECTED",
                "TP1_HIT",
                "TP2_HIT",
                "PROTECTION_ERROR",
                "PROTECTION_FAILED",
                "UNKNOWN",
            }:
                return False, "Trade is no longer active."
            if owner_id is not None and int(trade["user_id"]) != int(owner_id):
                return False, "This trade belongs to another Telegram user."
            try:
                credentials = self.vault.load_for_user(
                    int(trade["user_id"]), str(trade["environment"])
                )
            except Exception:
                credentials = None
            if not credentials:
                return False, "Encrypted credentials are unavailable; check the exchange manually."
            session = FuturesSession(
                api_key=credentials[0],
                api_secret=credentials[1],
                testnet=trade["environment"] == "testnet",
            )
            try:
                client = await session.connect()
                positions = await client.futures_position_information(
                    symbol=trade["symbol"]
                )
                position = next(
                    (item for item in positions if item.get("symbol") == trade["symbol"]),
                    None,
                )
                amount = float(position.get("positionAmt", 0) or 0) if position else 0.0
                if amount == 0:
                    await self._cancel_tracked_orders(client, trade)
                    close_status, close_reason = await self._confirmed_close_reason(
                        client, trade
                    )
                    await self._mark_closed(client, trade, close_status, close_reason)
                    log_event(
                        "trade.manually_closed",
                        actor_id,
                        trade_id=trade_id,
                        user_id=trade["user_id"],
                        already_flat=True,
                    )
                    return True, f"Trade #{trade_id} was already flat; tracked orders were cancelled."
                direction_matches = (
                    amount > 0 if trade["direction"] == "LONG" else amount < 0
                )
                if not direction_matches:
                    await self._alert_admin(
                        f"Manual close blocked for trade #{trade_id}: Binance position direction "
                        "does not match the bot record. Review the account manually."
                    )
                    return False, "Exchange position direction differs from the bot record; no order was sent."
                info = await client.futures_exchange_info()
                symbol_info = next(
                    (
                        item
                        for item in info.get("symbols", [])
                        if item.get("symbol") == trade["symbol"]
                    ),
                    {},
                )
                step, _tick = self._steps(symbol_info)
                quantity = decimal_floor(abs(amount), step)
                if quantity <= 0:
                    return False, "Exchange position is below its valid quantity step."
                side = "SELL" if amount > 0 else "BUY"
                close_client_id = (
                    f"cl{trade['setup_hash'][:28]}"[:36]
                )
                try:
                    close_order = await client.futures_create_order(
                        symbol=trade["symbol"],
                        side=side,
                        type="MARKET",
                        quantity=quantity,
                        reduceOnly="true",
                        newClientOrderId=close_client_id,
                    )
                except Exception as exc:
                    try:
                        close_order = await client.futures_get_order(
                            symbol=trade["symbol"],
                            origClientOrderId=close_client_id,
                        )
                    except Exception:
                        logger.warning(
                            "Manual close outcome unknown trade_id=%s error=%s",
                            trade_id,
                            safe_error_name(exc),
                        )
                        return False, "Close outcome is unknown. Check Binance before retrying."
                order_id = str(close_order.get("orderId", ""))
                if not order_id or close_order.get("status") not in {
                    "FILLED",
                    "PARTIALLY_FILLED",
                }:
                    return False, "Binance did not confirm the close order."
                current_positions = await client.futures_position_information(
                    symbol=trade["symbol"]
                )
                current = next(
                    (
                        item
                        for item in current_positions
                        if item.get("symbol") == trade["symbol"]
                    ),
                    None,
                )
                remainder = (
                    float(current.get("positionAmt", 0) or 0) if current else 0.0
                )
                if abs(remainder) > 0:
                    return (
                        False,
                        f"Binance confirmed a partial close, but {abs(remainder):g} remains open. Review it immediately.",
                    )
                try:
                    tracked_ids = json.loads(trade.get("exchange_order_ids") or "[]")
                except (ValueError, TypeError):
                    tracked_ids = []
                tracked_ids.append(order_id)
                update_trade(
                    trade_id,
                    exchange_order_ids=json.dumps(list(dict.fromkeys(tracked_ids))),
                )
                updated = get_trade(trade_id) or trade
                await self._cancel_tracked_orders(client, updated, except_id=order_id)
                await self._mark_closed(
                    client,
                    updated,
                    "MANUALLY_CLOSED",
                    "MANUALLY_CLOSED",
                )
                log_event(
                    "trade.manually_closed",
                    actor_id,
                    trade_id=trade_id,
                    user_id=trade["user_id"],
                    close_order_id=order_id,
                )
                return True, f"Binance confirmed trade #{trade_id} is flat; tracked protection orders were cancelled."
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Manual close failed trade_id=%s error=%s",
                    trade_id,
                    safe_error_name(exc),
                )
                return False, f"Manual close failed ({safe_error_name(exc)}); check Binance manually."
            finally:
                await session.close()

    async def execute_tp_stage(
        self, trade_id: int, *, owner_id: int | None, stage: int
    ) -> tuple[bool, str]:
        """Run one user-requested, configured reduce-only TP tranche."""
        if stage not in {1, 2, 3}:
            return False, "Invalid take-profit stage."
        async with self._monitor_lock:
            trade = get_trade(trade_id)
            if not trade or (
                owner_id is not None and int(trade["user_id"]) != int(owner_id)
            ):
                return False, "This trade is unavailable to your account."
            if trade["status"] not in {
                "OPEN", "PROTECTED", "TP1_HIT", "TP2_HIT",
                "PROTECTION_ERROR", "PROTECTION_FAILED", "UNKNOWN",
            }:
                return False, "Trade is no longer open."
            if trade.get(f"tp{stage}_hit"):
                return False, f"TP{stage} was already recorded; duplicate close blocked."
            if stage > 1 and not trade.get(f"tp{stage - 1}_hit"):
                return False, f"TP{stage - 1} must be completed before TP{stage}."
            try:
                credentials = self.vault.load_for_user(
                    int(trade["user_id"]), str(trade["environment"])
                )
            except Exception:
                credentials = None
            if not credentials:
                return False, "Encrypted credentials are unavailable; check Binance manually."
            session = FuturesSession(
                api_key=credentials[0],
                api_secret=credentials[1],
                testnet=trade["environment"] == "testnet",
            )
            try:
                client = await session.connect()
                positions = await client.futures_position_information(
                    symbol=trade["symbol"]
                )
                position = next(
                    (item for item in positions if item.get("symbol") == trade["symbol"]),
                    None,
                )
                amount = float(position.get("positionAmt", 0) or 0) if position else 0.0
                if not amount or (amount > 0) != (trade["direction"] == "LONG"):
                    return False, "Fresh Binance position is flat or has a different direction."

                # Cancel the corresponding conditional TP before a manual market
                # tranche so an already-triggering algo cannot double-close it.
                stage_id = trade.get(f"tp{stage}_order_id")
                if stage_id:
                    open_orders = await self._open_algo_orders(client, trade["symbol"])
                    if str(stage_id) in {self._algo_id(item) for item in open_orders}:
                        await client.futures_cancel_algo_order(
                            symbol=trade["symbol"], algoId=str(stage_id)
                        )
                refreshed_orders = await self._open_algo_orders(client, trade["symbol"])
                if stage_id and str(stage_id) in {
                    self._algo_id(item) for item in refreshed_orders
                }:
                    return False, "Binance still lists the conditional TP; no manual order was sent."

                refreshed = await client.futures_position_information(
                    symbol=trade["symbol"]
                )
                position = next(
                    (item for item in refreshed if item.get("symbol") == trade["symbol"]),
                    None,
                )
                amount = float(position.get("positionAmt", 0) or 0) if position else 0.0
                if not amount or (amount > 0) != (trade["direction"] == "LONG"):
                    return False, "Position changed while preparing TP; refresh the manager."

                info = await client.futures_exchange_info()
                symbol_info = next(
                    (item for item in info.get("symbols", []) if item.get("symbol") == trade["symbol"]),
                    {},
                )
                step, _tick = self._steps(symbol_info)
                allocation = self._tp_allocations()
                remaining_indices = [
                    index for index in range(3)
                    if index + 1 >= stage and not trade.get(f"tp{index + 1}_hit")
                ]
                if not remaining_indices:
                    return False, "No configured TP allocation remains."
                if stage == 3:
                    quantity = decimal_floor(abs(amount), step)
                else:
                    weight = sum(allocation[index] for index in remaining_indices)
                    quantity = decimal_floor(
                        abs(amount) * allocation[stage - 1] / weight, step
                    )
                if quantity <= 0:
                    return False, "The remaining position is below Binance's quantity step."
                side = "SELL" if amount > 0 else "BUY"
                client_id = f"mt{stage}{trade['setup_hash'][:27]}"[:36]
                try:
                    order = await client.futures_create_order(
                        symbol=trade["symbol"],
                        side=side,
                        type="MARKET",
                        quantity=quantity,
                        reduceOnly="true",
                        newClientOrderId=client_id,
                    )
                except Exception as exc:
                    try:
                        order = await client.futures_get_order(
                            symbol=trade["symbol"],
                            origClientOrderId=client_id,
                        )
                    except Exception:
                        await self._restore_trade_protection(client, trade)
                        logger.warning(
                            "Manual TP outcome unknown trade_id=%s stage=%s error=%s",
                            trade_id, stage, safe_error_name(exc),
                        )
                        return False, "TP outcome is unknown; check Binance before retrying."
                order_id = str(order.get("orderId") or "")
                if not order_id or order.get("status") not in {"FILLED", "PARTIALLY_FILLED"}:
                    await self._restore_trade_protection(client, trade)
                    return False, "Binance did not confirm the TP market order fill."

                after = await client.futures_position_information(symbol=trade["symbol"])
                remaining_position = next(
                    (item for item in after if item.get("symbol") == trade["symbol"]),
                    None,
                )
                remaining = (
                    float(remaining_position.get("positionAmt", 0) or 0)
                    if remaining_position else 0.0
                )
                if abs(remaining) >= abs(amount):
                    await self._restore_trade_protection(client, trade)
                    return False, "Binance did not confirm that position quantity decreased."
                try:
                    order_ids = json.loads(trade.get("exchange_order_ids") or "[]")
                except (TypeError, ValueError):
                    order_ids = []
                order_ids.append(order_id)
                realized, verified = await self._realized_pnl_for_order_ids(
                    client, str(trade["symbol"]), [order_id]
                )
                update_trade(
                    trade_id,
                    exchange_order_ids=json.dumps(list(dict.fromkeys(order_ids))),
                    remaining_quantity=abs(remaining),
                    **{
                        f"tp{stage}_hit": 1,
                        f"tp{stage}_actual_order_id": order_id,
                        f"tp{stage}_realized_pnl": realized if verified else None,
                        f"tp{stage}_at": datetime.now(timezone.utc).isoformat(),
                        "status": f"TP{stage}_HIT",
                    },
                )
                current_trade = get_trade(trade_id) or trade
                current_trade[f"tp{stage}_hit"] = 1
                current_trade["status"] = f"TP{stage}_HIT"
                log_event(
                    f"trade.tp{stage}_hit",
                    owner_id if owner_id is not None else int(trade["user_id"]),
                    trade_id=trade_id,
                    symbol=trade["symbol"],
                    actual_order_id=order_id,
                    manual=True,
                )
                await self._notify_trade_stage(
                    client,
                    current_trade,
                    f"🎯 TP{stage} HIT",
                    actual_order_id=order_id,
                    realized_pnl=realized if verified else None,
                )
                if stage == 3:
                    if abs(remaining) > 0:
                        return False, "TP3 filled partially; remaining Binance position is still open."
                    await self._cancel_tracked_orders(client, current_trade)
                    await self._mark_closed(client, current_trade, "COMPLETED", "TP3_CONFIRMED")
                    return True, "Binance confirmed TP3 closed the position."
                signal = get_signal(int(trade["signal_id"]))
                if not signal or not remaining_position:
                    return True, "Binance confirmed the partial TP; signal/protection data needs review."
                await self._install_protection(
                    client, current_trade, signal, remaining_position, symbol_info
                )
                return True, f"Binance confirmed TP{stage}; remaining position is protected."
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Manual TP failed trade_id=%s stage=%s error=%s",
                    trade_id, stage, safe_error_name(exc),
                )
                return False, f"TP failed ({safe_error_name(exc)}); check Binance."
            finally:
                await session.close()

    async def _restore_trade_protection(
        self, client: Any, trade: dict[str, Any]
    ) -> bool:
        """Restore the cancelled stage/order book after an unconfirmed manual TP."""
        try:
            positions = await client.futures_position_information(
                symbol=trade["symbol"]
            )
            position = next(
                (item for item in positions if item.get("symbol") == trade["symbol"]),
                None,
            )
            amount = float(position.get("positionAmt", 0) or 0) if position else 0.0
            if not position or not amount or (
                (amount > 0) != (trade["direction"] == "LONG")
            ):
                return False
            signal = get_signal(int(trade["signal_id"]))
            if not signal:
                return False
            info = await client.futures_exchange_info()
            symbol_info = next(
                (item for item in info.get("symbols", []) if item.get("symbol") == trade["symbol"]),
                {},
            )
            await self._install_protection(
                client, get_trade(int(trade["trade_id"])) or trade,
                signal, position, symbol_info,
            )
            return True
        except Exception as exc:
            logger.error(
                "Could not restore TP protection trade_id=%s error=%s",
                trade.get("trade_id"), safe_error_name(exc),
            )
            return False

    async def move_stop_to_breakeven(
        self, trade_id: int, *, owner_id: int | None
    ) -> tuple[bool, str]:
        async with self._monitor_lock:
            trade = get_trade(trade_id)
            if not trade or (
                owner_id is not None and int(trade["user_id"]) != int(owner_id)
            ):
                return False, "This trade is unavailable to your account."
            if trade["status"] not in {
                "OPEN", "PROTECTED", "TP1_HIT", "TP2_HIT",
                "PROTECTION_ERROR", "PROTECTION_FAILED", "UNKNOWN",
            }:
                return False, "Trade is no longer open."
            try:
                credentials = self.vault.load_for_user(
                    int(trade["user_id"]), str(trade["environment"])
                )
            except Exception:
                credentials = None
            if not credentials:
                return False, "Encrypted credentials are unavailable; check Binance manually."
            session = FuturesSession(
                api_key=credentials[0],
                api_secret=credentials[1],
                testnet=trade["environment"] == "testnet",
            )
            try:
                client = await session.connect()
                positions = await client.futures_position_information(symbol=trade["symbol"])
                position = next(
                    (item for item in positions if item.get("symbol") == trade["symbol"]),
                    None,
                )
                amount = float(position.get("positionAmt", 0) or 0) if position else 0.0
                if not amount or (amount > 0) != (trade["direction"] == "LONG"):
                    return False, "Fresh Binance position is flat or has a different direction."
                entry = float(position.get("entryPrice") or trade.get("entry_price") or 0)
                mark = float(position.get("markPrice") or 0)
                if entry <= 0 or mark <= 0:
                    mark_row = await client.futures_mark_price(symbol=trade["symbol"])
                    mark = float(mark_row.get("markPrice") or 0)
                if (trade["direction"] == "LONG" and mark <= entry) or (
                    trade["direction"] == "SHORT" and mark >= entry
                ):
                    return False, "Mark price has crossed breakeven; replacing the stop could trigger immediately."
                info = await client.futures_exchange_info()
                symbol_info = next(
                    (item for item in info.get("symbols", []) if item.get("symbol") == trade["symbol"]),
                    {},
                )
                current_stop = float(trade.get("stop_price") or 0)
                if trade["direction"] == "LONG" and current_stop >= entry:
                    return True, "Stop is already at or above breakeven."
                if trade["direction"] == "SHORT" and current_stop and current_stop <= entry:
                    return True, "Stop is already at or below breakeven."
                await self._replace_stop(client, trade, entry, symbol_info)
                updated = get_trade(trade_id) or trade
                await self._notify_trade_stage(
                    client, updated, "🛡 SL MOVED TO BREAKEVEN"
                )
                return True, "Binance confirmed the breakeven stop is open."
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Breakeven stop update failed trade_id=%s error=%s",
                    trade_id, safe_error_name(exc),
                )
                return False, f"Breakeven stop was not confirmed ({safe_error_name(exc)})."
            finally:
                await session.close()

    async def refresh_trade(
        self, trade_id: int, *, owner_id: int | None
    ) -> tuple[bool, str]:
        trade = get_trade(trade_id)
        if not trade or (
            owner_id is not None and int(trade["user_id"]) != int(owner_id)
        ):
            return False, "This trade is unavailable to your account."
        async with self._monitor_lock:
            await self._manage_trade(trade)
        updated = get_trade(trade_id)
        if not updated:
            return False, "Trade record is unavailable."
        return True, f"Binance data refreshed · Status: {updated['status']}."

    async def _cancel_tracked_orders(
        self, client: Any, trade: dict[str, Any], except_id: str | None = None
    ) -> None:
        try:
            tp_ids = json.loads(trade.get("tp_order_ids") or "[]")
            order_ids = set(json.loads(trade.get("exchange_order_ids") or "[]"))
            order_ids.update(
                str(value)
                for value in (
                    trade.get("sl_order_id"),
                    trade.get("stop_order_id"),
                    *tp_ids,
                    trade.get("tp1_order_id"),
                    trade.get("tp2_order_id"),
                    trade.get("tp3_order_id"),
                )
                if value
            )
        except (TypeError, ValueError):
            order_ids = set()
        try:
            open_algo = await self._open_algo_orders(client, str(trade["symbol"]))
            open_algo_ids = {self._algo_id(item) for item in open_algo}
        except Exception as exc:
            logger.warning(
                "Could not list open algo orders during cleanup trade_id=%s error=%s",
                trade.get("trade_id"),
                safe_error_name(exc),
            )
            open_algo_ids = set()
        for order_id in order_ids:
            if not order_id or str(order_id) == except_id:
                continue
            try:
                if str(order_id) in open_algo_ids:
                    await client.futures_cancel_algo_order(
                        symbol=trade["symbol"], algoId=order_id
                    )
                    continue
                # The detail endpoint distinguishes already-filled/cancelled
                # Algo orders from legacy standard Futures order IDs.
                algo = await self._get_algo_order(
                    client, str(trade["symbol"]), str(order_id)
                )
                if algo is not None:
                    continue
                # Compatibility for orders created by releases that predate the
                # Binance conditional-order Algo Service migration.
                order = await client.futures_get_order(
                    symbol=trade["symbol"], orderId=order_id
                )
                if order.get("status") in {"NEW", "PARTIALLY_FILLED"}:
                    await client.futures_cancel_order(
                        symbol=trade["symbol"], orderId=order_id
                    )
            except Exception as exc:
                logger.warning(
                    "Tracked order cancellation unresolved trade_id=%s order_id=%s error=%s",
                    trade["trade_id"],
                    order_id,
                    safe_error_name(exc),
                )

    async def monitor(self) -> None:
        """Reconcile open bot positions and advance protection after TP1."""
        try:
            await self._startup_recovery_scan()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Startup exchange recovery scan failed error=%s",
                safe_error_name(exc),
            )
        while True:
            try:
                async with self._monitor_lock:
                    for trade in all_active_trades():
                        try:
                            await self._manage_trade(trade)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "Position reconciliation failed trade_id=%s error=%s",
                                trade["trade_id"],
                                safe_error_name(exc),
                            )
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Trade monitor cycle failed error=%s", safe_error_name(exc))
                await asyncio.sleep(20)

    async def _startup_recovery_scan(self) -> None:
        """Inventory exchange positions/orders once before the live monitor loop."""
        trades_by_owner: dict[int, list[dict[str, Any]]] = {}
        for trade in all_active_trades():
            trades_by_owner.setdefault(int(trade["user_id"]), []).append(trade)
        environments_by_owner: dict[int, set[str]] = {}
        for user in all_users_with_preferences():
            environments_by_owner.setdefault(int(user["user_id"]), set()).add(
                str(user["environment"])
            )
        for user_id, trades in trades_by_owner.items():
            environments_by_owner.setdefault(user_id, set()).update(
                str(trade["environment"]) for trade in trades
            )

        for user_id, environments in environments_by_owner.items():
            for environment in environments:
                try:
                    credentials = self.vault.load_for_user(user_id, environment)
                except Exception:
                    credentials = None
                if not credentials:
                    continue
                session = FuturesSession(
                    api_key=credentials[0],
                    api_secret=credentials[1],
                    testnet=environment == "testnet",
                )
                try:
                    client = await session.connect()
                    positions = await client.futures_position_information()
                    open_positions = [
                        position
                        for position in positions
                        if abs(float(position.get("positionAmt", 0) or 0)) > 0
                    ]
                    open_algo = await self._open_algo_orders(client, "")
                    active_symbols = {
                        str(trade["symbol"])
                        for trade in trades_by_owner.get(user_id, [])
                        if str(trade["environment"]) == environment
                    }
                    logger.info(
                        "Startup exchange inventory user_id=%s environment=%s "
                        "open_positions=%s open_algo_orders=%s",
                        user_id, environment, len(open_positions), len(open_algo),
                    )
                    for position in open_positions:
                        symbol = str(position.get("symbol") or "")
                        if symbol and symbol not in active_symbols:
                            quantity = float(position.get("positionAmt", 0) or 0)
                            direction = "LONG" if quantity > 0 else "SHORT"
                            warning = (
                                "⚠️ UNTRACKED BINANCE POSITION\n"
                                f"Environment: {environment.upper()}\n"
                                f"Symbol: {symbol}\nSide: {direction}\n"
                                f"Quantity: {abs(quantity):g}\n"
                                "No active bot trade record exists, so the bot did not "
                                "change or adopt this position. Verify its Binance SL/TP manually."
                            )
                            await self._notify(user_id, warning)
                            await self._alert_admin(
                                f"{warning}\nTelegram user: {user_id}"
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Startup account recovery scan failed user_id=%s "
                        "environment=%s error=%s",
                        user_id, environment, safe_error_name(exc),
                    )
                finally:
                    await session.close()

    async def _manage_trade(self, trade: dict[str, Any]) -> None:
        try:
            credentials = self.vault.load_for_user(int(trade["user_id"]), trade["environment"])
        except Exception:
            credentials = None
        if not credentials:
            return
        session = FuturesSession(
            api_key=credentials[0],
            api_secret=credentials[1],
            testnet=trade["environment"] == "testnet",
        )
        try:
            client = await session.connect()
            positions = await client.futures_position_information(symbol=trade["symbol"])
            position = next(
                (p for p in positions if p.get("symbol") == trade["symbol"]),
                None,
            )
            amount = float(position.get("positionAmt", 0) or 0) if position else 0.0
            if abs(amount) <= 0:
                close_status, close_reason = await self._confirmed_close_reason(client, trade)
                await self._cancel_tracked_orders(client, trade)
                await self._mark_closed(client, trade, close_status, close_reason)
                return
            if (amount > 0) != (trade["direction"] == "LONG"):
                logger.error(
                    "Exchange position direction mismatch trade_id=%s symbol=%s",
                    trade["trade_id"],
                    trade["symbol"],
                )
                update_trade(
                    int(trade["trade_id"]),
                    status="PROTECTION_ERROR",
                    protection_error="Binance position direction does not match the stored trade.",
                )
                await self._alert_admin(
                    f"⚠️ PROTECTION ERROR\nSymbol: {trade['symbol']}\n"
                    "Reason: exchange position direction differs from bot state; no automatic order was sent."
                )
                return
            ticker = await client.futures_mark_price(symbol=trade["symbol"])
            mark = float(ticker.get("markPrice", 0) or 0)
            if mark <= 0:
                return
            signal = get_signal(int(trade["signal_id"]))
            if not signal:
                update_trade(
                    int(trade["trade_id"]),
                    status="PROTECTION_ERROR",
                    protection_error="Stored signal with the trade's SL/TP levels is missing.",
                )
                return
            tp_hits = await self._confirmed_take_profit_hits(client, trade)
            trade = get_trade(int(trade["trade_id"])) or trade
            # TP market triggers can change quantity between consecutive REST
            # calls; base the next protection decisions on a fresh position.
            positions = await client.futures_position_information(
                symbol=trade["symbol"]
            )
            position = next(
                (item for item in positions if item.get("symbol") == trade["symbol"]),
                None,
            )
            amount = float(position.get("positionAmt", 0) or 0) if position else 0.0
            if abs(amount) <= 0:
                close_status, close_reason = await self._confirmed_close_reason(
                    client, trade
                )
                await self._cancel_tracked_orders(client, trade)
                await self._mark_closed(client, trade, close_status, close_reason)
                return
            if tp_hits[2]:
                if await self._close_tp3_residual(client, trade, amount):
                    return
                # Refresh after a partial close so the displayed quantity and
                # PnL still come from Binance's latest position response.
                positions = await client.futures_position_information(
                    symbol=trade["symbol"]
                )
                position = next(
                    (p for p in positions if p.get("symbol") == trade["symbol"]),
                    None,
                )
                amount = float(position.get("positionAmt", 0) or 0) if position else 0.0
                if abs(amount) <= 0:
                    await self._cancel_tracked_orders(client, trade)
                    await self._mark_closed(
                        client, trade, "COMPLETED", "TP3_CONFIRMED"
                    )
                    return
            await self._maybe_advance_stop(client, trade, mark, tp_hits)
            trade = get_trade(int(trade["trade_id"])) or trade
            try:
                info = await client.futures_exchange_info()
                symbol_info = next(
                    (
                        item for item in info.get("symbols", [])
                        if item.get("symbol") == trade["symbol"]
                    ),
                    {},
                )
                try:
                    open_algo = await self._open_algo_orders(client, trade["symbol"])
                except Exception:
                    open_algo = None
                    raise
                open_ids = {self._algo_id(item) for item in open_algo}
                required_ids = [
                    trade.get("sl_order_id") or trade.get("stop_order_id"),
                    *json.loads(trade.get("tp_order_ids") or "[]"),
                ]
                missing_stop = not required_ids[0] or str(required_ids[0]) not in open_ids
                signal_targets_done = [
                    bool(trade.get(f"tp{i}_hit")) for i in range(1, 4)
                ]
                missing_tp = any(
                    not signal_targets_done[index]
                    and (
                        index + 1 >= len(required_ids)
                        or not required_ids[index + 1]
                        or str(required_ids[index + 1]) not in open_ids
                    )
                    for index in range(3)
                )
                if missing_stop or missing_tp or trade.get("status") in {
                    "OPEN", "UNKNOWN", "PROTECTION_ERROR", "PROTECTION_FAILED",
                }:
                    await self._install_protection(
                        client, trade, signal, position, symbol_info
                    )
                    trade = get_trade(int(trade["trade_id"])) or trade
            except Exception as exc:
                reason = safe_error_name(exc)
                logger.error(
                    "Protection recovery failed trade_id=%s reason=%s",
                    trade["trade_id"],
                    reason,
                )
                update_trade(
                    int(trade["trade_id"]),
                    status="PROTECTION_ERROR",
                    protection_error=reason,
                )
                await self._notify(
                    int(trade["user_id"]),
                    f"⚠️ PROTECTION ERROR\nSymbol: {trade['symbol']}\nReason: {reason}\n"
                    "Binance order-book recovery is being retried. Review the position now.",
                )
                await self._alert_admin(
                    f"⚠️ PROTECTION ERROR\nSymbol: {trade['symbol']}\nReason: {reason}\n"
                    "Recovery will retry; verify a live Binance stop is present."
                )
                # A live SL is the minimum safe condition. If it is absent,
                # attempt reduce-only flattening instead of leaving naked risk.
                try:
                    open_after_failure = await self._open_algo_orders(client, trade["symbol"])
                    has_stop = any(
                        str(item.get("orderType") or item.get("type") or "") == "STOP_MARKET"
                        and self._algo_is_open(item)
                        for item in open_after_failure
                    )
                except Exception:
                    has_stop = False
                if not has_stop:
                    closed = await self._emergency_close(
                        client,
                        trade["symbol"],
                        "SELL" if amount > 0 else "BUY",
                        abs(amount),
                        trade["setup_hash"],
                    )
                    if closed:
                        refreshed = await client.futures_position_information(
                            symbol=trade["symbol"]
                        )
                        remaining = next(
                            (item for item in refreshed if item.get("symbol") == trade["symbol"]),
                            None,
                        )
                        if not remaining or abs(float(remaining.get("positionAmt", 0) or 0)) <= 0:
                            await self._cancel_tracked_orders(
                                client, get_trade(int(trade["trade_id"])) or trade
                            )
                            await self._mark_closed(
                                client,
                                get_trade(int(trade["trade_id"])) or trade,
                                "COMPLETED",
                                "POSITION_CLOSED_AFTER_MISSING_STOP",
                            )
                            return
                    await self._alert_admin(
                        f"🚨 STOP MISSING for {trade['symbol']}; emergency close "
                        f"{'was not confirmed' if not closed else 'did not verify flat'}. "
                        "Intervene in Binance Futures immediately."
                    )
            entry = float(position.get("entryPrice") or trade.get("entry_price") or 0)
            raw_leverage = position.get("leverage")
            leverage = float(raw_leverage) if raw_leverage not in (None, "", "0", 0) else None
            pnl = (
                float(position["unRealizedProfit"])
                if position.get("unRealizedProfit") is not None
                else None
            )
            pnl_percent = (
                pnl / (abs(float(trade.get("quantity") or abs(amount))) * entry / leverage) * 100
                if pnl is not None and entry > 0 and leverage and leverage > 0
                else None
            )
            if pnl_percent is None:
                logger.info(
                    "Live PnL percent unavailable trade_id=%s leverage=%s entry=%s",
                    trade["trade_id"], raw_leverage, entry,
                )
            tp_fill_ids = [
                str(trade[f"tp{index}_actual_order_id"])
                for index in range(1, 4)
                if trade.get(f"tp{index}_actual_order_id")
            ]
            realized_pnl: float | None = 0.0 if not tp_fill_ids else None
            realized_verified = not tp_fill_ids
            if tp_fill_ids:
                realized_value, realized_verified = await self._realized_pnl_for_order_ids(
                    client, str(trade["symbol"]), tp_fill_ids
                )
                if realized_verified:
                    realized_pnl = realized_value
                    update_trade(
                        int(trade["trade_id"]),
                        realized_pnl=realized_value,
                        pnl_verified=1,
                    )
                else:
                    logger.info(
                        "Open-trade realized PnL unavailable trade_id=%s",
                        trade["trade_id"],
                    )
            live_trade = dict(trade)
            update_trade(
                int(trade["trade_id"]),
                remaining_quantity=abs(amount),
            )
            live_trade = get_trade(int(trade["trade_id"])) or live_trade
            live_trade.update(
                {
                    "entry_price": entry,
                    "mark_price": mark,
                    "position_amount": amount,
                    "quantity": float(trade.get("quantity") or abs(amount)),
                    "remaining_quantity": abs(amount),
                    "leverage": leverage,
                    "unrealized_pnl": pnl,
                    "pnl_percent": pnl_percent,
                    "realized_pnl": realized_pnl,
                    "realized_pnl_verified": realized_verified,
                    "tp_hits": tp_hits,
                    "stop_price": trade.get("stop_price") or signal.get("stop"),
                    "tp1": signal.get("tp1"),
                    "tp2": signal.get("tp2"),
                    "tp3": signal.get("tp3"),
                }
            )
            await self._update_live_trade_message(live_trade)
        finally:
            await session.close()

    async def _close_tp3_residual(
        self, client: Any, trade: dict[str, Any], amount: float
    ) -> bool:
        """Close any exchange-confirmed remainder after the TP3 order fills."""
        if abs(amount) <= 0:
            return True
        # Cancel remaining limit exits first, but keep the protective stop live
        # until Binance confirms that the position is flat.
        await self._cancel_tracked_orders(
            client, trade, except_id=trade.get("stop_order_id")
        )
        info = await client.futures_exchange_info()
        symbol_info = next(
            (
                item
                for item in info.get("symbols", [])
                if item.get("symbol") == trade["symbol"]
            ),
            {},
        )
        step, _tick = self._steps(symbol_info)
        quantity = decimal_floor(abs(amount), step)
        if quantity <= 0:
            return False
        side = "SELL" if amount > 0 else "BUY"
        client_id = f"t3c{trade['setup_hash'][:16]}{int(time.time()) % 100000000:08d}"
        try:
            order = await client.futures_create_order(
                symbol=trade["symbol"],
                side=side,
                type="MARKET",
                quantity=quantity,
                reduceOnly="true",
                newClientOrderId=client_id[:36],
            )
        except Exception as exc:
            try:
                order = await client.futures_get_order(
                    symbol=trade["symbol"],
                    origClientOrderId=client_id[:36],
                )
            except Exception:
                logger.warning(
                    "TP3 residual close outcome unknown trade_id=%s error=%s",
                    trade["trade_id"],
                    safe_error_name(exc),
                )
                return False
        order_id = str(order.get("orderId", ""))
        if not order_id or order.get("status") not in {
            "FILLED",
            "PARTIALLY_FILLED",
        }:
            logger.warning(
                "TP3 residual close not confirmed trade_id=%s", trade["trade_id"]
            )
            return False
        try:
            order_ids = json.loads(trade.get("exchange_order_ids") or "[]")
        except (TypeError, ValueError):
            order_ids = []
        order_ids.append(order_id)
        update_trade(
            int(trade["trade_id"]),
            exchange_order_ids=json.dumps(list(dict.fromkeys(order_ids))),
        )
        positions = await client.futures_position_information(
            symbol=trade["symbol"]
        )
        current = next(
            (item for item in positions if item.get("symbol") == trade["symbol"]),
            None,
        )
        remaining = float(current.get("positionAmt", 0) or 0) if current else 0.0
        if abs(remaining) > 0:
            logger.warning(
                "TP3 confirmed but residual remains trade_id=%s qty=%s",
                trade["trade_id"],
                abs(remaining),
            )
            return False
        latest = get_trade(int(trade["trade_id"])) or trade
        await self._cancel_tracked_orders(client, latest)
        await self._mark_closed(client, latest, "COMPLETED", "TP3_CONFIRMED")
        return True

    async def _confirmed_take_profit_hits(
        self, client: Any, trade: dict[str, Any]
    ) -> list[bool]:
        try:
            tp_ids = json.loads(trade.get("tp_order_ids") or "[]")
        except (TypeError, ValueError):
            tp_ids = []
        try:
            actual_ids = json.loads(trade.get("tp_actual_order_ids") or "[]")
        except (TypeError, ValueError):
            actual_ids = []
        hits = []
        for index in range(3):
            already_hit = bool(trade.get(f"tp{index + 1}_hit"))
            order_id = (
                trade.get(f"tp{index + 1}_order_id")
                or (tp_ids[index] if index < len(tp_ids) else None)
            )
            filled = already_hit
            actual_order_id = (
                trade.get(f"tp{index + 1}_actual_order_id")
                or (actual_ids[index] if index < len(actual_ids) else None)
            )
            if order_id and not already_hit:
                try:
                    algo = await self._get_algo_order(
                        client, trade["symbol"], str(order_id)
                    )
                    if algo:
                        actual_order_id = algo.get("actualOrderId") or None
                        if actual_order_id:
                            executed = await client.futures_get_order(
                                symbol=trade["symbol"],
                                orderId=actual_order_id,
                            )
                            filled = (
                                executed.get("status") == "FILLED"
                                and float(executed.get("executedQty", 0) or 0) > 0
                            )
                    else:
                        # Legacy pre-algo conditional orders remain queryable
                        # for trades that were already open during deployment.
                        legacy = await client.futures_get_order(
                            symbol=trade["symbol"], orderId=order_id
                        )
                        filled = (
                            legacy.get("status") == "FILLED"
                            and float(legacy.get("executedQty", 0) or 0) > 0
                        )
                        actual_order_id = order_id if filled else actual_order_id
                except Exception as exc:
                    logger.warning(
                        "Take-profit status check failed trade_id=%s target=%s error=%s",
                        trade["trade_id"],
                        index + 1,
                        safe_error_name(exc),
                    )
            if filled and not already_hit:
                hit_at = datetime.now(timezone.utc).isoformat()
                realized, verified = await self._realized_pnl_for_order_ids(
                    client, str(trade["symbol"]), [str(actual_order_id)] if actual_order_id else []
                )
                try:
                    actual_ids[index] = str(actual_order_id) if actual_order_id else None
                except IndexError:
                    actual_ids.extend([None] * (index + 1 - len(actual_ids)))
                    actual_ids[index] = str(actual_order_id) if actual_order_id else None
                status = f"TP{index + 1}_HIT"
                update_trade(
                    int(trade["trade_id"]),
                    **{
                        f"tp{index + 1}_hit": 1,
                        f"tp{index + 1}_at": hit_at,
                        f"tp{index + 1}_actual_order_id": str(actual_order_id) if actual_order_id else None,
                        f"tp{index + 1}_realized_pnl": realized if verified else None,
                        "tp_actual_order_ids": json.dumps(actual_ids),
                        "status": status,
                    },
                )
                log_event(
                    f"trade.tp{index + 1}_hit",
                    int(trade["user_id"]),
                    trade_id=trade["trade_id"],
                    signal_id=trade["signal_id"],
                    symbol=trade["symbol"],
                    timestamp=hit_at,
                    reason=f"Binance TP{index + 1} order confirmed FILLED",
                )
                trade[f"tp{index + 1}_hit"] = 1
                trade["status"] = status
                await self._notify_trade_stage(
                    client,
                    trade,
                    f"🎯 TP{index + 1} HIT",
                    actual_order_id=str(actual_order_id) if actual_order_id else None,
                    realized_pnl=realized if verified else None,
                )
            hits.append(filled)
        return hits

    async def _realized_pnl_for_order_ids(
        self, client: Any, symbol: str, order_ids: list[str]
    ) -> tuple[float, bool]:
        ids = {str(value) for value in order_ids if value}
        if not ids:
            return 0.0, False
        try:
            rows = await client.futures_account_trades(
                symbol=symbol,
                startTime=0,
                limit=1000,
            )
            fills = [item for item in rows if str(item.get("orderId")) in ids]
            if not fills:
                return 0.0, False
            value = sum(
                float(item.get("realizedPnl", 0) or 0)
                - (
                    float(item.get("commission", 0) or 0)
                    if item.get("commissionAsset") == "USDT"
                    else 0.0
                )
                for item in fills
            )
            return value, True
        except Exception as exc:
            logger.info(
                "Realized fill PnL unavailable symbol=%s error=%s",
                symbol,
                safe_error_name(exc),
            )
            return 0.0, False

    async def _notify_trade_stage(
        self,
        client: Any,
        trade: dict[str, Any],
        title: str,
        *,
        actual_order_id: str | None = None,
        realized_pnl: float | None = None,
    ) -> None:
        try:
            positions = await client.futures_position_information(
                symbol=trade["symbol"]
            )
            position = next(
                (item for item in positions if item.get("symbol") == trade["symbol"]),
                None,
            )
            remaining = abs(float(position.get("positionAmt", 0) or 0)) if position else 0.0
            mark = float(position.get("markPrice", 0) or 0) if position else 0.0
            entry = float(position.get("entryPrice") or trade.get("entry_price") or 0) if position else float(trade.get("entry_price") or 0)
            leverage = float(position.get("leverage", 0) or 0) if position else 0.0
            pnl = float(position.get("unRealizedProfit", 0) or 0) if position else 0.0
            pnl_pct = (
                pnl / (float(trade.get("quantity") or 0) * entry / leverage) * 100
                if entry > 0 and leverage > 0 and trade.get("quantity")
                else None
            )
            exit_price = None
            if actual_order_id:
                order = await client.futures_get_order(
                    symbol=trade["symbol"], orderId=actual_order_id
                )
                exit_price = float(order.get("avgPrice", 0) or 0) or None
        except Exception as exc:
            logger.info(
                "Trade stage snapshot unavailable trade_id=%s error=%s",
                trade.get("trade_id"),
                safe_error_name(exc),
            )
            remaining, mark, entry, pnl, pnl_pct, exit_price = 0.0, 0.0, float(trade.get("entry_price") or 0), None, None, None
        realized_text = (
            f"{realized_pnl:+.4f} USDT" if realized_pnl is not None else "N/A (fill lookup unavailable)"
        )
        text = (
            f"{title}\nSymbol: {trade['symbol']}\nSide: {trade['direction']}\n"
            f"Entry: {entry:g}\nExit/current: {exit_price or mark or 'N/A'}\n"
            f"Realized PnL: {realized_text}\n"
            f"Unrealized PnL: {pnl:+.4f} USDT" if pnl is not None else
            f"{title}\nSymbol: {trade['symbol']}\nSide: {trade['direction']}\n"
            f"Entry: {entry:g}\nExit/current: {exit_price or mark or 'N/A'}\n"
            f"Realized PnL: {realized_text}\nUnrealized PnL: N/A"
        )
        text += (
            f"\nPnL %: {pnl_pct:+.2f}%" if pnl_pct is not None else "\nPnL %: N/A"
        )
        text += f"\nRemaining quantity: {remaining:g}\nStatus: {trade.get('status', 'OPEN')}"
        await self._notify(int(trade["user_id"]), text)

    async def _maybe_advance_stop(
        self, client: Any, trade: dict[str, Any], mark: float, tp_hits: list[bool]
    ) -> None:
        signal = get_signal(int(trade["signal_id"]))
        if not signal:
            return
        entry = float(trade.get("entry_price") or signal["entry"])
        original_stop = float(signal["stop"])
        distance = abs(entry - original_stop)
        if distance <= 0:
            return
        direction = trade["direction"]
        current_stop = float(trade.get("stop_price") or original_stop)
        candidate = current_stop
        if tp_hits and tp_hits[0] and get_setting("move_sl_to_breakeven", "1") == "1":
            candidate = max(candidate, entry) if direction == "LONG" else min(candidate, entry)
        preference = get_preference(int(trade["user_id"]))
        if (
            bool(preference.get("trailing_stop"))
            and get_setting("trailing_stop_enabled", "1") == "1"
        ):
            reached = mark >= entry + 2 * distance if direction == "LONG" else mark <= entry - 2 * distance
            if reached:
                try:
                    trailing_percent = min(
                        10.0,
                        max(0.05, float(get_setting("trailing_stop_percent", "1") or 1)),
                    )
                except (TypeError, ValueError):
                    trailing_percent = 1.0
                trailing = (
                    mark * (1 - trailing_percent / 100)
                    if direction == "LONG"
                    else mark * (1 + trailing_percent / 100)
                )
                candidate = max(candidate, trailing) if direction == "LONG" else min(candidate, trailing)
        improves = candidate > current_stop if direction == "LONG" else candidate < current_stop
        if not improves or (direction == "LONG" and candidate >= mark) or (direction == "SHORT" and candidate <= mark):
            return
        info = await client.futures_exchange_info()
        symbol_info = next(
            (item for item in info.get("symbols", []) if item.get("symbol") == trade["symbol"]),
            {},
        )
        _step, tick = self._steps(symbol_info)
        candidate = decimal_ceil(candidate, tick) if direction == "LONG" else decimal_floor(candidate, tick)
        if (
            (direction == "LONG" and candidate >= mark)
            or (direction == "SHORT" and candidate <= mark)
        ):
            return
        _step, tick = self._steps(symbol_info)
        moving_to_be = bool(
            tp_hits
            and tp_hits[0]
            and get_setting("move_sl_to_breakeven", "1") == "1"
            and abs(candidate - entry) <= tick / 2
        )
        await self._replace_stop(client, trade, candidate, symbol_info, mark=mark)
        update_trade(int(trade["trade_id"]), best_price=mark)
        if moving_to_be:
            await self._notify_trade_stage(
                client,
                get_trade(int(trade["trade_id"])) or trade,
                "🛡 SL MOVED TO BREAKEVEN",
            )

    async def _replace_stop(
        self,
        client: Any,
        trade: dict[str, Any],
        candidate: float,
        symbol_info: dict[str, Any],
        *,
        mark: float | None = None,
    ) -> str:
        """Install and verify a replacement Algo stop before retiring the old one."""
        direction = str(trade["direction"])
        _step, tick = self._steps(symbol_info)
        if tick <= 0:
            raise RuntimeError("Binance price tick is unavailable")
        candidate = (
            decimal_floor(candidate, tick)
            if direction == "LONG"
            else decimal_ceil(candidate, tick)
        )
        if mark is not None and (
            (direction == "LONG" and candidate >= mark)
            or (direction == "SHORT" and candidate <= mark)
        ):
            raise RuntimeError("replacement stop would trigger at the current mark price")
        close_side = "SELL" if direction == "LONG" else "BUY"
        tick_count = int(round(candidate / tick))
        client_id = f"st{str(trade['setup_hash'])[:18]}{tick_count:x}"[:36]
        open_orders = await self._open_algo_orders(client, str(trade["symbol"]))
        replacement = next(
            (
                item for item in open_orders
                if item.get("clientAlgoId") == client_id
                or (
                    self._algo_id(item) == str(trade.get("sl_order_id") or trade.get("stop_order_id") or "")
                    and abs(float(item.get("triggerPrice") or 0) - candidate) <= tick / 2
                )
            ),
            None,
        )
        if replacement is None:
            response = await client.futures_create_algo_order(
                algoType="CONDITIONAL",
                symbol=trade["symbol"],
                side=close_side,
                type="STOP_MARKET",
                triggerPrice=candidate,
                closePosition="true",
                workingType="MARK_PRICE",
                clientAlgoId=client_id,
            )
            new_order_id = self._algo_id(response)
            if not new_order_id:
                raise RuntimeError("Binance did not return an algoId for the replacement stop")
            # A successful create response alone is not proof of open protection.
            confirmed = await self._open_algo_orders(client, str(trade["symbol"]))
            replacement = next(
                (item for item in confirmed if self._algo_id(item) == new_order_id),
                None,
            )
        else:
            new_order_id = self._algo_id(replacement)
        if (
            not replacement
            or not self._algo_is_open(replacement)
            or str(replacement.get("orderType") or replacement.get("type") or "") != "STOP_MARKET"
            or str(replacement.get("closePosition", "")).lower() != "true"
            or abs(float(replacement.get("triggerPrice") or 0) - candidate) > tick / 2
        ):
            raise RuntimeError("Binance did not verify the replacement stop as an open close-position order")

        old_id = str(trade.get("sl_order_id") or trade.get("stop_order_id") or "")
        if old_id and old_id != new_order_id:
            try:
                await client.futures_cancel_algo_order(
                    symbol=trade["symbol"], algoId=old_id
                )
            except Exception as exc:
                # The verified new stop remains live if old-order cancellation is
                # ambiguous; report rather than removing confirmed protection.
                logger.warning(
                    "Old Algo stop cancellation unresolved trade_id=%s error=%s",
                    trade["trade_id"], safe_error_name(exc),
                )
        try:
            tracked_ids = json.loads(trade.get("exchange_order_ids") or "[]")
        except (TypeError, ValueError):
            tracked_ids = []
        tracked_ids.append(new_order_id)
        update_trade(
            int(trade["trade_id"]),
            stop_order_id=new_order_id,
            sl_order_id=new_order_id,
            exchange_order_ids=json.dumps(list(dict.fromkeys(tracked_ids))),
            stop_price=candidate,
            best_price=mark if mark is not None else trade.get("best_price"),
        )
        log_event(
            "trade.stop_advanced",
            int(trade["user_id"]),
            trade_id=trade["trade_id"],
            stop=candidate,
        )
        return new_order_id

    async def _confirmed_close_reason(
        self, client: Any, trade: dict[str, Any]
    ) -> tuple[str, str]:
        try:
            tp_ids = json.loads(trade.get("tp_order_ids") or "[]")
        except (TypeError, ValueError):
            tp_ids = []
        tp3_id = trade.get("tp3_order_id") or (tp_ids[2] if len(tp_ids) > 2 else None)
        if tp3_id or trade.get("tp3_hit"):
            try:
                if trade.get("tp3_hit"):
                    return "COMPLETED", "TP3_CONFIRMED"
                algo = await self._get_algo_order(client, trade["symbol"], str(tp3_id))
                actual_id = algo.get("actualOrderId") if algo else None
                if actual_id:
                    executed = await client.futures_get_order(
                        symbol=trade["symbol"], orderId=actual_id
                    )
                    if executed.get("status") == "FILLED" and float(
                        executed.get("executedQty", 0) or 0
                    ) > 0:
                        update_trade(
                            int(trade["trade_id"]),
                            tp3_actual_order_id=str(actual_id),
                            tp3_hit=1,
                        )
                        return "COMPLETED", "TP3_CONFIRMED"
            except Exception:
                pass
        stop_id = trade.get("sl_order_id") or trade.get("stop_order_id")
        if stop_id:
            try:
                algo = await self._get_algo_order(client, trade["symbol"], str(stop_id))
                actual_id = algo.get("actualOrderId") if algo else None
                if actual_id:
                    executed = await client.futures_get_order(
                        symbol=trade["symbol"], orderId=actual_id
                    )
                    if executed.get("status") == "FILLED" and float(
                        executed.get("executedQty", 0) or 0
                    ) > 0:
                        update_trade(
                            int(trade["trade_id"]),
                            sl_actual_order_id=str(actual_id),
                        )
                        return "SL_HIT", "STOP_LOSS_CONFIRMED"
                if not algo:
                    legacy = await client.futures_get_order(
                        symbol=trade["symbol"], orderId=stop_id
                    )
                    if legacy.get("status") == "FILLED":
                        update_trade(
                            int(trade["trade_id"]), sl_actual_order_id=str(stop_id)
                        )
                        return "SL_HIT", "STOP_LOSS_CONFIRMED"
            except Exception:
                pass
        return "MANUALLY_CLOSED", "POSITION_CLOSED_WITHOUT_TRACKED_TP3_OR_STOP_FILL"

    async def _mark_closed(
        self,
        client: Any,
        trade: dict[str, Any],
        status: str = "MANUALLY_CLOSED",
        reason: str = "POSITION_CLOSED_EXTERNALLY_OR_MANUALLY",
    ) -> None:
        start_ms = 0
        try:
            start_ms = int(datetime.fromisoformat(trade["created_at"]).timestamp() * 1000)
        except (ValueError, TypeError):
            pass
        pnl = 0.0
        pnl_verified = False
        exit_price: float | None = None
        try:
            order_ids = set(json.loads(trade.get("exchange_order_ids") or "[]"))
            order_ids.update(
                str(value)
                for value in (
                    trade.get("entry_order_id"),
                    trade.get("sl_actual_order_id"),
                    trade.get("stop_order_id"),
                    trade.get("sl_order_id"),
                    trade.get("tp1_actual_order_id"),
                    trade.get("tp2_actual_order_id"),
                    trade.get("tp3_actual_order_id"),
                    *json.loads(trade.get("tp_order_ids") or "[]"),
                    *json.loads(trade.get("tp_actual_order_ids") or "[]"),
                )
                if value
            )
            for algo_id in (
                trade.get("sl_order_id") or trade.get("stop_order_id"),
                trade.get("tp3_order_id"),
            ):
                if not algo_id:
                    continue
                algo = await self._get_algo_order(
                    client, str(trade["symbol"]), str(algo_id)
                )
                if algo and algo.get("actualOrderId"):
                    order_ids.add(str(algo["actualOrderId"]))
                    if str(algo_id) == str(trade.get("sl_order_id") or trade.get("stop_order_id")):
                        update_trade(
                            int(trade["trade_id"]),
                            sl_actual_order_id=str(algo["actualOrderId"]),
                        )
                    elif str(algo_id) == str(trade.get("tp3_order_id")):
                        update_trade(
                            int(trade["trade_id"]),
                            tp3_actual_order_id=str(algo["actualOrderId"]),
                        )
            rows = await client.futures_account_trades(
                symbol=trade["symbol"],
                startTime=start_ms,
                limit=1000,
            )
            fills = [item for item in rows if str(item.get("orderId")) in order_ids]
            if fills:
                exit_fills = [
                    item
                    for item in fills
                    if str(item.get("orderId")) != str(trade.get("entry_order_id"))
                ]
                pnl = sum(
                    float(item.get("realizedPnl", 0) or 0)
                    - (
                        float(item.get("commission", 0) or 0)
                        if item.get("commissionAsset") == "USDT"
                        else 0.0
                    )
                    for item in fills
                )
                pnl_verified = bool(exit_fills)
                exit_qty = sum(float(item.get("qty", 0) or 0) for item in exit_fills)
                exit_quote = sum(
                    float(item.get("qty", 0) or 0) * float(item.get("price", 0) or 0)
                    for item in exit_fills
                )
                if exit_qty > 0:
                    exit_price = exit_quote / exit_qty
        except Exception:
            logger.info("Realized PnL reconciliation unavailable trade_id=%s", trade["trade_id"])
        update_trade(
            int(trade["trade_id"]),
            status=status,
            remaining_quantity=0,
            reason=reason,
            close_reason=reason,
            closed_at=datetime.now(timezone.utc).isoformat(),
            exit_price=exit_price,
            realized_pnl=pnl,
            pnl_verified=int(pnl_verified),
        )
        log_event(
            f"trade.{status.lower()}",
            int(trade["user_id"]),
            trade_id=trade["trade_id"],
            signal_id=trade["signal_id"],
            symbol=trade["symbol"],
            timestamp=datetime.now(timezone.utc).isoformat(),
            reason=reason,
            pnl=pnl if pnl_verified else "unavailable",
            pnl_verified=pnl_verified,
        )
        direction_icon = "🟢" if trade["direction"] == "LONG" else "🔴"
        pnl_text = (
            f"Realized PnL (Binance fills, tracked USDT fees): {pnl:+.4f} USDT"
            if pnl_verified
            else "Realized PnL: unavailable (Binance fills could not be reconciled)."
        )
        title = (
            "🛑 STOP LOSS HIT\n❌ TRADE CLOSED" if status == "SL_HIT"
            else "✅ TP3 HIT\n✅ TRADE COMPLETED"
            if status == "COMPLETED" and reason == "TP3_CONFIRMED"
            else "✅ POSITION FLAT · TRADE COMPLETED" if status == "COMPLETED"
            else "🔒 MANUALLY CLOSED" if status == "MANUALLY_CLOSED"
            else "⚠️ TRADE CLOSED AFTER PROTECTION ERROR" if status == "PROTECTION_ERROR"
            else "📕 TRADE CLOSED"
        )
        exit_line = f"Exit: {exit_price:g}\n" if exit_price is not None else ""
        entry = float(trade.get("entry_price") or 0)
        original_quantity = float(trade.get("quantity") or 0)
        leverage = None
        try:
            positions = await client.futures_position_information(symbol=trade["symbol"])
            row = next(
                (item for item in positions if item.get("symbol") == trade["symbol"]),
                None,
            )
            if row and float(row.get("leverage", 0) or 0) > 0:
                leverage = float(row["leverage"])
        except Exception as exc:
            logger.info("Closed trade leverage unavailable trade_id=%s error=%s", trade["trade_id"], safe_error_name(exc))
        pnl_percent = (
            pnl / (original_quantity * entry / leverage) * 100
            if pnl_verified and entry > 0 and original_quantity > 0 and leverage and leverage > 0
            else None
        )
        final_text = (
            f"{title}\n{direction_icon} {trade['direction']} {trade['symbol']}\n"
            f"Entry: {entry:g} · {exit_line}"
            f"PnL: {pnl:+.4f} USDT\n"
            f"PnL %: {pnl_percent:+.2f}%\n" if pnl_percent is not None else
            f"{title}\n{direction_icon} {trade['direction']} {trade['symbol']}\n"
            f"Entry: {entry:g} · {exit_line}"
            f"PnL: {pnl:+.4f} USDT\nPnL %: N/A\n"
        )
        final_text += f"Exit reason: {reason}\nStatus: {status}\n{pnl_text}"
        await self._edit_trade_message(trade, final_text)
        await self._notify(int(trade["user_id"]), final_text)

    async def _update_live_trade_message(self, trade: dict[str, Any]) -> None:
        direction = str(trade["direction"])
        icon = "🟢" if direction == "LONG" else "🔴"
        pnl = trade.get("unrealized_pnl")
        pnl_text = f"{float(pnl):+,.4f} USDT" if pnl is not None else "N/A"
        pnl_percent = trade.get("pnl_percent")
        percent_text = f"{float(pnl_percent):+,.2f}%" if pnl_percent is not None else "N/A"
        realized_pnl = trade.get("realized_pnl")
        realized_text = (
            f"{float(realized_pnl):+,.4f} USDT"
            if realized_pnl is not None
            else "N/A"
        )
        try:
            created = datetime.fromisoformat(str(trade["created_at"]))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            elapsed = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
            duration = f"{elapsed // 3600:02d}:{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}"
        except (ValueError, TypeError):
            duration = "Unavailable"
        hits = trade.get("tp_hits") or [
            bool(trade.get("tp1_hit")),
            bool(trade.get("tp2_hit")),
            bool(trade.get("tp3_hit")),
        ]
        tp_line = " · ".join(
            f"TP{i + 1}{' ✅' if i < len(hits) and hits[i] else ''}: "
            f"{self._display_number(trade.get(f'tp{i + 1}'))}"
            for i in range(3)
        )
        entry_text = self._display_number(trade.get("entry_price"))
        mark_text = self._display_number(trade.get("mark_price"))
        quantity_text = self._display_number(trade.get("quantity"))
        remaining_text = self._display_number(trade.get("remaining_quantity"))
        leverage = trade.get("leverage")
        leverage_text = f"{float(leverage):g}x" if leverage is not None and float(leverage) > 0 else "N/A"
        if leverage_text == "N/A":
            logger.info("Position leverage unavailable trade_id=%s", trade.get("trade_id"))
        status = str(trade.get("status") or "OPEN")
        if status not in {
            "OPEN", "PROTECTED", "TP1_HIT", "TP2_HIT", "TP3_HIT",
            "SL_HIT", "MANUALLY_CLOSED", "COMPLETED", "PROTECTION_ERROR",
        }:
            status = "PROTECTION_ERROR" if status in {"UNKNOWN", "PROTECTION_FAILED"} else "OPEN"
        text = (
            f"📊 TRADE MANAGER\n{icon} {direction} {trade['symbol']}\n\n"
            f"Entry: {entry_text}\n"
            f"Mark Price: {mark_text}\n"
            f"Quantity: {quantity_text} · Remaining: {remaining_text}\n"
            f"Leverage: {leverage_text}\n"
            f"Duration: {duration}\n\n"
            f"💵 Realized PnL: {realized_text}\n"
            f"💰 Unrealized PnL: {pnl_text}\n📈 PnL %: {percent_text}\n\n"
            f"🛑 SL: {self._display_number(trade.get('stop_price'))}\n"
            f"🎯 {tp_line}\n\nStatus: {status}"
        )
        await self._edit_trade_message(trade, text)

    @staticmethod
    def _display_number(value: Any) -> str:
        try:
            number = float(value)
            return f"{number:g}" if number > 0 else "N/A"
        except (TypeError, ValueError):
            return "N/A"

    @staticmethod
    def _trade_manager_keyboard(trade_id: int, active: bool = True) -> InlineKeyboardMarkup | None:
        if not active:
            return None
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔴 CLOSE TRADE", callback_data=f"trade:close:{trade_id}")],
                [
                    InlineKeyboardButton("🎯 TP1", callback_data=f"trade:tp1:{trade_id}"),
                    InlineKeyboardButton("🎯 TP2", callback_data=f"trade:tp2:{trade_id}"),
                    InlineKeyboardButton("🎯 TP3", callback_data=f"trade:tp3:{trade_id}"),
                ],
                [InlineKeyboardButton("🛡 MOVE SL → BE", callback_data=f"trade:be:{trade_id}")],
                [InlineKeyboardButton("📈 REFRESH", callback_data=f"trade:refresh:{trade_id}")],
            ]
        )

    async def _send_trade_manager(
        self,
        chat_id: int,
        trade: dict[str, Any],
        signal: dict[str, Any],
        *,
        header: str,
        extra: str = "",
    ) -> Any | None:
        snapshot = dict(trade)
        snapshot.setdefault("tp1", signal.get("tp1"))
        snapshot.setdefault("tp2", signal.get("tp2"))
        snapshot.setdefault("tp3", signal.get("tp3"))
        snapshot.setdefault("remaining_quantity", trade.get("quantity"))
        snapshot.setdefault("mark_price", None)
        snapshot.setdefault("leverage", None)
        lines = [
            header,
            f"Symbol: {trade.get('symbol', 'N/A')}",
            f"Side: {trade.get('direction', 'N/A')}",
            f"Entry: {self._display_number(trade.get('entry_price'))}",
            f"Mark: {self._display_number(snapshot.get('mark_price'))}",
            "PnL: N/A · PnL %: N/A",
            f"Leverage: {self._display_number(snapshot.get('leverage'))}",
            f"SL: {self._display_number(trade.get('stop_price'))}",
            f"TP1: {self._display_number(signal.get('tp1'))}",
            f"TP2: {self._display_number(signal.get('tp2'))}",
            f"TP3: {self._display_number(signal.get('tp3'))}",
            f"Remaining quantity: {self._display_number(snapshot.get('remaining_quantity'))}",
            f"Status: {trade.get('status', 'OPEN')}",
        ]
        if extra:
            lines.extend(["", extra])
        try:
            message = await self.application.bot.send_message(
                chat_id=chat_id,
                text="\n".join(lines)[:3900],
                reply_markup=self._trade_manager_keyboard(int(trade["trade_id"])),
                disable_web_page_preview=True,
            )
            update_trade(
                int(trade["trade_id"]),
                telegram_chat_id=chat_id,
                telegram_message_id=getattr(message, "message_id", None),
            )
            return message
        except Exception:
            logger.info("Trade manager delivery failed trade_id=%s", trade.get("trade_id"))
            return None

    async def _edit_trade_message(self, trade: dict[str, Any], text: str) -> None:
        chat_id = trade.get("telegram_chat_id") or int(trade["user_id"])
        message_id = trade.get("telegram_message_id")
        if message_id:
            try:
                await self.application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text[:3900],
                    reply_markup=self._trade_manager_keyboard(
                        int(trade["trade_id"]),
                        str(trade.get("status") or "") not in {
                            "COMPLETED", "SL_HIT", "MANUALLY_CLOSED",
                        },
                    ),
                    disable_web_page_preview=True,
                )
                return
            except Exception:
                logger.info(
                    "Live trade message edit failed trade_id=%s; retrying on next monitor cycle",
                    trade["trade_id"],
                )
                return
        try:
            message = await self.application.bot.send_message(
                chat_id=int(chat_id),
                text=text[:3900],
                reply_markup=self._trade_manager_keyboard(
                    int(trade["trade_id"]),
                    str(trade.get("status") or "") not in {
                        "COMPLETED", "SL_HIT", "MANUALLY_CLOSED",
                    },
                ),
                disable_web_page_preview=True,
            )
        except Exception:
            logger.info("Trade manager delivery failed trade_id=%s", trade["trade_id"])
            message = None
        if message is not None:
            update_trade(
                int(trade["trade_id"]),
                telegram_chat_id=int(chat_id),
                telegram_message_id=getattr(message, "message_id", None),
            )

    async def _install_protection(
        self,
        client: Any,
        trade: dict[str, Any],
        signal: dict[str, Any],
        position: dict[str, Any],
        symbol_info: dict[str, Any],
        *,
        only_missing: bool = False,
    ) -> dict[str, Any]:
        """Create/recover Binance Algo Service exits and verify the live order book."""
        trade_id = int(trade["trade_id"])
        symbol = str(trade["symbol"])
        direction = str(trade["direction"])
        amount = float(position.get("positionAmt", 0) or 0)
        if abs(amount) <= 0 or (amount > 0) != (direction == "LONG"):
            raise RuntimeError("live position is flat or its direction differs from the trade")
        step, tick = self._steps(symbol_info)
        if step <= 0 or tick <= 0:
            raise RuntimeError("Binance quantity or price filter is unavailable")
        quantity = decimal_floor(abs(amount), step)
        entry = float(position.get("entryPrice") or trade.get("entry_price") or 0)
        stop = float(trade.get("stop_price") or signal.get("stop") or 0)
        stop = decimal_floor(stop, tick) if direction == "LONG" else decimal_ceil(stop, tick)
        target_prices = [
            float(signal.get(f"tp{i}") or 0) for i in range(1, 4)
        ]
        target_prices = [
            decimal_ceil(price, tick) if direction == "LONG" else decimal_floor(price, tick)
            for price in target_prices
        ]
        levels_ok = (
            entry > 0
            and direction == "LONG"
            and stop < entry < target_prices[0] < target_prices[1] < target_prices[2]
        ) or (
            entry > 0
            and direction == "SHORT"
            and stop > entry > target_prices[0] > target_prices[1] > target_prices[2]
        )
        if not levels_ok or quantity <= 0:
            raise RuntimeError("SL/TP prices or position quantity failed exchange-filter validation")
        settings = self._tp_allocations()
        hits = [bool(trade.get(f"tp{i}_hit")) for i in range(1, 4)]
        remaining_indexes = [index for index in range(3) if not hits[index]]
        if not remaining_indexes:
            raise RuntimeError("all TP stages are recorded as complete")
        remaining_weight = sum(settings[index] for index in remaining_indexes)
        if remaining_weight <= 0:
            raise RuntimeError("remaining TP allocations are invalid")
        quantities = [0.0, 0.0, 0.0]
        for index in remaining_indexes[:-1]:
            quantities[index] = decimal_floor(
                quantity * settings[index] / remaining_weight, step
            )
        last_index = remaining_indexes[-1]
        quantities[last_index] = decimal_floor(
            max(0.0, quantity - sum(quantities)), step
        )
        if any(quantities[index] <= 0 for index in remaining_indexes):
            raise RuntimeError("remaining position is too small for the unfilled TP orders")

        sl_client_id = f"sl{str(trade['setup_hash'])[:30]}"[:36]
        tp_client_ids = [
            f"tp{i}{str(trade['setup_hash'])[:29]}"[:36] for i in range(1, 4)
        ]
        close_side = "SELL" if direction == "LONG" else "BUY"
        current_open = await self._open_algo_orders(client, symbol)

        def by_client(client_id: str) -> dict[str, Any] | None:
            return next(
                (item for item in current_open if item.get("clientAlgoId") == client_id),
                None,
            )

        def by_algo_id(algo_id: Any) -> dict[str, Any] | None:
            return next(
                (item for item in current_open if self._algo_id(item) == str(algo_id)),
                None,
            )

        async def ensure_order(
            client_id: str,
            order_type: str,
            trigger_price: float,
            order_qty: float | None,
            *,
            close_position: bool = False,
            tracked_id: Any = None,
        ) -> str:
            existing = by_algo_id(tracked_id) if tracked_id else None
            existing = existing or by_client(client_id)
            if existing:
                existing_id = self._algo_id(existing)
                existing_type = str(existing.get("orderType") or existing.get("type") or "")
                existing_trigger = float(
                    existing.get("triggerPrice") or existing.get("stopPrice") or 0
                )
                if (
                    not existing_id
                    or existing_type != order_type
                    or abs(existing_trigger - trigger_price) > tick / 2
                    or str(existing.get("side") or "").upper() != close_side
                    or str(existing.get("workingType") or "").upper() != "MARK_PRICE"
                ):
                    raise RuntimeError(f"existing {order_type} protection does not match expected parameters")
                if close_position and str(existing.get("closePosition", "")).lower() != "true":
                    raise RuntimeError("existing stop is not configured to close the position")
                if order_qty is not None:
                    if str(existing.get("reduceOnly", "")).lower() != "true":
                        raise RuntimeError("existing take-profit is not reduce-only")
                    existing_qty = float(existing.get("quantity") or 0)
                    if abs(existing_qty - order_qty) > step / 2:
                        await client.futures_cancel_algo_order(
                            symbol=symbol, algoId=existing_id
                        )
                        existing = None
                if existing:
                    return existing_id
            if only_missing:
                raise RuntimeError(f"expected {order_type} order is absent from Binance open algo orders")
            method = getattr(client, "futures_create_algo_order", None)
            if not callable(method):
                raise RuntimeError("installed python-binance does not expose Futures Algo Order methods")
            params: dict[str, Any] = {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": close_side,
                "type": order_type,
                "triggerPrice": trigger_price,
                "workingType": "MARK_PRICE",
                "clientAlgoId": client_id,
            }
            if close_position:
                params["closePosition"] = "true"
            else:
                params["quantity"] = order_qty
                params["reduceOnly"] = "true"
            response = await method(**params)
            new_id = self._algo_id(response)
            if not new_id:
                raise RuntimeError(f"Binance did not return an algoId for {order_type}")
            return new_id

        stop_id = await ensure_order(
            sl_client_id,
            "STOP_MARKET",
            stop,
            None,
            close_position=True,
            tracked_id=trade.get("sl_order_id") or trade.get("stop_order_id"),
        )
        update_trade(
            trade_id,
            stop_order_id=stop_id,
            sl_order_id=stop_id,
            stop_price=stop,
        )
        try:
            prior_tp_ids = json.loads(trade.get("tp_order_ids") or "[]")
        except (TypeError, ValueError):
            prior_tp_ids = []
        tp_ids: list[str | None] = [
            str(prior_tp_ids[index]) if hits[index] and index < len(prior_tp_ids) and prior_tp_ids[index]
            else None
            for index in range(3)
        ]
        active_tp_ids: list[str] = []
        expected_tp_specs: dict[str, tuple[str, float, float, bool]] = {}
        field_updates: dict[str, Any] = {}
        for index, (price, part, client_id) in enumerate(
            zip(target_prices, quantities, tp_client_ids), start=1
        ):
            if hits[index - 1]:
                continue
            order_id = await ensure_order(
                client_id,
                "TAKE_PROFIT_MARKET",
                price,
                part,
                tracked_id=trade.get(f"tp{index}_order_id")
                or (prior_tp_ids[index - 1] if index - 1 < len(prior_tp_ids) else None),
            )
            tp_ids[index - 1] = order_id
            active_tp_ids.append(order_id)
            expected_tp_specs[order_id] = (
                "TAKE_PROFIT_MARKET",
                price,
                part,
                False,
            )
            field_updates[f"tp{index}_order_id"] = order_id
            current_trade = get_trade(trade_id) or trade
            try:
                tracked = json.loads(current_trade.get("exchange_order_ids") or "[]")
            except (TypeError, ValueError):
                tracked = []
            tracked.append(order_id)
            field_updates["exchange_order_ids"] = json.dumps(list(dict.fromkeys(tracked)))
            update_trade(trade_id, **field_updates)
            field_updates = {}

        # Conditional orders are on Binance's Algo Service, separate from the
        # standard futures_get_open_orders response. Verify all four there.
        confirmed = await self._open_algo_orders(client, symbol)
        confirmed_ids = {self._algo_id(item) for item in confirmed}
        expected = [stop_id, *active_tp_ids]
        missing = [value for value in expected if value not in confirmed_ids]
        if missing:
            raise RuntimeError(
                "Binance did not list every SL/TP conditional order as open: "
                + ",".join(missing)
            )
        expected_specs = {
            stop_id: ("STOP_MARKET", stop, None, True),
            **expected_tp_specs,
        }
        for order_id, (expected_type, expected_trigger, expected_qty, is_stop) in expected_specs.items():
            order = next(
                (item for item in confirmed if self._algo_id(item) == order_id),
                None,
            )
            if (
                not order
                or not self._algo_is_open(order)
                or str(order.get("orderType") or order.get("type") or "") != expected_type
                or str(order.get("side") or "").upper() != close_side
                or str(order.get("workingType") or "").upper() != "MARK_PRICE"
                or abs(float(order.get("triggerPrice") or 0) - expected_trigger) > tick / 2
            ):
                raise RuntimeError(f"Binance open-order details failed verification for {expected_type}")
            if is_stop:
                if str(order.get("closePosition", "")).lower() != "true":
                    raise RuntimeError("Binance stop is not a close-position order")
            elif (
                str(order.get("reduceOnly", "")).lower() != "true"
                or abs(float(order.get("quantity") or 0) - float(expected_qty or 0)) > step / 2
            ):
                raise RuntimeError("Binance take-profit quantity/reduceOnly failed verification")
        complete_tp_ids = [str(value) if value else None for value in tp_ids]
        update_trade(
            trade_id,
            sl_order_id=stop_id,
            stop_order_id=stop_id,
            tp_order_ids=json.dumps(complete_tp_ids),
            tp1_order_id=complete_tp_ids[0],
            tp2_order_id=complete_tp_ids[1],
            tp3_order_id=complete_tp_ids[2],
            remaining_quantity=quantity,
            status=(
                "TP2_HIT" if hits[1]
                else "TP1_HIT" if hits[0]
                else "PROTECTED"
            ),
            protection_error=None,
        )
        return {"sl": stop_id, "tps": complete_tp_ids}

    @staticmethod
    def _algo_id(order: dict[str, Any] | None) -> str:
        if not order:
            return ""
        value = order.get("algoId") or order.get("orderId")
        return str(value) if value is not None else ""

    @staticmethod
    def _algo_is_open(order: dict[str, Any] | None) -> bool:
        if not order:
            return False
        return str(order.get("algoStatus") or order.get("status") or "").upper() in {
            "NEW", "WORKING", "ACCEPTED", "PARTIALLY_FILLED",
        }

    async def _open_algo_orders(
        self, client: Any, symbol: str | None
    ) -> list[dict[str, Any]]:
        method = getattr(client, "futures_get_open_algo_orders", None)
        if not callable(method):
            raise RuntimeError("python-binance Futures Algo open-orders API is unavailable")
        response = await method(**({"symbol": symbol} if symbol else {}))
        if isinstance(response, dict):
            response = response.get("orders") or response.get("rows") or []
        if not isinstance(response, list):
            raise RuntimeError("Binance returned an invalid open algo-orders response")
        return [item for item in response if isinstance(item, dict)]

    async def _get_algo_order(
        self, client: Any, symbol: str, algo_id: str
    ) -> dict[str, Any] | None:
        method = getattr(client, "futures_get_algo_order", None)
        if not callable(method):
            return None
        try:
            result = await method(symbol=symbol, algoId=algo_id)
            return result if isinstance(result, dict) else None
        except Exception as exc:
            logger.info(
                "Algo order query unresolved symbol=%s algo_id=%s error=%s",
                symbol, algo_id, safe_error_name(exc),
            )
            return None

    @staticmethod
    def _tp_allocations() -> tuple[float, float, float]:
        try:
            first_raw = get_setting("tp1_close_percent", "40")
            second_raw = get_setting("tp2_close_percent", "30")
            first = min(
                95.0,
                max(0.0, float("40" if first_raw is None else first_raw)),
            )
            second = min(
                95.0,
                max(0.0, float("30" if second_raw is None else second_raw)),
            )
        except (TypeError, ValueError):
            first, second = 40.0, 30.0
        if first + second > 95:
            scale = 95 / (first + second)
            first, second = first * scale, second * scale
        return first / 100, second / 100, (100 - first - second) / 100

    async def _emergency_close(
        self,
        client: Any,
        symbol: str,
        side: str,
        quantity: float,
        setup_hash: str,
    ) -> bool:
        try:
            order = await client.futures_create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=quantity,
                reduceOnly="true",
                newClientOrderId=f"em{setup_hash[:24]}",
            )
            order_id = order.get("orderId")
            if not order_id:
                return False
            checked = await client.futures_get_order(symbol=symbol, orderId=order_id)
            return checked.get("status") in {"FILLED", "PARTIALLY_FILLED"}
        except Exception as exc:
            logger.critical(
                "Emergency close unconfirmed symbol=%s error=%s",
                symbol,
                safe_error_name(exc),
            )
            return False

    def _preflight(self, user_id: int) -> str | None:
        if not is_premium(user_id):
            return "SUBSCRIPTION_INACTIVE"
        if get_setting("emergency_stop", "0") == "1":
            return "EMERGENCY_STOP_ACTIVE"
        if get_setting("global_auto_enabled", "1") != "1":
            return "AUTO_TRADE_DISABLED"
        preference = get_preference(user_id)
        if not preference["auto_trading"]:
            return "AUTO_TRADE_DISABLED"
        return None

    async def _reject(
        self,
        user_id: int,
        signal_id: int,
        reason: str,
        status: str = "REJECTED",
    ) -> tuple[bool, str]:
        signal = get_signal(signal_id) or {}
        update_signal_status(signal_id, status, reason)
        record_trade_rejection(user_id, signal_id, reason)
        timestamp = datetime.now(timezone.utc).isoformat()
        log_event(
            "trade.rejected",
            user_id,
            signal_id=signal_id,
            symbol=signal.get("symbol", "unknown"),
            timestamp=timestamp,
            created_at=signal.get("created_at"),
            reason=reason,
        )
        await self._notify(user_id, f"Trade skipped: {reason}")
        return False, reason

    async def _notify(self, chat_id: int, text: str) -> Any | None:
        try:
            return await self.application.bot.send_message(
                chat_id=chat_id, text=text[:3900], disable_web_page_preview=True
            )
        except Exception:
            logger.info("Telegram notification failed chat_id=%s", chat_id)
            return None

    async def _alert_admin(self, text: str) -> None:
        if ADMIN_CHAT_ID is None:
            return
        try:
            await self.application.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text[:3900])
        except Exception:
            logger.error("Critical admin alert delivery failed")

    @staticmethod
    def _steps(symbol_info: dict[str, Any]) -> tuple[float, float]:
        step = tick = 0.0
        market_step = 0.0
        for item in symbol_info.get("filters", []):
            if item.get("filterType") == "LOT_SIZE":
                step = float(item.get("stepSize", 0) or 0)
            elif item.get("filterType") == "MARKET_LOT_SIZE":
                market_step = float(item.get("stepSize", 0) or 0)
            elif item.get("filterType") == "PRICE_FILTER":
                tick = float(item.get("tickSize", 0) or 0)
        return market_step or step, tick

    @staticmethod
    def _format_signal(signal: dict[str, Any], auto: bool) -> str:
        direction = str(signal.get("direction", "LONG")).upper()
        long_side = direction == "LONG"
        icon = "🟢" if long_side else "🔴"
        entry = float(signal.get("entry") or 0)
        low = float(signal.get("entry_low") or entry * 0.997)
        high = float(signal.get("entry_high") or entry * 1.003)
        stop = float(signal.get("stop") or 0)
        risk = abs(entry - stop)
        ratios = [
            abs(float(signal.get(f"tp{index}") or entry) - entry) / risk
            if risk > 0 else 0
            for index in range(1, 4)
        ]
        created_at = signal.get("created_at")
        age = "00:00"
        try:
            created = datetime.fromisoformat(str(created_at))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            minutes = max(0, int((datetime.now(timezone.utc) - created).total_seconds() // 60))
            age = f"{minutes // 60:02d}:{minutes % 60:02d}"
        except (TypeError, ValueError):
            pass
        structure = str(signal.get("structure") or signal.get("reason") or "SMC structure confirmed")
        confidence = float(signal.get("confidence") or 0)
        timeframe = str(signal.get("timeframe") or "4H / 1H / 5m")
        status = "CHECKING..." if auto else "OFF — REVIEW ONLY"
        return (
            "🚀 SMC TRADE SIGNAL\n━━━━━━━━━━━━━━━━\n"
            f"{icon} {direction} | {signal.get('symbol', 'UNKNOWN')}\n"
            "📊 Binance Futures\n\n"
            f"💰 ENTRY\n{low:g} – {high:g}\n\n"
            f"🛑 STOP LOSS\n{stop:g}\n\n"
            "🎯 TAKE PROFITS\n"
            f"TP1 {float(signal.get('tp1') or 0):g} | {ratios[0]:.1f}R\n"
            f"TP2 {float(signal.get('tp2') or 0):g} | {ratios[1]:.1f}R\n"
            f"TP3 {float(signal.get('tp3') or 0):g} | {ratios[2]:.1f}R\n\n"
            f"📈 STRUCTURE\n{timeframe}: {structure[:500]}\n\n"
            f"🔥 Confidence score: {confidence:.0f}% (strategy confluence, not win probability)\n"
            f"⚖️ RR: {ratios[0]:.1f}R / {ratios[1]:.1f}R / {ratios[2]:.1f}R\n"
            f"⏱ Signal age: {age}\n\n"
            f"🤖 AUTO-EXECUTION: {status}"
        )

