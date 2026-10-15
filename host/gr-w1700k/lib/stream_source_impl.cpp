// SPDX-License-Identifier: MIT
#include <gnuradio/w1700k/stream_source.h>
#include <gnuradio/io_signature.h>
#include <gnuradio/gr_complex.h>
#include <gnuradio/sptr_magic.h>
#include <volk/volk.h>
#include "w1700k_stream.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstring>
#include <mutex>
#include <stdexcept>
#include <thread>

namespace gr::w1700k {
namespace {
constexpr uint64_t slice = W1700K_STREAM_SLICE;
pmt::pmt_t integer(uint64_t n) { return pmt::from_uint64(n); }
pmt::pmt_t field(pmt::pmt_t d, const char* k, pmt::pmt_t v)
{
    return pmt::dict_add(d, pmt::intern(k), v);
}

class source_impl final : public stream_source {
public:
    source_impl(std::string name, bool writable)
        : gr::sync_block("w1700k_stream_source", gr::io_signature::make(0, 0, 0),
                         gr::io_signature::make(1, 1, sizeof(gr_complex))),
          name_(std::move(name)), writable_(writable)
    {
        if (name_.size() < 2 || name_[0] != '/' || name_.find('/', 1) != std::string::npos)
            throw std::invalid_argument("stream name must be /NAME (a POSIX shared-memory name)");
        set_output_multiple(slice);
        set_max_noutput_items(65536);
    }

    ~source_impl() override { unmap(); }

    bool start() override
    {
        unmap();
        samples_ = events_ = lost_ = overruns_ = runs_ = 0;
        { std::lock_guard<std::mutex> lock(mutex_); error_.clear(); }
        run_ = next_ = 0;
        new_run_ = true;
        pending_overrun_ = 0;
        await_run_plus_one_ = 0;
        flattened_ = -1;
        seen_metadata_ = 0;
        try {
            h_ = w1700k_stream_open(name_.c_str(), writable_, &bytes_, nullptr);
            if (!h_)
                throw std::runtime_error("cannot open " + name_ + ": " + std::strerror(errno) +
                                         "; start w1700k-stream first");
            validate();
            if (!w1700k_stream_alive(h_))
                throw std::runtime_error("w1700k-stream heartbeat is stale; restart the service");
            if (!position(run_, next_))
                throw std::runtime_error("w1700k-stream position is not stable");
            if (on_start_) {
                controlling_ = true; // also clean up a partially completed start
                on_start_();
            }
        } catch (const std::exception& e) {
            const std::string error = e.what();
            stop();
            fail("source start failed: " + error);
            return false;
        }
        return true;
    }

    bool stop() override
    {
        if (controlling_) {
            controlling_ = false;
            try { if (on_stop_) on_stop_(); }
            catch (const std::exception& e) { fail(std::string("radio stop failed: ") + e.what()); }
        }
        unmap();
        return true;
    }

    void set_lifecycle(std::function<void()> start, std::function<void()> stop) override
    {
        on_start_ = std::move(start);
        on_stop_ = std::move(stop);
    }

    void set_metadata(double rate, double freq) override
    {
        if (!std::isfinite(rate) || !std::isfinite(freq) || rate < 0 || freq < 0)
            throw std::invalid_argument("sample rate and center frequency must be finite and nonnegative");
        std::lock_guard<std::mutex> lock(mutex_);
        rate_ = rate;
        freq_ = freq;
        ++metadata_;
    }

    void set_radio_state(double rate, double capture, double filter, bool flatten) override
    {
        for (double n : {rate, capture, filter})
            if (!std::isfinite(n) || n < 0 || n > 1e9)
                throw std::invalid_argument("invalid radio rate or filter width");
        std::lock_guard<std::mutex> lock(mapping_mutex_);
        if (!writable_) throw std::runtime_error("a passive source cannot change the stream service");
        if (!h_) throw std::runtime_error("stream source is not started");
        w1700k_stream_store(&h_->radio_sample_rate, uint64_t(rate));
        w1700k_stream_store(&h_->radio_capture_rate, uint64_t(capture));
        w1700k_stream_store(&h_->radio_filter_hz, uint64_t(filter));
        w1700k_stream_store(&h_->flatten, uint64_t(flatten));
    }

