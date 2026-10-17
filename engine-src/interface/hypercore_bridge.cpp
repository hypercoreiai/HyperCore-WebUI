// src/interface/hypercore_bridge.cpp
// The v1 boundary, implemented as the contract specifies: submit() is ASYNCHRONOUS. It reserves a
// batch slot, returns, and the stream callback fires from the request's own thread, ending with the
// terminal summary chunk.
//
// This is the version an embedder can actually test against. The bug it exposes is in the adapter,
// not here: a synchronous read of the metrics right after submit() sees nothing.
//
// The boundary is not a shell. It calls into the engine spine - the CPU pool topology from
// hypercore_kernels_cpu and the suffix drafter from hypercore_spec - so the capability numbers and the
// draft counts it reports come from engine code, not from a fixture constant.
#include "hypercore/hypercore_api.h"
#include "hypercore/kernels/cpu/pool.hpp"
#include "hypercore/spec/suffix_drafter.hpp"
#include "engine_process.h"   // a real engine, driven as a child process - see that header

#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <new>
#include <thread>
#include <unordered_map>
#include <vector>

#include <signal.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <sys/un.h>
#include <unistd.h>

#include "ipc_frame.h"   // the one definition of the frame; worker_main uses the same header

namespace hypercore {
    struct InternalEngineOptions {
        int32_t  pool_workers  = -1;
        float    pcie_frac     = -1.0f;
        float    spec_min_p    = -1.0f;
        int32_t  batch_slots   = 4;      // int32 so a bad parse is distinguishable from 0
        uint32_t max_context   = 4096;
    };

    /* One per in-flight request. cancel() only sets a flag; the request thread notices it
       between tokens. A flag rather than a join because cancel must not block: the caller is
       an HTTP thread that has already stopped reading. */
    struct RequestState {
        uint64_t id;
        hypercore_request_config_t effective{};   /* request frame resolved against the instance baseline */
        std::atomic<bool> cancelled{false};
    };

    class EngineContext {
    public:
        InternalEngineOptions options;
        std::atomic<int64_t> active{0};
        std::vector<std::thread> running;   // the async requests, joined on destroy
        bool is_initialized = false;

        /* request_id -> state. Guarded because submit(), cancel() and the request thread all
           touch it from different threads. Entries are removed by the request thread when it
           finishes, so a stale id can never be cancelled twice or reuse a slot. */
        std::mutex reg_mu;
        std::unordered_map<uint64_t, std::unique_ptr<RequestState>> registry;

        /* Out-of-process mode. The public contract is still the same five entry points: the
           engine runs in a child process and the frames cross a UNIX socket that the embedder
           never sees. The reason to use it is crash isolation - an engine fault kills the
           child and the host gets HYPERCORE_STATUS_ENGINE_DEAD instead of a signal.
           "inprocess" is the default and what every existing test exercises. */
        std::string engine_mode = "inprocess";   // "inprocess" | "worker"
        std::string worker_binary;
        std::string ipc_socket;
        std::string config_json;                 // handed to the child verbatim
        pid_t worker_pid = -1;
        int worker_fd = -1;
        /* A real model, when the instance config names one. Empty otherwise, and then the
           boundary is the fixture, which is what every existing test exercises. */
        hypercore::iface::EngineProcess model;
        std::mutex worker_mu;                    // serialises writes to the child
        std::thread worker_reader;
        std::atomic<bool> link_open{false};

        /* Every in-flight stream shares one socket, so one reader thread dispatches frames by
         * request_id. A per-request reader would steal each other's frames the moment batch > 1. */
        struct Sink { hypercore_stream_cb cb; void* user_data; };
        std::unordered_map<uint64_t, Sink> sinks;
    };
}

static const char* safe_getenv(const char* var) { return std::getenv(var); }

static bool parse_int(const char* s, int32_t& out) {
    char* end = nullptr;
    const long v = std::strtol(s, &end, 10);
    if (end == s || *end != '\0') return false;
    out = (int32_t) v;
    return true;
}
static bool parse_float(const char* s, float& out) {
    char* end = nullptr;
    const double v = std::strtod(s, &end);
    if (end == s || *end != '\0') return false;
    out = (float) v;
    return true;
}

static void merge_env_into_options(hypercore::InternalEngineOptions& opts) {
    if (const char* v = safe_getenv("HYPERCORE_POOL_WORKERS"))  parse_int(v, opts.pool_workers);
    if (const char* v = safe_getenv("HYPERCORE_PCIE_FRAC"))     parse_float(v, opts.pcie_frac);
    if (const char* v = safe_getenv("HYPERCORE_SPEC_MIN_P"))    parse_float(v, opts.spec_min_p);
    if (const char* v = safe_getenv("HYPERCORE_BATCH"))         parse_int(v, opts.batch_slots);
}

static bool json_number(const char* s, const char* key, double& out) {
    const char* p = std::strstr(s, key);
    if (p == nullptr) return false;
    p += std::strlen(key);
    while (*p == ' ' || *p == ':') ++p;
    char* end = nullptr;
    const double v = std::strtod(p, &end);
    if (end == p) return false;
    out = v;
    return true;
}

static void merge_json_into_options(const char* json, hypercore::InternalEngineOptions& opts) {
    if (json == nullptr) return;
    double v;
    if (json_number(json, "\"pool_workers\"", v))  opts.pool_workers = (int32_t) v;
    if (json_number(json, "\"pcie_frac\"", v))     opts.pcie_frac = (float) v;
    if (json_number(json, "\"spec_min_p\"", v))    opts.spec_min_p = (float) v;
    if (json_number(json, "\"batch\"", v))         opts.batch_slots = (uint32_t) v;
    if (json_number(json, "\"max_context\"", v))   opts.max_context = (uint32_t) v;
}

