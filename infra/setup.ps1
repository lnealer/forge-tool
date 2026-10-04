<#
.SYNOPSIS
    Windows setup and launcher for forge-tool (the PowerShell twin of infra/setup.sh).

.DESCRIPTION
    .\infra\setup.ps1 install            create .venv, install python\requirements.txt, then check
    .\infra\setup.ps1 check              verify the toolchain, AWS access and Windows settings
    .\infra\setup.ps1 config             print the effective configuration and where each value came from
    .\infra\setup.ps1 run                Streamlit in this window (Ctrl+C stops it), also logged to app.log
    .\infra\setup.ps1 start              Streamlit in the background, logging to app.log
    .\infra\setup.ps1 status             pid, uptime, health, connected browsers, run activity
    .\infra\setup.ps1 stop [-Force]      stop it; refuses while a browser is connected or a run is active
    .\infra\setup.ps1 restart [-Force]   stop + start, same guard

    Extra arguments after run/start go to the app:
    .\infra\setup.ps1 run --github_url https://github.com/org/repo.git --upgrade_details "Java 21"

    All configuration comes from the repo-root .env (copy it from the machine that ran
    infra/deploy.sh). Variables already set in the environment win over .env.
    Infrastructure (Terraform, SSM writes) is managed from macOS/Linux with
    infra/deploy.sh; this script only runs the app.

    If scripts are blocked:  powershell -ExecutionPolicy Bypass -File .\infra\setup.ps1 install
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('install', 'check', 'config', 'run', 'start', 'stop', 'restart', 'status', 'help')]
    [string]$Command = 'help',
    [switch]$Force,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArgs = @()
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$OnWindows = ($PSVersionTable.PSEdition -eq 'Desktop') -or ((Get-Variable -Name IsWindows -ErrorAction SilentlyContinue) -and $IsWindows)
$InfraDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $InfraDir
$EnvFile = Join-Path $RepoRoot '.env'
$PythonDir = Join-Path $RepoRoot 'python'
$AppLog = Join-Path $RepoRoot 'app.log'          # stderr: the FORGE_TOOL logger (tool calls, git, Maven)
$AppOut = Join-Path $RepoRoot 'app.stdout.log'   # stdout: Streamlit's own banner
$script:ShellKeys = @{}

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Warn([string]$Message) { Write-Host "[warn] $Message" -ForegroundColor Yellow }
function Write-Err([string]$Message) { Write-Host "[error] $Message" -ForegroundColor Red }

# --- .env --------------------------------------------------------------------
# Same rule as infra/lib/env.sh and python-dotenv(override=False): a variable that is
# already set (and not empty) wins over the file.
function Import-DotEnv {
    foreach ($key in [Environment]::GetEnvironmentVariables().Keys) { $script:ShellKeys[[string]$key] = $true }
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        $example = Join-Path $RepoRoot '.env.example'
        if (Test-Path -LiteralPath $example) {
            Write-Step 'Creating .env from .env.example (copy the real one from the deploy machine)'
            Copy-Item -LiteralPath $example -Destination $EnvFile
        } else {
            throw '.env and .env.example are both missing.'
        }
    }
    foreach ($line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
        $text = $line.Trim()
        if (-not $text -or $text.StartsWith('#') -or -not $text.Contains('=')) { continue }
        $eq = $text.IndexOf('=')
        $key = $text.Substring(0, $eq).Trim()
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        $value = $text.Substring($eq + 1)
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($key))) {
            [Environment]::SetEnvironmentVariable($key, $value, 'Process')
        }
    }
}

