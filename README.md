# ESI Pearl (PRL) remote miner — Linux deployment + Windows telemetry

Self-contained bundle for running **one** Pearl (PRL) miner on a Linux GPU
machine that you have rented yourself, and for reading its live state from
this Windows host over SSH.

This is the "deploy + observe" slice of the future
`Service Control Panel → Auto Mining` feature. It does **not** rent, release,
trade, withdraw, or switch anything, and it does not touch the local RTX 5060
miner or its configuration.

```
Windows host                                   Linux GPU instance (yours)
────────────                                   ───────────────────────────
windows/prl-remote.ps1  ── ssh/scp ──────────▶ scripts/prl_miner.py
  deploy / prepare / start / stop              ├─ bin/krig-miner      (pinned, SHA-256 verified)
  status / watch / logs                        ├─ etc/prl-mining.json (public wallet, pool, worker)
  state/host-telemetry.json  ◀── status --json ─┤ state/telemetry.json (sampled every 10 s)
  state/last-good.json                          └─ logs/*.log
```

---

## 1. What this is

| Piece | Purpose |
|---|---|
| `scripts/prl_miner.py` | Remote controller + sampler. Python 3.8+, standard library only. |
| `scripts/prl.sh` | Thin Linux entry point (`./prl.sh start`, …). |
| `miner-manifest.json` | Pinned miner releases with SHA-256 for the tarball **and** each extracted file. |
| `config/prl-mining.example.json` | Config template with placeholders (safe to publish). |
| `config/prl-mining.local.json` | Local wallet / pool / worker configuration, omitted from this release. Create locally from the example. |
| `windows/prl-remote.ps1` | Host-side deploy/status/stop/logs over SSH, plus freshness/status mapping. |
| `windows/host.example.json` | Template for the SSH target. |
| `docs/TELEMETRY-SCHEMA.md` | The JSON contract ESI Workbench will read later. |
| `docs/telemetry-example.json` | A full telemetry document (generated with the mock miner, scrubbed). |
| `tests/` | Offline suite: unit tests, mock-miner lifecycle tests, PowerShell mapping tests. |

## 2. Which miner and why

**krig-miner 1.5.12 (linux-x64)**, from `github.com/kryptex/krig-miner`
releases. Reasons, all checkable:

* It is the miner already in production on this host (1.5.12, Windows), and it
  speaks the same pool protocol the ESI config already uses
  (`stratum+ssl://prl-us.kryptex.network:8048`). No pool change is needed.
* Linux x64 is an official release asset (92,212,114 bytes), not a rebuild:
  <https://github.com/kryptex/krig-miner/releases/download/v1.5.12/krig-miner-1.5.12-linux-x64.tar.gz>
  SHA-256 `6c456f6540a26ab2dd5fa02434d5827f09d19e06001ef0e6eef5ac1f80b17cea`
  (downloaded and hashed here, and equal to the digest GitHub reports).
  Extracted `krig-miner` binary SHA-256
  `b1cf46c9c581099a59bc09535de30ddca8711eefa52a9a0c5dadc4dfff830e8a`.
* No compiler is needed on the rented box, so start-up cost is download +
  hash + extract only.
* It exposes both a JSON status API and a Prometheus endpoint, and it lists
  devices on demand (`--list-devices`), so GPU detection and telemetry come
  from the miner itself instead of a hand-maintained GPU table.
* Developer fee: `krig-miner --help` (1.5.12) states **0 % on Kryptex pools,
  3 % minimum on other pools**. The fee is inside the miner and is not
  disabled, wrapped or bypassed here. The scripts refuse a
  `stratum+tcp://` Kryptex endpoint instead of silently switching pools.

**open-pearl-miner is not used.** `work/pearl-kernel-research/RESEARCH_STATUS.md`
records that its SM120 path still lacks bit-level reference checks and
end-to-end accepted-share validation, and its pool protocol targets
LuckyPool. Shipping it would mean changing the pool and running unvalidated
kernels.

Version pinning: `miner-manifest.json` carries 1.5.12 (default) and 1.5.13
(also downloaded and hash-verified here). 1.5.12 is the default because the
telemetry schema in this project was **observed live** on 1.5.12; 1.5.13 shows
the same telemetry string set in the binary (static check only). Nothing ever
follows "latest".

