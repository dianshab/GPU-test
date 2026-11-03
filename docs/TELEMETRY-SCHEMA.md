# Telemetry contract — ESI Pearl remote miner

Two documents, one contract. Both are UTF-8 JSON, written atomically
(`<file>.tmp.<pid>` → `os.replace` / delete+move), so a reader never sees a
half-written file.

```
remote (Linux)      $PRL_HOME/state/telemetry.json     schema: esi.pearl.telemetry/1
host   (Windows)    <bundle>/state/host-telemetry.json schema: esi.pearl.host.telemetry/1
host   (Windows)    <bundle>/state/last-good.json      last document whose status was ok
```

`docs/telemetry-example.json` is a complete remote document (generated with the
mock miner and scrubbed; the values are illustrative, not measured).

---

## 1. Remote document — `esi.pearl.telemetry/1`

### Identity and timing

| Field | Type | Notes |
|---|---|---|
| `schema` | string | `esi.pearl.telemetry/1` |
| `collected_by` | object | `{name, version}` of the controller |
| `seq` | int | increments per write; a host that sees the same `seq` twice did not get new data |
| `sampled_at` | ISO-8601 UTC | when this sample was taken **on the remote clock** |
| `sample_interval_s` | int | collector period |
| `stale_after_s` | int | remote hint; the host applies `max(host max_age_s, this)` |
| `instance.machine_id` | string | `/etc/machine-id` (or hostname hash) |
| `instance.hostname` | string | |
| `instance.os` / `kernel` / `arch` | string | `PRETTY_NAME`, `uname -r`, machine |

### Miner

| Field | Type | Notes |
|---|---|---|
| `miner.name` | string | `krig-miner` |
| `miner.version` | string\|null | from the miner API, else from the install record |
| `miner.git_sha` | string\|null | `krig_miner_info` label |
| `miner.algorithm` | string\|null | `pearlhash` when reported |
| `miner.pid` | int\|null | supervised child |
| `miner.run_id` | string\|null | token we injected; not a secret |
| `miner.started_at` / `uptime_s` | ISO / float\|null | |
| `miner.process_state` | enum | `running` \| `exited` \| `not_started` \| `unknown` |
| `miner.identity` | enum | `verified` \| `failed` \| `process_gone` \| `none` |
| `miner.identity_problems` | string[] | why identity failed |
| `miner.args_masked` | string[] | command line with the wallet masked |
| `miner.exit` | object\|null | `{code, signal, at}` once the supervisor saw it exit |

`process_state = running` requires **all** of: PID alive, kernel start time
matches the record, `PRL_RUN_ID` matches, executable path matches. `unknown`
means "alive but not provable" and must be treated as suspect, never as healthy.

### Pool

| Field | Type | Notes |
|---|---|---|
| `pool.url` | string | the configured endpoint (no credentials in it) |
| `pool.worker` | string | |
| `pool.connected` | bool\|null | `krig_miner_pool_connected`; `null` when the metric is absent |
| `pool.latency_ms` | float\|null | `krig_miner_pool_latency_ms` |
| `pool.heartbeat_age_s` | float\|null | worker heartbeat age |
| `pool.last_job_at` | ISO\|null | derived from the log line `stratum: new job` |
| `pool.last_connected_at` | ISO\|null | derived from the log line `connected: …` |
| `pool.devfee_percent` | float\|null | parsed from `pool … devfee N%` |

### Hashrate — miner-local only

| Field | Type | Notes |
|---|---|---|
| `hashrate.total_hs` | float\|null | sum of the per-GPU values; **H/s**, not TH/s |
| `hashrate.unit` | string | always `H/s` |
| `hashrate.source` | enum\|null | `miner_api` \| `miner_metrics` \| `null` |
| `hashrate.per_gpu[]` | array | `{index, pci, name, hashrate_hs, hashrate_source, shares, error}` |
| `hashrate.per_gpu_available` | bool | false when only a total could be read |
| `hashrate.note` | string | restates "not a pool estimate, not a payout" |

Units are carried explicitly; nothing in this document is assumed to be TH/s.
A value is `null` when it could not be read — never a default, never a
theoretical figure.

### Shares

| Field | Type | Notes |
|---|---|---|
| `shares.accepted` / `stale` / `rejected` | int\|null | summed over devices |
| `shares.available` | bool | false when neither `/` nor `/metrics` exposed counters |
| `shares.source` | string | `miner_api` \| `miner_metrics` |
| `shares.last_accepted_at` | ISO\|null | counter-delta observation, else log fallback |

Accepted shares are the miner's own counters. They are **not** a payout and do
not by themselves prove the pool credited the wallet.

### GPU inventory

| Field | Type | Notes |
|---|---|---|
| `gpu.count` | int | devices reported by `krig-miner --list-devices` |
| `gpu.detected[]` | array | `{index, pci, name, uuid, driver, temp_c, power_w, util_pct, mem_used_mib, mem_total_mib}` |
| `gpu.source` | string | which probes produced the inventory |
| `gpu.telemetry_source` | string\|null | `nvidia-smi` or `null` |

`uuid` and the temperature/power fields are `null` when `nvidia-smi` is absent
or the device could not be matched.

