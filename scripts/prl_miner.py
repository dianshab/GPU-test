#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ESI Pearl (PRL) remote miner controller.

This is the Linux-side half of the ESI Workbench "remote Pearl miner" feature.
It prepares a pinned krig-miner release, verifies GPU/driver prerequisites,
starts exactly one supervised miner per machine, samples the miner's local
HTTP API, and writes a machine-readable telemetry snapshot that the Windows
host reads over SSH.

Design rules enforced here (see ../README.md):
  * one miner process per machine, identified by PID + /proc start time +
    run token + executable path + command line hash; no name-based killing;
  * the miner API is only ever bound to 127.0.0.1;
  * no wallet private key, mnemonic, SSH key or platform API key is used,
    stored, logged or transmitted;
  * miner binaries are pinned by SHA-256 and never installed from a pipe;
  * hardware settings (power limit, fan, clock offsets) are left untouched
    unless an operator sets them explicitly in the config.

Test mode: setting PRL_TEST_MODE=1 relaxes OS/driver/identity checks so the
state machine can be exercised with a mock miner off-GPU. It also enables
PRL_IDENTITY_BACKEND=spawn, a simulated identity provider. Never set these on
a real deployment.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "1.0.0"

CONFIG_SCHEMA = "esi.pearl.config/1"
MANIFEST_SCHEMA = "esi.pearl.miner-manifest/1"
TELEMETRY_SCHEMA = "esi.pearl.telemetry/1"
RUN_SCHEMA = "esi.pearl.run/1"
COLLECTOR_SCHEMA = "esi.pearl.collector/1"
EXIT_SCHEMA = "esi.pearl.exit/1"
PREPARE_SCHEMA = "esi.pearl.prepare/1"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_PREFLIGHT = 3
EXIT_START_FAILED = 4
EXIT_IDENTITY = 5
EXIT_STOP_FAILED = 6
EXIT_API_TIMEOUT = 7

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32_RE = re.compile(r"^prl1[" + BECH32_CHARSET + r"]{20,80}$")
WORKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
PCI_RE = re.compile(r"([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])$")

TEST_MODE = os.environ.get("PRL_TEST_MODE") == "1"


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    """Diagnostics go to stderr so --json output stays parseable."""
    stamp = datetime.now().strftime("%H:%M:%S")
    sys.stderr.write("[%s] %s\n" % (stamp, msg))
    sys.stderr.flush()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    dt = dt or now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def write_json_atomic(path: str, payload: Any, mode: int = 0o644) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def mask_wallet(wallet: str) -> str:
    if not wallet:
        return ""
    if len(wallet) <= 12:
        return wallet[:4] + "***"
    return wallet[:10] + "\u2026" + wallet[-4:]


def human_bytes(n: Optional[float]) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TiB" % n


