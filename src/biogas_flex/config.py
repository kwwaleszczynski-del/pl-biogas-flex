"""Plant, cost and support parameters with their sources, plus project-wide constants.

All money is in PLN. Every number that is not derived from data is an assumption and is listed, with its source,
in the README table "Assumptions". Statements about Polish support schemes are simplifications that need to be
checked against the law in force; this project is not legal or investment advice.

Usage:
    from biogas_flex.config import PlantParams, CostParams, design_plant
    plant = design_plant(multiplier=2.0, storage_h=12.0)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date

TZ = "Europe/Warsaw"
START = date(2024, 1, 1)
END = date(2026, 10, 25)
API_FIRST_DAY = date(2024, 6, 14)       # PSE reports API; legacy hourly RCE export before that
QUARTER_HOUR_SDAC = date(2025, 10, 1)   # first delivery day with 15-minute day-ahead products (checked in the data)

MODEL_VERSION = 1                       # bump to invalidate cached grid tasks after a model change
MULTIPLIERS = (1.0, 1.5, 2.0, 2.5, 3.0)
STORAGE_HOURS = (2.0, 4.0, 8.0, 12.0, 24.0)


@dataclass(frozen=True)
class Engine:
    p_max: float                 # MW electric
    min_load: float = 0.5        # fraction of p_max
    eta_full: float = 0.40       # electrical efficiency at full load
    eta_min: float = 0.36        # electrical efficiency at minimum load

    @property
    def p_min(self) -> float:
        return self.p_max * self.min_load

    @property
    def fuel_slope(self) -> float:
        """MW of gas per MW of electricity above the no-load intercept (affine fuel curve)."""
        return (self.p_max / self.eta_full - self.p_min / self.eta_min) / (self.p_max - self.p_min)

    @property
    def fuel_intercept(self) -> float:
        """MW of gas burnt by a running engine on top of the slope term."""
        return self.p_max / self.eta_full - self.fuel_slope * self.p_max

    def fuel(self, p: float) -> float:
        return self.fuel_intercept + self.fuel_slope * p if p > 0 else 0.0


@dataclass(frozen=True)
class PlantParams:
    """Agricultural biogas CHP plant: constant gas inflow, gas storage, 1-3 engines, heat obligation."""
    base_power_mw: float = 0.5           # rated power of the existing (1x) engine; the gas inflow is sized on it
    engines: tuple[Engine, ...] = (Engine(0.5),)
    storage_h: float = 2.0               # gross gas storage, hours of gas production
    storage_min_frac: float = 0.10
    storage_max_frac: float = 0.90
    storage_start_frac: float = 0.50     # start and end level of every day
    eta_th: float = 0.44                 # heat per unit of gas burnt in an engine
    min_up_h: float = 1.0
    min_down_h: float = 1.0
    start_cost: float = 50.0             # PLN per engine start (wear, extra gas)
    maintenance: float = 80.0            # PLN per MWh electric
    own_use_frac: float = 0.08           # constant own consumption, fraction of base_power_mw
    import_fee: float = 350.0            # PLN/MWh on top of the market price for electricity drawn from the grid
    heat_winter_frac: float = 0.45       # heat demand in mid-January, fraction of the 1x CHP heat output
    heat_summer_frac: float = 0.15       # heat demand in mid-July
    heat_tank_mwh: float = 0.0
    boiler_eff: float = 0.90
    boiler_mw: float = 1.0
    flare_penalty: float = 1000.0        # PLN per MWh of gas, optimisation only: flaring is a safety device
    boiler_penalty: float = 5.0          # PLN per MWh of heat, optimisation only: prefer CHP heat

    @property
    def gas_inflow(self) -> float:
        """MW of gas from the digester: what the 1x engine burns at full load."""
        return self.base_power_mw / self.engines[0].eta_full if self.engines else 0.0

    @property
    def p_max(self) -> float:
        return sum(e.p_max for e in self.engines)

    @property
    def multiplier(self) -> float:
        return self.p_max / self.base_power_mw

    @property
    def storage_mwh(self) -> float:
        return self.storage_h * self.gas_inflow

    @property
    def own_use_mw(self) -> float:
        return self.own_use_frac * self.base_power_mw

    @property
    def rated_heat_mw(self) -> float:
        return self.eta_th * self.gas_inflow


@dataclass(frozen=True)
class CostParams:
    """Investment and fixed costs of flexibilisation, relative to the existing 1x plant (PLN)."""
    chp_per_kw: float = 4000.0           # additional engine incl. installation and integration
    gas_storage_per_m3: float = 200.0    # additional double-membrane gas storage
    heat_tank_per_m3: float = 1200.0
    heat_tank_m3: float = 100.0          # built with any extra engine capacity
    grid_per_kw: float = 400.0           # connection upgrade for the extra capacity (placeholder, site specific)
    other_frac: float = 0.10             # planning, permits, controls
    fixed_opex_frac: float = 0.015       # insurance and servicing of the new equipment, per year
    existing_storage_h: float = 2.0      # gas storage the 1x plant already has
    biogas_kwh_per_m3: float = 5.5       # lower heating value of biogas (~55% methane)
    lifetime_years: int = 15

    def heat_tank_mwh(self, delta_t_k: float = 40.0) -> float:
        return self.heat_tank_m3 * 1.163 * delta_t_k / 1000.0


@dataclass(frozen=True)
class SupportParams:
    """Simplified support schemes. To be verified against the Polish RES act in force (see README)."""
    fixed_price: float = 894.0           # PLN/MWh, ~95% of the 2023 reference price 941 PLN/MWh (biogas CHP 0.5-1 MW)
    negative_run_hours: int = 6          # PL-like rule: no support in runs of >= 6 h of negative hourly prices


@dataclass(frozen=True)
class Design:
    multiplier: float
    storage_h: float
    plant: PlantParams = field(compare=False, repr=False, default_factory=PlantParams)

    @property
    def key(self) -> str:
        return f"{self.multiplier:g}x_{self.storage_h:g}h"


def engine_sizes(multiplier: float, base_power_mw: float = 0.5) -> list[float]:
    """Existing engine plus new engines of at most the base size: 1.5x -> [P, P/2], 3x -> [P, P, P]."""
    sizes = [base_power_mw]
    extra = round((multiplier - 1.0) * base_power_mw, 9)
    for _ in range(math.ceil(multiplier - 1.0 - 1e-9)):
        size = min(base_power_mw, extra)
        sizes.append(size)
        extra = round(extra - size, 9)
    return sizes


def design_plant(multiplier: float, storage_h: float, base: PlantParams | None = None,
                 costs: CostParams | None = None) -> PlantParams:
    """Plant for one point of the design grid; a heat tank comes with any extra engine capacity."""
    base = base or PlantParams()
    costs = costs or CostParams()
    proto = base.engines[0]
    engines = tuple(replace(proto, p_max=s) for s in engine_sizes(multiplier, base.base_power_mw))
    tank = costs.heat_tank_mwh() if multiplier > 1.0 else 0.0
    return replace(base, engines=engines, storage_h=storage_h, heat_tank_mwh=tank)


def capex(plant: PlantParams, costs: CostParams) -> dict[str, float]:
    """Incremental investment of a design compared with the existing 1x plant (PLN)."""
    extra_kw = max(0.0, plant.p_max - plant.base_power_mw) * 1000.0
    extra_h = max(0.0, plant.storage_h - costs.existing_storage_h)
    gas_m3_per_h = plant.gas_inflow * 1000.0 / costs.biogas_kwh_per_m3
    parts = {
        "chp": extra_kw * costs.chp_per_kw,
        "gas_storage": extra_h * gas_m3_per_h * costs.gas_storage_per_m3,
        "heat_tank": costs.heat_tank_m3 * costs.heat_tank_per_m3 if extra_kw > 0 else 0.0,
        "grid": extra_kw * costs.grid_per_kw,
    }
    parts["other"] = costs.other_frac * sum(parts.values())
    parts["total"] = sum(parts.values())
    return parts
