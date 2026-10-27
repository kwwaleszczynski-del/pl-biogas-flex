"""Parallel runs: design grid x strategies per market, and support-scheme scenarios.

Each task backtests one (design, strategy, scenario) over the whole period and returns monthly sums, so that the
dashboard can annualise any sub-period. Days of an example week keep their full schedules.

Usage:
    ctx = build_context("PL", Path("data"))
    monthly, weeks = run_grid(ctx, designs, STRATEGIES, ["merchant"], keep=week, workers=10)
"""
from __future__ import annotations

import json
import logging
import pickle
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from biogas_flex.backtest import SETTLEMENT_COLUMNS, Context, run_strategy
from biogas_flex.config import END, MODEL_VERSION, START, design_plant
from biogas_flex.data import load_de_prices, load_pl_prices, make_client, to_days
from biogas_flex.forecast import profile_forecast, trailing_mean

log = logging.getLogger(__name__)

HISTORY_DAYS = 35   # loaded before START for the as-of forecast and premium estimate


def build_context(market: str, data_dir: Path, start: date = START, end: date = END) -> Context:
    """Days, forecasts and trailing means for ``market`` ("PL" or "DE"); evaluation restricted to start..end."""
    lo = start - timedelta(days=HISTORY_DAYS)
    if market == "PL":
        q = load_pl_prices(make_client(data_dir), lo, end)
    elif market == "DE":
        q = load_de_prices(lo, end, data_dir)
    else:
        raise ValueError(market)
    days = to_days(q)
    fc = profile_forecast(days)
    tr = trailing_mean(days)
    inside = [d for d in days if start <= d.date <= end]
    missing = (end - start).days + 1 - len(inside)
    if missing:
        log.warning("%s: %d incomplete days skipped", market, missing)
    return Context(inside, {d.date: fc[d.date] for d in inside if d.date in fc}, tr)


def cached_context(market: str, data_dir: Path, start: date = START, end: date = END) -> Path:
    """Build the context once and pickle it for the worker processes (a local file this code wrote itself)."""
    path = Path(data_dir) / "cache" / f"context_{market}_{start}_{end}.pkl"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        ctx = build_context(market, data_dir, start, end)
        path.write_bytes(pickle.dumps(ctx))
    return path


_CTX: dict[str, Context] = {}


def _load(path: str) -> Context:
    if path not in _CTX:
        _CTX[path] = pickle.loads(Path(path).read_bytes())
    return _CTX[path]


def _task(ctx_path: str, multiplier: float, storage_h: float, strategy: str, scenario: str, keep: tuple):
    ctx = _load(ctx_path)
    plant = design_plant(multiplier, storage_h)
    t0 = time.perf_counter()
    daily, kept = run_strategy(ctx, plant, strategy, scenario, keep=set(keep))
    month = pd.to_datetime(pd.Series(daily.index, index=daily.index)).dt.strftime("%Y-%m")
    agg = daily[SETTLEMENT_COLUMNS].groupby(month.values).sum()
    agg["days"] = daily.groupby(month.values).size()
    agg["mip_gap_mean"] = daily["mip_gap"].groupby(month.values).mean()
    agg["mip_gap_max"] = daily["mip_gap"].groupby(month.values).max()
    agg = agg.rename_axis("month").reset_index()
    agg.insert(0, "scenario", scenario)
    agg.insert(0, "strategy", strategy)
    agg.insert(0, "storage_h", storage_h)
    agg.insert(0, "multiplier", multiplier)
    week = {str(d): {"power": s.power.round(4).tolist(), "storage_frac": (s.storage / plant.storage_mwh).round(4).tolist(),
                     "tank": s.tank.round(3).tolist(), "dt": s.dt} for d, s in kept.items()}
    return agg, week, time.perf_counter() - t0


def _task_files(ctx_path: Path, t: tuple) -> tuple[Path, Path]:
    """Per-task result files next to the context, so an interrupted grid resumes where it stopped."""
    folder = Path(ctx_path).with_suffix("") / f"model_v{MODEL_VERSION}"
    stem = f"{t[1]:g}x_{t[2]:g}h_{t[3]}_{t[4]}"
    return folder / f"{stem}.csv", folder / f"{stem}.json"


def run_grid(ctx_path: Path, designs: list[tuple[float, float]], strategies, scenarios, keep=(),
             workers: int = 8, attempts: int = 3) -> tuple[pd.DataFrame, dict]:
    """Run every (design, strategy, scenario); returns monthly sums and the kept schedules.

    Each finished task is written to disk at once; tasks already on disk are not run again, and a broken process
    pool (a worker killed from outside) is restarted up to ``attempts`` times."""
    tasks = [(str(ctx_path), m, h, st, sc, tuple(keep)) for (m, h) in designs for st in strategies for sc in scenarios]
    # slow MILP tasks first so the pool drains evenly
    tasks.sort(key=lambda t: (not t[3].startswith("milp"), -t[1], t[2]))
    for attempt in range(1, attempts + 1):
        todo = [t for t in tasks if not _task_files(ctx_path, t)[0].exists()]
        if not todo:
            break
        log.info("attempt %d: %d of %d tasks to run", attempt, len(todo), len(tasks))
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_task, *t): t for t in todo}
                for k, fut in enumerate(as_completed(futures), 1):
                    t = futures[fut]
                    agg, week, secs = fut.result()
                    csv, js = _task_files(ctx_path, t)
                    csv.parent.mkdir(parents=True, exist_ok=True)
                    js.write_text(json.dumps(week, separators=(",", ":")))
                    agg.to_csv(csv, index=False)          # written last: marks the task as done
                    log.info("[%d/%d] %gx %gh %s %s: %.0f s", k, len(todo), t[1], t[2], t[3], t[4], secs)
        except BrokenProcessPool as exc:
            log.warning("process pool broke (%s); restarting", exc)
    frames, weeks = [], {}
    for t in tasks:
        csv, js = _task_files(ctx_path, t)
        if not csv.exists():
            raise RuntimeError(f"task not finished: {csv.name}")
        frames.append(pd.read_csv(csv))
        weeks[f"{t[1]:g}x_{t[2]:g}h|{t[3]}|{t[4]}"] = json.loads(js.read_text())
    monthly = pd.concat(frames, ignore_index=True).sort_values(["multiplier", "storage_h", "strategy", "scenario", "month"])
    return monthly.reset_index(drop=True), weeks


def pick_example_week(ctx: Context) -> list[date]:
    """Monday-to-Sunday week with quarter-hour products and the most negative-price intervals."""
    best, score = None, -1.0
    for d in ctx.days:
        if d.date.weekday() != 0 or d.dt >= 1.0:
            continue
        week = [x for x in ctx.days if d.date <= x.date < d.date + timedelta(days=7)]
        if len(week) < 7:
            continue
        neg = sum(float(np.sum(x.price < 0) * x.dt) for x in week)
        if neg > score:
            best, score = d.date, neg
    return [best + timedelta(days=k) for k in range(7)] if best else []