function Get-Setting([string]$Name, [string]$Default = '') {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

function Get-Source([string]$Name) {
    if ($script:ShellKeys.ContainsKey($Name)) { return 'shell' }
    if ((Test-Path -LiteralPath $EnvFile) -and (Select-String -LiteralPath $EnvFile -Pattern "^\s*$Name=" -Quiet)) { return '.env' }
    return 'default'
}

function Resolve-RepoPath([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) { return $Path }
    return (Join-Path $RepoRoot ($Path -replace '^[.][\\/]', ''))
}

Import-DotEnv

$VenvDir = Resolve-RepoPath (Get-Setting 'VENV_DIR' '.venv')
if ($OnWindows) { $VenvBin = Join-Path $VenvDir 'Scripts' } else { $VenvBin = Join-Path $VenvDir 'bin' }
$VenvPython = Join-Path $VenvBin ($(if ($OnWindows) { 'python.exe' } else { 'python' }))
$Port = [int](Get-Setting 'STREAMLIT_SERVER_PORT' '8501')
$Address = Get-Setting 'STREAMLIT_SERVER_ADDRESS' 'localhost'
$Headless = Get-Setting 'STREAMLIT_SERVER_HEADLESS' 'false'
$Entrypoint = Resolve-RepoPath (Get-Setting 'APP_ENTRYPOINT' 'python/chat.py')
$AwsRegion = Get-Setting 'AWS_REGION' 'us-east-1'
$RunActiveMinutes = [int](Get-Setting 'RUN_ACTIVE_MINUTES' '5')
$Prefix = Get-Setting 'PARAMETER_STORE_PREFIX' 'forge_tool_'
$ServerUrl = "http://${Address}:$Port"

# --- toolchain -----------------------------------------------------------------
function Get-BasePython {
    # PYTHON_BIN from a macOS .env is usually "python3", which on Windows can be the
    # Microsoft Store stub; prefer the py launcher there.
    $candidates = @()
    $configured = Get-Setting 'PYTHON_BIN' ''
    if ($configured -and -not ($OnWindows -and $configured -eq 'python3')) { $candidates += , @($configured) }
    if ($OnWindows) { $candidates += , @('py', '-3'); $candidates += , @('python') } else { $candidates += , @('python3'); $candidates += , @('python') }
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $rest = @($candidate | Select-Object -Skip 1)
        try {
            $version = & $exe @rest -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>$null
        } catch { continue }
        if ($version -and [version]$version -ge [version]'3.11') { return , $candidate }
    }
    throw 'Python 3.11+ not found. Install it from python.org (tick "Add python.exe to PATH") and try again.'
}

function Get-JavaMajor([string]$JavaHome) {
    $release = Join-Path $JavaHome 'release'
    if (-not (Test-Path -LiteralPath $release)) { return '' }
    $m = Select-String -LiteralPath $release -Pattern '^JAVA_VERSION="(\d+)' | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value }
    return ''
}

function Resolve-JavaHome {
    # JAVA_VERSION is a pin and wins over an inherited JAVA_HOME (same rule as setup.sh).
    $want = Get-Setting 'JAVA_VERSION' ''
    $javaExe = $(if ($OnWindows) { 'java.exe' } else { 'java' })
    if ($want) {
        $roots = @()
        if ($OnWindows) {
            foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA 'Programs'))) {
                if (-not $base) { continue }
                foreach ($vendor in @('Eclipse Adoptium', 'Microsoft', 'Zulu', 'Java', 'Amazon Corretto', 'BellSoft', 'Semeru')) {
                    $roots += (Join-Path $base $vendor)
                }
            }
        }
        foreach ($root in $roots) {
            if (-not (Test-Path -LiteralPath $root)) { continue }
            foreach ($dir in Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending) {
                if ((Test-Path -LiteralPath (Join-Path $dir.FullName "bin\$javaExe")) -and (Get-JavaMajor $dir.FullName) -eq $want) {
                    return @($dir.FullName, "JAVA_VERSION=$want")
                }
            }
        }
        if (-not $OnWindows -and (Test-Path '/usr/libexec/java_home')) {
            $found = & /usr/libexec/java_home -v $want 2>$null
            if ($found -and (Get-JavaMajor $found) -eq $want) { return @($found, "JAVA_VERSION=$want") }
        }
        Write-Warn "No JDK $want found in the usual install folders; falling back to JAVA_HOME / PATH."
    }
    $inherited = Get-Setting 'JAVA_HOME' ''
    if ($inherited -and (Test-Path -LiteralPath $inherited)) { return @($inherited, 'JAVA_HOME') }
    $onPath = Get-Command $javaExe -ErrorAction SilentlyContinue
    if ($onPath) { return @((Split-Path -Parent (Split-Path -Parent $onPath.Source)), 'java on PATH') }
    return @('', 'not found')
}

