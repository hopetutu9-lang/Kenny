"""SQLite persistence, subscriptions, payment decisions, and audit records."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from config import DB_PATH, DEFAULT_PLANS, DEFAULT_SETTINGS
from logger import logger, safe_event_text


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 20000")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    schema = """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS plans (
        plan_key TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        duration_days INTEGER NOT NULL,
        price REAL NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'USDT',
        enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS subscriptions (
        user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
        plan_key TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        expires_at TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS payments (
        payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(user_id),
        plan_key TEXT NOT NULL REFERENCES plans(plan_key),
        amount REAL NOT NULL,
        reference TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED')),
        proof_chat_id INTEGER NOT NULL,
        proof_message_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        reviewed_at TEXT,
        reviewed_by INTEGER,
        review_note TEXT
    );
    CREATE INDEX IF NOT EXISTS payments_user_idx ON payments(user_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS payments_status_idx ON payments(status, created_at);
    CREATE TABLE IF NOT EXISTS settings (
        setting_key TEXT PRIMARY KEY,
        setting_value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS preferences (
        user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
        environment TEXT NOT NULL DEFAULT 'testnet',
        auto_trading INTEGER NOT NULL DEFAULT 0,
        notifications INTEGER NOT NULL DEFAULT 1,
        risk_pct REAL NOT NULL DEFAULT 0.5,
        trailing_stop INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS user_secrets (
        user_id INTEGER NOT NULL REFERENCES users(user_id),
        environment TEXT NOT NULL CHECK(environment IN ('testnet','real')),
        key_cipher TEXT NOT NULL,
        secret_cipher TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(user_id, environment)
    );
    CREATE TABLE IF NOT EXISTS signals (
        signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        direction TEXT,
        side TEXT,
        setup_hash TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        entry REAL,
        entry_low REAL,
        entry_high REAL,
        entry_zone TEXT,
        stop REAL,
        tp1 REAL,
        tp2 REAL,
        tp3 REAL,
        confidence REAL NOT NULL DEFAULT 0,
        strategy TEXT NOT NULL DEFAULT 'SMC',
        timeframe TEXT NOT NULL DEFAULT '4H / 1H / 5m',
        structure TEXT NOT NULL DEFAULT '',
        sweep_time INTEGER,
        bos_time INTEGER,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT
    );
    CREATE TABLE IF NOT EXISTS scan_results (
        result_id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        outcome TEXT NOT NULL,
        reason TEXT NOT NULL,
        scanned_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS scan_results_time_idx ON scan_results(scanned_at DESC);
    CREATE TABLE IF NOT EXISTS trades (
        trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(user_id),
        signal_id INTEGER NOT NULL REFERENCES signals(signal_id),
        setup_hash TEXT NOT NULL,
        environment TEXT NOT NULL,
        symbol TEXT NOT NULL,
        direction TEXT NOT NULL,
        side TEXT,
        status TEXT NOT NULL,
        entry_order_id TEXT,
        stop_order_id TEXT,
        sl_order_id TEXT,
        sl_actual_order_id TEXT,
        exchange_order_ids TEXT NOT NULL DEFAULT '[]',
        tp_order_ids TEXT NOT NULL DEFAULT '[]',
        tp_actual_order_ids TEXT NOT NULL DEFAULT '[]',
        tp1_order_id TEXT,
        tp2_order_id TEXT,
        tp3_order_id TEXT,
        tp1_actual_order_id TEXT,
        tp2_actual_order_id TEXT,
        tp3_actual_order_id TEXT,
        tp1_realized_pnl REAL,
        tp2_realized_pnl REAL,
        tp3_realized_pnl REAL,
        protection_error TEXT,
        quantity REAL NOT NULL DEFAULT 0,
        remaining_quantity REAL,
        entry_price REAL,
        stop_price REAL,
        best_price REAL,
        exit_price REAL,
        realized_pnl REAL NOT NULL DEFAULT 0,
        pnl_verified INTEGER NOT NULL DEFAULT 0,
        reason TEXT,
        close_reason TEXT,
        tp1_hit INTEGER NOT NULL DEFAULT 0,
        tp2_hit INTEGER NOT NULL DEFAULT 0,
        tp3_hit INTEGER NOT NULL DEFAULT 0,
        tp1_at TEXT,
        tp2_at TEXT,
        tp3_at TEXT,
        telegram_chat_id INTEGER,
        telegram_message_id INTEGER,
        created_at TEXT NOT NULL,
        executed_at TEXT,
        closed_at TEXT,
        UNIQUE(user_id, environment, setup_hash)
    );
    CREATE INDEX IF NOT EXISTS trades_user_time_idx ON trades(user_id, created_at DESC);
    CREATE TABLE IF NOT EXISTS events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_id INTEGER,
        event_type TEXT NOT NULL,
        details TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS events_time_idx ON events(created_at DESC);
    """
    with connection() as conn:
        conn.executescript(schema)
        trade_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        trade_migrations = {
            "tp_order_ids": "TEXT NOT NULL DEFAULT '[]'",
            "exchange_order_ids": "TEXT NOT NULL DEFAULT '[]'",
            "sl_order_id": "TEXT",
            "sl_actual_order_id": "TEXT",
            "tp_actual_order_ids": "TEXT NOT NULL DEFAULT '[]'",
            "tp1_order_id": "TEXT",
            "tp2_order_id": "TEXT",
            "tp3_order_id": "TEXT",
            "tp1_actual_order_id": "TEXT",
            "tp2_actual_order_id": "TEXT",
            "tp3_actual_order_id": "TEXT",
            "tp1_realized_pnl": "REAL",
            "tp2_realized_pnl": "REAL",
            "tp3_realized_pnl": "REAL",
            "protection_error": "TEXT",
            "remaining_quantity": "REAL",
            "best_price": "REAL",
            "pnl_verified": "INTEGER NOT NULL DEFAULT 0",
            "exit_price": "REAL",
            "close_reason": "TEXT",
            "tp1_hit": "INTEGER NOT NULL DEFAULT 0",
            "tp2_hit": "INTEGER NOT NULL DEFAULT 0",
            "tp3_hit": "INTEGER NOT NULL DEFAULT 0",
            "tp1_at": "TEXT",
            "tp2_at": "TEXT",
            "tp3_at": "TEXT",
            "telegram_chat_id": "INTEGER",
            "telegram_message_id": "INTEGER",
            "side": "TEXT",
            "executed_at": "TEXT",
            "reason": "TEXT",
        }
        for column, definition in trade_migrations.items():
            if column not in trade_columns:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {column} {definition}")
        conn.execute("UPDATE trades SET side=COALESCE(side,direction)")
        signal_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(signals)").fetchall()
        }
        signal_migrations = {
            "side": "TEXT",
            "sweep_time": "INTEGER",
            "bos_time": "INTEGER",
            "entry_low": "REAL",
            "entry_high": "REAL",
            "entry_zone": "TEXT",
            "confidence": "REAL NOT NULL DEFAULT 0",
            "strategy": "TEXT NOT NULL DEFAULT 'SMC'",
            "timeframe": "TEXT NOT NULL DEFAULT '4H / 1H / 5m'",
            "structure": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT",
            "expires_at": "TEXT",
        }
        for column, definition in signal_migrations.items():
            if column not in signal_columns:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {column} {definition}")
        # Legacy signals remain usable: their original detection time is the
        # only safe expiry anchor; sweep_time can legitimately be much earlier.
        conn.execute(
            "UPDATE signals SET updated_at=COALESCE(updated_at,created_at), "
            "side=COALESCE(side,direction)"
        )
        for row in conn.execute(
            "SELECT signal_id,created_at,expires_at,entry_low,entry_high,entry_zone "
            "FROM signals"
        ).fetchall():
            updates: dict[str, Any] = {}
            if not row["expires_at"]:
                try:
                    detected = parse_dt(row["created_at"])
                    if detected:
                        updates["expires_at"] = to_iso(detected + timedelta(minutes=90))
                except (TypeError, ValueError):
                    pass
            if not row["entry_zone"] and (
                row["entry_low"] is not None or row["entry_high"] is not None
            ):
                updates["entry_zone"] = json.dumps(
                    {"low": row["entry_low"], "high": row["entry_high"]}
                )
            if updates:
                assignments = ",".join(f"{key}=?" for key in updates)
                conn.execute(
                    f"UPDATE signals SET {assignments} WHERE signal_id=?",
                    (*updates.values(), row["signal_id"]),
                )
        now = to_iso(utc_now())
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(setting_key,setting_value,updated_at) VALUES(?,?,?)",
                (key, value, now),
            )
        for plan in DEFAULT_PLANS:
            conn.execute(
                """INSERT OR IGNORE INTO plans(plan_key,label,duration_days,price,currency,enabled)
                   VALUES(?,?,?,?,?,1)""",
                (
                    plan["key"],
                    plan["label"],
                    plan["days"],
                    plan["price"],
                    "USDT",
                ),
            )
    logger.info("SQLite database initialized")


def log_event(event_type: str, actor_id: int | None = None, **details: Any) -> None:
    safe_details = {
        key: value for key, value in details.items()
        if key.lower() not in {"api_key", "api_secret", "secret", "token", "bot_token"}
    }
    text = json.dumps(safe_details, ensure_ascii=True, default=str)[:2000]
    with connection() as conn:
        conn.execute(
            "INSERT INTO events(actor_id,event_type,details,created_at) VALUES(?,?,?,?)",
            (actor_id, event_type[:100], text, to_iso(utc_now())),
        )
    logger.info(safe_event_text(event_type, {"actor_id": actor_id, **safe_details}))


def upsert_user(user_id: int, username: str | None, first_name: str) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO users(user_id,username,first_name,created_at) VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
               first_name=excluded.first_name""",
            (user_id, username, first_name[:120], to_iso(utc_now())),
        )
        conn.execute("INSERT OR IGNORE INTO preferences(user_id) VALUES(?)", (user_id,))


def get_setting(key: str, default: str | None = None) -> str | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key=?", (key,)
        ).fetchone()
    return str(row["setting_value"]) if row else default


def set_setting(key: str, value: object, actor_id: int | None = None) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO settings(setting_key,setting_value,updated_at) VALUES(?,?,?)
               ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,
               updated_at=excluded.updated_at""",
            (key, str(value), to_iso(utc_now())),
        )
    log_event("setting.updated", actor_id, setting=key, value=value)


def get_plans(include_disabled: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM plans"
    if not include_disabled:
        query += " WHERE enabled=1"
    query += " ORDER BY CASE plan_key WHEN 'daily' THEN 1 WHEN 'weekly' THEN 2 WHEN 'monthly' THEN 3 ELSE 4 END"
    with connection() as conn:
        return [dict(row) for row in conn.execute(query).fetchall()]


def update_plan(
    plan_key: str,
    price: float | None = None,
    duration_days: int | None = None,
    enabled: bool | None = None,
    actor_id: int | None = None,
) -> bool:
    with connection() as conn:
        row = conn.execute("SELECT * FROM plans WHERE plan_key=?", (plan_key,)).fetchone()
        if not row:
            return False
        conn.execute(
            """UPDATE plans SET price=?, duration_days=?, enabled=? WHERE plan_key=?""",
            (
                row["price"] if price is None else price,
                row["duration_days"] if duration_days is None else duration_days,
                row["enabled"] if enabled is None else int(enabled),
                plan_key,
            ),
        )
    log_event("plan.updated", actor_id, plan=plan_key, price=price, days=duration_days)
    return True


def create_payment(
    user_id: int,
    plan_key: str,
    reference: str,
    proof_chat_id: int,
    proof_message_id: int,
) -> int:
    with connection() as conn:
        if conn.execute(
            "SELECT 1 FROM payments WHERE user_id=? AND status='PENDING' LIMIT 1",
            (user_id,),
        ).fetchone():
            raise ValueError("a payment review is already pending")
        plan = conn.execute(
            "SELECT price,enabled FROM plans WHERE plan_key=?", (plan_key,)
        ).fetchone()
        if not plan or not plan["enabled"] or float(plan["price"]) <= 0:
            raise ValueError("plan unavailable")
        cursor = conn.execute(
            """INSERT INTO payments(user_id,plan_key,amount,reference,status,
               proof_chat_id,proof_message_id,created_at)
               VALUES(?,?,?,?,'PENDING',?,?,?)""",
            (
                user_id,
                plan_key,
                float(plan["price"]),
                reference.strip()[:160],
                proof_chat_id,
                proof_message_id,
                to_iso(utc_now()),
            ),
        )
        payment_id = int(cursor.lastrowid)
    log_event("payment.submitted", user_id, payment_id=payment_id, plan=plan_key)
    return payment_id


def get_payment(payment_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """SELECT p.*,u.username,u.first_name FROM payments p
               JOIN users u ON u.user_id=p.user_id WHERE p.payment_id=?""",
            (payment_id,),
        ).fetchone()
    return dict(row) if row else None


def review_payment(
    payment_id: int,
    decision: str,
    admin_id: int,
    note: str = "",
) -> tuple[bool, str]:
    if decision not in {"APPROVED", "REJECTED"}:
        return False, "invalid decision"
    now = utc_now()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        payment = conn.execute(
            "SELECT * FROM payments WHERE payment_id=?", (payment_id,)
        ).fetchone()
        if not payment:
            conn.rollback()
            return False, "payment not found"
        if payment["status"] != "PENDING":
            conn.rollback()
            return False, "already reviewed"
        conn.execute(
            """UPDATE payments SET status=?,reviewed_at=?,reviewed_by=?,review_note=?
               WHERE payment_id=? AND status='PENDING'""",
            (decision, to_iso(now), admin_id, note[:300], payment_id),
        )
        if decision == "APPROVED":
            user = conn.execute(
                "SELECT status FROM users WHERE user_id=?", (payment["user_id"],)
            ).fetchone()
            if not user or user["status"] != "active":
                conn.rollback()
                return False, "user is suspended"
            sub = conn.execute(
                "SELECT * FROM subscriptions WHERE user_id=?", (payment["user_id"],)
            ).fetchone()
            plan = conn.execute(
                "SELECT duration_days FROM plans WHERE plan_key=?",
                (payment["plan_key"],),
            ).fetchone()
            if not plan:
                conn.rollback()
                return False, "plan not found"
            duration = int(plan["duration_days"])
            current_expiry = parse_dt(sub["expires_at"]) if sub else None
            if duration == 0:
                expiry = None
            else:
                base = max(now, current_expiry) if current_expiry and sub["status"] == "active" else now
                expiry = base + timedelta(days=duration)
            started = (
                sub["started_at"]
                if sub and current_expiry and current_expiry > now and sub["status"] == "active"
                else to_iso(now)
            )
            conn.execute(
                """INSERT INTO subscriptions(user_id,plan_key,status,started_at,expires_at,updated_at)
                   VALUES(?,?,'active',?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET plan_key=excluded.plan_key,
                   status='active',started_at=excluded.started_at,expires_at=excluded.expires_at,
                   updated_at=excluded.updated_at""",
                (
                    payment["user_id"],
                    payment["plan_key"],
                    started,
                    to_iso(expiry),
                    to_iso(now),
                ),
            )
        conn.execute(
            "INSERT INTO events(actor_id,event_type,details,created_at) VALUES(?,?,?,?)",
            (
                admin_id,
                f"payment.{decision.lower()}",
                json.dumps({"payment_id": payment_id, "user_id": payment["user_id"]}),
                to_iso(now),
            ),
        )
        conn.commit()
    logger.info("Payment %s %s by admin", payment_id, decision.lower())
    return True, "reviewed"


def get_payment_status(user_id: int) -> list[dict[str, Any]]:
    with connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM payments WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
                (user_id,),
            ).fetchall()
        ]


def has_pending_payment(user_id: int) -> bool:
    with connection() as conn:
        return bool(
            conn.execute(
                "SELECT 1 FROM payments WHERE user_id=? AND status='PENDING' LIMIT 1",
                (user_id,),
            ).fetchone()
        )


def is_premium(user_id: int) -> bool:
    now = to_iso(utc_now())
    with connection() as conn:
        row = conn.execute(
            """SELECT s.status,s.expires_at,u.status AS user_status
               FROM subscriptions s JOIN users u ON u.user_id=s.user_id
               WHERE s.user_id=?""",
            (user_id,),
        ).fetchone()
        pending = conn.execute(
            "SELECT 1 FROM payments WHERE user_id=? AND status='PENDING' LIMIT 1",
            (user_id,),
        ).fetchone()
    if not row or row["status"] != "active" or row["user_status"] != "active" or pending:
        return False
    return row["expires_at"] is None or row["expires_at"] > now


def subscription_info(user_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """SELECT s.*,p.label FROM subscriptions s LEFT JOIN plans p
               ON p.plan_key=s.plan_key WHERE s.user_id=?""",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def set_user_status(user_id: int, status: str, actor_id: int) -> bool:
    if status not in {"active", "suspended", "revoked"}:
        return False
    with connection() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not exists:
            return False
        if status == "revoked":
            conn.execute(
                "UPDATE subscriptions SET status='revoked',updated_at=? WHERE user_id=?",
                (to_iso(utc_now()), user_id),
            )
        else:
            conn.execute("UPDATE users SET status=? WHERE user_id=?", (status, user_id))
            if status == "suspended":
                conn.execute(
                    "UPDATE subscriptions SET status='suspended',updated_at=? WHERE user_id=?",
                    (to_iso(utc_now()), user_id),
                )
            elif status == "active":
                conn.execute(
                    """UPDATE subscriptions SET status='active',updated_at=?
                       WHERE user_id=? AND (expires_at IS NULL OR expires_at>?)""",
                    (to_iso(utc_now()), user_id, to_iso(utc_now())),
                )
    log_event(f"user.{status}", actor_id, user_id=user_id)
    return True


def grant_subscription(
    user_id: int, plan_key: str, admin_id: int, days_override: int | None = None
) -> tuple[bool, str]:
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT status FROM users WHERE user_id=?", (user_id,)).fetchone()
        plan = conn.execute("SELECT * FROM plans WHERE plan_key=?", (plan_key,)).fetchone()
        if not user:
            conn.execute(
                "INSERT INTO users(user_id,first_name,created_at) VALUES(?,?,?)",
                (user_id, "Subscriber", to_iso(utc_now())),
            )
            conn.execute("INSERT OR IGNORE INTO preferences(user_id) VALUES(?)", (user_id,))
        if not plan:
            conn.rollback()
            return False, "plan not found"
        now = utc_now()
        days = int(plan["duration_days"] if days_override is None else days_override)
        expires = None if days == 0 else now + timedelta(days=days)
        conn.execute(
            """INSERT INTO subscriptions(user_id,plan_key,status,started_at,expires_at,updated_at)
               VALUES(?,?,'active',?,?,?) ON CONFLICT(user_id) DO UPDATE SET
               plan_key=excluded.plan_key,status='active',started_at=excluded.started_at,
               expires_at=excluded.expires_at,updated_at=excluded.updated_at""",
            (user_id, plan_key, to_iso(now), to_iso(expires), to_iso(now)),
        )
        conn.execute("UPDATE users SET status='active' WHERE user_id=?", (user_id,))
    log_event("subscription.granted", admin_id, user_id=user_id, plan=plan_key, days=days)
    return True, "granted"


def extend_subscription(user_id: int, days: int, admin_id: int) -> tuple[bool, str]:
    if days < 1 or days > 3650:
        return False, "days must be from 1 to 3650"
    with connection() as conn:
        sub = conn.execute("SELECT * FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()
        if not sub:
            return False, "subscription not found"
        now = utc_now()
        if sub["expires_at"] is None and sub["status"] == "active":
            return False, "lifetime subscription does not expire"
        expiry = parse_dt(sub["expires_at"])
        new_expiry = max(expiry, now) + timedelta(days=days) if expiry else now + timedelta(days=days)
        conn.execute(
            "UPDATE subscriptions SET expires_at=?,status='active',updated_at=? WHERE user_id=?",
            (to_iso(new_expiry), to_iso(now), user_id),
        )
        conn.execute("UPDATE users SET status='active' WHERE user_id=?", (user_id,))
    log_event("subscription.extended", admin_id, user_id=user_id, days=days)
    return True, to_iso(new_expiry) or ""


def get_preference(user_id: int) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM preferences WHERE user_id=?", (user_id,)
        ).fetchone()
    return dict(row) if row else {
        "user_id": user_id,
        "environment": "testnet",
        "auto_trading": 0,
        "notifications": 1,
        "risk_pct": 0.5,
        "trailing_stop": 0,
    }


def set_preference(user_id: int, key: str, value: object) -> None:
    allowed = {"environment", "auto_trading", "notifications", "risk_pct", "trailing_stop"}
    if key not in allowed:
        raise ValueError("invalid preference")
    with connection() as conn:
        conn.execute("INSERT OR IGNORE INTO preferences(user_id) VALUES(?)", (user_id,))
        conn.execute(f"UPDATE preferences SET {key}=? WHERE user_id=?", (value, user_id))
    log_event("preference.updated", user_id, preference=key, value=value)


def store_credentials(
    user_id: int, environment: str, encrypted_key: str, encrypted_secret: str
) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO user_secrets(user_id,environment,key_cipher,secret_cipher,updated_at)
               VALUES(?,?,?,?,?) ON CONFLICT(user_id,environment) DO UPDATE SET
               key_cipher=excluded.key_cipher,secret_cipher=excluded.secret_cipher,
               updated_at=excluded.updated_at""",
            (user_id, environment, encrypted_key, encrypted_secret, to_iso(utc_now())),
        )
    log_event("credentials.saved", user_id, environment=environment)


def get_encrypted_credentials(user_id: int, environment: str) -> tuple[str, str] | None:
    with connection() as conn:
        row = conn.execute(
            """SELECT key_cipher,secret_cipher FROM user_secrets
               WHERE user_id=? AND environment=?""",
            (user_id, environment),
        ).fetchone()
    return (row["key_cipher"], row["secret_cipher"]) if row else None


def delete_credentials(user_id: int, environment: str) -> bool:
    with connection() as conn:
        cur = conn.execute(
            "DELETE FROM user_secrets WHERE user_id=? AND environment=?",
            (user_id, environment),
        )
    log_event("credentials.deleted", user_id, environment=environment)
    return cur.rowcount > 0


def store_signal(signal: dict[str, Any]) -> int | None:
    created = utc_now()
    created_at = to_iso(created)
    try:
        max_age = max(1, int(float(get_setting("max_signal_age_minutes", "90") or 90)))
    except (TypeError, ValueError, OverflowError):
        max_age = 90
    expires_at = to_iso(created + timedelta(minutes=max_age))
    zone = signal.get("zone") or {}
    entry_low = signal.get("entry_low", zone.get("low"))
    entry_high = signal.get("entry_high", zone.get("high"))
    entry_zone = json.dumps({"low": entry_low, "high": entry_high})
    structure = signal.get("structure") or signal.get("reason", "")
    with connection() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO signals(
                   symbol,direction,side,setup_hash,status,entry,entry_low,entry_high,entry_zone,
                   stop,tp1,tp2,tp3,confidence,strategy,timeframe,structure,sweep_time,bos_time,reason,
                   created_at,updated_at,expires_at
                   ) VALUES(?,?,?,?,'NEW',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    signal["symbol"],
                    signal["direction"],
                    signal["direction"],
                    signal["setup_hash"],
                    signal["entry"],
                    entry_low,
                    entry_high,
                    entry_zone,
                    signal["stop"],
                    signal["tp1"],
                    signal["tp2"],
                    signal["tp3"],
                    float(signal.get("confidence", 0) or 0),
                    str(signal.get("strategy", "SMC"))[:80],
                    str(signal.get("timeframe", "4H / 1H / 5m"))[:80],
                    str(structure)[:1000],
                    signal.get("sweep_time"),
                    signal.get("bos_time"),
                    signal["reason"][:1000],
                    created_at,
                    created_at,
                    expires_at,
                ),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None


def update_signal_status(signal_id: int, status: str, reason: str | None = None) -> None:
    now = to_iso(utc_now())
    with connection() as conn:
        if reason is None:
            conn.execute(
                "UPDATE signals SET status=?,updated_at=? WHERE signal_id=?",
                (status, now, signal_id),
            )
        else:
            conn.execute(
                "UPDATE signals SET status=?,reason=?,updated_at=? WHERE signal_id=?",
                (status, reason[:1000], now, signal_id),
            )


def recent_signals(limit: int = 10) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
                "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?",
                (min(max(limit, 1), 50),),
            ).fetchall()
    return [_signal_dict(row) for row in rows]


def active_signals(limit: int = 50) -> list[dict[str, Any]]:
    now = to_iso(utc_now())
    with connection() as conn:
        rows = conn.execute(
            """SELECT * FROM signals
               WHERE status IN ('NEW','VALIDATING','EXECUTING')
                 AND (expires_at IS NULL OR expires_at>?)
               ORDER BY created_at DESC LIMIT ?""",
            (now, min(max(limit, 1), 100)),
        ).fetchall()
    return [_signal_dict(row) for row in rows]


def _signal_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["side"] = result.get("side") or result.get("direction")
    try:
        result["entry_zone"] = json.loads(result.get("entry_zone") or "")
    except (ValueError, TypeError):
        result["entry_zone"] = {
            "low": result.get("entry_low"),
            "high": result.get("entry_high"),
        }
    return result


def record_scan(symbol: str, outcome: str, reason: str) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO scan_results(symbol,outcome,reason,scanned_at) VALUES(?,?,?,?)",
            (symbol, outcome[:40], reason[:500], to_iso(utc_now())),
        )


def prune_scan_results(days: int = 14) -> None:
    cutoff = to_iso(utc_now() - timedelta(days=days))
    with connection() as conn:
        conn.execute("DELETE FROM scan_results WHERE scanned_at < ?", (cutoff,))


def latest_scan_summary() -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            """SELECT MAX(scanned_at) AS scanned_at,COUNT(*) AS count,
               SUM(CASE WHEN outcome='SIGNAL' THEN 1 ELSE 0 END) AS signals,
               SUM(CASE WHEN outcome='ERROR' THEN 1 ELSE 0 END) AS errors
               FROM scan_results WHERE scanned_at >= ?""",
            (to_iso(utc_now() - timedelta(minutes=10)),),
        ).fetchone()
    return dict(row) if row else {"scanned_at": None, "count": 0, "signals": 0, "errors": 0}


def add_trade(
    user_id: int,
    signal_id: int,
    setup_hash: str,
    environment: str,
    symbol: str,
    direction: str,
    reason: str | None = None,
) -> int | None:
    with connection() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO trades(user_id,signal_id,setup_hash,environment,symbol,direction,side,
                   status,reason,created_at) VALUES(?,?,?,?,?,?,?,'SUBMITTING',?,?)""",
                (
                    user_id,
                    signal_id,
                    setup_hash,
                    environment,
                    symbol,
                    direction,
                    direction,
                    reason[:1000] if reason else None,
                    to_iso(utc_now()),
                ),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None


def record_trade_rejection(user_id: int, signal_id: int, reason: str) -> None:
    signal = get_signal(signal_id)
    if not signal:
        return
    preference = get_preference(user_id)
    with connection() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO trades(
                   user_id,signal_id,setup_hash,environment,symbol,direction,side,status,
                   reason,close_reason,created_at,closed_at
               ) VALUES(?,?,?,?,?,?,?,'REJECTED',?,?,?,?)""",
            (
                user_id,
                signal_id,
                signal["setup_hash"],
                preference.get("environment", "testnet"),
                signal["symbol"],
                signal.get("direction") or "UNKNOWN",
                signal.get("side") or signal.get("direction") or "UNKNOWN",
                reason[:500],
                reason[:500],
                signal.get("created_at") or to_iso(utc_now()),
                to_iso(utc_now()),
            ),
        )


