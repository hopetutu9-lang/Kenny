"""Deterministic position sizing, exchange-filter rounding, and risk gates."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation
from typing import Any

from database import get_setting


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    quantity: float = 0.0
    notional: float = 0.0
    risk_amount: float = 0.0


def decimal_floor(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    try:
        v, s = Decimal(str(value)), Decimal(str(step))
        return float((v / s).to_integral_value(rounding=ROUND_DOWN) * s)
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return 0.0


def decimal_ceil(value: float, tick: float) -> float:
    if tick <= 0:
        return float(value)
    try:
        v, t = Decimal(str(value)), Decimal(str(tick))
        return float((v / t).to_integral_value(rounding=ROUND_UP) * t)
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return 0.0


def _float_setting(key: str, fallback: float) -> float:
    try:
        return float(get_setting(key, str(fallback)) or fallback)
    except (TypeError, ValueError):
        return fallback


def _filters(symbol_info: dict[str, Any]) -> tuple[float, float, float, float]:
    lot_step, min_qty, tick_size, min_notional = 0.0, 0.0, 0.0, 0.0
    for filt in symbol_info.get("filters", []):
        kind = filt.get("filterType")
        if kind == "LOT_SIZE":
            lot_step = float(filt.get("stepSize", 0))
            min_qty = float(filt.get("minQty", 0))
        elif kind == "MARKET_LOT_SIZE":
            market_step = float(filt.get("stepSize", 0))
            market_min = float(filt.get("minQty", 0))
            if market_step:
                lot_step, min_qty = market_step, market_min
        elif kind == "PRICE_FILTER":
            tick_size = float(filt.get("tickSize", 0))
        elif kind in {"MIN_NOTIONAL", "NOTIONAL"}:
            min_notional = float(filt.get("notional", filt.get("minNotional", 0)))
    return lot_step, min_qty, tick_size, min_notional


def calculate_position(
    *,
    equity: float,
    available_balance: float,
    entry: float,
    stop: float,
    direction: str,
    risk_pct: float,
    symbol_info: dict[str, Any],
    daily_realized_pnl: float = 0.0,
    open_position_count: int = 0,
    fee_rate: float = 0.0005,
) -> RiskDecision:
    """Size from stop risk and reject rather than force a minimum lot size."""
    if direction not in {"LONG", "SHORT"}:
        return RiskDecision(False, "invalid direction")
    if min(equity, available_balance, entry, stop, risk_pct) <= 0:
        return RiskDecision(False, "account equity, available margin, entry, stop, and risk must be positive")
    if direction == "LONG" and stop >= entry:
        return RiskDecision(False, "long stop must be below entry")
    if direction == "SHORT" and stop <= entry:
        return RiskDecision(False, "short stop must be above entry")
    max_positions = int(_float_setting("max_open_positions", 3))
    if open_position_count >= max_positions:
        return RiskDecision(False, "maximum simultaneous positions reached")
    leverage = min(3.0, _float_setting("max_leverage", 3.0))
    risk_limit_pct = _float_setting("risk_per_trade_pct", 0.5)
    safe_risk_pct = min(max(0.0, risk_pct), risk_limit_pct)
    risk_amount = equity * safe_risk_pct / 100.0
    loss_limit = equity * _float_setting("daily_loss_limit_pct", 2.0) / 100.0
    if daily_realized_pnl <= -loss_limit:
        return RiskDecision(False, "daily loss limit reached")
    stop_distance = abs(entry - stop)
    # A conservative round-trip fee estimate is included in the per-unit risk.
    per_unit_risk = stop_distance + entry * fee_rate * 2
    if per_unit_risk <= 0:
        return RiskDecision(False, "invalid stop distance")
    raw_qty = risk_amount / per_unit_risk
    max_notional = max(0.0, available_balance) * leverage
    raw_qty = min(raw_qty, max_notional / entry if entry else 0.0)
    step, min_qty, _tick, min_notional = _filters(symbol_info)
    quantity = decimal_floor(raw_qty, step)
    notional = quantity * entry
    if quantity <= 0 or quantity < min_qty:
        return RiskDecision(False, "risk-sized quantity is below the exchange minimum; quantity was not increased")
    if notional < min_notional:
        return RiskDecision(False, "risk-sized order is below minimum notional; quantity was not increased")
    if quantity * stop_distance + notional * fee_rate * 2 > risk_amount * 1.000001:
        return RiskDecision(False, "rounded quantity would exceed the configured risk amount")
    if notional / leverage > available_balance * 1.000001:
        return RiskDecision(False, "insufficient available margin")
    return RiskDecision(True, "risk checks passed", quantity, notional, risk_amount)