$script:JavaHomeSource = 'unset'
function Set-Paths {
    $sep = [IO.Path]::PathSeparator
    $resolved = Resolve-JavaHome
    if ($resolved[0]) {
        $env:JAVA_HOME = $resolved[0]
        $env:PATH = (Join-Path $env:JAVA_HOME 'bin') + $sep + $env:PATH
    }
    $script:JavaHomeSource = $resolved[1]
    if (Test-Path -LiteralPath $VenvBin) { $env:PATH = $VenvBin + $sep + $env:PATH }
    if (-not ($env:PYTHONPATH -and $env:PYTHONPATH.Split($sep) -contains $PythonDir)) {
        $env:PYTHONPATH = $(if ($env:PYTHONPATH) { $PythonDir + $sep + $env:PYTHONPATH } else { $PythonDir })
    }
    $env:PYTHONUTF8 = '1'          # UTF-8 for files and subprocess output, not the ANSI code page
    $env:PYTHONUNBUFFERED = '1'
    if (-not $env:MAVEN_OPTS) { $env:MAVEN_OPTS = '-Xmx2g' }
    if (-not $env:MAVEN_ARGS) { $env:MAVEN_ARGS = '-B -ntp' }
    $env:AWS_REGION = $AwsRegion
    if (-not $env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION = $AwsRegion }
    $env:FORGE_TOOL_ROOT = $RepoRoot
    $workRoot = Get-Setting 'WORK_ROOT' ''
    if ($workRoot -and -not (Test-Path -LiteralPath $workRoot)) { New-Item -ItemType Directory -Path $workRoot -Force | Out-Null }
}

# --- configuration summary -------------------------------------------------------
function Show-Kv([string]$Label, [string]$Value, [string]$Note = '') {
    $line = '  {0,-24} {1}' -f $Label, $Value
    if ($Note) { $line += "  ($Note)" }
    Write-Host $line
}
function Show-Env([string]$Name, [string]$WhenEmpty = '<unset>') {
    $value = Get-Setting $Name ''
    if ($value) { Show-Kv $Name $value (Get-Source $Name) } else { Show-Kv $Name $WhenEmpty }
}
function Show-Secret([string]$Name) {
    if (Get-Setting $Name '') { Show-Kv $Name 'set' (Get-Source $Name) } else { Show-Kv $Name 'unset' }
}

function Show-Config {
    Set-Paths
    Write-Step 'Paths for this process'
    Show-Kv 'JAVA_HOME' $(if ($env:JAVA_HOME) { $env:JAVA_HOME } else { '<unset>' }) $script:JavaHomeSource
    if ($env:JAVA_HOME) {
        $java = Join-Path (Join-Path $env:JAVA_HOME 'bin') $(if ($OnWindows) { 'java.exe' } else { 'java' })
        if (Test-Path -LiteralPath $java) {
            $first = (& $java -version 2>&1 | Select-Object -First 1)
            Show-Kv 'java' ([string]$first)
        }
    }
    Show-Kv 'PYTHONPATH' $env:PYTHONPATH
    Show-Kv 'venv' $VenvDir $(if (Test-Path -LiteralPath $VenvPython) { 'ready' } else { 'missing - run: .\infra\setup.ps1 install' })
    Show-Kv 'MAVEN_ARGS' $env:MAVEN_ARGS
    Show-Env 'WORK_ROOT' '<system temp>'
    Show-Kv 'AWS_REGION' $AwsRegion (Get-Source 'AWS_REGION')
    Write-Step 'AWS credentials'
    Show-Env 'AWS_PROFILE' '<none - default credential chain>'
    Show-Secret 'AWS_ACCESS_KEY_ID'
    Show-Secret 'AWS_SECRET_ACCESS_KEY'
    Show-Secret 'AWS_SESSION_TOKEN'
    Write-Step 'Models'
    Show-Env 'BEDROCK_MODEL_ID' '<app default>'
    Show-Env 'REVIEWER_MODEL_ID' '<app default>'
    Write-Step 'Knowledge base and guardrails'
    Show-Env 'KNOWLEDGE_BASE_ID' "<resolved from SSM ${Prefix}knowledge_base_id>"
    Show-Env 'GUARDRAIL_ID' "<resolved from SSM ${Prefix}guardrail_id>"
    Show-Env 'PII_SCAN_GUARDRAIL_ID' "<resolved from SSM ${Prefix}pii_scan_guardrail_id>"
    Write-Step 'Git and run defaults'
    Show-Env 'GIT_SOURCE_BRANCH' '<repo default branch>'
    Show-Env 'GIT_BASE_BRANCH' '<app default: main>'
    Show-Env 'DEFAULT_GITHUB_URL' '<none - the app will prompt>'
    Show-Env 'DEFAULT_UPGRADE_DETAILS' '<none - the app will prompt>'
    Show-Kv 'GitHub PAT' "SSM ${Prefix}api_key" 'SecureString, read into memory at startup'
    Write-Step 'Agent'
    Show-Env 'AGENT_RECURSION_LIMIT' '<app default: 400>'
    Show-Env 'PR_GATE' '<app default: block>'
    Write-Step 'Server'
    Show-Kv 'entrypoint' $Entrypoint
    Show-Kv 'url' $ServerUrl
    Show-Kv 'log' $AppLog
    Show-Kv '.env' $EnvFile
}

