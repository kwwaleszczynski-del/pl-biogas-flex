from datetime import date, timedelta

import numpy as np
import pandas as pd

from biogas_flex.data import expected_quarters, first_quarter_hour_day, spread_hourly, to_days
from biogas_flex.forecast import profile_forecast, quarter_slots, trailing_mean
from conftest import quarter_frame


def test_expected_quarters_on_dst_days():
    assert expected_quarters(date(2025, 3, 30)) == 92
    assert expected_quarters(date(2025, 10, 26)) == 100
    assert expected_quarters(date(2025, 10, 27)) == 96


def test_hourly_products_collapse_and_switch_is_detected():
    days = to_days(quarter_frame(date(2025, 9, 28), 5, hourly_until=date(2025, 10, 1)))
    assert [d.dt for d in days] == [1.0, 1.0, 1.0, 0.25, 0.25]
    assert first_quarter_hour_day(days) == date(2025, 10, 1)
    assert all(d.n == 24 for d in days if d.dt == 1.0)
    assert all(d.n == 96 for d in days if d.dt == 0.25)


def test_dst_days_and_incomplete_days():
    q = quarter_frame(date(2025, 10, 25), 3)
    days = to_days(q)
    assert [d.n for d in days] == [96, 100, 96]
    autumn = days[1]
    assert sorted(set(quarter_slots(autumn))) == list(range(96))   # 02:00-02:45 appear twice
    days = to_days(q.iloc[:-1])                                      # last day loses a quarter-hour
    assert [d.date for d in days] == [date(2025, 10, 25), date(2025, 10, 26)]


def test_spread_hourly():
    idx = pd.date_range("2024-01-01 00:00", periods=3, freq="h", tz="UTC")
    hourly = pd.DataFrame({"start_utc": idx, "business_date": [date(2024, 1, 1)] * 3, "p": [1.0, 2.0, 3.0]})
    q = spread_hourly(hourly, "p")
    assert len(q) == 12
    assert q["p"].tolist() == [1.0] * 4 + [2.0] * 4 + [3.0] * 4
    assert (q["start_utc"].diff().dropna() == pd.Timedelta(minutes=15)).all()


def test_forecast_never_uses_the_delivery_day_or_later():
    q = quarter_frame(date(2025, 9, 20), 16, hourly_until=date(2025, 10, 1))
    base = profile_forecast(to_days(q))
    target = date(2025, 10, 3)
    changed = q.copy()
    changed.loc[changed["business_date"] >= target, "price"] += 1000.0
    after = profile_forecast(to_days(changed))
    np.testing.assert_allclose(after[target], base[target])
    assert not np.allclose(after[target + timedelta(days=1)], base[target + timedelta(days=1)])


def test_forecast_of_repeating_profile_is_that_profile():
    q = quarter_frame(date(2025, 10, 5), 9)
    one = q[q["business_date"] == date(2025, 10, 5)]["price"].to_numpy()
    q["price"] = np.tile(one, 9)
    days = to_days(q)
    fc = profile_forecast(days)
    np.testing.assert_allclose(fc[date(2025, 10, 13)], one)
    hourly_like = to_days(quarter_frame(date(2025, 9, 20), 9, hourly_until=date(2025, 10, 1)))
    fch = profile_forecast(hourly_like)
    assert len(fch[date(2025, 9, 28)]) == 24


def test_trailing_mean_uses_previous_days_only():
    days = to_days(quarter_frame(date(2025, 10, 5), 4))
    tr = trailing_mean(days, window=1)
    assert tr[date(2025, 10, 6)] == np.mean(days[0].price)
    assert date(2025, 10, 5) not in tr
