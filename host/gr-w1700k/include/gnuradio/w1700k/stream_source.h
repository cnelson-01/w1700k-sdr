// SPDX-License-Identifier: MIT
#pragma once

#include <gnuradio/sync_block.h>
#include <gnuradio/attributes.h>
#include <functional>
#include <map>
#include <memory>
#include <string>

namespace gr::w1700k {

/** Complex float source for the local w1700k-stream shared ring.
 * Full scale is 8192: corrected samples may exceed +/-1. Starts at the current
 * frontier, follows subsequent runs, skips events, zero-fills known lost slices.
 * Overwritten completions are skipped with a discontinuity tag (their sample
 * count is unknown). A dead service ends the source; restart the flowgraph to
 * attach again. No hardware timestamps are available.
 */
class __GR_ATTR_EXPORT stream_source : virtual public gr::sync_block {
public:
    using sptr = std::shared_ptr<stream_source>;
    static sptr make(const std::string& name = "/w1700k-stream", bool writable = false);

    // Informational rx_rate/rx_freq tags on the next delivered slice. These are
    // not sample-accurate retune boundaries. Zero means unknown (omit that tag).
    virtual void set_metadata(double sample_rate, double center_freq) = 0;
    // Managed sources only: shared correction controls affect every consumer.
    virtual void set_radio_state(double sample_rate, double capture_rate,
                                 double filter_hz, bool flatten) = 0;
    virtual void restart_run() = 0;
    virtual std::map<std::string, uint64_t> stats() const = 0;
    virtual std::string last_error() const = 0;

    // Used by the Python managed source. Installed before starting; start runs
    // after the ring is attached, stop on scheduler shutdown. Neither is called
    // in the sample conversion loop. Callbacks must throw on control failure.
    // A start failure is logged and stored in last_error(); work then returns
    // WORK_DONE. Throwing from start() would strand GNU Radio's startup barrier.
    virtual void set_lifecycle(std::function<void()> start, std::function<void()> stop) = 0;
};

} // namespace gr::w1700k
