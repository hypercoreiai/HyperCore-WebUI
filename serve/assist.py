"""serve/assist.py - bridge to an OpenAI-compatible NVIDIA endpoint for the Assist tab.

The endpoint and key are read from the project .env file, or from the process environment.
The request body is the normal OpenAI Chat Completions shape: model name plus messages.

The dropdown on the Assist tab is built from list_models(). When the allowlist file exists it acts as a
pinned list: only the models in it are offered. It is written by tools/assist_latency_probe.py (or
probe_models() below) after timing every model on the endpoint, and it is plain JSON that can be edited
by hand - adding or removing an id is all it takes. Set HYPERCORE_ASSIST_FAST_LIST to point at a different
file, or delete it to list every model again.

Every conversation and every probe is also written to the call log (HYPERCORE_ASSIST_LOG), which
GET /assist/log reads for the Monitor's "External assistants" card. The chat tab and the MCP bridge are
separate processes, so the file - not memory - is where both can record.

One file each, not two: _converge() merges every copy of an Assist file into a single world-writable one
and deletes the rest, so the pinned count is one number for every account on this box.

These are the bridge's own settings and are deliberately not the local engine's. Nothing here is read
from the model run config or from the sampling fields on the Chat tab: the endpoint is a different engine
with different limits, and inheriting a local temperature or context size would make a failure in one of
them look like a failure in the other.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_ENDPOINT = "https://integrate.api.nvidia.com/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATHS = (PROJECT_ROOT / ".env", Path(__file__).resolve().parent / ".env")
DEFAULT_TIMEOUT = int(os.environ.get("HYPERCORE_ASSIST_TIMEOUT_S", "90"))
FAST_LIST_PATH = Path(os.environ.get("HYPERCORE_ASSIST_FAST_LIST") or PROJECT_ROOT / "serve" / "assist_fast_models.json")
DEFAULT_MAX_LAG = float(os.environ.get("HYPERCORE_ASSIST_MAX_LAG_S", "1.0"))
LOG_PATH = Path(os.environ.get("HYPERCORE_ASSIST_LOG") or PROJECT_ROOT / "serve" / "assist_calls.jsonl")
LOG_KEEP = int(os.environ.get("HYPERCORE_ASSIST_LOG_KEEP", "500"))   # lines kept; 0 turns the log off
# The bridge's own defaults, kept separate from the local engine's run config on purpose: a local
# temperature or context size means nothing to a hosted endpoint.
# max_tokens 1024: the Assist tab clipped answers at 512 (finish_reason "length").
ASSIST_DEFAULTS = {
    "max_tokens": int(os.environ.get("HYPERCORE_ASSIST_MAX_TOKENS", "1024")),
    "timeout_s": DEFAULT_TIMEOUT,
    "max_lag_s": DEFAULT_MAX_LAG,
}
_LOG_LOCK = threading.Lock()


def _sibling(path: Path) -> Path | None:
    """The same file name one directory up - where a legacy copy of an Assist file ended up.

    Two accounts share this checkout and one of them could not write inside serve/, so an older version
    of every Assist file also exists one level above it. Same name, different contents: that is the
    second half of "the popup said 12 and the list still shows 10".
    """
    if path.parent == path.parent.parent:
        return None
    return path.parent.parent / path.name


def _can_write(path: Path) -> bool:
    """Can this account write this path? A file another account owns is readable but not writable."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            with path.open("a"):
                return True
        with path.open("w"):
            pass
        return True
    except OSError:
        return False


def _read_path(path: Path) -> Path:
    """The copy of this name the page should read: the newest one that exists.

    A legacy copy one directory up is a real file with real content; reading the empty path here
    would drop the allowlist the dropdown is built from.
    """
    copies = [p for p in (path, _sibling(path)) if p and p.is_file()]
    if not copies:
        return path
    return max(copies, key=lambda p: p.stat().st_mtime)


