"""Command line interface.

Usage:
    python -m biogas_flex grid    --market PL --workers 10       # design grid x strategies (merchant)
    python -m biogas_flex grid    --market DE --workers 10
    python -m biogas_flex support --workers 10                   # support-scheme scenarios, PL
    python -m biogas_flex check                                  # two-stage plan vs joint quarter-hour MILP
    python -m biogas_flex germany                                # SMARD biomass fleet behaviour
    python -m biogas_flex report                                 # summary tables, figures and dashboard data
    python -m biogas_flex --help

Raw data are cached in ./data (override with --data-dir or BIOGAS_DATA_DIR); derived results go to ./results,
figures to ./docs/img and the dashboard to ./docs.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pickle
from pathlib import Path

from biogas_flex.backtest import STRATEGIES
from biogas_flex.config import END, MULTIPLIERS, START, STORAGE_HOURS
from biogas_flex.support import SCENARIOS

log = logging.getLogger("biogas_flex")

SUPPORT_DESIGNS = [(1.0, 2.0), (2.0, 12.0), (3.0, 24.0)]
SUPPORT_STRATEGIES = ("baseload", "rank_forecast", "milp_forecast", "milp_perfect")


def cmd_grid(args):
    from biogas_flex.runner import cached_context, pick_example_week, run_grid
    ctx_path = cached_context(args.market, args.data_dir)
    pl_path = cached_context("PL", args.data_dir)
    week = pick_example_week(pickle.loads(pl_path.read_bytes()))   # same week for both markets (own cache file)
    log.info("%s: example week %s .. %s", args.market, week[0], week[-1])
    designs = [(m, h) for m in MULTIPLIERS for h in STORAGE_HOURS]
    monthly, weeks = run_grid(ctx_path, designs, STRATEGIES, ["merchant"], keep=week, workers=args.workers)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    monthly.to_csv(args.results_dir / f"grid_{args.market}_monthly.csv", index=False, float_format="%.4f")
    (args.results_dir / f"week_{args.market}.json").write_text(json.dumps(
        {"days": [str(d) for d in week], "schedules": weeks}, separators=(",", ":")))
    log.info("grid %s done: %d rows", args.market, len(monthly))


def cmd_support(args):
    from biogas_flex.runner import cached_context, run_grid
    ctx_path = cached_context("PL", args.data_dir)
    monthly, _ = run_grid(ctx_path, SUPPORT_DESIGNS, SUPPORT_STRATEGIES, SCENARIOS, workers=args.workers)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    monthly.to_csv(args.results_dir / "support_PL_monthly.csv", index=False, float_format="%.4f")


def cmd_check(args):
    """Two-stage quarter-hour plan vs the joint quarter-hour MILP on a sample of Polish days."""
    import pandas as pd

    from biogas_flex.config import design_plant
    from biogas_flex.milp import validate_two_stage
    from biogas_flex.runner import cached_context
    ctx = pickle.loads(cached_context("PL", args.data_dir).read_bytes())   # local file written by this code
    quarter = [d for d in ctx.days if d.dt < 1.0]
    sample = quarter[::max(1, len(quarter) // args.days)][:args.days]
    rows = []
    for m, h in ((1.5, 4.0), (2.0, 12.0), (3.0, 2.0), (3.0, 24.0)):
        res = validate_two_stage(sample, design_plant(m, h))
        rows.append({"multiplier": m, "storage_h": h, **res})
        log.info("%gx %gh: two-stage loses %.3f%% (worst day %.3f%%)", m, h, res["loss_pct"], res["worst_day_pct"])
    args.results_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(args.results_dir / "two_stage_check.csv", index=False, float_format="%.4f")


def cmd_germany(args):
    from biogas_flex.germany import fleet_analysis
    fleet_analysis(args.data_dir, args.results_dir)


def cmd_report(args):
    from biogas_flex.report import build_all
    build_all(args.data_dir, args.results_dir, Path("docs"))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="biogas-flex", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get("BIOGAS_DATA_DIR", "data")))
    ap.add_argument("--results-dir", type=Path, default=Path("results"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grid", help=f"design grid {START}..{END}")
    g.add_argument("--market", choices=["PL", "DE"], default="PL")
    g.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    s = sub.add_parser("support", help="support-scheme scenarios (PL)")
    s.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    c = sub.add_parser("check", help="two-stage quarter-hour plan vs joint MILP on sample days")
    c.add_argument("--days", type=int, default=12)
    sub.add_parser("germany", help="German biomass fleet behaviour from SMARD")
    sub.add_parser("report", help="tables, figures and dashboard from results/")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("pse_opendata").setLevel(logging.WARNING)
    {"grid": cmd_grid, "support": cmd_support, "check": cmd_check, "germany": cmd_germany,
     "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
