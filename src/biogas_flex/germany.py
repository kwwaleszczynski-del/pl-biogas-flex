"""How the German biomass fleet already follows day-ahead prices (SMARD, Bundesnetzagentur, CC BY 4.0).

SMARD "Biomasse" is the net generation of all biomass units (biogas and biomethane CHP, solid biomass, ...), so a
flat line would mean nobody shifts output. Per month and per day (local time, hourly values):

    swing          (max - min) / mean of the daily generation profile
    corr           Pearson correlation of hourly generation and price within the day, averaged over days
    slope          MW of extra generation per 10 EUR/MWh of price above the daily mean (within-day regression)
    top6_share     share of daily generation produced in the 6 most expensive hours (flat = 25%)
    neg_ratio      generation in negative-price hours / daily mean generation of those days

Usage:
    fleet_analysis(Path("data"), Path("results"))
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from biogas_flex.config import END
from biogas_flex.data import SMARD_PRICE, biomass_generation, smard_series

log = logging.getLogger(__name__)

FLEET_START = date(2019, 1, 1)


def load_fleet(data_dir: Path, start: date = FLEET_START, end: date = END) -> pd.DataFrame:
    """Hourly biomass generation (MW) and DE-LU day-ahead price (EUR/MWh), local date and hour."""
    gen = biomass_generation(start, end, data_dir)
    price = smard_series(*SMARD_PRICE, start, end, data_dir, resolution="hour")
    df = pd.concat([gen.rename("gen"), price.rename("price")], axis=1).dropna()
    local = df.index.tz_convert("Europe/Berlin")
    df["date"] = local.date
    df["hour"] = local.hour
    return df


def daily_metrics(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for d, g in df.groupby("date"):
        if len(g) < 23:
            continue
        gen, price = g["gen"].to_numpy(), g["price"].to_numpy()
        mean = gen.mean()
        dg, dp = gen - mean, price - price.mean()
        top = np.argsort(-price, kind="stable")[:6]
        neg = price < 0
        rows.append({
            "date": d,
            "gen_mean": mean,
            "swing": (gen.max() - gen.min()) / mean if mean else np.nan,
            "corr": float(np.corrcoef(gen, price)[0, 1]) if gen.std() > 0 and price.std() > 0 else np.nan,
            "slope_num": float(np.sum(dg * dp)),
            "slope_den": float(np.sum(dp * dp)),
            "top6_share": gen[top].sum() / gen.sum(),
            "neg_hours": int(neg.sum()),
            "neg_ratio": gen[neg].mean() / mean if neg.any() else np.nan,
        })
    return pd.DataFrame(rows).set_index("date")


def monthly_metrics(daily: pd.DataFrame) -> pd.DataFrame:
    month = pd.to_datetime(pd.Series(daily.index, index=daily.index)).dt.strftime("%Y-%m").values
    g = daily.groupby(month)
    out = pd.DataFrame({
        "gen_mean_mw": g["gen_mean"].mean(),
        "swing": g["swing"].mean(),
        "corr": g["corr"].mean(),
        "slope_mw_per_10eur": 10 * g["slope_num"].sum() / g["slope_den"].sum(),
        "top6_share": g["top6_share"].mean(),
        "neg_hours": g["neg_hours"].sum(),
        "neg_ratio": g["neg_ratio"].mean(),
    })
    return out.rename_axis("month").reset_index()


def yearly_profiles(df: pd.DataFrame) -> pd.DataFrame:
    """Mean generation and price per local hour and year, each divided by its yearly mean."""
    year = pd.to_datetime(pd.Series(df["date"].values)).dt.year.values
    g = df.assign(year=year).groupby(["year", "hour"])[["gen", "price"]].mean()
    norm = g / g.groupby(level="year").transform("mean")
    return norm.rename(columns={"gen": "gen_rel", "price": "price_rel"}).reset_index()


def fleet_analysis(data_dir: Path, results_dir: Path) -> dict:
    df = load_fleet(data_dir)
    daily = daily_metrics(df)
    monthly = monthly_metrics(daily)
    profiles = yearly_profiles(df)
    results_dir.mkdir(parents=True, exist_ok=True)
    monthly.to_csv(results_dir / "germany_fleet_monthly.csv", index=False, float_format="%.4f")
    profiles.to_csv(results_dir / "germany_fleet_profiles.csv", index=False, float_format="%.4f")
    year = pd.to_datetime(pd.Series(daily.index)).dt.year.values
    yearly = daily.assign(year=year).groupby("year").agg(
        swing=("swing", "mean"), corr=("corr", "mean"), top6_share=("top6_share", "mean"),
        gen_mean_mw=("gen_mean", "mean"), neg_hours=("neg_hours", "sum"), neg_ratio=("neg_ratio", "mean"))
    yearly.to_csv(results_dir / "germany_fleet_yearly.csv", float_format="%.4f")
    log.info("German fleet: %d days, %s .. %s", len(daily), daily.index.min(), daily.index.max())
    return {"days": len(daily), "yearly": yearly}
