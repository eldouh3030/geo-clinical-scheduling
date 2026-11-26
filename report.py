"""
Tables and figures for the paper, from the results of run-experiment.py.

  python report.py main final-raw final-federated final-residency
  python report.py main --out privacy final-residency final-noid final-k10
      One experiment per execution mode (any number of experiments; each name becomes a
      column). Writes response-time and WAN-traffic figures and tables in the style of
      the paper. Use it as well for experiments with new privacy rules or security costs,
      e.g.  python report.py main final-federated final-residency final-trust

  python report.py sweep sensitivity-wan
      One experiment with modes and a parameter sweep: response time against the swept
      value, one panel per mode.

  python report.py regions region-check
      Per-region analysis from the unit files of an experiment run with save_queries:
      waiting and response time by region, and the share of work run outside its region.

Output goes to reports/<name>/ as PNG figures (300 dpi) and CSV tables
(reports/main/ for "main", or reports/<out>/ with --out <out>).
"""
import sys
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
from scipy import stats

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9})
POLICY_ORDER = ["hash", "fifo", "delay", "wanaware", "flow"]
POLICY_LABEL = {"hash": "Hash", "fifo": "FIFO", "delay": "Delay", "wanaware": "WAN-aware",
                "flow": "Min-cost flow"}
POLICY_STYLE = {"hash": ("#9e9e9e", "x"), "fifo": ("#b07aa1", "s"), "delay": ("#e15759", "^"),
                "wanaware": ("#4e79a7", "o"), "flow": ("#59a14f", "D")}
MODE_COLORS = ["#bab0ac", "#76b7b2", "#4e79a7", "#f28e2b", "#59a14f", "#edc948"]


def ci95(x):
    x = np.asarray(x, dtype=float)
    return 0.0 if len(x) < 2 else stats.t.ppf(0.975, len(x) - 1) * x.std(ddof=1) / np.sqrt(len(x))


def policies_in(df):
    known = [p for p in POLICY_ORDER if p in set(df["policy"])]
    return known + sorted(set(df["policy"]) - set(known))


def read(name):
    f = ROOT / "results" / name / "results.csv"
    if not f.exists():
        raise SystemExit(f"missing {f}")
    d = pd.read_csv(f)
    for c in ("mode", "sweep_value"):
        if c not in d.columns:
            d[c] = ""
    return d


