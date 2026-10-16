"""Admin-only Telegram control center and protected moderation actions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import ADMIN_CHAT_ID, TIMEFRAME_INTERVALS
from database import (
    admin_stats,
    admin_recent_trades,
    all_active_trades,
    extend_subscription,
    get_preference,
    get_payment,
    get_trade,
    get_plans,
    get_setting,
    list_users,
    log_event,
    pending_payments,
    recent_events,
    review_payment,
    set_setting,
    set_user_status,
    subscription_info,
    grant_subscription,
    update_plan,
    is_premium,
    set_preference,
    user_count,
    latest_scan_summary,
)
from logger import logger, safe_error_name


def is_admin(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(
        ADMIN_CHAT_ID is not None
        and user is not None
        and chat is not None
        and chat.type == "private"
        and user.id == ADMIN_CHAT_ID
    )


def _nav(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    buttons = list(rows)
    buttons.append(
        [
            InlineKeyboardButton("🏠 User home", callback_data="home"),
            InlineKeyboardButton("🔄 Refresh", callback_data="admin:home"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def admin_keyboard() -> InlineKeyboardMarkup:
    return _nav(
        [
            InlineKeyboardButton("💳 Payments", callback_data="admin:payments"),
            InlineKeyboardButton("👥 Users", callback_data="admin:users:0"),
        ],
        [
            InlineKeyboardButton("🧾 Plans & instructions", callback_data="admin:plans"),
            InlineKeyboardButton("🔍 Scanner", callback_data="admin:scanner"),
        ],
        [
            InlineKeyboardButton("🛡️ Risk settings", callback_data="admin:risk"),
            InlineKeyboardButton("🛑 Emergency stop", callback_data="admin:stop"),
        ],
        [
            InlineKeyboardButton("📜 Live logs", callback_data="admin:logs"),
            InlineKeyboardButton("⚙️ Trading permissions", callback_data="admin:trading"),
        ],
        [
            InlineKeyboardButton("📊 Open trades & history", callback_data="admin:trades"),
        ],
    )


async def show_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        if update.effective_message:
            await update.effective_message.reply_text("This control center is unavailable.")
        return
    stats = admin_stats()
    scan = latest_scan_summary()
    text = (
        "🛡️ BINANCE AUTO TRADER PRO · ADMIN\n\n"
        f"Users: {stats['users']}  ·  Active subscriptions: {stats['active_subscriptions']}\n"
        f"Pending payments: {stats['pending_payments']}  ·  Open bot trades: {stats['open_trades']}\n"
        f"Latest scanner window: {scan['count']} results, {scan['signals']} signals, "
        f"{scan['errors']} errors\n\n"
        f"Scanner: {'RUNNING' if get_setting('scanner_enabled', '1') == '1' else 'PAUSED'}\n"
        f"Emergency stop: {'ON' if get_setting('emergency_stop', '0') == '1' else 'OFF'}"
    )
    if update.callback_query:
        await _edit(update, text, admin_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=admin_keyboard())


async def handle_admin_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> bool:
    """Handle all admin callbacks. Permission is rechecked for every action."""
    query = update.callback_query
    if not query or not is_admin(update):
        if query:
            await query.answer("Not authorized.", show_alert=True)
        return True
    await query.answer()
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    admin_id = update.effective_user.id if update.effective_user else None

    if data == "admin:home":
        await show_admin(update, context)
        return True
    if data == "admin:payments":
        rows = pending_payments(limit=8)
        buttons: list[list[InlineKeyboardButton]] = []
        for row in rows:
            label = f"#{row['payment_id']} · {row['amount']:g} USDT · {row['plan_key']}"
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"✅ Approve {label}",
                        callback_data=f"admin:pay:approve:{row['payment_id']}",
                    )
                ]
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"❌ Reject #{row['payment_id']}",
                        callback_data=f"admin:pay:reject:{row['payment_id']}",
                    )
                ]
            )
        buttons.append(
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="admin:payments"),
                InlineKeyboardButton("⬅️ Admin home", callback_data="admin:home"),
            ]
        )
        await _edit(
            update,
            "💳 PAYMENT REVIEW\n\n"
            + (
                "Pending requests are listed below. Approval is manual; screenshots alone do not verify a payment."
                if rows
                else "No payment requests are awaiting review."
            ),
            InlineKeyboardMarkup(buttons),
        )
        for row in rows:
            try:
                await context.bot.copy_message(
                    chat_id=ADMIN_CHAT_ID,
                    from_chat_id=row["proof_chat_id"],
                    message_id=row["proof_message_id"],
                )
            except Exception as exc:
                logger.warning(
                    "Payment proof copy failed payment_id=%s error=%s",
                    row["payment_id"],
                    safe_error_name(exc),
                )
        return True

    if len(parts) == 4 and parts[1] == "pay" and parts[2] in {"approve", "reject"}:
        try:
            payment_id = int(parts[3])
        except ValueError:
            await query.answer("Invalid payment.", show_alert=True)
            return True
        payment = get_payment(payment_id)
        if not payment or payment["status"] != "PENDING":
            await query.answer("This request was already reviewed or no longer exists.", show_alert=True)
            return True
        decision = "APPROVED" if parts[2] == "approve" else "REJECTED"
        ok, message = review_payment(payment_id, decision, int(admin_id or 0))
        if not ok:
            await query.answer(message, show_alert=True)
            return True
        try:
            if decision == "APPROVED":
                sub = subscription_info(int(payment["user_id"]))
                expiry = "Lifetime" if not sub or not sub.get("expires_at") else sub["expires_at"][:10]
                await context.bot.send_message(
                    chat_id=payment["user_id"],
                    text=f"✅ Payment approved. Your {payment['plan_key'].title()} subscription is active.\nExpires: {expiry}",
                )
            else:
                await context.bot.send_message(
                    chat_id=payment["user_id"],
                    text="❌ Payment was rejected after manual review. You can submit a corrected payment reference and proof from Payment Status.",
                )
        except Exception:
            logger.info("Could not notify user after payment review payment_id=%s", payment_id)
        await _edit(
            update,
            f"Payment #{payment_id} marked {decision}. The request can no longer be approved again.",
            admin_keyboard(),
        )
        return True

    if data == "admin:plans":
        plans = get_plans(include_disabled=True)
        summary = "\n".join(
            f"• {plan['label']}: "
            f"{(str(plan['price']) + ' ' + str(plan['currency'])) if plan['price'] else 'price not set'} · "
            f"{'Lifetime' if not plan['duration_days'] else str(plan['duration_days']) + ' days'} · "
            f"{'enabled' if plan['enabled'] else 'disabled'}"
            for plan in plans
        )
        rows = [
            [InlineKeyboardButton(f"Edit {plan['label']}", callback_data=f"admin:plan:{plan['plan_key']}")]
            for plan in plans
        ]
        rows.extend(
            [
                [InlineKeyboardButton("📝 Payment instructions", callback_data="admin:instructions")],
                [InlineKeyboardButton("⬅️ Admin home", callback_data="admin:home")],
            ]
        )
        await _edit(
            update,
            "🧾 PLANS & PAYMENTS\n\n"
            f"{summary}\n\nEdit a plan to set its USDT price and duration in days. "
            "A price of 0 disables payment for that plan.",
            InlineKeyboardMarkup(rows),
        )
        return True

    if len(parts) == 3 and parts[1] == "plan":
        plan_key = parts[2]
        plan = next((p for p in get_plans(True) if p["plan_key"] == plan_key), None)
        if not plan:
            await query.answer("Unknown plan.", show_alert=True)
            return True
        context.user_data["admin_state"] = "plan_config"
        await _edit(
            update,
            f"Current {plan['label']} plan: {plan['price']:g} USDT / {plan['duration_days']} days.\n\n"
            "Send: plan_key price duration_days\n"
            "Example: monthly 15 30\n"
            "Lifetime uses duration 0. Set price 0 to disable.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin:plans")]]),
        )
        return True

    if data == "admin:instructions":
        context.user_data["admin_state"] = "payment_instructions"
        current = get_setting("payment_instructions", "") or ""
        await _edit(
            update,
            "Send the full payment instructions as one message. Users will see this before submitting proof.\n\n"
            f"Current:\n{current[:1200]}",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin:plans")]]),
        )
        return True

    if data == "admin:scanner":
        enabled = get_setting("scanner_enabled", "1") == "1"
        interval = get_setting("scan_interval_seconds", "60")
        volume = get_setting("min_quote_volume_usdt", "10000000")
        excludes = get_setting("excluded_symbols", "") or "None"
        summary = latest_scan_summary()
        await _edit(
            update,
            "🔍 SCANNER CONTROL\n\n"
            f"Status: {'Running' if enabled else 'Paused'}\n"
            f"Cycle target: {interval}s\n"
            f"24h quote-volume filter: {volume} USDT\n"
            f"Excluded: {excludes[:400]}\n"
            f"Recent results: {summary['count']} · signals {summary['signals']} · errors {summary['errors']}",
            _nav(
                [InlineKeyboardButton("⏸ Pause" if enabled else "▶️ Resume", callback_data="admin:toggle:scanner")],
                [InlineKeyboardButton("⚙️ Set scanner values", callback_data="admin:scanner_config")],
                [InlineKeyboardButton("⬅️ Admin home", callback_data="admin:home")],
            ),
        )
        return True

    if data == "admin:scanner_config":
        context.user_data["admin_state"] = "scanner_config"
        await _edit(
            update,
            "Send scanner settings in this format:\n"
            "interval_seconds min_24h_volume_usdt excluded_symbols\n\n"
            "Example: 60 10000000 BTCUSDT,ETHUSDT\n"
            "Use - for no excluded pairs. Interval must be 30–600 seconds.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin:scanner")]]),
        )
        return True

    if data == "admin:risk":
        values = {
            "Risk / trade": get_setting("risk_per_trade_pct", "0.5") + "%",
            "Max leverage": get_setting("max_leverage", "3") + "x",
            "Max positions": get_setting("max_open_positions", "3"),
            "Minimum R:R": get_setting("min_reward_risk", "1.5") + ":1",
            "Daily loss limit": get_setting("daily_loss_limit_pct", "2.0") + "%",
            "Max signal age": get_setting("max_signal_age_minutes", "90") + " min",
            "TP1 / TP2 close": (
                get_setting("tp1_close_percent", "40") + "% / "
                + get_setting("tp2_close_percent", "30") + "%"
            ),
            "Move SL to breakeven": "On" if get_setting("move_sl_to_breakeven", "1") == "1" else "Off",
            "Trailing stop": (
                ("On" if get_setting("trailing_stop_enabled", "1") == "1" else "Off")
                + " · " + get_setting("trailing_stop_percent", "1") + "%"
            ),
        }
        context.user_data["admin_state"] = "risk_config"
        await _edit(
            update,
            "🛡️ RISK POLICY\n\n"
            + "\n".join(f"• {key}: {value}" for key, value in values.items())
            + "\n\nSet one value at a time using: risk_pct 0.5 | leverage 3 | positions 3 | "
            "rr 1.5 | daily_loss 2 | signal_age 90 | tp1_close 40 | tp2_close 30 | "
            "move_be 1 | trailing 1 | trailing_pct 1\n"
            "Hard ceilings: 3x leverage, 3 positions, and 2% daily loss. "
            "Values that increase risk require a second confirmation.",
            _nav([InlineKeyboardButton("⬅️ Admin home", callback_data="admin:home")]),
        )
        return True

    if data == "admin:risk:confirm":
        pending = context.user_data.pop("pending_risk_update", None)
        if not pending:
            await query.answer("No pending risk change.", show_alert=True)
            return True
        set_setting(pending[0], pending[1], int(admin_id or 0))
        await _edit(update, "Risk setting changed after admin confirmation.", admin_keyboard())
        return True
    if data == "admin:risk:cancel":
        context.user_data.pop("pending_risk_update", None)
        await _edit(update, "Risk setting change cancelled.", admin_keyboard())
        return True

    if data == "admin:trading":
        global_auto = get_setting("global_auto_enabled", "1") == "1"
        real_enabled = get_setting("real_trading_enabled", "0") == "1"
        await _edit(
            update,
            "⚙️ TRADING PERMISSIONS\n\n"
            f"Global auto-execution: {'Enabled' if global_auto else 'Disabled'}\n"
            f"Real trading permission: {'Enabled' if real_enabled else 'Disabled'}\n"
            f"Emergency stop: {'ACTIVE' if get_setting('emergency_stop', '0') == '1' else 'inactive'}\n\n"
            "Users must still enable auto-trading individually. Testnet is the default.",
            _nav(
                [InlineKeyboardButton("Disable auto" if global_auto else "Enable auto", callback_data="admin:toggle:global_auto")],
                [InlineKeyboardButton("Disable Real" if real_enabled else "Review Real permission", callback_data="admin:real:disable" if real_enabled else "admin:real:prepare")],
                [InlineKeyboardButton("⬅️ Admin home", callback_data="admin:home")],
            ),
        )
        return True

    if data == "admin:real:prepare":
        await _edit(
            update,
            "⚠️ REAL-MONEY PERMISSION\n\n"
            "Enabling this lets eligible users submit real-money Futures orders after their "
            "own API verification, explicit environment selection, and two user confirmations. "
            "Market losses can exceed expectations. This does not enable any user's auto-trading by itself.\n\n"
            "Confirm only after reviewing the risks.",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⚠️ Confirm enable Real", callback_data="admin:real:confirm")],
                    [InlineKeyboardButton("Cancel", callback_data="admin:trading")],
                ]
            ),
        )
        return True

    if data == "admin:real:confirm":
        set_setting("real_trading_enabled", "1", int(admin_id or 0))
        await show_admin(update, context)
        return True
    if data == "admin:real:disable":
        set_setting("real_trading_enabled", "0", int(admin_id or 0))
        await show_admin(update, context)
        return True

    if data == "admin:stop":
        active = get_setting("emergency_stop", "0") == "1"
        if active:
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Confirm clear emergency stop", callback_data="admin:stop:clear")],
                    [InlineKeyboardButton("Keep it active", callback_data="admin:home")],
                ]
            )
            await _edit(
                update,
                "🛑 Emergency stop is ACTIVE. Clearing it permits new automatic orders again. "
                "Open positions remain protected and monitored.\n\nConfirm to clear?",
                keyboard,
            )
        else:
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🛑 Activate emergency stop", callback_data="admin:stop:activate")],
                    [InlineKeyboardButton("⬅️ Admin home", callback_data="admin:home")],
                ]
            )
            await _edit(
                update,
                "Emergency stop is off. Activating it blocks new automatic orders; existing positions "
                "continue to be monitored.",
                keyboard,
            )
        return True

    if data == "admin:stop:activate":
        set_setting("emergency_stop", "1", int(admin_id or 0))
        await _edit(update, "🛑 Emergency stop is active. New automatic orders are blocked.", admin_keyboard())
        return True
    if data == "admin:stop:clear":
        set_setting("emergency_stop", "0", int(admin_id or 0))
        await _edit(update, "Emergency stop cleared. Automatic orders still require user opt-in.", admin_keyboard())
        return True

    if data == "admin:toggle:scanner":
        value = "0" if get_setting("scanner_enabled", "1") == "1" else "1"
        set_setting("scanner_enabled", value, int(admin_id or 0))
        await _edit(
            update,
            f"Scanner {'resumed' if value == '1' else 'paused'}.",
            admin_keyboard(),
        )
        return True
    if data == "admin:toggle:global_auto":
        value = "0" if get_setting("global_auto_enabled", "1") == "1" else "1"
        set_setting("global_auto_enabled", value, int(admin_id or 0))
        await _edit(
            update,
            f"Global automatic execution {'enabled' if value == '1' else 'disabled'}.",
            admin_keyboard(),
        )
        return True

    if len(parts) == 3 and parts[1] == "users":
        try:
            page = max(0, int(parts[2]))
        except ValueError:
            page = 0
        users = list_users(page, 8)
        lines: list[str] = ["👥 USERS\n"]
        buttons: list[list[InlineKeyboardButton]] = []
        for user in users:
            name = user.get("username") or user.get("first_name") or "User"
            expiry = user.get("expires_at") or "Lifetime / none"
            lines.append(
                f"• {name[:32]} · ID {user['user_id']} · "
                f"{user.get('subscription_status') or 'no subscription'} · {expiry[:10]} · "
                f"{user.get('environment', 'testnet').upper()} auto "
                f"{'ON' if user.get('auto_trading') else 'OFF'}"
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"Grant monthly → {user['user_id']}",
                        callback_data=f"admin:grant:{user['user_id']}:monthly",
                    ),
                    InlineKeyboardButton(
                        "Suspend" if user.get("status") != "suspended" else "Reactivate",
                        callback_data=f"admin:status:{user['user_id']}:{'suspended' if user.get('status') != 'suspended' else 'active'}",
                    ),
                ]
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        "Revoke subscription",
                        callback_data=f"admin:status:{user['user_id']}:revoked",
                    )
                ]
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        "Disable user auto" if user.get("auto_trading") else "Enable user auto",
                        callback_data=f"admin:auto:{user['user_id']}",
                    )
                ]
            )
        if page > 0:
            buttons.append(
                [
                    InlineKeyboardButton("← Previous", callback_data=f"admin:users:{page-1}"),
                    InlineKeyboardButton("Next →", callback_data=f"admin:users:{page+1}"),
                ]
            )
        else:
            buttons.append([InlineKeyboardButton("Next →", callback_data=f"admin:users:{page+1}")])
        buttons.append([InlineKeyboardButton("⬅️ Admin home", callback_data="admin:home")])
        await _edit(
            update,
            "\n".join(lines) if users else "No users registered yet.",
            InlineKeyboardMarkup(buttons),
        )
        return True

    if len(parts) == 4 and parts[1] == "grant":
        try:
            user_id = int(parts[2])
        except ValueError:
            await query.answer("Invalid user.", show_alert=True)
            return True
        ok, message = grant_subscription(user_id, parts[3], int(admin_id or 0))
        if ok:
            sub = subscription_info(user_id)
            expiry = "Lifetime" if not sub or not sub.get("expires_at") else sub["expires_at"][:10]
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"✅ The admin granted you a {parts[3].title()} subscription. Expires: {expiry}",
                )
            except Exception:
                pass
        if not ok:
            await query.answer(message, show_alert=True)
        else:
            await _edit(update, f"Subscription granted for user {user_id}.", admin_keyboard())
        return True

    if len(parts) == 4 and parts[1] == "status":
        try:
            target_id = int(parts[2])
        except ValueError:
            await query.answer("Invalid user.", show_alert=True)
            return True
        ok = set_user_status(target_id, parts[3], int(admin_id or 0))
        if ok:
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=(
                        "Your premium access has been suspended by the admin."
                        if parts[3] == "suspended"
                        else "Your premium subscription has been revoked by the admin."
                        if parts[3] == "revoked"
                        else "Your account has been reactivated."
                    ),
                )
            except Exception:
                pass
        if not ok:
            await query.answer("Could not update this user.", show_alert=True)
        else:
            await _edit(update, f"User {target_id} status updated: {parts[3]}.", admin_keyboard())
        return True

    if data == "admin:logs":
        events = recent_events(12)
        lines = [
            f"• {str(row['created_at'])[11:19]} · {row['event_type']} · {row['details'][:120]}"
            for row in events
        ]
        scan = latest_scan_summary()
        await _edit(
            update,
            "📜 LIVE ADMIN LOGS\n\n"
            f"Recent scanner: {scan['count']} results / 10m, "
            f"{scan['signals']} signals, {scan['errors']} errors\n\n"
            + ("\n".join(lines) if lines else "No events recorded yet."),
            _nav([InlineKeyboardButton("🔄 Refresh logs", callback_data="admin:logs")]),
        )
        return True

    if data == "admin:trades":
        trades = admin_recent_trades(15)
        active_ids = {int(row["trade_id"]) for row in all_active_trades()}
        lines = ["📊 OPEN TRADES & RECENT HISTORY\n"]
        buttons: list[list[InlineKeyboardButton]] = []
        for trade in trades:
            pnl = (
                f"{float(trade['realized_pnl']):+.3f} USDT"
                if trade.get("pnl_verified")
                else "PnL not reconciled"
            )
            lines.append(
                f"#{trade['trade_id']} · {trade['user_id']} · {trade['environment']} · "
                f"{trade['direction']} {trade['symbol']} · {trade['status']} · {pnl}"
            )
            if int(trade["trade_id"]) in active_ids:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"Review close #{trade['trade_id']}",
                            callback_data=f"admin:close:{trade['trade_id']}",
                        )
                    ]
                )
        buttons.append(
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="admin:trades"),
                InlineKeyboardButton("⬅️ Admin home", callback_data="admin:home"),
            ]
        )
        await _edit(
            update,
            "\n".join(lines) if trades else "No bot trades have been recorded.",
            InlineKeyboardMarkup(buttons),
        )
        return True

    if len(parts) == 3 and parts[1] == "close":
        try:
            trade_id = int(parts[2])
        except ValueError:
            await query.answer("Invalid trade.", show_alert=True)
            return True
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
            await query.answer("This trade is no longer active.", show_alert=True)
            return True
        await _edit(
            update,
            f"⚠️ Confirm admin close\n\nTrade #{trade_id}: {trade['environment']} "
            f"{trade['direction']} {trade['symbol']} for user {trade['user_id']}.\n"
            "The bot will query the current Binance position, submit a reduce-only "
            "market close, verify the account is flat, and cancel only tracked bot orders.",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Confirm close position",
                            callback_data=f"admin:close:confirm:{trade_id}",
                        )
                    ],
                    [InlineKeyboardButton("Cancel", callback_data="admin:trades")],
                ]
            ),
        )
        return True

    if (
        len(parts) == 4
        and parts[1] == "close"
        and parts[2] == "confirm"
    ):
        try:
            trade_id = int(parts[3])
        except ValueError:
            await query.answer("Invalid trade.", show_alert=True)
            return True
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
            await query.answer("This trade is no longer active.", show_alert=True)
            return True
        engine = context.application.bot_data.get("trade_engine")
        if not engine:
            await query.answer("Trade engine is unavailable.", show_alert=True)
            return True
        ok, message = await engine.admin_close_trade(trade_id, int(admin_id or 0))
        await _edit(
            update,
            f"{'✅' if ok else '⚠️'} {message}",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Refresh trades", callback_data="admin:trades")],
                    [InlineKeyboardButton("⬅️ Admin home", callback_data="admin:home")],
                ]
            ),
        )
        return True

    if len(parts) == 3 and parts[1] == "auto":
        try:
            target_id = int(parts[2])
        except ValueError:
            await query.answer("Invalid user.", show_alert=True)
            return True
        current = get_preference(target_id)
        enabled = not bool(current["auto_trading"])
        if enabled and not is_premium(target_id):
            await query.answer(
                "Auto-trading requires an active, approved subscription.",
                show_alert=True,
            )
            return True
        set_preference(target_id, "auto_trading", int(enabled))
        log_event(
            "admin.user_auto_trading",
            int(admin_id or 0),
            user_id=target_id,
            enabled=enabled,
        )
        await _edit(
            update,
            f"Auto-trading {'enabled' if enabled else 'disabled'} for user {target_id}.",
            admin_keyboard(),
        )
        return True

    await query.answer("That admin action is unavailable.", show_alert=True)
    return True


async def handle_admin_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> bool:
    if not is_admin(update):
        return False
    state = context.user_data.get("admin_state")
    if not state:
        return False
    admin_id = update.effective_user.id if update.effective_user else 0
    context.user_data.pop("admin_state", None)
    if state == "payment_instructions":
        instructions = text.strip()[:3000]
        if not instructions:
            await update.effective_message.reply_text("Instructions cannot be empty.")
            return True
        set_setting("payment_instructions", instructions, admin_id)
        await update.effective_message.reply_text(
            "Payment instructions updated.",
            reply_markup=admin_keyboard(),
        )
        return True
    if state == "plan_config":
        parts = text.split()
        if len(parts) != 3:
            await update.effective_message.reply_text("Use: plan_key price duration_days")
            return True
        try:
            key, price, duration = parts[0].lower(), float(parts[1]), int(parts[2])
        except ValueError:
            await update.effective_message.reply_text("Price must be numeric and duration must be whole days.")
            return True
        if price < 0 or price > 1_000_000 or duration < 0 or duration > 36500:
            await update.effective_message.reply_text("Price or duration is outside the allowed range.")
            return True
        if not update_plan(key, price, duration, enabled=price > 0, actor_id=admin_id):
            await update.effective_message.reply_text("Unknown plan key.")
            return True
        await update.effective_message.reply_text("Plan updated.", reply_markup=admin_keyboard())
        return True
    if state == "scanner_config":
        parts = text.strip().split(maxsplit=2)
        if len(parts) < 2:
            await update.effective_message.reply_text("Use: interval_seconds min_volume excluded_symbols")
            return True
        try:
            interval = int(parts[0])
            volume = float(parts[1])
        except ValueError:
            await update.effective_message.reply_text("Interval and volume must be numeric.")
            return True
        excludes = parts[2] if len(parts) > 2 else "-"
        if interval < 30 or interval > 600 or volume < 0 or volume > 1e12:
            await update.effective_message.reply_text("Interval must be 30–600 seconds and volume 0–1e12 USDT.")
            return True
        symbols = [] if excludes.strip() == "-" else [s.strip().upper() for s in excludes.split(",")]
        if any(not s.isalnum() or len(s) > 20 for s in symbols):
            await update.effective_message.reply_text("Excluded symbols must be comma-separated Binance symbols.")
            return True
        set_setting("scan_interval_seconds", interval, admin_id)
        set_setting("min_quote_volume_usdt", volume, admin_id)
        set_setting("excluded_symbols", ",".join(symbols), admin_id)
        await update.effective_message.reply_text("Scanner settings updated.", reply_markup=admin_keyboard())
        return True
    if state == "risk_config":
        parts = text.split()
        if len(parts) != 2:
            await update.effective_message.reply_text("Use one field and value, for example: risk_pct 0.5")
            return True
        key, raw_value = parts[0].lower(), parts[1]
        mapping = {
            "risk_pct": ("risk_per_trade_pct", 0.05, 2.0),
            "leverage": ("max_leverage", 1, 3),
            "positions": ("max_open_positions", 1, 3),
            "rr": ("min_reward_risk", 1.5, 10),
            "daily_loss": ("daily_loss_limit_pct", 0.25, 2.0),
            "signal_age": ("max_signal_age_minutes", 1, 1440),
            "tp1_close": ("tp1_close_percent", 0, 95),
            "tp2_close": ("tp2_close_percent", 0, 95),
            "move_be": ("move_sl_to_breakeven", 0, 1),
            "trailing": ("trailing_stop_enabled", 0, 1),
            "trailing_pct": ("trailing_stop_percent", 0.05, 10),
        }
        if key not in mapping:
            await update.effective_message.reply_text("Unknown field.")
            return True
        setting, minimum, maximum = mapping[key]
        try:
            value = float(raw_value)
        except ValueError:
            await update.effective_message.reply_text("Value must be numeric.")
            return True
        if value < minimum or value > maximum:
            await update.effective_message.reply_text(
                f"Value must be between {minimum} and {maximum}."
            )
            return True
        if key in {"tp1_close", "tp2_close"}:
            other_key = "tp2_close_percent" if key == "tp1_close" else "tp1_close_percent"
            other = float(get_setting(other_key, "30" if key == "tp1_close" else "40") or 0)
            if value + other > 95:
                await update.effective_message.reply_text(
                    "TP1 and TP2 allocations must leave at least 5% for TP3."
                )
                return True
        if key in {"move_be", "trailing"}:
            value = int(value)
        current = float(get_setting(setting, str(minimum)) or minimum)
        risk_increase = (
            value > current if key in {"risk_pct", "leverage", "positions", "daily_loss"}
            else value < current
        )
        if risk_increase:
            context.user_data["pending_risk_update"] = (setting, value)
            await update.effective_message.reply_text(
                f"⚠️ Confirm risk-policy change: {key} {current:g} → {value:g}. "
                "Confirming can increase exposure.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("✅ Confirm change", callback_data="admin:risk:confirm")],
                        [InlineKeyboardButton("Cancel", callback_data="admin:risk:cancel")],
                    ]
                ),
            )
        else:
            set_setting(setting, value, admin_id)
            await update.effective_message.reply_text("Risk setting updated.", reply_markup=admin_keyboard())
        return True
    await update.effective_message.reply_text("Unknown admin input state.")
    return True


async def _edit(update: Update, text: str, markup: InlineKeyboardMarkup) -> None:
    query = update.callback_query
    if not query:
        return
    try:
        await query.edit_message_text(
            text=text[:3900],
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.info("Admin screen edit failed error=%s", safe_error_name(exc))
        if update.effective_chat:
            await update.effective_chat.send_message(text=text[:3900], reply_markup=markup)

