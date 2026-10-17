/*
 * src/interface/ipc_frame.h - the binary frame both ends of the worker boundary speak.
 *
 * Shared by src/interface/worker_main.cpp (the daemon) and src/interface/hypercore_bridge.cpp
 * (the side that spawns and reads it). There is one definition of the header, the message
 * types and the exact-arity I/O helpers; anything that drifts is a protocol bug, not a
 * compiler difference.
 *
 * The payload of every frame is laid out by the public structs in
 * include/hypercore/hypercore_api.h, which are #pragma pack and size-checked with
 * static_assert. This header adds no layout of its own.
 */
#ifndef HYPERCORE_IPC_FRAME_H
#define HYPERCORE_IPC_FRAME_H

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <atomic>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <vector>

namespace hypercore {
namespace ipc {

/* "STRA", read as a big-endian literal. Kept for compatibility with the first worker build;
 * a frame that does not start with it is not this protocol and is dropped. */
constexpr uint32_t IPC_MAGIC   = 0x53545241u;
constexpr uint64_t MAX_PAYLOAD = 0x00FFFFFFull;

enum MsgType : uint32_t {
    CMD_SUBMIT        = 1,
    CMD_CANCEL        = 2,
    RESP_STATUS       = 3,
    STREAM_TOKEN      = 4,
    STREAM_TERMINAL   = 5,
    CMD_CAPS          = 6,
    RESP_CAPS         = 7,
    CMD_PING          = 8,
    RESP_PONG         = 9,
};

struct Header {
    uint32_t magic;
    uint32_t type;
    uint64_t request_id;
    uint64_t payload_length;
};

static_assert(sizeof(Header) == 24, "the frame header is 24 bytes with no padding");

/* Exact-arity I/O. A single write() on a socket is not guaranteed to write the whole buffer,
 * and a single read() is not guaranteed to fill it; both can return short. Treating either as
 * atomic is the assumption that breaks first under load. */
inline bool write_all(int fd, const void* data, size_t n) {
    const char* p = static_cast<const char*>(data);
    while (n > 0) {
        ssize_t w = write(fd, p, n);
        if (w <= 0) return false;
        p += static_cast<size_t>(w);
        n -= static_cast<size_t>(w);
    }
    return true;
}

inline bool read_exact(int fd, void* data, size_t n) {
    char* p = static_cast<char*>(data);
    while (n > 0) {
        ssize_t r = read(fd, p, n);
        if (r <= 0) return false;
        p += static_cast<size_t>(r);
        n -= static_cast<size_t>(r);
    }
    return true;
}

/* One frame, one syscall. Two writes per token is two syscalls per token and lets two
 * engine threads interleave a header with someone else's payload. */
inline bool send_frame(int fd, uint32_t type, uint64_t request_id,
                       const void* payload, size_t len) {
    std::vector<uint8_t> frame(sizeof(Header) + len);
    Header h{IPC_MAGIC, type, request_id, len};
    memcpy(frame.data(), &h, sizeof h);
    if (len) memcpy(frame.data() + sizeof h, payload, len);
    return write_all(fd, frame.data(), frame.size());
}

}  // namespace ipc
}  // namespace hypercore

#endif  // HYPERCORE_IPC_FRAME_H