# ------------------------------------------------------------------ main
def main_report(names, out_name="main"):
    out = ROOT / "reports" / out_name
    out.mkdir(parents=True, exist_ok=True)
    df = pd.concat([read(n).assign(experiment=n) for n in names], ignore_index=True)
    pols = policies_in(df)
    loads = sorted(df["load"].unique())

    # Figure: response time against load, one panel per experiment
    fig, axs = plt.subplots(1, len(names), figsize=(2.4 * len(names) + 0.2, 2.6),
                            sharey=True, squeeze=False)
    for ax, n in zip(axs[0], names):
        for p in pols:
            g = df[(df.experiment == n) & (df.policy == p)].groupby("load")["resp_mean_s"]
            if g.ngroups == 0:
                continue
            c, mk = POLICY_STYLE.get(p, ("#333333", "o"))
            ax.errorbar(g.mean().index, g.mean().values, yerr=g.apply(ci95).values,
                        label=POLICY_LABEL.get(p, p), color=c, marker=mk, ms=4, lw=1.2, capsize=2)
        ax.set_yscale("log")
        ax.set_title(n)
        ax.set_xlabel("Load (x reference saturation)")
        ax.set_xticks(loads)
        ax.grid(alpha=0.3, which="both", lw=0.4)
    axs[0][0].set_ylabel("Mean response time (s)")
    axs[0][-1].legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(out / "response.png", dpi=300)

    # Figure: WAN traffic at the load closest to 0.9
    l09 = min(loads, key=lambda x: abs(x - 0.9))
    fig, ax = plt.subplots(figsize=(4.8, 2.6))
    x = np.arange(len(pols))
    w = 0.8 / len(names)
    for i, n in enumerate(names):
        v = [df[(df.experiment == n) & (df.policy == p) & (df.load == l09)]["wan_mb"].mean()
             for p in pols]
        ax.bar(x + (i - (len(names) - 1) / 2) * w, v, w, label=n,
               color=MODE_COLORS[i % len(MODE_COLORS)])
    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABEL.get(p, p) for p in pols], fontsize=8)
    ax.set_ylabel(f"Inter-site WAN traffic (MB), load {l09}")
    ax.legend(fontsize=7)
    ax.grid(axis="y", alpha=0.3, lw=0.4)
    fig.tight_layout()
    fig.savefig(out / "wan.png", dpi=300)

    # Figure: the two network-aware policies across experiments
    best = [p for p in ("wanaware", "flow") if p in pols]
    if best:
        fig, axs = plt.subplots(1, len(best), figsize=(3.2 * len(best), 2.5), sharey=True,
                                squeeze=False)
        for ax, p in zip(axs[0], best):
            for i, n in enumerate(names):
                g = df[(df.experiment == n) & (df.policy == p)].groupby("load")["resp_mean_s"]
                ax.errorbar(g.mean().index, g.mean().values, yerr=g.apply(ci95).values,
                            label=n, color=MODE_COLORS[i % len(MODE_COLORS)], marker="o",
                            ms=4, lw=1.3, capsize=2)
            ax.set_title(POLICY_LABEL[p])
            ax.set_yscale("log")
            ax.set_xticks(loads)
            ax.set_xlabel("Load (x reference saturation)")
            ax.grid(alpha=0.3, which="both", lw=0.4)
        axs[0][0].set_ylabel("Mean response time (s)")
        axs[0][-1].legend(fontsize=7)
        fig.tight_layout()
        fig.savefig(out / "best-policies.png", dpi=300)

    # Tables: mean ± 95% CI, and paired tests between consecutive experiments
    rows = []
    for load in loads:
        for p in pols:
            r = {"load": load, "policy": POLICY_LABEL.get(p, p)}
            for n in names:
                s = df[(df.experiment == n) & (df.policy == p) & (df.load == load)]
                r[f"{n} resp_mean_s"] = f"{s.resp_mean_s.mean():.3f} ± {ci95(s.resp_mean_s):.3f}"
                r[f"{n} resp_p99_s"] = round(s.resp_p99_s.mean(), 3)
                r[f"{n} wan_mb"] = round(s.wan_mb.mean(), 1)
                r[f"{n} throughput"] = round(s.throughput.mean(), 1)
                r[f"{n} unfinished"] = round(s.unfinished.mean(), 1)
                if "blocked_frac" in s:      # privacy rules: share of queries no node may run
                    r[f"{n} blocked %"] = round(100 * s.blocked_frac.mean(), 1)
                for sc in ("patient", "region", "all"):
                    c = f"resp_mean_{sc}_s"
                    if c in s:               # response time per scope, comparable across rules
                        r[f"{n} {c}"] = round(s[c].mean(), 4)
            rows.append(r)
    pd.DataFrame(rows).to_csv(out / "table.csv", index=False)

    tests = []
    for a, b in zip(names, names[1:]):
        for load in loads:
            for p in pols:
                x = df[(df.experiment == a) & (df.policy == p) & (df.load == load)].set_index("workload").resp_mean_s
                y = df[(df.experiment == b) & (df.policy == p) & (df.load == load)].set_index("workload").resp_mean_s
                common = x.index.intersection(y.index)
                pv = np.nan
                if len(common) >= 5 and (x[common] != y[common]).any():
                    pv = stats.wilcoxon(x[common], y[common]).pvalue
                tests.append(dict(compare=f"{a} vs {b}", load=load, policy=p,
                                  mean_a=x[common].mean(), mean_b=y[common].mean(), p_value=pv))
    pd.DataFrame(tests).to_csv(out / "tests.csv", index=False)
    print(f"figures and tables written to {out}")