def update_trade(trade_id: int, **fields: Any) -> None:
    allowed = {
        "status", "entry_order_id", "stop_order_id", "exchange_order_ids", "quantity",
        "remaining_quantity", "entry_price",
        "sl_order_id", "sl_actual_order_id", "stop_price", "tp_order_ids", "tp_actual_order_ids",
        "tp1_order_id", "tp2_order_id", "tp3_order_id", "tp1_actual_order_id",
        "tp2_actual_order_id", "tp3_actual_order_id", "tp1_realized_pnl", "tp2_realized_pnl",
        "tp3_realized_pnl", "protection_error", "best_price", "realized_pnl", "pnl_verified", "closed_at",
        "exit_price", "reason", "close_reason", "tp1_hit", "tp2_hit", "tp3_hit", "tp1_at", "tp2_at",
        "tp3_at", "telegram_chat_id", "telegram_message_id",
        "executed_at",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    assignments = ",".join(f"{key}=?" for key in updates)
    with connection() as conn:
        conn.execute(
            f"UPDATE trades SET {assignments} WHERE trade_id=?",
            (*updates.values(), trade_id),
        )


def get_trade(trade_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE trade_id=?",
            (trade_id,),
        ).fetchone()
    return dict(row) if row else None


def active_trades(user_id: int) -> list[dict[str, Any]]:
    with connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM trades WHERE user_id=? AND status IN
                   ('SUBMITTING','OPEN','PROTECTED','TP1_HIT','TP2_HIT',
                    'PROTECTION_ERROR','PROTECTION_FAILED','UNKNOWN')
                   ORDER BY created_at DESC""",
                (user_id,),
            ).fetchall()
        ]


def all_active_trades(limit: int = 500) -> list[dict[str, Any]]:
    with connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM trades WHERE status IN
                   ('SUBMITTING','OPEN','PROTECTED','TP1_HIT','TP2_HIT',
                    'PROTECTION_ERROR','PROTECTION_FAILED','UNKNOWN')
                   ORDER BY created_at LIMIT ?""",
                (min(max(limit, 1), 1000),),
            ).fetchall()
        ]


