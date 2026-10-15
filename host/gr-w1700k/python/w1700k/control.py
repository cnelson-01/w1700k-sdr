# SPDX-License-Identifier: MIT
"""Bounded, serialized client for the repository's newline-delimited serve protocol."""
import json
import math
import os
import select
import shlex
import signal
import subprocess
import threading
import time
import weakref


class Client:
    def __init__(self, command, timeout=60.0):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("controller timeout must be positive and finite")
        self.timeout = timeout
        self.state = None
        self._buffer = bytearray()
        self._id = 0
        self._usable = True
        self.process = subprocess.Popen(shlex.split(command), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, start_new_session=True)
        try:
            # status can return a default snapshot before Controller.attach has
            # finished. Wait for its initial state event instead.
            deadline = time.monotonic() + timeout
            while self.state is None:
                message = self._read(deadline)
                self._event(message)
                if message.get("event") == "log" and message.get("level") == "error":
                    raise RuntimeError(message.get("text", "controller attach failed"))
        except BaseException:
            self.close()
            raise

    def _read(self, deadline):
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError("w1700k controller timed out")
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data:
                raise RuntimeError("w1700k controller exited before replying")
            self._buffer.extend(data)
            if len(self._buffer) > 1024 * 1024:
                raise RuntimeError("w1700k controller response exceeds 1 MiB")
        line, _, self._buffer = self._buffer.partition(b"\n")
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("w1700k controller returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("w1700k controller response must be an object")
        return value

    def _event(self, message):
        if isinstance(message.get("state"), dict):
            self.state = message["state"]

    def request(self, op, **fields):
        if not self._usable:
            raise RuntimeError("w1700k controller connection failed; restart the flowgraph")
        self._id += 1
        try:
            data = json.dumps(dict(id=self._id, op=op, **fields)).encode() + b"\n"
            self.process.stdin.write(data)
            self.process.stdin.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                message = self._read(deadline)
                self._event(message)
                if "id" not in message:
                    continue
                if message["id"] != self._id:
                    raise RuntimeError("w1700k controller returned an unexpected request id")
                break
        except (OSError, RuntimeError, TimeoutError):
            self._usable = False
            raise
        if message.get("ok") is not True:
            raise RuntimeError(message.get("error", "w1700k controller request failed"))
        return self.state

    def close(self):
        self._usable = False
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.process.pid, sig)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    continue
        self.process.stdout.close()


def rate_pair(band, rate, capture):
    if band not in (0, 1, 2) or isinstance(band, bool):
        raise ValueError("band must be 0, 1 or 2")
    if rate not in (40e6, 80e6, 160e6, 320e6):
        raise ValueError("sample rate must be 40, 80, 160 or 320 Msps")
    capture = capture or rate
    if capture != rate and (band, rate, capture) != (2, 320e6, 640e6):
        raise ValueError("640/2 requires band 2, sample rate 320e6 and capture rate 640e6")
    return int(rate), int(capture)


class RadioSession:
    def __init__(self, block, command, band, rate, capture, freq, agc, lna, lpf, filter_mhz,
                 flatten, timeout):
        self.block = weakref.ref(block)  # the native block holds our lifecycle callbacks
        self.command, self.band, self.timeout = command, band, timeout
        self.rate, self.capture = rate_pair(band, rate, capture)
        self.lock = threading.RLock()
        self.client = None
        self.owns_stream = False
        self.active = False
        self.set_center_freq(freq)
        self.set_gain(agc, lna, lpf)
        self.set_filter(filter_mhz)
        self.flatten = bool(flatten)

    def _update(self):
        state = self.client.state
        radio, rf = state["radio"], state["rf"]
        self.block().set_metadata(radio["sample_rate"], rf["lo_hz"])
        self.block().set_radio_state(radio["sample_rate"], radio["capture_rate"],
                                     rf["filter_mhz"] * 1e6, self.flatten)

    def start(self):
        with self.lock:
            self.client = Client(self.command, self.timeout)
            if self.client.state["stream"]["running"]:
                raise RuntimeError("radio is already streaming; use a passive source or stop it first")
            if not self.client.state["radio"]["loaded"]:
                self.client.request("load", band=self.band)
            self.client.request("configure", band=self.band, sample_rate=self.rate,
                                capture_rate=self.capture, lo_hz=self.freq)
            self.client.request("filter", mhz=self.filter_mhz)
            self._gain_request(self.agc, self.lna, self.lpf)
            self._update()
            self.block().restart_run()
            self.owns_stream = True  # a timed-out start may have reached the hardware
            self.client.request("start")
            self._update()
            self.active = True

    def stop(self):
        with self.lock:
            self.active = False
            if self.client is None:
                return
            try:
                if self.owns_stream:
                    self.client.request("stop")
                    self.block().restart_run()
            finally:
                self.owns_stream = False
                self.client.close()
                self.client = None

    def set_center_freq(self, freq):
        if not math.isfinite(freq) or freq <= 0:
            raise ValueError("center frequency must be positive and finite")
        with self.lock:
            if self.active:
                self.client.request("tune", hz=int(freq))
                self._update()
            self.freq = int(freq)

    def _gain_request(self, agc, lna, lpf):
        if agc:
            self.client.request("gain", agc=True)
        else:
            self.client.request("gain", lna=lna, lpf=lpf)

    def set_gain(self, agc, lna, lpf):
        if lna not in range(5) or lpf not in range(10) or int(lna) != lna or int(lpf) != lpf:
            raise ValueError("LNA is an index 0..4; LPF gain is an index 0..9")
        with self.lock:
            if self.active:
                self._gain_request(bool(agc), int(lna), int(lpf))
            self.agc, self.lna, self.lpf = bool(agc), int(lna), int(lpf)

    def set_filter(self, mhz):
        if mhz not in (20, 40, 80, 160, 320):
            raise ValueError("analog filter width must be 20, 40, 80, 160 or 320 MHz")
        with self.lock:
            if self.active:
                self.client.request("filter", mhz=int(mhz))
                self._update()
            self.filter_mhz = int(mhz)

    def set_flatten(self, enabled):
        with self.lock:
            self.flatten = bool(enabled)
            if self.active:
                self._update()
