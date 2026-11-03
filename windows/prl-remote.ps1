#requires -Version 5.1
<#
.SYNOPSIS
  ESI Workbench - Windows-side reader/controller for a remote Pearl (PRL) miner.

.DESCRIPTION
  Talks to a Linux GPU machine over SSH (the same channel used for deployment)
  and turns the remote telemetry snapshot into a host-side document with an
  explicit freshness/status verdict.

  This script never stores or sends a wallet private key, mnemonic, SSH private
  key content or platform API key. The SSH key is referenced by path only and
  handed to the OpenSSH client; the public PRL payout address lives only in the
  remote config file.

  Statuses produced here (see docs/TELEMETRY-SCHEMA.md):
    ok                 fresh sample, miner running, local API answering
    stale              the newest data is older than telemetry.max_age_s
    miner_exited       the supervised miner process is gone
    miner_not_running  no miner has been started on that instance
    api_unreachable    miner process state is unknown/unverified or its API is down
    identity_unverified the pid on the remote box cannot be proven to be ours
    unreachable        SSH/transport failed, no data at all
    protocol_error     the remote answered with something unusable

.PARAMETER Action
  status | watch | deploy | check | start | stop | logs | prepare | devices

.EXAMPLE
  .\prl-remote.ps1 -Action deploy
  .\prl-remote.ps1 -Action status
  .\prl-remote.ps1 -Action watch -IntervalSec 10
  .\prl-remote.ps1 -Action stop

.NOTES
  Exit codes: 0 ok, 2 config, 3 stale, 4 miner_exited, 5 api_unreachable,
  6 unreachable, 7 protocol_error, 8 identity_unverified, 9 miner_not_running.
#>
[CmdletBinding()]
param(
    [ValidateSet('status', 'watch', 'deploy', 'check', 'start', 'stop', 'logs', 'prepare', 'devices')]
    [string]$Action = 'status',
    [string]$Config,
    [string]$ConfigFile,
    [int]$IntervalSec = 10,
    [int]$Iterations = 0,
    [int]$Lines = 60,
    [ValidateSet('miner', 'collector', 'supervisor', 'prl')]
    [string]$LogSource = 'miner',
    [int]$MaxAgeSec = 0,
    [switch]$Json,
    [switch]$ShowSsh,
    [switch]$LibraryOnly
)

$ErrorActionPreference = 'Stop'

$script:Root = Split-Path -Parent $PSScriptRoot
$script:StateDir = Join-Path $script:Root 'state'
$script:ExitCode = 0

function Write-Info {
    param([string]$Message)
    if (-not $Json) { Write-Host $Message }
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Write-JsonFileAtomic {
    param([string]$Path, $Object)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $tmp = "$Path.tmp"
    $json = $Object | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force }
    Move-Item -LiteralPath $tmp -Destination $Path
}

function Get-HostConfig {
    param([string]$Path)
    if (-not $Path) {
        $Path = Join-Path $PSScriptRoot 'host.local.json'
    }
    $cfg = Read-JsonFile $Path
    if ($null -eq $cfg) { throw "host config not found or invalid JSON: $Path" }
    foreach ($field in @('host', 'user')) {
        if (-not $cfg.ssh.$field) { throw "host config is missing ssh.$field ($Path)" }
    }
    if (-not $cfg.remote.home) { throw "host config is missing remote.home ($Path)" }
    if ($cfg.remote.home -notmatch '^/[-A-Za-z0-9._/]+$') { throw "remote.home must be an absolute Linux path: $($cfg.remote.home)" }
    if ($cfg.remote.home -match '\.\.') { throw "remote.home must not contain '..'" }
    return $cfg
}

function Protect-RemoteArg {
    param([string]$Value)
    return "'" + ($Value -replace "'", "'\''") + "'"
}

function Join-RemoteCommand {
    param([string[]]$Argv)
    return (($Argv | ForEach-Object { Protect-RemoteArg $_ }) -join ' ')
}

