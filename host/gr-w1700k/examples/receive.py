#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Receive for a bounded wall-clock interval; optionally save complex float IQ."""
import argparse
import json
import time
from gnuradio import blocks, gr, w1700k


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller", default="", help="e.g. '/path/host/bin/w1700k serve'")
    parser.add_argument("--stream", default="/w1700k-stream")
    parser.add_argument("--band", type=int, default=1)
    parser.add_argument("--rate", type=float, default=40e6)
    parser.add_argument("--capture-rate", type=float, default=0)
    parser.add_argument("--freq", type=float, default=5250e6)
    parser.add_argument("--filter-mhz", type=int, default=40)
    parser.add_argument("--flatten", action="store_true")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--out", help="raw native-endian complex float32 file (8 bytes/sample)")
    args = parser.parse_args()
    if not 0 < args.seconds < float("inf"):
        parser.error("--seconds must be positive and finite")
    tb = gr.top_block()
    src = w1700k.source(stream_name=args.stream, controller=args.controller, band=args.band,
                         sample_rate=args.rate, capture_rate=args.capture_rate, center_freq=args.freq,
                         filter_mhz=args.filter_mhz, flatten=args.flatten)
    sink = blocks.file_sink(gr.sizeof_gr_complex, args.out, False) if args.out else blocks.null_sink(gr.sizeof_gr_complex)
    tb.connect(src, sink)
    try:
        tb.start()
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline and not src.last_error():
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        tb.stop()
        tb.wait()
    print(json.dumps(src.stats(), sort_keys=True))
    if src.last_error():
        raise SystemExit(src.last_error())


if __name__ == "__main__":
    main()
