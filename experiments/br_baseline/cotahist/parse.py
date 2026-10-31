"""Parse COTAHIST annual files into one compact table of spot stock trades (lote padrão, mercado à vista)."""
import sys
import zipfile
from pathlib import Path

import pandas as pd

src = Path(sys.argv[1])
rows = []
for path in sorted(src.glob("COTAHIST_A*.ZIP")):
    with zipfile.ZipFile(path) as z:
        name = z.namelist()[0]
        with z.open(name) as fh:
            for raw in fh:
                line = raw.decode("latin-1")
                if not line.startswith("01"):
                    continue
                codbdi, tpmerc = line[10:12], line[24:27]
                if tpmerc != "010" or codbdi not in ("02", "12", "14"):
                    continue
                rows.append((line[2:10], codbdi, line[12:24].strip(), line[39:49].strip(), line[27:39].strip(),
                             int(line[108:121]) / 100, int(line[170:188]) / 100, int(line[152:170]),
                             int(line[210:217]), line[230:242], line[242:245]))
    print(path.name, len(rows), flush=True)
cols = ["date", "codbdi", "code", "especi", "name", "close", "volume", "quantity", "fatcot", "isin", "dismes"]
frame = pd.DataFrame(rows, columns=cols)
frame["date"] = pd.to_datetime(frame["date"])
frame["close"] = frame["close"] / frame["fatcot"]
frame.to_csv(src / "spot.csv.gz", index=False)
print(frame.groupby("codbdi").size(), frame["date"].min(), frame["date"].max())
