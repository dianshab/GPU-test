# What can go into your own GitHub repository

This directory is *not* a git repository yet and nothing was pushed anywhere.
The lists below are the preparation for that step.

## Upload these

```
README.md
UPLOAD-MANIFEST.md
.gitignore
miner-manifest.json
config/prl-mining.example.json
docs/TELEMETRY-SCHEMA.md
docs/telemetry-example.json
scripts/prl.sh
scripts/prl_miner.py
windows/prl-remote.ps1
windows/host.example.json
tests/mock_krig.py
tests/test_units.py
tests/test_lifecycle.py
tests/test_host_status.ps1
tests/run-tests.sh
tests/run-tests.ps1
tests/fixtures/krig-api-root-1.5.12.json
tests/fixtures/krig-metrics-1.5.12.txt
```

Notes:

* `miner-manifest.json` contains only public release URLs and checksums.
* `docs/telemetry-example.json` is generated with the mock miner; it contains a
  masked wallet (`<MASKED_PRL_WALLET>`) and no host names or paths from this
  machine.
* `tests/fixtures/*` are the real `/` and `/metrics` payloads of a krig-miner
  1.5.12 instance (no wallet, no worker, no pool credentials inside).

## Never upload these

| Path | Why |
|---|---|
| `config/prl-mining.local.json` | Your payout address + worker name. Public, but unnecessary to publish; keep it local. |
| `windows/host.local.json` | Your instance address, SSH user, key path and known-hosts path. |
| `state/` | Host-side docs: fetch history, last deploy, error strings from your instance. |
| `logs/` | Any runtime log. |
| `cache/`, `_cache/` | Downloaded miner archives. |
| `bin/krig-miner`, `*.tar.gz`, `*.zip` | ~100 MB binaries. Redistribution *is* allowed by the krig-miner LICENSE ("release binaries may be freely downloaded, redistributed, and run"), but the scripts download and verify the pinned asset anyway, so bundling ~100 MB into git buys nothing. |
| `*.pem`, `id_*`, `*.key`, `.env`, `*.secret` | Keys. This project never creates or needs them, but keep them out by habit. |
| Any wallet private key / mnemonic / seed / wallet file | Never used, never stored, never logged by these scripts. |

## Sanity check before publishing

PowerShell:

```powershell
# no real wallet address
Select-String -Path .\* -Pattern 'prl1' -Recurse |
  Where-Object { $_.Path -notmatch 'local\.json|state\\|logs\\' }
# no private key blocks
Select-String -Path .\* -Pattern 'BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY' -Recurse
# no host paths from this machine
Select-String -Path .\* -Pattern 'C:\\Users\\'
```

bash:

```bash
grep -RIn --exclude-dir=.git --exclude='*.local.json' -E 'prl1|PRIVATE KEY|C:\\Users' .
```

Nothing should match except the masked, documented examples and the
`<PRL_WALLET_ADDRESS>` placeholders in `config/prl-mining.example.json`.