def get_signal(signal_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM signals WHERE signal_id=?", (signal_id,)
        ).fetchone()
    return _signal_dict(row) if row else None


def premium_users_with_preferences() -> list[dict[str, Any]]:
    now = to_iso(utc_now())
    with connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT u.user_id,p.environment,p.auto_trading,p.notifications,p.risk_pct,
                          p.trailing_stop
                   FROM users u JOIN subscriptions s ON s.user_id=u.user_id
                   JOIN preferences p ON p.user_id=u.user_id
                   WHERE u.status='active' AND s.status='active'
                     AND (s.expires_at IS NULL OR s.expires_at>?)
                     AND NOT EXISTS (
                       SELECT 1 FROM payments pay WHERE pay.user_id=u.user_id
                         AND pay.status='PENDING'
                     )""",
                (now,),
            ).fetchall()
        ]


def all_users_with_preferences() -> list[dict[str, Any]]:
    """Return account owners and their selected environment for recovery scans."""
    with connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT u.user_id,p.environment
                   FROM users u JOIN preferences p ON p.user_id=u.user_id
                   ORDER BY u.user_id"""
            ).fetchall()
        ]


def recent_trades(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT t.*,s.stop AS signal_stop,s.tp1,s.tp2,s.tp3,
                          s.created_at AS signal_created_at,s.reason AS signal_reason
                   FROM trades t JOIN signals s ON s.signal_id=t.signal_id
                   WHERE t.user_id=? ORDER BY t.created_at DESC LIMIT ?""",
                (user_id, min(max(limit, 1), 50)),
            ).fetchall()
        ]


def active_signal_count() -> int:
    with connection() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM signals WHERE status IN ('NEW','VALIDATING','EXECUTING') "
                "AND (expires_at IS NULL OR expires_at>?)",
                (to_iso(utc_now()),),
            ).fetchone()[0]
        )


def user_performance(user_id: int) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            """SELECT SUM(CASE WHEN status!='REJECTED' THEN 1 ELSE 0 END) AS total_trades,
                      SUM(CASE WHEN status IN (
                          'CLOSED','COMPLETE','STOPPED','COMPLETED','SL_HIT','MANUALLY_CLOSED'
                      )
                               THEN 1 ELSE 0 END) AS closed_trades,
                      SUM(CASE WHEN pnl_verified=1 AND realized_pnl>0 THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN pnl_verified=1 THEN realized_pnl ELSE 0 END) AS realized_pnl,
                      SUM(CASE WHEN pnl_verified=1 THEN 1 ELSE 0 END) AS verified_trades
               FROM trades WHERE user_id=?""",
            (user_id,),
        ).fetchone()
    values = dict(row) if row else {}
    wins = int(values.get("wins") or 0)
    verified = int(values.get("verified_trades") or 0)
    values["win_rate"] = (wins / verified * 100) if verified else None
    return values


def user_recent_events(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT event_type,details,created_at FROM events "
                "WHERE actor_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, min(max(limit, 1), 50)),
            ).fetchall()
        ]


def daily_realized_pnl(user_id: int, environment: str) -> float:
    since = to_iso(utc_now().replace(hour=0, minute=0, second=0, microsecond=0))
    with connection() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(realized_pnl),0) AS pnl FROM trades
               WHERE user_id=? AND environment=? AND closed_at>=? AND pnl_verified=1""",
            (user_id, environment, since),
        ).fetchone()
    return float(row["pnl"] or 0)


