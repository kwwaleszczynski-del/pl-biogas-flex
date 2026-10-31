"""Summary tables, README figures and the static dashboard, all built from results/ (no optimisation here).

Usage:
    from biogas_flex.report import build_all
    build_all(Path("data"), Path("results"), Path("docs"))
"""
from __future__ import annotations

import json
import logging
import pickle
from dataclasses import asdict
from datetime import date, datetime
from importlib import resources
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from biogas_flex.backtest import SETTLEMENT_COLUMNS, payback  # noqa: E402
from biogas_flex.config import (END, MULTIPLIERS, START, STORAGE_HOURS, CostParams, PlantParams,  # noqa: E402
                                SupportParams, capex, design_plant)

log = logging.getLogger(__name__)

BASELINE = (1.0, 2.0, "baseload")
LABELS = {
    "baseload": "Baseload",
    "rank_forecast": "Rank rule, forecast",
    "rank_perfect": "Rank rule, actual prices",
    "milp_forecast": "MILP, forecast (realizable)",
    "milp_perfect": "MILP, perfect foresight",
}
COLORS = {"baseload": "#898781", "rank_forecast": "#e8a33d", "rank_perfect": "#b5781c", "milp_forecast": "#2a78d6",
          "milp_perfect": "#1b3f8f", "price": "#52514e"}
SUPPORT_LABELS = {
    "merchant": "Merchant", "fit": "Fixed price (FIT-like)", "fit_neg6": "FIT-like, no support in >=6 h negative runs",
    "fit_neg1": "FIT-like, no support at any negative price", "fip": "Sliding premium (FIP-like)",
    "fip_neg6": "FIP-like, no premium in >=6 h negative runs", "fip_neg1": "FIP-like, no premium at any negative price",
}
SUPPORT_SHORT = {
    "merchant": "Merchant", "fit": "FIT-like", "fit_neg6": "FIT-like,\nnone in >=6 h\nnegative runs",
    "fit_neg1": "FIT-like,\nnone at any\nnegative price", "fip": "FIP-like",
    "fip_neg6": "FIP-like,\nnone in >=6 h\nnegative runs", "fip_neg1": "FIP-like,\nnone at any\nnegative price",
}
KEYS = ["multiplier", "storage_h", "strategy", "scenario"]


def periods(months: list[str]) -> dict[str, list[str]]:
    """Named sub-periods as lists of months present in the results."""
    last = months[-1]
    y, m = int(last[:4]), int(last[5:])
    first12 = f"{y - 1}-{m + 1:02d}" if m < 12 else f"{y}-01"
    out = {"all": months, "last12": [x for x in months if x >= first12]}
    for year in sorted({x[:4] for x in months}):
        out[year] = [x for x in months if x.startswith(year)]
    return out


def annualise(monthly: pd.DataFrame, months: list[str] | None = None) -> pd.DataFrame:
    """Per-year figures per design/strategy/scenario over the selected months."""
    df = monthly if months is None else monthly[monthly["month"].isin(months)]
    g = df.groupby(KEYS)
    sums = g[SETTLEMENT_COLUMNS + ["days"]].sum()
    out = sums[SETTLEMENT_COLUMNS].mul(365.25 / sums["days"], axis=0)
    out["days"] = sums["days"]
    out["capture_price"] = sums["market_value"] / sums["export_mwh"]
    out["mip_gap_mean"] = g["mip_gap_mean"].mean()
    out["mip_gap_max"] = g["mip_gap_max"].max()
    return out.reset_index()


