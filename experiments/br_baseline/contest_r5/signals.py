"""The six selectors of the protocol (§1, §2): DY, Q, V, M, L and QVM, at a decision date."""

from __future__ import annotations

import numpy as np
import pandas as pd
from contest_r5 import data

SECTOR_CAP, HOLDINGS = 2, 10


def pct(x: pd.Series) -> pd.Series:
    """(average rank - 1) / (n - 1); 0.5 when n = 1."""
    x = x.dropna()
    if len(x) <= 1:
        return pd.Series(0.5, index=x.index)
    return (x.rank(method="average") - 1) / (len(x) - 1)


class Signals:
    def __init__(self, market, codes: dict, sectors: dict, dfp: pd.DataFrame):
        self.m = market
        self.codes = codes
        self.sectors = sectors
        self.dfp = dfp
        self.sessions = market.sessions
        self.tr = {c: k.total_return(self.sessions) for c, k in codes.items()}

    # -- helpers ----------------------------------------------------------------------------------
    def _factor_between(self, code: str, start: pd.Timestamp, end: pd.Timestamp) -> float:
        """Product of share factors of events in (start, end]."""
        f = 1.0
        for d, v in self.codes[code].splits.items():
            if start < d <= end:
                f *= v
        return f

    def _close(self, code: str, t: pd.Timestamp) -> float | None:
        c = self.codes[code].close.loc[:t]
        return float(c.iloc[-1]) if len(c) and c.index[-1] >= t - pd.Timedelta(days=10) else None

    # -- signals ------------------------------------------------------------------------------------
    def dy(self, code: str, t: pd.Timestamp) -> tuple[float | None, bool]:
        """DY24 on today's shares and whether it paid in each of the three 12-month windows."""
        k = self.codes[code]
        close = self._close(code, t)
        if close is None or not len(k.proventos):
            return None, False
        prov = k.proventos.loc[:t]
        if not len(prov):
            return None, False
        # Each provento in today's shares: divided by the share factors since its ex-date.
        per_share = pd.Series({d: v / self._factor_between(code, d, t) for d, v in prov.items()}, dtype=float)
        windows = [per_share[(per_share.index > t - pd.DateOffset(months=12 * (i + 1)))
                             & (per_share.index <= t - pd.DateOffset(months=12 * i))].sum() for i in range(3)]
        return (windows[0] + windows[1]) / 2 / close, all(w > 0 for w in windows)

    def momentum_and_vol(self, code: str, t: pd.Timestamp) -> tuple[float | None, float | None]:
        i = self.sessions.get_indexer([t])[0]
        if i < 252:
            return None, None
        traded = self.codes[code].close.reindex(self.sessions[i - 252:i + 1]).notna().sum()
        if traded < 253:
            return None, None
        tr = self.tr[code]
        m = float(tr.iloc[i - 21] / tr.iloc[i - 252] - 1)
        r = tr.iloc[i - 251:i + 1].pct_change().dropna()
        return m, float(-r.std(ddof=1) * np.sqrt(252))

    def _annual(self, cnpj: str, t: pd.Timestamp) -> pd.DataFrame:
        """Annual statements public by ``t``: the kept version of each year, if delivered by then."""
        rows = self.dfp[(self.dfp["cnpj"] == cnpj)]
        rows = rows.sort_values("version").groupby("refer").tail(1)          # the kept (latest) version
        return rows[rows["received"] <= t].sort_values("refer")

    def quality(self, cnpj: str, t: pd.Timestamp) -> tuple[float, float, float] | None:
        a = self._annual(cnpj, t)
        if not len(a) or (t - a["refer"].iloc[-1]).days > 550:
            return None
        a = a.set_index(a["refer"].dt.year)
        a = a[~a.index.duplicated(keep="last")]           # a changed fiscal year-end: the later statement
        last = int(a.index[-1])
        years = [last - 2, last - 1, last]
        if any(y not in a.index for y in [last - 3] + years):
            return None
        eq = a["equity"]
        if (eq.loc[[last - 3] + years] <= 0).any() or not (a.loc[last, "assets"] > 0):
            return None
        roe = [a.loc[y, "net_income"] / ((eq.loc[y - 1] + eq.loc[y]) / 2) for y in years]
        if any(pd.isna(x) for x in roe):
            return None
        return float(np.mean(roe)), float(np.std(roe, ddof=1)), float(a.loc[last, "equity"] / a.loc[last, "assets"])

    def value(self, code: str, cnpj: str, t: pd.Timestamp) -> float | None:
        cls = code[4:]
        column = {"3": "eps_on", "4": "eps_pn", "5": "eps_pn", "6": "eps_pn"}.get(cls)
        if column is None:                                  # units: no reconciled earnings per unit (§2)
            return None
        a = self._annual(cnpj, t)
        if not len(a) or (t - a["refer"].iloc[-1]).days > 550:
            return None
        eps = a[column].iloc[-1]
        close = self._close(code, t)
        if pd.isna(eps) or eps <= 0 or close is None:
            return None
        return float(eps / self._factor_between(code, a["refer"].iloc[-1], t) / close)

    # -- the table and the selections ---------------------------------------------------------------
    def table(self, t: pd.Timestamp) -> pd.DataFrame:
        u = self.m.universe(t)
        rows = []
        for _, r in u.iterrows():
            code, cnpj = r["code"], r["company"]
            dy, constant = self.dy(code, t)
            mom, vol = self.momentum_and_vol(code, t)
            q = self.quality(cnpj, t)
            rows.append({"code": code, "company": cnpj, "traded": r["traded"],
                         "sector": data.sector_of(cnpj, self.sectors, t.year),
                         "dy24": dy, "constant": constant, "M": mom, "L": vol,
                         "roe_mean": q[0] if q else None, "roe_sd": q[1] if q else None,
                         "capital": q[2] if q else None, "V": self.value(code, cnpj, t),
                         "proventos_source": self.codes[code].source})
        f = pd.DataFrame(rows)
        for col in ("dy24", "M", "L", "roe_mean", "roe_sd", "capital", "V"):
            f[col] = pd.to_numeric(f[col], errors="coerce")
        valid = f["roe_mean"].notna()
        cap = pd.Series(np.nan, index=f.index)
        for _sector, idx in f[valid].groupby("sector").groups.items():
            cap.loc[idx] = pct(f.loc[idx, "capital"]) if len(idx) >= 3 else 0.5
        f["Q"] = (pct(f.loc[valid, "roe_mean"]) + pct(-f.loc[valid, "roe_sd"]) + cap[valid]) / 3
        both = f[["Q", "V", "M"]].notna().all(axis=1)
        f["QVM"] = (pct(f.loc[both, "Q"]) + pct(f.loc[both, "V"]) + pct(f.loc[both, "M"])) / 3
        return f

    @staticmethod
    def select(f: pd.DataFrame, arm: str) -> list[str]:
        if arm == "DY":
            pool = f[f["constant"] & f["dy24"].notna()]
            return list(pool.sort_values(["dy24", "company"], ascending=[False, True])["code"].head(HOLDINGS))
        pool = f[f[arm].notna() & f["sector"].notna()].sort_values([arm, "company"], ascending=[False, True])
        chosen, per = [], {}
        for _, r in pool.iterrows():
            if per.get(r["sector"], 0) >= SECTOR_CAP:
                continue
            chosen.append(r["code"])
            per[r["sector"]] = per.get(r["sector"], 0) + 1
            if len(chosen) == HOLDINGS:
                break
        return chosen
