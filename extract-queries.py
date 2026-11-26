"""
Collect the real SQL queries of the official eICU code repository (MIT-LCP/eicu-code),
translate them to the DuckDB dialect, run each one on the local eICU demo database,
and write a catalog of the queries that work.

Sources:
  concepts/**/*.sql      derived clinical concepts (pivoted vitals, labs, scores, ...)
  notebooks/**/*.ipynb   exploratory queries written by the database authors

Run from the project folder:
    python extract-queries.py

Output:
  queries/eicu/<id>.sql          each query in DuckDB SQL (notebook queries keep {p1}, {p2} placeholders)
  queries/eicu/catalog.csv       one row per query: source, status, run time, rows, tables read
"""
import csv
import json
import re
import time
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent
REPO = next((ROOT / "sources").glob("eicu-code*"), None)
DB = ROOT / "data" / "eicu.duckdb"
OUT = ROOT / "queries" / "eicu"

SCHEMA_PREFIXES = [
    "`physionet-data.eicu_crd_derived.", "`physionet-data.eicu_crd.",
    "physionet-data.eicu_crd_derived.", "physionet-data.eicu_crd.",
    "eicu_crd_derived.", "eicu_crd.",
]


def strip_schema(sql: str) -> str:
    """Remove the BigQuery / Postgres schema names so that tables resolve in DuckDB."""
    for p in SCHEMA_PREFIXES:
        if p.startswith("`"):
            # `physionet-data.eicu_crd.patient` -> patient
            sql = re.sub(re.escape(p) + r"([A-Za-z0-9_]+)`", r"\1", sql)
        else:
            sql = sql.replace(p, "")
    return sql


def tables_read(expr: exp.Expression) -> list[str]:
    """Base tables read by a statement (CTE names excluded)."""
    ctes = {c.alias_or_name.lower() for c in expr.find_all(exp.CTE)}
    target = None
    if isinstance(expr, exp.Create):
        target = expr.this.find(exp.Table)
    names = set()
    for t in expr.find_all(exp.Table):
        if target is not None and t is target:
            continue
        n = t.name.lower()
        if n and n not in ctes:
            names.add(n)
    return sorted(names)


SKIP_TYPES = tuple(getattr(exp, n) for n in ("Drop", "Set", "Command") if hasattr(exp, n))


# ---------------------------------------------------------------- concept files
def concept_statements():
    for f in sorted((REPO / "concepts").rglob("*.sql")):
        raw = f.read_text(encoding="utf-8", errors="replace")
        dialect = "bigquery" if "`" in raw else "postgres"
        sql = strip_schema(raw)
        # DuckDB has no materialized views; DROP statements are not needed
        sql = re.sub(r"(?i)CREATE\s+MATERIALIZED\s+VIEW", "CREATE TABLE", sql)
        sql = re.sub(r"(?im)^\s*DROP\s+[^;]*;", "", sql)
        try:
            parsed = sqlglot.parse(sql, read=dialect)
        except Exception as e:  # noqa: BLE001
            yield dict(id=f"concept-{f.stem}", source=str(f.relative_to(REPO)),
                       kind="concept", dialect=dialect, sql=None, tables=[],
                       params=[], error=f"parse: {e}".splitlines()[0])
            continue
        k = 0
        for stmt in parsed:
            if stmt is None or isinstance(stmt, SKIP_TYPES):
                continue
            k += 1
            sid = f"concept-{f.stem}" + (f"-{k}" if k > 1 else "")
            try:
                if isinstance(stmt, exp.Create) and stmt.expression is not None:
                    name = stmt.this.find(exp.Table).name.lower()
                    body = stmt.expression.sql(dialect="duckdb")
                    duck = f"CREATE OR REPLACE TABLE {name} AS\n{body}"
                else:
                    duck = stmt.sql(dialect="duckdb")
                yield dict(id=sid, source=str(f.relative_to(REPO)), kind="concept",
                           dialect=dialect, sql=duck, tables=tables_read(stmt),
                           params=[], error=None)
            except Exception as e:  # noqa: BLE001
                yield dict(id=sid, source=str(f.relative_to(REPO)), kind="concept",
                           dialect=dialect, sql=None, tables=[], params=[],
                           error=f"translate: {e}".splitlines()[0])


# ---------------------------------------------------------------- notebook queries
STRING_RE = re.compile(r'"""(.*?)"""(\s*\.format\(([^\n]*)\))?', re.S)


def split_args(s: str) -> list[str]:
    """Split the arguments of .format(...) at top-level commas."""
    s = s.strip()
    if s.endswith(")"):
        s = s[:-1]
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def find_assignment(name: str, code: str):
    """Value assigned to a variable in the notebook, e.g.  drug = 'aspirin'."""
    m = None
    for m in re.finditer(rf"^\s*{re.escape(name)}\s*=\s*(.+)$", code, re.M):
        pass
    if not m:
        return None
    rhs = m.group(1).split("#")[0].strip()
    lit = re.fullmatch(r"""(['"])(.*)\1""", rhs)
    if lit:
        return lit.group(2)
    if re.fullmatch(r"-?\d+", rhs):
        return int(rhs)
    return None


