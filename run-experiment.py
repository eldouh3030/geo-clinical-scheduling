"""
Run scheduling policies on one or more workloads at several load levels.

Load. 100% load is the saturation throughput of a reference policy (delay scheduling
by default) under the reference simulator settings: the whole workload is sent at once
and the completed queries per second are measured. All runs of a workload then use the
same fractions of that rate, so every policy, mode and parameter value sees exactly the
same arrivals.

Modes and sweeps (optional).
  modes:  named simulator settings run side by side, e.g. raw / federated / residency
  sweep:  one simulator setting varied over a list of values, e.g.
            sweep: {key: network.inter_site_mbps, values: [50, 100, 200, 500]}
Each row of results.csv records its workload, mode, sweep value, policy and load.

Runs are independent, so they can use several processor cores (setting "parallel").

Run from the project folder:
    python run-experiment.py configs\\final-federated.yaml
"""
import copy
import csv
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import yaml

from policies import make_policy
from simulator import Simulator, load_workload

ROOT = Path(__file__).resolve().parent

SHOW = [("workload", "{:<12}"), ("mode", "{:<10}"), ("sweep_value", "{:>7}"),
        ("policy", "{:<9}"), ("load", "{:>5.2f}"),
        ("resp_mean_s", "{:>9.3f}"), ("resp_p95_s", "{:>9.3f}"), ("resp_p99_s", "{:>9.3f}"),
        ("wan_mb", "{:>9.1f}"), ("moved_mb", "{:>9.1f}"), ("result_mb", "{:>8.2f}"),
        ("locality", "{:>8.3f}"), ("unfinished", "{:>6d}"), ("decision_us", "{:>9.1f}")]


def set_key(cfg: dict, dotted: str, value) -> dict:
    """Copy of cfg with cfg[a][b]... = value for a key written as 'a.b'."""
    out = copy.deepcopy(cfg)
    d = out
    parts = dotted.split(".")
    for k in parts[:-1]:
        d = d.setdefault(k, {})
    d[parts[-1]] = value
    return out


def save_units(sim, path: Path) -> None:
    """One row per scheduled unit (a query, or one regional fragment of a decomposed query)."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "parent", "template", "kind", "scope", "site", "node", "node_site",
                    "arrival", "dispatch", "finish", "response", "wait", "query_response",
                    "input_bytes", "local_bytes", "fetched_bytes", "exec_s", "result_bytes"])
        for u in sim.units:
            p = u.parent
            done = u.finish >= 0
            top = p if p is not None else u
            w.writerow([u.id, p.id if p is not None else "", u.template, u.kind, u.scope,
                        u.site, u.node, sim.site.get(u.node, ""),
                        round(u.arrival, 6), round(u.dispatch, 6), round(u.finish, 6),
                        round(u.finish - u.arrival, 6) if done else "",
                        round(u.dispatch - u.arrival, 6) if u.dispatch >= 0 else "",
                        round(top.finish - top.arrival, 6) if top.finish >= 0 else "",
                        u.extra["total"], u.local_bytes, u.fetched_bytes, u.exec_s,
                        u.result_bytes])


def calibrate(job):
    wname, sim_cfg, ref_spec, seed = job
    topo, parts, queries = load_workload(ROOT / "workloads" / wname)
    sim = Simulator(topo, parts, queries, sim_cfg, make_policy(ref_spec), seed=seed)
    return wname, sim.run(rate=1e9)["throughput"]


def run_one(job):
    wname, mode, sweep_value, sim_cfg, spec, seed, load, sat, out, keep = job
    topo, parts, queries = load_workload(ROOT / "workloads" / wname)
    pol = make_policy(spec)
    sim = Simulator(topo, parts, queries, sim_cfg, pol, seed=seed)
    t0 = time.perf_counter()
    m = sim.run(float(load) * sat)
    m.update(workload=wname, mode=mode, sweep_value=sweep_value, load=float(load),
             saturation_rate=sat, wall_s=time.perf_counter() - t0)
    if keep:
        tag = " ".join(str(x) for x in (wname, mode, sweep_value, pol.name, f"{float(load):.2f}")
                       if x != "")
        save_units(sim, Path(out) / f"units {tag}.csv")
    return m


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python run-experiment.py <config.yaml>")
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
    name = cfg.get("name", Path(sys.argv[1]).stem)
    out = ROOT / "results" / name
    out.mkdir(parents=True, exist_ok=True)
    workloads = cfg.get("workloads") or [cfg["workload"]]
    seed = int(cfg.get("seed", 1))
    workers = int(cfg.get("parallel", 1))
    keep = bool(cfg.get("save_queries", len(workloads) == 1))

    base_sim = cfg.get("simulator", {})
    modes = cfg.get("modes") or {"": base_sim}
    sweep = cfg.get("sweep")
    variants = []                         # (mode name, sweep value, simulator settings)
    for mname, msim in modes.items():
        msim = {**base_sim, **(msim or {})}
        if sweep:
            for v in sweep["values"]:
                variants.append((mname, v, set_key(msim, sweep["key"], v)))
        else:
            variants.append((mname, "", msim))

    pool = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
    mapper = pool.map if pool else map

    ref_cfg = cfg.get("reference_simulator", base_sim)
    sats = dict(mapper(calibrate, [(w, ref_cfg, cfg["reference"], seed) for w in workloads]))
    for w in workloads:
        print(f"{w}: saturation throughput of the reference = {sats[w]:.1f} queries/s")
    print()

    jobs = [(w, mname, v, scfg, spec, seed, load, sats[w], str(out), keep)
            for w in workloads for (mname, v, scfg) in variants
            for load in cfg["loads"] for spec in cfg["policies"]]
    print(" ".join(f"{k[:9]:<9}" for k, _ in SHOW))
    rows, last = [], None
    for m in mapper(run_one, jobs):
        key = (m["workload"], m["mode"], m["sweep_value"], m["load"])
        if last is not None and key != last:
            print()
        last = key
        rows.append(m)
        print(" ".join(fmt.format(m[k]) for k, fmt in SHOW), flush=True)
    if pool:
        pool.shutdown()

    with open(out / "results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (out / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"\nresults written to {out / 'results.csv'}")


if __name__ == "__main__":
    main()
