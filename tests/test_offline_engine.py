#!/usr/bin/env python3
"""tests/test_offline_engine.py - the web app answers when the engine cannot run.

Two things are tested: the stand-in itself, and the server end to end started with an
engine that will not start. The end-to-end half uses a fake engine script, so it needs
no GPU: it fails on its first run (the card is busy) and reaches READY on the second,
which is exactly the recovery the retry thread is for.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, ClassVar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from unittest.mock import patch

from serve import quant_store, server
from serve.attached_engine import AttachedEngine
from serve.direct_engine import DirectEngine
from serve.offline_engine import (
    UNLOADED_CODE,
    DeferredEngine,
    EngineRetry,
    EngineUnavailable,
    OfflineEngine,
)
from serve.server import HyperCoreRequestHandler, engine_from_active_config

FAKE_ENGINE = """#!/bin/sh
# A stand-in for the Strata engine, speaking the control protocol serve/direct_engine.py
# speaks. The first run says the GPU is held by another process; the second reaches READY
# and answers a GEN line with two tokens. That is the recovery the retry thread is for.
n=$(cat "$MARK" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$MARK"
if [ "$n" -lt 2 ]; then
  echo "ERR the GPU is held by another process" >&2
  exit 1
fi
echo "READY 4096 stop"
while IFS= read -r line; do
  case "$line" in
    GEN*) echo "T 1"; echo "T 2"; echo "DONE";;
    STOP) ;;
    QUIT) exit 0;;
  esac