## 3. Requirements

Remote Linux host, expected (see §10 for what is actually tested):

* Linux x86-64. `python3` ≥ 3.8. `tar` is not required (Python `tarfile` is used).
* An NVIDIA GPU with a working driver (`nvidia-smi` answers). AMD/ROCm is
  detected but **not** accepted by default: set `allow_unverified_gpu: true`
  only if you accept that this project never verified AMD+Pearl.
* No root needed. Everything lives under `~/esi-pearl` (override `PRL_HOME`).
* Outbound HTTPS (GitHub) for the one-time miner download, and outbound TCP to
  the pool.
* No wallets, no private keys. Only the public payout address.

Windows host: Windows PowerShell 5.1, OpenSSH client (`ssh`, `scp`), and the
host's SSH key already trusted by the instance (host key verified — see §7).

## 4. Quick start (remote side)

```bash
# 1. put the bundle on the machine (or use -Action deploy from Windows, §7)
scp -r pearl-remote ubuntu@gpu-host:~/esi-pearl-src

# 2. install the pinned miner (one-time, ~100 MB download)
cd ~/esi-pearl-src && ./scripts/prl.sh prepare --allow-download

# 3. preflight: config, wallet shape, GPU list, driver, port, disk
./scripts/prl.sh check

# 4. start one supervised miner + the sampler
./scripts/prl.sh start

# 5. look at it
./scripts/prl.sh status          # human summary
./scripts/prl.sh status --json   # machine-readable telemetry
./scripts/prl.sh logs --source miner --lines 60

# 6. stop (miner only — the rented instance keeps billing)
./scripts/prl.sh stop
```

`PRL_HOME` defaults to the bundle root when the scripts sit in
`<root>/scripts` next to `etc/` or `miner-manifest.json`, and to `~/esi-pearl`
otherwise (`--home`/`PRL_HOME` override both). The config is looked up at
`$PRL_HOME/etc/prl-mining.json` first, then at `<bundle>/config/prl-mining.local.json`,
so the tree works in place and after `deploy`. The `deploy` action lays the
installed layout out for you.

Exit codes: `0` ok, `2` config error, `3` preflight blocked, `4` start/exited,
`5` identity not provable (refused to signal), `6` stop failed, `7` API never
answered.

## 5. Wallet, pool, worker

This publication snapshot excludes the user's local configuration. Create
`config/prl-mining.local.json` locally from `config/prl-mining.example.json`,
then fill in the public payout address, pool URL and worker name. Keep that
file out of the repository. The example pool is
`stratum+ssl://prl-us.kryptex.network:8048`. The address is masked in status
output; this tool does not require the wallet private key.

Rules enforced in code:

* Missing wallet, placeholder wallet, uppercase wallet, or a string that is
  not `prl1` + bech32 charset fails **before** the miner is launched. This is a
  shape check only — it does not verify the bech32 checksum and does not prove
  the address belongs to you.
* Kryptex host + `stratum+tcp://` is rejected with an explanation; the pool is
  never switched automatically.
* A non-Kryptex pool is accepted but warns about the ≥ 3 % developer fee and
  about the unverified pool protocol.
* Environment overrides for one-off runs: `PRL_WALLET`, `PRL_POOL_URL`,
  `PRL_WORKER`, `PRL_API_PORT`, `PRL_GPU_SELECT`, `PRL_SAMPLE_INTERVAL_S`.
* On a cloud box you almost certainly want a per-instance worker name
  (`ESI-CLOUD-01`, …). Set `remote.worker_name` in the host config, or
  `PRL_WORKER` on the remote side. Otherwise the configured local worker
  name is used.

No private key, mnemonic, wallet file, SSH key or platform API key is used,
stored, logged or transmitted. Telemetry contains no wallet field at all.

## 6. GPU handling

* Inventory comes from `krig-miner --list-devices` (index, PCI address, name),
  enriched by `nvidia-smi` (UUID, driver, temperature, power, utilisation,
  memory) when present.
* No GPU → `check`/`start` stop immediately with a clear error. Nothing waits
  forever.
