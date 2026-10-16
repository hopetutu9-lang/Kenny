"""Rate-limited asynchronous USDT-M market scanner."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import pandas as pd

from binance_client import FuturesSession
from config import TIMEFRAME_INTERVALS
from database import (
    get_setting,
    log_event,
    prune_scan_results,
    record_scan,
    store_signal,
)
from logger import logger, safe_error_name
from strategy import analyze_smc

SignalCallback = Callable[[dict[str, Any], int], Awaitable[None]]


@dataclass
class CachedCandles:
    frame: pd.DataFrame
    cached_at: float


class MarketScanner:
    def __init__(self, signal_callback: SignalCallback) -> None:
        self.signal_callback = signal_callback
        self.stop_event = asyncio.Event()
        self._client_session: FuturesSession | None = None
        self._request_sem = asyncio.Semaphore(6)
        self._pace_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._cache: dict[tuple[str, str], CachedCandles] = {}
        self.last_started_at: float | None = None
        self.last_finished_at: float | None = None
        self.last_symbols_total = 0
        self.last_symbols_done = 0
        self.last_errors = 0
        self.last_signals = 0
        self.is_running = False
        self.last_error: str | None = None

    async def stop(self) -> None:
        self.stop_event.set()
        if self._client_session:
            await self._client_session.close()

    async def run(self) -> None:
        session = FuturesSession(testnet=False)
        self._client_session = session
        self.is_running = True
        logger.info("Public futures scanner worker started")
        try:
            while not self.stop_event.is_set():
                cycle_started = time.monotonic()
                self.last_started_at = cycle_started
                self.last_errors = self.last_signals = self.last_symbols_done = 0
                self.last_error = None
                try:
                    if get_setting("scanner_enabled", "1") == "1":
                        await self.scan_cycle()
                    else:
                        logger.info("Scanner paused by administrator")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_errors += 1
                    self.last_error = safe_error_name(exc)
                    logger.error("Scanner cycle failed error=%s", self.last_error)
                    log_event("scanner.cycle.error", None, error=self.last_error)
                self.last_finished_at = time.monotonic()
                elapsed = self.last_finished_at - cycle_started
                interval = self._setting_int("scan_interval_seconds", 60, 30, 600)
                delay = max(2.0, interval - elapsed)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.is_running = False
            await session.close()

    async def scan_cycle(self) -> None:
        assert self._client_session is not None
        client = await self._client_session.connect()
        metadata = await client.futures_exchange_info()
        ticker = await client.futures_ticker()
        tickers = {
            item["symbol"]: float(item.get("quoteVolume", 0) or 0)
            for item in ticker
            if item.get("symbol")
        }
        excluded = {
            item.strip().upper()
            for item in (get_setting("excluded_symbols", "") or "").split(",")
            if item.strip()
        }
        minimum_volume = self._setting_float("min_quote_volume_usdt", 10_000_000, 0, 1e12)
        symbols = [
            row["symbol"]
            for row in metadata.get("symbols", [])
            if row.get("status") == "TRADING"
            and row.get("contractType") == "PERPETUAL"
            and row.get("quoteAsset") == "USDT"
            and row.get("symbol") not in excluded
            and tickers.get(row["symbol"], 0) >= minimum_volume
        ]
        self.last_symbols_total = len(symbols)
        logger.info("Scanner cycle started eligible_symbols=%s", len(symbols))
        log_event("scanner.cycle.started", None, eligible_symbols=len(symbols))
        timeframes = self._timeframes()
        tasks = [self._scan_symbol(symbol, timeframes) for symbol in symbols]
        for batch_start in range(0, len(tasks), 40):
            batch = tasks[batch_start:batch_start + 40]
            results = await asyncio.gather(*batch, return_exceptions=True)
            for symbol, result in zip(symbols[batch_start:batch_start + 40], results):
                self.last_symbols_done += 1
                if isinstance(result, BaseException):
                    self.last_errors += 1
                    record_scan(symbol, "ERROR", safe_error_name(result))
                    logger.warning(
                        "Individual symbol scan failed symbol=%s error=%s",
                        symbol,
                        safe_error_name(result),
                    )
        self.last_finished_at = time.monotonic()
        duration = self.last_finished_at - (self.last_started_at or self.last_finished_at)
        logger.info(
            "Scanner cycle completed symbols=%s signals=%s errors=%s duration_s=%.1f",
            self.last_symbols_done,
            self.last_signals,
            self.last_errors,
            duration,
        )
        log_event(
            "scanner.cycle.completed",
            None,
            symbols=self.last_symbols_done,
            signals=self.last_signals,
            errors=self.last_errors,
            duration_seconds=round(duration, 1),
        )
        prune_scan_results(14)

    async def _scan_symbol(self, symbol: str, timeframes: tuple[str, ...]) -> None:
        assert self._client_session is not None
        client = await self._client_session.connect()
        frames: dict[str, pd.DataFrame] = {}
        try:
            for interval in timeframes:
                frames[interval] = await self._get_candles(client, symbol, interval)
            result = analyze_smc(symbol, frames)
            if not result.get("approved"):
                record_scan(symbol, "REJECTED", str(result.get("reason", "setup rejected")))
                return
            signal_id = store_signal(result)
            if signal_id is None:
                record_scan(symbol, "DUPLICATE", "setup already recorded")
                return
            self.last_signals += 1
            record_scan(symbol, "SIGNAL", result["reason"])
            logger.info(
                "SMC signal recorded symbol=%s direction=%s signal_id=%s",
                symbol,
                result["direction"],
                signal_id,
            )
            await self.signal_callback(result, signal_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record_scan(symbol, "ERROR", safe_error_name(exc))
            raise

    async def _get_candles(self, client: Any, symbol: str, interval: str) -> pd.DataFrame:
        key = (symbol, interval)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached.cached_at < 45:
            return cached.frame
        async with self._request_sem:
            async with self._pace_lock:
                wait_for = 0.075 - (time.monotonic() - self._last_request_at)
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
                self._last_request_at = time.monotonic()
            rows = await client.futures_klines(symbol=symbol, interval=interval, limit=180)
        now_ms = int(time.time() * 1000)
        completed = [row for row in rows if int(row[6]) < now_ms]
        if len(completed) < 35:
            raise ValueError("insufficient completed candles")
        frame = pd.DataFrame(
            {
                "open_time": [int(row[0]) for row in completed],
                "open": [float(row[1]) for row in completed],
                "high": [float(row[2]) for row in completed],
                "low": [float(row[3]) for row in completed],
                "close": [float(row[4]) for row in completed],
                "volume": [float(row[5]) for row in completed],
                "close_time": [int(row[6]) for row in completed],
            }
        )
        self._cache[key] = CachedCandles(frame=frame, cached_at=time.monotonic())
        return frame

    def _timeframes(self) -> tuple[str, ...]:
        try:
            parsed = json.loads(get_setting("timeframes", '["4h","1h","15m","5m"]') or "[]")
            selected = tuple(tf for tf in parsed if tf in TIMEFRAME_INTERVALS)
        except (ValueError, TypeError):
            selected = TIMEFRAME_INTERVALS
        return selected if set(selected) == set(TIMEFRAME_INTERVALS) else TIMEFRAME_INTERVALS

    @staticmethod
    def _setting_int(key: str, fallback: int, low: int, high: int) -> int:
        try:
            return min(max(int(get_setting(key, str(fallback)) or fallback), low), high)
        except (ValueError, TypeError):
            return fallback

    @staticmethod
    def _setting_float(key: str, fallback: float, low: float, high: float) -> float:
        try:
            return min(max(float(get_setting(key, str(fallback)) or fallback), low), high)
        except (ValueError, TypeError):
            return fallback