    void restart_run() override
    {
        std::lock_guard<std::mutex> lock(mapping_mutex_);
        if (!writable_ || !h_) throw std::runtime_error("restart_run requires a started, managed source");
        // A managed start must not deliver the old run's unpublished tail while
        // waiting for the first new packet. Zero means no pending request.
        await_run_plus_one_ = w1700k_stream_load(&h_->run) + 1;
        __atomic_fetch_add(&h_->restart_requests, uint64_t(1), __ATOMIC_ACQ_REL);
    }

    std::map<std::string, uint64_t> stats() const override
    {
        return {{"samples", samples_}, {"events", events_}, {"lost_slices", lost_},
                {"overrun_completions", overruns_}, {"runs", runs_}};
    }

    std::string last_error() const override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        return error_;
    }

    int work(int count, gr_vector_const_void_star&, gr_vector_void_star& output) override
    {
        if (!h_) return WORK_DONE;
        if (!w1700k_stream_alive(h_)) {
            fail("w1700k-stream heartbeat expired; restart the service and flowgraph");
            return WORK_DONE;
        }
        auto* out = static_cast<gr_complex*>(output[0]);
        int produced = 0;
        // Bound even an all-event backlog, so stop() never waits on a spin loop.
        for (unsigned scanned = 0; scanned < 8192 && produced + int(slice) <= count; ++scanned) {
            uint64_t run, newest;
            if (!position(run, newest)) break;
            uint64_t awaiting = await_run_plus_one_;
            if (awaiting) {
                if (run + 1 == awaiting) break;
                await_run_plus_one_.compare_exchange_strong(awaiting, 0);
            }
            if (run != run_) {
                run_ = run;
                next_ = 0;
                new_run_ = true;
                flattened_ = -1;
            }
            if (!run || next_ >= newest) break;
            if (newest - next_ > h_->ring_slices) {
                overrun(newest - next_);
                next_ = newest; // time of overwritten events is unknowable; resume live
                break;
            }
            const auto* tagp = w1700k_stream_tags(h_) + (next_ & (h_->ring_slices - 1));
            const uint64_t tag = w1700k_stream_load(tagp);
            if ((tag & ~W1700K_STREAM_TAG_KIND) != w1700k_stream_tag(run_, next_)) {
                overrun(newest - next_);
                next_ = newest;
                break;
            }
            if (tag & W1700K_STREAM_TAG_EVENT) { ++events_; ++next_; continue; }
            bool lost = tag & W1700K_STREAM_TAG_LOST;
            const char* reason = "transport";
            if (!lost) {
                volk_16i_s32f_convert_32f(reinterpret_cast<float*>(out + produced),
                    w1700k_stream_slice(h_, next_), W1700K_STREAM_FULL_SCALE, 2 * slice);
                std::atomic_thread_fence(std::memory_order_acq_rel);
                if (w1700k_stream_load(tagp) != tag) { lost = true; reason = "overwrite"; }
            }
            if (lost) {
                std::fill_n(out + produced, slice, gr_complex{});
                ++lost_;
            }
            const uint64_t offset = nitems_written(0) + produced;
            if (new_run_) {
                auto value = field(pmt::make_dict(), "run", integer(run_));
                value = field(value, "first_slice", integer(next_));
                emit(offset, "w1700k_run", value);
                new_run_ = false;
                ++runs_;
                seen_metadata_ = 0;
            }
            if (pending_overrun_) {
                auto value = field(pmt::make_dict(), "reason", pmt::intern("overrun"));
                value = field(value, "completions", integer(pending_overrun_));
                // No sample count: some of those completions may have been events.
                emit(offset, "w1700k_gap", value);
                pending_overrun_ = 0;
            }
            if (lost) {
                auto value = field(pmt::make_dict(), "reason", pmt::intern(reason));
                value = field(value, "slice", integer(next_));
                value = field(value, "samples", integer(slice));
                emit(offset, "w1700k_gap", value);
            }
            if (!lost) {
                const int flat = bool(tag & W1700K_STREAM_TAG_FLAT);
                if (flat != flattened_) {
                    emit(offset, "w1700k_flattened", pmt::from_bool(flat));
                    flattened_ = flat;
                }
            }
            metadata_tags(offset);
            produced += slice;
            ++next_;
        }
        samples_ += produced;
        if (!produced) std::this_thread::sleep_for(std::chrono::milliseconds(1));
        return produced;
    }

private:
    void validate()
    {
        const uint64_t n = h_->ring_slices, total = h_->total_bytes;
        if (h_->header_bytes != W1700K_STREAM_HEADER_BYTES || !n || (n & (n - 1)) ||
            h_->tags_offset < W1700K_STREAM_HEADER_BYTES || h_->tags_offset % 8 ||
            h_->samples_offset % 8 || h_->tags_offset > total ||
            n > (total - h_->tags_offset) / 8 ||
            h_->samples_offset < h_->tags_offset + n * 8 || h_->samples_offset > total ||
            n > (total - h_->samples_offset) / (slice * 4))
            throw std::runtime_error("invalid w1700k-stream ring layout");
    }