* Multi-GPU: hashrate and shares are reported per device when the miner API
  exposes them (it does per device); the total is the sum. If only the
  Prometheus total is available, telemetry says so via
  `hashrate.source = "miner_metrics"` and `per_gpu_available`.
* PCI matching is normalised (`00000000:08:00.0` → `08:00.0`). If a device is
  listed by the miner but missing from `nvidia-smi`, its temperature/power stay
  `null` and `gpu_temperature_power` appears in `unavailable`.
* **No overclocking, no power limit, no fan change by default.** The flags
  (`power_limit_w`, `fan_percent`, `core_offset_mhz`) are `null` in the shipped
  config and are only added to the command line when you set them explicitly.
* GPU selection defaults to all supported devices; `gpu_select` accepts
  indices (`0,2`) or PCI addresses.
* The device list is cached for 60 s inside the sampler (CUDA init on every
  sample would be wasteful); a GPU disappearing is therefore visible within
  about a minute.

## 7. Deploying and reading it from Windows

```powershell
# one-time: describe the instance (no secrets inside, key by path only)
Copy-Item windows\host.example.json windows\host.local.json
notepad windows\host.local.json

# ship the bundle, extract it, run the remote preflight; prints phase timings
.\windows\prl-remote.ps1 -Action deploy
.\windows\prl-remote.ps1 -Action prepare
.\windows\prl-remote.ps1 -Action start

# live view
.\windows\prl-remote.ps1 -Action status
.\windows\prl-remote.ps1 -Action watch -IntervalSec 10
.\windows\prl-remote.ps1 -Action stop
```

SSH policy in `windows/prl-remote.ps1`:

* `BatchMode=yes` — never prompts, never types a password.
* `StrictHostKeyChecking=yes` by default, with `UserKnownHostsFile` from the
  config. If the host key is unknown the action fails and says so; the script
  never turns verification off on its own.
* The private key is referenced by path and passed to `ssh -i`; its content is
  never read, printed or copied.
* Remote commands are built as an argument vector and shell-quoted; the remote
  home must be an absolute path without `..`.
* The host document records which policy was used (`strict-host-key` or
  `insecure-host-key`) so ESI can show it.

## 8. Telemetry, freshness and what "running" means

Three different numbers are kept apart on purpose:

1. **miner-local hashrate** — `hashrate.total_hs` from the miner (this is what
   you see in the miner UI; it is an instantaneous local measurement).
2. **pool-side estimate** — not available to this tool. `pool_side.hashrate_estimate`
   is `null` and says so.
3. **payouts to the wallet** — not available to this tool;
   `pool_side.payouts` is `null`.

Remote sampler (`_collect`, started by `start`, default every **10 s**):

* 2 HTTP GETs on `127.0.0.1` only (`/` ~0.5 KB, `/metrics` ~4 KB),
* optional one `nvidia-smi --query-gpu` call (~30–80 ms),
* one 64 KB log tail for the last accepted share / last job / devfee line,
* one cached `--list-devices` call per minute.

That is a few percent of one CPU core at 10 s intervals; the interval is
configurable (`sample_interval_s`, 2–300 s) and `stale_after_s` defaults to
`max(45, 3 × interval)`.

Host-side verdict (`status` → `state/host-telemetry.json`) is computed in
`Convert-RemoteSnapshot` and has **one** of:

| status | meaning |
|---|---|
| `ok` | fresh sample, verified miner process, local API answering |
| `stale` | newest sample older than `max_age_s` (default 45 s) — the UI must not show it as live |
| `miner_exited` | the supervised miner is gone (exit code when known) |
| `miner_not_running` | nothing was ever started on that instance |
| `api_unreachable` | process state unknown/unverified, or its local API is down |
| `identity_unverified` | the PID cannot be proven to belong to this controller |
| `unreachable` | SSH/transport failed — no data at all |
| `protocol_error` | the remote answered with unusable JSON |

Age is computed on the **host** clock from the snapshot's `sampled_at`; a
remote clock skew above 120 s is flagged (`clock_skew_suspect`) instead of
being hidden. Old values are kept as `remote` for display but never marked
`fresh`, and the human output labels them `last known:`.

