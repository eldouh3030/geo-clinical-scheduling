"""
Build a distributed setting and a query workload from the real eICU demo data
and the real queries of the eICU code repository.

Steps
  1. Topology   hospitals -> nodes. A site is a US region of the hospital table;
                inside a site, hospitals are spread over `nodes_per_site` nodes
                (largest hospital first, to the least-filled node). A cloud node
                holds no patient data.
  2. Partitions every patient-linked table is split by node and written as one
                Parquet file per node; the file size is the transfer size.
                Tables without patient rows (the hospital table) are small
                reference tables replicated on every node.
  3. Workload   query instances drawn from the working queries of the catalog,
                each with a scope (patient, region or all regions), real
                parameter values, the list of partitions it reads, its arrival
                time, and its processing time measured on DuckDB.

Run from the project folder:
    python make-workload.py configs\\base-workload.yaml
    python make-workload.py configs\\base-workload.yaml 3     (seed 3, folder base-s3)
"""
import csv
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import sqlglot
import yaml
from sqlglot import exp

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "eicu.duckdb"
CSV_DIR = ROOT / "data" / "eicu"
QUERY_DIR = ROOT / "queries" / "eicu"

PATIENT_PARAMS = {"patientunitstayid", "uniquepid"}


# ------------------------------------------------------------------ helpers
def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def base_tables() -> list[str]:
    """The 31 original eICU tables (not the derived concept tables)."""
    return sorted(f.name[: -len(".csv.gz")].lower() for f in CSV_DIR.rglob("*.csv.gz"))


def sql_quote(v) -> str:
    return str(v).replace("'", "''")


# ------------------------------------------------------------------ 1. topology
def build_topology(con, cfg) -> tuple[dict, pd.DataFrame]:
    k = int(cfg["topology"]["nodes_per_site"])
    hosp = con.execute("""
        SELECT p.hospitalid,
               COALESCE(h.region, 'unknown') AS site,
               COUNT(*) AS stays
        FROM patient p LEFT JOIN hospital h USING (hospitalid)
        GROUP BY 1, 2
    """).df()

    # data volume per hospital (rows of all patient-linked tables) for balancing
    rows = con.execute("""
        SELECT p.hospitalid, COUNT(*) AS n
        FROM nursecharting x JOIN patient p USING (patientunitstayid)
        GROUP BY 1
    """).df()
    hosp = hosp.merge(rows, on="hospitalid", how="left").fillna({"n": 0})

    assign = {}
    nodes = []
    for site, grp in hosp.groupby("site"):
        names = [f"{site.lower()}-{i + 1}" for i in range(k)]
        load = dict.fromkeys(names, 0.0)
        for _, r in grp.sort_values("n", ascending=False).iterrows():
            target = min(load, key=load.get)
            assign[int(r.hospitalid)] = target
            load[target] += r.n + 1
        for nm in names:
            nodes.append(dict(node=nm, site=site,
                              slots=int(cfg["topology"]["site_slots"]),
                              hospitals=sorted(h for h, n in assign.items() if n == nm)))
    nodes.append(dict(node="cloud", site="cloud",
                      slots=int(cfg["topology"]["cloud_slots"]), hospitals=[]))

    stay_node = con.execute("SELECT patientunitstayid, hospitalid FROM patient").df()
    stay_node["node"] = stay_node["hospitalid"].map(assign)
    topo = dict(nodes=nodes, network=cfg["network"])
    return topo, stay_node


