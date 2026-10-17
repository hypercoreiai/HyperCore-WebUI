#!/usr/bin/env python3
"""serve/engine_bridge.py - the shared-library routing layer.

Replaces the subprocess pipe bridge with the ABI in include/hypercore/hypercore_api.h.
The engine calls back from its own worker threads; those callbacks are Python callables,
so they run under the GIL. Everything below is built around that one fact: keep the
per-token work off the hot path, and wake the asyncio loop in batches, not per token.

Run:  HYPERCORE_SHARED_LIB_PATH=build-shared/libhypercore_api.so uvicorn serve.engine_bridge:app
"""
from __future__ import annotations

import asyncio
import collections
import ctypes
import json
import os
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from tools.calibrate_adapter import (
    BUSY,
    CMPFUNC,
    SUCCESS,
    UNSUPPORTED,
    CapsStruct,
    RequestConfig,
)


# ---------------------------------------------------------------------------
# Boundary layout: asserted, not assumed. A struct drift at a ctypes boundary is
# silent (fields just read as garbage), so it has to fail loudly at import.
# ---------------------------------------------------------------------------
def _assert_layout() -> None:
    expected = {
        RequestConfig: 4 + 4 + 4 + 4 + 4 + 4 + 4,
        CapsStruct: 4 + 4 + 4 + 4 + 4,
    }
    for struct, want in expected.items():
        got = ctypes.sizeof(struct)
        if got != want:
            raise RuntimeError(f"ABI drift: {struct.__name__} is {got} bytes, header says {want}")
    # Offsets the engine's abi_declared_ok() checks against.
    if getattr(RequestConfig, "max_new_tokens", None) is None:
        raise RuntimeError("ABI drift: RequestConfig has no max_new_tokens field")



# ---------------------------------------------------------------------------
# Request model. Validation mirrors the boundary's rules so the client gets a 422
# instead of a bare status code from the engine.
# ---------------------------------------------------------------------------
class InferenceRequestPayload(BaseModel):
    prompt_tokens: list[int] = Field(..., min_length=1)
    temperature: float = Field(0.0, ge=0.0)
    top_p: float = Field(1.0, ge=0.0, le=1.0)
    top_k: int = Field(1, ge=1, le=64)
    max_new_tokens: int = Field(128, ge=1)
    pcie_frac: float = Field(-1.0, ge=-1.0, le=1.0)   # -1 = engine default, not "zero"
    spec_min_p: float = Field(-1.0, ge=-1.0, le=1.0)


# ---------------------------------------------------------------------------
# Session registry: C user_data (a void*) -> the asyncio side of one request.
# ---------------------------------------------------------------------------
class RequestSession:
    """One in-flight request. Buffer is a deque so appends need no lock; the
    asyncio loop is woken once every `flush_every` tokens, not per token."""

    __slots__ = (
        "buffer",
        "closed",
        "flush_every",
        "loop",
        "pending",
        "session_id",
        "terminal",
        "wakeup",
    )

    def __init__(self, session_id: int, loop: asyncio.AbstractEventLoop, flush_every: int) -> None:
        self.session_id = session_id
        self.loop = loop
        self.buffer: collections.deque[dict[str, Any]] = collections.deque()
        self.flush_every = flush_every
        self.pending = 0
        self.wakeup = asyncio.Event()
        self.terminal: dict[str, Any] | None = None
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _stop_reason_name(reason: int) -> str:
    return {0: "unspecified", 1: "eos", 2: "limit", 3: "error"}.get(int(reason), "unknown")


# A Python exception raised inside a ctypes callback is swallowed by ctypes: the engine
# never sees it and the token is simply gone. Counting them is the only way the host can
# see that the bridge is dropping tokens.
CALLBACK_ERRORS = 0


def _stream_broker(chunk_ptr: Any, user_data: Any) -> None:
    """Called from an engine worker thread, under the GIL. Keep this short."""
    global CALLBACK_ERRORS
    try:
        session_id = int(user_data) if user_data else 0
        session = SESSIONS.get(session_id)
        if session is None or session.closed:
            return  # client is gone; the engine still holds the slot until EOS or LIMIT

        chunk = chunk_ptr.contents
        if chunk.token_id < 0 and chunk.summary:
            s = chunk.summary.contents
            session.terminal = {
                "decode_ms": s.decode_ms,
                "produced_tokens": s.produced_tokens,
                "drafts_accepted": s.drafts_accepted,
                "drafts_offered": s.drafts_offered,
                "stop_reason": _stop_reason_name(s.stop_reason),
            }
            session.loop.call_soon_threadsafe(session.wakeup.set)
            return

        session.buffer.append({"token_id": chunk.token_id, "prompt_reused": chunk.prompt_reused})
        session.pending += 1
        if session.pending >= session.flush_every:
            session.pending = 0
            session.loop.call_soon_threadsafe(session.wakeup.set)
    except Exception:  # noqa: BLE001 - swallowing here is the point; counting is the fix
        CALLBACK_ERRORS += 1


NATIVE_BROKER_HOOK = CMPFUNC(_stream_broker)   # module-level: must outlive every request

