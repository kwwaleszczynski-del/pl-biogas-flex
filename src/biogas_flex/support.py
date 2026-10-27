"""Simplified support schemes: what one MWh exported earns in each interval.

These are stylised versions, NOT a model of the Polish RES act. They have to be verified against the law in force
(ustawa o odnawialnych źródłach energii, art. 70a-70f and art. 93; see README for sources and dates):

    merchant   market price
    fit        fixed price F for every MWh (feed-in tariff like)
    fip        market price + premium max(0, F - mean market price of the calendar month) (sliding premium like)
    *_neg6     no support in runs of at least 6 consecutive hours with a negative hourly price
               (PL-like rule): the energy earns the market price instead
    *_neg1     no support in any interval with a negative price (stricter, like newer EU and German rules)

Negative runs are evaluated within the delivery day.

Usage:
    rate = export_rate("fip_neg6", prices, dt=0.25, premium=120.0, fixed_price=894.0)
"""
from __future__ import annotations

import numpy as np

SCENARIOS = ("merchant", "fit", "fit_neg6", "fit_neg1", "fip", "fip_neg6", "fip_neg1")


def hourly_mean(price: np.ndarray, dt: float) -> np.ndarray:
    per = int(round(1.0 / dt)) if dt < 1.0 else 1
    return np.asarray(price, dtype=float).reshape(-1, per).mean(axis=1)


def negative_mask(price: np.ndarray, dt: float, min_hours: int) -> np.ndarray:
    """True for intervals inside a run of >= ``min_hours`` hours with negative hourly mean price.
    ``min_hours`` <= 0 means: every interval with a negative price."""
    price = np.asarray(price, dtype=float)
    if min_hours <= 0:
        return price < 0
    per = int(round(1.0 / dt)) if dt < 1.0 else 1
    neg = hourly_mean(price, dt) < 0
    mask_h = np.zeros_like(neg)
    i = 0
    while i < len(neg):
        if neg[i]:
            j = i
            while j < len(neg) and neg[j]:
                j += 1
            if j - i >= min_hours:
                mask_h[i:j] = True
            i = j
        else:
            i += 1
    return np.repeat(mask_h, per)


def export_rate(scenario: str, price: np.ndarray, dt: float, premium: float, fixed_price: float,
                run_hours: int = 6) -> np.ndarray:
    """PLN per MWh exported, per interval."""
    price = np.asarray(price, dtype=float)
    base, _, rule = scenario.partition("_")
    if base == "merchant":
        return price.copy()
    if rule == "":
        mask = np.zeros(len(price), dtype=bool)
    elif rule == "neg6":
        mask = negative_mask(price, dt, run_hours)
    elif rule == "neg1":
        mask = negative_mask(price, dt, 0)
    else:
        raise ValueError(f"unknown scenario {scenario}")
    if base == "fit":
        return np.where(mask, price, fixed_price)
    if base == "fip":
        return price + np.where(mask, 0.0, premium)
    raise ValueError(f"unknown scenario {scenario}")


def monthly_premium(days, fixed_price: float) -> dict:
    """{(year, month): max(0, F - time-weighted mean market price of the month)}."""
    acc: dict[tuple[int, int], list[float]] = {}
    for d in days:
        k = (d.date.year, d.date.month)
        s = acc.setdefault(k, [0.0, 0.0])
        s[0] += float(np.sum(d.price) * d.dt)
        s[1] += d.n * d.dt
    return {k: max(0.0, fixed_price - v / h) for k, (v, h) in acc.items()}
