/*
 * src/interface/worker_main.cpp - hypercore_worker: the engine as a separate process.
 *
 * Why this exists: the boundary keeps crash-on-error, so an engine abort takes down whatever
 * process it runs in. Inside a web server that means every client. Running the engine in its
 * own process moves the blast radius to one worker, and the engine threads never enter the
 * interpreter, so the GIL is not on their path.
 *
 * Build:  cmake --build build-shared --target hypercore-worker
 * Run:    hypercore_worker /tmp/hypercore.sock '{"batch":4}'
 *
 * Framing. Every message is one 24-byte header followed by payload_length bytes.
 *
 *   offset  size  field
 *   0       4     magic  0x53545241
 *   4       4     message type
 *   8       8     request id
 *   16      8     payload length (<= 0x00FFFFFF)
 *
 * The header is 24 bytes and every field sits on its own natural boundary, so a reader can
 * memcpy it straight into the struct with no padding surprises on either side.
 *
 * Payload layouts are stated as byte offsets, not as C structs. A C struct would insert
 * padding the other language would not know about; the offsets below are the contract.
 */
#include "hypercore/hypercore_api.h"

#include <atomic>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "ipc_frame.h"   // the one definition of the frame; the bridge uses the same header

namespace {

using hypercore::ipc::IPC_MAGIC;
using hypercore::ipc::MAX_PAYLOAD;
using hypercore::ipc::Header;
using hypercore::ipc::write_all;
using hypercore::ipc::read_exact;
using hypercore::ipc::MsgType;
using MsgType::CMD_SUBMIT;
using MsgType::CMD_CANCEL;
using MsgType::RESP_STATUS;
using MsgType::STREAM_TOKEN;
using MsgType::STREAM_TERMINAL;
using MsgType::CMD_CAPS;
using MsgType::RESP_CAPS;
using MsgType::CMD_PING;
using MsgType::RESP_PONG;

hypercore_engine_t* g_engine = nullptr;
int g_client_fd = -1;
std::atomic<bool> g_link_open{true};
std::mutex g_write_mu;

/* One frame, one syscall. Two writes per token is two syscalls per token and lets two
 * engine threads interleave a header with someone else's payload. */
void send_frame(uint32_t type, uint64_t request_id, const void* payload, size_t len) {
    if (!g_link_open.load(std::memory_order_relaxed)) return;
    std::vector<uint8_t> frame(sizeof(Header) + len);
    Header h{IPC_MAGIC, type, request_id, len};
    memcpy(frame.data(), &h, sizeof h);
    if (len) memcpy(frame.data() + sizeof h, payload, len);
    std::lock_guard<std::mutex> lock(g_write_mu);
    if (!write_all(g_client_fd, frame.data(), frame.size())) g_link_open.store(false);
}

/* Engine threads call this. It does no allocation beyond the frame buffer and never touches
 * the interpreter - that is the whole point of putting the engine in its own process. */
void ipc_stream_callback(const hypercore_output_chunk_t* chunk, void* user_data) {
    const uint64_t request_id = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(user_data));

    if (chunk->token_id >= 0) {
        uint8_t payload[8];
        memcpy(payload, &chunk->token_id, 4);
        memcpy(payload + 4, &chunk->prompt_reused, 4);
        send_frame(STREAM_TOKEN, request_id, payload, sizeof payload);
        return;
    }