def human_hs(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    value = float(value)
    for unit, factor in (("TH/s", 1e12), ("GH/s", 1e9), ("MH/s", 1e6), ("kH/s", 1e3)):
        if abs(value) >= factor:
            return "%.2f %s" % (value / factor, unit)
    return "%.0f H/s" % value


def norm_pci(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    m = PCI_RE.search(value.strip())
    if not m:
        return None
    return ("%s:%s.%s" % (m.group(1), m.group(2), m.group(3))).lower()


def pid_alive(pid: Optional[int]) -> bool:
    """Liveness check that never signals the target.

    On Windows os.kill(pid, 0) calls TerminateProcess (it is not a probe), so
    this uses OpenProcess/GetExitCodeProcess there. Production is Linux, where
    signal 0 is a pure existence check.
    """
    if not pid:
        return False
    pid = int(pid)
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel32.CloseHandle(handle)
            return bool(ok) and code.value == STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

class Paths(object):
    def __init__(self, home: str):
        self.home = os.path.abspath(os.path.expanduser(home))
        self.bin = os.path.join(self.home, "bin")
        self.etc = os.path.join(self.home, "etc")
        self.state = os.path.join(self.home, "state")
        self.logs = os.path.join(self.home, "logs")
        self.cache = os.path.join(self.home, "cache")
        self.config = os.path.join(self.etc, "prl-mining.json")
        self.manifest = os.path.join(self.home, "miner-manifest.json")
        self.miner_binary = os.environ.get("PRL_MINER_BINARY") or os.path.join(self.bin, "krig-miner")
        self.telemetry = os.path.join(self.state, "telemetry.json")
        self.run = os.path.join(self.state, "run.json")
        self.collector = os.path.join(self.state, "collector.json")
        self.exit = os.path.join(self.state, "exit.json")
        self.prepare = os.path.join(self.state, "prepare.json")
        self.share_tracker = os.path.join(self.state, "share-tracker.json")
        self.history = os.path.join(self.state, "history.jsonl")
        self.op_lock = os.path.join(self.state, "op.lock")
        self.miner_log = os.path.join(self.logs, "miner.log")
        self.collector_log = os.path.join(self.logs, "collector.log")
        self.supervisor_log = os.path.join(self.logs, "supervisor.log")
        self.prl_log = os.path.join(self.logs, "prl.log")

    def ensure(self) -> None:
        for path in (self.bin, self.etc, self.state, self.logs, self.cache):
            os.makedirs(path, exist_ok=True)


def default_home() -> str:
    """State root.

    PRL_HOME wins. Otherwise, if this file lives in <root>/scripts and <root>
    looks like an installed bundle (has etc/ or miner-manifest.json), use that
    root so `cd <bundle> && ./scripts/prl.sh check` works in place. Falls back
    to ~/esi-pearl.
    """
    env = os.environ.get("PRL_HOME")
    if env:
        return os.path.expanduser(env)
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if os.path.basename(here) == "scripts" and (
            os.path.isfile(os.path.join(root, "etc", "prl-mining.json"))
            or os.path.isfile(os.path.join(root, "miner-manifest.json"))):
        return root
    return os.path.join(os.path.expanduser("~"), "esi-pearl")


# --------------------------------------------------------------------------
# file lock (best effort, never required for correctness of identity checks)
# --------------------------------------------------------------------------

class OpLock(object):
    def __init__(self, path: str):
        self.path = path
        self.fh = None

    def __enter__(self) -> "OpLock":
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            import fcntl  # Linux/POSIX only
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.fh:
                self.fh.close()
        except Exception:
            pass
        self.fh = None


# --------------------------------------------------------------------------
# config + manifest
# --------------------------------------------------------------------------

class ConfigError(Exception):
    pass


def load_manifest(path: str) -> Dict[str, Any]:
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
        raise ConfigError("miner manifest missing or not %s: %s" % (MANIFEST_SCHEMA, path))
    if not isinstance(data.get("miners"), list) or not data["miners"]:
        raise ConfigError("miner manifest has no entries: %s" % path)
    return data


def find_miner_entry(manifest: Dict[str, Any], version: str) -> Dict[str, Any]:
    for entry in manifest["miners"]:
        if str(entry.get("version")) == str(version):
            return entry
    known = ", ".join(str(e.get("version")) for e in manifest["miners"])
    raise ConfigError("miner version %s is not pinned in the manifest (known: %s)" % (version, known))


def _as_int(value: Any, name: str, low: int, high: int) -> int:
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        raise ConfigError("%s must be an integer, got %r" % (name, value))
    if ivalue < low or ivalue > high:
        raise ConfigError("%s must be in [%d, %d], got %d" % (name, low, high, ivalue))
    return ivalue


def _as_opt_int(value: Any, name: str, low: int, high: int) -> Optional[int]:
    if value is None or value == "":
        return None
    return _as_int(value, name, low, high)


def parse_pool_url(url: str) -> Dict[str, Any]:
    """Accept only explicit stratum endpoints. No implicit scheme guessing."""
    if not isinstance(url, str) or not url.strip():
        raise ConfigError("pool_url is empty")
    url = url.strip()
    m = re.match(r"^(stratum\+ssl|stratum\+tcp)://([^/:]+):(\d{1,5})$", url)
    if not m:
        raise ConfigError(
            "pool_url must look like stratum+ssl://host:port or stratum+tcp://host:port, got %r" % url)
    scheme, host, port = m.group(1), m.group(2), int(m.group(3))
    if not (1 <= port <= 65535):
        raise ConfigError("pool_url port out of range: %d" % port)
    return {"raw": url, "scheme": scheme, "host": host, "port": port}


def load_config(path: str, env: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, Any], List[str]]:
    """Load, validate and normalise the miner config. Returns (config, warnings)."""
    env = dict(os.environ if env is None else env)
    raw = read_json(path)
    if raw is None:
        raise ConfigError("config file not found or unreadable: %s" % path)
    if not isinstance(raw, dict):
        raise ConfigError("config file is not a JSON object: %s" % path)

    warnings: List[str] = []
    cfg: Dict[str, Any] = {}

    cfg["schema"] = raw.get("schema", CONFIG_SCHEMA)
    if cfg["schema"] != CONFIG_SCHEMA:
        warnings.append("config schema is %r, expected %r" % (cfg["schema"], CONFIG_SCHEMA))

    wallet = env.get("PRL_WALLET") or raw.get("wallet_address") or ""
    wallet = str(wallet).strip()
    if not wallet or wallet.upper().startswith("<"):
        raise ConfigError("wallet_address is not set (share the PRL *public* payout address only)")
    if not BECH32_RE.match(wallet):
        raise ConfigError(
            "wallet_address does not look like a lowercase bech32 Pearl address "
            "(expected prl1 + bech32 charset, 20-80 body chars); this is a shape check only")
    cfg["wallet_address"] = wallet

    pool_raw = env.get("PRL_POOL_URL") or raw.get("pool_url") or ""
    pool = parse_pool_url(str(pool_raw))
    if "kryptex" in pool["host"].lower() and pool["scheme"] != "stratum+ssl":
        raise ConfigError(
            "Kryptex pools require stratum+ssl (krig-miner --help); refusing to start with %s. "
            "This script will not switch pools automatically." % pool["raw"])
    if "kryptex" not in pool["host"].lower():
        warnings.append(
            "pool host %s is not a Kryptex host; krig-miner applies a developer fee of at least 3%% "
            "outside Kryptex pools and the pool protocol was not verified by this project" % pool["host"])
    cfg["pool"] = pool
    cfg["pool_url"] = pool["raw"]

    worker = env.get("PRL_WORKER") or raw.get("worker_name") or ""
    worker = str(worker).strip()
    if not worker or worker.upper().startswith("<"):
        raise ConfigError("worker_name is not set")
    if not WORKER_RE.match(worker):
        raise ConfigError("worker_name must match [A-Za-z0-9][A-Za-z0-9._-]{0,31}, got %r" % worker)
    cfg["worker_name"] = worker

    cfg["api_port"] = _as_int(env.get("PRL_API_PORT") or raw.get("api_port", 12000), "api_port", 1024, 65535)
    cfg["api_host"] = "127.0.0.1"  # deliberately not configurable

    cfg["miner_version"] = str(raw.get("miner_version", "1.5.12")).strip()

    gpu_select = str(env.get("PRL_GPU_SELECT") or raw.get("gpu_select") or "all").strip()
    if gpu_select != "all":
        parts = [p.strip() for p in gpu_select.split(",") if p.strip()]
        if not parts:
            raise ConfigError("gpu_select is empty")
        for part in parts:
            if not re.match(r"^(\d{1,2}|[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])$", part):
                raise ConfigError("gpu_select entry %r is neither an index nor a PCI address" % part)
        gpu_select = ",".join(parts)
    cfg["gpu_select"] = gpu_select

    cfg["power_limit_w"] = _as_opt_int(raw.get("power_limit_w"), "power_limit_w", 20, 1000)
    cfg["fan_percent"] = _as_opt_int(raw.get("fan_percent"), "fan_percent", 0, 100)
    cfg["core_offset_mhz"] = _as_opt_int(raw.get("core_offset_mhz"), "core_offset_mhz", -2000, 2000)

    cfg["sample_interval_s"] = _as_int(
        env.get("PRL_SAMPLE_INTERVAL_S") or raw.get("sample_interval_s", 10), "sample_interval_s", 2, 300)
    cfg["stale_after_s"] = _as_int(raw.get("stale_after_s", max(45, 3 * cfg["sample_interval_s"])),
                                   "stale_after_s", 10, 3600)
    cfg["api_wait_s"] = _as_int(raw.get("api_wait_s", 60), "api_wait_s", 5, 600)
    cfg["log_max_bytes"] = _as_int(raw.get("log_max_bytes", 20 * 1024 * 1024), "log_max_bytes",
                                  1024 * 1024, 1024 * 1024 * 1024)
    cfg["allow_unverified_gpu"] = bool(raw.get("allow_unverified_gpu", False))
    return cfg, warnings


def build_miner_args(cfg: Dict[str, Any], paths: Paths) -> List[str]:
    """Command line for krig-miner. Verified against krig-miner --help (1.5.12)."""
    args = [
        "--url", cfg["pool_url"],
        "--user", "%s/%s" % (cfg["wallet_address"], cfg["worker_name"]),
        "--api-port", str(cfg["api_port"]),
        "--api-host", "127.0.0.1",
        "--no-tui",
        "--log-level", "info",
    ]
    if cfg["gpu_select"] != "all":
        if re.match(r"^\d", cfg["gpu_select"]):
            args += ["--devices", cfg["gpu_select"]]
        else:
            args += ["--devices-pci", cfg["gpu_select"]]
    # Hardware settings are opt-in only; defaults leave the machine untouched.
    if cfg.get("power_limit_w") is not None:
        args += ["--gpu-plimit", str(cfg["power_limit_w"])]
    if cfg.get("fan_percent") is not None:
        args += ["--gpu-fan", str(cfg["fan_percent"])]
    if cfg.get("core_offset_mhz") is not None:
        args += ["--gpu-coffset", str(cfg["core_offset_mhz"])]
    return args


def mask_args(args: List[str]) -> List[str]:
    """Mask the wallet part of --user for logs/telemetry."""
    out: List[str] = []
    i = 0
    while i < len(args):
        item = args[i]
        if item == "--user" and i + 1 < len(args):
            user = args[i + 1]
            wallet, _, worker = user.partition("/")
            out += [item, "%s/%s" % (mask_wallet(wallet), worker)]
            i += 2
            continue
        out.append(item)
        i += 1
    return out


# --------------------------------------------------------------------------
# process identity
# --------------------------------------------------------------------------

def _proc_root() -> str:
    return os.environ.get("PRL_PROC_ROOT") or "/proc"


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def proc_start_ticks(pid: int) -> Optional[int]:
    text = _read_text(os.path.join(_proc_root(), str(pid), "stat"))
    if not text:
        return None
    try:
        tail = text[text.rindex(")") + 2:].split()
        return int(tail[19])  # field 22 (starttime), 1-based after comm
    except (ValueError, IndexError):
        return None


def proc_exe(pid: int) -> Optional[str]:
    try:
        return os.readlink(os.path.join(_proc_root(), str(pid), "exe"))
    except OSError:
        return None


def proc_cmdline(pid: int) -> Optional[str]:
    data = _read_bytes(os.path.join(_proc_root(), str(pid), "cmdline"))
    if data is None:
        return None
    return data.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def proc_env(pid: int) -> Dict[str, str]:
    data = _read_bytes(os.path.join(_proc_root(), str(pid), "environ"))
    if not data:
        return {}
    env: Dict[str, str] = {}
    for item in data.split(b"\x00"):
        if b"=" in item:
            key, _, value = item.partition(b"=")
            env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def observe_process(pid: Optional[int]) -> Dict[str, Any]:
    """Snapshot what we can prove about a PID.

    Backends:
      proc  - Linux /proc: start time, exe, environ token, cmdline (production)
      spawn - simulated, PRL_TEST_MODE only: reads state/testproc/<pid>.json
    """
    observed: Dict[str, Any] = {
        "pid": pid, "exists": False, "start_ticks": None, "exe": None,
        "exe_readable": False, "run_id": None, "cmdline_sha256": None, "backend": "proc",
    }
    if not pid:
        return observed

    if TEST_MODE and os.environ.get("PRL_IDENTITY_BACKEND") == "spawn":
        fake_path = os.path.join(os.environ.get("PRL_TEST_IDENTITY_DIR", ""), "%d.json" % int(pid))
        fake = read_json(fake_path) if fake_path else None
        observed["backend"] = "spawn"
        if fake and pid_alive(pid):
            observed.update({
                "exists": True,
                "start_ticks": fake.get("start_ticks"),
                "exe": fake.get("exe"),
                "exe_readable": True,
                "run_id": fake.get("run_id"),
                "cmdline_sha256": fake.get("cmdline_sha256"),
            })
        return observed

    if not os.path.isdir(os.path.join(_proc_root(), str(pid))):
        observed["exists"] = pid_alive(pid)
        return observed

    observed["exists"] = True
    observed["start_ticks"] = proc_start_ticks(pid)
    exe = proc_exe(pid)
    if exe:
        observed["exe"] = exe
        observed["exe_readable"] = True
    cmdline = proc_cmdline(pid)
    if cmdline is not None:
        observed["cmdline_sha256"] = sha256_bytes(cmdline.encode("utf-8"))
    env = proc_env(pid)
    observed["run_id"] = env.get("PRL_RUN_ID")
    return observed


def verify_identity(record: Dict[str, Any], observed: Dict[str, Any],
                    require_exe: bool = True) -> Tuple[bool, List[str]]:
    """Compare a stored run record with an observation.

    A process is ours only when the run token matches *and* the kernel start
    time matches *and* (when available) the executable path matches. Missing
    facts are treated as "not proven", never as success.
    """
    problems: List[str] = []
    record_run_id = record.get("run_id")
    record_ticks = record.get("miner_start_ticks", record.get("start_ticks"))
    record_exe = record.get("exe")
    record_cmd = record.get("cmdline_sha256")

    if not observed.get("exists"):
        problems.append("process does not exist")
    if not record_run_id or observed.get("run_id") != record_run_id:
        problems.append("run token mismatch (pid may have been reused)")
    if record_ticks is None:
        problems.append("no recorded kernel start time; identity not provable")
    elif observed.get("start_ticks") != record_ticks:
        problems.append("kernel start time mismatch (pid may have been reused)")
    if require_exe:
        if not observed.get("exe_readable"):
            problems.append("executable path is not readable; identity not provable")
        elif record_exe and observed.get("exe") != record_exe:
            problems.append("executable path mismatch")
    if record_cmd and observed.get("cmdline_sha256") and observed["cmdline_sha256"] != record_cmd:
        problems.append("command line mismatch")
    return (not problems), problems


def make_run_record(run_id: str, pid: int, exe: str, cmdline: List[str],
                    started_at: str, api_port: int, extra: Optional[Dict[str, Any]] = None,
                    start_ticks: Optional[int] = None) -> Dict[str, Any]:
    record = {
        "schema": RUN_SCHEMA,
        "run_id": run_id,
        "miner_pid": pid,
        "miner_start_ticks": proc_start_ticks(pid) if start_ticks is None else start_ticks,
        "exe": os.path.realpath(exe) if exe else None,
        "cmdline_sha256": sha256_bytes(" ".join(cmdline).encode("utf-8")),
        "started_at": started_at,
        "api_port": api_port,
        "written_at": iso(),
    }
    if extra:
        record.update(extra)
    return record


# --------------------------------------------------------------------------
# external commands: GPU discovery
# --------------------------------------------------------------------------

def miner_exec_prefix(path: str) -> List[str]:
    """argv prefix for the miner binary.

    Production: the ELF is executed directly. The offline suite substitutes a
    Python script, which is only honoured under PRL_TEST_MODE.
    """
    if TEST_MODE and path.endswith(".py"):
        return [sys.executable, path]
    return [path]

def run_cmd(args: List[str], timeout: float = 20.0, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, env=env or os.environ.copy())
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.decode("utf-8", "replace"),
            "stderr": proc.stderr.decode("utf-8", "replace"),
            "duration_ms": round((time.time() - started) * 1000.0, 1),
            "error": None,
        }
    except FileNotFoundError:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "",
                "duration_ms": round((time.time() - started) * 1000.0, 1),
                "error": "executable not found: %s" % args[0]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "",
                "duration_ms": round((time.time() - started) * 1000.0, 1),
                "error": "timed out after %.1fs: %s" % (timeout, " ".join(args))}
    except OSError as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "",
                "duration_ms": round((time.time() - started) * 1000.0, 1),
                "error": str(exc)}


DEVICE_LINE_RE = re.compile(r"^\s*\[(\d+)\]\s+(\S+)\s+(.+?)\s*$")
DEVICE_COUNT_RE = re.compile(r"(\d+)\s+device\(s\)")


