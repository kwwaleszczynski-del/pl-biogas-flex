"""Open data: Polish day-ahead prices (PSE), German prices and biomass generation (SMARD), EUR/PLN (ECB).

Prices are first put on a common quarter-hour grid (one row per 15-minute delivery interval) and then cut into
delivery days. A day whose four quarter-hours carry the same price in every hour is an hourly product and is
optimised with one-hour intervals; otherwise with 15-minute intervals. Both markets switched to 15-minute
day-ahead products on delivery day 2025-10-01 (SDAC); the switch is detected in the data, not hard-coded.

Sources:
    PL  2024-06-14 onwards: SDAC price of the Polish bidding zone, PSE report `csdac-pln` (via pse-opendata)
        before:              hourly RCE from the PSE legacy export `PL_CENY_RYN_EN`
    DE  SMARD (Bundesnetzagentur, CC BY 4.0): day-ahead price DE-LU (filter 4169), biomass generation (4066)
    FX  ECB reference rate PLN per EUR (EXR.D.PLN.EUR.SP00.A)

Usage:
    client = make_client(Path("data"))
    q = load_pl_prices(client, date(2024, 1, 1), date(2026, 10, 25))
    days = to_days(q)
"""
from __future__ import annotations

import io
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from biogas_flex.config import API_FIRST_DAY, TZ

log = logging.getLogger(__name__)

SMARD_URL = "https://www.smard.de/app/chart_data"
SMARD_PRICE = (4169, "DE-LU")      # day-ahead price, EUR/MWh
SMARD_BIOMASS = (4066, "DE")       # net generation from biomass, MWh per interval
ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.PLN.EUR.SP00.A"
QUARTER_COLUMNS = ["start_utc", "business_date", "price"]


# ---------------------------------------------------------------- Poland (PSE)

def make_client(data_dir: Path | None, offline: bool = False):
    """pse-opendata client with its monthly cache in ``<data_dir>/pse``."""
    import pse_opendata as pse
    cache = Path(data_dir) / "pse" if data_dir is not None else None
    return pse.Client(cache_dir=cache, offline=offline)


def _month_end(day: date) -> date:
    """Requests run to the end of the month, so the client caches the current month too."""
    return (pd.Timestamp(day) + pd.offsets.MonthEnd(0)).date()


def spread_hourly(hourly: pd.DataFrame, column: str) -> pd.DataFrame:
    """Hourly rows -> four quarter-hour rows with the same value and business date."""
    parts = []
    for k in range(4):
        part = hourly[["start_utc", "business_date", column]].copy()
        part["start_utc"] = part["start_utc"] + pd.Timedelta(minutes=15 * k)
        parts.append(part)
    return pd.concat(parts, ignore_index=True).sort_values("start_utc", ignore_index=True)


def load_pl_prices(client, start: date, end: date) -> pd.DataFrame:
    """Quarter-hour day-ahead prices for Poland (PLN/MWh), delivery days ``start``..``end``."""
    frames = []
    if start < API_FIRST_DAY:
        legacy = client.history("rce", start, min(end, API_FIRST_DAY - timedelta(days=1)))
        legacy = legacy[legacy["source"] == "legacy"].rename(columns={"rce": "price"})
        frames.append(spread_hourly(legacy, "price"))
    if end >= API_FIRST_DAY:
        api = client.get("csdac-pln", max(start, API_FIRST_DAY), _month_end(end), dropna=True)
        api = api[api["business_date"] <= end]
        frames.append(api.rename(columns={"csdac_pln": "price"})[QUARTER_COLUMNS])
    return _tidy(pd.concat(frames, ignore_index=True))


def _tidy(q: pd.DataFrame) -> pd.DataFrame:
    q = q.dropna(subset=["price"]).copy()
    q["start_utc"] = pd.to_datetime(q["start_utc"], utc=True).dt.as_unit("ns")
    q = q.drop_duplicates("start_utc").sort_values("start_utc", ignore_index=True)
    return q[QUARTER_COLUMNS]


# ---------------------------------------------------------------- Germany (SMARD) and FX (ECB)

def _get(url: str, retries: int = 4) -> requests.Response:
    for k in range(retries):
        try:
            r = requests.get(url, timeout=60, headers={"User-Agent": "pl-biogas-flex (open-data research)"})
            if r.status_code == 200:
                return r
            log.warning("HTTP %s for %s", r.status_code, url)
        except requests.RequestException as exc:
            log.warning("%s for %s", exc, url)
        time.sleep(2 * 2 ** k)
    raise RuntimeError(f"download failed: {url}")