    if (chunk->summary) {
        /* Frozen wire layout. The payload is 4 bytes of prompt_reused followed by the
         * 68-byte summary, written field by field. Nothing here reads a struct's memory
         * image: the byte offsets are stated literally, so a compiler, a different
         * toolchain, or a future field in the engine's internal summary cannot move a
         * byte on the wire. The asserts below are the contract made executable.
         *
         * The first 40 bytes are the layout every reader since 0.1.30 knows; the 28 bytes
         * after them are the expert-tier accounting the Monitor needs (#Monitor). A reader
         * that only knows the prefix still gets a correct frame - it reads the prefix and
         * discards the tail, which is the direction the forward-compatibility rule goes. */
        static constexpr size_t WIRE_SUMMARY_SIZE = 68;
        static constexpr size_t WIRE_SUMMARY_PREFIX = 40;
        static_assert(sizeof(hypercore_request_summary_t) == WIRE_SUMMARY_SIZE,
                      "hypercore_request_summary_t is no longer 68 bytes packed");
        static_assert(offsetof(hypercore_request_summary_t, struct_size) == 0, "offset");
        static_assert(offsetof(hypercore_request_summary_t, produced_tokens) == 4, "offset");
        static_assert(offsetof(hypercore_request_summary_t, prefill_ms) == 8, "offset");
        static_assert(offsetof(hypercore_request_summary_t, decode_ms) == 16, "offset");
        static_assert(offsetof(hypercore_request_summary_t, drafts_accepted) == 24, "offset");
        static_assert(offsetof(hypercore_request_summary_t, drafts_offered) == 28, "offset");
        static_assert(offsetof(hypercore_request_summary_t, stop_reason) == 32, "offset");
        static_assert(offsetof(hypercore_request_summary_t, time_to_first_token_ms) == 36,
                      "byte 36 is the reserved TTFT slot; moving it breaks every reader");
        static_assert(offsetof(hypercore_request_summary_t, cache_hits) == 40, "offset");
        static_assert(offsetof(hypercore_request_summary_t, cache_lookups) == 44, "offset");
        static_assert(offsetof(hypercore_request_summary_t, offloaded) == 48, "offset");
        static_assert(offsetof(hypercore_request_summary_t, prompt_read) == 52, "offset");
        static_assert(offsetof(hypercore_request_summary_t, ram_blobs) == 56, "offset");
        static_assert(offsetof(hypercore_request_summary_t, file_blobs) == 60, "offset");
        static_assert(offsetof(hypercore_request_summary_t, file_kb) == 64, "offset");

        uint8_t payload[4 + WIRE_SUMMARY_SIZE];
        memset(payload, 0, sizeof payload);

        const hypercore_request_summary_t* s = chunk->summary;
        const uint32_t wire_summary_size = static_cast<uint32_t>(WIRE_SUMMARY_SIZE);
        const uint32_t wire_stop_reason  = static_cast<uint32_t>(s->stop_reason);
        const uint32_t wire_ttft_ms      = s->time_to_first_token_ms;

        memcpy(payload + 0, &chunk->prompt_reused, 4);   /* header field, not part of the summary */
        memcpy(payload + 4, &wire_summary_size, 4);      /* protocol version tag */
        memcpy(payload + 8, &s->produced_tokens, 4);
        memcpy(payload + 12, &s->prefill_ms, 8);
        memcpy(payload + 20, &s->decode_ms, 8);
        memcpy(payload + 28, &s->drafts_accepted, 4);
        memcpy(payload + 32, &s->drafts_offered, 4);
        memcpy(payload + 36, &wire_stop_reason, 4);
        memcpy(payload + 40, &wire_ttft_ms, 4);   /* byte 36-39 of the summary */
        memcpy(payload + 44 + 0, &s->cache_hits, 4);
        memcpy(payload + 44 + 4, &s->cache_lookups, 4);
        memcpy(payload + 44 + 8, &s->offloaded, 4);
        memcpy(payload + 44 + 12, &s->prompt_read, 4);
        memcpy(payload + 44 + 16, &s->ram_blobs, 4);
        memcpy(payload + 44 + 20, &s->file_blobs, 4);
        memcpy(payload + 44 + 24, &s->file_kb, 4);
        (void) WIRE_SUMMARY_PREFIX;   /* the prefix is the first 40 bytes of the summary above */

        send_frame(STREAM_TERMINAL, request_id, payload, sizeof payload);
    }
}

