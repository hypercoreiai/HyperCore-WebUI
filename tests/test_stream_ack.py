#!/usr/bin/env python3
"""The ack frame and the reply that arrives before it.

serve/ipc_parser.py documents the wire as: SUBMIT is answered with RESP_STATUS, then
the stream frames. The engine bridge (hypercore-interface/interface/strata_bridge.cpp)
runs a request inside hypercore_engine_submit(), so in that build every token frame and
the terminal frame are written before the ack - a request that ran perfectly is then
reported as "expected an ack frame, got 4" and the reply is thrown away.

Two things follow, and both are tested here:

  * the worker sends the ack before the run (engine-src/interface/worker_main.cpp);
  * the supervisor reads a reply whose frames precede the ack instead of failing on it,
    so a worker built before the fix still answers.

No GPU, no subprocess: tests/frame_fixture.py is a socketpair speaking the real framing.
"""
from __future__ import annotations

import unittest

from serve.engine_supervisor import HyperCoreProcessSupervisor, WorkerRejected
from tests.frame_fixture import ScriptedWorker
from serve.ipc_parser import CMD_CANCEL, CMD_SUBMIT, STREAM_TERMINAL, STREAM_TOKEN


def wired(**kwargs) -> tuple[HyperCoreProcessSupervisor, ScriptedWorker]:
    """A supervisor whose "worker" is the socketpair mock."""
    worker = ScriptedWorker(**kwargs)
    sup = HyperCoreProcessSupervisor(transport=worker.connect, max_restarts=0)
    sup.start()
    return sup, worker


def kinds(sup: HyperCoreProcessSupervisor, ids: list[int], max_new: int) -> list[int]:
    return [kind for kind, _, _ in sup.stream(ids, max_new_tokens=max_new)]


class AckBeforeStreamTests(unittest.TestCase):
    """The contract: ack first, then the stream."""

    def test_a_normal_reply_is_ack_then_tokens_then_terminal(self) -> None:
        sup, worker = wired(tokens=3, max_slots=2)
        self.assertEqual(kinds(sup, [1, 2, 3], 8),
                         [STREAM_TOKEN, STREAM_TOKEN, STREAM_TOKEN, STREAM_TERMINAL])
        self.assertEqual([m for m, _, _ in worker.received if m in (CMD_SUBMIT, CMD_CANCEL)],
                         [CMD_SUBMIT])

    def test_a_rejected_config_is_refused_by_the_ack(self) -> None:
        sup, _ = wired(tokens=1, max_slots=2)
        with self.assertRaises(WorkerRejected):
            list(sup.stream([1, 2, 3], max_new_tokens=0))

    def test_a_late_ack_does_not_make_a_second_request_busy(self) -> None:
        # The slot is released by the run, not by the ack, so a synchronous worker that
        # answers late still leaves the link free for the next request.
        sup, _ = wired(tokens=2, max_slots=1, sync_submit=True)
        self.assertEqual(kinds(sup, [1, 2, 3], 8), [STREAM_TOKEN, STREAM_TOKEN, STREAM_TERMINAL])
        self.assertEqual(kinds(sup, [4, 5, 6], 8), [STREAM_TOKEN, STREAM_TOKEN, STREAM_TERMINAL])
        self.assertEqual(sup.in_flight(), 0)


class AckAfterStreamTests(unittest.TestCase):
    """The build that runs a request inside submit(): the frames come first."""

    def test_the_reply_is_read_though_the_ack_arrives_last(self) -> None:
        sup, worker = wired(tokens=4, max_slots=2, sync_submit=True)
        self.assertEqual(kinds(sup, [1, 2, 3], 8), [STREAM_TOKEN] * 4 + [STREAM_TERMINAL])
        # one submit, no cancel: the stream was read to its terminal frame
        self.assertEqual([m for m, _, _ in worker.received if m in (CMD_SUBMIT, CMD_CANCEL)],
                         [CMD_SUBMIT])

    def test_the_summary_of_a_late_ack_reply_is_still_recorded(self) -> None:
        sup, _ = wired(tokens=2, max_slots=2, sync_submit=True, cache_hits=7, cache_lookups=10)
        list(sup.stream([1, 2, 3], max_new_tokens=8))
        self.assertEqual(sup.last_summary["cache_hits"], 7)

    def test_generate_returns_the_tokens_of_a_late_ack_reply(self) -> None:
        import threading
        sup, _ = wired(tokens=3, max_slots=2, sync_submit=True, token_start=11)
        out = list(sup.generate([1, 2, 3], 8, {}, threading.Event()))
        self.assertEqual(out, [11, 12, 13])


if __name__ == "__main__":
    unittest.main()
