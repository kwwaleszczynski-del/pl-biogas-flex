"""Backtest of operating strategies over a price history, settlement and annual economics.

Strategies:
    baseload        original engine at full load, no information needed
    rank_forecast   price-rank heuristic on the as-of forecast (realizable)
    rank_perfect    price-rank heuristic on the actual day-ahead prices (look-ahead)
    milp_forecast   daily MILP on the as-of forecast, settled at actual prices (realizable)
    milp_perfect    daily MILP on the actual prices (perfect foresight, upper bound of a day-by-day plan)

Every strategy is settled the same way at the actual prices: export x rate of the support scenario, import of
own consumption at market price + fee, maintenance per MWh, cost per engine start.

Usage:
    daily = run_strategy(days, forecasts, plant, "milp_forecast", "merchant")
    summary = annualise(daily)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from biogas_flex.config import PlantParams, SupportParams
from biogas_flex.data import Day
from biogas_flex.heuristic import simulate
from biogas_flex.milp import DaySchedule, optimize_day
from biogas_flex.support import export_rate, monthly_premium

log = logging.getLogger(__name__)

STRATEGIES = ("baseload", "rank_forecast", "rank_perfect", "milp_forecast", "milp_perfect")
SETTLEMENT_COLUMNS = ["net", "sales", "import_cost", "maintenance", "start_cost", "gen_mwh", "export_mwh",
                      "market_value", "neg_export_mwh", "starts", "flare_mwh", "boiler_mwh", "dump_mwh"]


def heat_demand(day: date, plant: PlantParams) -> float:
    """Seasonal heat obligation (MW): cosine between the winter (mid-January) and summer (mid-July) levels."""
    mid = 0.5 * (plant.heat_winter_frac + plant.heat_summer_frac)
    amp = 0.5 * (plant.heat_winter_frac - plant.heat_summer_frac)
    doy = day.timetuple().tm_yday
    return plant.rated_heat_mw * (mid + amp * math.cos(2 * math.pi * (doy - 15) / 365.25))


def settle(s: DaySchedule, price: np.ndarray, rate: np.ndarray, plant: PlantParams) -> dict:
    """Money and energy of one day's schedule at actual prices (PLN, MWh)."""
    dt = s.dt
    sales = float(np.sum(rate * s.export) * dt)
    import_cost = float(np.sum((price + plant.import_fee) * s.imports) * dt)
    maintenance = float(plant.maintenance * np.sum(s.power) * dt)
    start_cost = float(plant.start_cost * np.sum(s.starts))
    return {
        "net": sales - import_cost - maintenance - start_cost,
        "sales": sales,
        "import_cost": import_cost,
        "maintenance": maintenance,
        "start_cost": start_cost,
        "gen_mwh": float(np.sum(s.power) * dt),
        "export_mwh": float(np.sum(s.export) * dt),
        "market_value": float(np.sum(price * s.export) * dt),
        "neg_export_mwh": float(np.sum(s.export[price < 0]) * dt),
        "starts": float(np.sum(s.starts)),
        "flare_mwh": float(np.sum(s.flare) * dt),
        "boiler_mwh": float(np.sum(s.boiler) * dt),
        "dump_mwh": float(np.sum(s.dump) * dt),
    }


@dataclass
class Context:
    """Everything a strategy needs besides the plant: days, as-of forecasts, premiums."""
    days: list[Day]
    forecasts: dict
    trailing: dict                  # {date: mean price of the 30 days before} for as-of premium estimates
    support: SupportParams = SupportParams()

    def __post_init__(self):
        self.premium = monthly_premium(self.days, self.support.fixed_price)


def _rates(ctx: Context, day: Day, scenario: str, prices: np.ndarray, as_of: bool) -> np.ndarray:
    if as_of:
        prem = max(0.0, ctx.support.fixed_price - ctx.trailing.get(day.date, ctx.support.fixed_price))
    else:
        prem = ctx.premium[(day.date.year, day.date.month)]
    return export_rate(scenario, prices, day.dt, prem, ctx.support.fixed_price, ctx.support.negative_run_hours)


def run_strategy(ctx: Context, plant: PlantParams, strategy: str, scenario: str = "merchant",
                 keep: set | None = None) -> tuple[pd.DataFrame, dict]:
    """Daily settlement of one strategy over ``ctx.days``; schedules of the days in ``keep`` are returned too."""
    keep = keep or set()
    days = [d for d in ctx.days if d.date in ctx.forecasts]
    heat = {d.date: heat_demand(d.date, plant) for d in days}
    if strategy == "baseload":
        scheds = simulate(days, plant, heat, baseload=True)
    elif strategy in ("rank_forecast", "rank_perfect"):
        as_of = strategy == "rank_forecast"
        signals = {d.date: _rates(ctx, d, scenario, ctx.forecasts[d.date] if as_of else d.price, as_of) for d in days}
        scheds = simulate(days, plant, heat, signals=signals)
    elif strategy in ("milp_forecast", "milp_perfect"):
        as_of = strategy == "milp_forecast"
        scheds, u_prev = [], None
        for d in days:
            signal = ctx.forecasts[d.date] if as_of else d.price
            s = optimize_day(_rates(ctx, d, scenario, signal, as_of), signal, plant, d.dt, heat[d.date], u_prev)
            u_prev = s.last_on
            scheds.append(s)
    else:
        raise ValueError(f"unknown strategy {strategy}")
    rows, kept = [], {}
    for d, s in zip(days, scheds):
        row = settle(s, d.price, _rates(ctx, d, scenario, d.price, as_of=False), plant)
        row["date"] = d.date
        row["dt"] = d.dt
        row["mip_gap"] = s.gap
        rows.append(row)
        if d.date in keep:
            kept[d.date] = s
    daily = pd.DataFrame(rows).set_index("date")
    return daily, kept


def annualise(daily: pd.DataFrame) -> dict:
    """Per-year figures from a daily frame (sum over days x 365.25 / number of days)."""
    f = 365.25 / len(daily) if len(daily) else 0.0
    out = {k: float(daily[k].sum() * f) for k in SETTLEMENT_COLUMNS}
    out["days"] = int(len(daily))
    out["capture_price"] = out["market_value"] / out["export_mwh"] if out["export_mwh"] else float("nan")
    return out


def payback(extra_margin: float, capex_total: float, fixed_opex: float) -> float:
    """Simple payback in years; inf when the extra margin does not cover the fixed opex."""
    net = extra_margin - fixed_opex
    if capex_total <= 0:
        return 0.0
    return capex_total / net if net > 0 else float("inf")
