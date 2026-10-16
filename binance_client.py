"""Encrypted credential storage and isolated Binance USD-M Futures clients."""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass
from typing import Any

from config import KEY_PATH, ON_RAILWAY, VOLUME_PATH
from database import get_encrypted_credentials, store_credentials
from logger import logger, safe_error_name

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - dependency installation issue
    Fernet = None  # type: ignore[assignment,misc]
    InvalidToken = Exception  # type: ignore[assignment,misc]


class CredentialVault:
    """Fernet encryption with a separate, permission-restricted key file."""

    def __init__(self) -> None:
        self._fernet: Any | None = None
        self.available = False
        self.reason = "not initialized"

    def initialize(self) -> bool:
        if Fernet is None:
            self.reason = "cryptography dependency unavailable"
            logger.error("Credential encryption unavailable")
            return False
        if ON_RAILWAY and not VOLUME_PATH:
            self.reason = "Railway persistent volume is not mounted"
            logger.error("Credential storage disabled: persistent Railway volume required")
            return False
        try:
            KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(KEY_PATH, flags)
            except FileNotFoundError:
                key = Fernet.generate_key()
                create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    create_flags |= os.O_NOFOLLOW
                fd = os.open(KEY_PATH, create_flags, stat.S_IRUSR | stat.S_IWUSR)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(key)
                    handle.flush()
                    os.fsync(handle.fileno())
                fd = os.open(KEY_PATH, flags)
            with os.fdopen(fd, "rb") as handle:
                key = handle.read(256)
            if len(key) != 44:
                self.reason = "invalid encryption key file"
                logger.error("Credential storage disabled: encryption key invalid")
                return False
            os.chmod(KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)
            self._fernet = Fernet(key)
            self.available = True
            self.reason = "ready"
            return True
        except Exception as exc:
            self.reason = safe_error_name(exc)
            logger.error("Credential storage disabled during encryption initialization")
            return False

    def encrypt(self, value: str) -> str:
        if not self.available or self._fernet is None:
            raise RuntimeError("credential encryption is unavailable")
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not self.available or self._fernet is None:
            raise RuntimeError("credential encryption is unavailable")
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise RuntimeError("credential decryption failed") from exc

    def save_for_user(
        self, user_id: int, environment: str, api_key: str, api_secret: str
    ) -> None:
        if environment not in {"testnet", "real"}:
            raise ValueError("unsupported environment")
        encrypted_key = self.encrypt(api_key)
        encrypted_secret = self.encrypt(api_secret)
        store_credentials(user_id, environment, encrypted_key, encrypted_secret)

    def load_for_user(self, user_id: int, environment: str) -> tuple[str, str] | None:
        row = get_encrypted_credentials(user_id, environment)
        if not row:
            return None
        return self.decrypt(row[0]), self.decrypt(row[1])


@dataclass
class FuturesSession:
    """A per-user, per-environment client. Never share clients across users."""

    api_key: str | None = None
    api_secret: str | None = None
    testnet: bool = True
    client: Any | None = None

    async def connect(self) -> Any:
        if self.client is not None:
            return self.client
        try:
            from binance import AsyncClient

            self.client = await AsyncClient.create(
                api_key=self.api_key,
                api_secret=self.api_secret,
                testnet=self.testnet,
            )
            return self.client
        except Exception as exc:
            logger.warning(
                "Binance client connection failed environment=%s error=%s",
                "testnet" if self.testnet else "real",
                safe_error_name(exc),
            )
            raise

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.close_connection()
            finally:
                self.client = None

    async def __aenter__(self) -> "FuturesSession":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def verify_trading_access(self) -> dict[str, Any]:
        if not self.api_key or not self.api_secret:
            raise ValueError("API key and secret are required")
        client = await self.connect()
        account = await client.futures_account()
        if account.get("canTrade") is False:
            raise PermissionError("Futures trading is not enabled for this key")
        mode = await client.futures_get_position_mode()
        if bool(mode.get("dualSidePosition", False)):
            raise PermissionError("Hedge mode is not supported; use one-way position mode")
        return {
            "can_trade": bool(account.get("canTrade", True)),
            "one_way_mode": True,
        }

    async def usdt_equity(self) -> tuple[float, float]:
        client = await self.connect()
        balances = await client.futures_account_balance()
        usdt = next((item for item in balances if item.get("asset") == "USDT"), None)
        if not usdt:
            raise ValueError("USDT futures wallet not found")
        return float(usdt.get("balance", 0)), float(usdt.get("availableBalance", 0))

    async def exchange_symbol(self, symbol: str) -> dict[str, Any]:
        client = await self.connect()
        info = await client.futures_exchange_info()
        return next(
            (item for item in info.get("symbols", []) if item.get("symbol") == symbol),
            {},
        )


async def verify_credentials(
    api_key: str, api_secret: str, environment: str
) -> tuple[bool, str]:
    session = FuturesSession(
        api_key=api_key,
        api_secret=api_secret,
        testnet=environment == "testnet",
    )
    try:
        await asyncio.wait_for(session.verify_trading_access(), timeout=15)
        return True, "Connection verified; Futures trade access is enabled."
    except asyncio.TimeoutError:
        return False, "Binance did not respond in time. No credentials were saved."
    except PermissionError as exc:
        return False, str(exc)
    except Exception as exc:
        logger.warning(
            "Binance credential verification failed environment=%s error=%s",
            environment,
            safe_error_name(exc),
        )
        return False, "Connection could not be verified. Check the key, secret, and Futures permissions."
    finally:
        await session.close()