static bool json_string(const std::string& json, const std::string& key, std::string& out) {
    const std::string needle = "\"" + key + "\"";
    size_t i = json.find(needle);
    if (i == std::string::npos) return false;
    i = json.find(':', i + needle.size());   // past the key's own closing quote and the colon
    if (i == std::string::npos) return false;
    i = json.find('"', i + 1);
    if (i == std::string::npos) return false;
    size_t end = json.find('"', i + 1);
    if (end == std::string::npos) return false;
    out = json.substr(i + 1, end - i - 1);
    return true;
}

/* A JSON array of strings, e.g. "engine_args": ["--pack", "pack/full"]. Deliberately small:
 * the boundary needs one array shape, and a full parser here would be a dependency the rest
 * of the interface does not have. Escapes are not interpreted - an argument containing one is
 * an argument the caller should pass differently. */
static bool json_string_array(const std::string& json, const std::string& key,
                             std::vector<std::string>& out) {
    const std::string needle = "\"" + key + "\"";
    size_t i = json.find(needle);
    if (i == std::string::npos) return false;
    i = json.find(':', i + needle.size());
    if (i == std::string::npos) return false;
    i = json.find('[', i + 1);
    if (i == std::string::npos) return false;
    size_t end = json.find(']', i + 1);
    if (end == std::string::npos) return false;

    size_t p = i + 1;
    while (p < end) {
        size_t q = json.find('"', p);
        if (q == std::string::npos || q >= end) break;
        size_t r = json.find('"', q + 1);
        if (r == std::string::npos || r > end) break;
        out.push_back(json.substr(q + 1, r - q - 1));
        p = r + 1;
    }
    return true;
}

/* --- where the instance config comes from ------------------------------------
 *
 * init() accepts either an inline JSON object or a path to one. The form is decided by
 * the first non-whitespace character, so there is no guessing and no heuristic:
 *
 *   '{'  inline JSON, used verbatim
 *   else a file path, read once here - the worker is handed the resolved text and never
 *        re-reads the file, so a config change and a restart cannot disagree
 *   NULL or blank  no configuration at all
 *
 * A path that cannot be opened, an empty file, or a payload that is not a JSON object is
 * INVALID_ARG rather than "no config": a typo in a path, or a config file that a
 * provisioning script created but never wrote, must not run the engine on hidden defaults.
 * Only an absent argument is "no config". Both forms sit at the same precedence level -
 * system defaults, then HYPERCORE_* environment, then this config. */
static constexpr size_t MAX_CONFIG_BYTES = 10u * 1024u * 1024u;  /* 10 MB structural ceiling */

static bool is_blank(const std::string& s) {
    for (char c : s) {
        if (!std::isspace(static_cast<unsigned char>(c))) return false;
    }
    return true;
}

static std::string trimmed(const std::string& s) {
    const size_t first = s.find_first_not_of(" \t\r\n\f\v");
    if (first == std::string::npos) return std::string();
    const size_t last = s.find_last_not_of(" \t\r\n\f\v");
    return s.substr(first, last - first + 1);
}

static bool looks_like_json_object(const std::string& s) { return !s.empty() && s[0] == '{'; }

/* Reads a configuration payload without ever buffering more than the ceiling: the size is
 * taken from the file before anything is allocated, then the document is streamed in fixed
 * blocks. An inline object is forwarded character-for-character - no trim, no re-encode -
 * so the worker sees exactly the bytes the caller wrote. */
static hypercore_status_t load_config_text(const char* config, std::string& out) {
    out.clear();
    if (config == nullptr) return HYPERCORE_STATUS_SUCCESS;

    /* The ceiling is checked on the argument itself, before any copy is made. */
    if (std::strlen(config) > MAX_CONFIG_BYTES) return HYPERCORE_STATUS_INVALID_ARG;

    const std::string arg = trimmed(config);
    if (arg.empty()) return HYPERCORE_STATUS_SUCCESS;      /* no config: engine baselines */
    if (arg[0] == '{') { out = config; return HYPERCORE_STATUS_SUCCESS; }   /* verbatim */

    std::FILE* f = std::fopen(arg.c_str(), "rb");
    if (!f) return HYPERCORE_STATUS_INVALID_ARG;

    if (std::fseek(f, 0, SEEK_END) != 0) { std::fclose(f); return HYPERCORE_STATUS_INVALID_ARG; }
    const long long size = static_cast<long long>(std::ftell(f));
    if (size < 0) { std::fclose(f); return HYPERCORE_STATUS_INVALID_ARG; }
    if (size == 0) { std::fclose(f); return HYPERCORE_STATUS_INVALID_ARG; }   /* empty file */
    if (static_cast<size_t>(size) > MAX_CONFIG_BYTES) {
        std::fclose(f);
        return HYPERCORE_STATUS_INVALID_ARG;                                   /* over ceiling */
    }
    std::rewind(f);

    std::string body;
    body.reserve(static_cast<size_t>(size));
    char buffer[4096];
    size_t got = 0;
    while ((got = std::fread(buffer, 1, sizeof buffer, f)) > 0) {
        body.append(buffer, got);
        if (body.size() > MAX_CONFIG_BYTES) {   /* the file grew between stat and read */
            std::fclose(f);
            return HYPERCORE_STATUS_INVALID_ARG;
        }
    }
    std::fclose(f);

    if (is_blank(body)) return HYPERCORE_STATUS_INVALID_ARG;   /* whitespace is not a config */
    if (!looks_like_json_object(trimmed(body))) return HYPERCORE_STATUS_INVALID_ARG;
    out = std::move(body);
    return HYPERCORE_STATUS_SUCCESS;
}