### Health and honesty fields

| Field | Type | Notes |
|---|---|---|
| `api` | object | `{url, status, error, latency_ms}`; `status` ∈ `ok` \| `unreachable` \| `schema_error` |
| `metrics_api` | object | `{status, error}` |
| `log` | object | `{last_error, available}` |
| `unavailable[]` | string[] | machine-readable list of what is missing, e.g. `per_gpu_hashrate`, `gpu_temperature_power`, `shares`, `miner_api_hashrate` |
| `errors[]` | string[] | human-readable probe errors |
| `pool_side` | object | `{hashrate_estimate, payouts, note}` — **always `null`** here; this tool has no pool API credentials |

---

## 2. Host document — `esi.pearl.host.telemetry/1`

```json
{
  "schema": "esi.pearl.host.telemetry/1",
  "fetched_at": "2026-11-03T13:40:12Z",
  "transport": { "kind": "ssh", "security": "strict-host-key", "ok": true, "error": null, "duration_ms": 640 },
  "status": "ok",
  "status_reason": "fresh sample, verified miner process, local API answering",
  "fresh": true,
  "age_s": 1.8,
  "max_age_s": 45,
  "clock_skew_s": 0.4,
  "clock_skew_suspect": false,
  "remote": { "...the full remote document..." }
}
```

| Field | Notes |
|---|---|
| `status` | see below; **the only field a UI should branch on** |
| `fresh` | `true` only when `status == "ok"` |
| `age_s` | `fetched_at − remote.sampled_at`, host clock, clamped at 0 |
| `clock_skew_s` / `clock_skew_suspect` | remote-vs-host clock difference; `>120 s` is flagged |
| `transport.security` | `strict-host-key` or `insecure-host-key` (the latter only if you configured it) |
| `remote` | `null` when the transport failed, otherwise the raw remote document |

Status values and exit codes:

| status | exit | meaning / required UI behaviour |
|---|---|---|
| `ok` | 0 | show values as live |
| `stale` | 3 | newest sample too old; show "no current data", at most the last known values labelled as such |
| `miner_exited` | 4 | miner process gone; do not show a hash rate |
| `miner_not_running` | 9 | never started on this instance |
| `api_unreachable` | 5 | process unverified or local API down; metric values are unknown |
| `identity_unverified` | 8 | PID not provably ours; refuse to signal, do not present as healthy |
| `unreachable` | 6 | SSH/host/network failure; `remote` is `null` |
| `protocol_error` | 7 | unusable response; do not guess |

---

## 3. Consumer rules (important)

1. Branch on `status`, never on "the SSH command exited 0".
2. `fresh == false` ⇒ the numbers are historical. Label them `last known` or
   hide them; never print them as the current hash rate.
3. `pool_side.hashrate_estimate` and `pool_side.payouts` are `null` unless a
   future component fills them from a real pool/wallet API. Do not estimate
   them from `hashrate.total_hs`.
4. Treat `shares.accepted` as *miner-reported*. "Pool accepted a share" needs
   the pool side, which is out of scope here.
5. `miner.process_state == running` is not "earning": also check
   `pool.connected`, `shares.last_accepted_at`, and how long the miner has been
   up.
6. `unavailable[]` is the authoritative list of what is missing in this sample.
7. `seq` repeating across two fetches means the sampler did not produce a new
   sample (stopped or wedged), even if SSH worked.

---

## 4. Minimal ESI Workbench consumption sketch

PowerShell (works today):

```powershell
$doc = (& "$work\pearl-remote\windows\prl-remote.ps1" -Action status -Json) | ConvertFrom-Json
switch ($doc.status) {
    'ok'             { $kind = 'ReadyOwned';  $detail = "远程 Pearl · $([math]::Round($doc.remote.hashrate.total_hs/1e12,1)) TH/s · accepted $($doc.remote.shares.accepted)" }
    'stale'          { $kind = 'Stopped';     $detail = "远程数据已过期 ($($doc.age_s)s) · 不显示为实时算力" }
    'miner_exited'   { $kind = 'Stopped';     $detail = '远程矿工已退出' }
    'unreachable'    { $kind = 'Unavailable'; $detail = '无法连接云主机 (SSH)' }
    default          { $kind = 'Blocked';     $detail = "远程状态: $($doc.status)" }
}
```

C# (future adapter) only needs `status`, `age_s`, `remote.hashrate.total_hs`,
`remote.shares.accepted`, `remote.pool.connected`, `remote.miner.process_state`
and `remote.unavailable` — all of them optional/nullable by design.

Files to read from ESI:

* live view: `<bundle>/state/host-telemetry.json`
* last known good: `<bundle>/state/last-good.json` (must be labelled historical)
* deploy history: `<bundle>/state/last-deploy.json`

---

## 5. Sampling cost

Default `sample_interval_s = 10`. Per sample: two loopback HTTP GETs (~4.5 KB
total), one `nvidia-smi` query (~30–80 ms), one 64 KB log tail, plus one cached
`--list-devices` per minute. Telemetry is written to disk once per sample; the
host reads it only when you ask (`status`/`watch`), so nothing is pushed
continuously and nothing is sent to any external service.
