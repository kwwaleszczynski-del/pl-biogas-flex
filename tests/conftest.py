"""Synthetic price days for offline tests (no network, no dependence on pandas datetime resolution)."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from biogas_flex.data import Day, to_days


def duck_curve(hours: np.ndarray, depth: float = 300.0, seed: int = 0) -> np.ndarray:
    """Evening peak, midday solar dip (negative when deep enough)."""
    rng = np.random.default_rng(seed)
    p = 380 + 220 * np.sin((hours - 10) / 24 * 2 * np.pi) - depth * np.exp(-((hours - 13) / 2.5) ** 2)
    return p + rng.normal(0, 15, len(hours))


def quarter_frame(start: date, n_days: int, hourly_until: date | None = None, seed: int = 0) -> pd.DataFrame:
    """Quarter-hour price frame like data.load_pl_prices: hourly products repeated before ``hourly_until``."""
    t0 = pd.Timestamp(start).tz_localize("Europe/Warsaw").tz_convert("UTC")
    t1 = pd.Timestamp(start + timedelta(days=n_days)).tz_localize("Europe/Warsaw").tz_convert("UTC")
    idx = pd.date_range(t0, t1, freq="15min", inclusive="left")
    local = idx.tz_convert("Europe/Warsaw")
    hours = (local.hour + local.minute / 60).to_numpy(dtype=float)
    price = duck_curve(hours, seed=seed)
    bdate = np.array(local.date)
    if hourly_until is not None:
        hourly = pd.Series(price).groupby(np.arange(len(price)) // 4).transform("first").to_numpy()
        price = np.where(bdate < hourly_until, hourly, price)
    return pd.DataFrame({"start_utc": idx, "business_date": bdate, "price": price})


@pytest.fixture
def week_days() -> list[Day]:
    return to_days(quarter_frame(date(2025, 9, 26), 10, hourly_until=date(2025, 10, 1)))


@pytest.fixture
def hourly_day() -> Day:
    hours = np.arange(24, dtype=float)
    return Day(date(2025, 4, 15), np.zeros(24, dtype="datetime64[ns]"), 1.0, duck_curve(hours, depth=750.0),
               np.arange(24) * 4)