def _writable(path: Path) -> tuple[Path, str]:
    """Where this account can write, and the note the Monitor has to show.

    The read-only copy is left in place and the fallback is a copy of its content, not an empty file:
    an empty one would un-pin the dropdown the page is about to read.
    """
    if _can_write(path):
        return path, ""
    note = f"{path} is not writable by this user"
    alt = _sibling(path)
    if alt and _can_write(alt):
        try:
            alt.parent.mkdir(parents=True, exist_ok=True)
            alt.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            alt.chmod(alt.stat().st_mode | 0o66)
        except OSError:
            return path, note
        return alt, f"{note}; using {alt}"
    return path, note


def _stamp(line: str) -> str:
    """The log's own timestamp, for ordering merged lines. A line without one sorts first."""
    try:
        return str(json.loads(line).get("ts") or "")
    except ValueError:
        return ""


def _merge_json(copies: list[Path]) -> str:
    """The newest copy wins: an allowlist is one probe's snapshot, not a history to combine."""
    newest = max(copies, key=lambda p: p.stat().st_mtime)
    return newest.read_text(encoding="utf-8")


def _merge_jsonl(copies: list[Path]) -> str:
    """Every line of every copy, de-duplicated and in time order, capped at LOG_KEEP.

    A line present in both copies is the same request twice; the Monitor should show it once.
    """
    seen: set[str] = set()
    lines: list[str] = []
    for path in copies:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line and line not in seen:
                    seen.add(line)
                    lines.append(line)
        except OSError:
            continue
    lines.sort(key=_stamp)
    if LOG_KEEP > 0:
        lines = lines[-LOG_KEEP:]
    return "\n".join(lines) + ("\n" if lines else "")


def _converge(path: Path, merge) -> tuple[Path, str]:
    """One file per bridge: merge every copy of this name into it, then delete the rest.

    Returns (the path to use, a note about what was merged). The note is printed once at import, so
    the Monitor can name the file the dropdown and the log are actually reading.

    The stale copy is not renamed or kept as a backup: two files with the same name and different
    contents is the bug, and a backup nobody reads is the same bug with a longer name. The merged file
    is opened to 0o66 so the next account can write it too - otherwise the split returns the moment the
    other account runs a probe.
    """
    copies = [p for p in (path, _sibling(path)) if p and p.is_file()]
    content = merge(copies) if copies else ""

    target = path
    if not _can_write(target):
        # The copy here belongs to another account and cannot be opened for writing. Its directory is
        # shared, so dropping it and writing the merged content back under the same name keeps one path
        # for every account instead of a per-user fallback.
        try:
            target.unlink()
        except OSError:
            pass
        if not _can_write(target):
            alt = _sibling(path)
            if alt and _can_write(alt):
                target = alt

    if content:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        try:
            target.chmod(target.stat().st_mode | 0o66)   # open it to the next account
        except OSError:
            pass

    removed = [p for p in copies if p != target]
    for stale in removed:
        try:
            stale.unlink()
        except OSError:
            pass

    if len(copies) > 1:
        return target, (f"merged {len(copies)} copies of {path.name} into {target}; "
                       f"removed {', '.join(str(p) for p in removed)}")
    if target != path:
        return target, f"{path} is not writable by this user; using {target}"
    return target, ""


def _write_text(path: Path, text: str) -> list[Path]:
    """Write the one shared file, open to the next account. Returns what was written.

    There is only one file now; _converge removed the copies. The list return is kept so a probe can
    still report the file it wrote.
    """
    written = []
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(path.stat().st_mode | 0o66)   # open it to the next account
        written.append(path)
    except OSError:
        pass
    return written



# Resolved once at import so every reader and every writer use the same file.
FAST_LIST_PATH, FAST_LIST_NOTE = _converge(FAST_LIST_PATH, _merge_json)
LOG_PATH, LOG_NOTE = _converge(LOG_PATH, _merge_jsonl)
for _note in (FAST_LIST_NOTE, LOG_NOTE):
    if _note:
        print(f"[hypercore] assist: {_note}", file=sys.stderr, flush=True)


# Timing every model on the endpoint takes minutes, not seconds. The request that starts it stays open
# until the file is written, so the page needs a second, cheap route to ask "how far are you?" -
# otherwise a lag check looks like a hung button.
_PROBE_PROGRESS: dict = {"running": False, "total": 0, "done": 0, "started": "", "max_lag": None}
_PROGRESS_LOCK = threading.Lock()


