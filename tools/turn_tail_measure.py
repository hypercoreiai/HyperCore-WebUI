#!/usr/bin/env python3
"""tools/turn_tail_measure.py - does the end-of-turn check change the answer?

`--turn-tail-check` makes the server, when a turn would end, append one short turn asking
the model whether every task in the request is done; a bare confirmation ends the turn and
anything else keeps it going (serve/server.py:with_tail_check). The switch is settled; what
has never been measured is whether it changes the answer in practice: how many extra rounds
it costs, how many extra characters, and whether the answer actually covers more of the
request.

This is that measurement. It runs the same prompt twice against the same model:

  control  the endpoint you give it (the WebUI that is already running a model)
  test     a second WebUI started by this script in attach mode against that same
           endpoint, with --turn-tail-check on

Attach mode means no second copy of a tens-of-GB model is loaded, so the comparison runs
while the card is busy. Each request is streamed and the SSE chunks are read, so the
number of tail checks the server actually ran is counted, not guessed.

    python3 tools/turn_tail_measure.py --self                      # mock pair, no GPU
    python3 tools/turn_tail_measure.py --base http://127.0.0.1:8081 --key 0000
    python3 tools/turn_tail_measure.py --prompts my_prompts.json --runs 3

A prompt file is a JSON list of {"name", "prompt", "must": ["marker", ...]}; "must" is what
a complete answer has to contain. The default set is three multi-part tasks whose parts are
named in the prompt itself.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_PROMPTS: list[dict[str, Any]] = [
    {
        "name": "three named parts",
        "prompt": "Write three short sections, headed exactly PART A, PART B and PART C. "
                  "PART A: three risks of running an LLM on one GPU. PART B: what a KV cache "
                  "is, in two sentences. PART C: one command to check free VRAM on Linux.",
        "must": ["PART A", "PART B", "PART C"],
    },
    {
        "name": "four labelled tasks",
        "prompt": "Do all four of these and label each one with its number:\n"
                  "1. list the files a Python package needs to be installable\n"
                  "2. say what `pip install -e .` does\n"
                  "3. give a one-line git command to undo the last commit\n"
                  "4. explain what a .gitignore entry cannot do\n",
        "must": ["1.", "2.", "3.", "4."],
    },
    {
        "name": "count and then list",
        "prompt": "Count the items in this list, then print each one on its own line: "
                  "alpha, beta, gamma, delta. Finish with the line 'TOTAL: <number>'.",
        "must": ["alpha", "beta", "gamma", "delta", "TOTAL"],
    },
]

#: The mock script used by --self. The answer says something is still outstanding, so the
#: check fires and the turn is extended up to the configured maximum. That proves the wiring
#: end to end without a GPU: the control gets one round, the test server gets the checks.
MOCK_SCRIPT = ("Looking at the request.</think>\n\n"
               "Task one is done, but the second part is not yet complete.")

#: The key a --self run uses for the two mock servers it starts. A server started without a
#: key takes it from the run config it picks up (serve/server.py: `a.api_key or
#: cfg.get("api_key")`), and a config the Build tab wrote has "0000" in it - so the
#: harness' own requests would be 401'd on a machine that has built a quant. Naming the key
#: here is what makes --self self-contained.
SELF_KEY = "turn-tail-self-test"


def auth_key(key: str, self_run: bool) -> str:
    """The key the servers this script starts and the requests it sends agree on.

    Against a live endpoint the key is whatever that endpoint needs, and an empty one is
    fine: an unkeyed server is open. Against the mock pair --self starts, an empty --key
    still has to become a key, or the servers inherit the machine's run config and the
    comparison never gets past a 401.
    """
    return key or (SELF_KEY if self_run else "")


def request(endpoint: str, prompt: str, key: str, max_tokens: int, model: str,
            thinking_budget: int = 0) -> dict[str, Any]:
    """One streamed chat request. Returns the text, the rounds, the tail checks and the timing.

    `thinking_budget` caps how much the model may think (serve/server.py's
    `reasoning_budget_tokens`). A turn that runs out of tokens while still thinking ends with
    finish_reason "length", and the tail check only fires on a turn that ended by itself, so
    without a budget a thinking model never gets checked at all.
    """
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "stream": True, "max_tokens": max_tokens,
                       **({"reasoning_budget_tokens": thinking_budget} if thinking_budget else {})}).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    started = time.time()
    text, checks, cont, compl = "", 0, 0, 0
    req = urllib.request.Request(endpoint.rstrip("/") + "/v1/chat/completions",
                                data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            if r.status != 200:
                return {"error": f"HTTP {r.status}: {r.read().decode(errors='replace')[:200]}",
                        "elapsed": time.time() - started}
            for line in r:
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    chunk = json.loads(line[5:].strip())
                except ValueError:
                    continue
                for choice in (chunk.get("choices") or []):
                    delta = choice.get("delta") or choice.get("message") or {}
                    text += delta.get("content") or ""
                if chunk.get("hypercore_mcp"):
                    ev = chunk["hypercore_mcp"]
                    if ev.get("event") == "tail_check":
                        checks += 1
                        cont += ev.get("result") == "continue"
                        compl += ev.get("result") == "complete"
                text += chunk.get("content") or ""
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {"error": str(exc), "elapsed": time.time() - started}
    return {"text": text, "chars": len(text), "checks": checks, "continued": cont,
           "confirmed": compl, "elapsed": time.time() - started}


def wait_for_health(url: str, key: str, timeout_s: float = 40.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def start_tail_server(base: str, port: int, key: str, extra: list[str], mock: bool = False) -> subprocess.Popen:
    """A WebUI with the tail check on.

    Against a live model it runs in attach mode, so no second copy of a tens-of-GB model is
    loaded and the comparison can be made while the card is already busy. With --self it gets
    its own mock engine instead, so the whole comparison needs no GPU at all.
    """
    engine = (["--engine", "mock", "--script", MOCK_SCRIPT] if mock
              else ["--engine-url", base])
    proc = subprocess.Popen([sys.executable, "serve/server.py", *engine,
                            "--host", "127.0.0.1", "--port", str(port),
                            *( ["--api-key", key] if key else []), "--turn-tail-check",
                            *extra], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if not wait_for_health(f"http://127.0.0.1:{port}", key):
        out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
        proc.kill()
        raise SystemExit(f"the tail-check server never came up on port {port}:\n{out}")
    return proc


def start_mock_base(port: int, key: str) -> subprocess.Popen:
    """A mock-engine server for --self: the whole comparison, without a GPU."""
    proc = subprocess.Popen([sys.executable, "serve/server.py", "--engine", "mock",
                            "--script", MOCK_SCRIPT,
                            "--host", "127.0.0.1", "--port", str(port),
                            *(["--api-key", key] if key else [])],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if not wait_for_health(f"http://127.0.0.1:{port}", key):
        out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
        proc.kill()
        raise SystemExit(f"the mock server never came up on port {port}:\n{out}")
    return proc


def covered(text: str, must: list[str]) -> list[str]:
    low = text.lower()
    return [m for m in must if m.lower() in low]


def load_prompts(path: str | None) -> list[dict[str, Any]]:
    """The prompt set: the built-in three, or a JSON list of {name, prompt, must}.

    A file the user wrote is checked shape-first, so a typo fails with the shape
    expected rather than a KeyError halfway through a run against a live model.
    """
    if not path:
        return [dict(p) for p in DEFAULT_PROMPTS]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read the prompt file {path}: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(p, dict) and p.get("prompt") for p in data):
        raise SystemExit(f'{path} must be a JSON list of objects, each with a "prompt" and an optional "must" list')
    return [dict(p) for p in data]

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8081",
                    help="an OpenAI-compatible endpoint already running the model")
    ap.add_argument("--key", default="", help="the API key of that endpoint")
    ap.add_argument("--model", default="hypercore", help="the model name to send")
    ap.add_argument("--tail-port", type=int, default=8123, help="port for the check-on server")
    ap.add_argument("--runs", type=int, default=2, help="how many times to run each prompt")
    ap.add_argument("--max-tokens", type=int, default=600)
    ap.add_argument("--thinking-budget", type=int, default=400,
                    help="reasoning_budget_tokens for every request (0: no cap). A turn that ends "
                         "while still thinking is never tail-checked, so a thinking model needs one.")
    ap.add_argument("--prompts", help="a JSON list of {name, prompt, must}")
    ap.add_argument("--checks", type=int, default=3, help="turn_tail_checks for the test server")
    ap.add_argument("--self", action="store_true",
                    help="run against a mock engine pair instead of a live model (no GPU)")
    ap.add_argument("--json", action="store_true", help="print the raw rows as JSON")
    a = ap.parse_args()

    prompts = load_prompts(a.prompts)
    key = auth_key(a.key, a.self)
    base = "http://127.0.0.1:8122" if a.self else a.base
    procs = []
    if a.self:
        procs.append(start_mock_base(8122, key))
    tail = start_tail_server(base, a.tail_port, key, ["--turn-tail-checks", str(a.checks)],
                            mock=a.self)
    procs.append(tail)

    rows: list[dict[str, Any]] = []
    try:
        for prompt in prompts:
            for run in range(a.runs):
                off = request(base, prompt["prompt"], key, a.max_tokens, a.model, a.thinking_budget)
                on = request(f"http://127.0.0.1:{a.tail_port}", prompt["prompt"],
                            key, a.max_tokens, a.model, a.thinking_budget)
                rows.append({"name": prompt.get("name", "prompt"), "run": run,
                            "must": prompt.get("must", []),
                            "off": {**off, "covered": covered(off.get("text", ""), prompt.get("must", []))},
                            "on": {**on, "covered": covered(on.get("text", ""), prompt.get("must", []))}})
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except Exception:  # noqa: BLE001 - a server that will not close is not worth hanging on
                p.kill()

    if a.json:
        print(json.dumps(rows, indent=1))
        return 0

    print(f"control: {base} (tail check off)")
    print(f"test:    http://127.0.0.1:{a.tail_port} (tail check on"
          f"{', attached to the control' if not a.self else ', own mock engine'})")
    print(f"{'prompt':28} {'runs':>4}  {'chars off':>10} {'chars on':>9} {'delta':>7} "
          f"{'checks':>7} {'parts off':>10} {'parts on':>9}")
    tot_chars_off = tot_chars_on = tot_checks = 0
    better = worse = same = 0
    for row in rows:
        off, on = row["off"], row["on"]
        need = len(row["must"])
        c_off, c_on = len(off.get("covered", [])), len(on.get("covered", []))
        chars_off, chars_on = off.get("chars", 0), on.get("chars", 0)
        tot_chars_off += chars_off
        tot_chars_on += chars_on
        tot_checks += on.get("checks", 0)
        if c_on > c_off:
            better += 1
        elif c_on < c_off:
            worse += 1
        else:
            same += 1
        name = row["name"][:28]
        print(f"{name:28} {row['run']:>4}  {chars_off:>10} {chars_on:>9} "
              f"{chars_on - chars_off:>7} {on.get('checks', 0):>7} {c_off}/{need:>9} {c_on}/{need:>8}")

    print("\nsummary")
    print(f"  prompts run          {len(rows)}")
    print(f"  tail checks executed {tot_checks}")
    print(f"  characters           {tot_chars_off} without the check, {tot_chars_on} with it "
          f"({tot_chars_on - tot_chars_off:+d})")
    print(f"  coverage             {better} more complete with the check, {worse} less, {same} the same")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
