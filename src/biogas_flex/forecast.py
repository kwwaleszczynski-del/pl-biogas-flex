"""As-of price forecast for the realizable strategies: mean daily profile of the last seven delivery days.

The schedule for delivery day D has to be bid into the day-ahead auction before gate closure at 12:00 on D-1.
At that moment the prices of D-1 and earlier are known (published around 13:00 on D-2), the prices of D are
not. The forecast for D is therefore the mean price per local quarter-hour of the clock over D-7..D-1 and never
uses a price of D or later. A sibling project (pl-bess-paper-trading) found this simple profile as good as a
gradient-boosting model for ranking the hours of a day.

Usage:
    fc = profile_forecast(days)            # {date: array in the resolution of that day}
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np

from biogas_flex.data import Day


def quarter_slots(day: Day) -> np.ndarray:
    """Local clock quarter-hour (0..95) of every quarter-hour of the day."""
    if day.dt == 1.0:
        return (np.repeat(day.slot, 4) + np.tile(np.arange(4), day.n)).astype(int)
    return day.slot.astype(int)


def slot_profile(day: Day) -> np.ndarray:
    """96 local quarter-hour prices of a day (NaN where the clock time does not exist; the repeated autumn hour
    keeps its second occurrence)."""
    out = np.full(96, np.nan)
    out[quarter_slots(day)] = day.quarter_prices()
    return out


def forecast_for(day: Day, history: list[np.ndarray]) -> np.ndarray:
    """Forecast in the resolution of ``day`` from slot profiles of earlier days."""
    prof = np.nanmean(np.vstack(history), axis=0)
    prof = np.where(np.isnan(prof), np.nanmean(prof), prof)
    q = prof[quarter_slots(day)]
    return q.reshape(-1, 4).mean(axis=1) if day.dt == 1.0 else q


def profile_forecast(days: list[Day], window: int = 7) -> dict:
    """{date: forecast} for every day that has at least one known day in the ``window`` days before it."""
    profiles = {d.date: slot_profile(d) for d in days}
    out = {}
    for d in days:
        hist = [profiles[d.date - timedelta(days=k)] for k in range(1, window + 1)
                if d.date - timedelta(days=k) in profiles]
        if hist:
            out[d.date] = forecast_for(d, hist)
    return out


def trailing_mean(days: list[Day], window: int = 30) -> dict:
    """{date: mean price over the ``window`` days before it} (known at gate closure), for premium estimates."""
    daily = {d.date: (float(np.sum(d.price) * d.dt), d.n * d.dt) for d in days}
    out = {}
    for d in days:
        vals = [daily[d.date - timedelta(days=k)] for k in range(1, window + 1) if d.date - timedelta(days=k) in daily]
        if vals:
            out[d.date] = sum(v for v, _ in vals) / sum(h for _, h in vals)
    return out
