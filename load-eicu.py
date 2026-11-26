"""
Load the eICU Collaborative Research Database Demo (v2.0.1) into a local
DuckDB database and print a summary of the tables and of how the data are
spread across the 20 source hospitals.

Run from the project folder:
    python load-eicu.py
"""
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "data" / "eicu"
DB = ROOT / "data" / "eicu.duckdb"


def main() -> None:
    files = sorted(SRC.rglob("*.csv.gz"))
    if not files:
        raise SystemExit(f"No .csv.gz files found under {SRC}")

    con = duckdb.connect(str(DB))
    print(f"Loading {len(files)} tables into {DB}\n")

    for f in files:
        table = f.name[: -len(".csv.gz")].lower()
        path = f.as_posix().replace("'", "''")
        con.execute(
            f"CREATE OR REPLACE TABLE {table} AS "
            f"SELECT * FROM read_csv_auto('{path}', header=true, sample_size=-1)"
        )

    # 1. Rows and columns per table
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name").fetchall()]

    print(f"{'table':<28}{'rows':>12}{'columns':>9}  linked to stay")
    total_rows = 0
    linked = []
    for t in tables:
        rows = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        cols = [r[0].lower() for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ?", [t]).fetchall()]
        has_stay = "patientunitstayid" in cols
        if has_stay and t != "patient":
            linked.append(t)
        total_rows += rows
        print(f"{t:<28}{rows:>12,}{len(cols):>9}  {'yes' if has_stay else 'no'}")
    print(f"{'TOTAL':<28}{total_rows:>12,}\n")

    # 2. How the data are spread over hospitals
    #    Every clinical row is linked to an ICU stay, and every stay to one hospital.
    union = " UNION ALL ".join(
        f"SELECT patientunitstayid FROM {t}" for t in linked)
    per_hospital = con.execute(f"""
        WITH rows_per_stay AS (
            SELECT patientunitstayid, COUNT(*) AS n
            FROM ({union}) GROUP BY patientunitstayid
        )
        SELECT p.hospitalid,
               COUNT(DISTINCT p.patientunitstayid) AS stays,
               COALESCE(SUM(r.n), 0)              AS clinical_rows
        FROM patient p
        LEFT JOIN rows_per_stay r USING (patientunitstayid)
        GROUP BY p.hospitalid
        ORDER BY clinical_rows DESC
    """).fetchall()

    print(f"Data per hospital ({len(per_hospital)} hospitals)")
    print(f"{'hospitalid':>10}{'ICU stays':>11}{'clinical rows':>15}{'share':>8}")
    all_rows = sum(r[2] for r in per_hospital) or 1
    for hid, stays, n in per_hospital:
        print(f"{hid:>10}{stays:>11,}{n:>15,}{100 * n / all_rows:>7.1f}%")

    con.close()
    size_mb = DB.stat().st_size / 1e6
    print(f"\nDatabase file: {DB} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