# ------------------------------------------------------------------ sweep
def sweep_report(name):
    out = ROOT / "reports" / name
    out.mkdir(parents=True, exist_ok=True)
    df = read(name)
    sv = df["sweep_value"].fillna("").astype(str).replace("nan", "")
    if (sv == "").all():          # no sweep: compare workload families, e.g. mix10 / base / mix50
        df["sweep_value"] = df["workload"].str.replace(r"-s\d+$", "", regex=True)
    modes = [m for m in dict.fromkeys(df["mode"].astype(str))]
    pols = policies_in(df)
    loads = sorted(df["load"].unique())
    for load in loads:
        fig, axs = plt.subplots(1, len(modes), figsize=(2.6 * len(modes) + 0.2, 2.6),
                                sharey=True, squeeze=False)
        for ax, m in zip(axs[0], modes):
            for p in pols:
                g = df[(df["mode"].astype(str) == m) & (df.policy == p) & (df.load == load)] \
                    .groupby("sweep_value")["resp_mean_s"]
                c, mk = POLICY_STYLE.get(p, ("#333333", "o"))
                ax.errorbar(g.mean().index, g.mean().values, yerr=g.apply(ci95).values,
                            label=POLICY_LABEL.get(p, p), color=c, marker=mk, ms=4, lw=1.2,
                            capsize=2)
            ax.set_title(m)
            ax.set_yscale("log")
            ax.set_xlabel("value")
            ax.grid(alpha=0.3, which="both", lw=0.4)
        axs[0][0].set_ylabel(f"Mean response time (s), load {load}")
        axs[0][-1].legend(fontsize=7)
        fig.tight_layout()
        fig.savefig(out / f"sweep load {load}.png", dpi=300)
    t = (df.groupby(["mode", "sweep_value", "load", "policy"])
           .agg(resp_mean_s=("resp_mean_s", "mean"), ci=("resp_mean_s", ci95),
                resp_p99_s=("resp_p99_s", "mean"), wan_mb=("wan_mb", "mean"),
                throughput=("throughput", "mean"), workloads=("workload", "nunique"))
           .round(4).reset_index())
    t.to_csv(out / "table.csv", index=False)
    pd.set_option("display.width", 200)
    print(t.to_string(index=False))
    print(f"\nfigures and table written to {out}")


# ------------------------------------------------------------------ regions
def regions_report(name):
    rdir = ROOT / "results" / name
    out = ROOT / "reports" / name
    out.mkdir(parents=True, exist_ok=True)
    frames = []
    for f in sorted(rdir.glob("units *.csv")):
        parts = f.stem.split(" ")[1:]
        d = pd.read_csv(f)
        d["load"] = float(parts[-1])
        d["policy"] = parts[-2]
        d["mode"] = parts[1] if len(parts) >= 4 else ""
        d["workload"] = parts[0]
        frames.append(d)
    if not frames:
        raise SystemExit(f"no unit files in {rdir}; run the experiment with save_queries: true")
    df = pd.concat(frames, ignore_index=True)
    df = df[df["site"].astype(str) != "all"]      # undecomposed all-region queries have no home
    df["outside"] = (df["node_site"].astype(str) != df["site"].astype(str))
    t = (df.groupby(["mode", "policy", "load", "site"])
           .agg(units=("id", "size"), wait_mean_s=("wait", "mean"),
                response_mean_s=("response", "mean"),
                ran_outside_region=("outside", "mean"))
           .round(4).reset_index())
    t.to_csv(out / "regions.csv", index=False)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 500)
    print(t.to_string(index=False))
    print(f"\ntable written to {out / 'regions.csv'}")


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("main", "sweep", "regions"):
        raise SystemExit(__doc__)
    if sys.argv[1] == "main":
        args = sys.argv[2:]
        out_name = "main"
        if "--out" in args:
            i = args.index("--out")
            out_name = args[i + 1]
            args = args[:i] + args[i + 2:]
        main_report(args, out_name)
    elif sys.argv[1] == "sweep":
        sweep_report(sys.argv[2])
    else:
        regions_report(sys.argv[2])
