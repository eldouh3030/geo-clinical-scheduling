# Scheduling clinical analytics across geo-distributed hospitals

Benchmark and simulator used in the paper *The Performance Cost of Privacy and Security
Constraints in Geo-Distributed Clinical Analytics: A Benchmark with Real ICU Data and Queries*.

- **Data**: eICU Collaborative Research Database Demo v2.0.1 (open access, 186 hospitals, 5 US regions).
- **Queries**: 84 real SQL queries from the official eICU code repository.
- **Simulator**: regional sites linked by a WAN, a cloud node, five schedulers, three
  execution modes (raw, federated, federated + residency), pluggable privacy rules and security costs.

All measurements in the paper were made on one laptop (AMD Ryzen 5 4500U, 6 cores, 8 GB RAM,
Windows 11, Python 3.13.17, DuckDB 2.0.0, cryptography 50.0.1 with OpenSSL 4.0.2).

The results of the paper are included: `results\<experiment>\results.csv` (one row per workload,
load and policy), `results\privacy-meta\` (classification of every query and the small-cell
suppression table), `measurements\crypto.json` and the tables and figures in `reports\`.
`run-all.ps1` and `run-missing.ps1` rerun every experiment of the paper after the setup below.

---

## 1. Setup (Windows, PowerShell)

1. Install **Python 3.13** from python.org (tick *Add python.exe to PATH*).
2. In PowerShell, from this folder:

```
python -m venv venv
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Every new PowerShell window: `cd` to this folder, then `.\venv\Scripts\Activate.ps1`.

## 2. Data and queries

```
mkdir data\eicu
mkdir sources
```

- Download the ZIP from https://physionet.org/content/eicu-crd-demo/2.0.1/ (section *Files*) and extract it into `data\eicu`:
  `Get-ChildItem "$HOME\Downloads\eicu-collaborative*.zip" | Expand-Archive -DestinationPath data\eicu -Force`
- Download https://github.com/MIT-LCP/eicu-code/archive/refs/heads/main.zip and extract it into `sources`:
  `Get-ChildItem "$HOME\Downloads\eicu-code-main*.zip" | Expand-Archive -DestinationPath sources -Force`

```
python load-eicu.py          # builds data\eicu.duckdb (about 105 MB) and prints data per hospital
python explore-regions.py    # optional: data per region, bed size and teaching status
python extract-queries.py    # translates and tests the repository queries -> queries\eicu\catalog.csv
```

Expected: 89 statements found, 86 run; 84 templates are used (see the paper, Section 4.2).

## 3. Workloads

```
1..10 | ForEach-Object { python make-workload.py configs\base-workload.yaml $_ }
1..5  | ForEach-Object { python make-workload.py configs\mix10-workload.yaml $_; python make-workload.py configs\mix50-workload.yaml $_ }
```

