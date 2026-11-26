"""
Show how the eICU demo data are spread over US regions and hospital sizes,
to decide how hospitals are grouped into sites (nodes) of the distributed setting.

Run from the project folder:
    python explore-regions.py
"""
from pathlib import Path

import duckdb

DB = Path(__file__).resolve().parent / "data" / "eicu.duckdb"
con = duckdb.connect(str(DB), read_only=True)

print("hospital table columns:")
print(con.sql("DESCRIBE hospital").fetchall(), "\n")

# All tables that are linked to an ICU stay (except the patient table itself)
linked = [r[0] for r in con.execute("""
    SELECT table_name FROM information_schema.columns
    WHERE lower(column_name) = 'patientunitstayid' AND table_name <> 'patient'
""").fetchall()]
union = " UNION ALL ".join(f"SELECT patientunitstayid FROM {t}" for t in linked)

base = f"""
    WITH rows_per_stay AS (
        SELECT patientunitstayid, COUNT(*) AS n
        FROM ({union}) GROUP BY patientunitstayid
    ),
    stay AS (
        SELECT p.patientunitstayid,
               COALESCE(h.region, 'unknown')          AS region,
               COALESCE(h.numbedscategory, 'unknown') AS beds,
               COALESCE(h.teachingstatus::VARCHAR, 'unknown') AS teaching,
               p.hospitalid,
               COALESCE(r.n, 0) AS n
        FROM patient p
        LEFT JOIN hospital h USING (hospitalid)
        LEFT JOIN rows_per_stay r USING (patientunitstayid)
    )
"""

for col, title in [("region", "By region"),
                   ("beds", "By number of beds"),
                   ("teaching", "By teaching status")]:
    print(title)
    print(con.sql(base + f"""
        SELECT {col},
               COUNT(DISTINCT hospitalid) AS hospitals,
               COUNT(*)                   AS icu_stays,
               SUM(n)                     AS clinical_rows,
               ROUND(100.0 * SUM(n) / SUM(SUM(n)) OVER (), 1) AS share_pct
        FROM stay GROUP BY {col} ORDER BY clinical_rows DESC
    """))
    print()

con.close()
