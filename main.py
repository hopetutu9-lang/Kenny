"""Entry point for Binance Telegram Auto-Trading Bot PRO."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from admin_panel import admin_keyboard, is_admin
from binance_client import CredentialVault
from config import ADMIN_CHAT_ID, BOT_TOKEN, DB_PATH, STATE_DIR
from database import (
    admin_stats,
    extend_subscription,
    grant_subscription,
    init_db,
    is_premium,
    log_event,
    set_user_status,
)
from logger import get_logger, safe_error_name
from market_scanner import MarketScanner
from telegram_ui import (
    admin_action_command,
    admin_command,
    handle_media,
    handle_text,
    route_callback,
    start,
    welcome_keyboard,
)
from trade_engine import TradeEngine

logger = get_logger("binance_telegram_bot")
_background_tasks: list[asyncio.Task[Any]] = []


def _validate_runtime() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if ADMIN_CHAT_ID is None or ADMIN_CHAT_ID <= 0:
        missing.append("ADMIN_CHAT_ID (a positive numeric private-chat/user ID)")
    if missing:
        raise RuntimeError("Required Railway variables are missing or invalid: " + ", ".join(missing))


async def _post_init(application: Application) -> None:
    vault = CredentialVault()
    vault.initialize()
    application.bot_data["vault"] = vault
    trade_engine = TradeEngine(application, vault)
    scanner = MarketScanner(trade_engine.on_signal)
    application.bot_data["trade_engine"] = trade_engine
    application.bot_data["scanner"] = scanner
    worker_tasks = application.bot_data.setdefault("worker_tasks", {})
    for task_name, coroutine in (
        ("market-scanner", scanner.run()),
        ("position-monitor", trade_engine.monitor()),
    ):
        existing = worker_tasks.get(task_name)
        if existing is not None and not existing.done():
            coroutine.close()
            continue
        task = asyncio.create_task(coroutine, name=task_name)
        worker_tasks[task_name] = task
        _background_tasks.append(task)
    try:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Open welcome or premium menu"),
                BotCommand("help", "Payment and bot help"),
                BotCommand("status", "Check payment status"),
            ]
        )
    except Exception:
        logger.info("Telegram command list could not be updated")
    log_event(
        "system.started",
        ADMIN_CHAT_ID,
        persistent_volume=bool(__import__("os").environ.get("RAILWAY_VOLUME_MOUNT_PATH")),
        credential_storage=vault.available,
        database_path=str(DB_PATH),
    )
    logger.info(
        "Bot service started storage=%s credential_storage=%s",
        "volume" if __import__("os").environ.get("RAILWAY_VOLUME_MOUNT_PATH") else "local-or-ephemeral",
        vault.available,
    )
    await _notify_admin_start(application, vault.available)


async def _post_shutdown(application: Application) -> None:
    scanner: MarketScanner | None = application.bot_data.get("scanner")
    if scanner:
        await scanner.stop()
    tasks = list(application.bot_data.get("worker_tasks", {}).values())
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _background_tasks.clear()
    application.bot_data["worker_tasks"] = {}


async def _notify_admin_start(application: Application, vault_available: bool) -> None:
    if ADMIN_CHAT_ID is None:
        return
    text = (
        "🟢 Binance bot process is online.\n"
        f"Scanner: {'starting' if __import__('database').get_setting('scanner_enabled', '1') == '1' else 'paused'}\n"
        f"Credential encryption: {'ready' if vault_available else 'disabled'}\n"
        "Execution defaults to Testnet. Check persistent storage before saving any API credentials."
    )
    try:
        await application.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=text,
            reply_markup=admin_keyboard(),
        )
    except Exception:
        logger.warning("Startup notification could not be delivered to admin")


async def _help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not update.effective_message or not user:
        return
    if is_premium(user.id):
        text = (
            "Use the premium menu to view scanner status, signals, execution controls, "
            "positions, and risk settings. Testnet is the default. Contact the admin for support."
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚀 Open premium dashboard", callback_data="premium:dash")]]
        )
    else:
        text = (
            "Use View Plans, Payment Instructions, Submit Payment, and Payment Status below. "
            "Access is unlocked only after an admin manually approves payment."
        )
        keyboard = welcome_keyboard()
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def _status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    from database import get_payment_status, has_pending_payment

    requests = get_payment_status(user.id)
    if not requests:
        text = "No payment request found."
    else:
        text = "⏳ PAYMENT STATUS\n\n" + "\n".join(
            f"• {row['plan_key'].title()} · {row['amount']:g} USDT · {row['status']}"
            for row in requests
        )
    if has_pending_payment(user.id):
        text += "\n\nAccess remains locked while review is pending."
    await message.reply_text(text, reply_markup=welcome_keyboard())


async def _premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not is_premium(user.id):
        await message.reply_text(
            "Premium access is required.",
            reply_markup=welcome_keyboard(),
        )
        return
    await message.reply_text(
        "🚀 BINANCE AUTO TRADER PRO\nUse the premium menu for this feature.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📊 Dashboard", callback_data="premium:dash")]]
        ),
    )


async def _grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await admin_action_command(update, context)


async def _suspend_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await admin_action_command(update, context)


async def _revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await admin_action_command(update, context)


async def _extend_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await admin_action_command(update, context)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if error:
        logger.error("Telegram update failed error=%s", safe_error_name(error))


def build_application() -> Application:
    _validate_runtime()
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(10)
        .read_timeout(30)
        .write_timeout(30)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", _help_command))
    application.add_handler(CommandHandler("status", _status_command))
    application.add_handler(CommandHandler("dashboard", _premium_command))
    application.add_handler(CommandHandler("scanner", _premium_command))
    application.add_handler(CommandHandler("signals", _premium_command))
    application.add_handler(CommandHandler("auto", _premium_command))
    application.add_handler(CommandHandler("positions", _premium_command))
    application.add_handler(CommandHandler("history", _premium_command))
    application.add_handler(CommandHandler("wallet", _premium_command))
    application.add_handler(CommandHandler("risk", _premium_command))
    application.add_handler(CommandHandler("api", _premium_command))
    application.add_handler(CommandHandler("strategy", _premium_command))
    application.add_handler(CommandHandler("subscription", _premium_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("grant", _grant_command))
    application.add_handler(CommandHandler("suspend", _suspend_command))
    application.add_handler(CommandHandler("revoke", _revoke_command))
    application.add_handler(CommandHandler("extend", _extend_command))
    application.add_handler(CallbackQueryHandler(route_callback))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_media))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(_error_handler)
    return application


def main() -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        init_db()
        app = build_application()
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
            poll_interval=1.0,
            timeout=30,
        )
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.critical("Bot startup or runtime failed error=%s", safe_error_name(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())

