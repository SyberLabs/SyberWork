param(
    [string]$Repo = (Join-Path $PSScriptRoot '..\..'),
    [string]$Python,
    [string]$OutputRoot,
    [string]$Access,
    [switch]$PromptForToken
)
# Run as a child process: pwsh -NoProfile -File .\Run-GitHits-Upstream-v3.ps1 ...
# The token is used only for preflight and cleared before any model process.
$ErrorActionPreference = 'Stop'
if (-not $Python) { throw 'Supply -Python with the absolute pinned interpreter path.' }
$taskRepo = (Resolve-Path -LiteralPath $Repo).Path
$taskPython = (Resolve-Path -LiteralPath $Python).Path
if (-not $OutputRoot) { $OutputRoot = Join-Path $taskRepo '..\githits-results' }
$taskOutputRoot = [IO.Path]::GetFullPath($OutputRoot)
if ($taskOutputRoot -eq $taskRepo -or $taskOutputRoot.StartsWith($taskRepo + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Keep results outside the checkout.'
}
[void][IO.Directory]::CreateDirectory($taskOutputRoot)
$taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$taskAccess = Join-Path $taskOutputRoot "upstream-v3-access-$taskStamp"
$taskMatrix = Join-Path $taskOutputRoot "upstream-v3-matrix-$taskStamp"
$taskReplay = "$taskMatrix-recheck.json"
$taskSecret = $null
$taskPointer = [IntPtr]::Zero
Push-Location $taskRepo
try {
    & $taskPython -c 'from benchmarks.githits.upstream_v3 import registration,check_interpreter; import sys; registration(); check_interpreter(sys.executable); print("Frozen registration and package pins verified")'
    if ($LASTEXITCODE -ne 0) { throw 'Registration or dependency mismatch; no calls started.' }
    if (git status --porcelain --untracked-files=normal) { throw 'Use a clean checkout; keep the environment and output outside it.' }
    Write-Host 'Four upstream-derived bugs. Same model/high effort; two-call cap, one repeat; maximum 16 calls.'
    if ($Access) {
        $taskAccess = (Resolve-Path -LiteralPath $Access).Path
        & $taskPython -c 'import sys; from pathlib import Path; from benchmarks.githits.upstream_v3 import checked_contexts; checked_contexts(Path(sys.argv[1])); print("Saved authenticated preflight verified; no new retrieval")' $taskAccess
        if ($LASTEXITCODE -ne 0) { throw 'Saved preflight failed verification; no new retrieval or model calls.' }
    } else {
      if (-not $env:GITHITS_API_TOKEN -and -not $PromptForToken) {
          throw 'No GitHits credential is configured. No prompt, API request or model call was made. Use an existing environment credential or -Access with saved authenticated receipts.'
      }
      foreach ($taskSetting in 'GITHITS_USAGE_NODE','GITHITS_USAGE_HOME') {
        if (-not [Environment]::GetEnvironmentVariable($taskSetting,'Process')) {
            [Environment]::SetEnvironmentVariable($taskSetting,[Environment]::GetEnvironmentVariable($taskSetting,'User'),'Process')
        }
        if (-not [Environment]::GetEnvironmentVariable($taskSetting,'Process')) { throw "Configure approved private accounting first: $taskSetting" }
      }
      if (-not (Test-Path -LiteralPath $env:GITHITS_USAGE_NODE -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $env:GITHITS_USAGE_HOME 'usage.mjs') -PathType Leaf)) {
        throw 'Approved private accounting runtime is missing; no direct fallback.'
      }
      Write-Host 'First: five accounted source reads, zero model calls. Then the frozen eight-cell matrix.'
      if (-not $env:GITHITS_API_TOKEN) {
        $taskSecret = Read-Host 'Paste the GitHits token (hidden), then press Enter' -AsSecureString
        if ($taskSecret.Length -eq 0) { throw 'No token entered; no requests started.' }
        $taskPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskSecret)
        $env:GITHITS_API_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskPointer)
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskPointer)
        $taskPointer = [IntPtr]::Zero
      }
      & $taskPython -m benchmarks.githits.upstream_v3 preflight --python $taskPython --write $taskAccess
      if ($LASTEXITCODE -ne 0) { throw 'Preflight stopped; retain its partial receipt directory.' }
    }
    $env:GITHITS_API_TOKEN = $null
    if ($null -ne $taskSecret) { $taskSecret.Dispose(); $taskSecret = $null }
    & $taskPython -m benchmarks.githits.upstream_v3 run --python $taskPython --access $taskAccess --write $taskMatrix
    if ($LASTEXITCODE -ne 0) { throw 'Matrix interrupted; preserve partial evidence and do not silently restart.' }
    & $taskPython -m benchmarks.githits.upstream_v3 verify --python $taskPython --folder $taskMatrix --write $taskReplay
    if ($LASTEXITCODE -ne 0) { throw 'Host replay failed; preserve evidence for review.' }
    Write-Host "Matrix: $taskMatrix"
    Write-Host "Replay: $taskReplay"
    Write-Host 'Run finished. Return these paths for patch/evidence review, report and benchmark-branch push.'
} finally {
    if ($taskPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskPointer) }
    if ($null -ne $taskSecret) { $taskSecret.Dispose() }
    $env:GITHITS_API_TOKEN = $null
    Pop-Location
}
