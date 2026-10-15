# gr-w1700k

GNU Radio 3.10 receive source for the **W1700K SDR**, on Linux and macOS. The
sample path is C++ with VOLK conversion; Python handles radio control. Includes
Python bindings, a GNU Radio Companion block, a receive example, and hardware-free
tests that run the actual GNU Radio scheduler.

The source consumes the local [`w1700k-stream`](../stream/README.md) shared-memory
ring. Keep one stream service per UDP endpoint; any number of GNU Radio sources,
spectrum viewers and recorders can share its output. The plugin does not receive
UDP itself or launch the stream service.

## Build

Requirements: CMake 3.16+, a C++17 compiler, GNU Radio **3.10**, its development
headers and Python environment, and pybind11. VOLK comes with GNU Radio. The
plugin is optional and does not add GNU Radio to the regular host build.

```sh
# From the repository root; use GNU Radio's Python environment.
cmake -S host/gr-w1700k -B host/gr-w1700k/build -DCMAKE_BUILD_TYPE=Release
cmake --build host/gr-w1700k/build -j
ctest --test-dir host/gr-w1700k/build --output-on-failure
cmake --install host/gr-w1700k/build
```

Use `-DCMAKE_INSTALL_PREFIX=...` at configure time for a custom install. The
Python package installs in `${GR_PYTHON_DIR}/gnuradio/w1700k`, the library under
`lib` (or the platform's `lib64`), and the block under
`share/gnuradio/grc/blocks`. CMake prints the selected Python executable; it must
be the interpreter used by GNU Radio. `-DPYTHON_EXECUTABLE=...` can select it on
installations that do not already specify one. With Homebrew GNU Radio this is
normally `$(brew --prefix gnuradio)/libexec/venv/bin/python`.

For a build-tree trial without installation, add the absolute paths to
`host/gr-w1700k/build/python` to `PYTHONPATH` and `host/gr-w1700k/grc` to
`GRC_BLOCKS_PATH`, then launch Companion using its usual GNU Radio environment.
Search for **W1700K Source**. The build-tree extension already locates its C++
library. Custom installs likewise need their Python and GRC paths discoverable.
The installed extension has a relative library search path.

C++ applications can use `find_package(gnuradio-w1700k CONFIG REQUIRED)` and
link `gnuradio::gnuradio-w1700k`; its public header is
`gnuradio/w1700k/stream_source.h`. Python-only radio control is separate from
this native, passive source API.

## First flowgraph

Provision the stream link and configure the receiver as described in the
[host-control README](../w1700k/README.md). Start the host stream service:

```sh
host/stream/build/w1700k-stream
```

In Companion, connect **W1700K Source** to a QT GUI Frequency Sink, a File Sink
or another consumer of complex samples. Do **not** add a Throttle. Set:

- Controller command: `/absolute/path/to/host/bin/w1700k serve`.
- Band: `1`; sample rate: `40e6`; center frequency: `5250e6`.
- Analog filter: `40` MHz; AGC: enabled.

Use the same sample-rate variable for downstream frequency axes and filters.
Starting the flowgraph attaches the local ring, loads firmware if needed,
configures the radio, and starts streaming. Stopping it stops the radio. The
controller uses its usual configuration file; another file can be supplied as
`/path/to/host/bin/w1700k --config /path/to/config.json serve`.

Only one controller should operate a unit. Managed mode refuses a radio that
is already streaming; it does not take ownership of somebody else's stream.
To listen alongside the web spectrum viewer or a CLI-started receiver, leave
the controller command empty (**passive mode**). Set rate and frequency to the
actual receiver settings: in passive mode they only label the samples, and
gain/filter/flatten controls have no effect. The native `stream_source` omits
rate and frequency tags entirely until `set_metadata()` is called.

For a bounded trial without a GUI (add build-tree `PYTHONPATH` if not installed):

```sh
python3 host/gr-w1700k/examples/receive.py \
  --controller '/absolute/path/to/host/bin/w1700k serve' --seconds 10
```

Add `--out capture.cf32` to save native-endian complex float32, I then Q,
8 bytes per complex sample. At 40 Msps this is 320 MB/s; at 320 Msps it is
2.56 GB/s. A file sink saves samples but not stream tags. For captures with loss
metadata, use an appropriate tagged recorder or the existing `sdr-recv` utility.

## Controls and samples

`from gnuradio import w1700k; src = w1700k.source(...)` has the same parameters
as the Companion block. `src.set_center_freq(hz)`,
`src.set_gain(agc=True, lna=2, lpf=9)`, `src.set_filter(mhz)` and
`src.set_flatten(enabled)` work while running in managed mode. Control calls
are serialized, return after acknowledgment, and raise on controller failure.
Gain values are hardware **indices**, not dB: LNA 0–4, baseband/LPF 0–9.

Band, sample rate and capture rate are construction-time settings: rebuild the
flowgraph to change them. Rates are 40, 80, 160 and 320 Msps. Band 2 also supports
`sample_rate=320e6, capture_rate=640e6` (640/2); the default capture rate of zero
means the sample rate. The controller validates the selected unit's LO range.

Output is `complex64`: `(I + j*Q) / 8192`. This preserves the radio's signed
14-bit scale and the stream service's correction headroom: corrected samples
can exceed ±1. There is no additional clipping, IQ swap, DC removal, resampling
or decimation. Flatten is applied by the shared service and affects **all** its
consumers. In managed mode this source supplies the service's rate/filter state.

Attaching to a current run starts at its published frontier. A new run starts
from slice zero if still available. Firmware event completions are skipped
without consuming sample time. Known transport losses become 2048 zeros each.
If the reader falls behind or observes a reused slot, it resumes at the live
frontier: overwritten completions could include events, so their exact missing
sample count is unknowable. Data overwritten during conversion becomes a
zero-filled slice. Tags describe these boundaries:

| Tag | Value and location |
| --- | --- |
| `w1700k_run` | Dictionary `{run, first_slice}` on the first delivered sample of each run/attachment |
| `w1700k_gap` | Known loss: `{reason: 'transport' or 'overwrite', slice, samples: 2048}`, at the first zero |
| `w1700k_gap` | Skipped backlog: `{reason: 'overrun', completions: N}`, on the next delivered sample; deliberately no sample count |
| `w1700k_flattened` | Boolean at the first valid slice and whenever that slice's actual correction state changes |
| `rx_rate`, `rx_freq` | Double, at the first delivered slice and after metadata changes; managed mode uses acknowledged values |

Rate/frequency tags are **informational**, not sample-exact retune boundaries.
Buffered samples may predate the acknowledged setting, and samples captured
during tuning may be invalid. The ring exposes neither hardware timestamps nor
sample-exact RF command boundaries, so the source emits no `rx_time` tag.

`src.stats()` returns samples (including zero fill), events, lost slices,
overrun completions and observed runs since the last flowgraph start.
`src.last_error()` reports startup failure, service death or a failed radio stop.
A startup failure logs the error and ends the source cleanly (GNU Radio invokes
block startup in its scheduler threads, so it cannot raise into `tb.start()`). A healthy idle
service waits without producing zeros. A heartbeat older than one second ends
the source; restart the service and flowgraph to reattach. A timed-out controller
connection is not reused, because a late reply could be mistaken for a later
command. If a start/stop times out, inspect the radio with the CLI before retrying.

## Verification and device review

`ctest` creates isolated POSIX rings with native atomic publication and runs real
GNU Radio flowgraphs. It checks signed scaling and correction headroom, events,
losses, tags, overruns, live attachment, run changes, idle shutdown, a dead
producer with an unfinished position update, malformed layouts, managed control,
failure cleanup and controller timeouts. The fake controller never accesses a
device. The normal repository suite remains `make check`.

Hardware testing is still required: start at 40 Msps into a Null Sink, inspect
`stats()` and the stream service's loss counters, check a known signal's
frequency and level, exercise tuning/gain/filter and start/stop, then increase
to 80/160/320 Msps and try band 2's 640/2. Full-rate performance depends on the
host and downstream DSP; it has not been established by these functional tests.

The plugin source is MIT licensed, like the host stream service. GNU Radio and
its dependencies retain their own licenses.