static bool abi_declared_ok(uint32_t declared, size_t full, size_t minimum) {
    return declared >= minimum && declared <= full;
}

/* The sentinel is exactly -1.0f. -0.0f is not a sentinel: it compares equal to 0.0, which is a
   real value on the domain. Anything else that is not in [0,1] is out of domain and rejected, never clamped. */
static bool is_override_sentinel(float v) { return v == -1.0f; }
static bool in_unit_interval(float v)     { return v >= 0.0f && v <= 1.0f; }

/* Level 2 over Level 1. Returns INVALID_ARG on an out-of-domain field so the caller learns
   about a bad frame here rather than watching the engine run at the wrong settings. */
static hypercore_status_t resolve_request(const hypercore::EngineContext& ctx,
                                         const hypercore_request_config_t* request,
                                         hypercore_request_config_t* out) {
    if (!is_override_sentinel(request->pcie_frac) && !in_unit_interval(request->pcie_frac))
        return HYPERCORE_STATUS_INVALID_ARG;
    if (!is_override_sentinel(request->spec_min_p) && !in_unit_interval(request->spec_min_p))
        return HYPERCORE_STATUS_INVALID_ARG;

    hypercore_request_config_t merged = *request;
    if (is_override_sentinel(merged.pcie_frac))    merged.pcie_frac    = ctx.options.pcie_frac;
    if (is_override_sentinel(merged.spec_min_p))   merged.spec_min_p   = ctx.options.spec_min_p;
    *out = merged;
    return HYPERCORE_STATUS_SUCCESS;
}

extern "C" {

struct hypercore_engine_t {
    hypercore::EngineContext ctx;
};

/* ---- the worker boundary: spawn, connect, dispatch -------------------------- */

/* Returns the connected fd, or -1. Declared int, not bool: a socket fd of 1 is a valid
 * answer and would be indistinguishable from true. */
static int ipc_connect(const std::string& path, int wait_ms) {
    for (int waited = 0; waited < wait_ms; waited += 50) {
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd < 0) return -1;
        struct sockaddr_un sa;
        memset(&sa, 0, sizeof sa);
        sa.sun_family = AF_UNIX;
        if (path.size() >= sizeof sa.sun_path) { close(fd); return -1; }
        strncpy(sa.sun_path, path.c_str(), sizeof sa.sun_path - 1);
        if (connect(fd, (const struct sockaddr*)&sa, sizeof sa) == 0) return fd;
        close(fd);
        usleep(50000);   // the child is still binding; retry rather than fail on a race
    }
    return -1;
}

static bool read_frame(int fd, hypercore::ipc::Header& h, std::vector<uint8_t>& payload) {
    if (!hypercore::ipc::read_exact(fd, &h, sizeof h)) return false;
    if (h.magic != hypercore::ipc::IPC_MAGIC || h.payload_length > hypercore::ipc::MAX_PAYLOAD)
        return false;
    payload.assign(h.payload_length, 0);
    return h.payload_length == 0 ||
           hypercore::ipc::read_exact(fd, payload.data(), h.payload_length);
}

static void worker_read_loop(hypercore::EngineContext* ctx) {
    while (true) {
        hypercore::ipc::Header h;
        std::vector<uint8_t> payload;
        if (!read_frame(ctx->worker_fd, h, payload)) {
            ctx->link_open.store(false);   // the child is gone; submit() reports it next time
            return;
        }
        if (h.type != hypercore::ipc::STREAM_TOKEN && h.type != hypercore::ipc::STREAM_TERMINAL)
            continue;   // RESP_STATUS is the child's own gate; the host already holds the slot

        hypercore_stream_cb cb = nullptr;
        void* user_data = nullptr;
        {
            std::lock_guard<std::mutex> lock(ctx->reg_mu);
            auto it = ctx->sinks.find(h.request_id);
            if (it == ctx->sinks.end()) continue;
            cb = it->second.cb;
            user_data = it->second.user_data;
            if (h.type == hypercore::ipc::STREAM_TERMINAL) ctx->sinks.erase(it);
        }
        if (!cb) continue;

        hypercore_output_chunk_t chunk{};
        if (h.type == hypercore::ipc::STREAM_TOKEN) {
            if (payload.size() < sizeof(int32_t) + sizeof(uint32_t)) continue;
            memcpy(&chunk.token_id, payload.data(), sizeof chunk.token_id);
            memcpy(&chunk.prompt_reused, payload.data() + 4, sizeof chunk.prompt_reused);
            chunk.summary = nullptr;
            cb(&chunk, user_data);
        } else {
            /* The terminal payload is prompt_reused (4 bytes) followed by the summary struct.
               A child that declares fewer than the minimum bytes is refused by the worker before
               it ever reaches here. */
            if (payload.size() < 4 + sizeof(hypercore_request_summary_t)) {
                ctx->link_open.store(false);
                return;
            }
            hypercore_request_summary_t summary;
            memcpy(&summary, payload.data() + 4, sizeof summary);
            chunk.summary = &summary;   /* valid only for this call, exactly as in-process mode */
            chunk.token_id = -1;
            memcpy(&chunk.prompt_reused, payload.data(), sizeof chunk.prompt_reused);
            cb(&chunk, user_data);
            ctx->active.fetch_sub(1);
        }
    }
}

