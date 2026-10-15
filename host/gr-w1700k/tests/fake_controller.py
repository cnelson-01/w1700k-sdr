#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Protocol peer only. No network or hardware access."""
import argparse
import json
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--log", required=True)
parser.add_argument("--fail", default="")
parser.add_argument("--stall", default="")
parser.add_argument("--running", action="store_true")
parser.add_argument("--attach-fail", action="store_true")
args = parser.parse_args()
state = dict(radio=dict(loaded=False, band=1, sample_rate=40_000_000, capture_rate=40_000_000),
             rf=dict(lo_hz=5_250_000_000, filter_mhz=40), stream=dict(running=args.running))


def send(value):
    print(json.dumps(value), flush=True)


if args.attach_fail:
    send(dict(event="log", level="error", text="cannot reach simulated unit"))
else:
    send(dict(event="state", state=state))
for line in sys.stdin:
    req = json.loads(line)
    op = req["op"]
    with open(args.log, "a") as log:
        log.write(json.dumps(req) + "\n")
    if op == args.stall:
        time.sleep(30)
    if op == args.fail:
        send(dict(id=req["id"], ok=False, error="simulated " + op + " failure"))
        continue
    if op == "load":
        state["radio"]["loaded"] = True
    elif op == "configure":
        for key in ("band", "sample_rate", "capture_rate"):
            state["radio"][key] = req[key]
        state["rf"]["lo_hz"] = req["lo_hz"] - 1  # realised synthesizer grid
    elif op == "tune":
        state["rf"]["lo_hz"] = req["hz"] - 1
    elif op == "filter":
        state["rf"]["filter_mhz"] = req["mhz"]
    elif op in ("start", "stop"):
        state["stream"]["running"] = op == "start"
    send(dict(event="progress", text=op, fraction=1.0))
    send(dict(event="state", state=state))
    send(dict(id=req["id"], ok=True, state=state))