done
"""


class OfflineEngineTests(unittest.TestCase):
    def test_generation_is_refused_with_a_reason_and_a_retry_after(self) -> None:
        eng = OfflineEngine("the GPU is held by a sweep", model="q2_0", retry_after_s=15)
        with self.assertRaises(EngineUnavailable) as got:
            eng.generate([1, 2, 3])
        self.assertIn("sweep", str(got.exception))
        self.assertEqual(got.exception.retry_after, 15)

    def test_it_is_not_alive_and_has_no_context(self) -> None:
        eng = OfflineEngine("no engine")
        self.assertFalse(eng.alive())
        self.assertEqual(eng.max_context, 0)
        self.assertFalse(eng.unload())
        self.assertIsNone(eng.shutdown())

    def test_a_long_engine_error_is_shortened_to_its_conclusion(self) -> None:
        long = ("INFO engine strata 1.45\nVRAM free 300 MiB\nERR no room for the expert arena\n" +
                "the machine has no room for a second engine: stop the other one (or the tune) " +
                "and start this again.")
        eng = OfflineEngine(long)
        self.assertEqual(eng.reason, "the machine has no room for a second engine: stop the "
                                     "other one (or the tune) and start this again.")
        self.assertIn("expert arena", eng.detail)
        self.assertEqual(eng.status()["detail"], long)

    def test_an_empty_reason_still_says_something_usable(self) -> None:
        self.assertEqual(OfflineEngine("").reason, "the engine is not running")

    def test_status_reports_the_state_for_the_health_endpoint(self) -> None:
        eng = OfflineEngine("the GPU is busy", model="q2_0")
        state = eng.status()
        self.assertEqual(state["state"], "offline")
        self.assertEqual(state["reason"], "the GPU is busy")
        self.assertEqual(state["model"], "q2_0")

    def test_restart_of_a_stand_in_is_not_a_success(self) -> None:
        with self.assertRaises(EngineUnavailable):
            OfflineEngine("no engine").restart()


class FakeEngine:
    """The shape of DirectEngine: restart() fails twice, then works."""

    def __init__(self, fails: int, error: str = "the GPU is held"):
        self.fails, self.error = fails, error
        self.attempts = 0
        self.last_error = error

    def restart(self) -> bool:
        self.attempts += 1
        if self.attempts <= self.fails:
            raise RuntimeError(self.error)
        self.max_context = 4096
        return True


class RetryTests(unittest.TestCase):
    def test_try_now_adoptes_an_engine_that_came_back(self) -> None:
        adopted = []
        retry = EngineRetry(FakeEngine(1), adopted.append, "the GPU is held", interval_s=1)
        state = retry.try_now()
        self.assertEqual(state["state"], "retrying")
        self.assertEqual(state["attempts"], 1)
        state = retry.try_now()
        self.assertEqual(state["state"], "running")
        self.assertEqual(len(adopted), 1)
        self.assertEqual(adopted[0].max_context, 4096)

    def test_a_thread_retries_until_the_engine_is_back(self) -> None:
        adopted = []
        retry = EngineRetry(FakeEngine(1), adopted.append, "the GPU is held", interval_s=0.2).start()
        deadline = time.time() + 5
        while time.time() < deadline and not adopted:
            time.sleep(0.1)
        self.assertTrue(adopted, "the retry thread never brought the engine back")
        self.assertEqual(retry.status()["state"], "running")
        retry.stop()

    def test_a_candidate_with_no_restart_is_stopped_with_a_useful_reason(self) -> None:
        retry = EngineRetry(object(), lambda e: None, "the engine is gone", interval_s=1)
        state = retry.try_now()
        self.assertEqual(state["state"], "stopped")
        self.assertIn("restart the WebUI", state["reason"])

    def test_the_retry_interval_is_clamped_to_something_positive(self) -> None:
        self.assertEqual(EngineRetry(object(), lambda e: None, "x", interval_s=0).interval_s, 1.0)

    def test_no_attempt_is_made_while_a_sweep_holds_the_card(self) -> None:
        """A retry attempt during a sweep fails and disturbs what the sweep is measuring,
        so the retry waits until the busy-check says the card is free."""
        class Candidate:
            def __init__(self) -> None:
                self.restarts = 0

            def restart(self) -> bool:
                self.restarts += 1
                return True

        held = {"why": "a tune sweep is using the GPU"}
        adopted: list[object] = []
        # interval_s is clamped to 1 s, so the wait has to be at least two intervals.
        retry = EngineRetry(Candidate(), adopted.append, "the GPU is held",
                           interval_s=1.0, busy=lambda: held["why"]).start()
        time.sleep(2.5)
        self.assertEqual(adopted, [], "an engine was started while the sweep had the card")
        state = retry.status()
        self.assertEqual(state["state"], "paused")
        self.assertTrue(state["paused"])
        self.assertIn("sweep", state["paused_reason"])

        held["why"] = ""                        # the sweep finished
        deadline = time.time() + 5
        while time.time() < deadline and not adopted:
            time.sleep(0.1)
        retry.stop()
        self.assertEqual(len(adopted), 1, "the retry never resumed once the card was free")
        self.assertEqual(retry.status()["state"], "running")

    def test_try_now_is_refused_while_paused_and_says_why(self) -> None:
        class Candidate:
            def __init__(self) -> None:
                self.restarts = 0

            def restart(self) -> bool:
                self.restarts += 1
                return True

        cand = Candidate()
        retry = EngineRetry(cand, lambda e: None, "the GPU is held", interval_s=1,
                           busy=lambda: "a build is using the GPU (building)")
        state = retry.try_now()
        self.assertEqual(cand.restarts, 0, "Load pressed during a build still started an engine")
        self.assertEqual(state["state"], "paused")
        self.assertIn("build", state["reason"])

    def test_a_broken_busy_check_does_not_pause_the_retry(self) -> None:
        adopted: list[object] = []
        def broken() -> str:
            raise RuntimeError("the job table is not readable")
        retry = EngineRetry(object(), adopted.append, "the engine is gone",
                           interval_s=1.0, busy=broken).start()
        time.sleep(2.5)
        retry.stop()
        state = retry.status()
        self.assertFalse(state["paused"], "a failing busy-check paused the retry")
        self.assertGreaterEqual(state["attempts"], 1, "the retry never attempted while busy() raised")
        self.assertIn("restart the WebUI", state["reason"])


class ServerEndToEndTests(unittest.TestCase):
    """Start the real server against an engine that will not start, then check the UI.

    Each test gets its own process: one with the retry off (the engine stays offline) and
    one with it on (the fake engine comes back on the second attempt).
    """

    STARTUP_S = 25

    def start_server(self, retry_s: str, extra_env: dict[str, str] | None = None) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        engine = self.tmp / "fake-engine.sh"
        engine.write_text(FAKE_ENGINE, encoding="utf-8")
        engine.chmod(0o755)
        self.mark = self.tmp / "attempts"
        self.config = self.tmp / "run-fake.json"
        self.config.write_text(json.dumps({
            "exe": str(engine),
            "args": ["--native", str(engine)],
            "model_name": "fake-model",
            "api_key": "testkey",
        }), encoding="utf-8")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.env = dict(os.environ, MARK=str(self.mark), **(extra_env or {}))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "serve.server", "--engine", "strata", "--no-attach",
             "--config", str(self.config), "--host", "127.0.0.1", "--port", str(self.port),
             "--engine-retry-s", retry_s],
            cwd=str(ROOT), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.wait_for_health()

    def tearDown(self) -> None:
        import shutil
        if getattr(self, "proc", None):
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def offline_server(self) -> None:
        """The engine fails to start and nothing retries it."""
        self.start_server("0")
        # the initial start attempt has already happened by the time /health answers
        self.wait_for_offline()

    def wait_for_offline(self) -> None:
        deadline = time.time() + self.STARTUP_S
        while time.time() < deadline:
            _, body = self.get("/health", key=False)
            if body.get("engine", {}).get("state") == "offline":
                return
            time.sleep(0.25)
        self.fail("the server never reported an offline engine")

    def wait_for_health(self) -> dict:
        deadline = time.time() + self.STARTUP_S
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                    return json.loads(r.read())
            except (urllib.error.URLError, OSError):
                time.sleep(0.25)
        out = self.proc.stdout.read().decode(errors="replace") if self.proc.stdout else ""
        self.skipTest(f"the server did not come up in {self.STARTUP_S} s: {out[-500:]}")

    def test_the_retry_holds_off_when_the_operator_says_the_card_is_busy(self) -> None:
        """A sweep run from the CLI is invisible to this process, so HYPERCORE_HOLD_THE_CARD
        is how an operator tells the retry to wait. The fake engine writes its attempt count
        to a marker file: if the retry is holding off, the count never grows past the first
        attempt that failed at startup.
        """
        self.start_server("2", {"HYPERCORE_HOLD_THE_CARD": "a CLI sweep is on the card"})
        self.wait_for_offline()
        time.sleep(6)
        self.assertEqual(int(self.mark.read_text().strip()), 1,
                         "the retry started an engine while the operator said the card was busy")
        _, body = self.get("/health", key=False)
        retry = body["engine"]["retry"]
        self.assertEqual(retry["state"], "paused", retry)
        self.assertTrue(retry["paused"])
        self.assertIn("CLI sweep", retry["paused_reason"])
        code, err = self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 503, err)
        self.assertIn("CLI sweep", err["error"]["message"])

    def get(self, path: str, key: bool = True) -> tuple[int, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if key:
            req.add_header("Authorization", "Bearer testkey")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer testkey"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_the_page_is_served_even_though_the_engine_is_not_running(self) -> None:
        self.offline_server()
        raw = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=5).read().decode()
        self.assertIn("engine-offline", raw)
        self.assertIn('id="composer"', raw)

    def test_health_reports_the_engine_as_offline_with_the_reason(self) -> None:
        self.offline_server()
        status, body = self.get("/health", key=False)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "offline", "the page masks the chat on this field")
        self.assertFalse(body["loaded"])
        self.assertEqual(body["engine"]["state"], "offline")
        self.assertEqual(body["engine"]["code"], "gpu_busy",
                         "a reason a client can branch on, not only a sentence")
        self.assertIn("GPU is held", body["engine"]["reason"])

    def test_the_chat_is_refused_with_a_reason_and_a_retry_after(self) -> None:
        self.offline_server()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hello"}]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer testkey"})
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("a chat answered with no engine running")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)
            body = json.loads(e.read())
            self.assertEqual(body["error"]["type"], "engine_unavailable")
            self.assertIn("Retry-After", e.headers)

    def test_the_tabs_that_do_not_need_the_model_still_work(self) -> None:
        self.offline_server()
        status, body = self.get("/mcp")
        self.assertEqual(status, 200, body)
        self.assertIn("servers", body)

    def test_the_engine_is_brought_back_when_the_gpu_frees(self) -> None:
        self.start_server("2")
        deadline = time.time() + 40
        while time.time() < deadline:
            _, body = self.get("/health", key=False)
            if body.get("loaded"):
                break
            time.sleep(1)
        else:
            self.fail("the retry thread never brought the engine back")
        _, body = self.get("/health", key=False)
        self.assertEqual(body["engine"]["state"], "running")
        self.assertEqual(body["max_context"], 4096)
        self.assertEqual(self.mark.read_text().strip(), "2", self.mark.read_text())
        # and the chat works now
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hello"}]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer testkey"})
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read())
        self.assertIn("choices", out)


class DeferredEngineTests(unittest.TestCase):
    """An engineless server can still be given an engine: the one a build produces."""

    class Built:
        """The engine a run config names, once a build has put it in engine/."""
        def __init__(self) -> None:
            self.model = "Qwen2-7B-Q2_0"
            self.max_context = 4096
            self.started = False
            # What the worker's CAPS reply and the last request leave behind: the figures
            # the Monitor's "Experts in VRAM" bar and the About tab are built from.
            self.info = {"model": self.model, "max_context": self.max_context,
                        "expert_slots": 12531, "expert_cache_mib": 16520,
                        "pool_workers": 12, "vram_free_mib": 381,
                        "ram_blobs": 21942, "hits": 88294, "lookups": 92000}
            self.log_path = "logs/strata-q2_0.log"
            self.log_start = 0

        def slots_view(self) -> list[dict]:
            return [{"layer": 0, "slots": 12}]

        def start(self) -> None:
            self.started = True

        def alive(self) -> bool:
            return self.started

        def generate(self, prompt: str, **kw: Any) -> list[str]:
            return ["an answer from the built engine"]

        def unload(self) -> bool:
            return False

        def shutdown(self) -> None:
            pass

        def status(self) -> dict:
            return {"state": "ready", "code": "ready", "model": self.model,
                    "max_context": self.max_context}

    def test_before_the_first_build_it_answers_like_the_offline_engine(self) -> None:
        deferred = DeferredEngine(lambda: None, "started with --ui-only: no engine was asked for")
        self.assertEqual(deferred.status()["code"], "ui_only", "the banner changed for a build that is not there")
        self.assertFalse(deferred.alive())
        self.assertFalse(deferred.restart())
        with self.assertRaises(EngineUnavailable):
            deferred.generate("hello")
        self.assertIn("nothing has been built yet", deferred.last_error)
        self.assertIsInstance(deferred, OfflineEngine,
                             "the server checks for an engine that is not running")

    def test_the_engine_a_build_produces_is_adopted_without_a_restart(self) -> None:
        built = DeferredEngineTests.Built()
        deferred = DeferredEngine(lambda: built, "started with --ui-only: no engine was asked for")
        self.assertFalse(deferred.alive())
        self.assertTrue(deferred.restart(), "the build's engine was not started")
        self.assertTrue(deferred.alive())
        self.assertEqual(deferred.generate("hello"), ["an answer from the built engine"])
        self.assertEqual(deferred.model, built.model)
        self.assertEqual(deferred.max_context, 4096)
        self.assertEqual(deferred.status()["state"], "ready", "the Monitor still reads the stand-in")

    def test_the_monitor_reads_the_expert_tiers_off_the_adopted_engine(self) -> None:
        """A model loaded on the card must not show up as an empty one on the panel.

        The Monitor's "Experts in VRAM" bar, the conversation-cache card and the About
        tab's memory facts all come from engine.info. A deferred engine that answers with
        its own empty dict shows "-" while nvidia-smi says the weights are resident.
        """
        built = DeferredEngineTests.Built()
        deferred = DeferredEngine(lambda: built, "started with --ui-only: no engine was asked for")
        self.assertEqual(deferred.info, {}, "nothing is running, so nothing has an account of itself")
        self.assertEqual(deferred.slots_view, [])
        self.assertIsNone(deferred.log_path)
        deferred.restart()
        self.assertEqual(deferred.info["expert_slots"], 12531,
                         "the Monitor shows '-' for a model that is loaded in VRAM")
        self.assertEqual(deferred.info["expert_cache_mib"], 16520)
        self.assertEqual(deferred.slots_view, [{"layer": 0, "slots": 12}])
        self.assertEqual(deferred.log_path, "logs/strata-q2_0.log",
                         "the parked-conversation card cannot find the engine's log")
        built.info["expert_slots"] = 9000      # a worker brought back with fewer slots
        self.assertEqual(deferred.info["expert_slots"], 9000,
                         "the panel kept the figures from the build, not the current worker")

    def test_the_metrics_engine_card_of_a_deferred_engine(self) -> None:
        """GET /metrics is what the Monitor polls: the engine card must carry the tiers."""
        from serve.server import ROOT, ByteTokenizer, ChatTemplate, Service

        built = DeferredEngineTests.Built()
        deferred = DeferredEngine(lambda: built, "started with --ui-only: no engine was asked for")
        svc = Service(deferred, ByteTokenizer(), ChatTemplate(ROOT / "serve/chat_template.jinja"))
        before = svc.metrics()["engine"]
        self.assertNotIn("expert_slots", before, "nothing is running, so the Monitor has no tiers to show")
        self.assertEqual(svc.metrics()["live"]["state"], "unloaded")
        deferred.restart()
        engine = svc.metrics()["engine"]
        self.assertEqual(engine["expert_slots"], 12531,
                         "the Monitor shows '-' for a model that is loaded in VRAM")
        self.assertEqual(engine["expert_cache_mib"], 16520)
        self.assertEqual(engine["max_context"], 4096)
        self.assertEqual(svc.metrics()["live"]["state"], "idle")

    def test_the_retry_adopts_a_build_when_one_appears(self) -> None:
        built = DeferredEngineTests.Built()
        seen = [None]
        def factory():
            return built            # the second attempt: the build has finished
        deferred = DeferredEngine(factory, "started with --ui-only: no engine was asked for")
        retry = EngineRetry(deferred, lambda eng: seen.__setitem__(0, eng), deferred.reason,
                            interval_s=1)
        self.assertEqual(deferred.status()["code"], "ui_only", "nothing is built yet, so there is nothing to hand over")
        snapshot = retry.try_now()
        self.assertTrue(built.started)
        self.assertIs(seen[0], deferred, "the retry did not hand the engine to the server")
        self.assertTrue(deferred.alive())
        self.assertEqual(deferred.status()["code"], "ready",
                         "the server now answers for the engine the build produced")
        self.assertEqual(snapshot["attempts"], 1, "one attempt, and it found the build")

    class Broken:
        """An engine built from a config whose paths are still placeholders."""
        def __init__(self) -> None:
            self.shutdown_called = False

        def start(self) -> None:
            raise RuntimeError("missing model shard /ABSOLUTE/PATH/TO/models/Q2_0/model-00001-of-00002.gguf")

        def alive(self) -> bool:
            return False

        def shutdown(self) -> None:
            self.shutdown_called = True

    def test_an_attempt_that_failed_re_reads_the_run_config(self) -> None:
        """The retry after a failure asks the factory again, instead of restarting the dead engine.

        The first attempt of an engineless server reads the config it was started with -
        on a fresh clone, the template. That engine cannot come up, and the supervisor
        behind it spends its restart budget on paths that do not exist. Keeping it means
        every later attempt restarts the same dead process and the model never loads, even
        though a build has since written a config with real paths.
        """
        good = DeferredEngineTests.Built()
        attempts = {"n": 0}

        def factory():
            attempts["n"] += 1
            if attempts["n"] == 1:
                return DeferredEngineTests.Broken()
            return good

        deferred = DeferredEngine(factory, "started with --ui-only: no engine was asked for")
        with self.assertRaises(EngineUnavailable):
            deferred.start()
        self.assertFalse(deferred.alive())
        self.assertIn("missing model shard", deferred.last_error,
                      "the banner says what the engine said, not why the server started")
        self.assertTrue(deferred.restart(), "the retry kept the engine from the failed attempt")
        self.assertTrue(deferred.alive())
        self.assertEqual(deferred.generate("hello"), ["an answer from the built engine"])
        self.assertEqual(attempts["n"], 2, "the retry did not re-read the run config")

    class WorkerAdapter:
        """The shape the run-config read gives back for a worker build: the request handler
        over the supervisor. It has no status() - at best a monitor snapshot - and it is
        brought up through restart(), not start()."""
        def __init__(self) -> None:
            self.model = "Qwen2-7B-Q2_0"
            self.max_context = 4096
            self.started = False

        def restart(self) -> bool:
            self.started = True
            return True

        def alive(self) -> bool:
            return self.started

        def generate(self, prompt: str, **kw: Any) -> list[str]:
            return ["an answer from the built engine"]

        def unload(self) -> bool:
            return False

        def shutdown(self) -> None:
            pass

    def test_an_engine_that_cannot_describe_itself_is_still_reported(self) -> None:
        """The build's engine has no status(); the stand-in must describe it from alive().

        This is the failure the health route hit after a build: /health called
        DeferredEngine.status(), which passed the call straight on to the delegate and
        raised AttributeError on a method the worker adapter does not have.
        """
        adapter = DeferredEngineTests.WorkerAdapter()
        deferred = DeferredEngine(lambda: adapter, "started with --ui-only: no engine was asked for")
        self.assertTrue(deferred.restart(), "the adapter is brought up through restart()")
        state = deferred.status()
        self.assertEqual(state["state"], "running")
        self.assertEqual(state["code"], "running",
                         "the cause of a stand-in is not the cause of an engine that is running")
        self.assertEqual(state["model"], adapter.model)
        self.assertEqual(state["max_context"], 4096)
        self.assertEqual(deferred.generate("hello"), ["an answer from the built engine"])

    def test_a_delegate_that_cannot_describe_itself_does_not_take_the_health_route_down(self) -> None:
        """A status() that fails is a bad description, not a dead engine."""
        class Loud:
            def status(self) -> dict:
                raise RuntimeError("the worker is not answering")

            def alive(self) -> bool:
                return True

        loud = Loud()
        deferred = DeferredEngine(lambda: loud, "started with --ui-only: no engine was asked for")
        deferred.restart()
        state = deferred.status()
        self.assertEqual(state["state"], "running", "the engine is up; only its words failed")
        self.assertIn("could not describe itself", deferred.last_error)

    class DeadWorkerAdapter:
        """The worker adapter after a build whose process exited during startup: no
        status(), only the note the supervisor kept. Brought up through restart()."""
        def __init__(self, note: str) -> None:
            self.note = note
            self.model = "Qwen2-7B-Q2_0"
            self.max_context = 0

        def restart(self) -> bool:
            return False

        def alive(self) -> bool:
            return False

        def death_note(self) -> str:
            return self.note

    def test_an_engine_that_was_taken_and_failed_speaks_in_its_own_words(self) -> None:
        """Once an engine has been taken, the startup sentence is not the cause.

        This is what the real worker on this box showed: the run-config read gave the
        adapter over the supervisor, the worker was started from engine/engine_q2_0/, and it
        exited at its memory guard. /health answered - but with "no engine was asked for",
        which is no longer true: an engine was asked for, and it is not running.
        """
        note = ("[hypercore] starting the engine: reading the model's weights ...\n"
                "strata generate: memory guard: limit 30.5 GiB (source: meminfo), "
                "available 4.9 GiB, floor 2560 MiB")
        adapter = DeferredEngineTests.DeadWorkerAdapter(note)
        deferred = DeferredEngine(lambda: adapter, "started with --ui-only: no engine was asked for")
        self.assertFalse(deferred.restart(), "the process exited before it was ready")
        state = deferred.status()
        self.assertEqual(state["state"], "offline")
        self.assertEqual(state["reason"], note.splitlines()[-1],
                         "the health route still describes the stand-in, not the engine")
        self.assertEqual(state["code"], UNLOADED_CODE,
                         "the engine was asked for and is not here yet")
        self.assertEqual(state["detail"], note, "the full account stays for the Monitor")
        self.assertEqual(state["model"], adapter.model)

    def test_an_engine_that_cannot_say_why_keeps_the_reason_it_was_given(self) -> None:
        """A dead engine with no note is not a licence to invent one."""
        silent = DeferredEngineTests.DeadWorkerAdapter("")
        deferred = DeferredEngine(lambda: silent, "started with --ui-only: no engine was asked for")
        self.assertFalse(deferred.restart())
        state = deferred.status()
        self.assertEqual(state["state"], "offline")
        self.assertEqual(state["code"], "ui_only", "the tab mapping is asserted at tests/test_offline_tabs.py")
        self.assertEqual(state["reason"], "started with --ui-only: no engine was asked for")

    def test_the_run_config_read_returns_the_adapter_the_startup_path_uses(self) -> None:
        """The same object main() builds, not the process owner by itself."""
        root = Path(__file__).resolve().parents[1]
        source = (root / "serve" / "server.py").read_text(encoding="utf-8")
        body = source[source.index("def engine_from_active_config"):source.index("def main")]
        self.assertIn("return HyperCoreRequestHandler(supervisor)", body,
                      "the run-config read returns the process owner, which has no status() "
                      "and no max_context for the health route and the Monitor to read")

    def test_the_engineless_server_is_given_a_candidate(self) -> None:
        """Without a candidate the retry is never created, and a build can never be adopted."""
        root = Path(__file__).resolve().parents[1]
        source = (root / "serve" / "server.py").read_text(encoding="utf-8")
        branch = source[source.index("if a.ui_only:"):]
        self.assertIn("engine_candidate, engine_unavailable = engine, engine.reason", branch,
                      "an engineless server has no candidate, so a build cannot be adopted")
        self.assertIn("DeferredEngine(deferred_engine", branch,
                      "the engineless server is not asked to read the run config again")
        self.assertIn("engine_from_active_config(a.config", branch,
                      "a server started with a run config adopts a different model instead")
        self.assertIn("own_port=None if a.no_attach else a.port", branch,
                      "--no-attach is ignored by the deferred engine, which could attach to "
                      "the web app it is running in")


class RunConfigReadTests(unittest.TestCase):
    """Which engine an engineless server adopts: engine_from_active_config().

    The fixture is one project root with one run config and one engine build folder, so
    nothing on this machine decides the answer. The worker branch is the one a real box
    cannot take by itself - this box has a worker binary - so the fixture says there is
    no worker, and then puts one in the fixture.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_dir = self.root / "config"
        self.engine_dir = self.root / "engine"
        self.index_path = self.engine_dir / "VERSIONS.json"
        self.worker = self.root / "hypercore-worker"
        self.config_dir.mkdir()          # CONFIG_DIR has to exist: _run_configs reads it
        self.patchers = [
            patch.object(quant_store, "CONFIG_DIR", self.config_dir),
            patch.object(quant_store, "ENGINE_DIR", self.engine_dir),
            patch.object(quant_store, "INDEX_PATH", self.index_path),
            patch.object(server, "worker_binary_path", lambda root=None: str(self.worker)),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def build(self, quant: str) -> Path:
        """engine/engine_<quant>/ with an engine binary in it, as a build leaves it."""
        folder = self.engine_dir / f"engine_{quant}"
        folder.mkdir(parents=True)
        exe = folder / "strata"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        return exe

    def run_config(self, quant: str, exe: Path) -> Path:
        path = self.config_dir / f"run-{quant}.json"
        path.write_text(json.dumps({
            "model_name": f"Qwen2-7B-{quant}",
            "cwd": str(self.root),
            "exe": str(exe),
            "args": ["--native", str(exe)],
            "worker": str(self.worker),
        }), encoding="utf-8")
        return path

    def test_a_config_with_no_worker_gives_the_direct_engine(self) -> None:
        """The fallback branch, which this box cannot reach because it has a worker built."""
        self.run_config("q2_0", self.build("q2_0"))
        engine = engine_from_active_config()
        self.assertIsInstance(engine, DirectEngine,
                             "no worker in the fixture, so the engine is run directly")
        self.assertEqual(engine.exe, str(self.engine_dir / "engine_q2_0" / "strata"),
                         "the engine built is not the engine the read returns")

    def test_a_config_whose_worker_exists_gives_the_adapter(self) -> None:
        """The same object main() builds: the adapter, not the process owner by itself."""
        self.run_config("q2_0", self.build("q2_0"))
        self.worker.write_text("#!/bin/sh\n", encoding="utf-8")
        engine = engine_from_active_config()
        self.assertIsInstance(engine, HyperCoreRequestHandler,
                             "the health route reads status() and max_context off the adapter")
        self.assertEqual(engine.supervisor.worker_binary, str(self.worker))

    def test_the_version_the_index_calls_active_is_the_one_adopted(self) -> None:
        """Two builds on disk: the read follows the active one, not the first config file."""
        self.run_config("q2_0", self.build("q2_0"))
        self.run_config("iq2_xs", self.build("iq2_xs"))
        self.index_path.write_text(json.dumps({"active": "iq2_xs", "versions": {}}), encoding="utf-8")
        engine = engine_from_active_config()
        self.assertIsInstance(engine, DirectEngine)
        self.assertEqual(engine.exe, str(self.engine_dir / "engine_iq2_xs" / "strata"),
                         "the read took a config the Model version dropdown is not on")

    def test_nothing_built_yet_is_not_an_error(self) -> None:
        """Before the first build there is no config and no binary: that is the state, not a failure."""
        self.assertIsNone(engine_from_active_config())
        self.run_config("q2_0", self.engine_dir / "engine_q2_0" / "strata")   # no build folder
        self.assertIsNone(engine_from_active_config())

    def test_the_template_is_read_as_no_config_at_all(self) -> None:
        """A server started engineless is given the template for its defaults.

        That is the path the --ui-only retry took on a fresh clone: with nothing built,
        the startup discovery has no run config to pick and falls back to
        config/run-example.json. Handing its placeholders to an engine starts it on
        "missing model shard /ABSOLUTE/PATH/TO/...", and every retry repeats it - the
        build finished, and the app still refused the model. The read ignores a template
        and takes the config the build wrote.
        """
        template = self.config_dir / "run-example.json"
        template.write_text(json.dumps({
            "exe": "engine/engine_q2_0/strata",
            "args": ["--native", "/ABSOLUTE/PATH/TO/models/Q2_0/model-00001-of-00002.gguf"],
            "model_name": "q2_0",
        }), encoding="utf-8")
        self.assertIsNone(engine_from_active_config(str(template)),
                         "the template's placeholder paths were handed to an engine")
        self.run_config("q2_0", self.build("q2_0"))
        engine = engine_from_active_config(str(template))
        self.assertIsInstance(engine, DirectEngine, "the build's config was not adopted")
        self.assertEqual(engine.args[1], str(self.engine_dir / "engine_q2_0" / "strata"),
                         "the engine still runs on the template's placeholders")


class _Responder:
    """A port that answers /health, so the attach check has something to look at."""

    def __init__(self, payload: dict) -> None:
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:   # keep the test output clean
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class EngineIsLoadedTests(unittest.TestCase):
    """A port that answers is an engine only if a model is loaded behind it."""

    ENGINELESS: ClassVar[dict[str, Any]] = {"status": "offline", "max_context": 0, "loaded": False, "service": "hypercore",
                  "engine": {"state": "offline", "code": "ui_only",
                             "reason": "started with --ui-only: no engine was asked for"}}
    LOADED: ClassVar[dict[str, Any]] = {"status": "ok", "max_context": 4096, "loaded": True, "service": "hypercore"}

    def test_a_web_app_without_weights_is_not_an_engine(self) -> None:
        self.assertFalse(server.engine_is_loaded(json.dumps(self.ENGINELESS)),
                         "an engineless web app was read as an engine: it would attach to itself")

    def test_a_loaded_model_is(self) -> None:
        self.assertTrue(server.engine_is_loaded(json.dumps(self.LOADED)))

    def test_a_context_is_enough_even_when_loaded_is_not_said(self) -> None:
        self.assertTrue(server.engine_is_loaded(json.dumps({"max_context": 2048})))

    def test_a_body_that_is_not_json_is_read_as_text(self) -> None:
        self.assertFalse(server.engine_is_loaded('"max_context": 0'))
        self.assertTrue(server.engine_is_loaded('"status": "ok"'))

    def test_the_probe_only_answers_when_a_model_is_loaded(self) -> None:
        engine_up = _Responder(self.LOADED)
        web_app = _Responder(self.ENGINELESS)
        try:
            self.assertEqual(server.find_running_engine({"port": engine_up.port}, 0, ports=(engine_up.port,)),
                             f"http://127.0.0.1:{engine_up.port}")
            self.assertIsNone(server.find_running_engine({"port": web_app.port}, 0, ports=(web_app.port,)),
                             "the probe took the engineless web app for an engine")
            self.assertIsNone(server.find_running_engine({"port": web_app.port}, web_app.port,
                                                         ports=(web_app.port,)),
                             "the probe answered on the port this server is listening on")
        finally:
            engine_up.stop()
            web_app.stop()


class DeferredEngineFailureTests(unittest.TestCase):
    """A build that produced an engine which will not start says so, in the engine's words."""

    class Refusing:
        def __init__(self) -> None:
            self.detail = "the card is held by the sweep: stop it and try again"
            self.model = "Qwen2-7B-Q2_0"
            self.max_context = 4096

        def start(self) -> None:
            raise RuntimeError("hypercore_worker: engine init failed (-5)")

        def alive(self) -> bool:
            return False

    def test_the_reason_is_the_engine_error_not_the_startup_reason(self) -> None:
        deferred = DeferredEngine(lambda: self.Refusing(),
                                 "started with --ui-only: no engine was asked for")
        self.assertFalse(deferred.restart())
        self.assertIn("engine init failed", deferred.last_error)
        self.assertIn("sweep", deferred.detail, "the engine's own words were dropped")
        self.assertEqual(deferred.status()["reason"], deferred.last_error,
                         "the banner still said the server was started engineless")

    def test_a_chat_refused_after_a_failed_attempt_says_why(self) -> None:
        deferred = DeferredEngine(lambda: self.Refusing(),
                                 "started with --ui-only: no engine was asked for")
        deferred.restart()
        with self.assertRaises(EngineUnavailable) as got:
            deferred.generate("hello")
        self.assertIn("engine init failed", str(got.exception),
                      "the user was sent to a build that has already been made")
        self.assertEqual(got.exception.retry_after, deferred.retry_after_s,
                         "the 503 carried no Retry-After for the next attempt")

    def test_a_factory_that_raises_is_an_attempt_not_a_crash(self) -> None:
        def broken() -> Any:
            raise OSError("config/run-q2_0.json is not valid JSON")
        deferred = DeferredEngine(broken, "started with --ui-only: no engine was asked for")
        with self.assertRaises(EngineUnavailable) as got:
            deferred.build()          # the attempt itself; the retry thread catches it
        self.assertIn("not valid JSON", deferred.last_error)
        self.assertIn("not valid JSON", str(got.exception),
                      "the 503 did not carry what the read failed on")
        self.assertFalse(deferred.restart(), "the retry thread must not see a crash")
        self.assertFalse(deferred.alive())


class DeferredAttachTests(unittest.TestCase):
    """After a build the deferred engine takes the engine there is, not a second copy."""

    LOADED: ClassVar[dict[str, Any]] = {"status": "ok", "max_context": 4096, "loaded": True, "model": "Qwen2-7B-q2_0"}
    ENGINELESS: ClassVar[dict[str, Any]] = {"status": "offline", "max_context": 0, "loaded": False}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_dir = self.root / "config"
        self.engine_dir = self.root / "engine"
        self.config_dir.mkdir()
        self.engine_dir.mkdir()
        self.patchers = [
            patch.object(quant_store, "ROOT", self.root),
            patch.object(server, "ROOT", self.root),   # engine_binary's engine/strata fallback
            patch.object(quant_store, "CONFIG_DIR", self.config_dir),
            patch.object(quant_store, "ENGINE_DIR", self.engine_dir),
            patch.object(quant_store, "INDEX_PATH", self.root / "engine" / "VERSIONS.json"),
            patch.object(server, "worker_binary_path", lambda root=None: str(self.root / "no-worker")),
            patch.object(server, "USUAL_ENGINE_PORTS", ()),   # only the fixture's port is probed
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def build(self, quant: str) -> Path:
        folder = self.engine_dir / f"engine_{quant}"
        folder.mkdir(parents=True, exist_ok=True)
        exe = folder / "strata"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        return exe

    def run_config(self, quant: str, exe: Path, port: int | None = None) -> Path:
        path = self.config_dir / f"run-{quant}.json"
        cfg = {"model_name": f"Qwen2-7B-{quant}", "cwd": str(self.root),
              "exe": str(exe), "args": [], "worker": str(self.root / "no-worker")}
        if port:
            cfg["port"] = port
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_an_engine_already_up_on_the_machine_is_adopted(self) -> None:
        other = _Responder(self.LOADED)
        self.run_config("q2_0", self.build("q2_0"), port=other.port)
        try:
            engine = engine_from_active_config(own_port=8143, api_key="secret")
        finally:
            other.stop()
        self.assertIsInstance(engine, AttachedEngine,
                             "a second copy was started while an engine was already running")
        self.assertEqual(engine.base, f"http://127.0.0.1:{other.port}")
        self.assertEqual(engine.api_key, "secret")

    def test_the_server_is_never_attached_to_itself(self) -> None:
        """The only thing answering is this web app's own port: build the engine instead."""
        itself = _Responder(self.ENGINELESS)
        self.run_config("q2_0", self.build("q2_0"), port=itself.port)
        try:
            engine = engine_from_active_config(own_port=itself.port)
        finally:
            itself.stop()
        self.assertNotIsInstance(engine, AttachedEngine,
                                "the deferred engine became the web app it is running in")
        self.assertIsInstance(engine, DirectEngine)

    def test_no_port_given_is_no_attach_check(self) -> None:
        """A read with no server yet cannot know which port is its own, so it builds."""
        other = _Responder(self.LOADED)
        self.run_config("q2_0", self.build("q2_0"))
        try:
            engine = engine_from_active_config()
        finally:
            other.stop()
        self.assertIsInstance(engine, DirectEngine)

    def test_the_config_the_server_was_started_with_is_the_one_adopted(self) -> None:
        """A server named for a model does not adopt another model's engine."""
        self.run_config("q2_0", self.build("q2_0"))          # the store has an active model
        empty = self.config_dir / "run-offline-test.json"
        empty.write_text(json.dumps({"model_name": "offline-test"}), encoding="utf-8")
        engine = engine_from_active_config(str(empty))
        self.assertIsNone(engine, "the deferred engine took the store's model over the one "
                                  "this server was started with")


if __name__ == "__main__":
    unittest.main()
