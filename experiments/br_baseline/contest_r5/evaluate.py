"""The frozen promotion criteria of the protocol (§6), applied to ``contest_report.json``.

Usage, from experiments/br_baseline: python -m contest_r5.evaluate
"""

from __future__ import annotations

import json

from contest_r5 import data

SELECTORS = ["DY", "Q", "V", "M", "L", "QVM"]


def criteria(r: dict, arm: str, benches: list[str]) -> dict:
    a = r["arms"][arm]
    out = {}
    for b in benches:
        bb = r["arms"][b]
        c = f"{arm}-{b}"
        sens_a, sens_b = r["sensitivities"][arm], r["sensitivities"][b]
        win = r.get("windows_60m", {}).get("cagr", {})
        share_windows = (sum(x > y for x, y in zip(win[arm], win[b], strict=True)) / len(win[arm])) if win else None
        out[b] = {
            "irr_adv": round(a["irr_after_liquidation"] - bb["irr_after_liquidation"], 4),
            "cagr_adv": round(a["cagr_unit"] - bb["cagr_unit"], 4),
            "1_min_1pp": a["irr_after_liquidation"] - bb["irr_after_liquidation"] >= 0.01
                         and a["cagr_unit"] - bb["cagr_unit"] >= 0.01,
            "p_holm": r["inference"]["block_12"]["p_holm"].get(c),
            "2_holm_below_5pct": (r["inference"]["block_12"]["p_holm"].get(c, 1) < 0.05),
            "subperiods_ahead": sum(x > y for x, y in zip(r["subperiod_cagr"][arm], r["subperiod_cagr"][b], strict=True)),
            "windows_ahead": None if share_windows is None else round(share_windows, 3),
            "3_subperiods_and_windows": sum(x > y for x, y in zip(r["subperiod_cagr"][arm], r["subperiod_cagr"][b],
                                                                  strict=True)) >= 2
                                        and (share_windows is not None and share_windows >= 0.6),
            "4_without_2016_and_best_year": r["robustness"][c]["without_2016"] > 0
                                            and r["robustness"][c]["without_best_year"] > 0,
            "5_drawdown_within_5pp": abs(a["drawdown_daily"]["max"]) - abs(bb["drawdown_daily"]["max"]) <= 0.05,
            "6_terminal_in_all_sensitivities": all(sens_a[k] > sens_b[k] for k in sens_a)
                                               and a["final_after_liquidation"] > bb["final_after_liquidation"],
        }
        out[b]["passes"] = all(v for k, v in out[b].items() if k[0].isdigit())
    return out


def main():
    r = json.loads((data.OUT / "contest_report.json").read_text(encoding="utf-8"))
    result = {arm: criteria(r, arm, ["BOVA11", "DIVO11"]) for arm in SELECTORS}
    result["DIVO11"] = criteria(r, "DIVO11", ["BOVA11"])
    result["verdict"] = {arm: all(v["passes"] for v in result[arm].values()) for arm in SELECTORS + ["DIVO11"]}
    (data.OUT / "contest_criteria.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result["verdict"], indent=1))
    for arm in SELECTORS + ["DIVO11"]:
        for b, v in result[arm].items():
            print(arm, "vs", b, {k: v[k] for k in ("irr_adv", "cagr_adv", "p_holm", "subperiods_ahead",
                                                    "windows_ahead", "passes")})


if __name__ == "__main__":
    main()