# --- install / check -------------------------------------------------------------
function Invoke-Install {
    $base = Get-BasePython
    $exe = $base[0]; $rest = @($base | Select-Object -Skip 1)
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Step "Creating virtualenv at $VenvDir"
        & $exe @rest -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtualenv.' }
    } else {
        Write-Step "Reusing virtualenv at $VenvDir"
    }
    Set-Paths
    Write-Step 'Upgrading pip'
    & $VenvPython -m pip install --upgrade pip --quiet
    Write-Step 'Installing python\requirements.txt'
    & $VenvPython -m pip install -r (Join-Path $PythonDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }
    Write-Step 'Install complete.'
    $null = Invoke-Check
    Show-Config
}

function Invoke-Check {
    Set-Paths
    $problems = 0
    Write-Step 'Toolchain'
    $venvLabel = $(if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { $null })
    if ($venvLabel) { Write-Host ('  ok      {0,-9} {1}' -f 'python', $venvLabel) } else { Write-Host '  MISSING python   (run: .\infra\setup.ps1 install)'; $problems++ }
    foreach ($tool in @('mvn', 'java', 'git', 'aws')) {
        $cmd = Get-Command $tool -ErrorAction SilentlyContinue
        if ($cmd) { Write-Host ('  ok      {0,-9} {1}' -f $tool, $cmd.Source) } else { Write-Host ('  MISSING {0,-9} needed to run code upgrades' -f $tool); $problems++ }
    }
    Write-Host ('  JAVA_HOME = {0} (from {1})' -f $(if ($env:JAVA_HOME) { $env:JAVA_HOME } else { '<unset>' }), $script:JavaHomeSource)

    if ($OnWindows) {
        Write-Step 'Windows'
        $long = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction SilentlyContinue)
        if ($long -and $long.LongPathsEnabled -eq 1) { Write-Host '  ok      long paths enabled' } else {
            Write-Host '  WARN    long paths disabled: deep Maven paths can fail. As admin run:'
            Write-Host '          New-ItemProperty -Path HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force'
        }
        $gitLong = (& git config --global --get core.longpaths 2>$null)
        if ($gitLong -eq 'true') { Write-Host '  ok      git core.longpaths=true' } else { Write-Host '  WARN    git core.longpaths not set globally (forge sets it on its clones; for your own: git config --global core.longpaths true)' }
        if (-not (Get-Setting 'WORK_ROOT' '')) { Write-Host '  WARN    WORK_ROOT is empty: runs go under %TEMP%; a short root such as C:\forge-work avoids path-length issues' }
    }

    Write-Step 'AWS'
    $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $arn = (& aws sts get-caller-identity --query Arn --output text 2>$null)
    if ($LASTEXITCODE -eq 0 -and $arn) { Write-Host "  ok      credentials  $arn"; Write-Host "  region  $AwsRegion" } else {
        Write-Host '  MISSING valid AWS credentials (aws configure --profile <name>, then AWS_PROFILE=<name> in .env)'; $problems++
    }
    foreach ($item in @(@('KNOWLEDGE_BASE_ID', 'knowledge_base_id'), @('GUARDRAIL_ID', 'guardrail_id'), @('PII_SCAN_GUARDRAIL_ID', 'pii_scan_guardrail_id'))) {
        $value = Get-Setting $item[0] ''
        if ($value) { Write-Host ('  ok      {0}={1}' -f $item[0], $value) } else {
            $name = (& aws ssm get-parameter --name ($Prefix + $item[1]) --query Parameter.Name --output text 2>$null)
            if ($LASTEXITCODE -eq 0 -and $name) { Write-Host "  ok      $($item[0]) resolved from SSM $name" } else { Write-Host "  none    $($item[0]) (optional; created by infra/deploy.sh on the deploy machine)" }
        }
    }
    # The PAT's presence only; the value is never fetched here.
    $pat = (& aws ssm describe-parameters --parameter-filters "Key=Name,Values=${Prefix}api_key" --query 'Parameters[0].Name' --output text 2>$null)
    if ($LASTEXITCODE -eq 0 -and $pat -and $pat -ne 'None') { Write-Host "  ok      $pat (GitHub PAT, SecureString)" } else { Write-Host "  MISSING ${Prefix}api_key in SSM (store it from the deploy machine: ./infra/put_ssm_parameters.sh)"; $problems++ }
    $ErrorActionPreference = $previous

    if ($problems -gt 0) { Write-Warn "$problems item(s) need attention." } else { Write-Step 'Everything checks out.' }
    return $problems
}