void handle_submit(const Header& h, const std::vector<uint8_t>& payload) {
    /* Payload: config 28 bytes, reserved 4, prompt_len 8, then prompt_len int32 tokens.
     * The reserved field keeps prompt_len on an 8-byte boundary without leaving the layout
     * to compiler padding. */
    const size_t cfg_size = sizeof(hypercore_request_config_t);   // 28
    const size_t head_size = cfg_size + 4 + 8;                 // + reserved + prompt_len
    if (h.payload_length < head_size) {
        send_frame(RESP_STATUS, h.request_id, nullptr, 0);
        return;
    }

    hypercore_request_config_t config;
    memset(&config, 0, sizeof config);
    memcpy(&config, payload.data(), cfg_size);

    uint64_t declared_len = 0;
    memcpy(&declared_len, payload.data() + cfg_size + 4, sizeof declared_len);

    const uint64_t tokens_from_length = (h.payload_length - head_size) / 4;
    if (declared_len != tokens_from_length || declared_len > MAX_PAYLOAD) {
        /* Two length fields that disagree is a framing bug, not a bad request. Say so with
         * INVALID_ARG rather than reading past the buffer. */
        int32_t status = HYPERCORE_STATUS_INVALID_ARG;
        send_frame(RESP_STATUS, h.request_id, &status, sizeof status);
        return;
    }

    const int32_t* tokens = reinterpret_cast<const int32_t*>(payload.data() + head_size);

    /* The ack goes out BEFORE the request runs.
     * hypercore_engine_submit() in this bridge runs a request to completion: every token
     * frame and the terminal frame it produces are written to the socket inside that call.
     * An ack sent after it therefore reaches the host after the whole reply, and the host
     * - which is documented to read the ack first (serve/ipc_parser.py, stream() in
     * serve/engine_supervisor.py) - fails the request with "expected an ack frame, got 4".
     * The status the host acts on is the one this call can decide now: the framing has
     * already been checked above, and a live engine takes the request.
     * If the run itself fails, the terminal frame below closes the stream so the host
     * does not sit on its silence wall waiting for a request that will never answer. */
    int32_t status = HYPERCORE_STATUS_SUCCESS;
    send_frame(RESP_STATUS, h.request_id, &status, sizeof status);

    hypercore_status_t st = hypercore_engine_submit(
        g_engine, h.request_id, &config, tokens, static_cast<size_t>(declared_len),
        ipc_stream_callback, reinterpret_cast<void*>(static_cast<uintptr_t>(h.request_id)));

    if (st != HYPERCORE_STATUS_SUCCESS) {
        uint8_t payload_zero[4 + 68];   /* prompt_reused + a zeroed 68-byte summary */
        memset(payload_zero, 0, sizeof payload_zero);
        static constexpr uint32_t wire_summary_size = 68;
        memcpy(payload_zero + 4, &wire_summary_size, 4);
        send_frame(STREAM_TERMINAL, h.request_id, payload_zero, sizeof payload_zero);
    }
}

void handle_caps(const Header& h) {
    hypercore_caps_t caps;
    memset(&caps, 0, sizeof caps);
    caps.struct_size = sizeof caps;
    hypercore_status_t st = hypercore_engine_get_caps(g_engine, &caps);
    int32_t status = static_cast<int32_t>(st);
    /* backend, slots, max_context, pool_workers, status, instance_pcie_frac, instance_spec_min_p,
       then the expert tier: slots, slots_full, cache MiB, cache full MiB, free VRAM MiB, expert
       source, prompt chunk. 7 x 4 = 28 bytes, then 7 x 4 = 28 more. The floats are the resolved
       Level-1 baseline, so a calibration runner can read the starting point of a sweep without
       submitting a probe request.

       A reader that only knows the first 28 bytes reads those and ignores the tail; that is the
       forward-compatibility rule of this header, and it is why the fields are appended rather
       than inserted. */
    uint8_t payload[56];
    memset(payload, 0, sizeof payload);
    memcpy(payload + 0,  &caps.backend_type,        4);
    memcpy(payload + 4,  &caps.max_batch_slots,     4);
    memcpy(payload + 8,  &caps.max_context_len,     4);
    memcpy(payload + 12, &caps.active_pool_workers, 4);
    memcpy(payload + 16, &status,                   4);
    memcpy(payload + 20, &caps.instance_pcie_frac,  4);
    memcpy(payload + 24, &caps.instance_spec_min_p, 4);
    memcpy(payload + 28, &caps.expert_slots,            4);
    memcpy(payload + 32, &caps.expert_slots_full,       4);
    memcpy(payload + 36, &caps.expert_cache_mib,        4);
    memcpy(payload + 40, &caps.expert_cache_full_mib,   4);
    memcpy(payload + 44, &caps.vram_free_mib,           4);
    memcpy(payload + 48, &caps.expert_source,           4);
    memcpy(payload + 52, &caps.prompt_chunk,            4);
    send_frame(RESP_CAPS, h.request_id, payload, sizeof payload);
}

