param([string]$Repo, [string]$WriteRoot)
# Offline process-control tests. The Python process is a recording test double.
# No GitHits requests, model calls, or benchmark outcomes are produced.
$ErrorActionPreference = 'Stop'
if (-not $Repo -or -not $WriteRoot) { throw 'Supply a clean -Repo and new -WriteRoot outside the checkout.' }
$taskRepo = (Resolve-Path -LiteralPath $Repo).Path
$taskRoot = [IO.Path]::GetFullPath($WriteRoot)
if (Test-Path -LiteralPath $taskRoot) { throw 'Test output must be new.' }
[void][IO.Directory]::CreateDirectory($taskRoot)
$taskLauncher = Join-Path $PSScriptRoot 'run-upstream-v3.ps1'
$taskStub = Join-Path $taskRoot 'stub-python.ps1'
$taskDriver = Join-Path $taskRoot 'driver.ps1'
$taskStubText = @'
$phase = if ($args[0] -eq '-c') {
    if ($args[1] -match 'checked_contexts') { 'cached_validate' } else { 'registration' }
} else { [string]$args[2] }
@{phase=$phase;token_present=[bool]$env:GITHITS_API_TOKEN} | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:TEST_LAUNCHER_LOG
$global:LASTEXITCODE = if ($phase -eq $env:TEST_LAUNCHER_FAIL) { 1 } else { 0 }
'@
$taskDriverText = @'
$ErrorActionPreference='Stop'
$c=Get-Content -LiteralPath $args[0] -Raw | ConvertFrom-Json
$env:GITHITS_API_TOKEN=if ($c.has_token) { 'offline-unit-sentinel-not-a-credential' } else { $null }
$env:GITHITS_USAGE_NODE=$c.node
$env:GITHITS_USAGE_HOME=$c.runtime
$env:TEST_LAUNCHER_LOG=$c.log
$env:TEST_LAUNCHER_FAIL=$c.fail_phase
$options=@{Repo=$c.repo;Python=$c.stub;OutputRoot=$c.output}
if ($c.access) { $options.Access=$c.access }
try { & $c.launcher @options } catch { Write-Output $_.Exception.Message; exit 1 }
if ($env:GITHITS_API_TOKEN) { throw 'Child retained its credential after completion.' }
'@
Set-Content -LiteralPath $taskStub -Value $taskStubText -Encoding utf8
Set-Content -LiteralPath $taskDriver -Value $taskDriverText -Encoding utf8
$taskNode = Join-Path $taskRoot 'node-placeholder'
$taskRuntime = Join-Path $taskRoot 'runtime'
$taskAccess = Join-Path $taskRoot 'saved-access'
[void][IO.Directory]::CreateDirectory($taskRuntime)
[void][IO.Directory]::CreateDirectory($taskAccess)
Set-Content -LiteralPath $taskNode -Value 'offline placeholder; never executed'
Set-Content -LiteralPath (Join-Path $taskRuntime 'usage.mjs') -Value 'offline placeholder; never executed'
$taskCases = @(
    @{name='no_implicit_prompt';has_token=$false;access='';fail_phase='';exit=1;phases='registration';message='No prompt, API request or model call was made'},
    @{name='reuse_preflight';has_token=$true;access=$taskAccess;fail_phase='';exit=0;phases='registration,cached_validate,run,verify';message='Run finished'},
    @{name='retrieve_once';has_token=$true;access='';fail_phase='';exit=0;phases='registration,preflight,run,verify';message='Run finished'},
    @{name='reject_bad_preflight';has_token=$false;access=$taskAccess;fail_phase='cached_validate';exit=1;phases='registration,cached_validate';message='Saved preflight failed verification'},
    @{name='no_silent_retry';has_token=$true;access='';fail_phase='run';exit=1;phases='registration,preflight,run';message='Matrix interrupted'},
    @{name='reject_output_in_repo';has_token=$false;access='';fail_phase='';exit=1;phases='';message='Keep results outside the checkout'}
)
$taskResults = @()
foreach ($taskCase in $taskCases) {
    $taskConfig = Join-Path $taskRoot ($taskCase.name + '.json')
    $taskLog = Join-Path $taskRoot ($taskCase.name + '.jsonl')
    $taskOut = Join-Path $taskRoot ($taskCase.name + '.stdout')
    $taskErr = Join-Path $taskRoot ($taskCase.name + '.stderr')
    $taskOutput = if ($taskCase.name -eq 'reject_output_in_repo') { Join-Path $taskRepo 'invalid-launcher-output' } else { Join-Path $taskRoot $taskCase.name }
    @{repo=$taskRepo;stub=$taskStub;launcher=$taskLauncher;output=$taskOutput;node=$taskNode;runtime=$taskRuntime;access=$taskCase.access;has_token=$taskCase.has_token;fail_phase=$taskCase.fail_phase;log=$taskLog} |
        ConvertTo-Json | Set-Content -LiteralPath $taskConfig -Encoding utf8
    $taskProcess = Start-Process -FilePath (Get-Command pwsh).Source -ArgumentList @('-NoProfile','-File',('"' + $taskDriver + '"'),('"' + $taskConfig + '"')) -WindowStyle Hidden -RedirectStandardOutput $taskOut -RedirectStandardError $taskErr -PassThru
    if (-not $taskProcess.WaitForExit(20000)) {
        $taskProcess.Kill()
        throw ('Test blocked or prompted: ' + $taskCase.name)
    }
    $taskCalls = if (Test-Path -LiteralPath $taskLog) { @(Get-Content -LiteralPath $taskLog | ForEach-Object { $_ | ConvertFrom-Json }) } else { @() }
    $taskPhases = ($taskCalls | ForEach-Object phase) -join ','
    $taskText = (Get-Content -LiteralPath $taskOut -Raw) + (Get-Content -LiteralPath $taskErr -Raw)
    if ($taskProcess.ExitCode -ne $taskCase.exit -or $taskPhases -ne $taskCase.phases -or $taskText -notlike ('*' + $taskCase.message + '*')) {
        throw ('Unexpected control flow: ' + $taskCase.name + '; exit=' + $taskProcess.ExitCode + '; phases=' + $taskPhases + '; output=' + $taskText)
    }
    if (@($taskCalls | Where-Object { $_.phase -in 'run','verify' -and $_.token_present }).Count) {
        throw ('Credential reached model/replay stage: ' + $taskCase.name)
    }
    $taskResults += @{name=$taskCase.name;passed=$true;phases=$taskPhases;exit_code=$taskProcess.ExitCode}
}
@{at=(Get-Date).ToUniversalTime().ToString('o');test_kind='offline launcher control flow with test doubles';githits_requests=0;model_calls=0;cases=$taskResults} |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $taskRoot 'results.json') -Encoding utf8
Write-Output ('Passed ' + $taskResults.Count + ' launcher cases; zero API/model calls.')
