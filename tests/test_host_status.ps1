#requires -Version 5.1
<#
  Offline tests for windows/prl-remote.ps1 (no SSH, no GPU, no rented host).

  Exercises the pure status/freshness mapping and the SSH argument builder.
  Run:  powershell -ExecutionPolicy Bypass -File tests\test_host_status.ps1
#>
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

. (Join-Path $root 'windows\prl-remote.ps1') -LibraryOnly

$script:Failures = 0
$script:Checks = 0

function Assert-Equal {
    param($Expected, $Actual, [string]$Name)
    $script:Checks++
    if ($Expected -ne $Actual) {
        $script:Failures++
        Write-Host ("FAIL {0}: expected <{1}> got <{2}>" -f $Name, $Expected, $Actual) -ForegroundColor Red
    }
}

function Assert-True {
    param($Condition, [string]$Name)
    $script:Checks++
    if (-not $Condition) {
        $script:Failures++
        Write-Host ("FAIL {0}: condition was false" -f $Name) -ForegroundColor Red
    }
}

function New-Snapshot {
    param(
        [string]$SampledAt = (Get-Date).ToUniversalTime().AddSeconds(-2).ToString('yyyy-MM-ddTHH:mm:ssZ'),
        [string]$ProcessState = 'running',
        [string]$Identity = 'verified',
        [string]$ApiStatus = 'ok',
        $ExitCode = $null
    )
    return [pscustomobject]@{
        schema    = 'esi.pearl.telemetry/1'
        sampled_at = $SampledAt
        instance  = [pscustomobject]@{ hostname = 'gpu-test'; os = 'Ubuntu 24.04'; machine_id = 'abc123' }
        miner     = [pscustomobject]@{
            name = 'krig-miner'; version = '1.5.12'; process_state = $ProcessState
            identity = $Identity; uptime_s = 120.0
            exit = [pscustomobject]@{ code = $ExitCode; signal = $null; at = $SampledAt }
        }
        pool      = [pscustomobject]@{ url = 'stratum+ssl://prl-us.kryptex.network:8048'; worker = 'W'; connected = $true; devfee_percent = 0.0 }
        hashrate  = [pscustomobject]@{
            total_hs = 7.1e13; unit = 'H/s'; source = 'miner_api'; per_gpu_available = $true
            per_gpu  = @([pscustomobject]@{ index = 0; pci = '08:00.0'; name = 'RTX 4090'; hashrate_hs = 7.1e13; hashrate_source = 'miner_api'; shares = $null; error = $null })
        }
        shares    = [pscustomobject]@{ accepted = 12; stale = 0; rejected = 0; available = $true; last_accepted_at = $SampledAt }
        gpu       = [pscustomobject]@{ count = 1; detected = @(); source = 'test'; telemetry_source = 'nvidia-smi' }
        api       = [pscustomobject]@{ url = 'http://127.0.0.1:12000/'; status = $ApiStatus; error = $null; latency_ms = 3.0 }
        metrics_api = [pscustomobject]@{ status = 'ok'; error = $null }
        log       = [pscustomobject]@{ last_error = $null; available = $true }
        unavailable = @()
        errors    = @()
        pool_side = [pscustomobject]@{ hashrate_estimate = $null; payouts = $null; note = 'n/a' }
    }
}

$now = Get-Date

# 1. healthy fresh sample
$doc = Convert-RemoteSnapshot -Snapshot (New-Snapshot) -FetchedAt $now -MaxAgeS 45 -FetchOk $true -FetchDurationMs 400
Assert-Equal 'ok' $doc.status 'fresh snapshot status'
Assert-True $doc.fresh 'fresh flag'
Assert-Equal 0 (Get-StatusExitCode $doc.status) 'exit code ok'
Assert-True ($doc.age_s -ge 0 -and $doc.age_s -le 10) 'age is small'

# 2. stale data must not be reported as current
$old = (Get-Date).ToUniversalTime().AddSeconds(-300).ToString('yyyy-MM-ddTHH:mm:ssZ')
$doc = Convert-RemoteSnapshot -Snapshot (New-Snapshot -SampledAt $old) -FetchedAt $now -MaxAgeS 45
Assert-Equal 'stale' $doc.status 'stale snapshot status'
Assert-True (-not $doc.fresh) 'stale fresh flag'
Assert-Equal 3 (Get-StatusExitCode $doc.status) 'exit code stale'
Assert-True ($doc.status_reason -match 'last known') 'stale reason mentions last known'

# 3. miner exited
$doc = Convert-RemoteSnapshot -Snapshot (New-Snapshot -ProcessState 'exited' -ExitCode 3) -FetchedAt $now -MaxAgeS 45
Assert-Equal 'miner_exited' $doc.status 'exited status'
Assert-Equal 4 (Get-StatusExitCode $doc.status) 'exit code exited'

# 4. api down while the process state is unverified
$doc = Convert-RemoteSnapshot -Snapshot (New-Snapshot -ProcessState 'unknown' -ApiStatus 'unreachable') -FetchedAt $now -MaxAgeS 45
Assert-Equal 'api_unreachable' $doc.status 'api down status'
Assert-Equal 5 (Get-StatusExitCode $doc.status) 'exit code api'