void handle_ping(const Header& h) {
    static uint64_t served = 0;
    ++served;
    uint32_t alive = 1;
    send_frame(RESP_PONG, h.request_id, &alive, sizeof alive);
    (void)served;
}

void multiplex(int fd) {
    g_client_fd = fd;
    g_link_open.store(true);

    while (g_link_open.load(std::memory_order_relaxed)) {
        Header h;
        if (!read_exact(fd, &h, sizeof h)) break;   // EOF or a dead link

        if (h.magic != IPC_MAGIC) {
            std::fprintf(stderr, "hypercore_worker: framing desync (magic %08x)\n", h.magic);
            break;
        }
        if (h.payload_length > MAX_PAYLOAD) {
            std::fprintf(stderr, "hypercore_worker: payload too large (%llu)\n",
                        (unsigned long long)h.payload_length);
            break;
        }

        std::vector<uint8_t> payload(h.payload_length);
        if (h.payload_length && !read_exact(fd, payload.data(), h.payload_length)) break;

        switch (h.type) {
            case CMD_SUBMIT:  handle_submit(h, payload); break;
            case CMD_CANCEL:  hypercore_engine_cancel(g_engine, h.request_id); break;
            case CMD_CAPS:    handle_caps(h); break;
            case CMD_PING:    handle_ping(h); break;
            default:
                std::fprintf(stderr, "hypercore_worker: unknown message type %u\n", h.type);
                break;
        }
    }
    g_link_open.store(false);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: hypercore_worker <socket-path> [json-config]\n");
        return 2;
    }

    hypercore_status_t st = hypercore_engine_init(&g_engine, argc > 2 ? argv[2] : "{}");
    if (st != HYPERCORE_STATUS_SUCCESS) {
        std::fprintf(stderr, "hypercore_worker: engine init failed (%d)\n", static_cast<int>(st));
        return 1;
    }

    int listener = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listener < 0) { hypercore_engine_destroy(g_engine); return 1; }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    if (strlen(argv[1]) >= sizeof addr.sun_path) {
        std::fprintf(stderr, "hypercore_worker: socket path too long\n");
        return 2;
    }
    strncpy(addr.sun_path, argv[1], sizeof addr.sun_path - 1);

    /* A socket name outlives the process that bound it, so the leftover file is the usual
     * reason bind() answers EADDRINUSE. unlink() cannot clear a name this process does not
     * own, and it cannot clear a directory. Saying which one it is turns "cannot bind" into
     * something a reader can act on, instead of a path and a shrug. */
    if (unlink(argv[1]) != 0 && errno != ENOENT) {
        std::fprintf(stderr, "hypercore_worker: could not remove %s: %s\n",
                     argv[1], strerror(errno));
    }

    if (bind(listener, (struct sockaddr*)&addr, sizeof addr) != 0 || listen(listener, 1) != 0) {
        std::fprintf(stderr, "hypercore_worker: cannot bind %s: %s\n",
                     argv[1], strerror(errno));
        close(listener);
        hypercore_engine_destroy(g_engine);
        return 1;
    }

    int client = accept(listener, nullptr, nullptr);
    close(listener);
    if (client < 0) { hypercore_engine_destroy(g_engine); return 1; }

    multiplex(client);

    /* Reaching here means the parent is gone. Join the request threads before exiting so the
     * engine is not torn down while streams are still writing to a closed socket. */
    hypercore_engine_destroy(g_engine);
    close(client);
    return 0;
}