function Quote-WinArg {
    param([string]$Value)
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $backslashes = 0
    foreach ($ch in $Value.ToCharArray()) {
        if ($ch -eq '\') { $backslashes++; continue }
        if ($ch -eq '"') {
            [void]$sb.Append(('\' * ($backslashes * 2 + 1)) + '"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) { [void]$sb.Append('\' * $backslashes); $backslashes = 0 }
        [void]$sb.Append($ch)
    }
    if ($backslashes -gt 0) { [void]$sb.Append('\' * ($backslashes * 2)) }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Get-SshArgumentList {
    param($Config, [string]$RemoteCommand)
    $sshArgs = New-Object System.Collections.Generic.List[string]
    $sshArgs.Add('-o'); $sshArgs.Add('BatchMode=yes')
    if ($Config.ssh.strict_host_key_checking -eq $false) {
        # Explicitly configured by the operator; still reported in the output.
        $sshArgs.Add('-o'); $sshArgs.Add('StrictHostKeyChecking=no')
    } else {
        $sshArgs.Add('-o'); $sshArgs.Add('StrictHostKeyChecking=yes')
    }
    if ($Config.ssh.known_hosts) {
        $sshArgs.Add('-o'); $sshArgs.Add("UserKnownHostsFile=$($Config.ssh.known_hosts)")
    }
    $connectTimeout = 10
    if ($Config.ssh.connect_timeout_s) { $connectTimeout = [int]$Config.ssh.connect_timeout_s }
    $sshArgs.Add('-o'); $sshArgs.Add("ConnectTimeout=$connectTimeout")
    $sshArgs.Add('-o'); $sshArgs.Add('ServerAliveInterval=5')
    $sshArgs.Add('-o'); $sshArgs.Add('ServerAliveCountMax=2')
    $port = 22
    if ($Config.ssh.port) { $port = [int]$Config.ssh.port }
    $sshArgs.Add('-p'); $sshArgs.Add([string]$port)
    if ($Config.ssh.key_path) {
        $sshArgs.Add('-i'); $sshArgs.Add([string]$Config.ssh.key_path)
        $sshArgs.Add('-o'); $sshArgs.Add('IdentitiesOnly=yes')
    }
    $sshArgs.Add("$($Config.ssh.user)@$($Config.ssh.host)")
    $sshArgs.Add($RemoteCommand)
    return $sshArgs.ToArray()
}

function Invoke-Native {
    param([string]$Exe, [string[]]$ArgumentList, [int]$TimeoutMs = 60000)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $psi.Arguments = (($ArgumentList | ForEach-Object { Quote-WinArg $_ }) -join ' ')
    $start = Get-Date
    $proc = [System.Diagnostics.Process]::Start($psi)
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($TimeoutMs)) {
        try { $proc.Kill() } catch { }
        return [pscustomobject]@{
            code = -1; out = ''; err = "timed out after ${TimeoutMs} ms"; duration_ms = [int]((Get-Date) - $start).TotalMilliseconds
        }
    }
    return [pscustomobject]@{
        code        = $proc.ExitCode
        out         = $outTask.Result
        err         = $errTask.Result
        duration_ms = [int]((Get-Date) - $start).TotalMilliseconds
    }
}

function Invoke-RemoteJson {
    param($Config, [string[]]$RemoteArgv, [int]$TimeoutMs = 60000)
    $remoteCommand = Join-RemoteCommand $RemoteArgv
    if ($ShowSsh) { Write-Host ("ssh " + ($remoteCommand)) -ForegroundColor DarkGray }
    $sshArgs = Get-SshArgumentList -Config $Config -RemoteCommand $remoteCommand
    $result = Invoke-Native -Exe 'ssh' -ArgumentList $sshArgs -TimeoutMs $TimeoutMs
    $doc = $null
    $parseError = $null
    if ($result.code -eq 0 -and $result.out.Trim()) {
        try {
            $doc = $result.out | ConvertFrom-Json
        } catch {
            $parseError = "remote output is not JSON: $($_.Exception.Message)"
        }
    }
    $errText = $null
    if ($result.code -ne 0) { $errText = ($result.err.Trim() -replace '\s+', ' ') }
    elseif ($parseError) { $errText = $parseError }
    return [pscustomobject]@{
        ok          = [bool]($result.code -eq 0 -and $null -ne $doc)
        code        = $result.code
        json        = $doc
        stdout      = $result.out
        stderr      = $result.err
        error       = $errText
        duration_ms = $result.duration_ms
    }
}

function Get-RemoteBaseArgv {
    param($Config)
    $python = 'python3'
    if ($Config.remote.python) { $python = [string]$Config.remote.python }
    $remoteHome = [string]$Config.remote.home
    return @($python, "$remoteHome/scripts/prl_miner.py", '--home', $remoteHome)
}

function Convert-RemoteSnapshot {
    <#
      Pure mapping: remote telemetry snapshot -> host document.
      No I/O, so it is unit-testable offline.
    #>
    param(
        $Snapshot,
        [datetime]$FetchedAt,
        [int]$MaxAgeS = 45,
        [bool]$FetchOk = $true,
        [string]$FetchError = $null,
        [int]$FetchDurationMs = 0,
        [string]$Transport = 'ssh',
        [string]$TransportSecurity = 'strict-host-key'
    )
    $doc = [ordered]@{
        schema             = 'esi.pearl.host.telemetry/1'
        fetched_at         = $FetchedAt.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        transport          = [ordered]@{
            kind             = $Transport
            security         = $TransportSecurity
            ok               = $FetchOk
            error            = $FetchError
            duration_ms      = $FetchDurationMs
        }
        status             = 'unreachable'
        status_reason      = ''
        fresh              = $false
        age_s              = $null
        max_age_s          = $MaxAgeS
        clock_skew_s       = $null
        clock_skew_suspect = $false
        remote             = $Snapshot
    }

    if (-not $FetchOk) {
        $doc.status = 'unreachable'
        $doc.status_reason = "transport failed: $FetchError"
        return [pscustomobject]$doc
    }
    if ($null -eq $Snapshot) {
        $doc.status = 'protocol_error'
        $doc.status_reason = 'remote returned no parseable telemetry JSON'
        return [pscustomobject]$doc
    }
    $sampled = $null
    try {
        $sampled = [datetime]::Parse($Snapshot.sampled_at, [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor [System.Globalization.DateTimeStyles]::AssumeUniversal)
    } catch {
        $doc.status = 'protocol_error'
        $doc.status_reason = "remote snapshot has an unusable sampled_at: $($Snapshot.sampled_at)"
        return [pscustomobject]$doc
    }

    $mid = $FetchedAt.AddMilliseconds(-1 * ($FetchDurationMs / 2))
    $skew = [math]::Round(($sampled.ToUniversalTime() - $mid.ToUniversalTime()).TotalSeconds, 1)
    $age = [math]::Round(($FetchedAt.ToUniversalTime() - $sampled.ToUniversalTime()).TotalSeconds, 1)
    if ($age -lt 0) { $age = 0 }
    $doc.age_s = $age
    $doc.clock_skew_s = $skew
    $doc.clock_skew_suspect = [bool]([math]::Abs($skew) -gt 120)

    $processState = [string]$Snapshot.miner.process_state
    $apiStatus = [string]$Snapshot.api.status
    $identity = [string]$Snapshot.miner.identity

    if ($age -gt $MaxAgeS) {
        $doc.status = 'stale'
        $doc.status_reason = ("newest sample is {0:N0}s old (limit {1}s); last known miner state: {2}" -f $age, $MaxAgeS, $processState)
        return [pscustomobject]$doc
    }
    $doc.fresh = $true

    if ($processState -eq 'exited') {
        $doc.status = 'miner_exited'
        $code = $Snapshot.miner.exit.code
        $doc.status_reason = if ($null -ne $code) { "miner exited with code $code" } else { 'miner process is gone' }
    } elseif ($processState -eq 'not_started') {
        $doc.status = 'miner_not_running'
        $doc.status_reason = 'no miner has been started on this instance'
    } elseif ($apiStatus -ne 'ok') {
        $doc.status = 'api_unreachable'
        $doc.status_reason = "miner process state '$processState' but its local API is '$apiStatus'"
    } elseif ($identity -ne 'verified') {
        $doc.status = 'identity_unverified'
        $doc.status_reason = "pid cannot be proven to belong to this controller (identity=$identity)"
    } else {
        $doc.status = 'ok'
        $doc.status_reason = 'fresh sample, verified miner process, local API answering'
    }
    return [pscustomobject]$doc
}

function Get-StatusExitCode {
    param([string]$Status)
    switch ($Status) {
        'ok' { return 0 }
        'config' { return 2 }
        'stale' { return 3 }
        'miner_exited' { return 4 }
        'api_unreachable' { return 5 }
        'unreachable' { return 6 }
        'protocol_error' { return 7 }
        'identity_unverified' { return 8 }
        'miner_not_running' { return 9 }
        default { return 7 }
    }
}

function Format-Hashrate {
    param($Hs)
    if ($null -eq $Hs) { return 'n/a' }
    $v = [double]$Hs
    if ([math]::Abs($v) -ge 1e12) { return ('{0:N2} TH/s' -f ($v / 1e12)) }
    if ([math]::Abs($v) -ge 1e9) { return ('{0:N2} GH/s' -f ($v / 1e9)) }
    if ([math]::Abs($v) -ge 1e6) { return ('{0:N2} MH/s' -f ($v / 1e6)) }
    return ('{0:N0} H/s' -f $v)
}

function Write-HostDocument {
    param($Doc, [switch]$Stale)
    $remote = $Doc.remote
    $color = switch ($Doc.status) {
        'ok' { 'Green' }
        'stale' { 'Yellow' }
        'miner_exited' { 'Red' }
        'unreachable' { 'Red' }
        'protocol_error' { 'Red' }
        default { 'Yellow' }
    }
    Write-Host ("Pearl remote status: {0}  ({1})" -f $Doc.status.ToUpper(), $Doc.status_reason) -ForegroundColor $color
    Write-Host ("  fetched    {0}  age {1}s  transport {2}/{3} {4}ms" -f $Doc.fetched_at, $(if ($null -ne $Doc.age_s) { $Doc.age_s } else { '?' }), $Doc.transport.kind, $Doc.transport.security, $Doc.transport.duration_ms)
    if ($Doc.clock_skew_suspect) {
        Write-Host ("  warning    remote clock differs by {0}s; age may be wrong" -f $Doc.clock_skew_s) -ForegroundColor Yellow
    }
    if ($null -eq $remote) {
        Write-Host '  no remote data (never render cached values as live)' -ForegroundColor Yellow
        return
    }
    $prefix = if ($Doc.fresh) { '' } else { 'last known: ' }
    Write-Host ("  machine    {0}  {1}  {2}" -f $remote.instance.hostname, $remote.instance.os, $remote.instance.machine_id)
    Write-Host ("  miner      {0} {1}  state={2} identity={3}" -f $remote.miner.name, $remote.miner.version, $remote.miner.process_state, $remote.miner.identity)
    if ($null -ne $remote.miner.uptime_s) { Write-Host ("  uptime     {0:N0}s" -f [double]$remote.miner.uptime_s) }
    Write-Host ("  pool       {0}  connected={1}  devfee={2}%" -f $remote.pool.url, $remote.pool.connected, $remote.pool.devfee_percent)
    Write-Host ("  hashrate   {0}{1} ({2})" -f $prefix, (Format-Hashrate $remote.hashrate.total_hs), $remote.hashrate.source)
    foreach ($gpu in @($remote.hashrate.per_gpu)) {
        if ($null -eq $gpu) { continue }
        Write-Host ("    gpu[{0}] {1} {2}" -f $gpu.index, $gpu.pci, (Format-Hashrate $gpu.hashrate_hs))
    }
    if ($remote.shares.available) {
        Write-Host ("  shares     accepted={0} stale={1} rejected={2} last_accepted={3}" -f $remote.shares.accepted, $remote.shares.stale, $remote.shares.rejected, $remote.shares.last_accepted_at)
    } else {
        Write-Host '  shares     unavailable (miner API did not report them)'
    }
    foreach ($gpu in @($remote.gpu.detected)) {
        if ($null -eq $gpu) { continue }
        Write-Host ("    gpu[{0}] {1} {2}  {3}C {4}W uuid={5}" -f $gpu.index, $gpu.pci, $gpu.name, $gpu.temp_c, $gpu.power_w, $gpu.uuid)
    }
    if (@($remote.unavailable).Count -gt 0) { Write-Host ("  unavailable {0}" -f (@($remote.unavailable) -join ', ')) }
    Write-Host '  pool-side  hashrate estimate and payouts: not available from this tool'
}

function Invoke-StatusOnce {
    param($Config, [switch]$Quiet)
    $argv = Get-RemoteBaseArgv $Config
    $argv += @('status', '--json')
    $fetch = Invoke-RemoteJson -Config $Config -RemoteArgv $argv -TimeoutMs 45000
    $maxAge = 45
    if ($MaxAgeSec -gt 0) { $maxAge = $MaxAgeSec }
    elseif ($Config.telemetry.max_age_s) { $maxAge = [int]$Config.telemetry.max_age_s }
    $doc = Convert-RemoteSnapshot -Snapshot $fetch.json -FetchedAt (Get-Date) -MaxAgeS $maxAge `
        -FetchOk $fetch.ok -FetchError $fetch.error -FetchDurationMs $fetch.duration_ms `
        -Transport 'ssh' -TransportSecurity $(if ($Config.ssh.strict_host_key_checking -eq $false) { 'insecure-host-key' } else { 'strict-host-key' })
    Write-JsonFileAtomic -Path (Join-Path $script:StateDir 'host-telemetry.json') -Object $doc
    if ($doc.status -eq 'ok') {
        Write-JsonFileAtomic -Path (Join-Path $script:StateDir 'last-good.json') -Object $doc
    }
    return [pscustomobject]@{ doc = $doc; exit = (Get-StatusExitCode $doc.status) }
}

function Invoke-RemoteAction {
    param($Config, [string[]]$RemoteArgs, [int]$TimeoutMs = 120000)
    $argv = Get-RemoteBaseArgv $Config
    $argv += $RemoteArgs
    $result = Invoke-RemoteJson -Config $Config -RemoteArgv $argv -TimeoutMs $TimeoutMs
    if (-not $result.ok) {
        if ($result.stderr) { Write-Host $result.stderr.Trim() -ForegroundColor Red }
        if ($result.stdout.Trim()) { Write-Host $result.stdout.Trim() }
        throw "remote action failed (exit $($result.code)): $($result.error)"
    }
    return $result.json
}

function Build-DeployBundle {
    param($Config, [string]$TargetDir)
    $paths = @(
        'scripts/prl.sh',
        'scripts/prl_miner.py',
        'miner-manifest.json',
        'README.md',
        'UPLOAD-MANIFEST.md',
        'docs/TELEMETRY-SCHEMA.md'
    )
    foreach ($rel in $paths) {
        $src = Join-Path $script:Root ($rel -replace '/', [System.IO.Path]::DirectorySeparatorChar)
        if (-not (Test-Path -LiteralPath $src)) { throw "bundle source missing: $src" }
        $dst = Join-Path $TargetDir ($rel -replace '/', [System.IO.Path]::DirectorySeparatorChar)
        $dir = Split-Path -Parent $dst
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }
    $cfgPath = $ConfigFile
    if (-not $cfgPath) { $cfgPath = Join-Path $script:Root 'config\prl-mining.local.json' }
    if (-not (Test-Path -LiteralPath $cfgPath)) { throw "miner config not found: $cfgPath (copy config/prl-mining.example.json)" }
    $minerCfg = Read-JsonFile $cfgPath
    if ($null -eq $minerCfg) { throw "miner config is not valid JSON: $cfgPath" }
    if ($Config.remote.worker_name) {
        $minerCfg.worker_name = [string]$Config.remote.worker_name
    }
    $etc = Join-Path $TargetDir 'etc'
    if (-not (Test-Path -LiteralPath $etc)) { New-Item -ItemType Directory -Force -Path $etc | Out-Null }
    $target = Join-Path $etc 'prl-mining.json'
    [System.IO.File]::WriteAllText($target, ($minerCfg | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
    return $minerCfg
}

function Invoke-Deploy {
    param($Config)
    $id = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "esi-pearl-$id"
    $tgz = Join-Path ([System.IO.Path]::GetTempPath()) "esi-pearl-$id.tgz"
    $remoteTgz = "/tmp/esi-pearl-$id.tgz"
    $remoteHome = [string]$Config.remote.home
    $phases = [ordered]@{}
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        $minerCfg = Build-DeployBundle -Config $Config -TargetDir $tmp
        $phases['bundle_ms'] = [int]$sw.ElapsedMilliseconds
        Write-Info ("bundle built ({0}) worker={1}" -f $phases['bundle_ms'], $minerCfg.worker_name)

        & tar.exe -czf $tgz -C $tmp .
        if ($LASTEXITCODE -ne 0) { throw "tar failed (exit $LASTEXITCODE)" }
        $phases['pack_ms'] = [int]$sw.ElapsedMilliseconds

        $scpArgs = @('-P', [string]([int]($(if ($Config.ssh.port) { $Config.ssh.port } else { 22 }))),
                     '-o', 'BatchMode=yes',
                     '-o', $(if ($Config.ssh.strict_host_key_checking -eq $false) { 'StrictHostKeyChecking=no' } else { 'StrictHostKeyChecking=yes' }))
        if ($Config.ssh.known_hosts) { $scpArgs += @('-o', "UserKnownHostsFile=$($Config.ssh.known_hosts)") }
        if ($Config.ssh.key_path) { $scpArgs += @('-i', [string]$Config.ssh.key_path, '-o', 'IdentitiesOnly=yes') }
        $scpArgs += @($tgz, "$($Config.ssh.user)@$($Config.ssh.host):$remoteTgz")
        $scp = Invoke-Native -Exe 'scp' -ArgumentList $scpArgs -TimeoutMs 300000
        if ($scp.code -ne 0) { throw "scp failed (exit $($scp.code)): $($scp.err.Trim())" }
        $phases['upload_ms'] = [int]$sw.ElapsedMilliseconds

        $extract = "mkdir -p '$remoteHome' && tar -xzf '$remoteTgz' -C '$remoteHome' && chmod +x '$remoteHome/scripts/prl.sh' && rm -f '$remoteTgz' && echo esi-pearl-extracted"
        $sshArgs = Get-SshArgumentList -Config $Config -RemoteCommand $extract
        $r = Invoke-Native -Exe 'ssh' -ArgumentList $sshArgs -TimeoutMs 120000
        if ($r.code -ne 0 -or $r.out -notmatch 'esi-pearl-extracted') {
            throw "remote extract failed (exit $($r.code)): $($r.err.Trim())"
        }
        $phases['extract_ms'] = [int]$sw.ElapsedMilliseconds

        $check = Invoke-RemoteAction -Config $Config -RemoteArgs @('check', '--json') -TimeoutMs 180000
        $phases['check_ms'] = [int]$sw.ElapsedMilliseconds
        $phases['total_ms'] = [int]$sw.ElapsedMilliseconds
        $doc = [ordered]@{
            schema  = 'esi.pearl.deploy/1'
            ok      = [bool]$check.ok
            at      = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            phases  = $phases
            check   = $check
            note    = 'prepare downloads and verifies the pinned miner on the remote host; run -Action prepare next'
        }
        Write-JsonFileAtomic -Path (Join-Path $script:StateDir 'last-deploy.json') -Object $doc
        if ($Json) { $doc | ConvertTo-Json -Depth 10 } else {
            Write-Host ("deployed to {0}:{1} in {2}ms (bundle {3}ms, upload {4}ms, extract {5}ms, check {6}ms)" -f `
                $Config.ssh.host, $remoteHome, $phases['total_ms'], $phases['bundle_ms'], $phases['upload_ms'], $phases['extract_ms'], $phases['check_ms'])
            if ($check.ok) { Write-Host 'remote preflight: ready (run -Action prepare, then -Action start)' }
            else { Write-Host 'remote preflight: blocked' -ForegroundColor Yellow; $check.checks | Where-Object { -not $_.ok } | ForEach-Object { Write-Host ("  - {0}: {1}" -f $_.name, $_.detail) } }
        }
        return $(if ($check.ok) { 0 } else { 3 })
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
        if (Test-Path -LiteralPath $tgz) { Remove-Item -LiteralPath $tgz -Force }
    }
}

if ($LibraryOnly) {
    # Dot-source target for tests: expose the functions, do nothing else.
    return
}

try {
    $hostCfg = Get-HostConfig -Path $Config
    switch ($Action) {
        'status' {
            $r = Invoke-StatusOnce -Config $hostCfg
            if ($Json) {
                $r.doc | ConvertTo-Json -Depth 12
            } else {
                Write-HostDocument -Doc $r.doc
            }
            $script:ExitCode = $r.exit
        }
        'watch' {
            $i = 0
            while ($true) {
                $r = Invoke-StatusOnce -Config $hostCfg
                if ($Json) {
                    $r.doc | ConvertTo-Json -Depth 12 -Compress
                } else {
                    $remote = $r.doc.remote
                    $ageText = if ($null -ne $r.doc.age_s) { $r.doc.age_s } else { '?' }
                    $line = "{0}  {1,-20} age={2}s" -f $r.doc.fetched_at, $r.doc.status, $ageText
                    if ($null -ne $remote -and $r.doc.fresh) {
                        $line += ("  hashrate={0} accepted={1} pool={2}" -f (Format-Hashrate $remote.hashrate.total_hs), $remote.shares.accepted, $remote.pool.connected)
                    } elseif ($null -ne $remote) {
                        $line += ("  last-known hashrate={0} (not live)" -f (Format-Hashrate $remote.hashrate.total_hs))
                    }
                    Write-Host $line
                }
                $script:ExitCode = $r.exit
                $i++
                if ($Iterations -gt 0 -and $i -ge $Iterations) { break }
                Start-Sleep -Seconds $IntervalSec
            }
        }
        'deploy' {
            $script:ExitCode = Invoke-Deploy -Config $hostCfg
        }
        'prepare' {
            $r = Invoke-RemoteAction -Config $hostCfg -RemoteArgs @('prepare', '--allow-download', '--json') -TimeoutMs 900000
            if ($Json) { $r | ConvertTo-Json -Depth 8 } else { Write-Host ("miner prepared: {0} {1}" -f $r.prepare.miner_name, $r.prepare.miner_version) }
            $script:ExitCode = 0
        }
        'check' {
            $r = Invoke-RemoteAction -Config $hostCfg -RemoteArgs @('check', '--json')
            if ($Json) { $r | ConvertTo-Json -Depth 8 } else {
                foreach ($c in $r.checks) { Write-Host ("  [{0}] {1}: {2}" -f $(if ($c.ok) { 'ok' } else { 'FAIL' }), $c.name, $c.detail) }
            }
            $script:ExitCode = $(if ($r.ok) { 0 } else { 3 })
        }
        'start' {
            $r = Invoke-RemoteAction -Config $hostCfg -RemoteArgs @('start', '--json') -TimeoutMs 240000
            if ($Json) { $r | ConvertTo-Json -Depth 8 } else {
                Write-Host ("started run {0} pid {1} api={2}" -f $r.run_id, $r.miner_pid, $r.api_status)
            }
            $script:ExitCode = $(if ($r.ok) { 0 } else { 4 })
        }
        'stop' {
            $r = Invoke-RemoteAction -Config $hostCfg -RemoteArgs @('stop', '--json') -TimeoutMs 180000
            if ($Json) { $r | ConvertTo-Json -Depth 8 } else { Write-Host ("stopped: {0}" -f $r.miner) }
            $script:ExitCode = $(if ($r.ok) { 0 } else { 6 })
        }
        'logs' {
            # Logs are plain text, so they bypass the JSON path deliberately.
            $argv = Get-RemoteBaseArgv $hostCfg
            $argv += @('logs', '--source', $LogSource, '--lines', [string]$Lines)
            $remoteCommand = Join-RemoteCommand $argv
            $sshArgs = Get-SshArgumentList -Config $hostCfg -RemoteCommand $remoteCommand
            $r = Invoke-Native -Exe 'ssh' -ArgumentList $sshArgs -TimeoutMs 60000
            if ($r.code -ne 0) { throw "remote logs failed (exit $($r.code)): $($r.err.Trim())" }
            Write-Host $r.out.TrimEnd()
            $script:ExitCode = 0
        }
        'devices' {
            $r = Invoke-RemoteAction -Config $hostCfg -RemoteArgs @('devices', '--json')
            if ($Json) { $r | ConvertTo-Json -Depth 8 } else {
                foreach ($g in $r.miner_devices.devices) { Write-Host ("  [{0}] {1} {2}" -f $g.index, $g.pci, $g.name) }
                Write-Host ("support: " + $r.gpu_support.detail)
            }
            $script:ExitCode = $(if ($r.ok) { 0 } else { 3 })
        }
    }
} catch {
    Write-Host ("error: " + $_.Exception.Message) -ForegroundColor Red
    $script:ExitCode = 2
}

exit $script:ExitCode