# --- server lifecycle --------------------------------------------------------------
function Get-StreamlitArgs {
    $extra = @($AppArgs)
    if ($extra.Count -eq 0) {
        $url = Get-Setting 'DEFAULT_GITHUB_URL' ''
        $goal = Get-Setting 'DEFAULT_UPGRADE_DETAILS' ''
        if ($url) { $extra += @('--github_url', $url) }
        if ($goal) { $extra += @('--upgrade_details', $goal) }
    }
    $streamlitArgs = @('-m', 'streamlit', 'run', $Entrypoint, '--server.port', "$Port", '--server.address', $Address, '--server.headless', $Headless)
    if ($extra.Count -gt 0) { $streamlitArgs += @('--') + $extra }
    return , $streamlitArgs
}

function Get-ServerPid {
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c) { return [int]$c.OwningProcess }
        return $null
    }
    $pidText = (& lsof -nP "-iTCP:$Port" -sTCP:LISTEN -t 2>$null | Select-Object -First 1)
    if ($pidText) { return [int]$pidText }
    return $null
}

function Get-BrowserSessions([int]$ServerPid) {
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        return @(Get-NetTCPConnection -RemotePort $Port -State Established -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -ne $ServerPid }).Count
    }
    $lines = @(& lsof -nP "-iTCP:$Port" -sTCP:ESTABLISHED 2>$null | Select-Object -Skip 1)
    return @($lines | Where-Object { ($_ -split '\s+')[1] -ne "$ServerPid" }).Count
}