`state/last-good.json` is only written when the status is `ok`; when everything
is down the script prints "no remote data (never render cached values as live)".

## 9. Lifecycle, identity and failures

* `start` refuses to run a second miner: a `run.json` record whose PID, kernel
  start time, run token (environment variable) and executable path all match is
  treated as "already running".
* The miner is launched by a small supervisor process that owns exactly one
  child, writes `state/run.json` and, when the child ends, `state/exit.json`
  with the exit code or signal.
* `stop` re-verifies the four identity facts immediately before signalling, and
  again before escalating to SIGKILL. If any fact cannot be proven, it refuses
  and says why (exit 5). There is no `pkill`, no name matching, no "kill all
  python".
* PID reuse is caught by the kernel start time, not by the PID alone.
* The operator lock (`state/op.lock`) serialises start/stop on the same box.
* `stop` stops the sampler and the miner but prints, every time:
  *"miner stopped does not release the cloud GPU instance or stop its
  billing"*.
* Log rotation: any log above `log_max_bytes` (20 MB) becomes `.log.1` at the
  next start; one generation is kept.
* Miners are run with `--no-tui` and their stdout/stderr go to
  `logs/miner.log`; the supervisor and sampler log separately.

Handled failure cases: no GPU, no driver, missing/invalid wallet, missing or
incompatible pool, download failure, checksum mismatch, archive layout
mismatch, unpacked-binary hash mismatch, API never answering, miner exiting
during startup, API schema drift (falls back to `/metrics` and labels it),
unparseable API (marked, not guessed), a foreign process answering on the
API port (its data is rejected, never reported as ours), SSH loss, host reboot
(record survives, `stop` reports "already gone"), partial GPU detection, and an
existing miner that is not ours (the port check blocks startup). The sampler
also stops querying the API entirely once the miner is not verified as running,
so a stopped miner can never show a stale or foreign hash rate.

## 10. Tests: what was actually run

Run everything with `tests\run-tests.ps1` (Windows) or `tests/run-tests.sh`
(Linux). **A** = actually executed here, **B** = executed against the mock
miner, **C** = not executed (no cloud GPU / no Linux host available).

**A — passed on this Windows host (2026-11-03)**

* Python syntax check for all scripts and tests; `bash -n` for `scripts/prl.sh`
  and `tests/run-tests.sh` (Git Bash).
* `tests/test_units.py` — 44 tests: config/wallet/pool/worker validation,
  argument construction, PCI normalisation, live API JSON parsing (captured
  from the local 1.5.12 miner), Prometheus parsing, log parsing, identity
  verification (including PID-reuse and unreadable-exe refusals), and `prepare`
  refusing hash/size/member/traversal mismatches, refusing an API that reports
  a foreign `miner_name` on our port, and the download path (a local HTTP
  server serves the right and then tampered bytes; the tampered asset is never
  installed).
* `tests/test_lifecycle.py` — 8 tests (mock miner): single instance on double
  start, start→status→stop, share counters and `last_accepted_at`, immediate
  exit detection, API loss, schema error fallback to `/metrics`, refusal to
  signal an unproven PID, start blocked by a missing wallet, stop with nothing
  running.
* `tests/test_host_status.ps1` — 34 checks: every host status mapping, stale
  vs fresh, clock-skew flag, SSH argument policy (BatchMode, strict host key,
  no key material), remote argv quoting.
* `prepare` against the **real** 1.5.12 Linux tarball (92,212,114 bytes):
  SHA-256 matched the manifest; extracted `krig-miner` hash matched; timings on
  this Windows host — hash 228 ms, extract 1,669 ms, total 2,324 ms.
* Asset download from GitHub on this host: 96,809,310 bytes in **1.3 s**
  (≈74 MB/s), SHA-256 equal to GitHub's digest.
* Read-only observation of the live local miner (1.5.12, RTX 5060, Kryptex):
  the `/` JSON and `/metrics` payloads used as fixtures, plus its log lines.
  The local miner, its config and ESI Workbench were not modified.
