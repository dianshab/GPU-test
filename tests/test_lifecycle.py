#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lifecycle tests for prl_miner.py using tests/mock_krig.py.

These run on any OS with Python 3.8+ and no GPU: the real miner is replaced by
a mock that speaks the same CLI and the same local HTTP telemetry surface, and
process identity is provided by the test-only "spawn" identity backend
(PRL_IDENTITY_BACKEND=spawn) because Windows has no /proc.

What this proves: one instance only, start/status/stop wiring, exit detection,
API loss and schema degradation, and refusal to signal a process whose identity
cannot be proven. It does NOT prove anything about real GPU mining.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import shutil
import socket
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

os.environ["PRL_TEST_MODE"] = "1"
os.environ["PRL_IDENTITY_BACKEND"] = "spawn"
os.environ.setdefault("MOCK_GPU_COUNT", "2")
import prl_miner as pm  # noqa: E402

WALLET = "prl1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
MOCK_SOURCE = os.path.join(HERE, "mock_krig.py")


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="prl-life-")
        bin_dir = os.path.join(self.home, "bin")
        os.makedirs(bin_dir)
        self.mock = os.path.join(bin_dir, "mock_krig.py")
        shutil.copyfile(MOCK_SOURCE, self.mock)
        self.port = free_port()
        self.config = os.path.join(self.home, "prl-mining.json")
        self.manifest = os.path.join(self.home, "miner-manifest.json")
        self.identity_dir = os.path.join(self.home, "state", "testproc")
        os.makedirs(self.identity_dir, exist_ok=True)

        self.write_config()
        self.write_manifest()

        os.environ["PRL_MINER_BINARY"] = self.mock
        os.environ["PRL_TEST_IDENTITY_DIR"] = self.identity_dir
        os.environ["PRL_API_PORT"] = str(self.port)
        os.environ["MOCK_GPU_COUNT"] = "2"
        os.environ["MOCK_HASHRATE_HS"] = "7.1e13"
        os.environ["MOCK_MODE"] = "ok"
        os.environ["MOCK_SHARE_EVERY_S"] = "1"

    def tearDown(self):
        # Best effort cooperative stop first, then hard-kill whatever is left
        # (a previous assertion may have left the stack running on purpose).
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                pm.main(["--home", self.home, "stop", "--json",
                         "--config", self.config, "--manifest", self.manifest])
        except Exception:
            pass
        for path in (os.path.join(self.home, "state", "run.json"),
                     os.path.join(self.home, "state", "collector.json")):
            record = pm.read_json(path, {}) or {}
            pid = record.get("miner_pid") or record.get("pid")
            if pid and pm.pid_alive(pid):
                try:
                    os.kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
                except OSError:
                    pass
        shutil.rmtree(self.home, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def write_config(self, **overrides):
        cfg = {
            "schema": pm.CONFIG_SCHEMA,
            "wallet_address": WALLET,
            "pool_url": "stratum+ssl://prl-us.kryptex.network:8048",
            "worker_name": "ESITEST",
            "api_port": self.port,
            "miner_version": "test-1",
            "gpu_select": "all",
            "sample_interval_s": 2,
            "stale_after_s": 20,
            "api_wait_s": 6,
            "log_max_bytes": 2097152,
            "allow_unverified_gpu": False,
        }
        cfg.update(overrides)
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

    def write_manifest(self):
        manifest = {
            "schema": pm.MANIFEST_SCHEMA,
            "default_version": "test-1",
            "miners": [{
                "name": "krig-miner",
                "version": "test-1",
                "platform": "test",
                "asset": "mock.tar.gz",
                "url": "http://127.0.0.1:9/mock.tar.gz",
                "sha256": "0" * 64,
                "size_bytes": 1,
                "binary": "mock_krig.py",
                "binary_sha256": pm.sha256_file(self.mock),
            }],
        }
        with open(self.manifest, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pm.main(["--home", self.home] + list(args) +
                           ["--config", self.config, "--manifest", self.manifest, "--json"])
        stdout = out.getvalue()
        payload = None
        if stdout.strip():
            try:
                payload = json.loads(stdout)
            except ValueError:
                payload = {"_raw": stdout}
        return code, payload, err.getvalue()

    def telemetry(self):
        return pm.read_json(os.path.join(self.home, "state", "telemetry.json"), {})

    # -- tests -----------------------------------------------------------
    def test_start_status_stop_single_instance(self):
        code, payload, err = self.call("start")
        self.assertEqual(code, 0, err)
        self.assertFalse(payload["already_running"])
        pid = payload["miner_pid"]
        self.assertTrue(pm.pid_alive(pid))

        record = pm.read_json(os.path.join(self.home, "state", "run.json"), {})
        self.assertEqual(record["miner_pid"], pid)
        self.assertIsNotNone(record["miner_start_ticks"])

        code, status, err = self.call("status")
        self.assertEqual(code, 0, err)
        self.assertEqual(status["schema"], pm.TELEMETRY_SCHEMA)
        self.assertEqual(status["miner"]["process_state"], "running")
        self.assertEqual(status["miner"]["identity"], "verified")
        self.assertEqual(status["api"]["status"], "ok")
        self.assertAlmostEqual(status["hashrate"]["total_hs"], 2 * 7.1e13, delta=1e9)
        self.assertEqual(len(status["hashrate"]["per_gpu"]), 2)
        self.assertTrue(status["shares"]["available"])
        self.assertEqual(status["gpu"]["count"], 2)
        for key in ("sampled_at", "instance", "miner", "pool", "gpu", "unavailable", "pool_side"):
            self.assertIn(key, status)
        self.assertIsNone(status["pool_side"]["hashrate_estimate"])
        self.assertIsNone(status["pool_side"]["payouts"])

        # Second start must not create a second miner.
        code, payload2, err = self.call("start")
        self.assertEqual(code, 0, err)
        self.assertTrue(payload2["already_running"])
        self.assertEqual(payload2["pid"], pid)

        code, payload, err = self.call("stop")
        self.assertEqual(code, 0, err)
        time.sleep(0.5)
        self.assertFalse(pm.pid_alive(pid))

        final = self.telemetry()
        self.assertEqual(final["miner"]["process_state"], "exited")
        self.assertIsNotNone(pm.read_json(os.path.join(self.home, "state", "exit.json"), {}))

        # After the miner is gone its API must not be queried at all, so no
        # stale or foreign numbers can leak into a "not running" sample.
        code, after, err = self.call("status")
        self.assertEqual(code, 0, err)
        self.assertEqual(after["miner"]["process_state"], "exited")
        self.assertEqual(after["api"]["status"], "skipped")
        self.assertIsNone(after["hashrate"]["total_hs"])
        self.assertIn("miner_api_not_queried", after["unavailable"])

    def test_shares_and_last_accepted(self):
        code, _, err = self.call("start")
        self.assertEqual(code, 0, err)
        time.sleep(3.5)
        code, status, err = self.call("status")
        self.assertEqual(code, 0, err)
        self.assertGreaterEqual(status["shares"]["accepted"], 1)
        self.assertIsNotNone(status["shares"]["last_accepted_at"])

    def test_miner_exits_immediately(self):
        os.environ["MOCK_MODE"] = "exit_immediately"
        code, payload, err = self.call("start")
        self.assertEqual(code, pm.EXIT_START_FAILED, err)
        self.assertFalse(payload.get("ok"))

    def test_api_unreachable_is_reported_not_hidden(self):
        os.environ["MOCK_MODE"] = "no_api"
        code, payload, err = self.call("start")
        self.assertEqual(code, pm.EXIT_API_TIMEOUT, err)
        self.assertTrue(payload["ok"])
        self.assertNotEqual(payload["api_status"], "ok")
        telemetry = self.telemetry()
        self.assertEqual(telemetry["api"]["status"], "unreachable")
        self.assertIsNone(telemetry["hashrate"]["total_hs"])
        self.assertIn("miner_api_hashrate", telemetry["unavailable"])
        self.assertTrue(telemetry["api"]["error"])

    def test_schema_error_falls_back_to_metrics(self):
        os.environ["MOCK_MODE"] = "bad_schema"
        code, payload, err = self.call("start")
        self.assertEqual(code, pm.EXIT_API_TIMEOUT, err)
        telemetry = self.telemetry()
        self.assertEqual(telemetry["api"]["status"], "schema_error")
        # The metrics endpoint still answers, so hashrate stays available but is
        # labelled with its real source rather than invented.
        self.assertIsNotNone(telemetry["hashrate"]["total_hs"])
        self.assertEqual(telemetry["hashrate"]["source"], "miner_metrics")
        # The share counters come from /metrics in this case and are labelled as such.
        self.assertTrue(telemetry["shares"]["available"])
        self.assertEqual(telemetry["shares"]["source"], "miner_metrics")

    def test_stop_refuses_unproven_identity(self):
        code, payload, err = self.call("start")
        self.assertEqual(code, 0, err)
        pid = payload["miner_pid"]
        record_path = os.path.join(self.home, "state", "run.json")
        record = pm.read_json(record_path, {})
        record["run_id"] = "tampered-run-id"
        with open(record_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh)

        code, payload, err = self.call("stop")
        self.assertEqual(code, pm.EXIT_IDENTITY, err)
        self.assertFalse(payload["ok"])
        self.assertTrue(pm.pid_alive(pid), "must not kill a process it cannot prove is ours")

        # Restore the identity so tearDown/stop can finish normally.
        record["run_id"] = pm.read_json(os.path.join(self.identity_dir, "%d.json" % pid), {})["run_id"]
        with open(record_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        code, _, err = self.call("stop")
        self.assertEqual(code, 0, err)
        time.sleep(0.3)
        self.assertFalse(pm.pid_alive(pid))

    def test_missing_wallet_blocks_start(self):
        self.write_config(wallet_address="")
        code, payload, err = self.call("start")
        self.assertEqual(code, pm.EXIT_CONFIG, err)
        self.assertIn("wallet", (payload or {}).get("error", err).lower())

    def test_stop_when_nothing_running(self):
        code, payload, err = self.call("stop")
        self.assertEqual(code, 0, err)
        self.assertFalse(payload["stopped"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