static hypercore_status_t start_worker(hypercore::EngineContext& ctx) {
    if (ctx.worker_binary.empty() || ctx.ipc_socket.empty())
        return HYPERCORE_STATUS_INVALID_ARG;

    /* The child must not inherit the mode. It links the same bridge, so handing it the same
       config would make it spawn its own worker and recurse until the process table runs out.
       The parser reads the first match, so an explicit in-process entry prepended to the config
       is enough to keep the child an engine and never a supervisor. */
    std::string child_config = ctx.config_json.empty() ? "{}" : ctx.config_json;
    if (child_config[0] == '{')
        child_config = "{\"engine_mode\":\"inprocess\"," + child_config.substr(1);
    else
        child_config = "{\"engine_mode\":\"inprocess\"}";

    pid_t pid = fork();
    if (pid < 0) return HYPERCORE_STATUS_ENGINE_DEAD;
    if (pid == 0) {
        /* argv[1] is the socket, argv[2] the instance config - the same interface the
           supervisor uses, so a hand-run worker and a spawned one are the same process. */
        execl(ctx.worker_binary.c_str(), ctx.worker_binary.c_str(),
              ctx.ipc_socket.c_str(), child_config.c_str(), nullptr);
        _exit(127);   // exec failed: the child must not fall through into the parent's code
    }
    ctx.worker_pid = pid;

    int fd = ipc_connect(ctx.ipc_socket, 3000);
    if (fd < 0) {
        kill(ctx.worker_pid, SIGTERM);
        waitpid(ctx.worker_pid, nullptr, 0);
        ctx.worker_pid = -1;
        return HYPERCORE_STATUS_ENGINE_DEAD;
    }
    ctx.worker_fd = fd;
    ctx.link_open.store(true);
    ctx.worker_reader = std::thread(worker_read_loop, &ctx);
    return HYPERCORE_STATUS_SUCCESS;
}

static bool worker_send(hypercore::EngineContext& ctx, uint32_t type, uint64_t request_id,
                        const void* payload, size_t len) {
    if (ctx.worker_fd < 0) return false;
    std::lock_guard<std::mutex> lock(ctx.worker_mu);
    return hypercore::ipc::send_frame(ctx.worker_fd, type, request_id, payload, len);
}

HYPERCORE_API hypercore_status_t hypercore_engine_init(hypercore_engine_t** out_engine, const char* config) {
    if (!out_engine) return HYPERCORE_STATUS_INVALID_ARG;

    /* Resolve the config source before anything is allocated: a bad path is a bad argument,
       and there is nothing to unwind in that case. */
    std::string resolved_config;
    const hypercore_status_t config_status = load_config_text(config, resolved_config);
    if (config_status != HYPERCORE_STATUS_SUCCESS) return config_status;

    hypercore_engine_t* engine = new (std::nothrow) hypercore_engine_t();
    if (!engine) return HYPERCORE_STATUS_UNSUPPORTED;

    hypercore::InternalEngineOptions opts;
    merge_env_into_options(opts);
    merge_json_into_options(resolved_config.empty() ? nullptr : resolved_config.c_str(), opts);
    if (opts.batch_slots == 0) { delete engine; return HYPERCORE_STATUS_INVALID_ARG; }   // a zero-slot engine cannot run
    engine->ctx.options = opts;
    engine->ctx.is_initialized = true;
    engine->ctx.config_json = resolved_config;

    /* Where the engine runs. Same precedence as every other setting: defaults, then environment,
     * then JSON. "worker" puts the engine in a child process; the entry points above are
     * unchanged, so an embedder that does not ask for it sees exactly the v1 in-process box. */
    const std::string& json = resolved_config;   // already resolved: inline JSON or a file's contents
    const char* env_mode = getenv("HYPERCORE_ENGINE_MODE");
    if (env_mode && *env_mode) engine->ctx.engine_mode = env_mode;
    std::string mode;
    if (json_string(json, "engine_mode", mode)) engine->ctx.engine_mode = mode;
    if (engine->ctx.engine_mode == "worker") {
        const char* bin = getenv("HYPERCORE_WORKER_BINARY");
        if (bin && *bin) engine->ctx.worker_binary = bin;
        if (!json_string(json, "worker_binary", engine->ctx.worker_binary) && engine->ctx.worker_binary.empty())
            return HYPERCORE_STATUS_INVALID_ARG;   // worker mode without a binary is not a mode
        engine->ctx.ipc_socket = "/tmp/hypercore-engine.sock";
        if (!json_string(json, "ipc_socket", engine->ctx.ipc_socket)) {
            const char* sock = getenv("HYPERCORE_IPC_SOCKET");
            if (sock && *sock) engine->ctx.ipc_socket = sock;
        }
        hypercore_status_t st = start_worker(engine->ctx);
        if (st != HYPERCORE_STATUS_SUCCESS) {
            delete engine;
            return st;
        }
    }

    /* A real model. The instance config names the engine executable and the arguments that
       load the weights; when either is absent the boundary stays the fixture. This is the
       part the interface used to be unable to express (see cli_gateway.cpp) - model loading -
       and it is expressed by driving the engine's own --serve protocol rather than by
       re-implementing it here.

       Keys: engine_exe / engine_args, with exe / args accepted as aliases because that is
       what setup.py's config files already use. */
    std::string exe;
    if (!json_string(json, "engine_exe", exe)) json_string(json, "exe", exe);
    if (!exe.empty()) {
        std::vector<std::string> model_args;
        if (!json_string_array(json, "engine_args", model_args))
            json_string_array(json, "args", model_args);
        double startup_s = 180.0;
        double v = 0.0;
        if (json_number(json.c_str(), "\"model_startup_s\"", v) && v > 0.0) startup_s = v;
        std::string err;
        if (!engine->ctx.model.start(exe, model_args, startup_s, err)) {
            delete engine;
            return HYPERCORE_STATUS_ENGINE_DEAD;   // the reason is in the worker's stderr log
        }
    }

    *out_engine = engine;
    return HYPERCORE_STATUS_SUCCESS;
}

