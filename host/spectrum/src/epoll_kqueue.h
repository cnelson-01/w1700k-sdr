// SPDX-License-Identifier: MIT
// macOS: the subset of epoll and eventfd that web.cpp uses, on kqueue and a pipe.
#pragma once

#include <cerrno>
#include <cstdint>
#include <map>
#include <mutex>

#include <fcntl.h>
#include <sys/event.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

enum : uint32_t { EPOLLIN = 0x001, EPOLLOUT = 0x004, EPOLLERR = 0x008, EPOLLHUP = 0x010, EPOLLRDHUP = 0x2000 };
enum { EPOLL_CTL_ADD = 1, EPOLL_CTL_DEL = 2, EPOLL_CTL_MOD = 3 };
constexpr int EPOLL_CLOEXEC = O_CLOEXEC;
constexpr int EFD_NONBLOCK = O_NONBLOCK;
constexpr int EFD_CLOEXEC = O_CLOEXEC;
constexpr int SOCK_NONBLOCK = 0x10000000;
constexpr int SOCK_CLOEXEC = 0x20000000;
// MSG_NOSIGNAL is in the macOS SDK; accept4 also sets SO_NOSIGPIPE.

union epoll_data {
    void *ptr;
    int fd;
    uint32_t u32;
    uint64_t u64;
};
struct epoll_event {
    uint32_t events;
    epoll_data data;
};

inline int epoll_create1(int)
{
    int kq = kqueue();
    if (kq >= 0) fcntl(kq, F_SETFD, FD_CLOEXEC);
    return kq;
}

inline int epoll_ctl(int kq, int op, int fd, epoll_event *ev)
{
    struct kevent k[2];
    void *udata = ev ? reinterpret_cast<void *>(uintptr_t(ev->data.u64)) : nullptr;
    if (op == EPOLL_CTL_DEL) {
        EV_SET(&k[0], fd, EVFILT_READ, EV_DELETE, 0, 0, nullptr);
        EV_SET(&k[1], fd, EVFILT_WRITE, EV_DELETE, 0, 0, nullptr);
        kevent(kq, k, 2, nullptr, 0, nullptr);  // ENOENT for an unset filter is expected
        return 0;
    }
    EV_SET(&k[0], fd, EVFILT_READ, (ev->events & EPOLLIN) ? EV_ADD : EV_DELETE, 0, 0, udata);
    EV_SET(&k[1], fd, EVFILT_WRITE, (ev->events & EPOLLOUT) ? EV_ADD : EV_DELETE, 0, 0, udata);
    for (auto &one : k) {
        if (kevent(kq, &one, 1, nullptr, 0, nullptr) < 0 && !(one.flags & EV_DELETE)) return -1;
    }
    return 0;
}

inline int epoll_wait(int kq, epoll_event *events, int max, int timeout_ms)
{
    struct kevent k[64];
    if (max > 64) max = 64;
    timespec ts{timeout_ms / 1000, long(timeout_ms % 1000) * 1000000};
    int n = kevent(kq, nullptr, 0, k, max, timeout_ms < 0 ? nullptr : &ts);
    for (int i = 0; i < n; ++i) {
        events[i].data.u64 = uint64_t(reinterpret_cast<uintptr_t>(k[i].udata));
        if (k[i].flags & EV_ERROR)
            events[i].events = EPOLLERR;
        else if (k[i].filter == EVFILT_READ)
            events[i].events = EPOLLIN;  // at EOF too: the read returns 0, as after EPOLLRDHUP
        else
            events[i].events = (k[i].flags & EV_EOF) ? EPOLLERR : EPOLLOUT;
    }
    return n;
}

// An eventfd is one descriptor for both ends; a pipe has two. The read end stands for the
// eventfd, and eventfd_signal() writes to its partner.
inline std::map<int, int> &eventfd_partners()
{
    static std::map<int, int> partners;
    return partners;
}
inline std::mutex &eventfd_mutex()
{
    static std::mutex m;
    return m;
}

inline int eventfd(unsigned, int)
{
    int fds[2];
    if (pipe(fds)) return -1;
    for (int fd : fds) {
        fcntl(fd, F_SETFL, O_NONBLOCK);
        fcntl(fd, F_SETFD, FD_CLOEXEC);
    }
    std::lock_guard<std::mutex> lock(eventfd_mutex());
    eventfd_partners()[fds[0]] = fds[1];
    return fds[0];
}

inline ssize_t eventfd_signal(int fd)
{
    int partner;
    {
        std::lock_guard<std::mutex> lock(eventfd_mutex());
        auto it = eventfd_partners().find(fd);
        if (it == eventfd_partners().end()) return -1;
        partner = it->second;
    }
    char one = 1;
    return write(partner, &one, 1);  // a full pipe already holds a wake-up
}

inline void eventfd_close(int fd)
{
    std::lock_guard<std::mutex> lock(eventfd_mutex());
    auto it = eventfd_partners().find(fd);
    if (it != eventfd_partners().end()) {
        ::close(it->second);
        eventfd_partners().erase(it);
    }
    ::close(fd);
}

inline int accept4(int listener, sockaddr *address, socklen_t *length, int flags)
{
    int fd = accept(listener, address, length);
    if (fd < 0) return fd;
    if (flags & SOCK_NONBLOCK) fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
    if (flags & SOCK_CLOEXEC) fcntl(fd, F_SETFD, FD_CLOEXEC);
    int yes = 1;
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &yes, sizeof(yes));
    return fd;
}
