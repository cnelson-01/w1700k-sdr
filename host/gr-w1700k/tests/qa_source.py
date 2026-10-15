#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Real GNU Radio scheduler, native POSIX publisher and fake control process."""
import ctypes
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
import unittest

import numpy as np
import pmt
from gnuradio import blocks, gr, w1700k
from gnuradio.w1700k.control import Client

SLICE = 2048
EVENT, LOST, FLAT = 1 << 63, 1 << 62, 1 << 61
HERE = Path(__file__).resolve().parent


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for flowgraph")
        time.sleep(0.005)


class Ring:
    counter = 0

    def __init__(self):
        Ring.counter += 1
        self.name = f"/gr-w17-{os.getpid()}-{Ring.counter}"
        self.api = ctypes.CDLL(os.environ["W1700K_TEST_RING"], use_errno=True)
        self.api.fixture_create.argtypes = [ctypes.c_char_p, ctypes.c_uint64]
        self.api.fixture_create.restype = ctypes.c_void_p
        self.api.fixture_destroy.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint64]
        self.api.fixture_heartbeat.argtypes = [ctypes.c_void_p]
        self.api.fixture_write.argtypes = [ctypes.c_void_p] + [ctypes.c_uint64] * 3 + [ctypes.c_int16] * 2
        self.api.fixture_position.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64]
        self.ptr = self.api.fixture_create(self.name.encode(), 16)
        if not self.ptr:
            raise OSError(ctypes.get_errno(), "could not create QA ring: " + os.strerror(ctypes.get_errno()))
        self.bytes = self.get(40)
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.heartbeat)
        self.thread.start()

    def heartbeat(self):
        while not self.done.wait(0.05):
            self.api.fixture_heartbeat(self.ptr)

    def freeze(self):
        self.done.set()
        self.thread.join()

    def close(self):
        self.freeze()
        self.api.fixture_destroy(self.ptr, self.name.encode(), self.bytes)

    def get(self, offset):
        return ctypes.c_uint64.from_address(self.ptr + offset).value

    def set(self, offset, value):
        ctypes.c_uint64.from_address(self.ptr + offset).value = value

    def write(self, index, i=4096, q=-2048, kind=0, run=1):
        self.api.fixture_write(self.ptr, run, index, kind, i, q)

    def position(self, newest, run=1):
        self.api.fixture_position(self.ptr, run, newest)


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.ring = Ring()
        self.tb = None
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "requests.jsonl"

    def tearDown(self):
        if self.tb:
            self.tb.stop()
            self.tb.wait()
        self.ring.close()
        self.tmp.cleanup()

    def command(self, *args):
        return shlex.join([sys.executable, str(HERE / "fake_controller.py"), "--log", str(self.log), *args])

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def flow(self, src=None):
        self.src = src if src is not None else w1700k.stream_source(self.ring.name)
        started = threading.Event()
        if src is None:
            self.src._set_lifecycle(started.set, lambda: None)
        self.sink = blocks.vector_sink_c()
        self.tb = gr.top_block()
        self.tb.connect(self.src, self.sink)
        self.tb.start()
        if src is None:
            self.assertTrue(started.wait(5))
        return self.src

    def data(self, count):
        until(lambda: len(self.sink.data()) >= count * SLICE)
        return np.asarray(self.sink.data(), dtype=np.complex64)

    def tags(self, name):
        return [(t.offset, pmt.to_python(t.value)) for t in self.sink.tags() if pmt.symbol_to_string(t.key) == name]

    def test_samples_events_loss_and_flatten_tags(self):
        self.flow()
        self.src.set_metadata(40e6, 5250e6)
        self.ring.write(0, -8192, 8191)
        self.ring.write(1, kind=EVENT)
        self.ring.write(2, kind=LOST)
        self.ring.write(3, 32767, -32768, kind=FLAT)
        self.ring.position(4)
        data = self.data(3)
        np.testing.assert_array_equal(data[:SLICE], complex(-1, 8191/8192))
        np.testing.assert_array_equal(data[SLICE:2*SLICE], 0)
        np.testing.assert_array_equal(data[2*SLICE:], complex(32767/8192, -4))
        self.assertEqual(self.tags("w1700k_gap"), [(SLICE, dict(reason="transport", slice=2, samples=SLICE))])
        self.assertEqual(self.tags("w1700k_flattened"), [(0, False), (2*SLICE, True)])
        self.assertEqual(self.tags("w1700k_run"), [(0, dict(run=1, first_slice=0))])
        self.assertEqual(self.tags("rx_rate"), [(0, 40e6)])
        self.assertEqual(self.tags("rx_freq"), [(0, 5250e6)])
        self.assertEqual(self.src.stats(), dict(samples=3*SLICE, events=1, lost_slices=1,
                                               overrun_completions=0, runs=1))

    def test_attach_live_and_follow_new_run(self):
        self.ring.write(0)
        self.ring.position(1)
        self.flow()
        self.ring.write(1, 1024, -1024)
        self.ring.position(2)
        self.data(1)
        self.ring.write(0, run=2, kind=EVENT)
        self.ring.write(1, 2048, -4096, run=2)
        self.ring.position(2, run=2)
        data = self.data(2)
        np.testing.assert_array_equal(data[:SLICE], complex(0.125, -0.125))
        np.testing.assert_array_equal(data[SLICE:], complex(0.25, -0.5))
        self.assertEqual(self.tags("w1700k_run"), [(0, dict(run=1, first_slice=1)),
                                                 (SLICE, dict(run=2, first_slice=1))])

    def test_overrun_skips_unknown_time_and_marks_next_sample(self):
        self.flow()
        self.ring.position(32)
        until(lambda: self.src.stats()["overrun_completions"] == 32)
        self.ring.write(32)
        self.ring.position(33)
        self.data(1)
        self.assertEqual(self.tags("w1700k_gap"), [(0, dict(reason="overrun", completions=32))])

    def test_tag_reuse_is_not_delivered_as_samples(self):
        self.flow()
        self.ring.write(16)  # slot 0 already overwritten, even though frontier lags
        self.ring.position(1)
        until(lambda: self.src.stats()["overrun_completions"] == 1)
        self.ring.write(1, -512, 512)
        self.ring.position(2)
        np.testing.assert_array_equal(self.data(1), complex(-0.0625, 0.0625))
        self.assertEqual(self.tags("w1700k_gap"), [(0, dict(reason="overrun", completions=1))])

    def test_idle_stop_is_bounded_and_graph_can_restart(self):
        self.flow()
        start = time.monotonic()
        self.tb.stop()
        self.tb.wait()
        self.assertLess(time.monotonic() - start, 1)
        self.tb.start()
        time.sleep(0.1)
        self.ring.write(0)
        self.ring.position(1)
        self.data(1)

    def test_dead_service_with_odd_seqlock_ends_without_hanging(self):
        self.flow()
        self.ring.freeze()
        self.ring.set(128, 1)
        until(lambda: bool(self.src.last_error()), timeout=3)
        self.tb.wait()
        self.assertIn("heartbeat expired", self.src.last_error())

    def test_missing_service_and_malformed_layout(self):
        src = w1700k.stream_source(self.ring.name + "x")
        self.assertFalse(src.start())
        self.assertIn("start w1700k-stream first", src.last_error())
        for offset, value in ((16, 0), (16, 3), (24, 1), (32, self.ring.bytes + 8),
                              (16, 1 << 63), (32, 4096)):
            old = self.ring.get(offset)
            self.ring.set(offset, value)
            try:
                src = w1700k.stream_source(self.ring.name)
                self.assertFalse(src.start())
                self.assertIn("invalid w1700k-stream ring layout", src.last_error())
            finally:
                self.ring.set(offset, old)

    def test_managed_lifecycle_and_live_controls(self):
        src = w1700k.source(self.ring.name, self.command(), flatten=True)
        self.flow(src)
        until(lambda: any(r["op"] == "start" for r in self.requests()))
        src.set_center_freq(5300e6)  # waits for start's lock before sending
        src.set_gain(False, 1, 7)
        src.set_filter(80)
        src.set_flatten(False)
        self.ring.write(0)
        self.ring.position(1)
        self.data(1)
        self.assertEqual(self.tags("rx_freq"), [(0, 5300e6 - 1)])
        self.assertEqual((self.ring.get(504), self.ring.get(512), self.ring.get(520), self.ring.get(528)),
                         (0, 40_000_000, 40_000_000, 80_000_000))
        self.tb.stop()
        self.tb.wait()
        reqs = self.requests()
        self.assertEqual([r["op"] for r in reqs],
                         ["load", "configure", "filter", "gain", "start", "tune", "gain", "filter", "stop"])
        self.assertEqual({k: v for k, v in reqs[6].items() if k != "id"}, dict(op="gain", lna=1, lpf=7))
        self.assertFalse(src.last_error())
        self.assertEqual(self.ring.get(496), 2)

    def test_passive_wrapper_does_not_change_shared_controls(self):
        self.ring.set(504, 1)
        src = w1700k.source(self.ring.name)
        src.set_flatten(False)
        src.set_filter(320)
        src.set_gain(False, 4, 9)
        self.flow(src)
        time.sleep(0.1)
        self.ring.write(0)
        self.ring.position(1)
        self.data(1)
        self.assertEqual(self.ring.get(504), 1)
        self.assertEqual(self.ring.get(512), 0)

    def test_managed_start_waits_for_new_run_not_the_previous_tail(self):
        self.ring.position(1)
        src = w1700k.source(self.ring.name, self.command())
        self.flow(src)
        until(lambda: self.ring.get(496) == 1)
        self.ring.write(1, 7000, 7000)
        self.ring.position(2)
        time.sleep(0.1)
        self.assertEqual(len(self.sink.data()), 0)
        self.ring.write(0, 1024, -1024, run=2)
        self.ring.position(1, run=2)
        np.testing.assert_array_equal(self.data(1), complex(0.125, -0.125))

    def test_managed_start_failure_cleans_up_and_existing_stream_is_not_stopped(self):
        for args, error in ((("--fail", "configure"), "configure failure"),
                            (("--running",), "already streaming"),
                            (("--attach-fail",), "cannot reach")):
            with self.subTest(args=args):
                self.log.unlink(missing_ok=True)
                src = w1700k.source(self.ring.name, self.command(*args))
                self.assertFalse(src._stream.start())
                self.assertIn(error, src.last_error())
                self.assertIsNone(src._radio.client)
                self.assertNotIn("stop", [r["op"] for r in self.requests()])

    def test_failed_start_attempts_stop(self):
        src = w1700k.source(self.ring.name, self.command("--fail", "start"))
        self.assertFalse(src._stream.start())
        self.assertIn("start failure", src.last_error())
        self.assertEqual(self.requests()[-1]["op"], "stop")
        self.assertIsNone(src._radio.client)

    def test_failed_start_ends_actual_flowgraph_without_stranding_scheduler(self):
        src = w1700k.source(self.ring.name, self.command("--fail", "configure"))
        self.flow(src)
        until(lambda: bool(src.last_error()))
        self.tb.wait()
        self.assertIn("configure failure", src.last_error())

    def test_missing_service_ends_actual_flowgraph(self):
        src = w1700k.source(self.ring.name + "x")
        self.flow(src)
        until(lambda: bool(src.last_error()))
        self.tb.wait()
        self.assertIn("start w1700k-stream first", src.last_error())

    def test_client_failure_timeout_and_reaping(self):
        client = Client(self.command("--fail", "tune"), timeout=0.5)
        try:
            with self.assertRaisesRegex(RuntimeError, "tune failure"):
                client.request("tune", hz=5300e6)
            client.request("stop")  # explicit errors preserve request/reply alignment
        finally:
            client.close()
        self.assertIsNotNone(client.process.returncode)
        client = Client(self.command("--stall", "tune"), timeout=0.1)
        try:
            with self.assertRaises(TimeoutError):
                client.request("tune", hz=5300e6)
            with self.assertRaisesRegex(RuntimeError, "connection failed"):
                client.request("stop")
        finally:
            client.close()
        self.assertIsNotNone(client.process.returncode)

    def test_invalid_parameters_and_640_mode(self):
        for params in (dict(sample_rate=1e6), dict(band=3), dict(center_freq=float("nan")),
                       dict(capture_rate=640e6), dict(sample_rate=320e6, capture_rate=640e6)):
            with self.subTest(params=params), self.assertRaises(ValueError):
                w1700k.source(**params)
        src = w1700k.source(self.ring.name, self.command(), band=2, sample_rate=320e6,
                             capture_rate=640e6, center_freq=6105e6)
        self.assertTrue(src._stream.start())
        self.assertEqual(self.ring.get(520), 640_000_000)
        self.assertTrue(src._stream.stop())


