#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mock krig-miner used by the offline test suite.

It reproduces the CLI surface and the telemetry surface that the real miner
exposes (verified against krig-miner 1.5.12: --help, --version, --list-devices,
the HTTP "/" JSON and "/metrics" text), so the controller and the Windows host
reader can be exercised without a GPU, a pool or a rented instance.

This program mines nothing. It is never installed on a production machine.

Environment knobs:
  MOCK_GPU_COUNT      number of fake GPUs (default 1)
  MOCK_HASHRATE_HS    per-GPU hashrate in H/s (default 7.1e13)
  MOCK_MODE           ok | exit_immediately | no_api | bad_schema | pool_down
  MOCK_SHARE_EVERY_S  accepted-share increment period (default 2, 0 = never)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION_LINE = "krig-miner v1.5.12 (git 76573838469579bf0754f52aeb9363fda1169267)"

STATE = {
    "accepted": 0,
    "stale": 0,
    "rejected": 0,
    "started": time.time(),
    "connected": True,
}


def stamp() -> str:
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + ".%03d" % int((now % 1) * 1000)


def emit(line: str) -> None:
    sys.stdout.write("%s %s\n" % (stamp(), line))
    sys.stdout.flush()


def gpu_count() -> int:
    try:
        return max(1, int(os.environ.get("MOCK_GPU_COUNT", "1")))
    except ValueError:
        return 1


def gpu_name(index: int) -> str:
    names = ["NVIDIA GeForce RTX 5060", "NVIDIA GeForce RTX 4090", "NVIDIA A100-SXM4-80GB"]
    return names[index % len(names)]


def pci_for(index: int) -> str:
    return "%02x:00.0" % (8 + index)


def hashrate_hs() -> float:
    try:
        return float(os.environ.get("MOCK_HASHRATE_HS", "7.1e13"))
    except ValueError:
        return 7.1e13


def list_devices() -> None:
    count = gpu_count()
    print("krig-miner \u2014 %d device(s) \u2014 select with -d <index> or --devices-pci <pci>:" % count)
    for index in range(count):
        print("  [%d] %s %s" % (index, pci_for(index), gpu_name(index)))
    sys.stdout.flush()


def api_payload() -> dict:
    devices = []
    for index in range(gpu_count()):
        devices.append({
            "type": "gpu",
            "index": index,
            "name": gpu_name(index),
            "name_short": gpu_name(index).replace("NVIDIA GeForce ", ""),
            "pci_address": pci_for(index),
            "algorithms": [{
                "name": "pearlhash",
                "hashrate": hashrate_hs(),
                "shares": {
                    "accepted": STATE["accepted"],
                    "stale": STATE["stale"],
                    "rejected": STATE["rejected"],
                },
            }],
        })
    return {"miner_name": "krig-miner", "miner_version": "1.5.12", "devices": devices}