* `windows\prl-remote.ps1 -Action status/-Action watch` against a closed port:
  exit code 6, `status: unreachable`, the real SSH error recorded, and
  `state/host-telemetry.json` written — the host never invents data it did not
  receive. `-Json` emits the document on stdout.
* `./scripts/prl.sh check` run from the bundle root (Git Bash + Python 3.12,
  `PRL_TEST_MODE=1` because this host is Windows): it resolved the config,
  masked the wallet, detected the real GPU through `nvidia-smi`
  (driver 617.42) and blocked with the expected reasons — missing miner binary,
  no device list yet, and port 12000 held by the unmanaged local miner.

**B — exercised with the mock miner only**

* Every lifecycle path above, the freshness/status mapping, and the telemetry
  document shape (`docs/telemetry-example.json` was generated this way).
* The Windows `deploy`/`start`/`stop`/`logs` SSH plumbing was **not** executed
  against a real host — only its pure functions (config validation, argv
  construction, status mapping) were tested.

**C — not executed (needs your instance)**

* Running `krig-miner` on Linux at all: `--list-devices`, the HTTP API and
  `/metrics` on the 1.5.12 **Linux** build were not observed live. The parser
  is written against the live 1.5.12 Windows payload and tolerates drift
  (schema errors are reported, `/metrics` is used as a fallback).
* Real pool connection and **any accepted share** for a remote instance.
* End-to-end deployment timing on a rented machine, `nvidia-smi` in a rented
  container image, and SSH against a real host key.
* Multi-GPU and AMD/ROCm behaviour.
* Any claim about TH/s on a given rented GPU — that is a measurement, not a
  promise, and it was not measured here.

**Timing honesty:** the 1.3 s download and 2.3 s extraction above are on this
Windows desktop, not on a cloud instance. A remote deploy is expected to be
dominated by the ~97 MB download; **no end-to-end remote deploy time was
measured**, so no 5-minute guarantee is made.

## 11. ESI Workbench integration boundary

Not wired into the UI by this task. `source/PearlMiningModule.cs`,
`runtime/pearl-mining.json`, the local miner and its logs were only read.

What exists for the future integration:

* `state/host-telemetry.json` — the host document described in
  `docs/TELEMETRY-SCHEMA.md`, written on every `status`/`watch` tick.
* `state/last-good.json` — last `ok` document, explicitly *not* live.
* `docs/telemetry-example.json` — a complete example.
* The remote side is a separate object (different machine, own PID space). ESI
  must keep "remote cloud miner" and "local RTX 5060 miner" apart; the host
  script never touches the local miner.

Suggested next step for ESI: a `PearlRemoteMiningAdapter` that shells out to
`windows\prl-remote.ps1 -Action status -Json`, reads the JSON, and maps
`status` to the existing `ObservationKind` values — with `stale`/`unreachable`
shown as "no current data", never as the last hash rate.

The existing `relay/api_relay.py` was inspected and is an OpenAI-compatible
LLM API relay; it is unrelated to mining telemetry and was not reused.

## 12. Deliberately not implemented

Auto-renting/releasing GPUs, auto-trading, auto-withdrawal, coin switching,
wallet creation or modification, pool switching, overclocking/power/fan
changes by default, running the research kernels, pushing to any third-party
repository, or creating a GitHub repository for you.

## 13. Known limitations

* The Linux miner's HTTP schema is inferred from the same 1.5.12 release on
  Windows plus static string checks, not observed live.
* Pool-side hashrate and payouts need Kryptex API credentials that this task
  does not have; they are reported as unavailable rather than estimated.
* `last_accepted_at` comes from the miner's share counter delta (the collector
  notices an increase), so on a freshly started sampler it is `null` until the
  next accepted share; the log-derived timestamp is a fallback that assumes the
  log file's date.
* `last_job_at` is derived from a log line that carries only a wall-clock time;
  the date is inferred from the log file's mtime.
* GPU index from the miner and CUDA index from `nvidia-smi` are joined by PCI
  address first and by index second; a machine where those disagree could
  mislabel temperature/power, never hashrate.
* No systemd unit is shipped (the bundle must work without root); a reboot
  leaves the miner stopped, which the host reports as `miner_exited`.
* Windows PowerShell 5.1 only — PowerShell 7 was not tested on this host.