class CompanionTest(unittest.TestCase):
    def test_schema_flowgraph_and_generated_python(self):
        from gnuradio.grc.core.platform import Platform
        from gnuradio.grc.core import schema_checker
        from gnuradio.grc.core.io import yaml
        platform = Platform(version=gr.version())
        base = Path(gr.prefix()) / "share/gnuradio/grc/blocks"
        block_path = HERE.parent / "grc/w1700k_source.block.yml"
        data = yaml.safe_load(block_path.read_text())
        checker = schema_checker.Validator(schema_checker.BLOCK_SCHEME)
        self.assertTrue(checker.run(data), checker.messages)
        for path in (base / "options.block.yml", base / "blocks_null_sink.block.yml", block_path):
            platform.load_block_description(yaml.safe_load(path.read_text()), str(path))
        path = base / "stream.domain.yml"
        platform.load_domain_description(yaml.safe_load(path.read_text()), str(path))
        fg = platform.make_flow_graph()
        fg.options_block.params["id"].set_value("gr_w1700k_smoke")
        fg.options_block.params["generate_options"].set_value("no_gui")
        src = fg.new_block("w1700k_source")
        self.assertTrue(src.flags.throttle, "hardware-paced source must not suggest a Throttle")
        src.params["id"].set_value("source_0")
        sink = fg.new_block("blocks_null_sink")
        sink.params["id"].set_value("sink_0")
        sink.params["type"].set_value("complex")
        fg.connect(src.sources[0], sink.sinks[0])
        for band, rate, capture in ((1, 40e6, 0), (2, 320e6, 640e6)):
            src.params["band"].set_value(str(band))
            src.params["sample_rate"].set_value(str(rate))
            src.params["capture_rate"].set_value(str(capture))
            fg.rewrite()
            fg.validate()
            self.assertTrue(fg.is_valid(), fg.get_error_messages())
            with tempfile.TemporaryDirectory() as tmp:
                generator = platform.Generator(fg, tmp)
                generator.write()
                namespace = {"__name__": "qa_generated"}
                exec(compile(Path(generator.file_path).read_text(), generator.file_path, "exec"), namespace)
                graph = namespace["gr_w1700k_smoke"]()
                self.assertEqual(graph.source_0.stats()["samples"], 0)
        src.params["band"].set_value("1")
        fg.rewrite()
        fg.validate()
        self.assertFalse(fg.is_valid(), "GRC must reject 640/2 on band 1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
