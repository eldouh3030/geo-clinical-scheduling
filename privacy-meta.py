"""
Privacy metadata of every query result, for the result-release rules of extensions.py.

For every query instance and every regional fragment of an all-region query, the SQL is
run on DuckDB (the fragment SQL is rebuilt exactly as make-workload.py builds it) and its
result is classified:

  identifiable  the result has a patient identifier column
                (patientunitstayid, uniquepid, patienthealthsystemstayid)
  result_class  identifiable  identifier column present                     min_group = 1
                row-level     no identifier, no aggregation: one row per
                              record                                        min_group = 1
                patients      aggregate; the number of distinct patients
                              of every group is computed by rewriting the
                              aggregating SELECT to
                              COUNT(DISTINCT patientunitstayid)             min_group = smallest
                                                                            group (patients),
                                                                            or the smallest
                                                                            non-zero count cell
                                                                            if that is smaller
                counted       aggregate where that rewrite is not possible
                              but the result has a count column             min_group = smallest
                                                                            non-zero count (rows,
                                                                            an upper bound on
                                                                            patients)
                uncounted     aggregate with neither                        min_group unknown
                zero          every group is empty                          nothing released
                empty         no rows                                       nothing released
  group_basis   "patients", "rows" or null: what min_group counts

A count column is an output column computed by COUNT(...) in the aggregating SELECT
(followed through CTEs and subqueries), or an integer column named like a count
(n, n_*, num*, count*, *_count, *_cnt). The classification of every template is written
to a CSV so that it can be checked by hand.

workload.jsonl is not changed: the metadata go to workloads/<name>/privacy.jsonl, which
the simulator reads if present.

Run from the project folder after make-workload.py:
    python privacy-meta.py base-s1 base-s2 ... base-s10
Output: workloads/<name>/privacy.jsonl, results/privacy-meta/templates.csv and summary.csv
"""
import hashlib
import importlib.util
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("make_workload", ROOT / "make-workload.py")
mw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mw)

ID_COLS = {"patientunitstayid", "uniquepid", "patienthealthsystemstayid"}
COUNT_NAME = re.compile(r"^(n|num.*|count.*|cnt.*|n_.*|number.*)$|.*(_count|_cnt|_n)$")
KS = (5, 10, 20)
# argument names differ between sqlglot versions ("from" / "from_", "with" / "with_")
FROM = "from_" if "from_" in exp.Select.arg_types else "from"
WITH = "with_" if "with_" in exp.Select.arg_types else "with"


# ------------------------------------------------------------------ SQL analysis
def _strip(e):
    while isinstance(e, (exp.Alias, exp.Cast, exp.Paren, exp.TryCast)):
        e = e.this
    return e


def _has_agg(sel: exp.Select) -> bool:
    if sel.args.get("group"):
        return True
    for proj in sel.expressions:
        for n in proj.walk():
            if isinstance(n, (exp.Subquery, exp.Select)) and n is not proj:
                continue
            if isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window):
                return True
    return False


def aggregating_select(tree):
    """The SELECT that produces the rows of the result after aggregation, following a
    plain outer SELECT down through a single CTE or subquery. None if not aggregated."""
    ctes = {c.alias_or_name.lower(): c.this for c in tree.find_all(exp.CTE)}
    node = tree
    for _ in range(20):
        if isinstance(node, exp.Union):
            node = node.this                     # judge a union by its first branch
            continue
        if not isinstance(node, exp.Select):
            return None
        if _has_agg(node):
            return node
        frm = node.args.get(FROM)
        if frm is None or node.args.get("joins"):
            return None                          # joins of row sets: row-level
        src = frm.this
        if isinstance(src, exp.Subquery):
            node = src.this
        elif isinstance(src, exp.Table) and src.name.lower() in ctes:
            node = ctes[src.name.lower()]
        else:
            return None
    return None


