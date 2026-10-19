"""serve/config_payload.py - a run config in, a validated config dict out.

A config arrives as a JSON string, a path, a URL or an inline object. This module decides which it
is, slices the object out verbatim, and applies the shared sampling defaults.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def clean_shared_defaults(d) -> dict:
    """The Chat settings other apps get (POST /settings): only known keys, each checked; ValueError names a bad one."""
    if d is None:
        return {}
    if not isinstance(d, dict):
        raise ValueError("defaults must be an object")
    out = {}
    for key, value in d.items():
        if value is None or value == "":
            continue
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if key == "reasoning_effort":
            if value not in ("none", "low", "medium", "high"):
                raise ValueError("reasoning_effort: none, low, medium or high")
        elif key == "temperature":
            if not number or not 0 <= value <= 2:
                raise ValueError("temperature: 0..2")
        elif key == "top_p":
            if not number or not 0 < value <= 1:
                raise ValueError("top_p: 0 < top_p <= 1")
        elif key == "top_k":
            if not number or value != int(value) or not 1 <= value <= 64:
                raise ValueError("top_k: an integer 1..64")
            value = int(value)
        elif key in ("seed", "max_tokens"):
            if not number or value != int(value) or value <= 0:
                raise ValueError(f"{key}: a positive integer")
            value = int(value)
        elif key == "experimental_speed_projection":
            if not isinstance(value, bool):
                raise ValueError("experimental_speed_projection: true or false")
        else:
            raise ValueError(f"unknown setting {key!r}")
        out[key] = float(value) if key in ("temperature", "top_p") else value
    return out

def sampling_defaults_from_config(cfg: dict) -> dict:
    """The run config's optional `sampling` block: defaults for the sampling fields a request leaves out, so
    a plain client gets configured sampling instead of greedy.  Supported: temperature, top_p, top_k, min_p,
    presence_penalty, repetition_penalty, frequency_penalty, penalty_last_n, seed.  The request's own fields
    always win - an explicit temperature=0 still means greedy, a field set to null falls back to the default.
    A bad value refuses to start the server (a typo'd config should not quietly change sampling); unknown keys
    are named at startup and ignored."""
    out = {}
    for key, value in (cfg.get("sampling") or {}).items():
        if value is None:
            continue
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if key == "temperature":
            if not number or value < 0:
                raise SystemExit(f"[hypercore] config sampling.temperature={value!r}: expected a number >= 0 (0 = greedy)")
            out[key] = float(value)
        elif key == "top_p":
            if not number or not 0 < value <= 1:
                raise SystemExit(f"[hypercore] config sampling.top_p={value!r}: expected 0 < top_p <= 1")
            out[key] = float(value)
        elif key == "min_p":
            if not number or not 0 <= value <= 1:
                raise SystemExit(f"[hypercore] config sampling.min_p={value!r}: expected 0 <= min_p <= 1")
            out[key] = float(value)
        elif key == "top_k":
            if not number or value != int(value) or not 1 <= value <= 64:
                raise SystemExit(f"[hypercore] config sampling.top_k={value!r}: the sampled path takes an integer 1..64")
            out[key] = int(value)
        elif key == "presence_penalty":
            if not number or value < 0:
                raise SystemExit(f"[hypercore] config sampling.presence_penalty={value!r}: expected a number >= 0")
            out[key] = float(value)
        elif key == "frequency_penalty":
            if not number or value < 0:
                raise SystemExit(f"[hypercore] config sampling.frequency_penalty={value!r}: expected a number >= 0")
            out[key] = float(value)
        elif key == "repetition_penalty":
            if not number or value <= 0:
                raise SystemExit(f"[hypercore] config sampling.repetition_penalty={value!r}: expected a number > 0 (1 = off)")
            out[key] = float(value)
        elif key == "penalty_last_n":
            if not number or value != int(value) or value < 0:
                raise SystemExit(f"[hypercore] config sampling.penalty_last_n={value!r}: expected a non-negative integer")
            out[key] = int(value)
        elif key == "seed":
            if not number or value != int(value) or value <= 0:
                raise SystemExit(f"[hypercore] config sampling.seed={value!r}: expected a positive integer")
            out[key] = int(value)
        elif key == "experimental_speed_projection":
            if not isinstance(value, bool):
                raise SystemExit(f"[hypercore] config sampling.experimental_speed_projection={value!r}: expected true or "
                                 "false (the default for requests that leave it out, when the engine has the vector)")
            out[key] = value
        else:
            print(f"[hypercore] config sampling.{key}={value!r}: unknown key, ignored", flush=True)
    return out

MAX_CONFIG_BYTES = 10 * 1024 * 1024   # the ceiling hypercore_engine_init also enforces

def verbatim_object_slice(text: str, key: str) -> str | None:
    """The exact characters of `"key": { ... }` in the original document.

    Braces are matched on the raw text, so indentation, key order and spacing survive.
    Returns None when the key is absent or the object is malformed.
    """
    match = re.search(r'"' + re.escape(key) + r'"\s*:\s*\{', text)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None

def resolve_configuration_payload(config_input: str | None) -> str:
    """Return the configuration text the worker will receive, unmutated.

    Inline JSON is forwarded verbatim: whitespace, indentation and key order are kept, so
    a deployment that hashes its config still matches the payload the worker parses.
    A path is read here, once - the worker is never handed a path. The 10 MB ceiling is
    checked before anything is buffered, and an empty file is an error rather than a
    silent fall back to defaults.

    Raises ValueError for an empty file, an oversized payload or a non-object document,
    and FileNotFoundError when the path cannot be opened.
    """
    if not config_input:
        return ""
    if not config_input.strip():
        return ""                      # a blank argument is no configuration, not a bad path

    payload_bytes = len(config_input.encode("utf-8"))
    if payload_bytes > MAX_CONFIG_BYTES:
        raise ValueError(
            f"configuration is {payload_bytes} bytes; the ceiling is {MAX_CONFIG_BYTES}"
        )

    if is_inline_config(config_input):
        json.loads(config_input)          # validate, but never re-serialise
        return config_input               # verbatim

    path = Path(config_input.strip())
    try:
        size = path.stat().st_size
    except OSError as e:
        raise FileNotFoundError(f"configuration file not readable: {path} ({e})") from e
    if size == 0:
        raise ValueError(
            f"configuration file {path} is empty; refusing to run on hidden defaults"
        )
    if size > MAX_CONFIG_BYTES:
        raise ValueError(
            f"configuration file {path} is {size} bytes; the ceiling is {MAX_CONFIG_BYTES}"
        )

    text = path.read_text(encoding="utf-8-sig")   # Notepad adds a BOM
    if not is_inline_config(text):
        raise ValueError(f"configuration file {path} does not contain a JSON object")
    return text

def parse_config_object(text: str) -> dict:
    """Validate a configuration payload as an object, without rewriting it."""
    if not text:
        return {}
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"configuration is not valid JSON: {e}") from e
    if not isinstance(cfg, dict):
        raise ValueError("configuration must be a JSON object")
    return cfg

def is_inline_config(raw: str) -> bool:
    """The same rule the C boundary uses: the first non-whitespace character decides."""
    return bool(raw) and raw.strip().startswith("{")
