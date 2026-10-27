from datetime import date

import numpy as np
import pytest

from biogas_flex.backtest import Context, annualise, heat_demand, payback, run_strategy
from biogas_flex.config import CostParams, PlantParams, capex, design_plant, engine_sizes
from biogas_flex.forecast import profile_forecast, trailing_mean
from biogas_flex.heuristic import dispatch, rank_targets, simulate
from biogas_flex.support import SCENARIOS, export_rate, monthly_premium, negative_mask


def test_negative_runs():
    h = np.full(24, 50.0)
    h[10:16] = -5.0       # 6 h run
    h[20:23] = -1.0       # 3 h run
    m = negative_mask(h, 1.0, 6)
    assert m[10:16].all() and not m[20:23].any() and m.sum() == 6
    q = np.repeat(h, 4)
    assert negative_mask(q, 0.25, 6).sum() == 24
    assert negative_mask(q, 0.25, 0).sum() == 36


def test_export_rates():
    p = np.array([100.0, -20.0, 300.0])
    assert export_rate("merchant", p, 1.0, 50.0, 900.0).tolist() == p.tolist()
    assert export_rate("fit", p, 1.0, 50.0, 900.0).tolist() == [900.0] * 3
    assert export_rate("fit_neg1", p, 1.0, 50.0, 900.0).tolist() == [900.0, -20.0, 900.0]
    assert export_rate("fip", p, 1.0, 50.0, 900.0).tolist() == [150.0, 30.0, 350.0]
    assert export_rate("fip_neg1", p, 1.0, 50.0, 900.0).tolist() == [150.0, -20.0, 350.0]
    assert set(SCENARIOS) >= {"merchant", "fit", "fip", "fit_neg6", "fip_neg6"}
    with pytest.raises(ValueError):
        export_rate("cfd", p, 1.0, 0.0, 900.0)


def test_monthly_premium(week_days):
    prem = monthly_premium(week_days, 900.0)
    assert set(prem) == {(2025, 9), (2025, 10)}
    assert all(0 <= v <= 900 for v in prem.values())


def test_engine_sizes_and_capex():
    assert engine_sizes(1.0) == [0.5]
    assert engine_sizes(1.5) == [0.5, 0.25]
    assert engine_sizes(2.5) == [0.5, 0.5, 0.25]
    assert engine_sizes(3.0) == [0.5, 0.5, 0.5]
    costs = CostParams()
    assert capex(design_plant(1.0, 2.0), costs)["total"] == 0.0
    c = capex(design_plant(2.0, 12.0), costs)
    assert c["chp"] == pytest.approx(500 * costs.chp_per_kw)
    assert c["heat_tank"] > 0 and c["total"] > c["chp"]
    assert payback(100_000, 500_000, 0.0) == pytest.approx(5.0)
    assert payback(1_000, 500_000, 5_000) == float("inf")


def test_dispatch_respects_minimum_load():
    plant = design_plant(1.5, 4.0)
    assert dispatch(0.75, plant.engines).tolist() == [0.5, 0.25]
    assert dispatch(0.6, plant.engines).tolist() == [0.5, 0.0]       # 0.1 MW is below the small engine's 0.125
    assert dispatch(0.2, plant.engines).tolist() == [0.0, 0.2]


def test_rank_targets_run_the_dearest_hours(hourly_day):
    plant = design_plant(2.0, 12.0)
    target = rank_targets(hourly_day, hourly_day.price, plant, plant.storage_mwh * plant.storage_start_frac)
    full = target == plant.p_max
    assert 11 <= full.sum() <= 12
    assert hourly_day.price[full].min() >= np.sort(hourly_day.price)[-13]


def test_simulation_keeps_storage_in_bounds_and_closes_gas(week_days):
    plant = design_plant(3.0, 4.0)
    heat = {d.date: heat_demand(d.date, plant) for d in week_days}
    scheds = simulate(week_days, plant, heat, signals={d.date: d.price for d in week_days})
    smin, smax = plant.storage_mwh * plant.storage_min_frac, plant.storage_mwh * plant.storage_max_frac
    inflow = used = 0.0
    for d, s in zip(week_days, scheds):
        assert s.storage.min() >= smin - 1e-6 and s.storage.max() <= smax + 1e-6
        inflow += plant.gas_inflow * d.n * d.dt
        used += (s.fuel.sum() + s.boiler.sum() / plant.boiler_eff + s.flare.sum()) * d.dt
    end = scheds[-1].storage[-1]
    assert inflow - used == pytest.approx(end - plant.storage_mwh * plant.storage_start_frac, abs=1e-6)


def test_heat_demand_is_seasonal():
    plant = PlantParams()
    winter, summer = heat_demand(date(2025, 1, 15), plant), heat_demand(date(2025, 7, 16), plant)
    assert winter == pytest.approx(plant.heat_winter_frac * plant.rated_heat_mw, rel=1e-3)
    assert summer == pytest.approx(plant.heat_summer_frac * plant.rated_heat_mw, rel=1e-2)


def test_strategies_rank_as_expected(week_days):
    fc = profile_forecast(week_days)
    days = [d for d in week_days if d.date in fc]
    ctx = Context(days, {d.date: fc[d.date] for d in days}, trailing_mean(week_days))
    plant = design_plant(2.0, 12.0)
    net = {st: annualise(run_strategy(ctx, plant, st)[0])["net"]
           for st in ("baseload", "rank_forecast", "milp_forecast", "milp_perfect")}
    assert net["milp_perfect"] >= net["milp_forecast"] - 1e-6
    assert net["milp_perfect"] > net["baseload"]
    assert net["milp_forecast"] > net["baseload"]
    one = design_plant(1.0, 2.0)
    base = annualise(run_strategy(ctx, one, "baseload")[0])["net"]
    rank = annualise(run_strategy(ctx, one, "rank_perfect")[0])["net"]
    assert rank == pytest.approx(base)          # a 1x plant has nothing to shift with the rule
