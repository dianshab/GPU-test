#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline unit tests for prl_miner.py (no GPU, no pool, no SSH).

Run:  python -m unittest discover -s tests -p "test_*.py" -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

os.environ["PRL_TEST_MODE"] = "1"  # relax OS/driver gates for offline tests
import prl_miner as pm  # noqa: E402

WALLET = "prl1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
FIXTURES = os.path.join(HERE, "fixtures")


def read_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return fh.read()


def base_config(**overrides):
    cfg = {
        "schema": pm.CONFIG_SCHEMA,
        "wallet_address": WALLET,
        "pool_url": "stratum+ssl://prl-us.kryptex.network:8048",
        "worker_name": "EXAMPLE_WORKER",
        "api_port": 12000,
        "miner_version": "test-1",
        "gpu_select": "all",
        "power_limit_w": None,
        "fan_percent": None,
        "core_offset_mhz": None,
        "sample_interval_s": 10,
        "stale_after_s": 45,
        "api_wait_s": 30,
        "log_max_bytes": 2097152,
        "allow_unverified_gpu": False,
    }
    cfg.update(overrides)
    return cfg


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prl-cfg-")
        self.path = os.path.join(self.tmp, "prl-mining.json")

    def write(self, payload):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def test_valid_config_and_masking(self):
        self.write(base_config())
        cfg, warnings = pm.load_config(self.path, env={})
        self.assertEqual(cfg["wallet_address"], WALLET)
        self.assertEqual(cfg["pool"]["host"], "prl-us.kryptex.network")
        self.assertEqual(cfg["api_host"], "127.0.0.1")
        self.assertEqual(warnings, [])
        masked = pm.mask_wallet(WALLET)
        self.assertNotIn(WALLET, masked)
        self.assertTrue(masked.startswith(WALLET[:10]))
        self.assertTrue(masked.endswith(WALLET[-4:]))

    def test_wallet_missing(self):
        payload = base_config()
        payload.pop("wallet_address")
        self.write(payload)
        with self.assertRaises(pm.ConfigError):
            pm.load_config(self.path, env={})

    def test_wallet_placeholder(self):
        self.write(base_config(wallet_address="<PRL_WALLET_ADDRESS>"))
        with self.assertRaises(pm.ConfigError):
            pm.load_config(self.path, env={})

    def test_wallet_bad_prefix(self):
        self.write(base_config(wallet_address="btc1" + "q" * 40))
        with self.assertRaises(pm.ConfigError):
            pm.load_config(self.path, env={})

    def test_wallet_uppercase_rejected(self):
        self.write(base_config(wallet_address=WALLET.upper()))
        with self.assertRaises(pm.ConfigError):
            pm.load_config(self.path, env={})

    def test_wallet_too_short(self):
        self.write(base_config(wallet_address="prl1qqqq"))
        with self.assertRaises(pm.ConfigError):
            pm.load_config(self.path, env={})

    def test_pool_scheme_required(self):
        self.write(base_config(pool_url="prl-us.kryptex.network:8048"))
        with self.assertRaises(pm.ConfigError):
            pm.load_config(self.path, env={})

    def test_pool_kryptex_requires_ssl(self):
        self.write(base_config(pool_url="stratum+tcp://prl-us.kryptex.network:8048"))
        with self.assertRaises(pm.ConfigError) as ctx:
            pm.load_config(self.path, env={})
        self.assertIn("stratum+ssl", str(ctx.exception))

    def test_pool_non_kryptex_warns_not_raises(self):
        self.write(base_config(pool_url="stratum+tcp://pool.example.net:3333"))
        cfg, warnings = pm.load_config(self.path, env={})
        self.assertEqual(cfg["pool"]["scheme"], "stratum+tcp")
        self.assertTrue(any("3%" in w or "not a Kryptex" in w for w in warnings))

    def test_worker_validation(self):
        self.write(base_config(worker_name="bad worker; rm -rf /"))
        with self.assertRaises(pm.ConfigError):
            pm.load_config(self.path, env={})

    def test_api_port_range(self):
        self.write(base_config(api_port=80))
        with self.assertRaises(pm.ConfigError):
            pm.load_config(self.path, env={})

    def test_gpu_select_validation(self):
        self.write(base_config(gpu_select="0,1"))
        cfg, _ = pm.load_config(self.path, env={})
        self.assertEqual(cfg["gpu_select"], "0,1")
        self.write(base_config(gpu_select="0; rm -rf"))
        with self.assertRaises(pm.ConfigError):
            pm.load_config(self.path, env={})

    def test_env_overrides(self):
        self.write(base_config())
        cfg, _ = pm.load_config(self.path, env={
            "PRL_WORKER": "CLOUD-01",
            "PRL_API_PORT": "12099",
            "PRL_WALLET": WALLET,
        })
        self.assertEqual(cfg["worker_name"], "CLOUD-01")
        self.assertEqual(cfg["api_port"], 12099)

    def test_stale_default_follows_interval(self):
        self.write(base_config(sample_interval_s=20, stale_after_s=None))
        with open(self.path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        payload.pop("stale_after_s")
        self.write(payload)
        cfg, _ = pm.load_config(self.path, env={})
        self.assertEqual(cfg["stale_after_s"], 60)

    def test_build_miner_args_defaults(self):
        self.write(base_config())
        cfg, _ = pm.load_config(self.path, env={})
        paths = pm.Paths(os.path.join(self.tmp, "home"))
        args = pm.build_miner_args(cfg, paths)
        self.assertEqual(args[args.index("--api-host") + 1], "127.0.0.1")
        self.assertIn("--no-tui", args)
        self.assertNotIn("--gpu-plimit", args)
        self.assertNotIn("--gpu-fan", args)
        self.assertEqual(args[args.index("--user") + 1], "%s/EXAMPLE_WORKER" % WALLET)
        masked = pm.mask_args(args)
        self.assertNotIn(WALLET, " ".join(masked))
        self.assertIn("EXAMPLE_WORKER", masked[masked.index("--user") + 1])

    def test_build_miner_args_hardware_opt_in(self):
        self.write(base_config(power_limit_w=180, fan_percent=70, core_offset_mhz=-50))
        cfg, _ = pm.load_config(self.path, env={})
        args = pm.build_miner_args(cfg, pm.Paths(os.path.join(self.tmp, "home2")))
        self.assertEqual(args[args.index("--gpu-plimit") + 1], "180")
        self.assertEqual(args[args.index("--gpu-fan") + 1], "70")
        self.assertEqual(args[args.index("--gpu-coffset") + 1], "-50")


class ParsingTests(unittest.TestCase):
    def test_list_devices_real_output(self):
        text = ("krig-miner \u2014 1 device(s) \u2014 select with -d <index> or --devices-pci <pci>:\n"
                "  [0] 08:00.0 NVIDIA GeForce RTX 5060\n")
        parsed = pm.parse_list_devices(text)
        self.assertEqual(parsed["declared_count"], 1)
        self.assertEqual(parsed["devices"], [{
            "index": 0, "pci": "08:00.0", "name": "NVIDIA GeForce RTX 5060"}])

    def test_list_devices_multi_and_empty(self):
        text = ("krig-miner \u2014 2 device(s) \u2014 select with -d <index>:\n"
                "  [0] 08:00.0 NVIDIA GeForce RTX 4090\n"
                "  [1] 41:00.0 NVIDIA A100-SXM4-80GB\n")
        parsed = pm.parse_list_devices(text)
        self.assertEqual(len(parsed["devices"]), 2)
        self.assertEqual(parsed["devices"][1]["pci"], "41:00.0")
        self.assertEqual(pm.parse_list_devices("krig-miner \u2014 0 device(s)")["devices"], [])

    def test_parse_api_live_fixture(self):
        payload = json.loads(read_fixture("krig-api-root-1.5.12.json"))
        parsed = pm.parse_miner_api(payload)
        self.assertTrue(parsed["usable"])
        self.assertEqual(parsed["miner_version"], "1.5.12")
        self.assertEqual(parsed["algorithm"], "pearlhash")
        dev = parsed["devices"][0]
        self.assertEqual(dev["pci"], "08:00.0")
        self.assertGreater(dev["hashrate_hs"], 1e13)
        self.assertIsInstance(dev["shares"]["accepted"], int)

    def test_parse_api_missing_fields(self):
        parsed = pm.parse_miner_api({"miner_name": "krig-miner", "devices": [{"index": 0}]})
        self.assertFalse(parsed["usable"])
        self.assertIsNone(parsed["devices"][0]["hashrate_hs"])
        self.assertIn("no algorithms[] entry", parsed["devices"][0]["error"])

    def test_parse_api_string_hashrate_not_invented(self):
        parsed = pm.parse_miner_api({"devices": [{"index": 0, "algorithms": [
            {"name": "pearlhash", "hashrate": "72 TH/s", "shares": {"accepted": 1, "stale": 0, "rejected": 0}}]}]})
        self.assertIsNone(parsed["devices"][0]["hashrate_hs"])
        self.assertIn("not a number", parsed["devices"][0]["error"])
        self.assertTrue(parsed["usable"])  # shares still usable

    def test_parse_metrics_live_fixture(self):
        series = pm.parse_prometheus(read_fixture("krig-metrics-1.5.12.txt"))
        hashes = pm.first_value(series, "krig_miner_hashes_per_second", {"gpu": "0"})
        self.assertIsNotNone(hashes)
        self.assertGreater(hashes, 1e13)  # fixture was captured live; exact value moves
        self.assertEqual(pm.first_value(series, "krig_miner_pool_connected"), 1.0)
        latency = pm.first_value(series, "krig_miner_pool_latency_ms")
        self.assertIsNotNone(latency)
        self.assertGreater(latency, 0)
        accepted = sum(v for lab, v in series["krig_miner_blocks_submitted_total"] if lab.get("result") == "accepted")
        self.assertGreaterEqual(accepted, 0.0)  # live fixture, value grows

    def test_parse_metrics_garbage(self):
        series = pm.parse_prometheus("# comment\nnot a metric line\nbroken{x=\"1\"} abc\n")
        self.assertEqual(series, {})

    def test_norm_pci(self):
        self.assertEqual(pm.norm_pci("00000000:08:00.0"), "08:00.0")
        self.assertEqual(pm.norm_pci("08:00.0"), "08:00.0")
        self.assertEqual(pm.norm_pci("GPU-abc"), None)

    def test_parse_miner_log(self):
        tmp = tempfile.mkdtemp(prefix="prl-log-")
        path = os.path.join(tmp, "miner.log")
        now = pm.now_utc()
        stamp = (now - __import__("datetime").timedelta(seconds=5)).strftime("%H:%M:%S")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(stamp + ".100 krig-miner v1.5.12\n")
            fh.write(stamp + ".200 connected: stratum+ssl://prl-us.kryptex.network:8048\n")
            fh.write(stamp + ".300 stratum: new job abc_2097152 height 137788\n")
            fh.write(stamp + ".400 pool prl-us.kryptex.network:8048: devfee 0%\n")
            fh.write(stamp + ".500 share accepted: GPU0 62ms\n")
        info = pm.parse_miner_log(path, now)
        self.assertTrue(info["available"])
        self.assertEqual(info["devfee_percent"], 0.0)
        self.assertIsNotNone(info["last_job_at"])
        self.assertIsNotNone(info["last_accepted_log_at"])
        self.assertIsNone(info["last_error"])

    def test_parse_miner_log_missing(self):
        info = pm.parse_miner_log(os.path.join(tempfile.mkdtemp(), "nope.log"))
        self.assertFalse(info["available"])
        self.assertIsNone(info["last_job_at"])


class ApiGuardTests(unittest.TestCase):
    """A foreign process answering on our port must never be adopted."""

    def _serve(self, payload):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        body = json.dumps(payload).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def test_foreign_miner_name_is_rejected(self):
        server = self._serve({
            "miner_name": "some-other-miner",
            "miner_version": "9.9",
            "devices": [{"index": 0, "algorithms": [
                {"name": "pearlhash", "hashrate": 999.0, "shares": {"accepted": 5}}]}],
        })
        try:
            api = pm.sample_api({"api_host": "127.0.0.1", "api_port": server.server_address[1]}, timeout=2.0)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(api["status"], "schema_error")
        self.assertIn("unexpected miner_name", api["error"])

    def test_krig_miner_name_accepted(self):
        server = self._serve({
            "miner_name": "krig-miner",
            "miner_version": "1.5.12",
            "devices": [{"index": 0, "algorithms": [
                {"name": "pearlhash", "hashrate": 7.1e13, "shares": {"accepted": 5}}]}],
        })
        try:
            api = pm.sample_api({"api_host": "127.0.0.1", "api_port": server.server_address[1]}, timeout=2.0)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(api["status"], "ok")
        self.assertAlmostEqual(api["payload"]["devices"][0]["hashrate_hs"], 7.1e13)


class IdentityTests(unittest.TestCase):
    def base(self):
        return {
            "run_id": "run-1",
            "miner_pid": 4242,
            "miner_start_ticks": 1000,
            "exe": "/home/u/esi-pearl/bin/krig-miner",
            "cmdline_sha256": "abc",
            "started_at": pm.iso(),
            "api_port": 12000,
        }

    def observed(self, **over):
        obs = {
            "pid": 4242, "exists": True, "start_ticks": 1000,
            "exe": "/home/u/esi-pearl/bin/krig-miner", "exe_readable": True,
            "run_id": "run-1", "cmdline_sha256": "abc", "backend": "proc",
        }
        obs.update(over)
        return obs

    def test_verified(self):
        ok, problems = pm.verify_identity(self.base(), self.observed())
        self.assertTrue(ok, problems)

    def test_pid_reuse_detected(self):
        ok, problems = pm.verify_identity(self.base(), self.observed(start_ticks=2000))
        self.assertFalse(ok)
        self.assertTrue(any("start time" in p for p in problems))

    def test_token_mismatch(self):
        ok, problems = pm.verify_identity(self.base(), self.observed(run_id="run-2"))
        self.assertFalse(ok)
        self.assertTrue(any("token" in p for p in problems))

    def test_exe_mismatch(self):
        ok, problems = pm.verify_identity(self.base(), self.observed(exe="/usr/bin/other"))
        self.assertFalse(ok)
        self.assertTrue(any("executable" in p for p in problems))

    def test_unreadable_exe_not_proven(self):
        ok, problems = pm.verify_identity(self.base(), self.observed(exe=None, exe_readable=False))
        self.assertFalse(ok)
        self.assertTrue(any("not readable" in p or "not provable" in p for p in problems))

    def test_missing_ticks_not_proven(self):
        record = self.base()
        record["miner_start_ticks"] = None
        ok, problems = pm.verify_identity(record, self.observed())
        self.assertFalse(ok)
        self.assertTrue(any("start time" in p for p in problems))

    def test_process_gone(self):
        ok, problems = pm.verify_identity(self.base(), self.observed(exists=False))
        self.assertFalse(ok)
        self.assertTrue(any("does not exist" in p for p in problems))


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prl-prep-")
        self.home = os.path.join(self.tmp, "home")
        self.config = os.path.join(self.tmp, "prl-mining.json")
        self.manifest_path = os.path.join(self.tmp, "miner-manifest.json")
        self.asset = os.path.join(self.tmp, "asset.tar.gz")
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(base_config(), fh)
        self.members = {
            "krig-miner": b"#!/bin/sh\necho fake miner\n",
            "mine-pearl.sh": b"#!/bin/sh\necho fake helper\n",
            "libkrig_quantus_intel.so": b"\x7fELF fake library",
        }

    def build_asset(self, extra_member=None):
        with tarfile.open(self.asset, "w:gz") as tar:
            for name, data in self.members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(data))
            if extra_member:
                info = tarfile.TarInfo(extra_member)
                data = b"evil"
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return pm.sha256_file(self.asset)

    def write_manifest(self, sha, size=None, binary_sha=None, members=None):
        members = members or [
            {"path": name, "sha256": pm.sha256_bytes(data), "size_bytes": len(data)}
            for name, data in self.members.items()
        ]
        manifest = {
            "schema": pm.MANIFEST_SCHEMA,
            "default_version": "test-1",
            "miners": [{
                "name": "krig-miner",
                "version": "test-1",
                "platform": "test",
                "asset": "asset.tar.gz",
                "url": "http://127.0.0.1:9/asset.tar.gz",
                "sha256": sha,
                "size_bytes": size if size is not None else os.path.getsize(self.asset),
                "binary": "krig-miner",
                "binary_sha256": binary_sha or pm.sha256_bytes(self.members["krig-miner"]),
                "members": members,
            }],
        }
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        return manifest

    def run_prepare(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pm.main(["--home", self.home, "prepare", "--config", self.config,
                            "--manifest", self.manifest_path, "--asset-file", self.asset,
                            "--skip-gpu-check"])
        return code, out.getvalue()

    def test_prepare_installs_and_verifies(self):
        sha = self.build_asset()
        self.write_manifest(sha)
        code, _ = self.run_prepare()
        self.assertEqual(code, 0)
        binary = os.path.join(self.home, "bin", "krig-miner")
        self.assertTrue(os.path.isfile(binary))
        self.assertTrue(os.access(binary, os.X_OK) or os.name == "nt")
        with open(os.path.join(self.home, "state", "prepare.json"), "r", encoding="utf-8") as fh:
            prepared = json.load(fh)
        self.assertEqual(prepared["miner_version"], "test-1")
        self.assertEqual(prepared["asset_sha256"], sha)
        self.assertIn("total_ms", prepared["timings"])

    def test_prepare_rejects_hash_mismatch(self):
        self.build_asset()
        self.write_manifest("0" * 64)
        code, _ = self.run_prepare()
        self.assertEqual(code, pm.EXIT_PREFLIGHT)
        self.assertFalse(os.path.isfile(os.path.join(self.home, "bin", "krig-miner")))

    def test_prepare_rejects_size_mismatch(self):
        sha = self.build_asset()
        self.write_manifest(sha, size=12345)
        code, _ = self.run_prepare()
        self.assertEqual(code, pm.EXIT_PREFLIGHT)

    def test_prepare_rejects_unsafe_member(self):
        sha = self.build_asset(extra_member="../escape.txt")
        self.write_manifest(sha)
        code, _ = self.run_prepare()
        self.assertEqual(code, pm.EXIT_PREFLIGHT)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape.txt")))

    def test_prepare_rejects_missing_member(self):
        sha = self.build_asset()
        manifest = self.write_manifest(sha)
        manifest["miners"][0]["members"].append({"path": "extra.bin", "sha256": "0" * 64, "size_bytes": 1})
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        code, _ = self.run_prepare()
        self.assertEqual(code, pm.EXIT_PREFLIGHT)

    def test_prepare_rejects_wrong_member_hash(self):
        sha = self.build_asset()
        members = [
            {"path": "krig-miner", "sha256": "0" * 64, "size_bytes": len(self.members["krig-miner"])},
            {"path": "mine-pearl.sh", "sha256": pm.sha256_bytes(self.members["mine-pearl.sh"]), "size_bytes": 24},
            {"path": "libkrig_quantus_intel.so", "sha256": pm.sha256_bytes(self.members["libkrig_quantus_intel.so"]), "size_bytes": 16},
        ]
        self.write_manifest(sha, members=members)
        code, _ = self.run_prepare()
        self.assertEqual(code, pm.EXIT_PREFLIGHT)

    def test_prepare_without_config_uses_manifest_default(self):
        sha = self.build_asset()
        self.write_manifest(sha)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pm.main(["--home", self.home, "prepare", "--config",
                            os.path.join(self.tmp, "missing.json"),
                            "--manifest", self.manifest_path, "--asset-file", self.asset,
                            "--skip-gpu-check"])
        self.assertEqual(code, 0)


class PrepareDownloadTests(unittest.TestCase):
    """The download path must verify bytes and refuse a bad payload."""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.tmp = tempfile.mkdtemp(prefix="prl-dl-")
        self.home = os.path.join(self.tmp, "home")
        self.config = os.path.join(self.tmp, "cfg.json")
        self.manifest = os.path.join(self.tmp, "man.json")
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(base_config(), fh)

        # a tiny, valid tar.gz used as the release asset
        self.payload = io.BytesIO()
        self.members = {"krig-miner": b"#!/bin/sh\necho hi\n",
                        "mine-pearl.sh": b"#!/bin/sh\n",
                        "libkrig_quantus_intel.so": b"elf"}
        with tarfile.open(fileobj=self.payload, mode="w:gz") as tar:
            for name, data in self.members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        self.bytes_good = self.payload.getvalue()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                body = self.server.payload
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.payload = self.bytes_good
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/asset.tar.gz" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_manifest(self, sha, size):
        manifest = {
            "schema": pm.MANIFEST_SCHEMA,
            "default_version": "test-1",
            "miners": [{
                "name": "krig-miner", "version": "test-1", "platform": "test",
                "asset": "asset.tar.gz", "url": self.url, "sha256": sha, "size_bytes": size,
                "binary": "krig-miner",
                "binary_sha256": pm.sha256_bytes(self.members["krig-miner"]),
                "members": [{"path": n, "sha256": pm.sha256_bytes(d), "size_bytes": len(d)}
                            for n, d in self.members.items()],
            }],
        }
        with open(self.manifest, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)

    def run_prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return pm.main(["--home", self.home, "prepare", "--config", self.config,
                            "--manifest", self.manifest, "--allow-download", "--skip-gpu-check"])

    def test_download_and_install(self):
        self.write_manifest(pm.sha256_bytes(self.bytes_good), len(self.bytes_good))
        self.assertEqual(self.run_prepare(), 0)
        self.assertTrue(os.path.isfile(os.path.join(self.home, "bin", "krig-miner")))
        prepared = pm.read_json(os.path.join(self.home, "state", "prepare.json"), {})
        self.assertIn("download_ms", prepared.get("timings", {}))

    def test_download_with_wrong_bytes_is_refused(self):
        self.server.payload = self.bytes_good + b"tampered"
        self.write_manifest(pm.sha256_bytes(self.bytes_good), len(self.bytes_good))
        code = self.run_prepare()
        self.assertEqual(code, pm.EXIT_PREFLIGHT)
        self.assertFalse(os.path.isfile(os.path.join(self.home, "bin", "krig-miner")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
