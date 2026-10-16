"""Configuration for Binance Telegram Auto-Trading Bot PRO.

Only BOT_TOKEN and ADMIN_CHAT_ID are user-supplied environment variables.
Railway's RAILWAY_VOLUME_MOUNT_PATH is read when Railway provides it.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VOLUME_PATH = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
ON_RAILWAY = bool(os.environ.get("RAILWAY_PROJECT_ID"))
STATE_DIR = Path(VOLUME_PATH).expanduser() if VOLUME_PATH else ROOT
DB_PATH = STATE_DIR / "database.db"
KEY_PATH = STATE_DIR / "credential_master.key"
LOG_PATH = STATE_DIR / "bot.log"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID_RAW = os.environ.get("ADMIN_CHAT_ID", "").strip()
try:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)
except (TypeError, ValueError):
    ADMIN_CHAT_ID = None

DEFAULT_SETTINGS: dict[str, str] = {
    "scanner_enabled": "1",
    "global_auto_enabled": "1",
    "real_trading_enabled": "0",
    "emergency_stop": "0",
    "scan_interval_seconds": "60",
    "min_quote_volume_usdt": "10000000",
    "excluded_symbols": "",
    "timeframes": '["4h","1h","15m","5m"]',
    "payment_instructions": "Payment instructions are being configured. Please check back later.",
    "max_leverage": "3",
    "risk_per_trade_pct": "0.5",
    "max_open_positions": "3",
    "min_reward_risk": "1.5",
    "daily_loss_limit_pct": "2.0",
    "max_signal_age_minutes": "90",
    "tp1_close_percent": "40",
    "tp2_close_percent": "30",
    "move_sl_to_breakeven": "1",
    "trailing_stop_enabled": "1",
    "trailing_stop_percent": "1",
}

DEFAULT_PLANS: tuple[dict[str, object], ...] = (
    {"key": "daily", "label": "Daily", "days": 1, "price": 0},
    {"key": "weekly", "label": "Weekly", "days": 7, "price": 0},
    {"key": "monthly", "label": "Monthly", "days": 30, "price": 0},
    {"key": "lifetime", "label": "Lifetime", "days": 0, "price": 0},
)

TIMEFRAME_INTERVALS = ("4h", "1h", "15m", "5m")
SUPPORTED_ENVIRONMENTS = ("testnet", "real")