def list_users(page: int = 0, limit: int = 20) -> list[dict[str, Any]]:
    offset = max(0, page) * limit
    with connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT u.*,s.status AS subscription_status,s.plan_key,s.expires_at,
                          COALESCE(p.auto_trading,0) AS auto_trading,
                          COALESCE(p.environment,'testnet') AS environment
                   FROM users u LEFT JOIN subscriptions s ON s.user_id=u.user_id
                   LEFT JOIN preferences p ON p.user_id=u.user_id
                   ORDER BY u.created_at DESC LIMIT ? OFFSET ?""",
                (min(limit, 50), offset),
            ).fetchall()
        ]


def admin_recent_trades(limit: int = 20) -> list[dict[str, Any]]:
    with connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM trades ORDER BY created_at DESC LIMIT ?""",
                (min(max(limit, 1), 50),),
            ).fetchall()
        ]


def admin_stats() -> dict[str, int]:
    with connection() as conn:
        return {
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "active_subscriptions": conn.execute(
                """SELECT COUNT(*) FROM subscriptions s JOIN users u ON u.user_id=s.user_id
                   WHERE s.status='active' AND u.status='active'
                   AND (s.expires_at IS NULL OR s.expires_at>?)""",
                (to_iso(utc_now()),),
            ).fetchone()[0],
            "pending_payments": conn.execute(
                "SELECT COUNT(*) FROM payments WHERE status='PENDING'"
            ).fetchone()[0],
            "open_trades": conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status IN "
                "('OPEN','PROTECTED','TP1_HIT','TP2_HIT','PROTECTION_ERROR','PROTECTION_FAILED','UNKNOWN')"
            ).fetchone()[0],
        }


def recent_events(limit: int = 15) -> list[dict[str, Any]]:
    with connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM events ORDER BY event_id DESC LIMIT ?",
                (min(max(limit, 1), 50),),
            ).fetchall()
        ]


def pending_payments(page: int = 0, limit: int = 10) -> list[dict[str, Any]]:
    with connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT p.*,u.username,u.first_name FROM payments p JOIN users u
                   ON u.user_id=p.user_id WHERE p.status='PENDING'
                   ORDER BY p.created_at LIMIT ? OFFSET ?""",
                (min(max(limit, 1), 20), max(0, page) * limit),
            ).fetchall()
        ]


def set_user_auto(user_id: int, enabled: bool, actor_id: int) -> None:
    with connection() as conn:
        conn.execute("INSERT OR IGNORE INTO preferences(user_id) VALUES(?)", (user_id,))
        conn.execute(
            "UPDATE preferences SET auto_trading=? WHERE user_id=?",
            (int(enabled), user_id),
        )
    log_event("user.auto_trading", actor_id, user_id=user_id, enabled=enabled)


def user_count() -> int:
    with connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