# ---------------------------------------------------------------------------
# Engine lifecycle
# ---------------------------------------------------------------------------
class Engine:
    def __init__(self, lib_path: str) -> None:
        self.lib = ctypes.CDLL(lib_path)
        self.lib.hypercore_engine_init.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p]
        self.lib.hypercore_engine_init.restype = ctypes.c_int
        self.lib.hypercore_engine_get_caps.argtypes = [ctypes.c_void_p, ctypes.POINTER(CapsStruct)]
        self.lib.hypercore_engine_get_caps.restype = ctypes.c_int
        self.lib.hypercore_engine_submit.argtypes = [
            ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(RequestConfig),
            ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t, CMPFUNC, ctypes.c_void_p,
        ]
        self.lib.hypercore_engine_submit.restype = ctypes.c_int
        self.lib.hypercore_engine_destroy.argtypes = [ctypes.c_void_p]
        self.lib.hypercore_engine_destroy.restype = None

        self.handle = ctypes.c_void_p()
        code = self.lib.hypercore_engine_init(ctypes.byref(self.handle), b"{}")
        if code != SUCCESS:
            raise RuntimeError(f"hypercore_engine_init failed with status {code}")

        caps = CapsStruct(struct_size=ctypes.sizeof(CapsStruct))
        code = self.lib.hypercore_engine_get_caps(self.handle, ctypes.byref(caps))
        if code != SUCCESS:
            raise RuntimeError(f"hypercore_engine_get_caps failed with status {code}")
        self.max_batch_slots = int(caps.max_batch_slots)
        self.max_context_len = int(caps.max_context_len)
        self.active_pool_workers = int(caps.active_pool_workers)

    def submit(self, config: RequestConfig, tokens: list[int], request_id: int) -> int:
        arr = (ctypes.c_int32 * len(tokens))(*tokens)
        return self.lib.hypercore_engine_submit(
            self.handle, request_id, ctypes.byref(config), arr, len(tokens), NATIVE_BROKER_HOOK,
            ctypes.c_void_p(user_data),
        )

    def destroy(self) -> None:
        # Joins every request thread, so no callback fires after this returns.
        if self.handle:
            self.lib.hypercore_engine_destroy(self.handle)
            self.handle = None


SESSIONS: dict[int, RequestSession] = {}
SESSION_LOCK = threading.Lock()
_counter = 0
_counter_lock = threading.Lock()
ENGINE: Engine | None = None

def next_session_id() -> int:
    global _counter
    with _counter_lock:
        _counter += 1
        return _counter


def build_request_config(payload: InferenceRequestPayload) -> RequestConfig:
    return RequestConfig(
        struct_size=ctypes.sizeof(RequestConfig),
        temperature=payload.temperature,
        top_p=payload.top_p,
        top_k=payload.top_k,
        max_new_tokens=payload.max_new_tokens,
        pcie_frac=payload.pcie_frac,
        spec_min_p=payload.spec_min_p,
    )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="HyperCore Engine Web Services")

FLUSH_EVERY = max(1, int(os.getenv("HYPERCORE_FLUSH_EVERY", "16")))
STREAM_TIMEOUT_S = float(os.getenv("HYPERCORE_STREAM_TIMEOUT", "30"))


@app.on_event("startup")
def startup() -> None:
    global ENGINE
    _assert_layout()
    lib_path = os.getenv("HYPERCORE_SHARED_LIB_PATH", "./build-shared/libhypercore_api.so")
    ENGINE = Engine(lib_path)
    print(f"[bridge] engine ready: slots={ENGINE.max_batch_slots} "
          f"workers={ENGINE.active_pool_workers} ctx={ENGINE.max_context_len}")


@app.on_event("shutdown")
def shutdown() -> None:
    if ENGINE:
        ENGINE.destroy()


@app.get("/v1/caps")
def caps() -> dict[str, Any]:
    if not ENGINE:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="engine not mounted")
    return {
        "max_batch_slots": ENGINE.max_batch_slots,
        "max_context_len": ENGINE.max_context_len,
        "active_pool_workers": ENGINE.active_pool_workers,
        "flush_every": FLUSH_EVERY,
        "dropped_callback_tokens": CALLBACK_ERRORS,
    }


@app.post("/v1/inference/stream")
async def inference_stream(payload: InferenceRequestPayload) -> StreamingResponse:
    if not ENGINE:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="engine not mounted")

    if len(payload.prompt_tokens) + payload.max_new_tokens > ENGINE.max_context_len:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"prompt {len(payload.prompt_tokens)} + max_new_tokens {payload.max_new_tokens} "
                   f"exceeds max_context_len {ENGINE.max_context_len}",
        )

    session_id = next_session_id()
    session = RequestSession(session_id, asyncio.get_running_loop(), FLUSH_EVERY)
    with SESSION_LOCK:
        SESSIONS[session_id] = session

    code = ENGINE.submit(build_request_config(payload), payload.prompt_tokens, session_id)
    if code == BUSY:
        with SESSION_LOCK:
            SESSIONS.pop(session_id, None)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"all {ENGINE.max_batch_slots} batch slots are in use")
    if code == UNSUPPORTED:
        with SESSION_LOCK:
            SESSIONS.pop(session_id, None)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported for this build")
    if code != SUCCESS:
        with SESSION_LOCK:
            SESSIONS.pop(session_id, None)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"engine rejected the request: {code}")

    async def stream() -> AsyncIterator[str]:
        deadline = time.monotonic() + STREAM_TIMEOUT_S
        try:
            while True:
                try:
                    await asyncio.wait_for(session.wakeup.wait(), timeout=max(0.0, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'event': 'error', 'detail': 'engine silent'})}\n\n"
                    return

                batch = list(session.buffer)
                session.buffer.clear()
                for item in batch:
                    yield f"data: {json.dumps(item)}\n\n"

                if session.terminal is not None:
                    payload = {"event": "DONE", "summary": session.terminal}
                    yield f"event: DONE\ndata: {json.dumps(payload)}\n\n"
                    return
        finally:
            # Client disconnects land here. The engine keeps its slot until the
            # terminal chunk; v1 has no cancel, so this is a drain, not a stop.
            session.close()
            with SESSION_LOCK:
                SESSIONS.pop(session_id, None)

    return StreamingResponse(stream(), media_type="text/event-stream")
