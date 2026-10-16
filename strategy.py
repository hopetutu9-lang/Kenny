"""Deterministic, completed-candle SMC setup detection.

Signals require a 4H structural bias, a 1H liquidity sweep and confirmed
structure break, displacement, and a later 15m/5m order-block or FVG retest.
Nothing in this module submits exchange orders.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Swing:
    index: int
    price: float
    kind: str


def _valid_frame(frame: pd.DataFrame | None) -> bool:
    return (
        frame is not None
        and len(frame) >= 35
        and all(c in frame.columns for c in ("open", "high", "low", "close"))
    )


def confirmed_swings(frame: pd.DataFrame, wing: int = 2) -> tuple[list[Swing], list[Swing]]:
    """Strict fractal swings; a point is only returned after right-side bars exist."""
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    high_swings: list[Swing] = []
    low_swings: list[Swing] = []
    for i in range(wing, len(frame) - wing):
        left_h, right_h = highs[i - wing:i], highs[i + 1:i + wing + 1]
        left_l, right_l = lows[i - wing:i], lows[i + 1:i + wing + 1]
        if highs[i] > max(np.max(left_h), np.max(right_h)):
            high_swings.append(Swing(i, float(highs[i]), "HIGH"))
        if lows[i] < min(np.min(left_l), np.min(right_l)):
            low_swings.append(Swing(i, float(lows[i]), "LOW"))
    return high_swings, low_swings


def _trend_bias(frame: pd.DataFrame) -> tuple[str | None, str]:
    highs, lows = confirmed_swings(frame)
    if len(highs) < 2 or len(lows) < 2:
        return None, "insufficient confirmed 4H swings"
    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price
    if hh and hl:
        return "LONG", "4H higher-high / higher-low structure"
    if lh and ll:
        return "SHORT", "4H lower-high / lower-low structure"
    return None, "4H structure is mixed; no directional bias"


def _average_true_range(frame: pd.DataFrame, length: int = 14) -> float:
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)
    prev = np.roll(close, 1)
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    if len(tr) > 0:
        tr[0] = high[0] - low[0]
    return float(np.mean(tr[-length:]))


def _liquidity_break(
    frame: pd.DataFrame, direction: str
) -> tuple[dict[str, Any] | None, str]:
    highs, lows = confirmed_swings(frame)
    if len(highs) < 2 or len(lows) < 2:
        return None, "1H structure lacks confirmed liquidity levels"
    opens = frame["open"].to_numpy(dtype=float)
    hs = frame["high"].to_numpy(dtype=float)
    ls = frame["low"].to_numpy(dtype=float)
    closes = frame["close"].to_numpy(dtype=float)
    bodies = np.abs(closes - opens)
    avg_body = float(np.median(bodies[-24:-1])) if len(bodies) > 24 else float(np.median(bodies[:-1]))
    if avg_body <= 0:
        avg_body = max(_average_true_range(frame) * 0.08, 1e-10)

    first = max(3, len(frame) - 24)
    for i in range(len(frame) - 2, first - 1, -1):
        # A swing only becomes available after its right-side confirmation bars.
        known_highs = [s for s in highs if s.index < i - 2]
        known_lows = [s for s in lows if s.index < i - 2]
        if not known_highs or not known_lows:
            continue
        if direction == "LONG":
            level = known_lows[-1].price
            sweep = ls[i] < level and closes[i] > level
            structure = [s for s in known_highs if s.index < i]
            if not sweep or not structure:
                continue
            bos_level = structure[-1].price
            bos_index = next(
                (
                    j for j in range(i + 1, len(frame) - 1)
                    if closes[j] > bos_level and bodies[j] >= avg_body * 1.15
                ),
                None,
            )
            if bos_index is not None:
                return {
                    "sweep_index": i,
                    "sweep_level": level,
                    "sweep_extreme": float(ls[i]),
                    "bos_index": bos_index,
                    "bos_level": bos_level,
                    "sweep_time": int(frame.iloc[i].get("open_time", i)),
                    "bos_time": int(frame.iloc[bos_index].get("open_time", bos_index)),
                    "displacement": float(bodies[bos_index]),
                }, "1H sell-side sweep reclaimed, then bullish displacement BOS"
        else:
            level = known_highs[-1].price
            sweep = hs[i] > level and closes[i] < level
            structure = [s for s in known_lows if s.index < i]
            if not sweep or not structure:
                continue
            bos_level = structure[-1].price
            bos_index = next(
                (
                    j for j in range(i + 1, len(frame) - 1)
                    if closes[j] < bos_level and bodies[j] >= avg_body * 1.15
                ),
                None,
            )
            if bos_index is not None:
                return {
                    "sweep_index": i,
                    "sweep_level": level,
                    "sweep_extreme": float(hs[i]),
                    "bos_index": bos_index,
                    "bos_level": bos_level,
                    "sweep_time": int(frame.iloc[i].get("open_time", i)),
                    "bos_time": int(frame.iloc[bos_index].get("open_time", bos_index)),
                    "displacement": float(bodies[bos_index]),
                }, "1H buy-side sweep rejected, then bearish displacement BOS"
    return None, "no recent reclaimed liquidity sweep followed by confirmed displacement BOS"


def _equal_levels(frame: pd.DataFrame) -> tuple[int, int]:
    highs, lows = confirmed_swings(frame)

    def count(levels: list[Swing]) -> int:
        if len(levels) < 2:
            return 0
        last = levels[-1].price
        return sum(
            abs(item.price - last) / max(abs(last), 1e-12) <= 0.0015
            for item in levels[-5:-1]
        )

    return count(highs), count(lows)


def _retset_zone(
    frame: pd.DataFrame, direction: str, preferred_types: tuple[str, ...] = ("FVG", "OB")
) -> dict[str, Any] | None:
    if not _valid_frame(frame):
        return None
    opens = frame["open"].to_numpy(dtype=float)
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    closes = frame["close"].to_numpy(dtype=float)
    n = len(frame)
    current = n - 1
    candidates: list[dict[str, Any]] = []
    start = max(2, n - 18)
    for i in range(start, n - 2):
        # Three-candle fair-value gap. Retest must be on a later completed candle.
        if direction == "LONG" and lows[i] > highs[i - 2]:
            zone_low, zone_high = highs[i - 2], lows[i]
            for j in range(i + 1, n):
                touched = lows[j] <= zone_high and highs[j] >= zone_low
                rejected = closes[j] > (zone_low + zone_high) / 2 and closes[j] > opens[j]
                if touched and rejected:
                    candidates.append(
                        {"low": zone_low, "high": zone_high, "type": "FVG", "index": j}
                    )
                    break
        elif direction == "SHORT" and highs[i] < lows[i - 2]:
            zone_low, zone_high = highs[i], lows[i - 2]
            for j in range(i + 1, n):
                touched = highs[j] >= zone_low and lows[j] <= zone_high
                rejected = closes[j] < (zone_low + zone_high) / 2 and closes[j] < opens[j]
                if touched and rejected:
                    candidates.append(
                        {"low": zone_low, "high": zone_high, "type": "FVG", "index": j}
                    )
                    break

        # Last opposing candle before local displacement is a candidate OB.
        if i < n - 5:
            displacement = (closes[i + 2] - opens[i + 2]) if direction == "LONG" else (opens[i + 2] - closes[i + 2])
            prev_bodies = np.abs(closes[max(0, i - 16):i + 2] - opens[max(0, i - 16):i + 2])
            base = float(np.median(prev_bodies)) if len(prev_bodies) else 0
            opposing = (closes[i] < opens[i]) if direction == "LONG" else (closes[i] > opens[i])
            if opposing and base > 0 and displacement >= base * 1.25:
                zone_low, zone_high = lows[i], highs[i]
                for j in range(i + 3, n):
                    touched = lows[j] <= zone_high and highs[j] >= zone_low
                    rejected = (
                        closes[j] > zone_low and closes[j] > opens[j]
                        if direction == "LONG"
                        else closes[j] < zone_high and closes[j] < opens[j]
                    )
                    if touched and rejected:
                        candidates.append(
                            {"low": zone_low, "high": zone_high, "type": "OB", "index": j}
                        )
                        break
    if not candidates:
        return None
    candidates.sort(key=lambda zone: (zone["index"], preferred_types.index(zone["type"])))
    selected = candidates[-1]
    latest_price = closes[current]
    zone_width = selected["high"] - selected["low"]
    max_distance = max(zone_width * 2.5, latest_price * 0.0025)
    if abs(latest_price - (selected["low"] + selected["high"]) / 2) > max_distance:
        return None
    return selected


def _timestamp(frame: pd.DataFrame, index: int) -> int:
    value = frame.iloc[index].get("open_time", index)
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return index


def analyze_smc(symbol: str, frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Return a signal dict or a rejection with a precise reason."""
    required = {"4h", "1h", "15m", "5m"}
    if not required.issubset(frames):
        return {"approved": False, "reason": "required 4H/1H/15M/5M candles unavailable"}
    if any(not _valid_frame(frames[tf]) for tf in required):
        return {"approved": False, "reason": "insufficient completed candles"}

    bias, bias_reason = _trend_bias(frames["4h"])
    if not bias:
        return {"approved": False, "reason": bias_reason}
    structure, structure_reason = _liquidity_break(frames["1h"], bias)
    if not structure:
        return {"approved": False, "reason": f"{bias_reason}; {structure_reason}"}

    zone = None
    zone_tf = ""
    for timeframe in ("5m", "15m"):
        zone = _retset_zone(frames[timeframe], bias)
        if zone:
            zone_tf = timeframe
            break
    if not zone:
        return {
            "approved": False,
            "reason": f"{bias_reason}; {structure_reason}; no completed 15M/5M OB/FVG retest",
        }

    entry = float(frames["5m"].iloc[-1]["close"])
    atr = _average_true_range(frames["1h"])
    pad = max(atr * 0.1, entry * 0.0005)
    stop = min(structure["sweep_extreme"], zone["low"]) - pad if bias == "LONG" else max(structure["sweep_extreme"], zone["high"]) + pad
    risk = abs(entry - stop)
    if entry <= 0 or risk <= 0 or risk / entry > 0.03:
        return {"approved": False, "reason": "stop distance is invalid or exceeds 3% of entry"}
    min_rr = 1.5
    targets = (
        (entry + risk * 1.5, entry + risk * 2.0, entry + risk * 3.0)
        if bias == "LONG"
        else (entry - risk * 1.5, entry - risk * 2.0, entry - risk * 3.0)
    )
    swing_high_count, swing_low_count = _equal_levels(frames["1h"])
    timestamp = _timestamp(frames["1h"], int(structure["sweep_index"]))
    zone_timestamp = _timestamp(frames[zone_tf], int(zone["index"]))
    raw_hash = f"{symbol}|{bias}|{timestamp}|{structure['bos_time']}|{zone_tf}|{zone['type']}|{zone_timestamp}"
    setup_hash = hashlib.sha256(raw_hash.encode("utf-8")).hexdigest()[:32]
    detail = (
        f"{bias_reason}; {structure_reason}; {zone_tf} {zone['type']} retest confirmed; "
        f"equal-high clusters={swing_high_count}, equal-low clusters={swing_low_count}; "
        f"risk/reward targets 1.5R/2R/3R"
    )
    # This is a transparent confluence score, not a statistically calibrated
    # probability of winning. Each point comes from an observed setup component.
    confidence = min(
        95,
        70
        + min(8, 2 * (swing_high_count + swing_low_count))
        + (2 if zone["type"] == "FVG" else 0),
    )
    return {
        "approved": True,
        "symbol": symbol,
        "direction": bias,
        "entry": entry,
        "entry_low": float(zone["low"]),
        "entry_high": float(zone["high"]),
        "stop": float(stop),
        "tp1": float(targets[0]),
        "tp2": float(targets[1]),
        "tp3": float(targets[2]),
        "confidence": float(confidence),
        "strategy": "SMC",
        "timeframe": "4H / 1H / 5m",
        "structure": (
            f"4H: {bias_reason.removeprefix('4H ')} | "
            f"1H: {structure_reason} | {zone_tf}: {zone['type']} retest"
        ),
        "setup_hash": setup_hash,
        "reason": detail,
        "sweep_time": int(structure["sweep_time"]),
        "bos_time": int(structure["bos_time"]),
        "zone": {
            "timeframe": zone_tf,
            "type": zone["type"],
            "low": float(zone["low"]),
            "high": float(zone["high"]),
        },
        "minimum_rr": min_rr,
    }

