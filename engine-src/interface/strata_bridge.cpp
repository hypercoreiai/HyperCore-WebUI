/* engine-src/interface/strata_bridge.cpp - the five API entry points, on a Strata engine.
 *
 * This is the whole worker: it reads the run config, starts "strata --serve" as a child
 * process (engine_process.cpp), and answers the framed requests the web server sends.
 * It needs nothing from the engine source - only the binary and its --serve protocol -
 * which is why the worker builds against a stock Strata checkout.
 *
 * The point of the boundary is the blast radius: an engine abort takes down the worker,
 * not the web server, and the engine's threads never touch the Python GIL.
 */
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <new>
#include <string>
#include <vector>

#include "hypercore/hypercore_api.h"
#include "engine_process.h"   // drives the engine's --serve protocol as a child process

namespace {

/* The config is the run-config JSON the server already writes: {"exe": ..., "args": [...]}.
 * A bare string starting with { is an inline object; anything else is a path to one. */
std::string read_config(const char* config) {
    if (!config || !*config) return "";
    if (config[0] == '{') return std::string(config);
    FILE* f = fopen(config, "rb");
    if (!f) return "";
    std::string out;
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) out.append(buf, n);
    fclose(f);
    return out;
}

/* Enough JSON to read a run config. These files are written by setup.py and by this
 * project, not pasted by a human, so a small reader is enough and is easier to audit
 * than a dependency. */
bool json_string(const std::string& j, const std::string& key, std::string& out) {
    const std::string needle = "\"" + key + "\"";
    size_t at = j.find(needle);
    if (at == std::string::npos) return false;
    at = j.find(':', at + needle.size());
    if (at == std::string::npos) return false;
    at = j.find('"', at + 1);
    if (at == std::string::npos) return false;
    std::string v;
    for (size_t i = at + 1; i < j.size(); ++i) {
        if (j[i] == '\\' && i + 1 < j.size()) { v += j[++i]; continue; }
        if (j[i] == '"') { out = v; return true; }
        v += j[i];
    }
    return false;
}

std::vector<std::string> json_string_array(const std::string& j, const std::string& key) {
    std::vector<std::string> out;
    const std::string needle = "\"" + key + "\"";
    size_t at = j.find(needle);
    if (at == std::string::npos) return out;
    at = j.find('[', at + needle.size());
    if (at == std::string::npos) return out;
    size_t end = j.find(']', at);
    if (end == std::string::npos) return out;
    std::string body = j.substr(at + 1, end - at - 1);
    size_t i = 0;
    while (i < body.size()) {
        size_t q = body.find('"', i);
        if (q == std::string::npos) break;
        std::string v;
        size_t k = q + 1;
        for (; k < body.size(); ++k) {
            if (body[k] == '\\' && k + 1 < body.size()) { v += body[++k]; continue; }
            if (body[k] == '"') { ++k; break; }
            v += body[k];
        }
        out.push_back(v);
        i = k;
    }
    return out;
}

struct Engine {
    hypercore::iface::EngineProcess process;
    std::string config_json;
    uint64_t next_request_id = 1;
};

}  // namespace

HYPERCORE_API hypercore_status_t hypercore_engine_init(hypercore_engine_t** out_engine,
                                                      const char* config) {
    if (!out_engine) return HYPERCORE_STATUS_INVALID_ARG;

    std::string json = read_config(config);
    if (config && *config && json.empty()) return HYPERCORE_STATUS_INVALID_ARG;

    std::string exe;
    if (!json_string(json, "exe", exe)) json_string(json, "engine_exe", exe);
    if (exe.empty()) {
        const char* env = getenv("STRATA_ENGINE");
        if (env && *env) exe = env;
    }
    if (exe.empty()) return HYPERCORE_STATUS_INVALID_ARG;

    /* The same aliasing as the executable above: the server's worker config writes
       engine_args, a run config written by setup.py writes args. Reading only one of the
       two gives an empty argv, and the engine then fails at its own argument check rather
       than at a missing model path - which is why an engine that was handed nothing reads
       as a memory problem. */
    std::vector<std::string> args = json_string_array(json, "engine_args");
    if (args.empty()) args = json_string_array(json, "args");
    args.push_back("--serve");

    double startup_s = 300.0;
    const char* env_startup = getenv("HYPERCORE_STARTUP_TIMEOUT_S");
    if (env_startup && *env_startup) startup_s = strtod(env_startup, nullptr);

    Engine* engine = new (std::nothrow) Engine();
    if (!engine) return HYPERCORE_STATUS_UNSUPPORTED;
    engine->config_json = json;

    std::string err;
    if (!engine->process.start(exe, args, startup_s, err)) {
        delete engine;
        fprintf(stderr, "[hypercore-worker] could not start the engine: %s\n", err.c_str());
        return HYPERCORE_STATUS_ENGINE_DEAD;
    }

    *out_engine = reinterpret_cast<hypercore_engine_t*>(engine);
    return HYPERCORE_STATUS_SUCCESS;
}