def parse_list_devices(text: str) -> Dict[str, Any]:
    devices = []
    for line in text.splitlines():
        m = DEVICE_LINE_RE.match(line)
        if m:
            devices.append({
                "index": int(m.group(1)),
                "pci": norm_pci(m.group(2)) or m.group(2),
                "name": m.group(3).strip(),
            })
    declared = None
    m = DEVICE_COUNT_RE.search(text)
    if m:
        declared = int(m.group(1))
    return {"devices": devices, "declared_count": declared}


def list_devices(miner_bin: str, timeout: float = 60.0) -> Dict[str, Any]:
    if not os.path.isfile(miner_bin):
        return {"ok": False, "devices": [], "error": "miner binary not found: %s" % miner_bin,
                "raw": "", "declared_count": None}
    res = run_cmd(miner_exec_prefix(miner_bin) + ["--list-devices"], timeout=timeout)
    parsed = parse_list_devices(res["stdout"])
    out = {
        "ok": res["ok"] and bool(parsed["devices"]),
        "devices": parsed["devices"],
        "declared_count": parsed["declared_count"],
        "raw": res["stdout"].strip(),
        "error": None,
        "duration_ms": res["duration_ms"],
    }
    if not res["ok"]:
        out["error"] = (res["error"] or res["stderr"].strip() or
                        "miner --list-devices exited %s" % res["returncode"])
    elif not parsed["devices"]:
        out["error"] = "miner reported no devices"
    return out


NVSMI_FIELDS = "index,name,uuid,pci.bus_id,driver_version,temperature.gpu,power.draw,utilization.gpu,memory.used,memory.total"


def query_nvidia_smi(timeout: float = 3.0) -> Dict[str, Any]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"ok": False, "gpus": [], "error": "nvidia-smi not found"}
    res = run_cmd([exe, "--query-gpu=" + NVSMI_FIELDS, "--format=csv,noheader,nounits"], timeout=timeout)
    if not res["ok"]:
        return {"ok": False, "gpus": [], "error": (res["error"] or res["stderr"].strip() or "nvidia-smi failed")}
    gpus = []
    for line in res["stdout"].splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(NVSMI_FIELDS.split(",")):
            continue

        def num(value: str) -> Optional[float]:
            try:
                return float(value)
            except ValueError:
                return None

        gpus.append({
            "index": int(parts[0]) if parts[0].isdigit() else None,
            "name": parts[1],
            "uuid": None if parts[2] in ("[N/A]", "N/A") else parts[2],
            "pci": norm_pci(parts[3]),
            "driver": parts[4],
            "temp_c": num(parts[5]),
            "power_w": num(parts[6]),
            "util_pct": num(parts[7]),
            "mem_used_mib": num(parts[8]),
            "mem_total_mib": num(parts[9]),
        })
    return {"ok": bool(gpus), "gpus": gpus, "error": None if gpus else "nvidia-smi returned no rows"}


def rocm_available() -> bool:
    return bool(shutil.which("rocm-smi") or shutil.which("rocminfo"))


# --------------------------------------------------------------------------
# miner HTTP API + metrics
# --------------------------------------------------------------------------

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(url: str, timeout: float = 2.5) -> Dict[str, Any]:
    started = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "esi-pearl/%s" % VERSION})
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read(2 * 1024 * 1024).decode("utf-8", "replace")
            return {"ok": 200 <= resp.status < 300, "status": resp.status, "body": body,
                    "error": None, "latency_ms": round((time.time() - started) * 1000.0, 1)}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "body": "", "error": "HTTP %s" % exc.code,
                "latency_ms": round((time.time() - started) * 1000.0, 1)}
    except Exception as exc:  # URLError, socket.timeout, ConnectionResetError ...
        return {"ok": False, "status": None, "body": "",
                "error": "%s: %s" % (type(exc).__name__, exc),
                "latency_ms": round((time.time() - started) * 1000.0, 1)}