    bool position(uint64_t& run, uint64_t& newest) const
    {
        // Unlike the shared helper, do not spin forever if a crashed producer
        // left position_seq odd. The heartbeat check ends that source shortly.
        for (unsigned attempt = 0; attempt < 16; ++attempt) {
            uint64_t seq = w1700k_stream_load(&h_->position_seq);
            run = w1700k_stream_load(&h_->run);
            newest = w1700k_stream_load(&h_->newest);
            if (!(seq & 1) && w1700k_stream_load(&h_->position_seq) == seq) return true;
        }
        return false;
    }

    void overrun(uint64_t n) { overruns_ += n; pending_overrun_ += n; }
    void emit(uint64_t offset, const char* key, pmt::pmt_t value)
    {
        add_item_tag(0, offset, pmt::intern(key), value, pmt::intern("w1700k"));
    }
    void metadata_tags(uint64_t offset)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (seen_metadata_ == metadata_) return;
        if (rate_) emit(offset, "rx_rate", pmt::from_double(rate_));
        if (freq_) emit(offset, "rx_freq", pmt::from_double(freq_));
        seen_metadata_ = metadata_;
    }
    void fail(const std::string& error)
    {
        { std::lock_guard<std::mutex> lock(mutex_); error_ = error; }
        d_logger->error("{}", error);
    }
    void unmap()
    {
        std::lock_guard<std::mutex> lock(mapping_mutex_);
        if (h_) munmap(h_, bytes_);
        h_ = nullptr;
    }

    const std::string name_;
    const bool writable_;
    w1700k_stream_header* h_ = nullptr;
    size_t bytes_ = 0;
    uint64_t run_ = 0, next_ = 0, pending_overrun_ = 0, seen_metadata_ = 0;
    bool new_run_ = true, controlling_ = false;
    int flattened_ = -1;
    std::atomic<uint64_t> samples_{0}, events_{0}, lost_{0}, overruns_{0}, runs_{0};
    std::atomic<uint64_t> await_run_plus_one_{0};
    mutable std::mutex mutex_;
    std::mutex mapping_mutex_;
    std::string error_;
    double rate_ = 0, freq_ = 0;
    uint64_t metadata_ = 1;
    std::function<void()> on_start_, on_stop_;
};
} // namespace

stream_source::sptr stream_source::make(const std::string& name, bool writable)
{
    return gnuradio::make_block_sptr<source_impl>(name, writable);
}
} // namespace gr::w1700k