HYPERCORE_API hypercore_status_t hypercore_engine_get_caps(const hypercore_engine_t* engine,
                                                           hypercore_caps_t* out_caps) {
    if (!engine || !out_caps) return HYPERCORE_STATUS_INVALID_ARG;
    Engine* e = reinterpret_cast<Engine*>(const_cast<hypercore_engine_t*>(engine));
    memset(out_caps, 0, sizeof(*out_caps));
    out_caps->struct_size = sizeof(hypercore_caps_t);
    out_caps->max_context_len = (uint32_t)e->process.max_context();
    out_caps->max_batch_slots = (uint32_t)e->process.batch_slots();
    out_caps->expert_slots = (uint32_t)e->process.expert_slots();
    out_caps->expert_slots_full = (uint32_t)e->process.expert_slots_full();
    out_caps->expert_cache_mib = (uint32_t)e->process.expert_cache_mib();
    out_caps->expert_cache_full_mib = (uint32_t)e->process.expert_cache_full_mib();
    out_caps->vram_free_mib = (uint32_t)e->process.vram_free_mib();
    out_caps->expert_source = (uint32_t)e->process.expert_source();
    out_caps->prompt_chunk = (uint32_t)e->process.prompt_chunk();
    return HYPERCORE_STATUS_SUCCESS;
}

/* The request config is passed through unchanged. Sampling and the PCIe fraction are
 * applied by the engine itself from its own command line; the boundary only has to
 * hand them over. */
HYPERCORE_API hypercore_status_t hypercore_engine_resolve(const hypercore_engine_t* engine,
                                                          const hypercore_request_config_t* request,
                                                          hypercore_request_config_t* out) {
    if (!engine || !request || !out) return HYPERCORE_STATUS_INVALID_ARG;
    Engine* e = reinterpret_cast<Engine*>(const_cast<hypercore_engine_t*>(engine));
    if (!e->process.alive()) return HYPERCORE_STATUS_ENGINE_DEAD;
    *out = *request;
    if (out->max_new_tokens == 0) out->max_new_tokens = 1;
    return HYPERCORE_STATUS_SUCCESS;
}

HYPERCORE_API hypercore_status_t hypercore_engine_submit(hypercore_engine_t* engine,
                                                         uint64_t request_id,
                                                         const hypercore_request_config_t* config,
                                                         const int32_t* prompt_tokens,
                                                         size_t prompt_len,
                                                         hypercore_stream_cb callback,
                                                         void* user_data) {
    if (!engine || !config || !prompt_tokens || !callback) return HYPERCORE_STATUS_INVALID_ARG;
    Engine* e = reinterpret_cast<Engine*>(engine);
    if (!e->process.alive()) return HYPERCORE_STATUS_ENGINE_DEAD;

    std::vector<int32_t> prompt(prompt_tokens, prompt_tokens + prompt_len);
    hypercore_request_summary_t summary = {};
    std::string err;
    bool ok = e->process.run(prompt, config->max_new_tokens, callback, user_data, summary, err);
    if (!ok) fprintf(stderr, "[hypercore-worker] request %llu failed: %s\n",
                    (unsigned long long)request_id, err.c_str());
    return ok ? HYPERCORE_STATUS_SUCCESS : HYPERCORE_STATUS_ENGINE_DEAD;
}

HYPERCORE_API hypercore_status_t hypercore_engine_cancel(hypercore_engine_t* engine,
                                                        uint64_t request_id) {
    if (!engine) return HYPERCORE_STATUS_INVALID_ARG;
    Engine* e = reinterpret_cast<Engine*>(engine);
    if (!e->process.alive()) return HYPERCORE_STATUS_ENGINE_DEAD;
    e->process.request_stop();
    (void)request_id;
    return HYPERCORE_STATUS_SUCCESS;
}

HYPERCORE_API void hypercore_engine_destroy(hypercore_engine_t* engine) {
    if (!engine) return;
    Engine* e = reinterpret_cast<Engine*>(engine);
    e->process.shutdown();
    delete e;
}