# ------------------------------------------------------------------ 2. partitions
def build_partitions(con, tables, stay_node, out_dir: Path, buckets: int) -> pd.DataFrame:
    """One Parquet file per (table, node, bucket); bucket = patientunitstayid mod buckets."""
    con.register("stay_node", stay_node[["patientunitstayid", "node"]])
    linked = {r[0] for r in con.execute("""
        SELECT table_name FROM information_schema.columns
        WHERE lower(column_name) = 'patientunitstayid'""").fetchall()}
    rows = []
    part_dir = out_dir / "partitions"
    for t in tables:
        if t not in linked:
            rows.append(dict(partition=f"{t}@*", table=t, node="*", bucket=-1, rows=
                             con.execute(f"SELECT COUNT(*) FROM {t}").fetchall()[0][0],
                             bytes=0, replicated=True))
            continue
        for node in sorted(stay_node["node"].unique()):
            d = part_dir / node / t
            d.mkdir(parents=True, exist_ok=True)
            for b in range(buckets):
                f = d / f"bucket{b}.parquet"
                con.execute(f"""
                    COPY (SELECT x.* FROM {t} x JOIN stay_node s USING (patientunitstayid)
                          WHERE s.node = '{node}' AND x.patientunitstayid % {buckets} = {b})
                    TO '{f.as_posix()}' (FORMAT parquet)""").fetchall()
                n = con.execute(
                    f"SELECT COUNT(*) FROM read_parquet('{f.as_posix()}')").fetchall()[0][0]
                rows.append(dict(partition=f"{t}@{node}#{b}", table=t, node=node, bucket=b,
                                 rows=n, bytes=f.stat().st_size if n else 0,
                                 replicated=False))
    con.unregister("stay_node")
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ 3. templates
def load_templates(cfg, tables) -> list[dict]:
    excluded = set(cfg["workload"].get("exclude", []))
    out = []
    with open(QUERY_DIR / "catalog.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["status"] != "ok" or r["id"] in excluded:
                continue
            reads = r["tables"].split()
            if not reads or any(t not in tables for t in reads):
                continue  # e.g. queries on sqlite_master
            sql = (QUERY_DIR / f"{r['id']}.sql").read_text(encoding="utf-8").strip()
            sql = re.sub(r"(?is)^CREATE OR REPLACE TABLE \w+ AS\s*", "", sql).rstrip(";")
            params = r["params"].split()
            out.append(dict(id=r["id"], kind=r["kind"], sql=sql, tables=reads,
                            params=params,
                            patient=bool(PATIENT_PARAMS & set(params))))
    return out


def value_pools(con) -> dict:
    q = lambda s: [r[0] for r in con.execute(s).fetchall() if r[0] not in (None, "")]
    drug = q("""
        SELECT w FROM (
          SELECT lower(split_part(trim(drugname), ' ', 1)) AS w FROM medication
          UNION ALL SELECT lower(split_part(trim(drugname), ' ', 1)) FROM admissiondrug
          UNION ALL SELECT lower(split_part(trim(allergyname), ' ', 1)) FROM allergy)
        WHERE length(w) >= 4 AND regexp_matches(w, '^[a-z]+$')
        GROUP BY w ORDER BY COUNT(*) DESC LIMIT 150""")
    hicl = q("""
        SELECT drughiclseqno FROM medication WHERE drughiclseqno IS NOT NULL
        GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 150""")
    dx = q("""
        SELECT w FROM (
          SELECT lower(trim(string_split(diagnosisstring, '|')[-1])) AS w FROM diagnosis)
        WHERE length(w) >= 4
        GROUP BY w ORDER BY COUNT(*) DESC LIMIT 150""")
    icd9 = q("""
        SELECT trim(split_part(icd9code, ',', 1)) FROM diagnosis
        WHERE icd9code IS NOT NULL GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 150""")
    return dict(drug=drug, drugname=drug, allergyname=drug, hicl=hicl, dx=dx, icd9=icd9)


# ------------------------------------------------------------------ scope rewrite
def region_filter(table: str, site: str) -> str:
    site_cond = f"COALESCE(region, 'unknown') = '{sql_quote(site)}'"
    hosp = f"SELECT hospitalid FROM hospital WHERE {site_cond}"
    if table == "patient":
        return f"SELECT * FROM patient WHERE hospitalid IN ({hosp})"
    return (f"SELECT * FROM {table} WHERE patientunitstayid IN "
            f"(SELECT patientunitstayid FROM patient WHERE hospitalid IN ({hosp}))")


def scope_to_region(sql: str, site: str, linked: set) -> str:
    """Restrict every patient-linked table of the query to the hospitals of one region."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    targets = [t for t in tree.find_all(exp.Table)
               if t.name.lower() in linked and t.name.lower() not in ctes]
    for t in targets:
        alias = t.alias or t.name
        sub = sqlglot.parse_one(region_filter(t.name.lower(), site), read="duckdb")
        t.replace(exp.Subquery(this=sub, alias=exp.TableAlias(this=exp.to_identifier(alias))))
    return tree.sql(dialect="duckdb")


# ------------------------------------------------------------------ arrivals
def arrivals(n: int, cfg: dict, rng) -> np.ndarray:
    """Two-state Markov-modulated Poisson process: normal periods and bursts.

    Times are in units of the mean inter-arrival time (long-run rate = 1), so the
    simulator can set the load by dividing them by the arrival rate it wants.
    Period lengths are given in the same units (about that many arrivals).
    """
    a = cfg["arrivals"]
    factor = float(a["burst_factor"])
    mean_normal, mean_burst = float(a["normal_len"]), float(a["burst_len"])
    rate = (mean_normal + mean_burst) / (mean_normal + factor * mean_burst)
    t, out, burst = 0.0, [], False
    end = rng.exponential(mean_normal)
    while len(out) < n:
        lam = rate * (factor if burst else 1.0)
        gap = rng.exponential(1.0 / lam)
        if t + gap > end:
            t = end
            burst = not burst
            end = t + rng.exponential(mean_burst if burst else mean_normal)
            continue
        t += gap
        out.append(t)
    return np.array(out)


# ------------------------------------------------------------------ main
def main() -> None:
    if len(sys.argv) not in (2, 3):
        raise SystemExit("usage: python make-workload.py <config.yaml> [seed]")
    cfg_path = Path(sys.argv[1])
    cfg = load_config(cfg_path)
    name = cfg.get("name", cfg_path.stem)
    if len(sys.argv) == 3:              # repetition: same settings, another seed
        cfg["seed"] = int(sys.argv[2])
        name = f"{name}-s{cfg['seed']}"
    out_dir = ROOT / "workloads" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(int(cfg["seed"]))

    con = duckdb.connect(str(DB))
    con.execute(f"SET threads = {int(cfg['measure'].get('threads', 1))}").fetchall()
    tables = base_tables()
    linked = {r[0] for r in con.execute("""
        SELECT table_name FROM information_schema.columns
        WHERE lower(column_name) = 'patientunitstayid'""").fetchall()} & set(tables)

    print("1/3 topology and partitions")
    topo, stay_node = build_topology(con, cfg)
    buckets = int(cfg["topology"]["buckets"])
    parts = build_partitions(con, tables, stay_node, out_dir, buckets)
    parts.to_csv(out_dir / "partitions.csv", index=False)
    (out_dir / "topology.json").write_text(json.dumps(topo, indent=2), encoding="utf-8")
    sites = sorted({n["site"] for n in topo["nodes"] if n["site"] != "cloud"})
    site_nodes = {s: [n["node"] for n in topo["nodes"] if n["site"] == s] for s in sites}
    all_nodes = [n["node"] for n in topo["nodes"] if n["site"] != "cloud"]
    node_site = {n["node"]: n["site"] for n in topo["nodes"]}
    part_bytes = dict(zip(parts["partition"], parts["bytes"]))
    print(f"    {len(all_nodes)} data nodes in {len(sites)} sites + cloud, "
          f"{int(parts['bytes'].sum()) / 1e6:.1f} MB in {int((~parts['replicated']).sum())} partitions")

    print("2/3 templates and parameter values")
    templates = load_templates(cfg, set(tables))
    pools = value_pools(con)
    patients = con.execute(
        "SELECT patientunitstayid, uniquepid FROM patient").fetchall()
    stay_to_node = dict(zip(stay_node["patientunitstayid"], stay_node["node"]))
    # Optional: some patients are asked about much more often than others (patients
    # currently in the ICU). Zipf weights over a random order of the patients.
    zipf = float(cfg["workload"].get("patient_zipf", 0.0))
    if zipf > 0:
        prng = np.random.default_rng(int(cfg["seed"]) + 7)
        order = prng.permutation(len(patients))
        pw = np.empty(len(patients))
        pw[order] = 1.0 / np.arange(1, len(patients) + 1) ** zipf
        pw /= pw.sum()
    w = cfg["workload"]
    kind_w = w["kind_weights"]
    tw = np.array([kind_w[t["kind"]] for t in templates], dtype=float)
    tw /= tw.sum()
    scope_w = w["scope_weights"]  # for templates without a patient parameter
    site_w = np.array([len(site_nodes[s]) for s in sites], dtype=float)
    if w.get("site_choice", "data") == "data":
        site_w = np.array([parts.loc[parts["node"].isin(site_nodes[s]), "bytes"].sum()
                           for s in sites], dtype=float)
    site_w /= site_w.sum()
    print(f"    {len(templates)} templates "
          f"({sum(t['kind'] == 'concept' for t in templates)} concept, "
          f"{sum(t['kind'] == 'notebook' for t in templates)} notebook), "
          f"{sum(t['patient'] for t in templates)} patient-level")

    print("3/3 query instances")
    n = int(w["queries"])
    times = arrivals(n, cfg, rng)
    cache = {}
    out_rows = []
    t_start = time.perf_counter()
    tmp = out_dir / "result.tmp.parquet"

    def measure(q_sql):
        """Processing time on DuckDB and size of the result as a Parquet file."""
        key = hashlib.sha1(q_sql.encode()).hexdigest()
        if key not in cache:
            t0 = time.perf_counter()
            con.execute(q_sql).fetchall()
            elapsed = time.perf_counter() - t0
            con.execute(f"COPY ({q_sql}) TO '{tmp.as_posix()}' (FORMAT parquet)").fetchall()
            cache[key] = (elapsed, tmp.stat().st_size)
        return cache[key]
    for i in range(n):
        tpl = templates[rng.choice(len(templates), p=tw)]
        sql = tpl["sql"]
        values = {}
        if tpl["patient"]:
            k = rng.integers(len(patients))   # always drawn, so both workloads share the rest
            if zipf > 0:
                k = prng.choice(len(patients), p=pw)
            pid, upid = patients[k]
            scope, site, nodes = "patient", node_site[stay_to_node[pid]], [stay_to_node[pid]]
        else:
            scope = rng.choice(["region", "all"],
                               p=np.array([scope_w["region"], scope_w["all"]]) /
                               (scope_w["region"] + scope_w["all"]))
            site = sites[rng.choice(len(sites), p=site_w)] if scope == "region" else "all"
            nodes = site_nodes[site] if scope == "region" else all_nodes
        for k, p in enumerate(tpl["params"], start=1):
            if p == "patientunitstayid":
                v = pid
            elif p == "uniquepid":
                v = upid
            else:
                pool = pools.get(p) or pools["drug"]
                v = pool[rng.integers(len(pool))]
            values[p] = v
            sql = sql.replace("{p%d}" % k, sql_quote(v))
        if scope == "region":
            sql = scope_to_region(sql, site, linked)

        bks = [pid % buckets] if scope == "patient" else range(buckets)
        reads = [f"{t}@{nd}#{b}" for t in tpl["tables"] if t in linked
                 for nd in nodes for b in bks
                 if part_bytes.get(f"{t}@{nd}#{b}", 0) > 0]
        exec_s, result_bytes = measure(sql)
        row = dict(
            id=i, arrival_unit=round(float(times[i]), 6), template=tpl["id"], kind=tpl["kind"],
            scope=str(scope), site=site, params=values, sql=sql,
            partitions=reads, input_bytes=int(sum(part_bytes[r] for r in reads)),
            exec_s=round(exec_s, 6), result_bytes=result_bytes)
        if scope == "all":
            # Decomposed form: one fragment per region, run on that region's data only;
            # only its result leaves the region.
            frags = []
            for s_ in sites:
                fsql = scope_to_region(sql, s_, linked)
                freads = [f"{t}@{nd}#{b}" for t in tpl["tables"] if t in linked
                          for nd in site_nodes[s_] for b in range(buckets)
                          if part_bytes.get(f"{t}@{nd}#{b}", 0) > 0]
                if not freads:
                    continue
                f_exec, f_bytes = measure(fsql)
                frags.append(dict(site=s_, partitions=freads,
                                  input_bytes=int(sum(part_bytes[r] for r in freads)),
                                  exec_s=round(f_exec, 6), result_bytes=f_bytes))
            row["fragments"] = frags
        out_rows.append(row)
        if (i + 1) % 200 == 0 or i + 1 == n:
            print(f"    {i + 1}/{n} instances, {len(cache)} distinct queries measured, "
                  f"{time.perf_counter() - t_start:.0f} s")
    con.close()
    if tmp.exists():
        tmp.unlink()

    with open(out_dir / "workload.jsonl", "w", encoding="utf-8") as fh:
        for r in out_rows:
            fh.write(json.dumps(r, default=str) + "\n")

    df = pd.DataFrame(out_rows)
    print("\nSummary")
    print(df.groupby("scope").agg(queries=("id", "size"),
                                  mean_input_mb=("input_bytes", lambda s: s.mean() / 1e6),
                                  mean_result_kb=("result_bytes", lambda s: s.mean() / 1e3),
                                  mean_exec_ms=("exec_s", lambda s: 1000 * s.mean()),
                                  max_exec_ms=("exec_s", lambda s: 1000 * s.max())).round(2))
    alls = [r for r in out_rows if r.get("fragments")]
    if alls:
        moved_raw = np.mean([r["input_bytes"] for r in alls]) / 1e6
        moved_frag = np.mean([sum(f["result_bytes"] for f in r["fragments"]) for r in alls]) / 1e6
        print(f"\nall-regions queries: {moved_raw:.2f} MB of raw input on average, "
              f"{moved_frag:.3f} MB of fragment results if decomposed")
    slots = sum(nd["slots"] for nd in topo["nodes"])
    mean_exec = df["exec_s"].mean()
    print(f"\nmean processing time {1000 * mean_exec:.2f} ms, {slots} slots in total")
    print(f"processing alone saturates all slots at {slots / mean_exec:.0f} queries/s "
          f"(the simulator sets the arrival rate as a fraction of this)")
    print(f"written to {out_dir}")


if __name__ == "__main__":
    main()
