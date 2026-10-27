"""Rule-based operation: baseload and the simple price-rank heuristic, simulated interval by interval.

Price-rank rule for delivery day D (what a plant without an optimiser would do):
    1. gas to burn today = inflow of the day + (storage level at midnight - target level)
    2. hours at full output k = that gas / gas use of all engines at full load
    3. run all engines at full load in the k hours with the highest (forecast or actual) price, the remainder of k
       at part load in the next hour, and stop the engines otherwise.
A storage guard overrides the plan: engines speed up (and, as a last resort, the flare burns gas) when the
storage would overflow and slow down when it would run empty. Heat comes from the engines and the heat tank; a
biogas boiler covers any shortfall from the gas storage. Storage and tank levels carry over from day to day.

Baseload runs the original (1x) engine at full load all the time, which burns exactly the gas inflow.

Usage:
    scheds = simulate(days, plant, plan=rank_plan(days, signals, plant), heat=heat_by_day)
"""
from __future__ import annotations

import numpy as np

from biogas_flex.config import Engine, PlantParams
from biogas_flex.data import Day
from biogas_flex.milp import DaySchedule


def dispatch(target: float, engines: tuple[Engine, ...]) -> np.ndarray:
    """Split a power target over the engines, largest first; a remainder below an engine's minimum load is dropped."""
    out = np.zeros(len(engines))
    left = target
    for e in sorted(range(len(engines)), key=lambda k: -engines[k].p_max):
        en = engines[e]
        if left >= en.p_min - 1e-9:
            out[e] = min(en.p_max, left)
            left -= out[e]
    return out


def _fuel(power: np.ndarray, engines: tuple[Engine, ...]) -> float:
    return float(sum(en.fuel(p) for p, en in zip(power, engines)))


def _max_power_for_fuel(fuel: float, engines: tuple[Engine, ...]) -> float:
    """Largest total output whose fuel use (with the greedy dispatch) does not exceed ``fuel``."""
    lo, hi = 0.0, sum(e.p_max for e in engines)
    if _fuel(dispatch(hi, engines), engines) <= fuel:
        return hi
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if _fuel(dispatch(mid, engines), engines) <= fuel:
            lo = mid
        else:
            hi = mid
    return lo


def rank_targets(day: Day, signal: np.ndarray, plant: PlantParams, storage_level: float) -> np.ndarray:
    """Power target per interval for one day from the price-rank rule."""
    per = int(round(1.0 / day.dt)) if day.dt < 1.0 else 1
    hours = len(signal) // per
    hourly = np.asarray(signal, dtype=float).reshape(hours, per).mean(axis=1)
    s0 = plant.storage_mwh * plant.storage_start_frac
    gas = max(0.0, plant.gas_inflow * hours + storage_level - s0)
    full_fuel = _fuel(np.array([e.p_max for e in plant.engines]), plant.engines)
    k = min(float(hours), gas / full_fuel)
    order = np.argsort(-hourly, kind="stable")
    target_h = np.zeros(hours)
    whole = int(np.floor(k))
    target_h[order[:whole]] = plant.p_max
    if whole < hours and k - whole > 1e-9:
        target_h[order[whole]] = _max_power_for_fuel((k - whole) * full_fuel, plant.engines)
    return np.repeat(target_h, per)


def simulate(days: list[Day], plant: PlantParams, heat: dict, signals: dict | None = None,
             baseload: bool = False) -> list[DaySchedule]:
    """Run the rule interval by interval over consecutive days. ``signals``: {date: price array} for ranking."""
    engines = plant.engines
    E = len(engines)
    s_min, s_max = plant.storage_mwh * plant.storage_min_frac, plant.storage_mwh * plant.storage_max_frac
    S = plant.storage_mwh * plant.storage_start_frac
    T = 0.5 * plant.heat_tank_mwh
    on_prev = np.zeros(E)
    G, L = plant.gas_inflow, plant.own_use_mw
    out = []
    for day in days:
        n, dt = day.n, day.dt
        q = heat[day.date]
        if baseload:
            target = np.full(n, plant.base_power_mw)
        else:
            target = rank_targets(day, signals[day.date], plant, S)
        eng_p = np.zeros((E, n))
        rec = {k: np.zeros(n) for k in ("fuel", "storage", "flare", "boiler", "tank", "dump")}
        for t in range(n):
            p_e = dispatch(target[t], engines) if not baseload else _baseload_split(plant)
            fuel = _fuel(p_e, engines)
            s_next = S + dt * (G - fuel)
            if s_next > s_max + 1e-9:            # guard: burn more
                need = fuel + (s_next - s_max) / dt
                p_e = dispatch(_max_power_for_fuel(need, engines), engines)
                fuel = _fuel(p_e, engines)
            elif s_next < s_min - 1e-9:          # guard: burn less
                allow = max(0.0, fuel - (s_min - s_next) / dt)
                p_e = dispatch(_max_power_for_fuel(allow, engines), engines)
                fuel = _fuel(p_e, engines)
            S = S + dt * (G - fuel)
            flare = 0.0
            if S > s_max:
                flare = (S - s_max) / dt
                S = s_max
            T = T + dt * (plant.eta_th * fuel - q)
            dump = boiler = 0.0
            if T > plant.heat_tank_mwh:
                dump = (T - plant.heat_tank_mwh) / dt
                T = plant.heat_tank_mwh
            elif T < 0:
                boiler = min(-T / dt, plant.boiler_mw)
                gas_b = boiler / plant.boiler_eff * dt
                if S - gas_b < s_min:            # storage too low: take what is above the minimum
                    gas_b = max(0.0, S - s_min)
                    boiler = gas_b * plant.boiler_eff / dt
                S -= gas_b
                T = T + boiler * dt
            eng_p[:, t] = p_e
            rec["fuel"][t], rec["storage"][t], rec["flare"][t] = fuel, S, flare
            rec["boiler"][t], rec["tank"][t], rec["dump"][t] = boiler, T, dump
        on = (eng_p > 0).astype(float)
        starts = np.clip(np.diff(np.hstack([on_prev.reshape(E, 1), on]), axis=1), 0, None)
        on_prev = on[:, -1].copy()
        power = eng_p.sum(axis=0)
        net = power - L
        out.append(DaySchedule(power=power, engine_power=eng_p, on=on, starts=starts, fuel=rec["fuel"],
                               storage=rec["storage"], flare=rec["flare"], boiler=rec["boiler"], tank=rec["tank"],
                               dump=rec["dump"], export=np.clip(net, 0, None), imports=np.clip(-net, 0, None), dt=dt))
    return out


def _baseload_split(plant: PlantParams) -> np.ndarray:
    p = np.zeros(len(plant.engines))
    p[0] = plant.base_power_mw
    return p
