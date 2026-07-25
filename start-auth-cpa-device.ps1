# Windows: CPA-orchestrated xAI device-flow enroll
# Requires .env:
#   XAI_ENROLLER_CPA_BASE_URL=...
#   XAI_ENROLLER_CPA_MANAGEMENT_SECRET=...

param(
  [string]$SourceFile = "",
  [int]$Index = 0,
  [int]$Count = 1,
  [switch]$Headed,
  [switch]$ImportGrok2Api,
  [string]$JsonOut = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (Test-Path (Join-Path $Root ".env")) {
  Get-Content (Join-Path $Root ".env") | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { return }
    $k, $v = $line.Split("=", 2)
    $k = $k.Trim(); $v = $v.Trim().Trim('"')
    if (-not [string]::IsNullOrWhiteSpace($k)) {
      Set-Item -Path "Env:$k" -Value $v
    }
  }
}

if (-not $env:HTTP_PROXY -and -not $env:HTTPS_PROXY) {
  # optional default clash port; comment out if you don't want it
  # $env:HTTP_PROXY = "http://127.0.0.1:7897"
  # $env:HTTPS_PROXY = "http://127.0.0.1:7897"
}

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  # fall back to WSL project if Windows venv missing
  Write-Host "[*] Windows .venv missing; use WSL command in docs/guides/cpa-device-flow.md" -ForegroundColor Yellow
  throw "Missing $py"
}

if (-not $SourceFile) {
  foreach ($c in @(
    (Join-Path $Root "keys\auth-sessions.jsonl"),
    (Join-Path $Root "auth-local\source-snapshot.jsonl"),
    (Join-Path $Root "keys\accounts.txt")
  )) {
    if (Test-Path $c) { $SourceFile = $c; break }
  }
}
if (-not $SourceFile) { throw "No source file found (auth-sessions.jsonl / accounts.txt)" }

New-Item -ItemType Directory -Force -Path (Join-Path $Root "auth-local\authenticated"), (Join-Path $Root "keys"), (Join-Path $Root "logs") | Out-Null

$argsList = @(
  (Join-Path $Root "scripts\cpa_xai_device_enroll.py"),
  "--source-file", $SourceFile,
  "--index", "$Index",
  "--count", "$Count"
)
if ($Headed) { $argsList += "--headed" }
if ($ImportGrok2Api) { $argsList += "--import-grok2api" }
if ($JsonOut) { $argsList += @("--json-out", $JsonOut) }

Write-Host "[*] CPA device-flow enroll"
Write-Host "    source : $SourceFile"
Write-Host "    cpa    : $($env:XAI_ENROLLER_CPA_BASE_URL)"
Write-Host "    index  : $Index count=$Count headed=$Headed"

& $py @argsList
exit $LASTEXITCODE