def smard_series(filter_id: int, region: str, start: date, end: date, cache_dir: Path,
                 resolution: str = "quarterhour", pause: float = 0.3) -> pd.Series:
    """SMARD time series (weekly JSON chunks, cached once the week is more than 10 days old)."""
    cache = Path(cache_dir) / "smard" / f"{filter_id}_{region}_{resolution}"
    cache.mkdir(parents=True, exist_ok=True)
    index = _get(f"{SMARD_URL}/{filter_id}/{region}/index_{resolution}.json").json()["timestamps"]
    lo = pd.Timestamp(start).tz_localize(TZ) - pd.Timedelta(days=8)
    hi = pd.Timestamp(end + timedelta(days=1)).tz_localize(TZ)
    now = datetime.now(timezone.utc)
    parts = []
    for ts in index:
        t0 = pd.Timestamp(ts, unit="ms", tz="UTC")
        if t0 < lo or t0 >= hi:
            continue
        path = cache / f"{ts}.json"
        if path.exists():
            series = json.loads(path.read_text())
        else:
            url = f"{SMARD_URL}/{filter_id}/{region}/{filter_id}_{region}_{resolution}_{ts}.json"
            series = _get(url).json()["series"]
            if now - t0.to_pydatetime() > timedelta(days=17):   # week ended more than 10 days ago
                path.write_text(json.dumps(series))
            time.sleep(pause)
        parts.extend(series)
    s = pd.Series({pd.Timestamp(t, unit="ms", tz="UTC"): v for t, v in parts}, dtype=float).sort_index()
    s = s[~s.index.duplicated(keep="last")].dropna()
    s.index = pd.DatetimeIndex(s.index).as_unit("ns")
    s.index.name = "start_utc"
    local = s.index.tz_convert(TZ)
    keep = (local.date >= start) & (local.date <= end)
    return s[keep]


def eur_pln(start: date, end: date, cache_dir: Path) -> pd.Series:
    """ECB reference rate (PLN per EUR) by calendar day, forward-filled over weekends and holidays."""
    path = Path(cache_dir) / "ecb" / f"eurpln_{start}_{end}.csv"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{ECB_URL}?startPeriod={start - timedelta(days=10)}&endPeriod={end}&format=csvdata"
        path.write_text(_get(url).text, encoding="utf-8")
    raw = pd.read_csv(io.StringIO(path.read_text(encoding="utf-8")), usecols=["TIME_PERIOD", "OBS_VALUE"])
    rate = raw.set_index(pd.to_datetime(raw["TIME_PERIOD"]))["OBS_VALUE"].astype(float)
    days = pd.date_range(start - timedelta(days=10), end, freq="D")
    rate = rate.reindex(days).ffill()
    return pd.Series(rate.values, index=[d.date() for d in days], name="pln_per_eur")


def load_de_prices(start: date, end: date, cache_dir: Path) -> pd.DataFrame:
    """Quarter-hour DE-LU day-ahead prices converted to PLN/MWh with the ECB rate of the delivery day."""
    eur = smard_series(*SMARD_PRICE, start, end, cache_dir)
    fx = eur_pln(start, end, cache_dir)
    q = pd.DataFrame({"start_utc": eur.index, "price_eur": eur.values})
    q["business_date"] = q["start_utc"].dt.tz_convert(TZ).dt.date
    q["fx"] = q["business_date"].map(fx)
    q["price"] = q["price_eur"] * q["fx"]
    return _tidy(q)


# ---------------------------------------------------------------- delivery days

@dataclass
class Day:
    date: date
    start_utc: np.ndarray      # datetime64[ns] interval starts (UTC)
    dt: float                  # hours: 1.0 (hourly product) or 0.25
    price: np.ndarray          # PLN/MWh per interval
    slot: np.ndarray           # local wall-clock quarter-hour of the interval start (0..95)

    @property
    def n(self) -> int:
        return len(self.price)

    def quarter_prices(self) -> np.ndarray:
        """Prices on the quarter-hour grid (hourly products repeated four times)."""
        return np.repeat(self.price, 4) if self.dt == 1.0 else self.price


def expected_quarters(day: date) -> int:
    start = pd.Timestamp(day).tz_localize(TZ)
    end = pd.Timestamp(day + timedelta(days=1)).tz_localize(TZ)
    return int((end - start) / pd.Timedelta(minutes=15))


def to_days(q: pd.DataFrame) -> list[Day]:
    """Cut a quarter-hour price frame into complete delivery days, collapsing hourly products to hours."""
    days = []
    for d, grp in q.groupby("business_date", sort=True):
        if len(grp) != expected_quarters(d):
            log.debug("skip %s: %d quarter-hours", d, len(grp))
            continue
        starts = grp["start_utc"].to_numpy(dtype="datetime64[ns]")
        price = grp["price"].to_numpy(dtype=float)
        blocks = price.reshape(-1, 4)
        hourly = bool(np.all(np.ptp(blocks, axis=1) < 1e-9))
        local = pd.DatetimeIndex(starts).tz_localize("UTC").tz_convert(TZ)
        slot = (local.hour * 4 + local.minute // 15).to_numpy()
        if hourly:
            days.append(Day(d, starts[::4], 1.0, blocks[:, 0].copy(), slot[::4]))
        else:
            days.append(Day(d, starts, 0.25, price, slot))
    return days


def first_quarter_hour_day(days: list[Day]) -> date | None:
    """First delivery day with quarter-hour products (prices differ within an hour)."""
    return next((d.date for d in days if d.dt < 1.0), None)


def biomass_generation(start: date, end: date, cache_dir: Path) -> pd.Series:
    """German net generation from biomass, MW (hourly mean of the SMARD hourly MWh values)."""
    s = smard_series(*SMARD_BIOMASS, start, end, cache_dir, resolution="hour")
    return s.rename("biomass_mw")
