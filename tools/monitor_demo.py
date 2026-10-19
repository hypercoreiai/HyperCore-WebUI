#!/usr/bin/env python3
"""Run a mock HyperCore server with populated Monitor fields.

This is not a production server. It is a wiring demo for the Monitor tab: it starts a
ScriptedWorker that reports prompt reuse, expert-cache hits, PCIe share, prompt read,
RAM/file blob counters, and the expert-tier CAPS fields, then makes one request and
prints the /metrics row that the Recent requests table consumes.

    python3 tools/monitor_demo.py --port 8080

Then open http://127.0.0.1:8080/#monitor and send a chat message.
"""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serve.engine_supervisor import HyperCoreProcessSupervisor
from tests.frame_fixture import ScriptedWorker
from serve.frontend import ChatTemplate
from serve.server import ByteTokenizer, HyperCoreRequestHandler, Service, serve


def make_service() -> tuple[Service, HyperCoreProcessSupervisor]:
    """Build a Service backed by a mock worker that reports the Monitor fields."""
    tok = ByteTokenizer()
    worker = ScriptedWorker(
        max_slots=1,
        tokens=8,
        token_start=1,
        prefill_s=0.05,
        prompt_reused=12,
        cache_hits=97,
        cache_lookups=100,
        offloaded=3,
        prompt_read=8,
        ram_blobs=2,
        file_blobs=1,
        file_kb=1234,
        expert_slots=1200,
        expert_slots_full=3700,
        expert_cache_mib=1536,
        expert_cache_full_mib=3700,
        vram_free_mib=2048,
        expert_source=3,
        prompt_chunk=2048,
    )
    sup = HyperCoreProcessSupervisor(transport=worker.connect, max_restarts=0)
    sup.start()
    svc = Service(
        HyperCoreRequestHandler(sup),
        tok,
        ChatTemplate(ROOT / "serve/chat_template.jinja"),
        model_name="monitor-demo",
    )
    svc.api_monitor = True
    return svc, sup


def main() -> int:
    """Start the mock server and keep it running until Ctrl-C."""
    ap = argparse.ArgumentParser(description="Mock HyperCore Monitor demo.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    svc, sup = make_service()
    httpd = serve(svc, host=args.host, port=args.port)
    print(f"Monitor demo on http://{args.host}:{args.port}/#monitor")
    print("Send a chat message, then inspect /metrics or the Monitor tab.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.shutdown()
        httpd.server_close()
        sup.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
