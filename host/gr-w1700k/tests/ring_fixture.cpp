// SPDX-License-Identifier: MIT
// A small real POSIX ring publisher for QA. Use the same release/acquire ABI as
// the service, including on ARM hosts; Python mmap writes are not atomics.
#include "w1700k_stream.h"
#include <atomic>
#include <cstring>

extern "C" {
w1700k_stream_header* fixture_create(const char* name, uint64_t slots)
{
    const size_t bytes = W1700K_STREAM_HEADER_BYTES + slots * (8 + W1700K_STREAM_SLICE * 4);
    int fd = shm_open(name, O_CREAT | O_EXCL | O_RDWR, 0600);
    if (fd < 0) return nullptr;
    if (ftruncate(fd, bytes)) { close(fd); shm_unlink(name); return nullptr; }
    auto* h = static_cast<w1700k_stream_header*>(mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0));
    close(fd);
    if (h == MAP_FAILED) { shm_unlink(name); return nullptr; }
    std::memset(h, 0, bytes);
    h->magic = W1700K_STREAM_MAGIC;
    h->version = W1700K_STREAM_VERSION;
    h->header_bytes = W1700K_STREAM_HEADER_BYTES;
    h->slice_samples = W1700K_STREAM_SLICE;
    h->ring_slices = slots;
    h->tags_offset = W1700K_STREAM_HEADER_BYTES;
    h->samples_offset = h->tags_offset + slots * 8;
    h->total_bytes = bytes;
    h->service_pid = getpid();
    w1700k_stream_store(&h->heartbeat_ns, w1700k_stream_now_ns());
    return h;
}

void fixture_destroy(w1700k_stream_header* h, const char* name, uint64_t bytes)
{
    munmap(h, bytes);
    shm_unlink(name);
}

void fixture_heartbeat(w1700k_stream_header* h)
{
    w1700k_stream_store(&h->heartbeat_ns, w1700k_stream_now_ns());
}

void fixture_write(w1700k_stream_header* h, uint64_t run, uint64_t index,
                   uint64_t kind, int16_t i, int16_t q)
{
    auto* tag = w1700k_stream_tags(h) + (index & (h->ring_slices - 1));
    w1700k_stream_store(tag, uint64_t(0));
    std::atomic_thread_fence(std::memory_order_release);
    int16_t* out = w1700k_stream_slice(h, index);
    for (unsigned k = 0; k < W1700K_STREAM_SLICE; ++k) { out[2*k] = i; out[2*k+1] = q; }
    w1700k_stream_store(tag, w1700k_stream_tag(run, index) | kind);
}

void fixture_position(w1700k_stream_header* h, uint64_t run, uint64_t newest)
{
    __atomic_add_fetch(&h->position_seq, 1, __ATOMIC_ACQ_REL);
    w1700k_stream_store(&h->run, run);
    w1700k_stream_store(&h->newest, newest);
    __atomic_add_fetch(&h->position_seq, 1, __ATOMIC_RELEASE);
}
}