Each call writes `workloads\<name>-s<seed>\` with `topology.json`, `partitions.csv`, the Parquet
partitions, and `workload.jsonl` (2,000 queries with partitions, measured processing times,
result sizes and, for all-region queries, one fragment per region). About 1 minute each.

## 4. Experiments of the paper

| Config | What | Runs | Time on the laptop |
|---|---|---|---|
| `final-raw.yaml` | raw execution, 10 workloads, 5 loads, 5 policies | 250 | ~20 min |
| `final-federated.yaml` | federated execution | 250 | ~20 min |
| `final-residency.yaml` | federated + residency rule | 250 | ~20 min |
| `region-check.yaml` | per-region behaviour (saves every query) | 90 | ~15 min |
| `sensitivity-wan.yaml` | WAN 50/100/200/500 Mb/s x 3 modes | 360 | ~40 min |
| `sensitivity-cache.yaml` | cache 0/15/30% x 3 modes | 270 | ~30 min |
| `sensitivity-mix.yaml` | 10/30/50% all-region queries x 3 modes | 270 | ~30 min |

```
python run-experiment.py configs\final-raw.yaml | Tee-Object -FilePath final-raw-log.txt
python summarize.py final-raw
```

Keep the laptop plugged in and stop it from sleeping during long runs.
Our results are already in `results\` for comparison.

## 5. Tables and figures

```
python report.py main final-raw final-federated final-residency
python report.py regions region-check
python report.py sweep sensitivity-wan
```

Output in `reports\<name>\`: PNG figures (300 dpi), `table.csv`, `tests.csv` (paired Wilcoxon tests
between consecutive experiments).

## 6. Privacy rules and security costs (Sections 5 and 6.7)

Everything is declared in **`extensions.py`** and switched on by name in a configuration:

```
simulator: {decompose: true, rules: [residency, min-group-10], overheads: [aes-gcm], session_costs: [tls13]}
```

- A **rule** is `def my_rule(sim, q, node) -> bool`: may query (or fragment) `q` run on `node`?
- A **cost** is `def my_cost(sim, src, dst, nbytes) -> seconds`: extra time for a transfer.
- A **session cost** is `def my_cost(sim, src, dst) -> seconds`: time before data can flow
  between two nodes (e.g. a handshake); `sim.sessions` holds the pairs already connected.

The simulator applies rules and costs to **every** scheduler. A query that no node may run is
not scheduled and is counted in `blocked` / `unfinished` (one blocked fragment blocks the
whole query). Because blocked queries leave the mean, results also report the mean response
time per scope (`resp_mean_patient_s`, `resp_mean_region_s`, `resp_mean_all_s`).

| Name | Kind | Meaning |
|---|---|---|
| `residency` | rule | raw data processed only in its region (reference) |
| `no-raw-on-cloud` | rule | the cloud never processes raw data (implied by residency) |
| `no-identifiable-results` | rule | a fragment result with a patient identifier column may not leave its region |
| `min-group-5/10/20` | rule | a fragment result may leave its region only if every group has at least k patients (strict) |
| `min-group-k-lenient` | rule | same, but row counts and unknown group sizes pass (upper bound) |
| `aes-gcm`, `chacha20` | cost | AEAD encryption + decryption on WAN and site-cloud links (`-all`: also the LAN) |
| `tls13`, `tls13-mlkem` | session cost | TLS 1.3 handshake (1 RTT + measured CPU), once per node pair |
| `tls13-per-transfer` | session cost | a new handshake for every transfer (stress case) |

Steps, after the ten base workloads exist (section 3):

```
python measure-crypto.py                     # AES-GCM, ChaCha20, handshake CPU -> measurements\crypto.json
python privacy-meta.py base-s1 base-s2 base-s3 base-s4 base-s5 base-s6 base-s7 base-s8 base-s9 base-s10
                                             # identifiers and group sizes -> workloads\*\privacy.jsonl,
                                             # results\privacy-meta\templates.csv, summary.csv, suppression.csv and queries.csv
python run-experiment.py configs\final-k10.yaml
python summarize.py final-k10
python report.py main --out privacy final-residency final-noid final-k5 final-k10 final-k20
```

`privacy-meta.py` does not change `workload.jsonl`; its classification of every template is
in `results\privacy-meta\templates.csv` and should be checked by hand.
Configurations: `final-noid`, `final-k5`, `final-k10`, `final-k20`, `final-k10-lenient`,
`final-raw-tls`, `final-federated-tls`, `final-residency-tls`, `final-residency-tls-pt`,
`final-residency-tls-mlkem` (optional), `region-privacy`. They keep the workloads, loads,
reference and policies of the base study.

## 7. Files

| File | Purpose |
|---|---|
| `load-eicu.py` | loads the 31 CSV tables into DuckDB |
| `explore-regions.py` | data per region / bed size / teaching status |
| `extract-queries.py` | collects, translates and tests the repository queries |
| `make-workload.py` | topology, Parquet partitions, query instances, DuckDB measurements |
| `simulator.py` | discrete-event simulator: nodes, links, cache, fragments, metrics |
| `policies.py` | hash, FIFO, delay, WAN-aware, min-cost flow |
| `extensions.py` | privacy rules and security costs |
| `privacy-meta.py` | identifier columns and smallest group size of every result and fragment, and what small-cell suppression would remove instead of blocking (`suppression.csv`) |
| `measure-crypto.py` | encryption and handshake costs measured on the evaluation machine |
| `figure6.py` | Figure 6 of the paper (release rules) |
| `run-experiment.py` | calibration, runs (parallel), results.csv |
| `summarize.py` | means, 95% CIs, Wilcoxon tests against delay scheduling |
| `report.py` | figures and tables in the style of the paper |

## 8. Licences

Code: MIT (see `LICENSE`). eICU-CRD Demo: Open Data Commons Open Database License v1.0 (PhysioNet). eicu-code: MIT.