def notebook_queries():
    for f in sorted((REPO / "notebooks").rglob("*.ipynb")):
        nb = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        code = "\n".join("".join(c.get("source", []))
                         for c in nb.get("cells", []) if c.get("cell_type") == "code")
        k = 0
        for m in STRING_RE.finditer(code):
            text = m.group(1)
            if not re.search(r"(?is)\bselect\b.*\bfrom\b", text):
                continue
            k += 1
            args = split_args(m.group(3)) if m.group(3) else []
            # positional {} or {0} placeholders -> {p1}, {p2}, ...
            counter = iter(range(1, 100))
            text = re.sub(r"\{(\d*)\}",
                          lambda mm: "{p%d}" % (int(mm.group(1)) + 1 if mm.group(1)
                                                else next(counter)), text)
            n_ph = len(set(re.findall(r"\{p(\d+)\}", text)))
            params = []
            for a in args[:n_ph]:
                base = re.split(r"[.\[(]", a)[0].strip()
                params.append(dict(arg=a, name=base,
                                   value=find_assignment(base, code)))
            yield dict(id=f"nb-{f.stem}-{k}", source=str(f.relative_to(REPO)),
                       kind="notebook", dialect="postgres", sql=text.strip(),
                       tables=[], params=params, error=None)


def translate_notebook(q):
    """Translate a notebook query (with {pN} placeholders) to DuckDB."""
    marker = {}
    sql = q["sql"]
    for i in range(1, len(q["params"]) + 1):
        token = f"__p{i}__"
        marker[token] = "{p%d}" % i
        sql = sql.replace("{p%d}" % i, token)
    sql = strip_schema(sql)
    stmt = sqlglot.parse_one(sql, read="postgres")
    duck = stmt.sql(dialect="duckdb", pretty=True)
    for token, ph in marker.items():
        duck = duck.replace(token, ph)
    return duck, tables_read(stmt)


def fill_params(sql: str, params, defaults) -> str | None:
    values = {}
    for i, p in enumerate(params, start=1):
        v = p["value"]
        if v is None:
            v = defaults.get(p["name"])
        if v is None:
            return None
        values[f"p{i}"] = v
    for k, v in values.items():
        sql = sql.replace("{%s}" % k, str(v))
    return sql


# ---------------------------------------------------------------- main
def main() -> None:
    if REPO is None:
        raise SystemExit("Repository not found: unpack eicu-code-main.zip into the 'sources' folder")
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB))

    # sample values for notebook parameters that are not assigned in the notebook
    pid, upid = con.execute(
        "SELECT patientunitstayid, uniquepid FROM patient ORDER BY patientunitstayid LIMIT 1"
    ).fetchone()
    defaults = {"patientunitstayid": pid, "uniquepid": upid}

    queries = list(concept_statements())
    for q in notebook_queries():
        try:
            q["sql"], q["tables"] = translate_notebook(q)
        except Exception as e:  # noqa: BLE001
            q["error"] = f"translate: {e}".splitlines()[0]
        queries.append(q)

    # Run. Concept queries depend on each other (e.g. OASIS needs pivoted tables),
    # so failed concepts are retried until no further progress is made.
    pending = [q for q in queries if q["sql"] and not q["error"]]
    for q in queries:
        q.setdefault("status", "error" if q["error"] else "pending")
    for _round in range(6):
        progress = False
        for q in pending:
            if q["status"] == "ok":
                continue
            sql = q["sql"]
            if q["kind"] == "notebook":
                sql = fill_params(sql, q["params"], defaults)
                if sql is None:
                    q["status"], q["error"] = "skipped", "parameter value unknown"
                    continue
            try:
                t0 = time.perf_counter()
                res = con.execute(sql)
                rows = len(res.fetchall()) if q["kind"] == "notebook" else None
                q["seconds"] = round(time.perf_counter() - t0, 4)
                if rows is None:
                    name = re.search(r"CREATE OR REPLACE TABLE (\w+)", sql)
                    rows = con.execute(f"SELECT COUNT(*) FROM {name.group(1)}").fetchone()[0] if name else None
                q["rows"] = rows
                q["status"], q["error"] = "ok", None
                progress = True
            except Exception as e:  # noqa: BLE001
                q["status"], q["error"] = "error", f"run: {e}".splitlines()[0][:200]
        if not progress:
            break
    con.close()

    # Write the queries and the catalog
    with open(OUT / "catalog.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "kind", "source", "dialect", "status", "seconds", "rows",
                    "tables", "params", "error"])
        for q in queries:
            if q["sql"]:
                (OUT / f"{q['id']}.sql").write_text(q["sql"] + "\n", encoding="utf-8")
            w.writerow([q["id"], q["kind"], q["source"], q["dialect"], q["status"],
                        q.get("seconds", ""), q.get("rows", ""),
                        " ".join(q["tables"]),
                        " ".join(p["name"] for p in q["params"]),
                        q["error"] or ""])

    n_ok = sum(q["status"] == "ok" for q in queries)
    print(f"{len(queries)} queries found, {n_ok} run successfully on DuckDB")
    for kind in ("concept", "notebook"):
        sub = [q for q in queries if q["kind"] == kind]
        print(f"  {kind:<9} {sum(q['status'] == 'ok' for q in sub):>3} ok / {len(sub)}")
    print(f"\nCatalog written to {OUT / 'catalog.csv'}")


if __name__ == "__main__":
    main()
