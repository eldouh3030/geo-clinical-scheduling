# Runs every remaining step of the privacy and security part on this machine, in order.
# Start it from the project folder with the venv active:
#     .\run-all.ps1
# Each step writes a log in logs\. A step that fails is reported at the end; the next
# steps still run. Keep the laptop plugged in and stop it from sleeping.

$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Force logs | Out-Null
$failed = @()
$start = Get-Date

function Step($name, [scriptblock]$cmd) {
    $t = Get-Date
    Write-Host "`n=== $name  (started $($t.ToString('HH:mm')))" -ForegroundColor Cyan
    & $cmd 2>&1 | Tee-Object -FilePath "logs\$name.txt"
    if ($LASTEXITCODE -ne 0) { $script:failed += $name; Write-Host "!!! $name FAILED" -ForegroundColor Red }
    Write-Host "=== $name done in $([int]((Get-Date) - $t).TotalMinutes) min"
}

# 1. privacy metadata and small-cell suppression (about 15 min)
Step "privacy-meta" { python privacy-meta.py base-s1 base-s2 base-s3 base-s4 base-s5 base-s6 base-s7 base-s8 base-s9 base-s10 }

# 2. baselines again on this machine, with the new columns (about 1 h)
foreach ($c in "final-raw", "final-federated", "final-residency") {
    Step $c { python run-experiment.py "configs\$c.yaml" }
}

# 3. result-release rules (about 1-1.5 h)
foreach ($c in "final-noid", "final-k5", "final-k10", "final-k20", "final-k10-lenient") {
    Step $c { python run-experiment.py "configs\$c.yaml" }
}

# 4. encryption and handshakes (about 1.5-2 h)
foreach ($c in "final-raw-tls", "final-federated-tls", "final-residency-tls", "final-residency-tls-pt", "final-residency-tls-mlkem") {
    Step $c { python run-experiment.py "configs\$c.yaml" }
}

# 5. per-region behaviour under the rules (about 15 min)
Step "region-privacy" { python run-experiment.py configs\region-privacy.yaml }

# 6. reports
Step "report-main" { python report.py main final-raw final-federated final-residency }
Step "report-privacy" { python report.py main --out privacy final-residency final-noid final-k5 final-k10 final-k20 }
Step "report-bounds" { python report.py main --out privacy-bounds final-k10 final-k10-lenient }
Step "report-encryption" { python report.py main --out encryption final-raw final-raw-tls final-federated final-federated-tls final-residency final-residency-tls final-residency-tls-pt final-residency-tls-mlkem }
Step "report-regions" { python report.py regions region-privacy }

# 7. one zip with everything Claude needs to update the paper
$pack = "paper-results"
if (Test-Path $pack) { Remove-Item $pack -Recurse -Force }
New-Item -ItemType Directory -Force (Join-Path $pack "runs") | Out-Null
Get-ChildItem results -Directory | Where-Object { $_.Name -like "final-*" -or $_.Name -eq "region-privacy" } |
    ForEach-Object { Copy-Item (Join-Path $_.FullName "results.csv") (Join-Path $pack "runs\$($_.Name).csv") }
Copy-Item results\privacy-meta, reports, measurements, logs $pack -Recurse
$zip = "results-for-paper.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "$pack\*" -DestinationPath $zip

Write-Host "`nALL DONE in $([int]((Get-Date) - $start).TotalMinutes) min"
if ($failed.Count) { Write-Host "Failed steps: $($failed -join ', ')" -ForegroundColor Red }
else { Write-Host "No failed steps." -ForegroundColor Green }
Write-Host "Send Claude the file: $((Resolve-Path $zip).Path)"
