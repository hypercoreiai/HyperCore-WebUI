"""serve/narration.py - engine log lines become the status text the UI shows.

Pure functions over strings: no process, no socket, no model. That is what makes them testable
without a GPU.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

ENGINE_REQUEST = re.compile(
    # "+ 12288 of 98179 read": a request cancelled while its prompt was read (#471)
    r"prompt (?P<prompt>\d+) tokens = (?P<reused>\d+) reused \+ \d+(?: of \d+)? read in (?P<read>[\d.]+) ms "
    r"\((?P<pp>[\d.]+) tok/s\), "
    r"(?P<gen>\d+) generated in (?P<gen_ms>[\d.]+) ms \((?P<tg>[\d.]+) tok/s\)")

def echo_requests(log_path: str, offset: int) -> None:
    """HYPERCORE_REQUEST_LINES=1: one stdout line per finished request, from the engine's own summary in its log.

    The engine's stderr goes to the log file (the start narrator reads it), so a supervisor that only sees this
    process's output - a tray, llama-swap - has no per-request numbers. This re-states the engine's line with the
    total the two times make: `request prompt P cached C output O prompt_read R ms total S ms prefill X tok/s decode Y
    tok/s` (prompt_read: the time the engine spent reading the prompt's new tokens, not a time to first token).
    """
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            m = ENGINE_REQUEST.search(line)
            if m:
                read_ms, gen_ms = float(m["read"]), float(m["gen_ms"])
                print("[hypercore] request prompt %s cached %s output %s prompt_read %.0f ms total %.0f ms prefill %s "
                      "tok/s decode %s tok/s" % (m["prompt"], m["reused"], m["gen"], read_ms, read_ms + gen_ms, m["pp"],
                                                 m["tg"]), flush=True)

def experts_loading_words(args: list, size: str) -> str:
    """#505: what the start does with the experts, by the engine's flags (generate.cpp's option parsing): a RAM budget
    copies the hottest N GiB into RAM (--resident-budget-gib), the resident low-RAM mode the ones the GPU does not hold
    (--resident-experts), plain --mmap-experts reads them from the model files through the OS file cache (nothing is
    loaded into RAM up front); otherwise all of them go into RAM."""
    if "--resident-budget-gib" in args:
        try:
            n = f"up to {float(args[args.index('--resident-budget-gib') + 1]):g} GiB"
        except (IndexError, ValueError):
            n = "a RAM budget"
        return (f"loading the most-used experts into RAM ({n}; the rest are read from the model files as needed) "
                "and locking part of them for the GPU.")
    if "--resident-experts" in args:
        return (f"loading the experts the GPU does not hold into RAM (of {size}) and locking part of them for the "
                "GPU.")
    if "--mmap-experts" in args:
        return (f"mapping the experts from the model files ({size}, --mmap-experts): they are not loaded into RAM - "
                "the OS file cache reads them as the GPU's expert cache fills and as requests need them.")
    return f"loading the experts into RAM ({size}) and locking part of them for the GPU."

def narrate_start(log_path: str, offset: int, args: list, done: threading.Event, heartbeat=20.0) -> None:
    """While the engine starts, say in the server window what it is doing, from its log: the start reads tens of GB
    into RAM and locks part of it for the GPU, and on many PCs everything is slow or frozen for a minute or more -
    people closed the window thinking it had hung.  The warning comes at that step, not after it."""
    gb = 0.0
    if "--native" in args:                              # about the size of the experts it will read
        try:
            gb = os.path.getsize(args[args.index("--native") + 1]) / 1e9
        except (OSError, IndexError):
            pass
    size = f"about {gb:.0f} GB" if gb >= 1 else "tens of GB"
    loading = experts_loading_words(args, size)
    t0 = last = time.time()
    said = set()

    def say(key, text):
        nonlocal last
        if key not in said:
            said.add(key)
            last = time.time()
            print(text, flush=True)

    say("weights", "[hypercore] starting the engine: reading the model's weights ...")
    pos = offset
    while not done.wait(0.5):
        try:
            with open(log_path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
        except OSError:
            chunk = b""
        if chunk.count(b"\n"):
            cut = chunk.rfind(b"\n") + 1
            pos += cut
            for line in chunk[:cut].decode("utf-8", "replace").splitlines():
                if "PLE on" in line or "expert arena:" in line or "experts via mmap" in line:   # #505: mapped
                    say("arena", f"[hypercore] {loading}\n"
                                 "         YOUR PC CAN BE SLOW OR STOP RESPONDING FOR 1-3 MINUTES NOW - this is normal.\n"
                                 "         Please wait and don't close this window; the browser opens when it is ready.")
                elif " loaded " in line and "GiB at" in line:
                    say("loaded", "[hypercore] experts loaded: " + line.split(" loaded ", 1)[1].strip() +
                        f" ({time.time() - t0:.0f} s so far)")
                elif "expert cache " in line and " slots, " in line and "auto" not in line:
                    n = line.split("expert cache ", 1)[1].split(";")[0].replace(" slots,", " experts,").strip()
                    say("cache", f"[hypercore] filling the GPU's expert cache ({n}) ...")
                elif "session is up" in line:
                    say("up", "[hypercore] almost ready ...")
        if time.time() - last > heartbeat:
            last = time.time()
            print(f"[hypercore] still starting ({time.time() - t0:.0f} s) - please wait ...", flush=True)

def worker_start_words(supervisor) -> tuple[str, list[str]] | None:
    """What the narrator needs from a supervisor: the file its worker's stderr goes to, and the
    engine flags that worker was started with (its own config).  None without a log: there is
    nothing to read, and the window then says nothing about the minutes of loading (#505)."""
    log = getattr(supervisor, "stderr_log", None)
    if not log:
        return None
    try:
        cfg = json.loads(getattr(supervisor, "config", "") or "{}")
    except (TypeError, ValueError):
        cfg = {}
    return str(log), list(cfg.get("engine_args") or [])

def narrate_worker_start(supervisor, ready: threading.Event) -> None:
    """Say in the server window what the worker is doing while it starts, from its log (#505).
    Daemon thread, stopped by `ready`: the start reads tens of GB into RAM and locks part of it
    for the GPU, and on many PCs everything freezes for a minute or more - people closed the
    window thinking it had hung."""
    words = worker_start_words(supervisor)
    if not words:
        return
    log, args = words
    threading.Thread(target=narrate_start, args=(log, 0, args, ready), daemon=True).start()

EXPERT_SOURCE_NAMES = {
    0: "no expert tier",
    1: "mapped read-only from the pack (--mmap-experts)",
    2: "the arena copy (--expert-cache)",
    3: "resident in page-locked RAM (--resident-experts)",
}

def expert_config_line(info: dict) -> str:
    """The expert configuration the worker resolved, in one line, when setup finishes.

    The engine prints all of this at startup, but to its own log file. In the split
    architecture the worker's stderr goes to that log and not to the terminal, so the person
    starting the server sees a model name and a port and nothing about the tier that decides
    whether the run is fast. This is that line, from the numbers the worker already reports.
    """
    slots = int(info.get("expert_slots") or 0)
    full = int(info.get("expert_slots_full") or 0)
    mib = int(info.get("expert_cache_mib") or 0)
    full_mib = int(info.get("expert_cache_full_mib") or 0)
    source = info.get("expert_source")
    free = info.get("vram_free_mib")
    if source is None:
        return ("[hypercore] expert configuration: the worker did not report one. "
               "Its log holds the detail: " + str(info.get("log") or "(no log configured)"))
    parts = [f"{slots} of {full} VRAM expert slots" if full else f"{slots} VRAM expert slots",
            f"{mib / 1024:.2f} GiB" + (f" of {full_mib / 1024:.2f} GiB" if full_mib > mib else "")]
    line = "[hypercore] expert configuration: " + ", ".join(parts) + " held by " + EXPERT_SOURCE_NAMES.get(int(source), f"source {source}")
    if free:
        line += f" ({free} MiB of VRAM free when it was sized)"
    if info.get("prompt_chunk"):
        line += f", prompt chunk {info['prompt_chunk']} tokens"
    if info.get("pcie_frac") is not None and info.get("pcie_frac") >= 0:
        line += f", pcie_frac {info['pcie_frac']:.2f}"
    return line

class ConvCacheLog:
    """#596: the engine's conversation cache as its log tells it (the engine writes "hypercore serve: conversation
    cache: parked N tokens ...; parked=P bytes=B evictions=E" and "restored N tokens ...; parked=P bytes=B" to stderr,
    which is the log): read on from where it was last read, from the start of the engine's current run."""
    EVENT = re.compile(r"conversation cache: (parked|skipped|restored) (\d+) tokens.*?parked=(\d+) bytes=(\d+)"
                       r"(?: evictions=(\d+))?")
    DROPPED = re.compile(r"conversation cache: dropped \d+ superseded .*?parked=(\d+)")
    READ_MAX = 1 << 20                                  # at most the last MiB of new lines per read

    def __init__(self):
        self.key, self.pos = None, 0
        self.reset()

    def reset(self):
        self.state = {"parked": 0, "bytes": 0, "evictions": 0, "parks": 0, "restores": 0, "last_event": None,
                      "last_tokens": None, "last_at": None}

    def poll(self, path, start) -> dict:
        """The state after the log's new lines; `start` is where the engine's current run began in it."""
        if not path or start is None:
            return dict(self.state)
        if self.key != (path, start):                   # another start of the engine: its cache starts empty
            self.key, self.pos = (path, start), start
            self.reset()
        try:
            size = os.path.getsize(path)
            if size < self.pos:                         # the log was emptied or replaced
                self.pos = 0
            if size - self.pos > self.READ_MAX:
                self.pos = size - self.READ_MAX
            with open(path, "rb") as f:
                f.seek(self.pos)
                data = f.read(size - self.pos)
        except OSError:
            return dict(self.state)
        end = data.rfind(b"\n") + 1                     # whole lines only: the rest is read next time
        self.pos += end
        now = time.time()
        for line in data[:end].decode("utf-8", "replace").splitlines():
            if "conversation cache:" not in line:
                continue
            m = self.EVENT.search(line)
            if m:
                st = self.state
                st["parked"], st["bytes"] = int(m.group(3)), int(m.group(4))
                if m.group(5) is not None:
                    st["evictions"] = int(m.group(5))
                if m.group(1) != "skipped":
                    st["parks" if m.group(1) == "parked" else "restores"] += 1
                    st["last_event"], st["last_tokens"], st["last_at"] = m.group(1), int(m.group(2)), now
                continue
            m = self.DROPPED.search(line)
            if m:
                self.state["parked"] = int(m.group(1))
        return dict(self.state)

def conversation_cache_view(info: dict, hist: list, totals: dict, parked: dict) -> dict:
    """#596: the Monitor's Conversation cache card: the parked conversations (the engine's opt-in
    --conversation-cache-mib: budget, slots, what its log says) and how much of the prompts the cache gave back."""
    mib = info.get("conversation_cache_mib")
    last = hist[-1] if hist else None
    return {"enabled": isinstance(mib, int) and mib > 0, "budget_mib": mib if isinstance(mib, int) else None,
            "slots": info.get("conversation_cache_slots"), **parked,
            "requests": len(hist), "requests_reused": sum(1 for r in hist if (r.get("reused") or 0) > 0),
            "reused_tokens": totals.get("reused", 0), "prompt_tokens": totals.get("prompt_tokens", 0),
            "last_reused": last.get("reused") if last else None,
            "last_prompt": last.get("prompt_tokens") if last else None}

_BTRACE = bool(os.environ.get("HYPERCORE_BATCH_TRACE"))

def btrace(*a):
    if _BTRACE:
        print("[batch-trace]", threading.get_ident() % 10000, *a, file=sys.stderr, flush=True)
