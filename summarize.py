"""
Statistics over repeated workloads (different seeds) of one experiment.

For every load level and policy: mean of each metric with a 95% confidence interval
(Student t over the workloads), and a paired Wilcoxon signed-rank test against the
reference policy on the same workloads.

Run from the project folder after run-experiment.py:
    python summarize.py final-federated
    python summarize.py final-federated delay        (choose the reference policy)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent
METRICS = ["resp_mean_s", "resp_p95_s", "resp_p99_s", "wan_mb", "moved_mb", "locality",
           "unfinished"]
EXTRA = ["blocked_frac", "resp_mean_patient_s", "resp_mean_region_s", "resp_mean_all_s"]


def ci95(x):
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return 0.0
    return stats.t.ppf(0.975, len(x) - 1) * x.std(ddof=1) / np.sqrt(len(x))


def main() -> None:
    if len(sys.argv) not in (2, 3):
        raise SystemExit("usage: python summarize.py <experiment name> [reference policy]")
    rdir = ROOT / "results" / sys.argv[1]
    ref = sys.argv[2] if len(sys.argv) == 3 else "delay"
    df = pd.read_csv(rdir / "results.csv")
    for c in ("mode", "sweep_value"):
        if c not in df.columns:
            df[c] = ""
    df[["mode", "sweep_value"]] = df[["mode", "sweep_value"]].fillna("").astype(str)
    df["group"] = (df["mode"] + " " + df["sweep_value"]).str.strip()
    metrics = METRICS + [m for m in EXTRA if m in df.columns]   # columns of newer runs
    n_w = df["workload"].nunique()
    print(f"{sys.argv[1]}: {n_w} workloads, reference for the tests = {ref}\n")

    out_rows = []
    for (grp, load), part in df.groupby(["group", "load"]):
        print(f"===== {grp + ', ' if grp else ''}load {load:.2f} =====")
        head = f"{'policy':<10}" + "".join(f"{m[:11]:>22}" for m in METRICS[:4]) + "   p(resp_mean)  p(wan_mb)"
        print(head)
        base = part[part["policy"] == ref].set_index("workload")
        for pol, g in part.groupby("policy", sort=False):
            g = g.set_index("workload")
            row = dict(group=grp, load=load, policy=pol, workloads=len(g))
            cells = []
            for m in metrics:
                mean, h = g[m].mean(), ci95(g[m])
                row[m] = mean
                row[m + "_ci95"] = h
                if m in METRICS[:4]:
                    cells.append(f"{mean:>12.4f} ± {h:<7.4f}")
            pvals = []
            for m in ("resp_mean_s", "wan_mb"):
                p = np.nan
                if pol != ref and not base.empty:
                    common = g.index.intersection(base.index)
                    diff = g.loc[common, m] - base.loc[common, m]
                    if len(common) >= 5 and (diff != 0).any():
                        p = stats.wilcoxon(g.loc[common, m], base.loc[common, m]).pvalue
                row["p_" + m] = p
                pvals.append("      -     " if np.isnan(p) else f"{p:>12.4g}")
            out_rows.append(row)
            print(f"{pol:<10}" + "".join(cells) + "".join(pvals))
        print()
    pd.DataFrame(out_rows).to_csv(rdir / "summary.csv", index=False)
    print(f"written to {rdir / 'summary.csv'}")


if __name__ == "__main__":
    main()