def parse_prometheus(text: str) -> Dict[str, List[Tuple[Dict[str, str], float]]]:
    series: Dict[str, List[Tuple[Dict[str, str], float]]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+(\S+)$", line)
        if not m:
            continue
        name, label_text, value_text = m.group(1), m.group(2) or "", m.group(3)
        labels: Dict[str, str] = {}
        if label_text:
            for lm in re.finditer(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"', label_text):
                labels[lm.group(1)] = lm.group(2)
        try:
            value = float(value_text)
        except ValueError:
            continue
        series.setdefault(name, []).append((labels, value))
    return series


def first_value(series: Dict[str, List[Tuple[Dict[str, str], float]]], name: str,
                labels: Optional[Dict[str, str]] = None) -> Optional[float]:
    for lab, value in series.get(name, []):
        if labels is None or all(lab.get(k) == v for k, v in labels.items()):
            return value
    return None


def parse_miner_api(payload: Any) -> Dict[str, Any]:
    """Parse the krig-miner '/' JSON.

    Observed live on krig-miner 1.5.12 (Windows) as:
      {"miner_name","miner_version","devices":[{"type","index","name","name_short",
       "pci_address","algorithms":[{"name","hashrate","shares":{"accepted","stale","rejected"}}]}]}
    Fields are optional here: anything missing is reported, never invented.
    """
    out: Dict[str, Any] = {
        "miner_name": None, "miner_version": None, "devices": [],
        "algorithm": None, "errors": [], "usable": False,
    }
    if not isinstance(payload, dict):
        out["errors"].append("API payload is not a JSON object")
        return out
    out["miner_name"] = payload.get("miner_name")
    out["miner_version"] = payload.get("miner_version")
    devices = payload.get("devices")
    if not isinstance(devices, list):
        out["errors"].append("missing 'devices' array")
        return out
    if not devices:
        out["errors"].append("'devices' array is empty")
        return out

    for dev in devices:
        if not isinstance(dev, dict):
            continue
        entry = {
            "index": dev.get("index"),
            "name": dev.get("name") or dev.get("name_short"),
            "pci": norm_pci(dev.get("pci_address")),
            "type": dev.get("type"),
            "hashrate_hs": None,
            "algorithm": None,
            "shares": None,
            "error": None,
        }
        algos = dev.get("algorithms")
        algo = None
        if isinstance(algos, list):
            for cand in algos:
                if isinstance(cand, dict) and str(cand.get("name", "")).lower() in ("pearlhash", "pearl", "prl"):
                    algo = cand
                    break
            if algo is None and algos:
                algo = algos[0] if isinstance(algos[0], dict) else None
        if algo is None:
            entry["error"] = "no algorithms[] entry"
        else:
            entry["algorithm"] = algo.get("name")
            if out["algorithm"] is None and algo.get("name"):
                out["algorithm"] = algo.get("name")
            raw = algo.get("hashrate")
            if isinstance(raw, bool):
                entry["error"] = "hashrate is boolean"
            elif isinstance(raw, (int, float)):
                entry["hashrate_hs"] = float(raw)
            elif raw is None:
                entry["error"] = "hashrate missing"
            else:
                entry["error"] = "hashrate is %s, not a number" % type(raw).__name__
            shares = algo.get("shares")
            if isinstance(shares, dict):
                entry["shares"] = {
                    "accepted": shares.get("accepted"),
                    "stale": shares.get("stale"),
                    "rejected": shares.get("rejected"),
                }
        out["devices"].append(entry)

    out["usable"] = any(d.get("hashrate_hs") is not None or d.get("shares") for d in out["devices"])
    return out


def sample_api(cfg: Dict[str, Any], timeout: float = 2.5) -> Dict[str, Any]:
    base = "http://%s:%d" % (cfg["api_host"], cfg["api_port"])
    root = http_get(base + "/", timeout)
    result: Dict[str, Any] = {
        "url": base + "/",
        "status": "unreachable",
        "error": root["error"],
        "latency_ms": root["latency_ms"],
        "payload": None,
    }
    if not root["ok"]:
        return result
    try:
        payload = json.loads(root["body"])
    except ValueError as exc:
        result["status"] = "schema_error"
        result["error"] = "response is not valid JSON: %s" % exc
        return result
    parsed = parse_miner_api(payload)
    result["payload"] = parsed
    reported_name = parsed.get("miner_name")
    if isinstance(reported_name, str) and reported_name and "krig" not in reported_name.lower():
        # Something else is answering on our port. Never adopt its numbers.
        result["status"] = "schema_error"
        result["error"] = "unexpected miner_name %r on this port; refusing to use its data" % reported_name
        return result
    if parsed["usable"]:
        result["status"] = "ok"
        result["error"] = None
    else:
        result["status"] = "schema_error"
        result["error"] = "; ".join(parsed["errors"]) or "no usable device data"
    return result


def sample_metrics(cfg: Dict[str, Any], timeout: float = 2.5) -> Dict[str, Any]:
    url = "http://%s:%d/metrics" % (cfg["api_host"], cfg["api_port"])
    res = http_get(url, timeout)
    if not res["ok"]:
        return {"status": "unreachable", "error": res["error"], "series": {}, "latency_ms": res["latency_ms"]}
    series = parse_prometheus(res["body"])
    return {"status": "ok" if series else "schema_error",
            "error": None if series else "no metric lines parsed",
            "series": series, "latency_ms": res["latency_ms"]}


# --------------------------------------------------------------------------
# log helpers
# --------------------------------------------------------------------------

LOG_TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s+(.*)$")


def tail_lines(path: str, max_bytes: int = 65536, max_lines: int = 400) -> List[str]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    return lines[-max_lines:]


def _log_time_to_utc(hh: int, mm: int, ss: int, ms: int, anchor: datetime) -> Optional[datetime]:
    """krig-miner log lines carry only a wall-clock time. Infer the date from the
    log file's mtime and treat future-looking times as the previous day."""
    if hh > 23 or mm > 59 or ss > 59:
        return None
    candidate = anchor.replace(hour=hh, minute=mm, second=ss, microsecond=ms * 1000)
    if candidate - anchor > timedelta(minutes=2):
        candidate -= timedelta(days=1)
    return candidate


def parse_miner_log(path: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Best-effort facts from the miner log. All values may be None."""
    now = now or now_utc()
    try:
        anchor = datetime.fromtimestamp(os.path.getmtime(path), tz=now.tzinfo)
    except OSError:
        return {"available": False, "error": "log file not available",
                "last_job_at": None, "last_connected_at": None, "devfee_percent": None,
                "last_error": None, "share_lines": 0}
    lines = tail_lines(path)
    last_job = None
    last_connected = None
    devfee = None
    last_error = None
    last_accepted_line = None
    for line in lines:
        m = LOG_TIME_RE.match(line)
        stamp = _log_time_to_utc(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), anchor) if m else None
        body = m.group(5) if m else line
        if "connected:" in body and "stratum" in body:
            last_connected = stamp
        if "stratum: new job" in body:
            last_job = stamp
        if "devfee" in body:
            fm = re.search(r"devfee\s+([0-9]+(?:\.[0-9]+)?)\s*%", body)
            if fm:
                devfee = float(fm.group(1))
        if "share accepted" in body:
            last_accepted_line = stamp
        low = body.lower()
        if any(tok in low for tok in ("error", "failed", "panic", "reconnect", "disconnect")):
            last_error = line.strip()[:300]
    return {
        "available": True,
        "last_job_at": iso(last_job) if last_job else None,
        "last_connected_at": iso(last_connected) if last_connected else None,
        "last_accepted_log_at": iso(last_accepted_line) if last_accepted_line else None,
        "devfee_percent": devfee,
        "last_error": last_error,
        "share_lines": len(lines),
    }


# --------------------------------------------------------------------------
# telemetry sampling
# --------------------------------------------------------------------------

class Sampler(object):
    def __init__(self, cfg: Dict[str, Any], paths: Paths):
        self.cfg = cfg
        self.paths = paths
        self._devices_cache: Optional[Dict[str, Any]] = None
        self._devices_at = 0.0

    def devices(self, force: bool = False, ttl_s: float = 60.0) -> Dict[str, Any]:
        """GPU inventory from the miner itself.

        Cached for ttl_s because --list-devices initialises CUDA and costs
        hundreds of milliseconds; a GPU falling off the bus is then visible
        within ttl_s. Force a refresh after start/stop.
        """
        now = time.time()
        if not force and self._devices_cache is not None and (now - self._devices_at) < ttl_s:
            return self._devices_cache
        self._devices_cache = list_devices(self.paths.miner_binary, timeout=20.0)
        self._devices_at = now
        return self._devices_cache

    # -- identity ---------------------------------------------------------
    def miner_state(self) -> Dict[str, Any]:
        record = read_json(self.paths.run, None) or {}
        if not record.get("miner_pid"):
            return {"process_state": "not_started", "identity": "none", "record": {},
                    "observed": {}, "problems": []}
        observed = observe_process(record.get("miner_pid"))
        ok, problems = verify_identity(record, observed)
        if not observed.get("exists"):
            state = "exited"
        elif ok:
            state = "running"
        else:
            state = "unknown"
        return {"process_state": state,
                "identity": "verified" if ok else ("failed" if observed.get("exists") else "process_gone"),
                "record": record, "observed": observed, "problems": problems}

    def share_tracker(self, samples: List[Dict[str, Any]], sampled_at: str) -> Dict[str, Any]:
        previous = read_json(self.paths.share_tracker, None) or {}
        accepted_total = 0
        have_shares = False
        for dev in samples:
            shares = dev.get("shares") or {}
            if isinstance(shares, dict) and isinstance(shares.get("accepted"), (int, float)):
                accepted_total += int(shares["accepted"])
                have_shares = True
        last_accepted_at = previous.get("last_accepted_at")
        prev_total = previous.get("accepted_total")
        if have_shares:
            if isinstance(prev_total, (int, float)) and accepted_total > prev_total:
                last_accepted_at = sampled_at
            elif prev_total is None and accepted_total == 0:
                last_accepted_at = None
        tracker = {
            "accepted_total": accepted_total if have_shares else None,
            "last_accepted_at": last_accepted_at,
            "last_sample_at": sampled_at,
            "previous_total": prev_total,
        }
        return tracker

    # -- one sample -------------------------------------------------------
    def sample(self, write: bool = True, update_tracker: bool = True) -> Dict[str, Any]:
        started = now_utc()
        sampled_at = iso(started)
        cfg = self.cfg
        unavailable: List[str] = []
        errors: List[str] = []

        state = self.miner_state()
        # Only trust the loopback API when the process answering it is ours.
        # Otherwise a foreign miner on the same port would be reported as ours.
        query_api = state["process_state"] == "running"
        if query_api:
            api = sample_api(cfg)
            metrics = sample_metrics(cfg)
        else:
            reason = "miner process is %s; local API not queried" % state["process_state"]
            api = {"url": "http://%s:%d/" % (cfg["api_host"], cfg["api_port"]),
                   "status": "skipped", "error": reason, "latency_ms": None, "payload": None}
            metrics = {"status": "skipped", "error": reason, "series": {}, "latency_ms": None}
        smi = query_nvidia_smi()
        log_info = parse_miner_log(self.paths.miner_log, started)

        api_devices: List[Dict[str, Any]] = []
        miner_name = None
        miner_version = None
        algorithm = None
        if api["status"] == "ok" and api["payload"]:
            payload = api["payload"]
            api_devices = payload["devices"]
            miner_name = payload.get("miner_name")
            miner_version = payload.get("miner_version")
            algorithm = payload.get("algorithm")

        metrics_series: Dict[str, List[Tuple[Dict[str, str], float]]] = metrics.get("series") or {}

        # per-GPU hashrate: API first, metrics second
        per_gpu: List[Dict[str, Any]] = []
        for dev in api_devices:
            index = dev.get("index")
            hs = dev.get("hashrate_hs")
            source = "miner_api" if hs is not None else None
            if hs is None and index is not None:
                hs = first_value(metrics_series, "krig_miner_hashes_per_second", {"gpu": str(index)})
                if hs is not None:
                    source = "miner_metrics"
            per_gpu.append({
                "index": index,
                "pci": dev.get("pci"),
                "name": dev.get("name"),
                "hashrate_hs": hs,
                "hashrate_source": source,
                "shares": dev.get("shares"),
                "error": dev.get("error"),
            })
        if not per_gpu:
            for label, value in metrics_series.get("krig_miner_hashes_per_second", []):
                gpu_label = label.get("gpu")
                index = int(gpu_label) if gpu_label and gpu_label.isdigit() else None
                per_gpu.append({"index": index, "pci": None, "name": None,
                                "hashrate_hs": value, "hashrate_source": "miner_metrics",
                                "shares": None, "error": None})

        # GPU inventory from the miner itself (independent of the API)
        devices_now = self.devices()
        detected = devices_now.get("devices") or []

        smi_by_pci = {}
        smi_by_index = {}
        if smi.get("ok"):
            for gpu in smi["gpus"]:
                if gpu.get("pci"):
                    smi_by_pci[gpu["pci"]] = gpu
                if gpu.get("index") is not None:
                    smi_by_index[gpu["index"]] = gpu

        gpu_rows: List[Dict[str, Any]] = []
        for dev in detected:
            row = {
                "index": dev["index"],
                "pci": dev["pci"],
                "name": dev["name"],
                "uuid": None,
                "driver": None,
                "temp_c": None,
                "power_w": None,
                "util_pct": None,
                "mem_used_mib": None,
                "mem_total_mib": None,
            }
            match = smi_by_pci.get(dev["pci"]) if dev["pci"] else None
            if match is None and dev["index"] in smi_by_index:
                match = smi_by_index[dev["index"]]
            if match:
                row.update({
                    "uuid": match.get("uuid"),
                    "driver": match.get("driver"),
                    "temp_c": match.get("temp_c"),
                    "power_w": match.get("power_w"),
                    "util_pct": match.get("util_pct"),
                    "mem_used_mib": match.get("mem_used_mib"),
                    "mem_total_mib": match.get("mem_total_mib"),
                })
            gpu_rows.append(row)

        if not detected:
            unavailable.append("gpu_inventory")
            errors.append("GPU enumeration returned no devices: %s" % (devices_now.get("error") or "unknown"))
        if not smi.get("ok"):
            unavailable.append("gpu_temperature_power")
            errors.append("nvidia-smi unavailable: %s" % smi.get("error"))
        if not any(row.get("hashrate_hs") is not None for row in per_gpu):
            unavailable.append("per_gpu_hashrate")
        if api["status"] == "skipped":
            unavailable.append("miner_api_not_queried")
        elif api["status"] != "ok":
            unavailable.append("miner_api_hashrate")

        total_hs = None
        values = [row["hashrate_hs"] for row in per_gpu if row.get("hashrate_hs") is not None]
        if values:
            total_hs = float(sum(values))

        pool_connected = first_value(metrics_series, "krig_miner_pool_connected")
        connected: Optional[bool] = None
        if pool_connected is not None:
            try:
                connected = bool(int(pool_connected))
            except (TypeError, ValueError):
                connected = None
        pool_latency = first_value(metrics_series, "krig_miner_pool_latency_ms")
        heartbeat = first_value(metrics_series, "krig_miner_heartbeat_age_seconds")
        git_sha = None
        for labels, _value in metrics_series.get("krig_miner_info", []):
            git_sha = labels.get("git_sha") or git_sha

        shares = {"accepted": None, "stale": None, "rejected": None, "available": False}
        accepted_sum = stale_sum = rejected_sum = 0
        any_shares = False
        for dev in api_devices:
            s = dev.get("shares")
            if isinstance(s, dict) and any(isinstance(s.get(k), (int, float)) for k in ("accepted", "stale", "rejected")):
                any_shares = True
                accepted_sum += int(s.get("accepted") or 0)
                stale_sum += int(s.get("stale") or 0)
                rejected_sum += int(s.get("rejected") or 0)
        if any_shares:
            shares = {"accepted": accepted_sum, "stale": stale_sum, "rejected": rejected_sum,
                      "available": True, "source": "miner_api"}
        elif metrics_series.get("krig_miner_blocks_submitted_total"):
            agg = {}
            for labels, value in metrics_series["krig_miner_blocks_submitted_total"]:
                result = labels.get("result")
                agg[result] = agg.get(result, 0) + int(value)
            if agg:
                shares = {"accepted": agg.get("accepted"), "stale": agg.get("stale"),
                          "rejected": agg.get("rejected"), "available": True, "source": "miner_metrics"}
        if not shares["available"]:
            unavailable.append("shares")
        shares["last_accepted_at"] = log_info.get("last_accepted_log_at")

        tracker = self.share_tracker(api_devices, sampled_at)
        if tracker.get("last_accepted_at"):
            shares["last_accepted_at"] = tracker["last_accepted_at"]
        if write and update_tracker:
            try:
                write_json_atomic(self.paths.share_tracker, tracker)
            except OSError as exc:
                errors.append("cannot persist share tracker: %s" % exc)

        record = state["record"]
        uptime = None
        started_at = record.get("started_at")
        if started_at:
            started_dt = parse_iso(started_at)
            if started_dt:
                uptime = round((started - started_dt).total_seconds(), 1)

        exit_info = read_json(self.paths.exit, None)
        instance_id = machine_id()

        previous_telemetry = read_json(self.paths.telemetry, {})
        seq = 1
        if isinstance(previous_telemetry, dict) and isinstance(previous_telemetry.get("seq"), int):
            seq = previous_telemetry["seq"] + 1

        telemetry = {
            "schema": TELEMETRY_SCHEMA,
            "collected_by": {"name": "esi-pearl", "version": VERSION},
            "seq": seq,
            "sampled_at": sampled_at,
            "sample_interval_s": cfg["sample_interval_s"],
            "stale_after_s": cfg["stale_after_s"],
            "instance": {
                "machine_id": instance_id,
                "hostname": socket.gethostname(),
                "os": os_release_pretty(),
                "kernel": platform.release(),
                "arch": platform.machine(),
            },
            "miner": {
                "name": miner_name or (read_json(self.paths.prepare, {}) or {}).get("miner_name") or "krig-miner",
                "version": miner_version or (read_json(self.paths.prepare, {}) or {}).get("miner_version"),
                "git_sha": git_sha,
                "algorithm": algorithm,
                "pid": record.get("miner_pid"),
                "run_id": record.get("run_id"),
                "started_at": started_at,
                "uptime_s": uptime,
                "process_state": state["process_state"],
                "identity": state["identity"],
                "identity_problems": state["problems"],
                "args_masked": record.get("args_masked"),
                "exit": None if not exit_info else {
                    "code": exit_info.get("exit_code"),
                    "signal": exit_info.get("signal"),
                    "at": exit_info.get("exited_at"),
                },
            },
            "pool": {
                "url": cfg["pool_url"],
                "worker": cfg["worker_name"],
                "connected": connected,
                "latency_ms": pool_latency,
                "heartbeat_age_s": heartbeat,
                "last_job_at": log_info.get("last_job_at"),
                "last_connected_at": log_info.get("last_connected_at"),
                "devfee_percent": log_info.get("devfee_percent"),
            },
            "hashrate": {
                "total_hs": total_hs,
                "unit": "H/s",
                "source": ("miner_api" if any(r["hashrate_source"] == "miner_api" for r in per_gpu)
                           else ("miner_metrics" if values else None)),
                "per_gpu": per_gpu,
                "per_gpu_available": bool(values),
                "note": "miner-local hashrate; not a pool estimate and not a payout figure",
            },
            "shares": shares,
            "gpu": {
                "count": len(gpu_rows),
                "detected": gpu_rows,
                "source": "krig-miner --list-devices" + (" + nvidia-smi" if smi.get("ok") else ""),
                "telemetry_source": "nvidia-smi" if smi.get("ok") else None,
            },
            "api": {"url": api["url"], "status": api["status"], "error": api["error"],
                    "latency_ms": api["latency_ms"]},
            "metrics_api": {"status": metrics["status"], "error": metrics["error"]},
            "log": {"last_error": log_info.get("last_error"), "available": log_info.get("available")},
            "unavailable": sorted(set(unavailable)),
            "errors": errors,
            "pool_side": {
                "hashrate_estimate": None,
                "payouts": None,
                "note": "pool-side hashrate and wallet payouts are not available to this tool; "
                        "only the miner's local counters are reported",
            },
        }
        if write:
            write_json_atomic(self.paths.telemetry, telemetry)
        return telemetry


def machine_id() -> str:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        text = _read_text(path)
        if text and text.strip():
            return text.strip()[:32]
    return "host-" + sha256_bytes(socket.gethostname().encode("utf-8"))[:16]


def os_release_pretty() -> str:
    text = _read_text("/etc/os-release") or ""
    for line in text.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return platform.system() + " " + platform.release()


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------

def detect_gpu_support() -> Dict[str, Any]:
    smi = query_nvidia_smi()
    if smi.get("ok"):
        return {"kind": "cuda", "verified": True, "detail": "nvidia-smi reports %d GPU(s), driver %s"
                % (len(smi["gpus"]), smi["gpus"][0].get("driver"))}
    if rocm_available():
        return {"kind": "rocm", "verified": False,
                "detail": "ROCm tooling present; AMD/ROCm Pearl mining was not verified by this project"}
    return {"kind": "none", "verified": False, "detail": "neither nvidia-smi nor ROCm tooling found"}


def port_free(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def run_checks(cfg: Dict[str, Any], paths: Paths, manifest: Dict[str, Any],
               for_start: bool = False) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, fatal: bool = True) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "fatal": fatal})

    linux = sys.platform.startswith("linux")
    add("platform", linux or TEST_MODE,
        "linux/%s" % platform.machine() if linux else "not Linux (%s/%s)" % (sys.platform, platform.machine()))
    add("python", sys.version_info >= (3, 8), "Python %d.%d.%d" % sys.version_info[:3])

    entry = None
    try:
        entry = find_miner_entry(manifest, cfg["miner_version"])
        add("miner_pinned", True, "krig-miner %s (%s)" % (entry["version"], entry["sha256"][:12]))
    except ConfigError as exc:
        add("miner_pinned", False, str(exc))

    miner_ok = os.path.isfile(paths.miner_binary) and os.access(paths.miner_binary, os.X_OK)
    if miner_ok and entry:
        try:
            actual = sha256_file(paths.miner_binary)
        except OSError as exc:
            actual = None
            add("miner_hash", False, "cannot hash miner binary: %s" % exc)
        else:
            add("miner_hash", actual == entry["binary_sha256"],
                "sha256 %s%s" % (actual[:16], "" if actual == entry["binary_sha256"] else " (expected %s)" % entry["binary_sha256"][:16]))
    elif not miner_ok:
        add("miner_hash", False, "miner binary missing or not executable: %s (run prepare)" % paths.miner_binary)

    devices = list_devices(paths.miner_binary) if miner_ok else {"ok": False, "devices": [],
                                                                 "error": "miner binary not installed"}
    detected = devices.get("devices") or []
    if detected:
        detail = "%d GPU(s): %s" % (len(detected),
                                    ", ".join("%s %s" % (d["pci"], d["name"]) for d in detected))
    else:
        detail = "no GPU enumerated (%s)" % (devices.get("error") or "unknown")
    add("gpu_detected", bool(detected), detail)

    support = detect_gpu_support()
    gpu_ok = support["kind"] == "cuda" or cfg.get("allow_unverified_gpu") or TEST_MODE
    add("gpu_support", gpu_ok,
        "%s: %s%s" % (support["kind"], support["detail"],
                      "" if gpu_ok else " (set allow_unverified_gpu=true only if you accept the risk)"))

    port_busy = not port_free(cfg["api_host"], cfg["api_port"])
    if port_busy:
        record = read_json(paths.run, None) or {}
        observed = observe_process(record.get("miner_pid")) if record.get("miner_pid") else {}
        ok, _problems = verify_identity(record, observed) if observed else (False, [])
        add("api_port", ok,
            "port %d is held by our verified miner (pid %s)" % (cfg["api_port"], record.get("miner_pid"))
            if ok else "port %d is already in use by an unmanaged process" % cfg["api_port"])
    else:
        add("api_port", True, "port %d is free on 127.0.0.1" % cfg["api_port"])

    try:
        os.makedirs(paths.logs, exist_ok=True)
        usage = shutil.disk_usage(paths.home)
        enough = usage.free > 1.5 * 1024 * 1024 * 1024
        add("disk_space", enough, "%s free under %s" % (human_bytes(usage.free), paths.home))
    except OSError as exc:
        add("disk_space", False, "cannot inspect %s: %s" % (paths.home, exc))

    if for_start:
        state = Sampler(cfg, paths).miner_state()
        running = state["process_state"] == "running"
        add("single_instance", True,
            "a verified instance is already running (pid %s)" % state["record"].get("miner_pid")
            if running else "no verified miner instance is running", fatal=False)

    fatal_fail = [c for c in checks if c["fatal"] and not c["ok"]]
    return {
        "ok": not fatal_fail,
        "checks": checks,
        "gpus": devices.get("devices") or [],
        "gpu_support": support,
        "failed": [c["name"] for c in fatal_fail],
    }


# --------------------------------------------------------------------------
# log rotation
# --------------------------------------------------------------------------

def rotate_log(path: str, max_bytes: int) -> Optional[str]:
    try:
        if os.path.isfile(path) and os.path.getsize(path) > max_bytes:
            backup = path + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(path, backup)
            return backup
    except OSError as exc:
        log("warning: log rotation failed for %s: %s" % (path, exc))
    return None


# --------------------------------------------------------------------------
# subprocess launcher
# --------------------------------------------------------------------------

def detach_kwargs() -> Dict[str, Any]:
    if hasattr(os, "setsid"):
        return {"start_new_session": True}
    if os.name == "nt":  # test-only path
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {}


def spawn_detached(args: List[str], log_path: str, env: Optional[Dict[str, str]] = None) -> int:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    handle = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=handle,
                                stderr=subprocess.STDOUT, close_fds=True,
                                env=env or os.environ.copy(), **detach_kwargs())
    finally:
        handle.close()
    return proc.pid


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def resolve_config_path(args: argparse.Namespace, paths: Paths) -> str:
    """Config lookup: --config, then $PRL_HOME/etc/prl-mining.json, then the
    source-tree config/ directory (so the bundle works both in place and after
    `deploy` has laid it out under the state root)."""
    if args.config:
        return os.path.abspath(args.config)
    if os.path.isfile(paths.config):
        return paths.config
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (os.path.join(root, "config", "prl-mining.local.json"),
                      os.path.join(root, "config", "prl-mining.example.json")):
        if os.path.isfile(candidate):
            return candidate
    return paths.config


def prepare_manifest_path(args: argparse.Namespace, paths: Paths) -> str:
    if args.manifest:
        return os.path.abspath(args.manifest)
    if os.path.isfile(paths.manifest):
        return paths.manifest
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(here, "miner-manifest.json")
    return candidate


def cmd_prepare(args: argparse.Namespace, paths: Paths) -> int:
    t0 = time.time()
    timings: Dict[str, float] = {}
    paths.ensure()
    manifest = load_manifest(prepare_manifest_path(args, paths))
    version = args.miner_version
    if not version:
        try:
            cfg, warnings = load_config(resolve_config_path(args, paths))
            for w in warnings:
                log("warning: " + w)
            version = cfg["miner_version"]
        except ConfigError as exc:
            # prepare only needs to know which pinned asset to install; a missing
            # wallet must not block pre-baking an image, but the default is explicit.
            version = manifest.get("default_version")
            log("note: %s; using manifest default miner version %s" % (exc, version))
    entry = find_miner_entry(manifest, version)
    timings["config_ms"] = round((time.time() - t0) * 1000.0, 1)

    if not TEST_MODE:
        if not sys.platform.startswith("linux"):
            log("error: prepare targets Linux; this host is %s" % sys.platform)
            return EXIT_CONFIG
        if platform.machine().lower() not in ("x86_64", "amd64"):
            log("error: only linux-x86_64 assets are pinned; host arch is %s" % platform.machine())
            return EXIT_CONFIG

    if not args.skip_gpu_check:
        devices = list_devices(paths.miner_binary) if os.path.isfile(paths.miner_binary) else {"devices": []}
        smi = query_nvidia_smi()
        if not devices.get("devices") and not (smi.get("ok") and smi.get("gpus")):
            if not TEST_MODE:
                log("error: no GPU detected (krig-miner --list-devices and nvidia-smi both empty)")
                return EXIT_PREFLIGHT
        timings["gpu_check_ms"] = round((time.time() - t0) * 1000.0, 1)

    asset_path = None
    if args.asset_file:
        asset_path = os.path.abspath(args.asset_file)
        if not os.path.isfile(asset_path):
            log("error: --asset-file not found: %s" % asset_path)
            return EXIT_PREFLIGHT
    else:
        asset_path = os.path.join(paths.cache, entry["asset"])
        need_download = True
        if os.path.isfile(asset_path):
            if sha256_file(asset_path) == entry["sha256"]:
                log("using cached asset %s (sha256 verified)" % asset_path)
                need_download = False
            else:
                log("cached asset hash mismatch, re-downloading")
                os.remove(asset_path)
        if need_download:
            if not args.allow_download:
                log("error: asset missing and --allow-download not given: %s" % asset_path)
                return EXIT_PREFLIGHT
            log("downloading %s" % entry["url"])
            started = time.time()
            try:
                req = urllib.request.Request(entry["url"], headers={"User-Agent": "esi-pearl/%s" % VERSION})
                with _OPENER.open(req, timeout=args.download_timeout) as resp, open(asset_path + ".part", "wb") as fh:
                    shutil.copyfileobj(resp, fh, 1024 * 256)
                os.replace(asset_path + ".part", asset_path)
            except Exception as exc:
                log("error: download failed: %s: %s" % (type(exc).__name__, exc))
                try:
                    if os.path.exists(asset_path + ".part"):
                        os.remove(asset_path + ".part")
                except OSError:
                    pass
                return EXIT_PREFLIGHT
            timings["download_ms"] = round((time.time() - started) * 1000.0, 1)

    size = os.path.getsize(asset_path)
    if entry.get("size_bytes") and size != entry["size_bytes"]:
        log("error: asset size %d != pinned %d: %s" % (size, entry["size_bytes"], asset_path))
        return EXIT_PREFLIGHT
    started = time.time()
    digest = sha256_file(asset_path)
    timings["hash_ms"] = round((time.time() - started) * 1000.0, 1)
    if digest != entry["sha256"]:
        log("error: SHA-256 mismatch for %s\n  expected %s\n  actual   %s\nrefusing to extract"
            % (asset_path, entry["sha256"], digest))
        return EXIT_PREFLIGHT
    timings["verify_ms"] = timings["hash_ms"]

    started = time.time()
    try:
        with tarfile.open(asset_path, "r:gz") as tar:
            members = {m.name: m for m in tar.getmembers()}
            for name, member in members.items():
                if member.name.startswith("/") or ".." in member.name.split("/"):
                    log("error: refusing unsafe archive member %r" % name)
                    return EXIT_PREFLIGHT
            wanted = [m["path"] for m in entry["members"]]
            missing = [w for w in wanted if w not in members]
            if missing:
                log("error: archive is missing members: %s" % ", ".join(missing))
                return EXIT_PREFLIGHT
            for name in wanted:
                target = os.path.join(paths.bin, name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with tar.extractfile(members[name]) as src, open(target + ".tmp", "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(target + ".tmp", 0o755 if name == entry["binary"] else 0o644)
                os.replace(target + ".tmp", target)
    except (tarfile.TarError, OSError) as exc:
        log("error: extraction failed: %s" % exc)
        return EXIT_PREFLIGHT
    timings["extract_ms"] = round((time.time() - started) * 1000.0, 1)

    for member in entry["members"]:
        target = os.path.join(paths.bin, member["path"])
        if sha256_file(target) != member["sha256"]:
            log("error: extracted %s hash mismatch" % member["path"])
            return EXIT_PREFLIGHT

    timings["total_ms"] = round((time.time() - t0) * 1000.0, 1)
    record = {
        "schema": PREPARE_SCHEMA,
        "miner_name": entry["name"],
        "miner_version": entry["version"],
        "asset": entry["asset"],
        "asset_sha256": entry["sha256"],
        "binary_sha256": entry["binary_sha256"],
        "binary_path": paths.miner_binary,
        "prepared_at": iso(),
        "timings": timings,
        "host": {"os": os_release_pretty(), "kernel": platform.release(), "arch": platform.machine()},
        "download_source": entry["url"],
    }
    write_json_atomic(paths.prepare, record)
    log("prepared krig-miner %s in %.2fs -> %s" % (entry["version"], timings["total_ms"] / 1000.0, paths.miner_binary))
    if args.json:
        print(json.dumps({"ok": True, "prepare": record}, indent=2))
    return EXIT_OK


def cmd_check(args: argparse.Namespace, paths: Paths) -> int:
    paths.ensure()
    manifest = load_manifest(prepare_manifest_path(args, paths))
    cfg, warnings = load_config(resolve_config_path(args, paths))
    result = run_checks(cfg, paths, manifest, for_start=False)
    prepared = read_json(paths.prepare, None) or {}
    payload = {
        "ok": result["ok"],
        "checks": result["checks"],
        "gpus": result["gpus"],
        "gpu_support": result["gpu_support"],
        "warnings": warnings,
        "config": {
            "pool_url": cfg["pool_url"],
            "worker_name": cfg["worker_name"],
            "wallet_masked": mask_wallet(cfg["wallet_address"]),
            "api": "%s:%d" % (cfg["api_host"], cfg["api_port"]),
            "miner_version": cfg["miner_version"],
            "gpu_select": cfg["gpu_select"],
            "sample_interval_s": cfg["sample_interval_s"],
            "hardware_overrides": {k: cfg[k] for k in ("power_limit_w", "fan_percent", "core_offset_mhz")
                                   if cfg[k] is not None},
        },
        "prepared": {"miner_version": prepared.get("miner_version"), "prepared_at": prepared.get("prepared_at")},
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print_check_human(payload)
    return EXIT_OK if payload["ok"] else EXIT_PREFLIGHT


def print_check_human(payload: Dict[str, Any]) -> None:
    print("Pearl remote miner preflight")
    for check in payload["checks"]:
        print("  [%s] %-16s %s" % ("ok" if check["ok"] else "FAIL", check["name"], check["detail"]))
    for gpu in payload["gpus"]:
        print("  GPU %s %s" % (gpu.get("pci"), gpu.get("name")))
    for warning in payload["warnings"]:
        print("  warning: " + warning)
    print("  pool    %s" % payload["config"]["pool_url"])
    print("  worker  %s" % payload["config"]["worker_name"])
    print("  wallet  %s (public address, masked)" % payload["config"]["wallet_masked"])
    print("RESULT: %s" % ("ready" if payload["ok"] else "blocked: " + ", ".join(
        c["name"] for c in payload["checks"] if c["fatal"] and not c["ok"])))


def _collector_identity(paths: Paths) -> Dict[str, Any]:
    return read_json(paths.collector, None) or {}


def cmd_start(args: argparse.Namespace, paths: Paths) -> int:
    paths.ensure()
    manifest = load_manifest(prepare_manifest_path(args, paths))
    cfg, warnings = load_config(resolve_config_path(args, paths))
    for w in warnings:
        log("warning: " + w)

    with OpLock(paths.op_lock):
        sampler = Sampler(cfg, paths)
        state = sampler.miner_state()
        if state["process_state"] == "running":
            payload = {"ok": True, "already_running": True, "pid": state["record"].get("miner_pid"),
                       "telemetry": None}
            log("a verified miner instance is already running (pid %s); not starting another"
                % state["record"].get("miner_pid"))
            if args.json:
                print(json.dumps(payload, indent=2))
            return EXIT_OK

        checks = run_checks(cfg, paths, manifest, for_start=True)
        if not checks["ok"]:
            log("preflight failed: %s" % ", ".join(checks["failed"]))
            for check in checks["checks"]:
                if check["fatal"] and not check["ok"]:
                    log("  - %s: %s" % (check["name"], check["detail"]))
            if args.json:
                print(json.dumps({"ok": False, "stage": "preflight", "checks": checks["checks"]}, indent=2))
            return EXIT_PREFLIGHT

        rotate_log(paths.miner_log, cfg["log_max_bytes"])
        rotate_log(paths.collector_log, cfg["log_max_bytes"])
        rotate_log(paths.supervisor_log, cfg["log_max_bytes"])
        rotate_log(paths.history, 5 * 1024 * 1024)

        for stale in (paths.exit, paths.run, paths.collector):
            try:
                os.remove(stale)
            except OSError:
                pass

        run_id = "%s-%s" % (time.strftime("%Y%m%dT%H%M%S"), os.urandom(4).hex())
        config_path = os.path.abspath(resolve_config_path(args, paths))
        supervisor_args = [sys.executable, os.path.abspath(__file__), "--home", paths.home,
                           "_supervise", "--run-id", run_id, "--config", config_path]
        if args.manifest:
            supervisor_args += ["--manifest", os.path.abspath(args.manifest)]
        t_launch = time.time()
        sup_pid = spawn_detached(supervisor_args, paths.supervisor_log)
        log("supervisor started (pid %d), waiting for the miner process" % sup_pid)

        record = None
        deadline = time.time() + 20
        while time.time() < deadline:
            record = read_json(paths.run, None)
            if record and record.get("run_id") == run_id:
                break
            if not pid_alive(sup_pid):
                break
            time.sleep(0.25)

        if not record or record.get("run_id") != run_id:
            exit_info = read_json(paths.exit, None) or {}
            log("error: miner did not start (supervisor pid %d alive=%s, exit=%s)"
                % (sup_pid, pid_alive(sup_pid), exit_info.get("exit_code")))
            if args.json:
                print(json.dumps({"ok": False, "stage": "launch", "exit": exit_info}, indent=2))
            return EXIT_START_FAILED

        collector_pid = None
        if not args.no_collector:
            collector_args = [sys.executable, os.path.abspath(__file__), "--home", paths.home,
                              "_collect", "--config", config_path]
            if args.manifest:
                collector_args += ["--manifest", os.path.abspath(args.manifest)]
            collector_pid = spawn_detached(collector_args, paths.collector_log)

        # Wait for the local API to answer, but stop early if the miner dies.
        api_deadline = time.time() + cfg["api_wait_s"]
        api_status = None
        while time.time() < api_deadline:
            if not pid_alive(record.get("miner_pid")):
                exit_info = read_json(paths.exit, None) or {}
                log("error: miner exited during startup (code %s)" % exit_info.get("exit_code"))
                sampler.sample(write=True, update_tracker=False)
                if args.json:
                    print(json.dumps({"ok": False, "stage": "startup", "exit": exit_info}, indent=2))
                return EXIT_START_FAILED
            api = sample_api(cfg, timeout=1.5)
            api_status = api["status"]
            if api_status == "ok":
                break
            time.sleep(1.0)

        telemetry = sampler.sample(write=True, update_tracker=False)
        started_at = record.get("started_at")
        payload = {
            "ok": True,
            "already_running": False,
            "run_id": run_id,
            "miner_pid": record.get("miner_pid"),
            "collector_pid": collector_pid,
            "started_at": started_at,
            "api_status": api_status,
            "launch_ms": round((time.time() - t_launch) * 1000.0, 1),
            "gpus": telemetry["gpu"]["detected"],
            "hashrate_hs": telemetry["hashrate"]["total_hs"],
            "logs": {"miner": paths.miner_log, "collector": paths.collector_log,
                     "supervisor": paths.supervisor_log},
            "telemetry": paths.telemetry,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print("Pearl miner started")
            print("  run_id     %s" % run_id)
            print("  miner pid  %s (supervised)" % record.get("miner_pid"))
            print("  gpus       %s" % ", ".join("%s %s" % (g.get("pci"), g.get("name"))
                                                for g in telemetry["gpu"]["detected"]))
            print("  api        %s (%s)" % (telemetry["api"]["url"], api_status))
            print("  hashrate   %s (miner-local, may be 0 while warming up)"
                  % human_hs(telemetry["hashrate"]["total_hs"]))
            print("  logs       %s" % paths.miner_log)
            print("  note: 'started' is not 'pool accepted a share'. Check status/shares.")
        return EXIT_OK if api_status == "ok" else EXIT_API_TIMEOUT


def _stop_pid_group(record: Dict[str, Any], label: str, grace_s: float = 30.0,
                    require_exe: bool = True) -> Tuple[bool, str]:
    pid = record.get("pid") or record.get("miner_pid")
    if not pid:
        return True, "%s: no pid recorded" % label
    observed = observe_process(pid)
    ok, problems = verify_identity(record, observed, require_exe=require_exe)
    if not ok:
        if not observed.get("exists"):
            return True, "%s: already gone" % label
        return False, "%s: refusing to signal pid %s (%s)" % (label, pid, "; ".join(problems))
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError as exc:
        return False, "%s: SIGTERM failed: %s" % (label, exc)
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if not pid_alive(pid):
            return True, "%s: stopped (SIGTERM)" % label
        time.sleep(0.2)
    observed = observe_process(pid)
    ok, problems = verify_identity(record, observed, require_exe=require_exe)
    if not ok:
        return False, "%s: process still alive but identity no longer provable (%s); not killing" % (
            label, "; ".join(problems))
    try:
        os.kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError as exc:
        return False, "%s: SIGKILL failed: %s" % (label, exc)
    time.sleep(0.3)
    if pid_alive(pid):
        return False, "%s: still alive after SIGKILL" % label
    return True, "%s: stopped (SIGKILL after %.0fs)" % (label, grace_s)


def cmd_stop(args: argparse.Namespace, paths: Paths) -> int:
    # Stopping must work even when the config became invalid after start, so the
    # config is loaded lazily and only for the final telemetry sample.
    with OpLock(paths.op_lock):
        record = read_json(paths.run, None) or {}
        collector = _collector_identity(paths)
        if not record.get("miner_pid") and not collector.get("pid"):
            if not args.json:
                print("no managed miner instance recorded; nothing to stop")
            if args.json:
                print(json.dumps({"ok": True, "stopped": False, "reason": "no run record"}, indent=2))
            return EXIT_OK

        miner_ok = True
        miner_msg = "miner: no record"
        if record.get("miner_pid"):
            miner_ok, miner_msg = _stop_pid_group(record, "miner", grace_s=args.grace)
        if not miner_ok:
            print(miner_msg, file=sys.stderr)
            if args.json:
                print(json.dumps({"ok": False, "stage": "miner", "error": miner_msg}, indent=2))
            return EXIT_IDENTITY

        collector_ok = True
        collector_msg = "collector: no record"
        if collector.get("pid"):
            # The collector is a python child of ours: the run token plus the
            # kernel start time prove identity; its executable is the
            # interpreter, whose /proc/pid/exe may be a different real path.
            collector_ok, collector_msg = _stop_pid_group(collector, "collector",
                                                          grace_s=5.0, require_exe=False)

        # Final sample so the host can see process_state=exited with an exit code.
        try:
            cfg, _warnings = load_config(resolve_config_path(args, paths))
            Sampler(cfg, paths).sample(write=True, update_tracker=False)
        except Exception as exc:  # sampling must never break stop
            log("warning: final telemetry sample failed: %s" % exc)

        payload = {"ok": miner_ok and collector_ok, "stopped": True,
                   "miner": miner_msg, "collector": collector_msg,
                   "note": "miner stopped locally; the cloud GPU instance may still be running and billed"}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(miner_msg)
            print(collector_msg)
            print("NOTE: 'miner stopped' does not release the cloud GPU instance or stop its billing.")
        return EXIT_OK if payload["ok"] else EXIT_STOP_FAILED


def cmd_status(args: argparse.Namespace, paths: Paths) -> int:
    cfg, _warnings = load_config(resolve_config_path(args, paths))
    telemetry = Sampler(cfg, paths).sample(write=not args.no_write, update_tracker=True)
    if args.json:
        print(json.dumps(telemetry, indent=2))
        return EXIT_OK
    print_status_human(telemetry)
    return EXIT_OK


def print_status_human(t: Dict[str, Any]) -> None:
    miner = t["miner"]
    print("Pearl remote miner status  %s" % t["sampled_at"])
    print("  machine    %s / %s / %s" % (t["instance"]["hostname"], t["instance"]["os"], t["instance"]["machine_id"][:12]))
    print("  miner      %s %s  state=%s identity=%s" % (miner["name"], miner["version"] or "?",
                                                          miner["process_state"], miner["identity"]))
    if miner.get("uptime_s") is not None:
        print("  uptime     %.0f s" % miner["uptime_s"])
    print("  pool       %s  connected=%s  devfee=%s" % (
        t["pool"]["url"], t["pool"]["connected"], t["pool"]["devfee_percent"]))
    print("  hashrate   %s (miner-local%s)" % (
        human_hs(t["hashrate"]["total_hs"]),
        ", source=" + str(t["hashrate"]["source"]) if t["hashrate"]["source"] else ", unavailable"))
    for gpu in t["hashrate"]["per_gpu"]:
        print("    gpu[%s] %s %s %s" % (gpu["index"], gpu["pci"] or "", gpu["name"] or "",
                                        human_hs(gpu["hashrate_hs"])))
    shares = t["shares"]
    if shares["available"]:
        print("  shares     accepted=%s stale=%s rejected=%s last_accepted=%s" % (
            shares["accepted"], shares["stale"], shares["rejected"], shares.get("last_accepted_at")))
    else:
        print("  shares     unavailable")
    for gpu in t["gpu"]["detected"]:
        print("    gpu[%s] %s %s %sC %sW util=%s%%" % (
            gpu["index"], gpu["pci"], gpu["name"], gpu["temp_c"], gpu["power_w"], gpu["util_pct"]))
    if t["unavailable"]:
        print("  unavailable %s" % ", ".join(t["unavailable"]))
    if t["errors"]:
        for err in t["errors"]:
            print("  note       %s" % err)
    print("  pool-side  hashrate estimate and payouts: not available to this tool")


def cmd_devices(args: argparse.Namespace, paths: Paths) -> int:
    devices = list_devices(paths.miner_binary)
    smi = query_nvidia_smi()
    payload = {"ok": devices["ok"], "miner_devices": devices, "nvidia_smi": smi,
               "gpu_support": detect_gpu_support()}
    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK
    print(devices.get("raw") or "(no output)")
    if smi.get("ok"):
        for gpu in smi["gpus"]:
            print("  smi[%s] %s %s %sC %sW" % (gpu["index"], gpu["pci"], gpu["name"],
                                               gpu["temp_c"], gpu["power_w"]))
    print("support: %s" % payload["gpu_support"]["detail"])
    return EXIT_OK if devices["ok"] else EXIT_PREFLIGHT


def cmd_logs(args: argparse.Namespace, paths: Paths) -> int:
    source = args.source
    path = {"miner": paths.miner_log, "collector": paths.collector_log,
            "supervisor": paths.supervisor_log, "prl": paths.prl_log,
            "telemetry": paths.telemetry}[source]
    if not os.path.exists(path):
        print("%s not found: %s" % (source, path), file=sys.stderr)
        return EXIT_PREFLIGHT
    if source == "telemetry":
        print(open(path, "r", encoding="utf-8", errors="replace").read())
        return EXIT_OK
    for line in tail_lines(path, max_bytes=256 * 1024, max_lines=args.lines):
        print(line)
    return EXIT_OK


def cmd_supervise(args: argparse.Namespace, paths: Paths) -> int:
    """Owns exactly one miner process and records its exit status."""
    paths.ensure()
    cfg, _warnings = load_config(resolve_config_path(args, paths))
    manifest = load_manifest(prepare_manifest_path(args, paths))
    entry = find_miner_entry(manifest, cfg["miner_version"])
    miner_bin = paths.miner_binary
    if not os.path.isfile(miner_bin):
        log("supervisor: miner binary missing: %s" % miner_bin)
        return EXIT_PREFLIGHT
    if sha256_file(miner_bin) != entry["binary_sha256"] and not TEST_MODE:
        log("supervisor: miner binary hash does not match the manifest; refusing to run")
        return EXIT_PREFLIGHT

    argv = miner_exec_prefix(miner_bin) + build_miner_args(cfg, paths)
    env = os.environ.copy()
    env["PRL_RUN_ID"] = args.run_id
    env.pop("PRL_TEST_MODE", None)  # never leak test mode into the miner

    rotate_log(paths.miner_log, cfg["log_max_bytes"])
    os.makedirs(paths.logs, exist_ok=True)
    handle = open(paths.miner_log, "ab", buffering=0)
    started_at = iso()
    try:
        proc = subprocess.Popen(argv, cwd=paths.bin, stdin=subprocess.DEVNULL,
                                stdout=handle, stderr=subprocess.STDOUT, env=env,
                                close_fds=True, **detach_kwargs())
    except OSError as exc:
        handle.close()
        log("supervisor: failed to launch miner: %s" % exc)
        write_json_atomic(paths.exit, {"schema": EXIT_SCHEMA, "exit_code": None,
                                       "signal": None, "error": str(exc), "exited_at": iso()})
        return EXIT_START_FAILED
    handle.close()

    # The kernel start time is the anti-PID-reuse anchor; read it right away.
    ticks = None
    deadline = time.time() + 2.0
    while time.time() < deadline:
        ticks = proc_start_ticks(proc.pid)
        if ticks is not None:
            break
        time.sleep(0.05)
    if ticks is None:
        log("supervisor: warning: could not read the kernel start time of pid %d; "
            "stop will refuse to signal it later" % proc.pid)

    fake = None
    if TEST_MODE and os.environ.get("PRL_IDENTITY_BACKEND") == "spawn":
        fake = _write_fake_identity(proc.pid, miner_bin, argv, args.run_id, started_at)
        ticks = fake["start_ticks"] if fake else ticks

    record = make_run_record(args.run_id, proc.pid, miner_bin, argv, started_at, cfg["api_port"],
                             extra={"args_masked": mask_args(argv[1:]),
                                    "pool_url": cfg["pool_url"],
                                    "worker_name": cfg["worker_name"],
                                    "supervisor_pid": os.getpid(),
                                    "supervisor_start_ticks": proc_start_ticks(os.getpid())},
                             start_ticks=ticks)
    write_json_atomic(paths.run, record, mode=0o600)
    log("supervisor: miner pid %d started (run_id %s)" % (proc.pid, args.run_id))

    rc = proc.wait()
    sig = None
    if rc is not None and rc < 0:
        sig = -rc
    write_json_atomic(paths.exit, {
        "schema": EXIT_SCHEMA,
        "run_id": args.run_id,
        "miner_pid": proc.pid,
        "exit_code": rc if rc is not None and rc >= 0 else None,
        "signal": sig,
        "exited_at": iso(),
    })
    log("supervisor: miner pid %d exited (rc=%s signal=%s)" % (proc.pid, rc, sig))
    return EXIT_OK


def _write_fake_identity(pid: int, exe: str, argv: List[str], run_id: str,
                         started_at: str) -> Optional[Dict[str, Any]]:
    """TEST ONLY: simulate /proc facts for the spawn identity backend."""
    directory = os.environ.get("PRL_TEST_IDENTITY_DIR")
    if not directory:
        return None
    os.makedirs(directory, exist_ok=True)
    payload = {
        "pid": pid,
        "start_ticks": int(time.time() * 1000),
        "exe": os.path.realpath(exe),
        "cmdline_sha256": sha256_bytes(" ".join(argv).encode("utf-8")),
        "run_id": run_id,
        "started_at": started_at,
    }
    write_json_atomic(os.path.join(directory, "%d.json" % pid), payload)
    return payload


def cmd_collect(args: argparse.Namespace, paths: Paths) -> int:
    """Long-running sampler. Writes telemetry snapshots until stopped."""
    paths.ensure()
    cfg, _warnings = load_config(resolve_config_path(args, paths))
    sampler = Sampler(cfg, paths)
    identity = {
        "schema": COLLECTOR_SCHEMA,
        "pid": os.getpid(),
        "exe": os.path.realpath(sys.executable),
        "start_ticks": proc_start_ticks(os.getpid()),
        "cmdline_sha256": sha256_bytes(" ".join(sys.argv).encode("utf-8")),
        "run_id": "collector-" + os.urandom(4).hex(),
        "started_at": iso(),
        "interval_s": cfg["sample_interval_s"],
    }
    if TEST_MODE and os.environ.get("PRL_IDENTITY_BACKEND") == "spawn":
        fake = _write_fake_identity(os.getpid(), sys.executable, sys.argv,
                                    identity["run_id"], identity["started_at"])
        if fake:
            identity["start_ticks"] = fake["start_ticks"]
    write_json_atomic(paths.collector, identity, mode=0o600)
    log("collector: sampling every %ds -> %s" % (cfg["sample_interval_s"], paths.telemetry))
    running = True

    def _stop(_sig: int, _frame: Any) -> None:
        nonlocal running
        running = False

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    while running:
        started = time.time()
        try:
            telemetry = sampler.sample(write=True, update_tracker=True)
            if args.history:
                try:
                    with open(paths.history, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({
                            "sampled_at": telemetry["sampled_at"],
                            "process_state": telemetry["miner"]["process_state"],
                            "api_status": telemetry["api"]["status"],
                            "hashrate_hs": telemetry["hashrate"]["total_hs"],
                            "accepted": telemetry["shares"].get("accepted"),
                            "connected": telemetry["pool"].get("connected"),
                        }) + "\n")
                except OSError:
                    pass
            if args.exit_on_miner_exit and telemetry["miner"]["process_state"] in ("exited", "not_started"):
                log("collector: miner is not running; exiting as requested")
                break
        except Exception as exc:  # keep sampling; report the error in the log
            log("collector: sample failed: %s: %s" % (type(exc).__name__, exc))
        while running:
            remaining = cfg["sample_interval_s"] - (time.time() - started)
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))
    # Mark the last snapshot as no-longer-updating, but never replace a missing
    # or foreign document with a stub that lacks the telemetry contract fields.
    try:
        existing = read_json(paths.telemetry, None)
        if isinstance(existing, dict) and existing.get("schema") == TELEMETRY_SCHEMA:
            existing["collector_stopped_at"] = iso()
            write_json_atomic(paths.telemetry, existing)
    except OSError:
        pass
    return EXIT_OK


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prl_miner.py",
        description="ESI Pearl (PRL) remote miner controller. One miner per machine, "
                    "pinned binary, localhost-only API, JSON telemetry.")
    parser.add_argument("--home", default=default_home(), help="state root (default: ~/esi-pearl)")
    parser.add_argument("--version", action="version", version="esi-pearl " + VERSION)
    sub = parser.add_subparsers(dest="command")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="path to prl-mining.json")
        p.add_argument("--manifest", help="path to miner-manifest.json")
        p.add_argument("--json", action="store_true", help="machine-readable output on stdout")

    p = sub.add_parser("prepare", help="download, verify and install the pinned miner")
    common(p)
    p.add_argument("--miner-version", help="override the pinned miner version")
    p.add_argument("--allow-download", action="store_true", help="allow network download of the asset")
    p.add_argument("--asset-file", help="use an already-downloaded tarball instead of the network")
    p.add_argument("--skip-gpu-check", action="store_true", help="prepare on a machine without a GPU (pre-baking)")
    p.add_argument("--download-timeout", type=float, default=600.0)

    p = sub.add_parser("check", help="preflight: config, GPU, driver, miner, port")
    common(p)

    p = sub.add_parser("start", help="start the supervised miner and the collector")
    common(p)
    p.add_argument("--no-collector", action="store_true", help="do not start the sampling collector")

    p = sub.add_parser("stop", help="stop the collector and the supervised miner")
    common(p)
    p.add_argument("--grace", type=float, default=30.0, help="seconds before SIGKILL")

    p = sub.add_parser("status", help="sample once and print telemetry")
    common(p)
    p.add_argument("--no-write", action="store_true", help="do not overwrite state/telemetry.json")

    p = sub.add_parser("devices", help="list GPUs as the miner sees them")
    common(p)

    p = sub.add_parser("logs", help="print the tail of a log")
    common(p)
    p.add_argument("--source", default="miner", choices=["miner", "collector", "supervisor", "prl", "telemetry"])
    p.add_argument("--lines", type=int, default=80)

    p = sub.add_parser("_supervise", help="internal: run by start, do not call directly")
    common(p)
    p.add_argument("--run-id", required=True)

    p = sub.add_parser("_collect", help="internal: run by start, do not call directly")
    common(p)
    p.add_argument("--history", action="store_true")
    p.add_argument("--exit-on-miner-exit", action="store_true")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_CONFIG
    paths = Paths(args.home)
    handlers = {
        "prepare": cmd_prepare,
        "check": cmd_check,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "devices": cmd_devices,
        "logs": cmd_logs,
        "_supervise": cmd_supervise,
        "_collect": cmd_collect,
    }
    try:
        return handlers[args.command](args, paths)
    except ConfigError as exc:
        log("config error: %s" % exc)
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "stage": "config", "error": str(exc)}, indent=2))
        return EXIT_CONFIG
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    except OSError as exc:
        log("os error: %s" % exc)
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "stage": "os", "error": str(exc)}, indent=2))
        return EXIT_PREFLIGHT


if __name__ == "__main__":
    sys.exit(main())