def _clean_value(value: str) -> str:
    value = value.strip()
    if value.startswith(("\"", "'")):
        value = value[1:]
    if value.endswith(("\"", "'")):
        value = value[:-1]
    return value


def load_env() -> tuple[str, str]:
    """Load ENDPOINT/API_KEY from .env, then environment variables. Never returns the key in status output."""
    for path in ENV_PATHS:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and not os.environ.get(key):
                os.environ[key] = _clean_value(value)

    endpoint = os.environ.get("ENDPOINT") or os.environ.get("NVIDIA_ENDPOINT") or DEFAULT_ENDPOINT
    key = os.environ.get("API_KEY") or os.environ.get("NVIDIA_API_KEY")
    if not key:
        raise RuntimeError("No API_KEY found. Put ENDPOINT and API_KEY in .env beside the HyperCore project.")
    return endpoint.rstrip("/"), key


def _api_url(endpoint: str, path: str) -> str:
    endpoint = endpoint.rstrip("/")
    if not endpoint.endswith("/v1") and "/v1" not in endpoint:
        endpoint += "/v1"
    return endpoint + path


def _request(endpoint: str, key: str, path: str, payload: dict | None = None, timeout: int | None = None) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        _api_url(endpoint, path),
        data=body,
        method="POST" if body is not None else "GET",
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or DEFAULT_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            message = parsed.get("error", {}).get("message") or parsed.get("message") or detail
        except ValueError:
            message = detail or exc.reason
        raise RuntimeError(f"NVIDIA endpoint returned {exc.code}: {message}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the NVIDIA endpoint: {exc}") from None


def _clean_messages(messages) -> list[dict]:
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    out = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        if role not in ("user", "assistant", "system"):
            role = "user"
        content = item.get("content") or ""
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(str(part.get("text") or part.get("content") or ""))
                else:
                    parts.append(str(part))
            content = " ".join(p for p in parts if p)
        out.append({"role": role, "content": str(content)})
    return out


def status() -> dict:
    endpoint, key = load_env()
    try:
        listed = _request(endpoint, key, "/models")
        count = len(listed.get("data") or [])
        error = None
    except Exception as exc:
        count = 0
        error = str(exc)
    return {
        "configured": True,
        "endpoint": endpoint,
        "key_present": bool(key),
        "model_count": count,
        "last_error": error,
    }


