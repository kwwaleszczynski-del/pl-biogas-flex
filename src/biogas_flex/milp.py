"""Daily dispatch of a biogas CHP plant as a mixed-integer linear program (HiGHS via scipy).

For one delivery day with n intervals of length dt hours, engines e = 1..E and an export value r_t (PLN/MWh, the
market price or a support-scheme rate) the plant chooses engine power P_et, hourly on/off state u_eh, gas storage
level S_t, flare X_t, boiler heat B_t, heat tank level T_t, dumped heat D_t, export Ex_t and import Im_t to maximise

    sum_t dt * [ r_t Ex_t - (p_t + fee) Im_t - m sum_e P_et ] - c_start sum_eh v_eh

subject to
    Pmin_e u_eh(t) <= P_et <= Pmax_e u_eh(t),  u binary               (minimum load; commitment per clock hour)
    F_et = a_e u_eh(t) + b_e P_et                                     (affine fuel curve = part-load efficiency)
    v_eh - w_eh = u_eh - u_e,h-1,  sum_{last UT} v <= u,  sum_{last DT} w <= 1 - u   (starts, min up/down time)
    S_t = S_t-1 + dt (G - sum_e F_et - B_t / eta_b - X_t),  S_min <= S_t <= S_max,  S_end = S_start
    T_t = T_t-1 + dt (eta_th sum_e F_et + B_t - q_t - D_t),  0 <= T_t <= T_max,  T_end = T_start,  B_t <= q_t
    sum_e P_et - L = Ex_t - Im_t,  Im_t <= L (1 - u_eh(t))           (own consumption L, one net meter)

Engines are committed per clock hour, which matches the one-hour minimum up and down times. Quarter-hour days are
solved in two stages: (1) the MILP on hourly mean prices fixes the hourly commitment, (2) a linear program with that
commitment fixed sets the output of every quarter-hour between minimum and full load. A joint quarter-hour MILP
spent 1-15 s per day in HiGHS's root node with three engines and small storage, too slow for the design grid; the
loss of the two-stage plan against it is measured in `validate_two_stage` and reported in the README. The MILP stops
at a 0.5% relative gap (on sample days the incumbent was within 0.03% of the 0.1%-gap solution at 2-5x less time);
no wall-clock limit binds in normal runs (120 s safety), so results do not depend on machine load.

The gas inflow G is constant, so over a day the plant burns the same gas whatever the strategy: flexibility only
moves production in time. Flaring and boiler heat carry penalties in the objective only (flaring is a safety
device, not a market option); they are not part of the reported revenue.

Usage:
    from biogas_flex.milp import optimize_day
    sched = optimize_day(prices, prices, plant, dt_hours=1.0, heat_demand=0.2)
    sched.power, sched.starts
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import sparse
from scipy.optimize import Bounds, LinearConstraint, milp

from biogas_flex.config import PlantParams


@dataclass
class DaySchedule:
    """Physical schedule of one day; settlement is done separately (see settlement.py)."""
    power: np.ndarray          # MW, total electric output per interval
    engine_power: np.ndarray   # MW, (E, n)
    on: np.ndarray             # 0/1, (E, n)
    starts: np.ndarray         # (E, n), 1 in the interval where an engine starts
    fuel: np.ndarray           # MW gas burnt in engines per interval
    storage: np.ndarray        # MWh gas at the end of each interval
    flare: np.ndarray          # MW gas
    boiler: np.ndarray         # MW heat
    tank: np.ndarray           # MWh heat at the end of each interval
    dump: np.ndarray           # MW heat
    export: np.ndarray         # MW
    imports: np.ndarray        # MW
    dt: float
    objective: float = float("nan")
    gap: float = 0.0           # relative MIP gap reached by the solver (0 for rule-based schedules)

    @property
    def last_on(self) -> tuple[int, ...]:
        return tuple(int(round(x)) for x in self.on[:, -1]) if self.on.size else ()


def _window_sum(n: int, k: int) -> sparse.csr_matrix:
    """Row t sums columns max(0, t-k+1) .. t."""
    rows, cols = [], []
    for t in range(n):
        for tau in range(max(0, t - k + 1), t + 1):
            rows.append(t)
            cols.append(tau)
    return sparse.csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n))


class _Model:
    """Tiny helper: named variable blocks and block-sparse constraint rows."""

    def __init__(self):
        self.sizes: dict[str, int] = {}
        self.rows, self.lo, self.hi = [], [], []

    def var(self, name: str, size: int):
        self.sizes[name] = size

    def slice(self, name: str) -> slice:
        off = 0
        for k, s in self.sizes.items():
            if k == name:
                return slice(off, off + s)
            off += s
        raise KeyError(name)

    @property
    def nv(self) -> int:
        return sum(self.sizes.values())

    def add(self, cols: dict[str, sparse.spmatrix], lower, upper):
        nrows = next(iter(cols.values())).shape[0]
        blocks = [cols[k] if k in cols else sparse.csr_matrix((nrows, s)) for k, s in self.sizes.items()]
        self.rows.append(sparse.hstack(blocks, format="csr"))
        self.lo.append(np.broadcast_to(np.asarray(lower, dtype=float), (nrows,)))
        self.hi.append(np.broadcast_to(np.asarray(upper, dtype=float), (nrows,)))


def optimize_day(export_value, import_price, plant: PlantParams, dt_hours: float, heat_demand,
                 u_prev: tuple[int, ...] | None = None, mip_gap: float = 5e-3, two_stage: bool = True,
                 time_limit: float = 120.0) -> DaySchedule:
    """Dispatch of one day. ``export_value`` and ``import_price`` in PLN/MWh, one value per interval.

    Quarter-hour days are solved in two stages when ``two_stage``: the hourly commitment from the MILP on hourly
    mean values, then the quarter-hour output from a linear program with that commitment fixed (see module notes).
    """
    r = np.asarray(export_value, dtype=float)
    p = np.asarray(import_price, dtype=float)
    if np.isnan(r).any() or np.isnan(p).any():
        raise ValueError("prices contain NaN")
    if dt_hours < 1.0 and two_stage:
        per = int(round(1.0 / dt_hours))
        q = np.broadcast_to(np.asarray(heat_demand, dtype=float), (len(r),))
        hourly = _solve(r.reshape(-1, per).mean(axis=1), p.reshape(-1, per).mean(axis=1), plant, 1.0,
                        q.reshape(-1, per).mean(axis=1), u_prev, mip_gap, time_limit)
        quarter = _solve(r, p, plant, dt_hours, heat_demand, u_prev, mip_gap, time_limit, fixed_on=hourly.on)
        quarter.gap = hourly.gap          # the LP stage has no gap of its own
        return quarter
    return _solve(r, p, plant, dt_hours, heat_demand, u_prev, mip_gap, time_limit)


def _solve(r: np.ndarray, p: np.ndarray, plant: PlantParams, dt_hours: float, heat_demand, u_prev, mip_gap: float,
           time_limit: float, fixed_on: np.ndarray | None = None) -> DaySchedule:
    """The MILP (or, with ``fixed_on`` = hourly on/off per engine, the LP of the quarter-hour stage)."""
    n, dt = len(r), float(dt_hours)
    per_hour = int(round(1.0 / dt)) if dt < 1.0 else 1
    if n % per_hour:
        raise ValueError(f"{n} intervals of {dt} h do not fill whole hours")
    H = n // per_hour
    hour_of = np.arange(n) // per_hour
    Mh = sparse.csr_matrix((np.ones(n), (np.arange(n), hour_of)), shape=(n, H))   # interval -> its hour
    q = np.broadcast_to(np.asarray(heat_demand, dtype=float), (n,)).copy()
    engines = plant.engines
    E = len(engines)
    u_prev = tuple(u_prev) if u_prev is not None else (0,) * E
    G = plant.gas_inflow
    s_min, s_max = plant.storage_mwh * plant.storage_min_frac, plant.storage_mwh * plant.storage_max_frac
    s0 = plant.storage_mwh * plant.storage_start_frac
    t_max = plant.heat_tank_mwh
    t0 = 0.5 * t_max
    hstep = dt * per_hour
    up = max(1, int(round(plant.min_up_h / hstep)))
    down = max(1, int(round(plant.min_down_h / hstep)))
    L = plant.own_use_mw

    m = _Model()
    for e in range(E):
        m.var(f"P{e}", n)
        m.var(f"u{e}", H)
        m.var(f"v{e}", H)
        m.var(f"w{e}", H)
    for name in ("S", "X", "B", "T", "D", "Ex", "Im"):
        m.var(name, n)

    In, IH = sparse.identity(n, format="csr"), sparse.identity(H, format="csr")
    shift_n = sparse.eye(n, k=-1, format="csr")
    shift_h = sparse.eye(H, k=-1, format="csr")
    for e, en in enumerate(engines):
        m.add({f"P{e}": In, f"u{e}": -en.p_max * Mh}, -np.inf, 0.0)
        m.add({f"P{e}": -In, f"u{e}": en.p_min * Mh}, -np.inf, 0.0)
        rhs = np.zeros(H)
        rhs[0] = -u_prev[e]
        m.add({f"v{e}": IH, f"w{e}": -IH, f"u{e}": -(IH - shift_h)}, rhs, rhs)
        if up > 1:
            m.add({f"v{e}": _window_sum(H, up), f"u{e}": -IH}, -np.inf, 0.0)
        if down > 1:
            m.add({f"w{e}": _window_sum(H, down), f"u{e}": IH}, -np.inf, 1.0)
        m.add({"Im": In, f"u{e}": L * Mh}, -np.inf, L)   # one net meter: no import while an engine runs
    for e in range(E - 1):                               # symmetry breaking for identical engines
        if engines[e] == engines[e + 1]:
            m.add({f"u{e + 1}": IH, f"u{e}": -IH}, -np.inf, 0.0)

    # gas balance: S_t - S_t-1 + dt*(fuel + B/eta_b + X) = dt*G  (+ s0 at t = 0)
    cols = {"S": In - shift_n, "B": (dt / plant.boiler_eff) * In, "X": dt * In}
    for e, en in enumerate(engines):
        cols[f"u{e}"] = dt * en.fuel_intercept * Mh
        cols[f"P{e}"] = dt * en.fuel_slope * In
    rhs = np.full(n, dt * G)
    rhs[0] += s0
    m.add(cols, rhs, rhs)
    end = sparse.csr_matrix(([1.0], ([0], [n - 1])), shape=(1, n))
    m.add({"S": end}, s0, s0)

    # heat balance: T_t - T_t-1 - dt*(eta_th*fuel + B - D) = -dt*q  (+ t0 at t = 0)
    cols = {"T": In - shift_n, "B": -dt * In, "D": dt * In}
    for e, en in enumerate(engines):
        cols[f"u{e}"] = -dt * plant.eta_th * en.fuel_intercept * Mh
        cols[f"P{e}"] = -dt * plant.eta_th * en.fuel_slope * In
    rhs = -dt * q
    rhs[0] += t0
    m.add(cols, rhs, rhs)
    m.add({"T": end}, t0, t0)

    # grid exchange: sum P - Ex + Im = own use
    cols = {f"P{e}": In for e in range(E)}
    cols["Ex"], cols["Im"] = -In, In
    m.add(cols, L, L)

    nv = m.nv
    c, lb, ub, integrality = np.zeros(nv), np.zeros(nv), np.full(nv, np.inf), np.zeros(nv)
    for e, en in enumerate(engines):
        c[m.slice(f"P{e}")] = dt * plant.maintenance
        c[m.slice(f"v{e}")] = plant.start_cost
        ub[m.slice(f"P{e}")] = en.p_max
        for k in ("u", "v", "w"):
            ub[m.slice(f"{k}{e}")] = 1.0
        if fixed_on is None:
            integrality[m.slice(f"u{e}")] = 1
        else:
            lb[m.slice(f"u{e}")] = ub[m.slice(f"u{e}")] = fixed_on[e]
    c[m.slice("X")] = dt * plant.flare_penalty
    c[m.slice("B")] = dt * plant.boiler_penalty
    c[m.slice("Ex")] = -dt * r
    c[m.slice("Im")] = dt * (p + plant.import_fee)
    lb[m.slice("S")], ub[m.slice("S")] = s_min, s_max
    ub[m.slice("B")] = np.minimum(q, plant.boiler_mw)
    ub[m.slice("T")] = t_max

    A = sparse.vstack(m.rows, format="csr")
    options = {"disp": False, "mip_rel_gap": mip_gap, "time_limit": time_limit}
    res = milp(c, constraints=LinearConstraint(A, np.concatenate(m.lo), np.concatenate(m.hi)), bounds=Bounds(lb, ub),
               integrality=integrality, options=options)
    if res.x is None:
        raise RuntimeError(f"MILP failed: {res.message}")
    x = res.x

    def get(name: str) -> np.ndarray:
        v = x[m.slice(name)].copy()
        v[np.abs(v) < 1e-9] = 0.0
        return v
    on_h = np.array([np.round(get(f"u{e}")) for e in range(E)]).reshape(E, H)
    on = on_h[:, hour_of]
    eng_p = np.where(on > 0, np.array([get(f"P{e}") for e in range(E)]).reshape(E, n), 0.0)
    prev = np.array(u_prev, dtype=float).reshape(E, 1)
    starts = np.clip(np.diff(np.hstack([prev, on]), axis=1), 0, None)
    fuel = np.zeros(n)
    for e, en in enumerate(engines):
        fuel += en.fuel_intercept * on[e] + en.fuel_slope * eng_p[e]
    return DaySchedule(power=eng_p.sum(axis=0), engine_power=eng_p, on=on, starts=starts, fuel=fuel,
                       storage=get("S"), flare=get("X"), boiler=get("B"), tank=get("T"), dump=get("D"),
                       export=get("Ex"), imports=get("Im"), dt=dt, objective=-float(res.fun),
                       gap=float(getattr(res, "mip_gap", 0.0) or 0.0))


def validate_two_stage(days, plant: PlantParams, heat: float = 0.2) -> dict:
    """Objective of the two-stage plan against the joint quarter-hour MILP on the given quarter-hour days."""
    rows = []
    for d in days:
        joint = optimize_day(d.price, d.price, plant, d.dt, heat, two_stage=False)
        two = optimize_day(d.price, d.price, plant, d.dt, heat, two_stage=True)
        rows.append((joint.objective, two.objective))
    joint_sum = sum(j for j, _ in rows)
    two_sum = sum(t for _, t in rows)
    return {"days": len(rows), "joint": joint_sum, "two_stage": two_sum,
            "loss_pct": 100 * (joint_sum - two_sum) / abs(joint_sum) if joint_sum else 0.0,
            "worst_day_pct": max(100 * (j - t) / abs(j) for j, t in rows) if rows else 0.0}
