"""Telegram subscription gate, premium user interface, and payment intake."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from admin_panel import admin_keyboard, handle_admin_callback, handle_admin_message, is_admin, show_admin
from binance_client import CredentialVault, FuturesSession, verify_credentials
from config import ADMIN_CHAT_ID, DEFAULT_SETTINGS
from database import (
    create_payment,
    delete_credentials,
    active_signal_count,
    active_signals,
    active_trades,
    get_payment_status,
    get_plans,
    get_preference,
    get_setting,
    get_signal,
    get_trade,
    has_pending_payment,
    is_premium,
    latest_scan_summary,
    log_event,
    recent_signals,
    recent_trades,
    user_performance,
    user_recent_events,
    set_preference,
    subscription_info,
    upsert_user,
)
from logger import logger, safe_error_name


def is_private_admin(update: Update) -> bool:
    return is_admin(update)


def _buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )


def _nav(page: str, back: str = "dash") -> list[list[tuple[str, str]]]:
    return [[("⬅️ Back", back), ("🏠 Home", "home"), ("🔄 Refresh", page)]]


def welcome_keyboard() -> InlineKeyboardMarkup:
    return _buttons(
        [
            [("💎 View Plans", "plans"), ("💳 Payment Instructions", "pay:instructions")],
            [("📤 Submit Payment", "pay:submit"), ("⏳ Payment Status", "pay:status")],
            [("🆘 Help", "help")],
        ]
    )


def premium_keyboard() -> InlineKeyboardMarkup:
    return _buttons(
        [
            [("📊 Dashboard", "premium:dash"), ("📈 Active Signals", "premium:signals")],
            [("💹 Open Positions", "premium:positions"), ("📜 Trade History", "premium:history")],
            [("📊 Performance", "premium:performance"), ("🛠 Trade Management", "premium:management")],
            [("⚙️ Settings", "premium:settings"), ("📋 Logs", "premium:logs")],
            [("🔍 Live Market Scanner", "premium:scanner"), ("⚡ Auto Trading", "premium:auto")],
            [("💰 Wallet & PnL", "premium:wallet"), ("🧠 SMC Strategy", "premium:strategy")],
            [("🛡️ Risk Management", "premium:risk"), ("🔑 Binance API", "premium:api")],
            [("🧪 Testnet / 🔴 Real", "premium:environment"), ("💎 My Subscription", "premium:subscription")],
            [("🆘 Support", "premium:support")],
        ]
    )


def _welcome_text(first_name: str) -> str:
    return (
        f"👋 Welcome{', ' + first_name[:40] if first_name else ''} to Binance Auto Trader PRO\n\n"
        "A premium research and execution service for Binance USDT-M Futures.\n\n"
        "Choose a plan to unlock the private scanner, SMC signals, risk tools, and "
        "optional automated execution. Payments are reviewed manually by an admin. "
        "Access stays locked while a payment is pending."
    )


def _plan_text() -> str:
    plans = get_plans()
    available = [plan for plan in plans if float(plan["price"]) > 0]
    if not available:
        return (
            "💎 SUBSCRIPTION PLANS\n\n"
            "Daily · Weekly · Monthly · Lifetime\n\n"
            "Plan prices are being configured by the admin. Check again later or contact support."
        )
    lines = ["💎 SUBSCRIPTION PLANS\n"]
    for plan in available:
        duration = "Lifetime" if not int(plan["duration_days"]) else f"{plan['duration_days']} days"
        lines.append(f"• {plan['label']} — {duration} — {float(plan['price']):g} {plan['currency']}")
    lines.append("\nPremium access starts only after an admin approves the payment.")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.effective_message:
        return
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("Open a private chat with this bot to continue.")
        return
    upsert_user(user.id, user.username, user.first_name or "")
    if is_premium(user.id):
        await update.effective_message.reply_text(
            "🚀 BINANCE AUTO TRADER PRO\nPremium access is active. Choose a workspace:",
            reply_markup=premium_keyboard(),
        )
    else:
        await update.effective_message.reply_text(
            _welcome_text(user.first_name or ""),
            reply_markup=welcome_keyboard(),
        )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_admin(update):
        return
    await show_admin(update, context)


async def admin_action_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_admin(update) or not update.effective_message:
        return
    command = update.effective_message.text.split()[0].split("@")[0].lower()
    args = context.args
    admin_id = update.effective_user.id if update.effective_user else 0
    try:
        if command == "/grant" and len(args) in {2, 3}:
            target = int(args[0])
            plan = args[1].lower()
            days = int(args[2]) if len(args) == 3 else None
            from database import grant_subscription

            ok, result = grant_subscription(target, plan, admin_id, days_override=days)
            await update.effective_message.reply_text(
                f"Subscription {'granted' if ok else 'not changed'}: {result}"
            )
        elif command in {"/suspend", "/revoke"} and len(args) == 1:
            target = int(args[0])
            state = "suspended" if command == "/suspend" else "revoked"
            from database import set_user_status

            ok = set_user_status(target, state, admin_id)
            await update.effective_message.reply_text(
                "User access updated." if ok else "User not found."
            )
        elif command == "/extend" and len(args) == 2:
            target, days = int(args[0]), int(args[1])
            from database import extend_subscription

            ok, result = extend_subscription(target, days, admin_id)
            await update.effective_message.reply_text(
                f"Subscription {'extended to ' + result if ok else 'not changed: ' + result}"
            )
        else:
            await update.effective_message.reply_text(
                "Admin commands:\n/grant <user_id> <daily|weekly|monthly|lifetime> [days]\n"
                "/suspend <user_id>\n/revoke <user_id>\n/extend <user_id> <days>",
                reply_markup=admin_keyboard(),
            )
    except (ValueError, TypeError):
        await update.effective_message.reply_text("Invalid command values.")


async def route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        await query.answer("Open a private chat with this bot to continue.", show_alert=True)
        return
    data = query.data or ""
    if is_private_admin(update) and data.startswith("admin:"):
        await handle_admin_callback(update, context, data)
        return
    if data.startswith("admin:"):
        await query.answer("Not authorized.", show_alert=True)
        return
    if data.startswith("trade:"):
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        expected_length = 3
        if len(parts) != expected_length:
            await query.answer("Invalid trade action.", show_alert=True)
            return
        try:
            trade_id = int(parts[-1])
        except ValueError:
            await query.answer("Invalid trade.", show_alert=True)
            return
        trade = get_trade(trade_id)
        admin = is_private_admin(update)
        if not trade or (
            int(trade["user_id"]) != int(user.id) and not admin
        ):
            await query.answer("Not authorized for this trade.", show_alert=True)
            return
        engine = context.application.bot_data.get("trade_engine")
        if not engine:
            await query.answer("Trade manager is unavailable.", show_alert=True)
            return
        owner_id = None if admin else int(user.id)
        if action == "close":
            await query.answer()
            await _render(
                update,
                context,
                f"⚠️ CLOSE POSITION?\n\n{trade['direction']} {trade['symbol']} · "
                f"Trade #{trade_id}\nThe bot will fetch the fresh Binance position, "
                "submit a reduce-only close, verify quantity is zero, then cancel this "
                "trade's remaining protective orders.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ YES, CLOSE",
                                callback_data=f"trade:confirm-close:{trade_id}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "❌ CANCEL",
                                callback_data=f"trade:cancel:{trade_id}",
                            )
                        ],
                    ]
                ),
            )
            return
        if action == "confirm-close":
            ok, message = await engine.close_trade(
                trade_id, owner_id=owner_id, actor_id=int(user.id)
            )
        elif action == "cancel":
            await query.answer("Close cancelled.")
            await engine.refresh_trade(trade_id, owner_id=owner_id)
            return
        elif action in {"tp1", "tp2", "tp3"}:
            ok, message = await engine.execute_tp_stage(
                trade_id, owner_id=owner_id, stage=int(action[-1])
            )
        elif action == "be":
            ok, message = await engine.move_stop_to_breakeven(
                trade_id, owner_id=owner_id
            )
        elif action == "refresh":
            ok, message = True, "Fresh Binance data fetched."
        else:
            await query.answer("Unknown trade action.", show_alert=True)
            return
        latest = get_trade(trade_id) or trade
        await query.answer(
            f"{'✅' if ok else '⚠️'} {message}"[:190],
            show_alert=not ok,
        )
        if action != "refresh" and latest["status"] in {
            "SUBMITTING", "OPEN", "PROTECTED", "TP1_HIT", "TP2_HIT",
            "PROTECTION_ERROR", "PROTECTION_FAILED", "UNKNOWN",
        }:
            await engine.refresh_trade(trade_id, owner_id=owner_id)
        return

    public_actions = {
        "home",
        "plans",
        "pay:instructions",
        "pay:submit",
        "pay:status",
        "help",
    }
    if data in public_actions or data.startswith("pay:"):
        upsert_user(user.id, user.username, user.first_name or "")
        if data == "home":
            if is_premium(user.id):
                await _render(
                    update,
                    context,
                    "🚀 BINANCE AUTO TRADER PRO\nPremium access is active. Choose a workspace:",
                    premium_keyboard(),
                )
            else:
                await _render(update, context, _welcome_text(user.first_name or ""), welcome_keyboard())
        elif data == "plans":
            plans = [p for p in get_plans() if float(p["price"]) > 0]
            rows = [
                [(f"{p['label']} · {float(p['price']):g} {p['currency']}", f"pay:plan:{p['plan_key']}")]
                for p in plans
            ]
            rows.extend(_nav("plans", "home"))
            await _render(update, context, _plan_text(), _buttons(rows))
        elif data == "pay:instructions":
            rows = _nav("pay:instructions", "home")
            rows.insert(0, [("💎 Choose a plan", "plans"), ("📤 Submit payment", "pay:submit")])
            await _render(
                update,
                context,
                "💳 PAYMENT INSTRUCTIONS\n\n"
                f"{get_setting('payment_instructions', DEFAULT_SETTINGS['payment_instructions'])}\n\n"
                "Only submit a payment after selecting an enabled plan. Access is activated manually.",
                _buttons(rows),
            )
        elif data == "pay:submit":
            if has_pending_payment(user.id):
                await _render(
                    update,
                    context,
                    "⏳ Your payment is already pending manual review. Premium access remains locked until a decision.",
                    _buttons(_nav("pay:status", "home")),
                )
            else:
                plans = [p for p in get_plans() if float(p["price"]) > 0]
                rows = [
                    [(f"{p['label']} · {float(p['price']):g} {p['currency']}", f"pay:plan:{p['plan_key']}")]
                    for p in plans
                ]
                rows.extend(_nav("pay:submit", "home"))
                await _render(
                    update,
                    context,
                    "📤 SUBMIT PAYMENT\n\nSelect the plan you paid for. The bot will ask for a transaction reference and proof. An admin will review both.",
                    _buttons(rows),
                )
        elif data == "pay:status":
            statuses = get_payment_status(user.id)
            lines = ["⏳ PAYMENT STATUS\n"]
            if not statuses:
                lines.append("No payment request found.")
            for row in statuses:
                lines.append(
                    f"• {row['plan_key'].title()} · {row['amount']:g} USDT · {row['status']} · "
                    f"{str(row['created_at'])[:10]}"
                )
            if has_pending_payment(user.id):
                lines.append("\nYour access remains locked while the request is pending.")
            else:
                lines.append("\nRejected requests may be resubmitted with corrected proof.")
            rows = _nav("pay:status", "home")
            rows.insert(0, [("📤 Submit / resubmit", "pay:submit")])
            await _render(update, context, "\n".join(lines), _buttons(rows))
        elif data == "help":
            await _render(
                update,
                context,
                "🆘 HELP\n\n"
                "Use View Plans to see enabled subscriptions, Payment Instructions to review payment details, "
                "and Submit Payment to send your transaction reference and proof. An admin must approve "
                "the request before premium access is unlocked.\n\n"
                "If a payment is pending, use Payment Status. Contact the bot admin for account support. "
                "Futures trading is risky; signals and past results do not guarantee returns.",
                _buttons(_nav("help", "home")),
            )
        elif data.startswith("pay:plan:"):
            key = data.split(":")[-1]
            plan = next(
                (p for p in get_plans() if p["plan_key"] == key and float(p["price"]) > 0),
                None,
            )
            if not plan:
                await query.answer("Plan is unavailable.", show_alert=True)
                return
            instructions = get_setting("payment_instructions", DEFAULT_SETTINGS["payment_instructions"])
            context.user_data["selected_plan"] = key
            await _render(
                update,
                context,
                f"💳 {plan['label']} plan · {float(plan['price']):g} {plan['currency']}\n\n"
                f"{instructions}\n\nAfter paying, continue to submit your transaction reference and proof.",
                _buttons(
                    [
                        [("✅ I paid — submit proof", f"pay:start:{key}")],
                        [("⬅️ Back", "pay:submit"), ("🏠 Home", "home"), ("🔄 Refresh", f"pay:plan:{key}")],
                    ]
                ),
            )
        elif data.startswith("pay:start:"):
            key = data.split(":")[-1]
            if has_pending_payment(user.id):
                await _render(update, context, "A payment is already pending manual review.", _buttons(_nav("pay:status", "home")))
                return
            if not any(p["plan_key"] == key and float(p["price"]) > 0 for p in get_plans()):
                await _render(update, context, "This plan is unavailable.", _buttons(_nav("plans", "home")))
                return
            context.user_data["selected_plan"] = key
            context.user_data["user_state"] = "payment_reference"
            await _render(
                update,
                context,
                "📤 Send the transaction reference as a message. Do not send API credentials here.",
                _buttons(_nav("pay:submit", "pay:submit")),
            )
        return

    # Every premium callback is independently gated; callback data alone never
    # grants access. A pending renewal/payment deliberately locks an existing sub.
    if not is_premium(user.id):
        await query.answer("Premium access is required. Please use the subscription options.", show_alert=True)
        await _render(update, context, _welcome_text(user.first_name or ""), welcome_keyboard())
        return
    upsert_user(user.id, user.username, user.first_name or "")
    if not data.startswith("premium:"):
        await query.answer("That action is unavailable.", show_alert=True)
        return
    await query.answer()
    page = data.removeprefix("premium:")
    if page == "dash":
        summary = latest_scan_summary()
        last_signal = recent_signals(1)
        performance = user_performance(user.id)
        exchange = await _dashboard_exchange_snapshot(
            user.id, context, last_signal[0] if last_signal else None
        )
        scanner = context.application.bot_data.get("scanner")
        tasks = context.application.bot_data.get("worker_tasks", {})
        scanner_task = tasks.get("market-scanner")
        monitor_task = tasks.get("position-monitor")
        worker_running = bool(
            scanner_task and not scanner_task.done()
            and monitor_task and not monitor_task.done()
        )
        scanner_running = bool(scanner and getattr(scanner, "is_running", False))
        latest = last_signal[0] if last_signal else None
        last_signal_text = (
            f"{latest['symbol']} {latest['direction']} · {latest['created_at']}"
            if latest else "None recorded"
        )
        last_trade = next(
            (
                event for event in user_recent_events(user.id, 30)
                if event["event_type"] in {"trade.rejected", "trade.opened", "trade.closed",
                                           "trade.complete", "trade.stopped"}
            ),
            None,
        )
        last_order_text = (
            f"{last_trade['event_type']} · {last_trade['created_at']}"
            if last_trade else "No order attempt recorded"
        )
        realized = performance.get("realized_pnl")
        win_rate_text = (
            f"{performance['win_rate']:.1f}%"
            if performance.get("win_rate") is not None else "N/A"
        )
        realized_text = (
            f"{float(realized or 0):+,.4f} USDT (verified Binance fills)"
            if int(performance.get("verified_trades") or 0)
            else "No verified closed-trade PnL"
        )
        scan_text = summary.get("scanned_at") or "No recent scan recorded"
        await _render(
            update,
            context,
            "╔══ BINANCE FUTURES · OVERVIEW ══╗\n"
            f"Bot: 🟢 ONLINE  ·  Worker: {'🟢 RUNNING' if worker_running else '🔴 STOPPED'}\n"
            f"Scanner: {'🟢 RUNNING' if scanner_running else '🔴 STOPPED'}  ·  "
            f"WebSocket: ⚪ REST polling (no stream configured)\n"
            f"Binance API: {'🟢 CONNECTED' if exchange['connected'] else '🔴 NOT VERIFIED'}\n\n"
            f"Wallet balance: {exchange['balance']}\n"
            f"Available: {exchange['available']}\n"
            f"Unrealized PnL: {exchange['unrealized']}\n"
            f"Realized PnL: {realized_text}\n"
            f"Win rate: {win_rate_text}\n"
            f"Total trades: {int(performance.get('total_trades') or 0)}\n"
            f"Active signals: {active_signal_count()}\n"
            f"Open positions: {exchange['position_count']}\n\n"
            f"Last market scan: {scan_text}\n"
            f"Last signal: {last_signal_text}\n"
            f"Last live price fetch: {exchange['last_price']}\n"
            f"Last balance fetch: {exchange['last_balance']}\n"
            f"Last order attempt: {last_order_text}\n"
            "╚══════════════════════════════╝\n"
            "Exchange figures are live when API status is connected; realized PnL uses verified fills.",
            premium_keyboard(),
        )
    elif page == "scanner":
        summary = latest_scan_summary()
        await _render(
            update,
            context,
            "🔍 LIVE MARKET SCANNER\n\n"
            f"State: {'Running' if get_setting('scanner_enabled', '1') == '1' else 'Paused by admin'}\n"
            f"Latest results: {summary['count']} · signals {summary['signals']} · errors {summary['errors']}\n"
            f"Last result time: {summary.get('scanned_at') or 'No scan recorded'}\n"
            f"Cycle target: {get_setting('scan_interval_seconds', '60')} seconds\n"
            f"Universe: active USDT-M perpetuals above {get_setting('min_quote_volume_usdt', '10000000')} USDT volume.\n\n"
            "Only completed candles are used. Some cycles take longer to respect the exchange request limit.",
            _buttons(_nav(data, "premium:dash")),
        )
    elif page == "signals":
        signals = active_signals(10)
        lines = ["📈 ACTIVE SMC SIGNALS\n"]
        for signal in signals:
            zone = signal.get("entry_zone") or {}
            low = zone.get("low", signal.get("entry_low"))
            high = zone.get("high", signal.get("entry_high"))
            lines.append(
                f"• {signal['symbol']} {signal.get('side') or signal['direction']} · {signal['status']}\n"
                f"  Entry zone {float(low or 0):g}–{float(high or 0):g} · SL {float(signal['stop'] or 0):g} · "
                f"TP1 {float(signal['tp1'] or 0):g}\n"
                f"  Created {signal['created_at']} · expires {signal.get('expires_at') or 'not set'}\n"
                f"  {signal['reason'][:200]}"
            )
        if not signals:
            lines.append("No unexpired signals. The bot does not create setups to increase activity.")
        await _render(update, context, "\n".join(lines), _buttons(_nav(data, "premium:dash")))
    elif page == "auto":
        preference = get_preference(user.id)
        environment = preference["environment"]
        auto = bool(preference["auto_trading"])
        real_admin = get_setting("real_trading_enabled", "0") == "1"
        text = (
            "⚡ AUTO TRADING\n\n"
            f"User auto-trading: {'ON' if auto else 'OFF'}\n"
            f"Environment: {environment.upper()}\n"
            f"Admin global auto permission: {'ON' if get_setting('global_auto_enabled', '1') == '1' else 'OFF'}\n"
            f"Emergency stop: {'ON' if get_setting('emergency_stop', '0') == '1' else 'OFF'}\n"
            f"Real trading permission: {'ON' if real_admin else 'OFF'}\n\n"
            "Automatic entry is disabled until you enable it. Every setup is rechecked for subscription, "
            "risk, margin, filters, price drift, duplicate orders, and a protective stop."
        )
        label = "⏸ Disable auto-trading" if auto else "▶️ Enable auto-trading"
        await _render(
            update,
            context,
            text,
            _buttons(
                [
                    [(label, "premium:auto:toggle")],
                    [("🧪 Testnet / 🔴 Real", "premium:environment")],
                    *_nav(data, "premium:dash"),
                ]
            ),
        )
    elif page == "auto:toggle":
        preference = get_preference(user.id)
        if preference["auto_trading"]:
            set_preference(user.id, "auto_trading", 0)
            log_event("user.auto_trading", user.id, enabled=False)
            await _render(update, context, "Automatic trading is off for your account.", _buttons(_nav("premium:auto", "premium:dash")))
        else:
            await _enable_auto(update, context)
    elif page == "environment":
        pref = get_preference(user.id)
        rows = [
            [("🧪 Use Testnet", "premium:env:testnet")],
        ]
        if get_setting("real_trading_enabled", "0") == "1":
            rows.append([("🔴 Request Real Trading", "premium:env:real")])
        rows.extend(_nav(data, "premium:dash"))
        await _render(
            update,
            context,
            f"🧪 EXECUTION ENVIRONMENT\n\nCurrently selected: {pref['environment'].upper()}.\n\n"
            "Testnet is the default. Real-money execution is unavailable unless an admin enables it "
            "and you complete both confirmation steps. API keys are kept separate by environment.",
            _buttons(rows),
        )
    elif page == "env:testnet":
        set_preference(user.id, "environment", "testnet")
        set_preference(user.id, "auto_trading", 0)
        await _render(
            update,
            context,
            "🧪 Testnet selected. Automatic trading is off after an environment change; review your settings before enabling it.",
            _buttons(_nav("premium:environment", "premium:dash")),
        )
    elif page == "env:real":
        if get_setting("real_trading_enabled", "0") != "1":
            await _render(update, context, "Real trading has not been enabled by the admin.", _buttons(_nav("premium:environment", "premium:dash")))
            return
        vault: CredentialVault = context.application.bot_data["vault"]
        try:
            credentials = vault.load_for_user(user.id, "real")
        except Exception:
            credentials = None
        if not credentials:
            await _render(
                update,
                context,
                "A verified Real Binance API key is required before the real-money confirmation flow. "
                "Add it with withdrawals disabled; it is stored separately from Testnet credentials.",
                _buttons(
                    [
                        [("🔴 Add Real API key", "premium:api:env:real")],
                        *_nav("premium:environment", "premium:dash"),
                    ]
                ),
            )
            return
        verified, _verification_message = await verify_credentials(
            credentials[0], credentials[1], "real"
        )
        if not verified:
            await _render(
                update,
                context,
                "The Real API connection could not be verified. No environment change was made.",
                _buttons(_nav("premium:api", "premium:dash")),
            )
            return
        context.user_data["pending_real_confirmation"] = True
        await _render(
            update,
            context,
            "⚠️ REAL-MONEY TRADING WARNING\n\n"
            "Futures trading can lose money rapidly, including your entire margin. "
            "This bot does not guarantee outcomes. Use a key without withdrawal permission. "
            "Real mode uses a different Binance endpoint and separate API credentials.\n\n"
            "To continue, you must confirm again on the next screen.",
            _buttons(
                [
                    [("Continue to second confirmation", "premium:env:real:confirm1")],
                    [("Cancel", "premium:environment")],
                ]
            ),
        )
    elif page == "env:real:confirm1":
        if not context.user_data.pop("pending_real_confirmation", False):
            await query.answer("Start the real-trading confirmation again.", show_alert=True)
            return
        if get_setting("real_trading_enabled", "0") != "1":
            await _render(update, context, "Admin real-trading permission is off.", _buttons(_nav("premium:environment", "premium:dash")))
            return
        context.user_data["pending_real_confirmation_stage2"] = True
        await _render(
            update,
            context,
            "🔴 SECOND CONFIRMATION\n\nYou are requesting real Binance Futures execution with real funds. Confirming selects Real mode, turns auto-trading OFF, and still requires a separately verified Real API key.",
            _buttons(
                [
                    [("🔴 I accept — select Real", "premium:env:real:confirm2")],
                    [("Cancel", "premium:environment")],
                ]
            ),
        )
    elif page == "env:real:confirm2":
        if not context.user_data.pop("pending_real_confirmation_stage2", False):
            await query.answer("Complete the first confirmation screen first.", show_alert=True)
            return
        if get_setting("real_trading_enabled", "0") != "1":
            await _render(update, context, "Admin real-trading permission is off.", _buttons(_nav("premium:environment", "premium:dash")))
            return
        set_preference(user.id, "environment", "real")
        set_preference(user.id, "auto_trading", 0)
        log_event("user.real_environment_selected", user.id)
        await _render(
            update,
            context,
            "🔴 Real mode selected after both confirmations. Auto-trading remains off. Add and verify a separate Real API key before enabling it.",
            _buttons(_nav("premium:environment", "premium:dash")),
        )
    elif page == "positions":
        await _show_positions(update, context)
    elif page == "history":
        trades = recent_trades(user.id, 15)
        if not trades:
            text = "📜 TRADE HISTORY\n\nNo bot trades have been recorded."
        else:
            lines = ["📜 TRADE HISTORY\n"]
            for trade in trades:
                pnl = (
                    f"{float(trade['realized_pnl']):+,.4f} USDT"
                    if trade.get("pnl_verified")
                    else "Unverified"
                )
                lines.append(
                    f"• #{trade['trade_id']} · Signal {trade['signal_id']} · "
                    f"{trade['symbol']} {trade.get('side') or trade['direction']} · "
                    f"{trade['environment'].upper()} · "
                    f"{trade['status']} · PnL {pnl}\n"
                    f"  Entry {float(trade.get('entry_price') or 0):g} · "
                    f"Exit {float(trade.get('exit_price') or 0):g} · "
                    f"Qty {float(trade.get('quantity') or 0):g}\n"
                    f"  SL {float(trade.get('stop_price') or trade.get('signal_stop') or 0):g} · "
                    f"TP {float(trade.get('tp1') or 0):g} / {float(trade.get('tp2') or 0):g} / "
                    f"{float(trade.get('tp3') or 0):g}\n"
                    f"  Reason: {trade.get('close_reason') or trade.get('reason') or trade.get('signal_reason') or '—'}\n"
                    f"  Created: {trade.get('created_at') or '—'} · "
                    f"Executed: {trade.get('executed_at') or '—'} · "
                    f"Closed: {trade.get('closed_at') or '—'}"
                )
            text = "\n".join(lines)
        await _render(update, context, text, _buttons(_nav(data, "premium:dash")))
    elif page == "performance":
        stats = user_performance(user.id)
        win_rate = stats.get("win_rate")
        realized = float(stats.get("realized_pnl") or 0)
        await _render(
            update,
            context,
            "📊 PERFORMANCE · VERIFIED DATA\n\n"
            f"Trades recorded: {int(stats.get('total_trades') or 0)}\n"
            f"Closed: {int(stats.get('closed_trades') or 0)}\n"
            f"PnL-verified trades: {int(stats.get('verified_trades') or 0)}\n"
            f"Win rate (verified only): {f'{win_rate:.1f}%' if win_rate is not None else 'N/A'}\n"
            f"Realized PnL (verified Binance fills): {realized:+,.4f} USDT\n\n"
            "Unverified exchange outcomes are excluded from PnL and win-rate totals.",
            _buttons(_nav(data, "premium:dash")),
        )
    elif page == "management":
        preference = get_preference(user.id)
        await _render(
            update,
            context,
            "🛠 TRADE MANAGEMENT\n\n"
            f"AUTO_TRADE: {'ON' if preference['auto_trading'] else 'OFF'} (per-user opt-in)\n"
            f"RISK_PERCENT: {float(preference['risk_pct']):g}% · admin cap "
            f"{get_setting('risk_per_trade_pct', '0.5')}%\n"
            f"LEVERAGE: max {get_setting('max_leverage', '3')}x\n"
            f"MAX_SIGNAL_AGE_MINUTES: {get_setting('max_signal_age_minutes', '90')}\n"
            f"TP1_CLOSE_PERCENT: {get_setting('tp1_close_percent', '40')}%\n"
            f"TP2_CLOSE_PERCENT: {get_setting('tp2_close_percent', '30')}%\n"
            f"MOVE_SL_TO_BREAKEVEN: {'ON' if get_setting('move_sl_to_breakeven', '1') == '1' else 'OFF'}\n"
            f"TRAILING_STOP_ENABLED: {'ON' if get_setting('trailing_stop_enabled', '1') == '1' and preference['trailing_stop'] else 'OFF'}\n"
            f"TRAILING_STOP_PERCENT: {get_setting('trailing_stop_percent', '1')}%\n"
            f"MAX_OPEN_POSITIONS: {get_setting('max_open_positions', '3')}\n\n"
            "Global execution and risk controls are admin-managed. Your trailing-stop opt-in is available in Settings.",
            _buttons(_nav(data, "premium:dash")),
        )
    elif page == "logs":
        events = user_recent_events(user.id, 12)
        if not events:
            text = "📋 RECENT LOGS\n\nNo user-specific trading events recorded."
        else:
            lines = ["📋 RECENT LOGS\n"]
            for event in events:
                lines.append(
                    f"• {event['created_at']} · {event['event_type']}\n"
                    f"  {event['details'][:240]}"
                )
            text = "\n".join(lines)
        await _render(update, context, text, _buttons(_nav(data, "premium:dash")))
    elif page == "wallet":
        await _show_wallet(update, context)
    elif page == "strategy":
        await _render(
            update,
            context,
            "🧠 SMC STRATEGY\n\n"
            "4H higher-high/higher-low or lower-high/lower-low bias → 1H structure and liquidity → "
            "reclaimed liquidity sweep → displacement BOS → completed 15M/5M order-block or FVG retest → "
            "risk validation.\n\n"
            "A wick or an isolated zone does not create an entry. Setups expire if the structure, "
            "retest, stop, or signal price becomes invalid.",
            _buttons(_nav(data, "premium:dash")),
        )
    elif page == "risk":
        pref = get_preference(user.id)
        await _render(
            update,
            context,
            "🛡️ RISK MANAGEMENT\n\n"
            f"Your risk per trade: {float(pref['risk_pct']):g}% (admin cap "
            f"{get_setting('risk_per_trade_pct', '0.5')}%)\n"
            f"Maximum leverage: {get_setting('max_leverage', '3')}x\n"
            f"Maximum concurrent positions: {get_setting('max_open_positions', '3')}\n"
            f"Minimum reward/risk: {get_setting('min_reward_risk', '1.5')}:1\n"
            f"Daily loss limit: {get_setting('daily_loss_limit_pct', '2.0')}%\n\n"
            "Quantity is sized from stop distance, fees, available margin, and exchange filters. "
            "The bot will skip a trade if minimum quantity would exceed your risk limit.",
            _buttons(
                [
                    [("Risk 0.1%", "premium:risk:0.1"), ("Risk 0.25%", "premium:risk:0.25"), ("Risk 0.5%", "premium:risk:0.5")],
                    *_nav(data, "premium:dash"),
                ]
            ),
        )
    elif page.startswith("risk:"):
        try:
            requested = float(page.split(":", 1)[1])
            cap = float(get_setting("risk_per_trade_pct", "0.5") or 0.5)
            set_preference(user.id, "risk_pct", min(requested, cap))
            await _render(update, context, f"Risk preference saved at {min(requested, cap):g}%.", _buttons(_nav("premium:risk", "premium:dash")))
        except (ValueError, TypeError):
            await _render(update, context, "Invalid risk preference.", _buttons(_nav("premium:risk", "premium:dash")))
    elif page == "api":
        await _show_api_menu(update, context)
    elif page.startswith("api:"):
        await _api_action(update, context, page)
    elif page == "subscription":
        sub = subscription_info(user.id)
        expiry = "Lifetime" if sub and not sub.get("expires_at") else (str(sub.get("expires_at"))[:10] if sub else "Not active")
        plan_name = sub.get("label") if sub else "None"
        await _render(
            update,
            context,
            f"💎 MY SUBSCRIPTION\n\nPlan: {plan_name}\nStatus: {'Active' if is_premium(user.id) else 'Inactive / payment pending'}\nExpires: {expiry}",
            _buttons(_nav(data, "premium:dash")),
        )
    elif page == "settings":
        pref = get_preference(user.id)
        await _render(
            update,
            context,
            "⚙️ SETTINGS\n\n"
            f"Signal notifications: {'On' if pref['notifications'] else 'Off'}\n"
            f"Trailing stop: {'On' if pref['trailing_stop'] else 'Off'}\n"
            f"Execution environment: {pref['environment'].upper()}",
            _buttons(
                [
                    [("Toggle signal notifications", "premium:settings:notifications")],
                    [("Toggle trailing stop", "premium:settings:trailing")],
                    *_nav(data, "premium:dash"),
                ]
            ),
        )
    elif page == "settings:notifications":
        pref = get_preference(user.id)
        set_preference(user.id, "notifications", 0 if pref["notifications"] else 1)
        await _render(update, context, "Notification setting updated.", _buttons(_nav("premium:settings", "premium:dash")))
    elif page == "settings:trailing":
        pref = get_preference(user.id)
        set_preference(user.id, "trailing_stop", 0 if pref["trailing_stop"] else 1)
        await _render(update, context, "Trailing stop setting updated. It activates only after price reaches 2R.", _buttons(_nav("premium:settings", "premium:dash")))
    elif page == "support":
        context.user_data["user_state"] = "support_message"
        await _render(
            update,
            context,
            "🆘 SUPPORT\n\nSend a message for the admin. Do not include API keys, secrets, passwords, or recovery codes.",
            _buttons(_nav(data, "premium:dash")),
        )
    else:
        await query.answer("That premium page is unavailable.", show_alert=True)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        await message.reply_text("Open a private chat with this bot to continue.")
        return
    if is_private_admin(update) and await handle_admin_message(update, context, message.text or ""):
        return
    state = context.user_data.get("user_state")
    text = (message.text or "").strip()
    if state == "payment_reference":
        upsert_user(user.id, user.username, user.first_name or "")
        if len(text) < 3 or len(text) > 160:
            await message.reply_text("Send a transaction reference between 3 and 160 characters.")
            return
        context.user_data["payment_reference"] = text
        context.user_data["user_state"] = "payment_proof"
        await message.reply_text(
            "Now attach a screenshot or payment document as proof. An admin will verify the transaction manually."
        )
        return
    if not is_premium(user.id):
        await message.reply_text(_welcome_text(user.first_name or ""), reply_markup=welcome_keyboard())
        return
    if state == "payment_proof":
        await message.reply_text("Attach a photo or document of payment proof to continue.")
        return
    if state == "await_api_key":
        await _delete_sensitive_message(message)
        if len(text) < 12 or len(text) > 200:
            await message.reply_text("That key format does not look valid. Try again or cancel in Binance API settings.")
            return
        context.user_data["pending_api_key"] = text
        context.user_data["user_state"] = "await_api_secret"
        await message.reply_text(
            "API key received securely. Now send the matching API secret as a single message. "
            "Your message will be deleted after receipt; it will not be echoed."
        )
        return
    if state == "await_api_secret":
        await _delete_sensitive_message(message)
        api_key = context.user_data.pop("pending_api_key", "")
        api_secret = text
        environment = context.user_data.pop("pending_api_environment", "testnet")
        context.user_data.pop("user_state", None)
        if not api_key or len(api_secret) < 12 or len(api_secret) > 200:
            await message.reply_text("Credentials were not saved. Start again in Binance API settings.")
            return
        await message.reply_text("Verifying the API connection and Futures permissions…")
        ok, result = await verify_credentials(api_key, api_secret, environment)
        if not ok:
            await message.reply_text(result)
            return
        vault: CredentialVault = context.application.bot_data["vault"]
        if not vault.available:
            await message.reply_text("Encrypted credential storage is unavailable. No credentials were saved.")
            return
        try:
            await asyncio.to_thread(
                vault.save_for_user,
                user.id,
                environment,
                api_key,
                api_secret,
            )
            await message.reply_text(
                f"✅ {environment.upper()} Futures API connection verified and encrypted at rest. "
                "Make sure the Binance API key has no withdrawal permission."
            )
        except Exception as exc:
            logger.error("Credential encryption or storage failed error=%s", safe_error_name(exc))
            await message.reply_text("Credentials were not saved because encryption failed.")
        return
    if state == "support_message":
        context.user_data.pop("user_state", None)
        await _send_support(update, context, text)
        return
    await message.reply_text(
        "Use the buttons below to navigate. Your premium workspace is available with /start.",
        reply_markup=premium_keyboard(),
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        await message.reply_text("Open a private chat with this bot to continue.")
        return
    if context.user_data.get("user_state") != "payment_proof":
        if not is_premium(user.id):
            await message.reply_text(_welcome_text(user.first_name or ""), reply_markup=welcome_keyboard())
            return
        await message.reply_text("Use the premium menu to choose an action.", reply_markup=premium_keyboard())
        return
    reference = str(context.user_data.get("payment_reference", "")).strip()
    plan_key = str(context.user_data.get("selected_plan", ""))
    proof_message_id = message.message_id
    try:
        payment_id = await asyncio.to_thread(
            create_payment,
            user.id,
            plan_key,
            reference,
            message.chat_id,
            proof_message_id,
        )
    except sqlite3.IntegrityError:
        await message.reply_text("That transaction reference has already been submitted. Contact the admin if this is an error.")
        return
    except ValueError as exc:
        await message.reply_text(
            "A payment is already pending, or the selected plan is no longer available."
        )
        return
    context.user_data.pop("user_state", None)
    context.user_data.pop("payment_reference", None)
    context.user_data.pop("selected_plan", None)
    await message.reply_text(
        f"⏳ Payment request #{payment_id} submitted. Access remains locked while the admin reviews it.",
        reply_markup=welcome_keyboard(),
    )
    if ADMIN_CHAT_ID is not None:
        plan = next((p for p in get_plans(True) if p["plan_key"] == plan_key), None)
        username = f"@{user.username}" if user.username else user.first_name
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"💳 PAYMENT REVIEW #{payment_id}\n\n"
                    f"User: {username}\nID: {user.id}\n"
                    f"Plan: {plan_key}\nAmount: {float(plan['price']) if plan else 0:g} USDT\n"
                    f"Reference: {reference}\n\n"
                    "Review the copied proof separately. Do not approve from a screenshot alone."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ APPROVE", callback_data=f"admin:pay:approve:{payment_id}"),
                            InlineKeyboardButton("❌ REJECT", callback_data=f"admin:pay:reject:{payment_id}"),
                        ]
                    ]
                ),
            )
            await context.bot.copy_message(
                chat_id=ADMIN_CHAT_ID,
                from_chat_id=message.chat_id,
                message_id=proof_message_id,
            )
        except Exception as exc:
            logger.error(
                "Admin payment notification failed payment_id=%s error=%s",
                payment_id,
                safe_error_name(exc),
            )
    log_event("payment.admin_notified", user.id, payment_id=payment_id)


async def _api_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: str
) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    if page == "api:add":
        await _render(
            update,
            context,
            "🔑 Choose which Binance environment these credentials belong to. "
            "Testnet and Real credentials are encrypted and stored separately.",
            _buttons(
                [
                    [("🧪 Add Testnet key", "premium:api:env:testnet")],
                    *(
                        [[("🔴 Add Real key", "premium:api:env:real")]]
                        if get_setting("real_trading_enabled", "0") == "1"
                        else []
                    ),
                    *_nav("premium:api", "premium:dash"),
                ]
            ),
        )
    elif page.startswith("api:env:"):
        environment = page.rsplit(":", 1)[-1]
        if environment not in {"testnet", "real"}:
            await query.answer("Invalid environment.", show_alert=True)
            return
        if environment == "real" and get_setting("real_trading_enabled", "0") != "1":
            await query.answer("Real API entry is disabled until the admin enables Real trading.", show_alert=True)
            return
        vault: CredentialVault = context.application.bot_data["vault"]
        if not vault.available:
            await _render(
                update,
                context,
                "Credential entry is disabled because encrypted storage is unavailable. "
                "Attach persistent Railway storage and restart the bot.",
                _buttons(_nav("premium:api", "premium:dash")),
            )
            return
        context.user_data["user_state"] = "await_api_key"
        context.user_data["pending_api_environment"] = environment
        await _render(
            update,
            context,
            f"Send your {environment.upper()} Binance API key as a single message. "
            "The bot will delete the message after receiving it. Never enable withdrawals.",
            _buttons(_nav("premium:api", "premium:dash")),
        )
    elif page == "api:delete":
        environment = get_preference(user.id)["environment"]
        await asyncio.to_thread(delete_credentials, user.id, environment)
        await _render(
            update,
            context,
            f"Stored {environment.upper()} Binance credentials deleted.",
            _buttons(_nav("premium:api", "premium:dash")),
        )
    else:
        await _show_api_menu(update, context)


async def _show_api_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    pref = get_preference(user.id)
    vault: CredentialVault = context.application.bot_data["vault"]
    rows = [
        [("➕ Add / replace credentials", "premium:api:add")],
        [("🗑 Delete current-environment credentials", "premium:api:delete")],
        *_nav("premium:api", "premium:dash"),
    ]
    status = "encrypted storage ready" if vault.available else "encrypted storage unavailable"
    await _render(
        update,
        context,
        "🔑 BINANCE API\n\n"
        f"Selected environment: {pref['environment'].upper()}\n"
        f"Storage: {status}\n\n"
        "Use a Futures API key with withdrawals disabled and only the permissions needed for Futures. "
        "Keys are isolated by Telegram user and environment. A verified key is required before auto-trading.",
        _buttons(rows),
    )


async def _enable_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if get_setting("emergency_stop", "0") == "1":
        await _render(update, context, "Emergency stop is active. Auto-trading cannot be enabled.", _buttons(_nav("premium:auto", "premium:dash")))
        return
    if get_setting("global_auto_enabled", "1") != "1":
        await _render(update, context, "Automatic execution is disabled by admin.", _buttons(_nav("premium:auto", "premium:dash")))
        return
    environment = get_preference(user.id)["environment"]
    if environment == "real" and get_setting("real_trading_enabled", "0") != "1":
        await _render(update, context, "Real-money trading is disabled by admin.", _buttons(_nav("premium:auto", "premium:dash")))
        return
    vault: CredentialVault = context.application.bot_data["vault"]
    try:
        credentials = vault.load_for_user(user.id, environment)
    except Exception:
        credentials = None
    if not credentials:
        await _render(update, context, f"Add and verify a {environment.upper()} API key before enabling auto-trading.", _buttons(_nav("premium:api", "premium:dash")))
        return
    session = FuturesSession(
        api_key=credentials[0],
        api_secret=credentials[1],
        testnet=environment == "testnet",
    )
    try:
        await session.verify_trading_access()
    except Exception:
        await _render(update, context, "Binance access verification failed. Auto-trading remains off.", _buttons(_nav("premium:api", "premium:dash")))
        return
    finally:
        await session.close()
    set_preference(user.id, "auto_trading", 1)
    log_event("user.auto_trading", user.id, enabled=True, environment=environment)
    await _render(
        update,
        context,
        f"✅ Auto-trading enabled for {environment.upper()}. New orders remain subject to the scanner, risk limits, account checks, exchange confirmation, and a protective stop.",
        _buttons(_nav("premium:auto", "premium:dash")),
    )


async def _dashboard_exchange_snapshot(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    last_signal: dict[str, Any] | None,
) -> dict[str, Any]:
    unavailable = {
        "connected": False,
        "balance": "Unavailable",
        "available": "Unavailable",
        "unrealized": "Unavailable",
        "position_count": "Unavailable",
        "last_price": "Not fetched",
        "last_balance": "Not fetched",
    }
    pref = get_preference(user_id)
    vault: CredentialVault | None = context.application.bot_data.get("vault")
    if not vault:
        return unavailable
    try:
        credentials = vault.load_for_user(user_id, pref["environment"])
    except Exception:
        credentials = None
    if not credentials:
        return unavailable
    session = FuturesSession(
        api_key=credentials[0],
        api_secret=credentials[1],
        testnet=pref["environment"] == "testnet",
    )
    result = dict(unavailable)
    try:
        client = await session.connect()
        equity, available = await session.usdt_equity()
        positions = await client.futures_position_information()
        active = [
            item for item in positions
            if abs(float(item.get("positionAmt", 0) or 0)) > 0
        ]
        unrealized = sum(
            float(item.get("unRealizedProfit", 0) or 0) for item in active
        )
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        result.update(
            {
                "connected": True,
                "balance": f"${equity:,.2f} USDT",
                "available": f"${available:,.2f} USDT",
                "unrealized": f"{unrealized:+,.4f} USDT",
                "position_count": len(active),
                "last_balance": fetched_at,
                "last_price": "Not fetched",
            }
        )
        if last_signal and last_signal.get("symbol"):
            price = await client.futures_mark_price(symbol=last_signal["symbol"])
            mark = float(price.get("markPrice", 0) or 0)
            if mark > 0:
                result["last_price"] = f"{last_signal['symbol']} {mark:g} · {fetched_at}"
        return result
    except Exception as exc:
        logger.info("Dashboard exchange snapshot unavailable error=%s", safe_error_name(exc))
        return result
    finally:
        await session.close()


async def _show_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    pref = get_preference(user.id)
    vault: CredentialVault = context.application.bot_data["vault"]
    try:
        credentials = vault.load_for_user(user.id, pref["environment"])
    except Exception:
        credentials = None
    if not credentials:
        await _render(update, context, "Add and verify Binance API credentials to view live positions.", _buttons(_nav("premium:api", "premium:dash")))
        return
    session = FuturesSession(
        api_key=credentials[0], api_secret=credentials[1], testnet=pref["environment"] == "testnet"
    )
    try:
        client = await session.connect()
        items = await client.futures_position_information()
        active = [p for p in items if abs(float(p.get("positionAmt", 0) or 0)) > 0]
        tracked = {
            trade["symbol"]: trade
            for trade in active_trades(user.id)
            if trade["environment"] == pref["environment"]
        }
        lines = [f"💹 OPEN POSITIONS · {pref['environment'].upper()}\n"]
        manager_buttons: list[list[InlineKeyboardButton]] = []
        if not active:
            lines.append("No open Futures positions.")
        for position in active[:15]:
            amount = float(position.get("positionAmt", 0) or 0)
            direction = "LONG" if amount > 0 else "SHORT"
            trade = tracked.get(position["symbol"])
            signal = get_signal(int(trade["signal_id"])) if trade else None
            entry = float(position.get("entryPrice", 0) or 0)
            mark = float(position.get("markPrice", 0) or 0)
            pnl = float(position.get("unRealizedProfit", 0) or 0)
            raw_leverage = position.get("leverage")
            leverage = (
                float(raw_leverage)
                if raw_leverage not in (None, "", "0", 0)
                else None
            )
            pnl_percent = (
                pnl / (abs(amount) * entry / leverage) * 100
                if entry > 0 and leverage and leverage > 0 else None
            )
            pnl_percent_text = (
                f"{pnl_percent:+.2f}%" if pnl_percent is not None else "N/A"
            )
            if leverage is None:
                logger.info("Position leverage unavailable user_id=%s symbol=%s", user.id, position["symbol"])
            if pnl_percent is None:
                logger.info("Position PnL percent unavailable user_id=%s symbol=%s", user.id, position["symbol"])
            created = trade.get("created_at") if trade else None
            duration = "Not tracked"
            if created:
                try:
                    started = datetime.fromisoformat(str(created))
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
                    duration = f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
                except (TypeError, ValueError):
                    pass
            stop = trade.get("stop_price") if trade else None
            if stop is None and signal:
                stop = signal.get("stop")
            stop_text = f"{float(stop):g}" if stop is not None else "Not tracked"
            targets = [
                signal.get(f"tp{index}") if signal else None
                for index in range(1, 4)
            ]
            position_text = (
                f"{'🟢' if direction == 'LONG' else '🔴'} {position['symbol']} · {direction}\n"
                f"Entry: {entry:g} · Mark: {mark:g}\n"
                f"Quantity: {abs(amount):g} · Leverage: {f'{leverage:g}x' if leverage else 'N/A'}\n"
                f"Unrealized PnL: {pnl:+,.4f} USDT · PnL %: {pnl_percent_text}\n"
                f"SL: {stop_text}"
            )
            lines.append(position_text)
            lines[-1] += (
                f" · TP1: {float(targets[0]):g}" if targets[0] is not None else " · TP1: Not tracked"
            )
            lines[-1] += (
                f" · TP2: {float(targets[1]):g}" if targets[1] is not None else " · TP2: Not tracked"
            )
            lines[-1] += (
                f" · TP3: {float(targets[2]):g}" if targets[2] is not None else " · TP3: Not tracked"
            )
            lines[-1] += f"\nDuration: {duration} · Status: {trade['status'] if trade else 'EXCHANGE POSITION'}"
            if trade:
                manager_buttons.append(
                    [
                        InlineKeyboardButton(
                            f"📊 Manage {position['symbol']}",
                            callback_data=f"trade:refresh:{trade['trade_id']}",
                        )
                    ]
                )
        manager_buttons.extend(
            [
                [
                    InlineKeyboardButton("⬅️ Back", callback_data="premium:dash"),
                    InlineKeyboardButton("🔄 Refresh", callback_data="premium:positions"),
                ]
            ]
        )
        await _render(
            update,
            context,
            "\n".join(lines),
            InlineKeyboardMarkup(manager_buttons),
        )
    except Exception as exc:
        logger.warning("Position display failed error=%s", safe_error_name(exc))
        await _render(update, context, "Could not retrieve Binance positions. Check API permissions and try again.", _buttons(_nav("premium:positions", "premium:dash")))
    finally:
        await session.close()


async def _show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    pref = get_preference(user.id)
    vault: CredentialVault = context.application.bot_data["vault"]
    try:
        credentials = vault.load_for_user(user.id, pref["environment"])
    except Exception:
        credentials = None
    if not credentials:
        await _render(update, context, "Add and verify Binance API credentials to view your Futures wallet.", _buttons(_nav("premium:api", "premium:dash")))
        return
    session = FuturesSession(
        api_key=credentials[0], api_secret=credentials[1], testnet=pref["environment"] == "testnet"
    )
    try:
        equity, available = await session.usdt_equity()
        trades = recent_trades(user.id, 50)
        verified_trades = [item for item in trades if item.get("pnl_verified")]
        bot_pnl = sum(float(item["realized_pnl"]) for item in verified_trades)
        await _render(
            update,
            context,
            f"💰 WALLET & PNL · {pref['environment'].upper()}\n\n"
            f"USDT Futures wallet: {equity:.4f}\nAvailable balance: {available:.4f}\n"
            f"Verified bot realized PnL (latest {len(verified_trades)} reconciled trades): "
            f"{bot_pnl:+.4f} USDT\n\n"
            "Balances are fetched from the selected Binance environment. Testnet and Real are kept separate.",
            _buttons(_nav("premium:wallet", "premium:dash")),
        )
    except Exception as exc:
        logger.warning("Wallet display failed error=%s", safe_error_name(exc))
        await _render(update, context, "Could not retrieve Binance Futures wallet. Check API permissions and try again.", _buttons(_nav("premium:wallet", "premium:dash")))
    finally:
        await session.close()


async def _send_support(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not text or len(text) > 1500:
        await message.reply_text("Support message must contain 1–1500 characters.")
        return
    log_event("support.requested", user.id, message_length=len(text))
    if ADMIN_CHAT_ID is not None:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🆘 SUPPORT · user {user.id} (@{user.username or 'no-username'})\n\n{text[:1500]}",
            )
        except Exception:
            logger.warning("Support request delivery failed user_id=%s", user.id)
    await message.reply_text("Support request sent to the admin.")


async def _delete_sensitive_message(message: Any) -> None:
    try:
        await message.delete()
    except Exception:
        logger.info("Sensitive Telegram message could not be deleted immediately")


async def _render(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(
                text=text[:3900],
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            return
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                return
            logger.info("Telegram screen edit failed error=%s", safe_error_name(exc))
        except Exception as exc:
            logger.info("Telegram screen edit failed error=%s", safe_error_name(exc))
        if update.effective_chat:
            await update.effective_chat.send_message(text=text[:3900], reply_markup=markup)
        return
    if update.effective_message:
        await update.effective_message.reply_text(text=text[:3900], reply_markup=markup)