def count_columns(sel: exp.Select) -> set:
    out = set()
    for proj in sel.expressions:
        if isinstance(_strip(proj), exp.Count):
            out.add(proj.alias_or_name.lower())
    return out


def patients_per_group(con, tree, agg):
    """Number of distinct patients in every group of the aggregating SELECT (a list), or
    None if the rewrite does not run (e.g. no patientunitstayid in its FROM clause)."""
    sources = []
    frm = agg.args.get(FROM)
    if frm is not None:
        sources.append(frm.this)
    sources += [j.this for j in agg.args.get("joins") or []]
    # the main FROM source first, unqualified last (an unqualified name can be ambiguous)
    quals = [x.alias_or_name for x in sources if x.alias_or_name] + [None]
    with_ = tree.args.get(WITH)
    for qual in quals:
        sel = agg.copy()
        sel.set(WITH, None)
        sel.set("order", None)
        sel.set("limit", None)
        sel.set("having", None)        # may name output aliases that no longer exist
        col = exp.column("patientunitstayid", table=qual)
        sel.set("expressions", [exp.alias_(exp.Count(this=exp.Distinct(expressions=[col])), "n_pat")])
        q = exp.select("n_pat").from_(exp.Subquery(this=sel, alias=exp.TableAlias(
            this=exp.to_identifier("_g")))).where("n_pat >= 1")
        if with_ is not None:
            q.set(WITH, with_.copy())
        try:
            v = [int(r[0]) for r in con.execute(q.sql(dialect="duckdb")).fetchall()]
        except Exception:
            continue
        return v                               # []: every group is empty
    return None


def suppression(sizes, df, cols, counted) -> dict:
    """Small-cell suppression, the usual alternative to blocking a result: the result is
    released, but every group (output row) below k is removed. A row is removed if its
    group has fewer than k distinct patients, or if one of its non-zero count cells is
    below k (the same two tests as min_group). The two tests cannot be matched row by row,
    so the number of removed rows is the larger of the two counts (a lower bound), and
    the removed patients are the smallest groups up to that number (also a lower bound).
    Without patient group sizes, each row's smallest count cell is used ("rows")."""
    cells = []
    if counted:
        sub = df.set_axis(cols, axis=1)[counted].apply(pd.to_numeric, errors="coerce")
        cells = [int(v) for v in sub.where(sub >= 1).min(axis=1).dropna()]
    if sizes is None:
        sizes = cells
    sizes = sorted(sizes)
    out = dict(groups=len(sizes), patients=int(sum(sizes)))
    for k in KS:
        n_small = min(len(sizes), max(sum(x < k for x in sizes), sum(c < k for c in cells)))
        out[f"k{k}"] = dict(groups=n_small, patients=int(sum(sizes[:n_small])))
    return out


def classify(con, sql: str, linked: set) -> dict:
    df = con.execute(sql).df()
    cols = [c.lower() for c in df.columns]
    meta = dict(rows=int(len(df)), identifiable=bool(ID_COLS & set(cols)), group_basis=None)
    if meta["identifiable"]:
        return dict(meta, result_class="identifiable", min_group=1, group_basis="patients")
    if len(df) == 0:
        return dict(meta, result_class="empty", min_group=None)
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        tree = None
    agg = aggregating_select(tree) if tree is not None else None
    any_agg = tree is not None and any(_has_agg(s) for s in tree.find_all(exp.Select))
    named = count_columns(agg) if agg is not None else set()
    ints = {c for c, dt in zip(cols, df.dtypes) if pd.api.types.is_integer_dtype(dt)}
    counted = [c for c in cols if c in named or (c in ints and COUNT_NAME.match(c))]
    cells = None                   # smallest non-zero count cell of the result
    if counted:
        v = pd.to_numeric(df.set_axis(cols, axis=1)[counted].stack(), errors="coerce").dropna()
        v = v[v >= 1]
        cells = int(v.min()) if not v.empty else None
    if agg is not None:
        sizes = patients_per_group(con, tree, agg)
        if sizes is not None:
            if not sizes:
                return dict(meta, result_class="zero", min_group=None)
            n = min(sizes)
            # small-cell rule: every group, and every released count, has at least k
            g = n if cells is None else min(n, cells)
            return dict(meta, result_class="patients", min_group=g, group_basis="patients",
                        group_patients=n, smallest_count=cells,
                        suppression=suppression(sizes, df, cols, counted))
    elif not any_agg:
        return dict(meta, result_class="row-level", min_group=1, group_basis="patients")
    if not counted:
        if agg is None:            # e.g. a join of row sets: treat as row-level
            return dict(meta, result_class="row-level", min_group=1, group_basis="patients")
        return dict(meta, result_class="uncounted", min_group=None)
    vals = pd.to_numeric(df.set_axis(cols, axis=1)[counted].stack(), errors="coerce").dropna()
    pos = vals[vals >= 1]
    if pos.empty:
        return dict(meta, result_class="zero", min_group=None, count_columns=counted)
    return dict(meta, result_class="counted", min_group=int(pos.min()), group_basis="rows",
                count_columns=counted, suppression=suppression(None, df, cols, counted))


