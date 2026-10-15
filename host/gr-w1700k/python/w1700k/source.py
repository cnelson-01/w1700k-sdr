# SPDX-License-Identifier: MIT
from gnuradio import gr
from .w1700k_python import stream_source
from .control import RadioSession, rate_pair


class source(gr.hier_block2):
    """Complex float W1700K source, optionally owning the receiver's control.

    An empty controller attaches passively. A command such as
    '/path/to/host/bin/w1700k serve' configures and starts a stopped receiver on
    flowgraph start, then stops that receiver on flowgraph shutdown. The local
    w1700k-stream service must already be running in either mode.
    """
    def __init__(self, stream_name="/w1700k-stream", controller="", band=1,
                 sample_rate=40e6, center_freq=5250e6, capture_rate=0,
                 agc=True, lna=2, lpf=9, filter_mhz=40, flatten=False,
                 command_timeout=60.0):
        gr.hier_block2.__init__(self, "W1700K Source", gr.io_signature(0, 0, 0),
                               gr.io_signature(1, 1, gr.sizeof_gr_complex))
        self._rate, self._capture = rate_pair(band, sample_rate, capture_rate)
        self._freq = center_freq
        self._stream = stream_source(stream_name, bool(controller))
        self._stream.set_metadata(self._rate, self._freq)
        self._radio = None
        if controller:
            self._radio = RadioSession(self._stream, controller, band, self._rate, self._capture,
                                       center_freq, agc, lna, lpf, filter_mhz, flatten, command_timeout)
            self._stream._set_lifecycle(self._radio.start, self._radio.stop)
        self.connect(self._stream, self)

    def set_center_freq(self, hz):
        if self._radio:
            self._radio.set_center_freq(hz)
        else:
            self._stream.set_metadata(self._rate, hz)
        self._freq = hz

    def set_gain(self, agc=True, lna=2, lpf=9):
        if self._radio:
            self._radio.set_gain(agc, lna, lpf)

    def set_filter(self, mhz):
        if self._radio:
            self._radio.set_filter(mhz)

    def set_flatten(self, enabled):
        if self._radio:
            self._radio.set_flatten(enabled)

    def stats(self):
        return self._stream.stats()

    def last_error(self):
        return self._stream.last_error()