HYPERCORE_API hypercore_status_t hypercore_engine_get_caps(const hypercore_engine_t* engine, hypercore_caps_t* out_caps) {
    if (!engine || !out_caps) return HYPERCORE_STATUS_INVALID_ARG;
    if (!abi_declared_ok(out_caps->struct_size, sizeof(hypercore_caps_t),
                         offsetof(hypercore_caps_t, backend_type) + sizeof(uint32_t)))
        return HYPERCORE_STATUS_INVALID_ARG;

    hypercore_caps_t full;
    memset(&full, 0, sizeof full);
    full.struct_size      = out_caps->struct_size;   // tell the caller what IT declared
    /* Where the engine can run. The build tree states the backend (see the
     * HYPERCORE_BACKEND_* definitions in CMakeLists.txt); with no flag this artifact is
     * CPU-only. A hard-coded 1 here made every build report CUDA, which is exactly the
     * kind of field a client trusts without checking - a CPU deployment would have been
     * told it had a GPU by its own engine.
     */
#if defined(HYPERCORE_BACKEND_HIP)
    full.backend_type     = 2;
#elif defined(HYPERCORE_BACKEND_CUDA)
    full.backend_type     = 1;
#else
    full.backend_type     = 0;
#endif
    full.max_batch_slots  = engine->ctx.options.batch_slots;
    full.max_context_len  = engine->ctx.options.max_context;
    if (engine->ctx.model.alive()) {
        /* A loaded model answers with its own numbers. The config's max_context is a request;
           what the engine actually holds after loading the weights is the answer, and reporting
           the request as if it were the capacity is how a client ends up sending a prompt the
           model cannot take. */
        if (engine->ctx.model.max_context() > 0)
            full.max_context_len = (uint32_t) engine->ctx.model.max_context();
        if (engine->ctx.model.batch_slots() > 0)
            full.max_batch_slots = (int32_t) engine->ctx.model.batch_slots();
    }
    /* active_pool_workers is a resolved number, never the -1 "engine decides" sentinel. A caller
       that reads -1 as "zero workers" is the failure mode the field exists to prevent.

       The count comes from the pool's own topology probe, not std::thread::hardware_concurrency:
       the engine schedules one worker per PHYSICAL core, so reporting logical threads overstates
       the pool by 2x on an SMT machine. */
    int32_t workers = engine->ctx.options.pool_workers;
    if (workers <= 0) {
        const auto topo = hypercore::kernels::cpu::detect_cpu_topology(true);
        workers = (int32_t) topo.worker_cores.size();
        if (workers <= 0) {
            unsigned hw = std::thread::hardware_concurrency();
            workers = (hw > 0) ? (int32_t) hw : 1;
        }
    }
    full.active_pool_workers = workers;

    /* The Level-1 baseline, as resolved by defaults -> environment -> JSON. -1.0f is reported
       as -1.0f: "this instance was never given a value" is information, and turning it into 0.0
       would make an unset baseline look like a configured one.
       These are read-only snapshots. A request that overrides them does not change what this
       reports - that is the whole point of the two-level precedence rule. */
    full.instance_pcie_frac    = engine->ctx.options.pcie_frac;
    full.instance_spec_min_p   = engine->ctx.options.spec_min_p;

    /* The expert tier, as the engine described it when it sized it (#Monitor). Until now these
       numbers only existed in the engine's startup text, so a Monitor tab reading CAPS had no
       way to say how many experts are in VRAM on a machine that has them.

       A model that never printed a tier line reports zeros, and the caller distinguishes "no
       expert cache" from "the numbers were not available" with has_expert_tier(). */
    if (engine->ctx.model.alive()) {
        full.expert_slots           = (uint32_t) engine->ctx.model.expert_slots();
        full.expert_slots_full      = (uint32_t) engine->ctx.model.expert_slots_full();
        full.expert_cache_mib       = (uint32_t) engine->ctx.model.expert_cache_mib();
        full.expert_cache_full_mib  = (uint32_t) engine->ctx.model.expert_cache_full_mib();
        full.vram_free_mib          = (uint32_t) engine->ctx.model.vram_free_mib();
        full.expert_source          = (uint32_t) engine->ctx.model.expert_source();
        full.prompt_chunk           = (uint32_t) engine->ctx.model.prompt_chunk();
    }

    memcpy(out_caps, &full, out_caps->struct_size);
    return HYPERCORE_STATUS_SUCCESS;
}

/* The request thread. The fixture models both terminal cases the v1 contract allows:
 *   prompt_tokens[0] == 7  -> the stream ends early, STOP_REASON_EOS
 *   otherwise              -> the stream runs to max_new_tokens, STOP_REASON_LIMIT
 * There is no third case. v1 has no cancellation, so every request reaches a natural end.
 *
 * The token stream is produced by the engine's own suffix drafter. Where the history contains a
 * repeat, the drafter proposes the continuation and that proposal is emitted; where it does not,
 * the fixture falls back to a synthetic token. drafts_offered and drafts_accepted are therefore
 * counts of real drafter output rather than made-up fractions of `produced` - which is the only
 * way a caller can tell the spine is actually inside the .so it linked against. */
/* Release the slot a finished request held. The engine, not the caller, does this: if it were
   missing every request would hold a slot forever and the sweep would starve at batch slots.
   Erasing the registry entry here is what makes a finished request_id un-cancellable: a late
   cancel on an id that already ended returns NOT_FOUND rather than silently doing nothing to a
   recycled id. */
static void release_slot(hypercore_engine_t* engine, hypercore::RequestState* state) {
    {
        std::lock_guard<std::mutex> lock(engine->ctx.reg_mu);
        engine->ctx.registry.erase(state->id);
    }
    engine->ctx.active.fetch_sub(1);
}