# ------------------------------------------------------------------ main
def allowed(m: dict, k: int, strict: bool) -> bool:
    """Same logic as min_group() in extensions.py."""
    if m["result_class"] in ("empty", "zero"):
        return True
    if m["min_group"] is None or (strict and m["group_basis"] != "patients"):
        return not strict
    return m["min_group"] >= k


def suppressed(fr: list, k: int) -> dict:
    """Outcome of an all-region query under small-cell suppression at k, over its regional
    fragments. Identifiable and row-level fragments release nothing (every row is one
    patient); empty and zero fragments lose nothing; uncounted fragments are unknown."""
    g_all = g_sup = p_all = p_sup = 0
    unknown = False
    for m in fr:
        c = m["result_class"]
        if c in ("empty", "zero"):
            continue
        if c in ("identifiable", "row-level"):
            g_all += m["rows"]; g_sup += m["rows"]; p_all += m["rows"]; p_sup += m["rows"]
        elif "suppression" in m:
            s_ = m["suppression"]
            g_all += s_["groups"]; g_sup += s_[f"k{k}"]["groups"]
            p_all += s_["patients"]; p_sup += s_[f"k{k}"]["patients"]
        else:
            unknown = True
    if unknown:
        state = "unknown"
    elif g_all == 0 or g_sup == 0:
        state = "complete"
    elif g_sup == g_all:
        state = "nothing released"
    else:
        state = "partial"
    return {f"k{k}_supp_state": state,
            f"k{k}_supp_groups": g_sup / g_all if g_all and not unknown else None,
            f"k{k}_supp_patients": p_sup / p_all if p_all and not unknown else None}


