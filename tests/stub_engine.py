#!/usr/bin/env python3
"""A stand-in for the engine, for tests and for a machine with no GPU.

It speaks the same --serve protocol as strata: READY, INFO, T, PP, DONE, STOP, VRAM,
QUIT. It is not a model; it echoes a fixed token sequence so the WebUI, the Monitor
and the engine boundary can be exercised end to end without weights on disk.

Two details are deliberate, because they are the two the real engine gets right and a
shortcut gets wrong:

  * the third field of READY is the literal word "stop" - the engine's way of saying
    it honours a STOP line mid-request (src/program/generate.cpp), not a token id;
  * stdin is read on its own thread, so a STOP reaches a request that is still
    printing tokens.

Each token takes 50 ms on purpose: it gives a STOP time to arrive.
"""
import queue
import sys
import threading
import time

MAX_CONTEXT = 32768
TOKEN_MS = 0.05

requests: queue.Queue = queue.Queue()
stop = threading.Event()


def read_stdin() -> None:
    """The engine's own stdin thread: a STOP reaches a request that is still running."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "QUIT":
            requests.put(None)
            return
        if line == "STOP":
            stop.set()
            continue
        if line.startswith("VRAM"):
            print("VRAM expert_slots=0 vram_free_mib=1024", flush=True)
            continue
        if line.startswith("GEN"):
            requests.put(line)


def run(line: str) -> None:
    stop.clear()
    parts = line.split()
    max_new = int(parts[1])
    ids = parts[2] if len(parts) > 2 else ""
    prompt = len(ids.split(",")) if ids else 0
    print("PP %d %d 12.0 8000.0" % (prompt, prompt), flush=True)
    produced = 0
    for t in range(1, min(max_new, 8) + 1):
        if stop.is_set():
            break
        print("T %d" % t, flush=True)
        produced += 1
        time.sleep(TOKEN_MS)
    print("DONE %d %d 12.0 30.0 %s 0 0 0 0 0 0 0 0.0 %d 0"
          % (produced, prompt, "cancel" if stop.is_set() else "length", prompt), flush=True)


def main():
    print("INFO context=%d kv=int8 expert_slots=0 expert_cache_mib=0 engine=0.0.0-stub"
          % MAX_CONTEXT, flush=True)
    print("READY %d stop" % MAX_CONTEXT, flush=True)
    threading.Thread(target=read_stdin, daemon=True).start()
    while True:
        line = requests.get()
        if line is None:
            break
        run(line)


if __name__ == "__main__":
    main()
