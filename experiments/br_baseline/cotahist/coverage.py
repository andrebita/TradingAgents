"""Top-50 companies by traded value at each June/December decision (COTAHIST) and which lack Yahoo data.

Usage, from experiments/br_baseline: python cotahist/coverage.py <spot.csv.gz> > results/cotahist/coverage.txt
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, ".")
from dividend_tilt import fca

CACHE = Path("results/dividends/cache")
spot = pd.read_csv(sys.argv[1], parse_dates=["date"], dtype={"codbdi": str, "dismes": str})
st = spot[spot["codbdi"] == "02"]
codes, _ = fca(CACHE)
company = {c: cnpj for cnpj, cs in codes.items() for c in cs}
st = st.assign(company=st["code"].map(company).fillna("ISIN:" + st["isin"].str[2:6]))
sessions = st["date"].drop_duplicates().sort_values()
ends = sessions.groupby(sessions.dt.to_period("M")).max()
dates = [d for d in ends if d >= pd.Timestamp("2014-06-01") and d.month in (6, 12)]
piv = st.pivot_table(index="date", columns="code", values="volume", aggfunc="sum")
comp_of = st.drop_duplicates("code").set_index("code")["company"]
yahoo = {p.stem for p in (CACHE / "prices").glob("*.csv") if p.stat().st_size > 200}
rows = []
for t in dates:
    w = piv.loc[:t].tail(63)
    med = w.median().dropna()
    med = med[w.count() >= 42]
    best = med.groupby(comp_of.reindex(med.index)).idxmax()
    traded = med.loc[best.values].sort_values(ascending=False).head(50)
    missing = [c for c in traded.index if c not in yahoo]
    rows.append({"date": t.date(), "missing": len(missing), "codes": " ".join(missing),
                 "share_of_volume": round(float(traded[missing].sum() / traded.sum()), 3)})
out = pd.DataFrame(rows)
print(out.to_string())