def main() -> None:
    names = sys.argv[1:]
    if not names:
        raise SystemExit("usage: python privacy-meta.py <workload> [<workload> ...]")
    con = duckdb.connect(str(mw.DB), read_only=True)
    tables = mw.base_tables()
    linked = {r[0] for r in con.execute("""
        SELECT table_name FROM information_schema.columns
        WHERE lower(column_name) = 'patientunitstayid'""").fetchall()} & set(tables)
    cache = {}

    def meta_of(sql):
        key = hashlib.sha1(sql.encode()).hexdigest()
        if key not in cache:
            cache[key] = classify(con, sql, linked)
        return cache[key]

    rows = []                       # one row per all-region query instance
    tpl_rows = defaultdict(list)    # template -> fragment metadata
    for name in names:
        wdir = ROOT / "workloads" / name
        out = []
        with open(wdir / "workload.jsonl", encoding="utf-8") as fh:
            qs = [json.loads(line) for line in fh]
        for q in qs:
            rec = dict(id=q["id"], meta=meta_of(q["sql"]), fragments={})
            if q.get("fragments"):
                for f in q["fragments"]:
                    fsql = mw.scope_to_region(q["sql"], f["site"], linked)
                    fm = meta_of(fsql)
                    rec["fragments"][f["site"]] = fm
                    tpl_rows[(q["template"], q["kind"])].append(fm)
                fr = list(rec["fragments"].values())
                r = dict(workload=name, id=q["id"], template=q["template"],
                         identifiable=any(m["identifiable"] for m in fr))
                for k in KS:
                    r[f"k{k}_strict"] = all(allowed(m, k, True) for m in fr)
                    r[f"k{k}_lenient"] = all(allowed(m, k, False) for m in fr)
                    r.update(suppressed(fr, k))
                rows.append(r)
            out.append(rec)
        with open(wdir / "privacy.jsonl", "w", encoding="utf-8") as fh:
            for rec in out:
                fh.write(json.dumps(rec) + "\n")
        print(f"{name}: {len(out)} queries, {sum(bool(q.get('fragments')) for q in qs)} "
              f"all-region, {len(cache)} distinct results classified so far")
    con.close()

    res = ROOT / "results" / "privacy-meta"
    res.mkdir(parents=True, exist_ok=True)
    t = []
    for (tpl, kind), ms in sorted(tpl_rows.items()):
        classes = pd.Series([m["result_class"] for m in ms]).value_counts()
        groups = [m["min_group"] for m in ms if m["min_group"] is not None]
        t.append(dict(template=tpl, kind=kind, fragments=len(ms),
                      main_class=classes.index[0],
                      classes=" ".join(f"{c}:{n}" for c, n in classes.items()),
                      min_group_min=min(groups) if groups else "",
                      min_group_median=float(np.median(groups)) if groups else ""))
    pd.DataFrame(t).to_csv(res / "templates.csv", index=False)

    d = pd.DataFrame(rows)
    if d.empty:
        print("no all-region queries found")
        return
    summ = [dict(rule="none (federated + residency)", allowed_share=1.0)]
    summ.append(dict(rule="no-identifiable-results", allowed_share=float((~d.identifiable).mean())))
    for k in KS:
        summ.append(dict(rule=f"min-group-{k} (strict)", allowed_share=float(d[f"k{k}_strict"].mean())))
        summ.append(dict(rule=f"min-group-{k} (lenient)", allowed_share=float(d[f"k{k}_lenient"].mean())))
    s = pd.DataFrame(summ)
    s["blocked_share"] = 1 - s["allowed_share"]
    per_w = d.groupby("workload").size()
    s["all_region_queries_per_workload"] = round(per_w.mean(), 1)
    s.to_csv(res / "summary.csv", index=False)

    # small-cell suppression instead of blocking (same queries, same k)
    sup = []
    for k in KS:
        st = d[f"k{k}_supp_state"].value_counts(normalize=True)
        known = d[d[f"k{k}_supp_state"] != "unknown"]
        sup.append(dict(k=k,
                        blocked_share_strict=float(1 - d[f"k{k}_strict"].mean()),
                        complete=float(st.get("complete", 0)), partial=float(st.get("partial", 0)),
                        nothing_released=float(st.get("nothing released", 0)),
                        unknown=float(st.get("unknown", 0)),
                        groups_removed_mean=float(known[f"k{k}_supp_groups"].mean()),
                        patients_removed_mean=float(known[f"k{k}_supp_patients"].mean())))
    sp = pd.DataFrame(sup)
    sp.to_csv(res / "suppression.csv", index=False)
    d.to_csv(res / "queries.csv", index=False)
    print("\nsmall-cell suppression instead of blocking (shares of all-region queries):")
    print(sp.to_string(index=False))
    print(f"\nall-region query instances: {len(d)} in {d.workload.nunique()} workloads, "
          f"{d.template.nunique()} templates")
    print(s.to_string(index=False))
    print(f"\nwritten to {res}  (check templates.csv by hand)")


if __name__ == "__main__":
    main()
