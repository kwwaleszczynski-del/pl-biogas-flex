import numpy as np
import pytest

from biogas_flex.backtest import settle
from biogas_flex.config import design_plant
from biogas_flex.heuristic import simulate
from biogas_flex.milp import optimize_day


def _balances(s, plant, q):
    dt = s.dt
    gas_in = plant.gas_inflow * len(s.power) * dt
    gas_out = (s.fuel.sum() + s.boiler.sum() / plant.boiler_eff + s.flare.sum()) * dt
    heat_in = (plant.eta_th * s.fuel.sum() + s.boiler.sum()) * dt
    heat_out = (q * len(s.power) + s.dump.sum()) * dt
    return gas_in - gas_out, heat_in - heat_out


@pytest.mark.parametrize("mult,hours", [(1.0, 2.0), (1.5, 6.0), (2.0, 12.0), (3.0, 24.0)])
def test_physics_close(hourly_day, mult, hours):
    plant = design_plant(mult, hours)
    q = 0.2
    s = optimize_day(hourly_day.price, hourly_day.price, plant, 1.0, q)
    gas, heat = _balances(s, plant, q)
    assert abs(gas) < 1e-6          # cyclic storage: the day's inflow is used up
    assert abs(heat) < 1e-6         # cyclic heat tank
    smin, smax = plant.storage_mwh * plant.storage_min_frac, plant.storage_mwh * plant.storage_max_frac
    assert s.storage.min() >= smin - 1e-6 and s.storage.max() <= smax + 1e-6
    for e, en in enumerate(plant.engines):
        p = s.engine_power[e]
        assert np.all((p == 0) | (p >= en.p_min - 1e-6))
        assert np.all(p <= en.p_max + 1e-6)
    # one net meter: import only when every engine is off
    assert np.all(s.imports[s.power > 0] < 1e-9)


def test_flexible_plant_avoids_negative_prices_and_beats_baseload(hourly_day):
    plant = design_plant(2.0, 12.0)
    p = hourly_day.price
    assert (p < 0).any()
    s = optimize_day(p, p, plant, 1.0, 0.2)
    assert s.power[p < 0].sum() == 0
    base = simulate([hourly_day], plant, {hourly_day.date: 0.2}, baseload=True)[0]
    assert settle(s, p, p, plant)["net"] > settle(base, p, p, plant)["net"]
    captured = (s.power * p).sum() / s.power.sum()
    assert captured > p.mean() + 50


def test_flat_prices_give_no_flexibility_value(hourly_day):
    plant = design_plant(2.0, 12.0)
    p = np.full(24, 400.0)
    s = optimize_day(p, p, plant, 1.0, 0.2)
    base = simulate([hourly_day], plant, {hourly_day.date: 0.2}, baseload=True)[0]
    # with flat prices the optimum is to burn the gas at the best efficiency, as baseload does
    assert settle(s, p, p, plant)["net"] == pytest.approx(settle(base, p, p, plant)["net"], rel=2e-3)


def test_quarter_hour_day_commits_per_clock_hour(week_days):
    day = next(d for d in week_days if d.dt == 0.25 and d.n == 96)
    plant = design_plant(2.5, 8.0)
    s = optimize_day(day.price, day.price, plant, 0.25, 0.15)
    blocks = s.on.reshape(len(plant.engines), 24, 4)
    assert np.all(blocks.min(axis=2) == blocks.max(axis=2))
    gas, heat = _balances(s, plant, 0.15)
    assert abs(gas) < 1e-6 and abs(heat) < 1e-6


def test_start_cost_counts_carried_state(hourly_day):
    plant = design_plant(1.0, 2.0)
    p = hourly_day.price
    cold = optimize_day(p, p, plant, 1.0, 0.2, u_prev=(0,))
    warm = optimize_day(p, p, plant, 1.0, 0.2, u_prev=(1,))
    assert cold.starts.sum() >= 1
    assert warm.starts.sum() <= cold.starts.sum()


def test_two_stage_close_to_joint_milp(week_days):
    day = next(d for d in week_days if d.dt == 0.25 and d.n == 96)
    plant = design_plant(2.0, 4.0)
    joint = optimize_day(day.price, day.price, plant, 0.25, 0.15, two_stage=False)
    two = optimize_day(day.price, day.price, plant, 0.25, 0.15, two_stage=True)
    assert two.objective <= joint.objective * (1 + 6e-3) + 1e-6   # joint is optimal up to its 0.5% MIP gap
    assert two.objective >= 0.99 * joint.objective
