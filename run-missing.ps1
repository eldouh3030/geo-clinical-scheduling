# Reruns the experiments that were still old (from Nov 12) on this laptop:
# region check, and the three sensitivity studies (WAN, cache, query mix).
# Start it from the project folder with the venv active:
#     .\run-missing.ps1
# Each step writes a log in logs\. Keep the laptop plugged in and stop it from sleeping.

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

# 1. the mix10 and mix50 workloads, only if they are missing (about 1 min each)
foreach ($m in "mix10", "mix50") {
    foreach ($s in 1..5) {
        if (-not (Test-Path "workloads\$m-s$s\workload.jsonl")) {
            Step "make-$m-s$s" { python make-workload.py "configs\$m-workload.yaml" $s }
        } else {
            Write-Host "workloads\$m-s$s already exists"
        }
    }
}

# 2. remove the old results of these experiments, so nothing old is mixed in
foreach ($c in "region-check", "sensitivity-wan", "sensitivity-cache", "sensitivity-mix") {
    foreach ($d in "results\$c", "reports\$c") {
        if (Test-Path $d) { Remove-Item $d -Recurse -Force }
    }
}

# 3. the experiments (about 2 h in total)
foreach ($c in "region-check", "sensitivity-wan", "sensitivity-cache", "sensitivity-mix") {
    Step $c { python run-experiment.py "configs\$c.yaml" }
}

# 4. reports
Step "report-region-check" { python report.py regions region-check }
Step "report-region-privacy" { python report.py regions region-privacy }
Step "report-wan" { python report.py sweep sensitivity-wan }
Step "report-cache" { python report.py sweep sensitivity-cache }
Step "report-mix" { python report.py sweep sensitivity-mix }

# 5. one zip with everything Claude needs
$pack = "paper-results-2"
if (Test-Path $pack) { Remove-Item $pack -Recurse -Force }
New-Item -ItemType Directory -Force (Join-Path $pack "runs") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $pack "reports") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $pack "logs") | Out-Null
foreach ($c in "region-check", "sensitivity-wan", "sensitivity-cache", "sensitivity-mix") {
    if (Test-Path "results\$c\results.csv") { Copy-Item "results\$c\results.csv" (Join-Path $pack "runs\$c.csv") }
}
foreach ($c in "region-check", "region-privacy", "sensitivity-wan", "sensitivity-cache", "sensitivity-mix") {
    if (Test-Path "reports\$c") { Copy-Item "reports\$c" (Join-Path $pack "reports") -Recurse }
}
Get-ChildItem logs -Filter *.txt | Where-Object { $_.LastWriteTime -ge $start } |
    ForEach-Object { Copy-Item $_.FullName (Join-Path $pack "logs") }
$zip = "results-part-2.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "$pack\*" -DestinationPath $zip

Write-Host "`nALL DONE in $([int]((Get-Date) - $start).TotalMinutes) min"
if ($failed.Count) { Write-Host "Failed steps: $($failed -join ', ')" -ForegroundColor Red }
else { Write-Host "No failed steps." -ForegroundColor Green }
Write-Host "Send Claude the file: $((Resolve-Path $zip).Path)"