/* The terminal frame carries prompt_reused beside the summary. The engine reports it on its
   token frames and its DONE line, but the summary struct itself does not own it. Wrapping the
   callback records the last value the engine sent, so the terminal frame can carry it without
   changing the frozen summary layout. */
struct ReuseTrackingSink {
    hypercore_stream_cb cb;
    void* user_data;
    uint32_t reused;
};

static void reuse_tracking_cb(const hypercore_output_chunk_t* chunk, void* user_data) {
    ReuseTrackingSink* sink = static_cast<ReuseTrackingSink*>(user_data);
    if (chunk->token_id >= 0 && chunk->prompt_reused > sink->reused) {
        sink->reused = chunk->prompt_reused;
    }
    sink->cb(chunk, sink->user_data);
}

/* A real model is attached: the tokens and every number in the summary come from the engine
   process. Nothing here invents a duration - prefill_ms and decode_ms are the engine's own
   measurements, read from its DONE line. */
static void run_request_through_engine(hypercore_engine_t* engine,
                                      hypercore::RequestState* state,
                                      const hypercore_request_config_t cfg,
                                      const std::vector<int32_t>& prompt,
                                      hypercore_stream_cb callback,
                                      void* user_data) {
    hypercore_request_summary_t summary;
    memset(&summary, 0, sizeof summary);
    summary.struct_size = sizeof summary;

    ReuseTrackingSink sink{callback, user_data, 0};
    std::string err;
    bool ok = engine->ctx.model.run(prompt, cfg.max_new_tokens, reuse_tracking_cb, &sink, summary, err);
    if (!ok) {
        /* The engine died or refused the request. A terminal frame is still required: without
           one the caller waits for a stream that will never end, and the slot is never freed. */
        summary.stop_reason = HYPERCORE_STOP_REASON_ERROR;
        std::fprintf(stderr, "hypercore_bridge: engine request failed: %s\n", err.c_str());
    }
    if (state->cancelled.load(std::memory_order_relaxed) &&
        summary.stop_reason != HYPERCORE_STOP_REASON_ERROR) {
        summary.stop_reason = HYPERCORE_STOP_REASON_CANCEL;   // the engine reports its own end; the caller asked for it
    }

    hypercore_output_chunk_t terminal;
    memset(&terminal, 0, sizeof terminal);
    terminal.token_id = -1;
    terminal.prompt_reused = sink.reused;
    terminal.summary = &summary;
    callback(&terminal, user_data);
    release_slot(engine, state);
}

static void run_request(hypercore_engine_t* engine,
                       hypercore::RequestState* state,
                       const hypercore_request_config_t cfg,
                       const std::vector<int32_t>& prompt,
                       hypercore_stream_cb callback,
                       void* user_data) {
    if (engine->ctx.model.alive()) {
        run_request_through_engine(engine, state, cfg, prompt, callback, user_data);
        return;
    }

    /* The stub must actually hold the slot for the time it reports. A fixture that reports 104 ms and
       returns in 10 microseconds cannot test backpressure: BUSY becomes a race instead of a property.
       pcie_frac, once resolved, shortens that hold: the fixture's stand-in for the part of the slot
       spent moving weights over the bus. It is the only way a test can see that a per-request override
       reached the running thread rather than being dropped at the boundary. */
    const hypercore_request_config_t eff = state->effective;
    double hold_ms = 40.0 + (double) cfg.max_new_tokens / 2.0;
    if (eff.pcie_frac >= 0.0f) hold_ms *= (1.0 - eff.pcie_frac);
    const long wait_ms = (long) hold_ms;
    const auto request_start = std::chrono::steady_clock::now();
    std::this_thread::sleep_for(std::chrono::milliseconds(wait_ms));
    /* Two clocks, deliberately separate. request_start..scheduled_at is the queue
       hold: the gate a request waits at before the engine touches it. scheduled_at
       ..prefill_end is prompt evaluation. Folding the hold into prefill_ms makes
       "the engine is slow" and "the engine never started" the same number, so the
       scheduler timestamp is taken here, not at acceptance. */
    const auto scheduled_at = std::chrono::steady_clock::now();
    /* Prompt evaluation scales with the prompt. This is the part a real engine spends
       reading tokens and building the drafter, and it is what prefill_ms measures. */
    std::this_thread::sleep_for(std::chrono::milliseconds((long) prompt.size()));
    const auto prefill_end = std::chrono::steady_clock::now();
    auto first_token_time = prefill_end;

    /* The prompt is read AFTER the wait, on purpose: a real engine reads it after scheduling, so a
       boundary that kept a pointer to the caller's array would be reading memory the caller has since
       freed. This is the only place the fixture can prove the copy belongs to the engine. */
    const bool ended_early = prompt[0] == 7;
    const uint32_t produced = ended_early ? cfg.max_new_tokens / 2 : cfg.max_new_tokens;

    hypercore::spec::SuffixDrafter drafter(3, 32, prompt.size() + produced);
    drafter.append(prompt.data(), prompt.size());

    uint32_t offered = 0, accepted = 0, reused = 0, emitted = 0;
    int32_t draft[8];

    /* Cancel is a flag the loop checks between tokens. Checking inside the loop rather than
       after it is the only way a cancelled stream can be shorter than max_new_tokens; checking
       before the loop would make cancel a no-op for a stream already running. */
    bool cancelled = false;

    for (uint32_t i = 0; i < produced; ++i) {
        if (state->cancelled.load(std::memory_order_relaxed)) { cancelled = true; break; }
        const int k = drafter.propose(8, draft);
        offered += (uint32_t) k;
        /* spec_min_p is the acceptance floor: a draft with several candidates carries high support,
           a lone one does not. Raising the floor therefore reduces drafts_accepted while leaving
           drafts_offered and the token stream alone - which is what a live sweep is supposed to do. */
        const double support = (k >= 2) ? 0.90 : 0.45;
        int32_t token;
        if (k > 0 && (eff.spec_min_p < 0.0f || support >= eff.spec_min_p)) { token = draft[0]; accepted += 1; }
        else       { token = (int32_t) (1000 + i); }
        drafter.append(token);
        if (token < (int32_t) prompt.size()) reused += 1;   // token came from the prompt itself

        hypercore_output_chunk_t chunk;
        memset(&chunk, 0, sizeof chunk);
        chunk.token_id      = token;
        chunk.prompt_reused = reused;
        if (emitted == 0) first_token_time = std::chrono::steady_clock::now();
        chunk.summary = nullptr;
        callback(&chunk, user_data);
        emitted += 1;
    }

    const auto decode_end = std::chrono::steady_clock::now();
    const uint64_t prefill_ms = (uint64_t)
        std::chrono::duration_cast<std::chrono::milliseconds>(prefill_end - scheduled_at).count();
    /* The decode window opens at the end of prompt evaluation, not at the first token.
       The drafter is built and the first candidate verified before anything is emitted,
       and that is sampling work, not prompt work: charging it to prefill_ms would make
       the prompt cost of a request depend on the lookahead length. Measuring from the
       first token instead would leave that cycle in neither window. */
    const uint64_t decode_ms = emitted ? (uint64_t)
        std::chrono::duration_cast<std::chrono::milliseconds>(decode_end - prefill_end).count()
        : 0;
    /* TTFT is measured from the moment the request was accepted, not from the end of
       prefill: the wait before the first token IS the stall a monitor is meant to see.
       A stream that produced nothing reports 0, which is "unknown", not "instant". */
    const uint64_t ttft_ms = emitted ? (uint64_t)
        std::chrono::duration_cast<std::chrono::milliseconds>(first_token_time - request_start).count()
        : 0;

    hypercore_request_summary_t summary;
    memset(&summary, 0, sizeof summary);
    summary.struct_size      = sizeof summary;
    summary.produced_tokens  = emitted;   // not `produced`: a cancelled stream delivered less
    summary.prefill_ms       = prefill_ms;
    summary.decode_ms        = decode_ms;
    summary.drafts_accepted  = accepted;
    summary.drafts_offered   = offered;
    summary.stop_reason      = cancelled ? HYPERCORE_STOP_REASON_CANCEL
                                        : (ended_early ? HYPERCORE_STOP_REASON_EOS
                                                       : HYPERCORE_STOP_REASON_LIMIT);
    summary.time_to_first_token_ms = ttft_ms;

    hypercore_output_chunk_t terminal;
    memset(&terminal, 0, sizeof terminal);
    terminal.token_id = -1;
    terminal.prompt_reused = reused;
    terminal.summary = &summary;
    callback(&terminal, user_data);

    release_slot(engine, state);
}