def metrics_text() -> str:
    lines = [
        "# HELP krig_miner_info Build metadata.",
        "# TYPE krig_miner_info gauge",
        'krig_miner_info{git_sha="76573838469579bf0754f52aeb9363fda1169267"} 1',
    ]
    for index in range(gpu_count()):
        lines += [
            "# HELP krig_miner_hashes_per_second Per-worker hashes/s (gauge).",
            "# TYPE krig_miner_hashes_per_second gauge",
            'krig_miner_hashes_per_second{gpu="%d"} %r' % (index, hashrate_hs()),
            'krig_miner_heartbeat_age_seconds{gpu="%d"} 0.001' % index,
        ]
    connected = 1 if STATE["connected"] else 0
    lines += [
        "# HELP krig_miner_pool_connected 1 if the mining stream is open, 0 otherwise.",
        "# TYPE krig_miner_pool_connected gauge",
        "krig_miner_pool_connected %d" % connected,
        "# HELP krig_miner_pool_latency_ms Latest pool round-trip time in milliseconds.",
        "# TYPE krig_miner_pool_latency_ms gauge",
        "krig_miner_pool_latency_ms 62.375",
        "# HELP krig_miner_blocks_submitted_total Submitted shares by pool result.",
        "# TYPE krig_miner_blocks_submitted_total counter",
        'krig_miner_blocks_submitted_total{gpu="0",result="accepted"} %d' % STATE["accepted"],
        'krig_miner_blocks_submitted_total{gpu="0",result="stale"} %d' % STATE["stale"],
        'krig_miner_blocks_submitted_total{gpu="0",result="rejected"} %d' % STATE["rejected"],
    ]
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the test output quiet
        pass

    def do_GET(self):  # noqa: N802 (http.server naming)
        mode = os.environ.get("MOCK_MODE", "ok")
        if self.path.rstrip("/") in ("", "/"):
            if mode == "bad_schema":
                body = json.dumps({"miner_name": "krig-miner", "devices": []}).encode()
            else:
                body = json.dumps(api_payload()).encode()
            ctype = "application/json"
        elif self.path.rstrip("/") == "/metrics":
            body = metrics_text().encode()
            ctype = "text/plain; version=0.0.4"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int, host: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--url")
    parser.add_argument("--user")
    parser.add_argument("--api-port", type=int, default=0)
    parser.add_argument("--api-host", default="127.0.0.1")
    parser.add_argument("--no-tui", action="store_true")
    parser.add_argument("--log-level")
    parser.add_argument("--log-file")
    parser.add_argument("--devices")
    parser.add_argument("--devices-pci")
    parser.add_argument("--gpu-plimit")
    parser.add_argument("--gpu-fan")
    parser.add_argument("--gpu-coffset")
    args, _unknown = parser.parse_known_args()

    if args.version:
        print(VERSION_LINE)
        print("backend: not selected")
        return 0
    if args.list_devices:
        list_devices()
        return 0

    mode = os.environ.get("MOCK_MODE", "ok")
    if mode == "exit_immediately":
        emit("error: cannot initialise CUDA device 0 (mock failure)")
        return 3

    emit(VERSION_LINE)
    if args.api_port and mode != "no_api":
        try:
            serve(args.api_port, args.api_host)
        except OSError as exc:
            emit("warn: metrics API could not bind %s:%d: %s" % (args.api_host, args.api_port, exc))
        else:
            emit("metrics: HTTP API on http://%s:%d/" % (args.api_host, args.api_port))

    for index in range(gpu_count()):
        emit("GPU%d %s %s" % (index, pci_for(index), gpu_name(index)))

    if mode == "pool_down":
        STATE["connected"] = False
        emit("warn: pool connection failed (mock)")
    else:
        emit("connected: %s" % (args.url or "stratum+ssl://pool.invalid:8048"))
        emit("stratum: new job mockjob_2097152 height 137790")
        emit("pool %s: devfee 0%%" % ((args.url or "pool.invalid:8048").split("//")[-1].split(":")[0]))

    stop = {"flag": False}

    def _stop(_sig, _frame):
        stop["flag"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    try:
        share_every = float(os.environ.get("MOCK_SHARE_EVERY_S", "2"))
    except ValueError:
        share_every = 2.0
    next_share = time.time() + share_every if share_every > 0 else None

    while not stop["flag"]:
        time.sleep(0.5)
        if next_share and time.time() >= next_share:
            STATE["accepted"] += 1
            emit("share accepted: GPU0 62ms")
            next_share = time.time() + share_every
        if int(time.time() * 2) % 20 == 0:  # periodic status line, like the real miner
            emit("GPU0 %s %s: %.2f TH/s %d/0/0 456 GH/W 160W 71C 58C 75%%" % (
                pci_for(0), gpu_name(0).replace("NVIDIA GeForce ", ""),
                hashrate_hs() / 1e12, STATE["accepted"]))
            emit("Total: %.2f TH/s shares: %d accepted 0 stale 0 rejected" % (
                hashrate_hs() / 1e12, STATE["accepted"]))
    emit("shutting down (mock)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