def economics(table: pd.DataFrame, costs: CostParams, baseline_net: float) -> pd.DataFrame:
    rows = []
    for _, r in table.iterrows():
        cap = capex(design_plant(r["multiplier"], r["storage_h"]), costs)
        extra = r["net"] - baseline_net
        fixed = costs.fixed_opex_frac * cap["total"]
        rows.append({"capex": cap["total"], "extra_net": extra, "fixed_opex": fixed,
                     "payback_years": payback(extra, cap["total"], fixed)})
    return pd.concat([table.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def summary(monthly: pd.DataFrame, costs: CostParams, months: list[str] | None = None) -> pd.DataFrame:
    table = annualise(monthly, months)
    b = table[(table["multiplier"] == BASELINE[0]) & (table["storage_h"] == BASELINE[1]) &
              (table["strategy"] == BASELINE[2]) & (table["scenario"] == "merchant")]
    return economics(table, costs, float(b["net"].iloc[0]))


# ---------------------------------------------------------------- figures

def _save(fig, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def fig_value_grid(summ: pd.DataFrame, market: str, path: Path):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    for ax, st in zip(axes, ("milp_forecast", "milp_perfect")):
        t = summ[summ["strategy"] == st].pivot(index="multiplier", columns="storage_h", values="extra_net") / 1000
        im = ax.imshow(t.values, origin="lower", aspect="auto", cmap="Blues", vmin=0,
                       vmax=float(summ[summ["strategy"] == "milp_perfect"]["extra_net"].max() / 1000))
        ax.set_xticks(range(len(t.columns)), [f"{c:g}" for c in t.columns])
        ax.set_yticks(range(len(t.index)), [f"{i:g}x" for i in t.index])
        for i in range(t.shape[0]):
            for j in range(t.shape[1]):
                v = t.values[i, j]
                ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=8,
                        color="white" if v > 0.55 * im.get_clim()[1] else "black")
        ax.set_xlabel("Gas storage [h of gas production]")
        ax.set_title(LABELS[st])
    axes[0].set_ylabel("CHP capacity [x average gas power]")
    fig.suptitle(f"{market}: extra net margin vs 1x baseload [thousand PLN / year]", fontsize=11)
    fig.colorbar(im, ax=axes, shrink=0.85)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def fig_strategies(summaries: dict[str, pd.DataFrame], designs: list[tuple[float, float]], path: Path):
    fig, axes = plt.subplots(1, len(summaries), figsize=(11, 4), sharey=True)
    axes = np.atleast_1d(axes)
    strategies = [s for s in LABELS if s != "baseload"]
    width = 0.8 / len(strategies)
    for ax, (market, summ) in zip(axes, summaries.items()):
        for k, st in enumerate(strategies):
            vals = [float(summ[(summ["multiplier"] == m) & (summ["storage_h"] == h) & (summ["strategy"] == st)]
                          ["extra_net"].iloc[0]) / 1000 for m, h in designs]
            ax.bar(np.arange(len(designs)) + k * width - 0.4 + width / 2, vals, width, color=COLORS[st], label=LABELS[st])
        ax.set_xticks(range(len(designs)), [f"{m:g}x / {h:g} h" for m, h in designs])
        ax.set_title(market)
        ax.grid(axis="y", alpha=0.3)
        ax.axhline(0, color="black", lw=0.8)
    axes[0].set_ylabel("Extra net margin vs 1x baseload [thousand PLN / year]")
    axes[0].legend(fontsize=8)
    _save(fig, path)


def fig_example_week(week: dict, prices: dict, design: str, path: Path, market: str):
    days = week["days"]
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(11, 7), sharex=True, height_ratios=[1, 1.2, 0.8])
    x = np.arange(len(prices["price"])) * 0.25
    ax1.step(x, prices["price"], where="post", color=COLORS["price"], lw=0.9, label="actual price")
    ax1.step(x, prices["forecast"], where="post", color=COLORS["milp_forecast"], lw=0.9, ls="--", label="as-of forecast")
    ax1.axhline(0, color="black", lw=0.6)
    ax1.set_ylabel("PLN/MWh")
    # headroom above the data so the one-row legends no longer cover price peaks and full-load output
    lo = min(np.nanmin(prices["price"]), np.nanmin(prices["forecast"]))
    hi = max(np.nanmax(prices["price"]), np.nanmax(prices["forecast"]))
    ax1.set_ylim(lo - 0.05 * (hi - lo), hi + 0.22 * (hi - lo))
    ax1.legend(fontsize=8, loc="upper right", ncol=2)
    for st in ("baseload", "milp_forecast", "milp_perfect"):
        key = f"{design}|{st}|merchant" if st != "baseload" else "1x_2h|baseload|merchant"
        p = np.concatenate([np.repeat(week["schedules"][key][d]["power"], 4 if week["schedules"][key][d]["dt"] == 1.0 else 1)
                            for d in days])
        ax2.step(x, p, where="post", color=COLORS[st], lw=1.1, label=LABELS[st])
        if st != "baseload":
            s = np.concatenate([np.repeat(week["schedules"][key][d]["storage_frac"],
                                          4 if week["schedules"][key][d]["dt"] == 1.0 else 1) for d in days])
            ax3.plot(x, 100 * s, color=COLORS[st], lw=1.1)
    ax2.set_ylabel("Output [MW]")
    top = ax2.get_ylim()[1]
    ax2.set_yticks([t for t in ax2.get_yticks() if 0 <= t <= top])
    ax2.set_ylim(top=1.3 * top)
    ax2.legend(fontsize=8, loc="upper right", ncol=3)
    ax3.set_ylabel("Gas storage [%]")
    ax3.set_xticks(np.arange(0, 7 * 24 + 1, 24), [d[5:] for d in days] + [""])
    for ax in (ax1, ax2, ax3):
        ax.grid(alpha=0.3)
    ax1.set_title(f"{market}, {days[0]} .. {days[-1]}: plant {design.replace('_', ' / ')}, quarter-hour prices")
    _save(fig, path)


def fig_germany(fleet_monthly: pd.DataFrame, profiles: pd.DataFrame, path: Path):
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    m = pd.to_datetime(fleet_monthly["month"])
    ax1.plot(m, fleet_monthly["corr"], color=COLORS["milp_forecast"], label="within-day correlation output vs price")
    ax1.plot(m, fleet_monthly["top6_share"] / 0.25 - 1, color=COLORS["rank_forecast"],
             label="output in 6 dearest hours vs flat (share / 25% - 1)")
    ax1.axhline(0, color="black", lw=0.6)
    ax1.set_title("German biomass fleet: price following, monthly")
    ax1.legend(fontsize=8)
    ax1.grid(alpha=0.3)
    for year, color in ((2019, "#c3c2b7"), (2022, "#898781"), (2024, "#2a78d6"), (2025, "#1b3f8f")):
        p = profiles[profiles["year"] == year]
        if len(p):
            ax2.plot(p["hour"], p["gen_rel"], color=color, label=f"output {year}")
    last = profiles["year"].max()
    p = profiles[profiles["year"] == last]
    ax2b = ax2.twinx()
    ax2b.plot(p["hour"], p["price_rel"], color=COLORS["price"], ls="--", lw=1, label=f"price {last}")
    ax2b.set_ylabel("price / daily mean")
    ax2.set_xlabel("hour of day (local)")
    ax2.set_ylabel("output / yearly mean")
    ax2.set_title("Mean daily profile")
    ax2.legend(fontsize=8, loc="upper left")
    ax2.grid(alpha=0.3)
    _save(fig, path)


def fig_support(sup: pd.DataFrame, design: tuple[float, float], path: Path):
    scen = list(SUPPORT_LABELS)
    fig, ax = plt.subplots(figsize=(11, 4.2))
    base = sup[(sup["multiplier"] == 1.0) & (sup["strategy"] == "baseload")].set_index("scenario")["net"]
    flex = sup[(sup["multiplier"] == design[0]) & (sup["storage_h"] == design[1])]
    strategies = ["rank_forecast", "milp_forecast", "milp_perfect"]
    width = 0.8 / len(strategies)
    for k, st in enumerate(strategies):
        f = flex[flex["strategy"] == st].set_index("scenario")["net"]
        vals = [(f[s] - base[s]) / 1000 for s in scen]
        ax.bar(np.arange(len(scen)) + k * width - 0.4 + width / 2, vals, width, color=COLORS[st], label=LABELS[st])
    ax.set_xticks(range(len(scen)), [SUPPORT_SHORT[s] for s in scen], fontsize=8)
    ax.axhline(0, color="black", lw=0.8)
    ax.set_ylabel("Extra net margin vs 1x baseload\n[thousand PLN / year]")
    ax.set_title(f"Value of flexibility ({design[0]:g}x CHP, {design[1]:g} h storage) under simplified support schemes, PL")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=8)
    _save(fig, path)