function Test-Health {
    try { return ((Invoke-WebRequest -Uri "$ServerUrl/_stcore/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) } catch { return $false }
}

function Get-RunActivity {
    if (-not (Test-Path -LiteralPath $AppLog)) { return $null }
    $age = [int]((Get-Date) - (Get-Item -LiteralPath $AppLog).LastWriteTime).TotalSeconds
    $last = Select-String -LiteralPath $AppLog -Pattern '→ |← |Running: |Cloning repo|Secret guardrail|Reviewer' -Encoding UTF8 | Select-Object -Last 1
    $line = $(if ($last) { ($last.Line -replace '^INFO:FORGE_TOOL:', '') } else { '' })
    if ($line.Length -gt 110) { $line = $line.Substring(0, 110) }
    return [pscustomobject]@{ AgeSeconds = $age; Active = ($age -lt $RunActiveMinutes * 60); Line = $line }
}

function Move-LogAside([string]$Path) {
    if (Test-Path -LiteralPath $Path) { Move-Item -LiteralPath $Path -Destination "$Path.1" -Force }
}

function Invoke-Run {
    if (-not (Test-Path -LiteralPath $VenvPython)) { throw 'The virtualenv is missing. Run: .\infra\setup.ps1 install' }
    Set-Paths
    Show-Config
    Move-LogAside $AppLog
    Write-Step "Starting Streamlit on $ServerUrl (also logging to $AppLog) - Ctrl+C stops it"
    $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        & $VenvPython @(Get-StreamlitArgs) 2>&1 | ForEach-Object {
            $text = "$_"
            Write-Host $text
            Add-Content -LiteralPath $AppLog -Value $text -Encoding UTF8
        }
    } finally { $ErrorActionPreference = $previous }
}

function Invoke-Start {
    $existing = Get-ServerPid
    if ($existing) { throw "Port $Port is already in use by pid $existing. Use: .\infra\setup.ps1 restart" }
    if (-not (Test-Path -LiteralPath $VenvPython)) { throw 'The virtualenv is missing. Run: .\infra\setup.ps1 install' }
    Set-Paths
    Move-LogAside $AppLog
    Move-LogAside $AppOut
    Write-Step "Starting Streamlit in the background on $ServerUrl (log: $AppLog)"
    $startArgs = @{ FilePath = $VenvPython; ArgumentList = (Get-StreamlitArgs); RedirectStandardError = $AppLog; RedirectStandardOutput = $AppOut; PassThru = $true; WorkingDirectory = $RepoRoot }
    if ($OnWindows) { $startArgs['WindowStyle'] = 'Hidden' }
    $null = Start-Process @startArgs
    for ($i = 0; $i -lt 60; $i++) { if (Test-Health) { break }; Start-Sleep -Seconds 1 }
    if (Test-Health) {
        Write-Step "Running: pid $(Get-ServerPid) - $ServerUrl"
        Show-Config
    } else {
        Write-Err 'Streamlit did not become healthy within 60s. Last log lines:'
        if (Test-Path -LiteralPath $AppLog) { Get-Content -LiteralPath $AppLog -Tail 20 }
        exit 1
    }
}

function Invoke-Stop {
    $serverPid = Get-ServerPid
    if (-not $serverPid) { Write-Step "Nothing is listening on port $Port; nothing to stop."; return }
    $reasons = @()
    $sessions = Get-BrowserSessions $serverPid
    if ($sessions -gt 0) { $reasons += "$sessions browser session(s) connected" }
    $activity = Get-RunActivity
    if ($activity -and $activity.Active) {
        $what = $(if ($activity.Line) { $activity.Line } else { 'recent output' })
        $reasons += "a run looks active (app.log changed $($activity.AgeSeconds)s ago: $what)"
    }
    if ($reasons.Count -gt 0 -and -not $Force) {
        Write-Err "Refusing to stop pid ${serverPid}: $($reasons -join '; ')."
        Write-Err 'Stopping kills any upgrade in progress and resets the chat. Re-run with -Force to stop anyway.'
        exit 1
    }
    if ($reasons.Count -gt 0) { Write-Warn "Stopping pid $serverPid anyway (-Force): $($reasons -join '; ')" }
    Write-Step "Stopping Streamlit (pid $serverPid)"
    Stop-Process -Id $serverPid -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 10; $i++) { if (-not (Get-ServerPid)) { break }; Start-Sleep -Seconds 1 }
    if (Get-ServerPid) { Write-Err "Port $Port is still busy (pid $(Get-ServerPid))."; exit 1 }
    Write-Step 'Stopped.'
}

function Invoke-Status {
    $serverPid = Get-ServerPid
    if (-not $serverPid) { Write-Step "Streamlit: not running (nothing listens on port $Port)"; exit 1 }
    $proc = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
    $uptime = $(if ($proc -and $proc.StartTime) { ((Get-Date) - $proc.StartTime).ToString('hh\:mm\:ss') } else { '?' })
    $health = $(if (Test-Health) { 'ok' } else { 'DOWN' })
    Write-Step "Streamlit: pid $serverPid, up $uptime, health $health, $ServerUrl"
    Write-Host ('  browser sessions : {0}' -f (Get-BrowserSessions $serverPid))
    $activity = Get-RunActivity
    if ($activity -and $activity.Active) { Write-Host ('  activity         : app.log changed {0}s ago - {1}' -f $activity.AgeSeconds, $(if ($activity.Line) { $activity.Line } else { '(no tool activity logged)' })) }
    else { Write-Host ('  activity         : idle (no log output for >{0} min)' -f $RunActiveMinutes) }
    Write-Host ('  log              : {0}' -f $AppLog)
}

switch ($Command) {
    'install' { Invoke-Install }
    'check' { $problems = Invoke-Check; Show-Config; if ($problems -gt 0) { exit 1 } }
    'config' { Show-Config }
    'run' { Invoke-Run }
    'start' { Invoke-Start }
    'stop' { Invoke-Stop }
    'restart' { Invoke-Stop; Invoke-Start }
    'status' { Invoke-Status }
    default { Get-Help $PSCommandPath -Detailed | Out-String | Write-Host }
}