def _pinned_models() -> list[dict]:
    """The allowlist the dropdown is built from. A bare string is as valid as a {"id": ...} object."""
    try:
        data = json.loads(_read_path(FAST_LIST_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    models = []
    for item in data.get("models") or []:
        model_id = item if isinstance(item, str) else str((item or {}).get("id") or "").strip()
        if model_id:
            # A pinned entry can have been kept without a measurement (the endpoint throttled the probe).
            # The dropdown still offers it; it just has to say so.
            models.append({"id": model_id,
                          "lag_s": (item or {}).get("ttft_s") if isinstance(item, dict) else None,
                          "ok": (item or {}).get("ok", True) if isinstance(item, dict) else True,
                          "error": (item or {}).get("error") or "" if isinstance(item, dict) else ""})
    return models


def list_models(show_all: bool = False) -> dict:
    endpoint, key = load_env()
    data = _request(endpoint, key, "/models")
    catalog = []
    for item in data.get("data") or []:
        model_id = str(item.get("id") or "").strip()
        if model_id:
            catalog.append({"id": model_id, "owned_by": item.get("owned_by"), "created": item.get("created")})
    catalog.sort(key=lambda m: m["id"])

    pinned = _pinned_models()
    if not pinned or show_all:
        return {"endpoint": endpoint, "models": catalog, "pinned": False}
    live = {m["id"] for m in catalog}
    models = [m for m in pinned if m["id"] in live] or pinned
    return {"endpoint": endpoint, "models": models, "pinned": True,
            "filter": {"file": str(FAST_LIST_PATH), "note": FAST_LIST_NOTE or "",
                      **_pinned_meta(),
                       "dropped": [m["id"] for m in pinned if m["id"] not in live]},
            "over_limit": _over_limit_models()}


def _pinned_meta() -> dict:
    try:
        data = json.loads(FAST_LIST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {"updated": data.get("updated"), "max_lag_s": data.get("max_lag_s"), "rounds": data.get("rounds")}


# Why a model is not in the dropdown. "slow" is a judgement about the model; the others are about the
# endpoint at the moment of the probe, and must not be read as one.
TRANSIENT_CODES = (429, 500, 502, 503)
RETRY_DELAY_S = 2.0


def _probe_reason(error) -> str:
    """Classify a probe failure: too slow, not served here, or the endpoint throttled us."""
    if not error:
        return "slow"
    low = str(error).lower()
    if "404" in low or "400" in low or "not found" in low:
        return "not available"
    if "429" in low or "503" in low or "500" in low or "502" in low or "limit reached" in low:
        return "rate limited"
    if "timeout" in low or "timed out" in low:
        return "timed out"
    return "probe failed"


def _over_limit_models() -> list[dict]:
    """The models the last probe did not pin, with the reason - the long list, in the UI."""
    try:
        data = json.loads(FAST_LIST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [{"id": str(m.get("id") or ""), "ttft_s": m.get("ttft_s"), "worst_s": m.get("worst_s"),
            "ok": m.get("ok"), "error": m.get("error"), "reason": m.get("reason") or _probe_reason(m.get("error"))}
            for m in (data.get("over_limit") or []) if m.get("id")]


def _log_lines() -> list[str]:
    """The call log, in time order. One file, one history - see _converge."""
    try:
        out = [line for line in LOG_PATH.read_text(encoding="utf-8").splitlines() if line]
    except OSError:
        return []
    out.sort(key=_stamp)
    return out


def record_call(kind: str, model: str, ok: bool, latency_s: float, usage: dict | None = None,
                chars: int = 0, error: str = "", note: str = "", finish_reason: str = "",
                prompt: str = "") -> dict:
    """Append one line to the call log. Best effort: a log failure never breaks a reply.

    The prompt is kept in truncated form on purpose. Without it a strange answer in the log is only a
    length and a token count - there is no way to ask the same question again and see whether it repeats.
    """
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "kind": kind,
        "model": str(model or ""),
        "ok": bool(ok),
        "latency_s": round(float(latency_s), 3),
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "completion_tokens": (usage or {}).get("completion_tokens"),
        "chars": int(chars),
        # "length" means the model was stopped by max_tokens rather than finishing. A reasoning model cut
        # off that way often ends in token salad, so the Monitor has to be able to say so.
        "finish_reason": str(finish_reason or "")[:40],
        "truncated": str(finish_reason or "") == "length",
        "error": str(error or "")[:300],
        "note": str(note or "")[:300],
        "prompt_head": " ".join(str(prompt or "").split())[:200],
    }
    try:
        if LOG_KEEP > 0:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LOG_LOCK:
                line = json.dumps(entry, ensure_ascii=False)
                lines = _log_lines() + [line]
                _write_text(LOG_PATH, "\n".join(lines[-LOG_KEEP:]) + "\n")
    except OSError:
        pass
    # stderr, not stdout: the MCP bridge writes its JSON protocol on stdout.
    detail = f" ({entry['completion_tokens']} tokens)" if entry["completion_tokens"] else ""
    print(f"[hypercore] assist {kind}: {entry['model'] or '-'} {'ok' if ok else 'failed'} "
          f"in {entry['latency_s']:.2f}s{detail}" + (f": {entry['error'] or entry['note']}" if (entry['error'] or entry['note']) else ""),
          file=sys.stderr, flush=True)
    return entry


def call_log(limit: int = 40) -> dict:
    """What the Monitor shows: the endpoint, the pinned list, the models that were measured but not
    pinned, and the last `limit` calls newest first. No network: everything is read from the files."""
    try:
        endpoint, key = load_env()
        configured, key_present, last_error = True, bool(key), None
    except Exception as exc:
        endpoint, configured, key_present, last_error = None, False, False, str(exc)
    calls = []
    for line in _log_lines()[-max(1, int(limit)):]:
        try:
            calls.append(json.loads(line))
        except ValueError:
            continue
    calls.reverse()
    return {
        "endpoint": endpoint,
        "configured": configured,
        "key_present": key_present,
        "last_error": last_error,
        "pinned": bool(FAST_LIST_PATH.is_file()),
        "filter": {"file": str(FAST_LIST_PATH), "note": FAST_LIST_NOTE or LOG_NOTE, **_pinned_meta()},
        "models": _pinned_models(),
        "over_limit": _over_limit_models(),
        "calls": calls,
        "call_count": len(calls),
        "log_file": str(LOG_PATH) if LOG_KEEP > 0 else None,
    }


def _timed_chat(endpoint: str, key: str, model: str, timeout: int) -> dict:
    """One one-token request, streamed: the lag is the time to the first byte of the first choice."""
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1, "stream": True}
    req = urllib.request.Request(
        _api_url(endpoint, "/chat/completions"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    first = None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for _ in resp:
            if first is None:
                first = time.perf_counter() - started
    if first is None:                                  # the endpoint answered with an empty body
        raise RuntimeError("the model returned an empty response")
    return {"ttft": first, "total": time.perf_counter() - started}


def probe_models(models=None, max_lag: float = DEFAULT_MAX_LAG, rounds: int = 2,
                 workers: int = 8, timeout: int = 15, out: Path | None = None) -> dict:
    """Time every model on the endpoint and write the ones under max_lag to the allowlist.

    The probe's per-request timeout is deliberately short (15 s, not the 90 s a chat call gets): a model
    that takes that long to send its first byte is not a candidate for the fast list, and waiting for it
    is what makes a lag check take half an hour.

    The models that were measured but not pinned are kept in the same file under "over_limit", so the
    Monitor can show why a model is missing from the dropdown instead of that table living only in a
    report file nobody regenerates.
    """
    endpoint, key = load_env()
    started = time.perf_counter()
    if models is None:
        models = [m["id"] for m in list_models(show_all=True)["models"]]
    with _PROGRESS_LOCK:
        _PROBE_PROGRESS.update({"running": True, "total": len(models), "done": 0,
                               "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                               "elapsed_s": 0.0, "max_lag": max_lag})
    results: list[dict] = []
    lock = threading.Lock()
    queue = list(models)

    def worker():
        while True:
            with lock:
                if not queue:
                    return
                model = queue.pop(0)
            samples, error = [], None
            rounds_needed = max(1, rounds)
            attempts = 0
            while attempts < rounds_needed + 2:                 # two extra tries, only for a throttle
                attempts += 1
                try:
                    samples.append(_timed_chat(endpoint, key, model, timeout))
                except urllib.error.HTTPError as exc:
                    error = f"HTTP {exc.code}"
                    if exc.code in TRANSIENT_CODES and len(samples) < rounds_needed:
                        time.sleep(RETRY_DELAY_S)               # a 429 recovers in seconds; it is not a verdict
                        continue
                    break
                except Exception as exc:                        # timeouts, empty responses, unreachable hosts
                    error = str(exc)
                    break
            if not samples:
                results.append({"id": model, "ok": False, "error": error or "no response",
                                "reason": _probe_reason(error)})
            else:
                ttfts = [s["ttft"] for s in samples]
                results.append({"id": model, "ok": True, "reason": "measured",
                               "ttft_s": round(statistics.median(ttfts), 3),
                               "best_s": round(min(ttfts), 3),
                               "worst_s": round(max(ttfts), 3)})
            with _PROGRESS_LOCK:
                _PROBE_PROGRESS["done"] = len(results)
                _PROBE_PROGRESS["elapsed_s"] = round(time.perf_counter() - started, 1)
    threads = [threading.Thread(target=worker) for _ in range(max(1, workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    results.sort(key=lambda r: (not r["ok"], r.get("ttft_s", 9e9)))
    fast = [r for r in results if r["ok"] and r["ttft_s"] < max_lag]
    fast_ids = {r["id"] for r in fast}
    for r in results:
        if r["ok"] and r["id"] not in fast_ids:
            r["reason"] = "over the limit"          # measured, and genuinely slower than the limit
    # A model already on the allowlist is not dropped because the endpoint throttled the probe. Someone was
    # using it; a 429 or a timeout says nothing about how fast it is. Only a measurement over the limit, or
    # a 404 (the endpoint no longer serves it), removes it.
    previously = {m["id"] for m in _pinned_models()}
    held = [r for r in results if r["id"] in previously and not r["ok"]
            and r["reason"] != "not available" and r["id"] not in fast_ids]
    kept = fast + held or results
    kept_ids = {r["id"] for r in kept}
    over = [r for r in results if r["id"] not in kept_ids]
    # A count that moves without an explanation reads as a bug: the dropdown went from 12 to 10 and the
    # page said nothing about which two left. Name them.
    removed = [r for r in results if r["id"] in previously and r["id"] not in kept_ids]
    path = Path(out or FAST_LIST_PATH)
    _write_text(path, json.dumps({
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "endpoint": endpoint,
        "max_lag_s": max_lag,
        "rounds": rounds,
        "models": [{"id": r["id"], "ttft_s": r.get("ttft_s"), "best_s": r.get("best_s"),
                    "worst_s": r.get("worst_s"), "ok": r["ok"], "error": r.get("error"),
                    "reason": r.get("reason") or "measured"} for r in kept],
        "over_limit": [{"id": r["id"], "ttft_s": r.get("ttft_s"), "worst_s": r.get("worst_s"),
                        "ok": r["ok"], "error": r.get("error"),
                        "reason": r.get("reason") or "measured"} for r in over],
    }, indent=1) + "\n")
    with _PROGRESS_LOCK:
        _PROBE_PROGRESS.update({"running": False, "done": len(results),
                               "elapsed_s": round(time.perf_counter() - started, 1)})
    record_call("probe", f"{len(fast)} of {len(results)} models", True, time.perf_counter() - started,
                note=f"max lag {max_lag}s, {max(1, rounds)} rounds -> {path}"
                     + (f"; {len(held)} already pinned kept despite a failed probe" if held else "")
                     + (f"; {len(removed)} dropped from the list: " + ", ".join(
                         f"{r['id']} ({r['reason']})" for r in removed) if removed else ""))
    return {"endpoint": endpoint, "probed": len(results),
            "kept": len(fast), "fast": len(fast), "held": len(held), "pinned": len(kept),
            "previous": len(previously), "removed": removed,
            "max_lag_s": max_lag, "file": str(path), "note": FAST_LIST_NOTE or "",
            "models": results, "allowlist": kept,
            "over_limit": over}


def probe_progress() -> dict:
    """How far the running "Check lag" probe is. The POST that starts it stays open until the allowlist
    is written, so the page asks this route every couple of seconds instead of staring at a dead button."""
    with _PROGRESS_LOCK:
        return dict(_PROBE_PROGRESS)


def converse(model: str, messages, max_tokens: int | None = None) -> dict:
    """One chat request to the external endpoint. No field in the payload comes from the local engine's
    settings: it is the model name, the messages, and this bridge's own token cap."""
    endpoint, key = load_env()
    model = str(model or "").strip()
    if not model:
        raise ValueError("A model name is required.")

    payload = {
        "model": model,
        "messages": _clean_messages(messages),
        "max_tokens": max(1, int(max_tokens or ASSIST_DEFAULTS["max_tokens"])),
    }
    started = time.perf_counter()
    try:
        data = _request(endpoint, key, "/chat/completions", payload)
    except Exception as exc:
        record_call("chat", model, False, time.perf_counter() - started, error=str(exc))
        raise
    choices = data.get("choices") or []
    if not choices:
        record_call("chat", model, False, time.perf_counter() - started,
                    error=f"the model returned no choices: {json.dumps(data)[:200]}")
        raise RuntimeError(f"The model returned no choices: {json.dumps(data)[:500]}")

    message = choices[0].get("message") or {}
    text = message.get("content") or message.get("reasoning_content") or message.get("reasoning") or ""
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    asked = next((str(m.get("content") or "") for m in reversed(payload["messages"])
                  if m.get("role") == "user"), "")
    record_call("chat", data.get("model") or model, True, time.perf_counter() - started,
                usage=data.get("usage") or {}, chars=len(text),
                finish_reason=choices[0].get("finish_reason") or "", prompt=asked)
    return {
        "model": data.get("model") or model,
        "text": text,
        "reasoning": reasoning,
        "usage": data.get("usage") or {},
        "id": data.get("id") or "",
        "finish_reason": choices[0].get("finish_reason") or "",
    }