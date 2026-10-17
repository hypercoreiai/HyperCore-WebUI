#!/usr/bin/env python3
"""serve/flood_stub.py - the HTTP face the load test talks to, without a model pack.

Why this exists: tools/bench_flood.py measures the process boundary - slot saturation,
503 shedding, slot reclamation after a client hangs up. None of those properties belong
to a model; they belong to the supervisor and the worker. Requiring a downloaded pack
makes the one test that can run on a bare machine depend on a 4 GB download.

So the stub exposes the same three routes the real server does, with the same shapes:

    POST /infer/stream   {"prompt": [..], "max_new": n}  -> "data: {...}" frames
    GET  /v1/slots, /slots -> {"slots": [...], "in_flight": n, "max_batch_slots": m}
    GET  /health         -> {"ok": true}

and the same gate in front of them. `--api-key` turns on the key requirement, and the
check is `serve.http_auth.key_matches` - the identical function serve/server.py calls.
That is the point: a 401 from the stub is a 401 from the server, not a lookalike.

It is a thin adapter over HyperCoreProcessSupervisor and nothing else. When a pack is
present, run bench_flood against serve/server.py instead (--server); the assertions are
identical because the shedding is done by the worker, not by the HTTP layer.

Run: python3 serve/flood_stub.py --port 8133 --slots 4
     python3 serve/flood_stub.py --port 8133 --slots 4 --api-key secret
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serve.http_auth import auth_error, key_matches
from serve.engine_supervisor import (
    HyperCoreProcessSupervisor,
    WorkerBusy,
)
from serve.ipc_parser import STREAM_TERMINAL, STREAM_TOKEN

STREAM_PATH = "/infer/stream"
SLOTS_PATH = "/v1/slots"
HEALTH_PATH = "/health"


class FloodHandler(BaseHTTPRequestHandler):
    """One request thread per client, which is exactly what a bounded queue needs."""

    protocol_version = "HTTP/1.1"

    def __init__(self, request, client_address, server: ThreadingHTTPServer) -> None:
        self.supervisor: HyperCoreProcessSupervisor = server.supervisor  # type: ignore[attr-defined]
        self.api_key: str = getattr(server, "api_key", "")  # type: ignore[attr-defined]
        super().__init__(request, client_address, server)

    # ---------------------------------------------------------------- gate
    def _authorized(self) -> bool:
        """serve/server.py's rule, by way of the same function it uses."""
        if key_matches(self.headers, self.api_key):
            return True
        self._json(401, auth_error())
        return False

    # ---------------------------------------------------------------- helpers
    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    # ---------------------------------------------------------------- routes
    def do_GET(self) -> None:
        if self.path == HEALTH_PATH:
            # Not gated: a monitoring probe has no key. It is told whether one is required.
            self._json(200, {"ok": True, "alive": self.supervisor.alive(),
                             "api_key": bool(self.api_key)})
            return
        if self.path in (SLOTS_PATH, "/slots"):
            if not self._authorized():
                return
            caps = self.supervisor.get_caps()
            in_flight = self.supervisor.in_flight()
            slots = [
                {"slot": i,
                 "max_context": int(caps.get("max_context_len", 0)),
                 "is_processing": i < in_flight}
                for i in range(int(caps.get("max_batch_slots", 0)))
            ]
            self._json(200, {"slots": slots, "in_flight": in_flight,
                             "max_batch_slots": int(caps.get("max_batch_slots", 0))})
            return
        self._json(404, {"error": f"no route {self.path}"})

    def do_POST(self) -> None:
        if self.path != STREAM_PATH:
            self._json(404, {"error": f"no route {self.path}"})
            return
        # Before the body is read: an unauthenticated request never reaches the queue, so
        # it never occupies a slot and never shows up in the in-flight count.
        if not self._authorized():
            return

        body = self._read_body()
        # The real server takes "tokens"; "prompt" is accepted so the same client code
        # works against either front.
        tokens = body.get("tokens", body.get("prompt", []))
        prompt = [int(t) for t in tokens]
        if not prompt:
            self._json(400, {"error": "a request needs a token list"})
            return
        request_id = self.supervisor.next_request_id()

        try:
            stream = self.supervisor.stream(
                prompt,
                temperature=float(body.get("temperature", 0.0)),
                top_p=float(body.get("top_p", 1.0)),
                top_k=int(body.get("top_k", 1)),
                max_new_tokens=int(body.get("max_new_tokens", 64)),
                pcie_frac=float(body.get("pcie_frac", -1.0)),
                spec_min_p=float(body.get("spec_min_p", -1.0)),
                timeout_s=60.0,
                request_id=request_id,
            )
            first = next(stream)
        except WorkerBusy as exc:
            # Shedding is answered before a single byte of the stream is written, so a
            # refusal is never mistaken for an empty but successful stream.
            self._json(503, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - a dead worker must not kill the test host
            self._json(503 if not self.supervisor.alive() else 500, {"error": str(exc)})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        delivered = 0
        try:
            tag, token_id, summary = first
            if tag == STREAM_TOKEN:
                self.wfile.write(b"data: " + json.dumps({"token_id": token_id}).encode() + b"\n")
                delivered += 1
                for tag, token_id, summary in stream:
                    if tag == STREAM_TERMINAL:
                        break
                    self.wfile.write(b"data: " + json.dumps({"token_id": token_id}).encode() + b"\n")
                    delivered += 1
            self.wfile.write(
                b"data: " + json.dumps({"done": True, "summary": summary or {}}).encode() + b"\n"
            )
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The only place the host learns the client hung up.
            self.supervisor.cancel(request_id)
            print(f"[stub] client dropped after {delivered} tokens; cancelled {request_id}")
        except Exception as exc:  # noqa: BLE001
            print(f"[stub] stream failed: {exc}")

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    ap = argparse.ArgumentParser(description="HTTP front for the worker, no model required")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8133)
    ap.add_argument("--worker", default=str(ROOT / "build-shared/hypercore-worker"))
    ap.add_argument("--ipc-socket", default="/tmp/hypercore-flood.sock")
    ap.add_argument("--config", default="{}")
    ap.add_argument("--slots", type=int, default=0, help="batch slots; written into the config")
    ap.add_argument("--api-key", default=os.environ.get("HYPERCORE_API_KEY", ""),
                   help="require this key on /infer/stream and /v1/slots, exactly as server.py does")
    args = ap.parse_args()

    config = json.loads(args.config) if args.config.strip() else {}
    if args.slots:
        config["batch"] = args.slots

    supervisor = HyperCoreProcessSupervisor(
        worker_binary=args.worker, socket_path=args.ipc_socket, config=json.dumps(config)
    )
    supervisor.start()

    server = ThreadingHTTPServer((args.host, args.port), FloodHandler)
    server.supervisor = supervisor  # type: ignore[attr-defined]
    server.api_key = args.api_key  # type: ignore[attr-defined]
    print(f"[stub] listening on http://{args.host}:{args.port} "
          f"slots={supervisor.get_caps().get('max_batch_slots')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        supervisor.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