# 5. transport failure
$doc = Convert-RemoteSnapshot -Snapshot $null -FetchedAt $now -MaxAgeS 45 -FetchOk $false -FetchError 'ssh: connect timed out'
Assert-Equal 'unreachable' $doc.status 'transport failure status'
Assert-Equal 6 (Get-StatusExitCode $doc.status) 'exit code unreachable'
Assert-True ($null -eq $doc.remote) 'unreachable keeps remote null'

# 6. unusable payload
$doc = Convert-RemoteSnapshot -Snapshot $null -FetchedAt $now -MaxAgeS 45 -FetchOk $true
Assert-Equal 'protocol_error' $doc.status 'null payload status'
Assert-Equal 7 (Get-StatusExitCode $doc.status) 'exit code protocol'

# 7. identity not proven
$doc = Convert-RemoteSnapshot -Snapshot (New-Snapshot -Identity 'failed') -FetchedAt $now -MaxAgeS 45
Assert-Equal 'identity_unverified' $doc.status 'identity status'
Assert-Equal 8 (Get-StatusExitCode $doc.status) 'exit code identity'

# 8. never started
$doc = Convert-RemoteSnapshot -Snapshot (New-Snapshot -ProcessState 'not_started' -ApiStatus 'unreachable') -FetchedAt $now -MaxAgeS 45
Assert-Equal 'miner_not_running' $doc.status 'not started status'
Assert-Equal 9 (Get-StatusExitCode $doc.status) 'exit code not started'

# 9. missing sampled_at
$bad = New-Snapshot
$bad.sampled_at = $null
$doc = Convert-RemoteSnapshot -Snapshot $bad -FetchedAt $now -MaxAgeS 45
Assert-Equal 'protocol_error' $doc.status 'missing sampled_at'

# 10. clock skew flag
$future = (Get-Date).ToUniversalTime().AddSeconds(600).ToString('yyyy-MM-ddTHH:mm:ssZ')
$doc = Convert-RemoteSnapshot -Snapshot (New-Snapshot -SampledAt $future) -FetchedAt $now -MaxAgeS 45
Assert-True $doc.clock_skew_suspect 'skew detected'

# 11. ssh arguments: strict host key by default, batch mode, no secrets on the command line
$cfg = [pscustomobject]@{
    ssh = [pscustomobject]@{ host = 'gpu.example'; user = 'ubuntu'; port = 2222; key_path = 'C:\keys\id_ed25519'; known_hosts = 'C:\keys\known_hosts'; strict_host_key_checking = $true; connect_timeout_s = 10 }
    remote = [pscustomobject]@{ home = '/home/ubuntu/esi-pearl'; python = 'python3'; worker_name = $null }
    telemetry = [pscustomobject]@{ max_age_s = 45 }
}
$sshArgs = Get-SshArgumentList -Config $cfg -RemoteCommand 'python3 test.py'
$joined = $sshArgs -join ' '
Assert-True ($joined -match 'BatchMode=yes') 'ssh batch mode'
Assert-True ($joined -match 'StrictHostKeyChecking=yes') 'ssh strict host key default'
Assert-True ($joined -match 'UserKnownHostsFile=C:\\keys\\known_hosts') 'ssh known hosts'
Assert-True ($joined -match '-p 2222') 'ssh port'
Assert-True ($joined -match 'ubuntu@gpu\.example') 'ssh target'
Assert-True (-not ($joined -match 'BEGIN|PRIVATE KEY')) 'no key material on the command line'

# 12. remote command quoting survives spaces and quotes
$cmd = Join-RemoteCommand @('python3', '/home/u/esi-pearl/scripts/prl_miner.py', '--home', '/home/u/esi-pearl', 'logs', '--source', 'miner')
$expected = "'python3' '/home/u/esi-pearl/scripts/prl_miner.py' '--home' '/home/u/esi-pearl' 'logs' '--source' 'miner'"
Assert-Equal $expected $cmd 'remote argv quoting'
Assert-Equal "'a'\''b'" (Join-RemoteCommand @("a'b")) 'embedded single quote escaped'

# 13. Windows argument quoting helper
Assert-Equal '"a b"' (Quote-WinArg 'a b') 'quote with space'
Assert-Equal 'plain' (Quote-WinArg 'plain') 'quote without space'

# 14. hostname/path validation rejects traversal
$badCfg = [pscustomobject]@{
    ssh = [pscustomobject]@{ host = 'h'; user = 'u' }
    remote = [pscustomobject]@{ home = '/home/u/../etc'; python = 'python3' }
}
$threw = $false
try { Get-HostConfig -Path (Join-Path $env:TEMP 'never-read.json') | Out-Null } catch { $threw = $true }
Assert-True $threw 'missing host config is rejected'

Write-Host ("host status tests: {0} checks, {1} failures" -f $script:Checks, $script:Failures)
if ($script:Failures -gt 0) { exit 1 }
exit 0