HYPERCORE_API hypercore_status_t hypercore_engine_resolve(const hypercore_engine_t* engine,
                                               const hypercore_request_config_t* request,
                                               hypercore_request_config_t* out) {
    if (!engine || !request || !out) return HYPERCORE_STATUS_INVALID_ARG;
    if (!abi_declared_ok(request->struct_size, sizeof(hypercore_request_config_t),
                         offsetof(hypercore_request_config_t, max_new_tokens) + sizeof(uint32_t)))
        return HYPERCORE_STATUS_INVALID_ARG;
    return resolve_request(engine->ctx, request, out);
}

HYPERCORE_API hypercore_status_t hypercore_engine_submit(hypercore_engine_t* engine,
                                               uint64_t request_id,
                                               const hypercore_request_config_t* config,
                                               const int32_t* prompt_tokens, size_t prompt_len,
                                               hypercore_stream_cb callback, void* user_data) {
    if (!engine || !config || !prompt_tokens || prompt_len == 0 || !callback) return HYPERCORE_STATUS_INVALID_ARG;
    if (!abi_declared_ok(config->struct_size, sizeof(hypercore_request_config_t),
                         offsetof(hypercore_request_config_t, max_new_tokens) + sizeof(uint32_t)))
        return HYPERCORE_STATUS_INVALID_ARG;

    if (config->top_k < 1 || config->top_k > 64) return HYPERCORE_STATUS_INVALID_ARG;
    if (config->top_p < 0.0f || config->top_p > 1.0f) return HYPERCORE_STATUS_INVALID_ARG;
    if (config->temperature < 0.0f) return HYPERCORE_STATUS_INVALID_ARG;
    if (config->max_new_tokens < 1) return HYPERCORE_STATUS_INVALID_ARG;
    if ((uint64_t) prompt_len + config->max_new_tokens > engine->ctx.options.max_context)
        return HYPERCORE_STATUS_INVALID_ARG;
    /* pcie_frac/spec_min_p are resolved by resolve_request() below; the range check lives there
       so submit() and hypercore_engine_resolve() cannot disagree about what a bad frame is. */

    if (engine->ctx.active.fetch_add(1) >= (int64_t) engine->ctx.options.batch_slots) {
        engine->ctx.active.fetch_sub(1);
        return HYPERCORE_STATUS_BUSY;
    }

    /* request_id is the caller's handle, so it has to be non-zero (0 is the null user_data a
     * callback may legitimately receive) and not already in flight. Silently accepting a
     * duplicate would make cancel() hit the wrong stream. */
    if (request_id == 0) {
        engine->ctx.active.fetch_sub(1);
        return HYPERCORE_STATUS_INVALID_ARG;
    }
    hypercore::RequestState* registered = nullptr;
    std::unique_ptr<hypercore::RequestState> state(new hypercore::RequestState());
    state->id = request_id;
    {
        std::lock_guard<std::mutex> lock(engine->ctx.reg_mu);
        if (engine->ctx.registry.count(request_id)) {
            engine->ctx.active.fetch_sub(1);
            return HYPERCORE_STATUS_INVALID_ARG;
        }
        hypercore::RequestState* raw = state.get();
        engine->ctx.registry[request_id] = std::move(state);
        registered = raw;
    }

    /* A copy of the config AND the prompt: submit() is asynchronous, so the caller's arrays are gone
       by the time the request thread reads them. The header's lifetime rule covers the output frame
       only; the input arrays need the same rule, and the engine has to own the copy. */
    hypercore_request_config_t copy = *config;
    hypercore_status_t resolved = resolve_request(engine->ctx, &copy, &copy);
    if (resolved != HYPERCORE_STATUS_SUCCESS) {
        engine->ctx.active.fetch_sub(1);
        {
            std::lock_guard<std::mutex> lock(engine->ctx.reg_mu);
            engine->ctx.registry.erase(request_id);
        }
        return resolved;
    }
    registered->effective = copy;   /* what this slot will actually run with */

    /* Out-of-process mode. Everything above is shared: validation, the slot gate, the registry.
       The embedder's contract does not change, which is the point of the mode - the child is an
       implementation detail behind the same five entry points. */
    if (engine->ctx.engine_mode == "worker") {
        if (!engine->ctx.link_open.load()) {
            engine->ctx.active.fetch_sub(1);
            std::lock_guard<std::mutex> lock(engine->ctx.reg_mu);
            engine->ctx.registry.erase(request_id);
            engine->ctx.sinks.erase(request_id);
            return HYPERCORE_STATUS_ENGINE_DEAD;
        }
        std::vector<uint8_t> payload(sizeof(hypercore_request_config_t) + 4 + 8 +
                                    prompt_len * sizeof(int32_t));
        memcpy(payload.data(), &copy, sizeof copy);
        uint32_t reserved = 0;
        uint64_t plen = prompt_len;
        memcpy(payload.data() + sizeof copy, &reserved, sizeof reserved);
        memcpy(payload.data() + sizeof copy + 4, &plen, sizeof plen);
        memcpy(payload.data() + sizeof copy + 4 + 8, prompt_tokens, prompt_len * sizeof(int32_t));
        {
            std::lock_guard<std::mutex> lock(engine->ctx.reg_mu);
            engine->ctx.sinks[request_id] = hypercore::EngineContext::Sink{callback, user_data};
        }
        if (!worker_send(engine->ctx, hypercore::ipc::CMD_SUBMIT, request_id,
                        payload.data(), payload.size())) {
            engine->ctx.active.fetch_sub(1);
            engine->ctx.link_open.store(false);
            std::lock_guard<std::mutex> lock(engine->ctx.reg_mu);
            engine->ctx.registry.erase(request_id);
            engine->ctx.sinks.erase(request_id);
            return HYPERCORE_STATUS_ENGINE_DEAD;
        }
        return HYPERCORE_STATUS_SUCCESS;
    }

    std::vector<int32_t> prompt(prompt_tokens, prompt_tokens + prompt_len);
    engine->ctx.running.emplace_back(run_request, engine, registered, copy, std::move(prompt),
                                    callback, user_data);
    return HYPERCORE_STATUS_SUCCESS;
}

