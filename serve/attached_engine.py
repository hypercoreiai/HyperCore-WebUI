#!/usr/bin/env python3
"""serve/attached_engine.py - run the WebUI against an engine that is already up.

A Strata engine is not a small process: the binary is 77 MB and the weights it reads
are tens of GB. Two HyperCore servers on one machine cannot both hold it, so the
second one attaches to the first instead of starting a second copy of the model.

The boundary is the same as every other engine here:

    engine.generate(prompt_ids, max_new, sampling, cancel) -> iterator of token ids

What differs is how the tokens arrive. A direct engine answers on stdin/stdout; an
attached one answers over the HTTP API the running server already speaks - OpenAI
POST /v1/chat/completions with stream=true. The prompt is decoded with the model's
own tokenizer, sent as one message, and every text delta that comes back is encoded
again, so the Chat tab, the token counters and the Monitor see a token stream in the
same shape as from a local engine.

What attaching does not do: it does not own the engine. Stop, unload and VRAM resize
belong to the server that started it. The Build tab's restart is a no-op that says
so, and the Monitor names the engine's address instead of pretending to control it.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

HEALTH_PATHS = ("/health", "/api/health")
FIRST_TOKEN_TIMEOUT_S = 600.0
# The text we send is re-rendered by the other server's chat template, so the prompt the
# engine counts is a little longer than the ids we were given. A tool turn is where this
# bites: the tool definitions alone are thousands of tokens, and a request that asks for
# the whole remaining context is then refused with 400 before a token is generated.
TEMPLATE_SLACK = 512


class AttachedEngineError(RuntimeError):
    pass


class AttachedEngine:
    """A Strata engine running somewhere else, reached over its HTTP API."""

    attached = True     # the server that started the engine owns it, not this one

    def __init__(self, base_url: str, api_key: str = "", tokenizer=None,
                 model: str = "", timeout_s: float = FIRST_TOKEN_TIMEOUT_S):
        self.base = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.tokenizer = tokenizer
        self.model = model or "attached"
        self.timeout_s = float(timeout_s)
        self.max_context = 0
        self.version = None
        self.info: dict[str, object] = {}
        self.last: dict[str, object] | None = None
        self.progress: tuple[int, int] | None = None
        self.progress_ms = 0.0
        self.prefill_tok_s_mean: float | None = None
        self.reused = 0
        self.last_err = ""
        self.ended = True
        self.unloaded = False
        self.batch = 0
        self.log_path = None
        self.lock = threading.Lock()
        self._health_cache: tuple[float, dict] | None = None

    # ---------------------------------------------------------------- the wire
    def _request(self, path: str, body: dict | None = None, timeout: float | None = None):
        req = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                    **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
        )
        return urllib.request.urlopen(req, timeout=timeout or self.timeout_s)

    def _send(self, body: dict):
        """POST a chat request, fitting max_tokens to the room the engine says is left.

        The first attempt is sent as asked. A 400 that names the largest max_tokens that
        would fit is retried once with it: the engine's own count is the only accurate one,
        since it includes its chat template and any tool block it renders itself.
        """
        for attempt in range(2):
            try:
                return self._request("/v1/chat/completions", body)
            except urllib.error.HTTPError as exc:
                detail = _body_text(exc)
                fit = _fits_max_tokens(detail)
                if exc.code != 400 or fit is None or fit < 1 or body.get("max_tokens") <= fit:
                    raise AttachedEngineError(
                        f"the attached engine refused the request ({exc.code}): {detail}") from None
                body["max_tokens"] = fit
            except (urllib.error.URLError, OSError) as exc:
                raise AttachedEngineError(f"the attached engine refused the request: {exc}") from None
        raise AttachedEngineError("the attached engine refused the request twice")

    def _json(self, path: str, body: dict | None = None, timeout: float = 10.0) -> dict:
        try:
            with self._request(path, body, timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            raise AttachedEngineError(
                f"the engine at {self.base} answered {exc.code} on {path}: {_body_text(exc)}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise AttachedEngineError(f"the engine at {self.base} did not answer: {exc}") from None

    def health(self, refresh: bool = False) -> dict:
        """The running engine's own /health, cached for a few seconds."""
        now = time.time()
        if not refresh and self._health_cache and now - self._health_cache[0] < 5.0:
            return self._health_cache[1]
        out: dict[str, object] = {}
        for path in HEALTH_PATHS:
            try:
                out = self._json(path)
                break
            except AttachedEngineError:
                continue
        self._health_cache = (now, out)
        return out

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        with self.lock:
            h = self.health(refresh=True)
            if not h:
                raise AttachedEngineError(f"nothing is listening at {self.base}")
            self.max_context = int(h.get("max_context") or 0)
            self.model = h.get("model") or self.model
            self.ended = False
            self.unloaded = False
            self.info = {"attached": True, "engine_url": self.base,
                        "model": self.model, "max_context": self.max_context,
                        "loaded": bool(h.get("loaded", True))}
            try:
                status = self._json("/v1/status")
                self.version = status.get("engine") or status.get("version")
                self.info.update({"version": self.version, "service": status.get("service")})
            except AttachedEngineError:
                pass
            if self.max_context <= 0:
                raise AttachedEngineError(f"the engine at {self.base} reported no context size")
            return True

    def alive(self) -> bool:
        if self.ended:
            return False
        try:
            return bool(self.health().get("loaded", True))
        except AttachedEngineError:
            return False

    def exit_code(self) -> int | None:
        return None

    def death_note(self) -> str:
        return self.last_err or f"the engine at {self.base} is not reachable"

    def stop(self) -> None:
        """We do not own this engine: stop only means stop talking to it."""
        self.ended = True

    def unload(self) -> None:
        """Ask the engine's own server to give the GPU back.

        The Tune tab needs the card free before a sweep; the engine is not ours to
        kill, but its server can unload it, and /load brings it back.
        """
        try:
            self._json("/unload", {}, timeout=120.0)
            self.unloaded = True
            self._health_cache = None          # the engine's own state has changed
        except AttachedEngineError:
            pass

    def restart(self) -> bool:
        try:
            self._json("/load", {}, timeout=180.0)
        except AttachedEngineError:
            pass
        self._health_cache = None
        try:
            return self.start()
        except AttachedEngineError:
            return False

    def vram(self, reserve_mib: int | None) -> dict:
        raise ValueError("an attached engine's VRAM is set by the server that started it")

    def slots_view(self) -> list[dict[str, object]]:
        return []

    def session_file(self, action: str, path: str) -> dict:
        return {"status": "unsupported", "note": "the attached engine's sessions belong to its own server"}

    # ---------------------------------------------------------------- requests
    def generate(self, ids: list[int], max_new: int, sampling: dict | None = None,
                 cancel: threading.Event | None = None, embeddings=None) -> Iterator[int]:
        if cancel is None:
            cancel = threading.Event()
        with self.lock:
            if not self.alive():
                raise AttachedEngineError("the attached engine is not running")
            self.progress, self.progress_ms, self.reused = None, 0.0, 0
            body = {"messages": [{"role": "user", "content": self._text(ids)}],
                    "max_tokens": int(max_new), "stream": True,
                    **_openai_sampling(sampling or {})}
            if self.max_context:
                body["max_tokens"] = max(1, min(int(max_new),
                                               self.max_context - len(ids) - TEMPLATE_SLACK))
            resp = self._send(body)

            started = time.perf_counter()
            generated = 0
            try:
                for raw in resp:
                    if cancel.is_set():
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    piece = delta.get("content") or delta.get("reasoning_content") or ""
                    if piece:
                        # The engine's stream gives one text piece per model token, so
                        # encoding a piece back through the model's tokenizer recovers
                        # the ids the Chat tab and the token counters need.
                        for tok in (self.tokenizer.encode(piece, parse_special=True)
                                   if self.tokenizer else list(piece.encode("utf-8"))):
                            generated += 1
                            yield tok
                    if chunk.get("prompt_progress"):
                        pp = chunk["prompt_progress"]
                        self.progress = (int(pp.get("read") or 0), int(pp.get("total") or 0))
                    if delta.get("finish_reason"):
                        break
            except (urllib.error.URLError, OSError) as exc:
                self.last_err = str(exc)
                raise AttachedEngineError(f"the attached engine stopped mid-request: {exc}") from None
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001 - closing a stream that is already gone is not an error
                    pass
            self.last = {"generated": generated, "prompt_tokens": len(ids),
                        "decode_ms": round((time.perf_counter() - started) * 1000, 1),
                        "finish": "stop", "attached": True}

    def _text(self, ids: list[int]) -> str:
        if self.tokenizer is not None:
            return self.tokenizer.decode(list(ids))
        return " ".join(str(int(i)) for i in ids)


def _fits_max_tokens(detail: str) -> int | None:
    """The max_tokens a refusal says would fit, from text like 'at most 255930 here'."""
    for pattern in (r"at most (\d+)", r"max_tokens \(at most (\d+)\)", r"room left: (\d+)"):
        m = re.search(pattern, detail)
        if m:
            return int(m.group(1))
    return None


def _body_text(exc: urllib.error.HTTPError) -> str:
    """The engine's own reason for a 4xx, which is the only part of a refusal that can be
    acted on: 'no room in the context' and 'bad model name' look the same from the status.
    """
    try:
        text = exc.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 - the body may already be consumed
        text = ""
    if not text:
        return exc.reason or "no reason given"
    try:
        data = json.loads(text)
        message = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
        if message:
            text = str(message)
    except ValueError:
        pass
    return text[:600]


def _openai_sampling(sampling: dict) -> dict:
    out: dict[str, object] = {}
    for key in ("temperature", "top_p", "top_k", "min_p", "seed",
                "repetition_penalty", "frequency_penalty", "presence_penalty"):
        value = sampling.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
    return out
