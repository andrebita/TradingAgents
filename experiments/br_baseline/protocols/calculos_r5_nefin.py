"""Cálculos exploratórios da revisão 5. Não executa backtest nem chama LLM."""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
FILES = {
    "value_weighted": "nefin_factors_monthly.csv",
    "equally_weighted": "nefin_factors_equally_weighted_monthly.csv",
}


def mean_hac(values, lags=6):
    """Média aritmética anualizada e t da média com HAC Bartlett, divisor n."""
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    u = x - x.mean()
    long_variance = np.dot(u, u) / n
    for lag in range(1, lags + 1):
        long_variance += 2 * (1 - lag / (lags + 1)) * np.dot(u[lag:], u[:-lag]) / n
    se = np.sqrt(long_variance / n)
    return {"n_months": n, "annual_arithmetic_mean_pct": 1200 * x.mean(),
            "t_mean_HAC6": x.mean() / se}


result = {"retrieved": "2026-10-31", "interpretation":
          "Factor spreads, not investible net CAGRs. Unadjusted exploratory t statistics.",
          "nefin": {}, "hypothetical_wealth": {}}
for weighting, filename in FILES.items():
    source = ROOT / filename
    frame = pd.read_csv(source)
    assert not frame.duplicated(["year", "month"]).any()
    historic = frame.loc[frame.year.between(2001, 2011)]
    assert len(historic) == 132
    result["nefin"][weighting] = {
        "url": "https://nefin.com.br/nefindata/risk-factors/" + filename,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "period": "2001-01 through 2011-12",
        "factors": {factor: mean_hac(historic[factor]) for factor in ["HML", "WML"]}}

for annual_rate in [0.08, 0.09]:
    monthly_rate = (1 + annual_rate) ** (1 / 12) - 1
    wealth = 125000 * (1 + monthly_rate) ** 60 + 3000 * ((1 + monthly_rate) ** 60 - 1) / monthly_rate
    result["hypothetical_wealth"][str(annual_rate)] = wealth

(ROOT / "calculos.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))