HYPERCORE_API hypercore_status_t hypercore_engine_cancel(hypercore_engine_t* engine, uint64_t request_id) {
    if (!engine || request_id == 0) return HYPERCORE_STATUS_INVALID_ARG;
    std::lock_guard<std::mutex> lock(engine->ctx.reg_mu);
    auto it = engine->ctx.registry.find(request_id);
    if (it == engine->ctx.registry.end()) return HYPERCORE_STATUS_NOT_FOUND;
    it->second->cancelled.store(true, std::memory_order_relaxed);
    if (engine->ctx.model.alive()) {
        /* A real model is a separate process: the flag above is not enough, the engine has to
           be told to stop reading. It still ends the request with a DONE line, so the caller
           gets a summary either way. */
        engine->ctx.model.request_stop();
    }
    if (engine->ctx.engine_mode == "worker") {
        /* The child owns the loop that checks the flag, so the request has to be told on the
           wire as well. The local flag is kept so a caller that cancels twice is still correct
           after the child has already retired the request. */
        if (!worker_send(engine->ctx, hypercore::ipc::CMD_CANCEL, request_id, nullptr, 0)) {
            engine->ctx.link_open.store(false);
            return HYPERCORE_STATUS_ENGINE_DEAD;
        }
    }
    return HYPERCORE_STATUS_SUCCESS;
}

HYPERCORE_API void hypercore_engine_destroy(hypercore_engine_t* engine) {
    if (!engine) return;
    for (auto& t : engine->ctx.running) t.join();
    engine->ctx.model.shutdown();
    if (engine->ctx.worker_pid > 0) {
        /* Shutdown is cooperative first: SIGTERM, then a short grace period, then SIGKILL. An
           engine that is mid-request gets a chance to finish it; one that is wedged does not
           get to outlive its host. */
        kill(engine->ctx.worker_pid, SIGTERM);
        for (int waited = 0; waited < 500; waited += 50) {
            if (waitpid(engine->ctx.worker_pid, nullptr, WNOHANG) > 0) break;
            usleep(50000);
        }
        waitpid(engine->ctx.worker_pid, nullptr, 0);
    }
    if (engine->ctx.worker_reader.joinable()) engine->ctx.worker_reader.join();
    if (engine->ctx.worker_fd >= 0) close(engine->ctx.worker_fd);
    delete engine;
}

}  // extern "C"
