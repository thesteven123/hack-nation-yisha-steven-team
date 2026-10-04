#requires -Version 7.0
# Source development only. No installer, updater or installed service is invoked.
[CmdletBinding()]
param(
    [switch]$ServerOnly,
    [switch]$StopServer,
    [string]$WslDistribution = 'Ubuntu',
    [string]$WslUser = 'agentsdock',
    [string]$ServerPython = '/home/agentsdock/.cache/agentsdock-ai-lab-dev/server-venv/bin/python',
    [string]$ServerRoot = '/home/agentsdock/.cache/agentsdock-idea-lab-dev'
)

$ErrorActionPreference = 'Stop'
if ($ServerOnly -and $StopServer) { throw 'Choose either -ServerOnly or -StopServer.' }
foreach ($argument in @($WslDistribution, $WslUser, $ServerPython, $ServerRoot)) {
    if ([string]::IsNullOrWhiteSpace($argument) -or $argument -match '["\r\n]') {
        throw 'WSL arguments must be nonempty and contain no quotes or line breaks.'
    }
}

$source = $PSScriptRoot
$electron = Join-Path $source 'electron'
$exe = Join-Path $electron 'node_modules\electron\dist\electron.exe'
$profile = Join-Path $env:LOCALAPPDATA 'Programs\AgentsDockIdeaLabData'
$wsl = (Get-Command wsl.exe -ErrorAction Stop).Source

function ConvertTo-LinuxPath([string]$Path) {
    $converted = & $wsl -d $WslDistribution -u $WslUser --exec wslpath -a $Path
    if ($LASTEXITCODE -ne 0 -or !$converted) { throw "Cannot access this source path in WSL: $Path" }
    return ($converted | Out-String).Trim()
}

if (!(Test-Path -LiteralPath (Join-Path $source 'server\scripts\idea_lab_dev.py'))) {
    throw 'Run Start-AILab.ps1 from a complete AgentsDock source checkout.'
}
$linuxSource = ConvertTo-LinuxPath $source
$helper = "$linuxSource/server/scripts/idea_lab_dev.py"
if ($StopServer) {
    & $wsl -d $WslDistribution -u $WslUser --exec $ServerPython $helper stop --root $ServerRoot
    if ($LASTEXITCODE -ne 0) { throw 'The marked development server did not stop. No unrelated process was stopped.' }
    return
}
if (!$ServerOnly -and (!(Test-Path -LiteralPath $exe) -or !(Test-Path -LiteralPath (Join-Path $electron 'out\main\index.js')))) {
    throw 'Source build missing. In electron, prepare dependencies and run pnpm build before launching.'
}
$linuxProfile = ConvertTo-LinuxPath (Join-Path $profile 'idea-lab-dev-profile.json')
$nativePath = "/home/$WslUser/.local/bin:/usr/local/bin:/usr/bin:/bin"
& $wsl -d $WslDistribution -u $WslUser --exec /usr/bin/env "PATH=$nativePath" $ServerPython $helper prepare --root $ServerRoot --client-profile $linuxProfile
if ($LASTEXITCODE -ne 0) { throw 'AI Lab preparation failed. The installed app was not changed.' }

$ready = & $wsl -d $WslDistribution -u $WslUser --exec $ServerPython $helper status --root $ServerRoot 2>$null
if ($LASTEXITCODE -ne 0) {
    $logs = Join-Path $profile 'logs\server-start'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    # Start-Process joins arguments; quote each path/distribution containing spaces.
    $serverArgs = @('-d', ('"' + $WslDistribution + '"'), '-u', ('"' + $WslUser + '"'), '--exec',
        ('"' + $ServerPython + '"'), ('"' + $helper + '"'), 'serve', '--root', ('"' + $ServerRoot + '"'))
    $server = Start-Process -FilePath $wsl -ArgumentList $serverArgs -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs 'server.stdout.log') -RedirectStandardError (Join-Path $logs 'server.stderr.log')
    $connected = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        $ready = & $wsl -d $WslDistribution -u $WslUser --exec $ServerPython $helper status --root $ServerRoot 2>$null
        if ($LASTEXITCODE -eq 0) { $connected = $true; break }
        $server.Refresh()
        if ($server.HasExited) { break }
    }
    if (!$connected) { throw "The isolated server did not become ready. See $logs\server.stderr.log" }
}
if ($ServerOnly) { Write-Host 'Isolated AI Lab server ready at http://127.0.0.1:17851'; return }

# Only a PID whose executable is this checkout's development Electron can match.
$pidFile = Join-Path $profile 'idea-lab-client.pid'
if (Test-Path -LiteralPath $pidFile) {
    $savedPid = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$savedPid)) {
        $prior = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        if ($prior -and $prior.Path -and [string]::Equals($prior.Path, $exe, [StringComparison]::OrdinalIgnoreCase)) {
            Write-Host 'The independent AI Lab window is already open.'; return
        }
    }
}
$start = [System.Diagnostics.ProcessStartInfo]::new($exe)
$start.WorkingDirectory = $electron
$start.UseShellExecute = $false
$start.ArgumentList.Add($electron)
$start.Environment['AGENTSDOCK_USER_DATA'] = $profile
$start.Environment['AGENTSDOCK_IDEA_DEV'] = '1'
$start.Environment.Remove('ELECTRON_RUN_AS_NODE') | Out-Null
$client = [System.Diagnostics.Process]::Start($start)
Set-Content -LiteralPath $pidFile -Value $client.Id -Encoding ascii
Write-Host 'Opened AgentsDock - Idea Lab (Dev). Choose Idea group or Research Lab in the sidebar.'
