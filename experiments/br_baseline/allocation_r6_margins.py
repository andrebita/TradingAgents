"""Review 6, exploratory (post hoc): P(beat Selic by a margin) for three risk levels, on the bootstrap of
allocation_r5 (same paths, seed, executor). Run from experiments/br_baseline: python allocation_r6_margins.py
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path.cwd()))
import allocation_r5 as a  # noqa: E402

out = Path("results/allocation_r5")
cache = out / "cache"
daily, _ = a.daily_indexes(cache)
m = a.monthly_returns(daily, a.sgs(433, cache))
arr = m[["bova", "glob", "selic", "ipca"]].to_numpy()
fx = a.sgs(1, cache)
fx_m = fx.groupby(fx.index.to_period("M")).last().pct_change().reindex(m.index).fillna(0.0)
drift = float((1 + fx_m).prod() ** (1 / len(fx_m)) - 1)
neutral = arr.copy()
neutral[:, 1] = (1 + neutral[:, 1]) / (1 + drift) - 1
rng = np.random.default_rng(a.SEED)
n = len(arr)
starts = rng.integers(0, n, size=(a.PATHS, a.WINDOW // a.BLOCK))
idx_all = ((starts[:, :, None] + np.arange(a.BLOCK)) % n).reshape(a.PATHS, a.WINDOW)
months = [((i % 12) + 1) for i in range(a.WINDOW)]
mixes = {"60% ações (20/40/40, atual)": (0.20, 0.40, 0.40), "45% ações (15/30/55)": (0.15, 0.30, 0.55),
         "30% ações (10/20/70)": (0.10, 0.20, 0.70)}
margins = [0.0, 0.005, 0.01, 0.02]
for label, data in (("com o câmbio histórico", arr), ("sem a desvalorização média do real", neutral)):
    print(f"## {label}")
    for name, w in mixes.items():
        ratios = np.array([(lambda r: r["final"] / r["final_selic"])(a.run(data[i], months, w, a.MAIN_SCALE))
                           for i in idx_all])
        probs = [float((ratios >= (1 + mm) ** 5).mean()) for mm in margins]
        stress = 0.5 * w[0] + 0.35 * w[1]
        print(name, "| P(>Selic):", " ".join(f"+{mm:.1%}/ano={p:.0%}" for mm, p in zip(margins, probs, strict=True)),
              f"| mediana {np.median(ratios) - 1:+.1%} | p5 {np.percentile(ratios, 5) - 1:+.1%}"
              f" | estresse instantâneo -{stress:.1%}", flush=True)
