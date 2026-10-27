import json

import numpy as np
import pandas as pd
import pytest

from biogas_flex.backtest import SETTLEMENT_COLUMNS, STRATEGIES
from biogas_flex.config import CostParams
from biogas_flex.report import annualise, periods, site_data, summary, write_site


def _monthly() -> pd.DataFrame:
    rows = []
    for m, h in ((1.0, 2.0), (2.0, 12.0)):
        for k, st in enumerate(STRATEGIES):
            for month, days in (("2025-12", 31), ("2026-01", 31)):
                row = {c: 0.0 for c in SETTLEMENT_COLUMNS}
                gain = 0.0 if m == 1.0 else 100.0 * k
                row.update(multiplier=m, storage_h=h, strategy=st, scenario="merchant", month=month, days=days,
                           net=days * (3000.0 + gain), sales=days * 3500.0, market_value=days * 3500.0,
                           export_mwh=days * 11.0, mip_gap_mean=0.0, mip_gap_max=0.0)
                rows.append(row)
    return pd.DataFrame(rows)


def test_periods():
    p = periods(["2025-11", "2025-12", "2026-01"])
    assert p["all"] == ["2025-11", "2025-12", "2026-01"]
    assert p["2026"] == ["2026-01"]
    assert p["last12"] == ["2025-11", "2025-12", "2026-01"]


def test_annualise_and_economics():
    mon = _monthly()
    a = annualise(mon)
    base = a[(a["multiplier"] == 1.0) & (a["strategy"] == "baseload")]["net"].iloc[0]
    assert base == pytest.approx(3000.0 * 365.25)
    s = summary(mon, CostParams())
    pf = s[(s["multiplier"] == 2.0) & (s["strategy"] == "milp_perfect")].iloc[0]
    assert pf["extra_net"] == pytest.approx(400.0 * 365.25)
    assert pf["capex"] > 0
    assert np.isinf(pf["payback_years"]) or pf["payback_years"] > 0


def test_site_is_written_with_inline_data(tmp_path):
    data = site_data({"PL": _monthly()}, {}, {}, None, None, CostParams())
    write_site(data, tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "/*DATA*/null" not in html
    start = html.index("const D = ") + len("const D = ")
    payload = json.loads(html[start:html.index(";\nconst $")])
    assert payload["markets"]["PL"]["grid"]["2x_12h"]["milp_perfect"]["days"] == [31, 31]
    assert payload["capex"]["1x_2h"]["total"] == 0.0