# ---------------------------------------------------------------- dashboard

def _week_prices(data_dir: Path, market: str, days: list[str]) -> dict:
    """Actual prices and as-of forecast of the example week on the quarter-hour grid (from the local context)."""
    path = Path(data_dir) / "cache" / f"context_{market}_{START}_{END}.pkl"
    ctx = pickle.loads(path.read_bytes())   # local file written by runner.cached_context
    by_date = {d.date: d for d in ctx.days}
    price, fc = [], []
    for s in days:
        d = by_date[date.fromisoformat(s)]
        rep = 4 if d.dt == 1.0 else 1
        price.extend(np.repeat(d.price, rep).round(2).tolist())
        fc.extend(np.repeat(ctx.forecasts[d.date], rep).round(2).tolist())
    return {"price": price, "forecast": fc}


def site_data(monthly: dict[str, pd.DataFrame], weeks: dict, week_prices: dict, support: pd.DataFrame | None,
              fleet: tuple[pd.DataFrame, pd.DataFrame] | None, costs: CostParams) -> dict:
    months = sorted(next(iter(monthly.values()))["month"].unique())
    designs = [f"{m:g}x_{h:g}h" for m in MULTIPLIERS for h in STORAGE_HOURS]
    data = {
        "generated": datetime.now().strftime("%Y-%m-%d"),
        "period": [str(START), str(END)],
        "months": months,
        "periods": periods(months),
        "multipliers": list(MULTIPLIERS),
        "storage": list(STORAGE_HOURS),
        "strategies": list(LABELS),
        "labels": LABELS,
        "colors": COLORS,
        "metrics": ["net", "sales", "import_cost", "maintenance", "start_cost", "gen_mwh", "export_mwh",
                    "market_value", "starts", "boiler_mwh", "days"],
        "capex": {k: capex(design_plant(*map(float, k.replace("x", "").replace("h", "").split("_"))), costs)
                  for k in designs},
        "fixed_opex_frac": costs.fixed_opex_frac,
        "plant": {k: v for k, v in asdict(PlantParams()).items() if k != "engines"} | {"engine": asdict(PlantParams().engines[0])},
        "costs": asdict(costs),
        "support": asdict(SupportParams()),
        "markets": {},
    }
    for market, mon in monthly.items():
        mk = {}
        for (m, h, st), g in mon[mon["scenario"] == "merchant"].groupby(["multiplier", "storage_h", "strategy"]):
            g = g.set_index("month").reindex(months)
            mk.setdefault(f"{m:g}x_{h:g}h", {})[st] = {c: g[c].fillna(0).round(2).tolist() for c in data["metrics"]}
        wk = weeks.get(market)
        week = None
        if wk:
            days = wk["days"]

            def series(key, field):
                out = []
                for d in days:
                    s = wk["schedules"][key][d]
                    out.extend(np.repeat(s[field], 4 if s["dt"] == 1.0 else 1).round(3).tolist())
                return out
            week = {"days": days, **week_prices[market], "designs": {}}
            for dkey in designs:
                week["designs"][dkey] = {
                    st: {"power": series(f"{dkey}|{st}|merchant", "power")}
                    for st in ("rank_forecast", "milp_forecast", "milp_perfect")}
                week["designs"][dkey]["milp_perfect"]["storage"] = series(f"{dkey}|milp_perfect|merchant", "storage_frac")
                week["designs"][dkey]["milp_forecast"]["storage"] = series(f"{dkey}|milp_forecast|merchant", "storage_frac")
            week["baseload"] = series("1x_2h|baseload|merchant", "power")
        data["markets"][market] = {"grid": mk, "week": week}
    if support is not None:
        data["support"] = {"labels": SUPPORT_LABELS, "rows": support.round(1).to_dict(orient="records")} | data["support"]
    if fleet is not None:
        fm, prof = fleet
        data["germany"] = {"monthly": _columns(fm.round(4)), "profiles": _columns(prof.round(4))}
    return data


def _columns(df: pd.DataFrame) -> dict:
    """Column lists for JSON, with missing values as null."""
    return {c: [None if pd.isna(v) else v for v in df[c].tolist()] for c in df.columns}


def write_site(data: dict, docs_dir: Path):
    template = resources.files("biogas_flex").joinpath("templates/index.html").read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":"), allow_nan=False, default=_json_default)
    html = template.replace("/*DATA*/null", payload)
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "index.html").write_text(html, encoding="utf-8", newline="\n")


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    raise TypeError(type(o))


def build_all(data_dir: Path, results_dir: Path, docs_dir: Path, costs: CostParams | None = None):
    costs = costs or CostParams()
    img = docs_dir / "img"
    monthly, summaries, weeks, week_prices = {}, {}, {}, {}
    for market in ("PL", "DE"):
        path = results_dir / f"grid_{market}_monthly.csv"
        if not path.exists():
            continue
        monthly[market] = pd.read_csv(path)
        months = sorted(monthly[market]["month"].unique())
        summ = summary(monthly[market], costs)
        summaries[market] = summ
        summ.to_csv(results_dir / f"summary_{market}.csv", index=False, float_format="%.2f")
        by_period = []
        for name, ms in periods(months).items():
            s = summary(monthly[market], costs, ms)
            s.insert(0, "period", name)
            by_period.append(s)
        pd.concat(by_period).to_csv(results_dir / f"summary_{market}_by_period.csv", index=False, float_format="%.2f")
        fig_value_grid(summ, market, img / f"value_grid_{market}.png")
        wpath = results_dir / f"week_{market}.json"
        if wpath.exists():
            weeks[market] = json.loads(wpath.read_text())
            week_prices[market] = _week_prices(data_dir, market, weeks[market]["days"])
            fig_example_week(weeks[market], week_prices[market], "2x_12h", img / f"example_week_{market}.png", market)
    if summaries:
        fig_strategies(summaries, [(1.5, 8.0), (2.0, 12.0), (3.0, 24.0)], img / "strategies.png")
    support = None
    spath = results_dir / "support_PL_monthly.csv"
    if spath.exists():
        sm = pd.read_csv(spath)
        support = annualise(sm)
        support.to_csv(results_dir / "support_PL_summary.csv", index=False, float_format="%.2f")
        fig_support(support, (2.0, 12.0), img / "support_PL.png")
    fleet = None
    fpath = results_dir / "germany_fleet_monthly.csv"
    if fpath.exists():
        fleet = (pd.read_csv(fpath), pd.read_csv(results_dir / "germany_fleet_profiles.csv"))
        fig_germany(*fleet, img / "germany_fleet.png")
    if monthly:
        sup_rows = support[["multiplier", "storage_h", "strategy", "scenario", "net", "sales", "gen_mwh",
                            "neg_export_mwh", "starts"]] if support is not None else None
        write_site(site_data(monthly, weeks, week_prices, sup_rows, fleet, costs), docs_dir)
    log.info("report written to %s and %s", results_dir, docs_dir)
