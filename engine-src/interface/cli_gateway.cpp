// src/interface/cli_gateway.cpp - the boundary used as a CLI, through the boundary only.
//
// This is the shape decision 2 points at: an executable that reaches the engine exclusively
// through hypercore_engine_init / get_caps / submit / destroy. It is not a replacement for
// src/program/generate.cpp - it is the proof that the single gateway is linkable, and the
// clearest statement of what the boundary still cannot express (model loading, conversation
// state, tool calls, the process-level CUDA/HIP setup that generate.cpp does before main's
// argument loop).
//
// Build: cmake --build build-shared --target hypercore-cli
// Usage: ./hypercore-cli --prompt "5 6 7 5 6 7" --max-new 32
#include "hypercore/hypercore_api.h"

#include <cstdio>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

namespace {

struct Flags {
    std::string json;
    std::vector<int32_t> prompt;
    uint32_t max_new = 32;
    float temperature = 0.0f;
    float top_p = 1.0f;
    uint32_t top_k = 1;
};

/* Command line -> the JSON block the boundary takes. Deliberately minimal: the point is that
   every setting arrives through one parser, not that this parser is the whole CLI. */
std::string flags_to_json(const Flags& f) { return f.json.empty() ? std::string("{}") : f.json; }

void usage(const char* prog) {
    std::fprintf(stderr,
                 "usage: %s --prompt \"t1 t2 ...\" [--max-new N] [--config JSON]\n", prog);
}

/* The stream callback is the only way tokens leave the engine here, which is exactly the
   contract an external embedder has to work with. */
void print_chunk(const hypercore_output_chunk_t* chunk, void* user_data) {
    auto* seen = static_cast<uint64_t*>(user_data);
    if (chunk->token_id < 0) {
        if (chunk->summary) {
            std::printf("\n[summary] produced=%u decode_ms=%llu drafts=%u/%u stop=%d\n",
                        chunk->summary->produced_tokens,
                        (unsigned long long) chunk->summary->decode_ms,
                        chunk->summary->drafts_accepted,
                        chunk->summary->drafts_offered,
                        (int) chunk->summary->stop_reason);
        }
        return;
    }
    ++*seen;
    std::printf("%d ", chunk->token_id);
}

}  // namespace

int main(int argc, char** argv) {
    std::setvbuf(stdout, nullptr, _IONBF, 0);

    Flags f;
    for (int i = 1; i < argc; ++i) {
        const char* a = argv[i];
        if (std::strcmp(a, "--prompt") == 0 && i + 1 < argc) {
            std::string s = argv[++i];
            std::size_t j = 0;
            while (j < s.size()) {
                while (j < s.size() && (s[j] == ' ' || s[j] == ',')) ++j;
                if (j >= s.size()) break;
                f.prompt.push_back((int32_t) std::strtol(s.c_str() + j, nullptr, 10));
                while (j < s.size() && s[j] != ' ' && s[j] != ',') ++j;
            }
        } else if (std::strcmp(a, "--max-new") == 0 && i + 1 < argc) {
            f.max_new = (uint32_t) std::strtoul(argv[++i], nullptr, 10);
        } else if (std::strcmp(a, "--config") == 0 && i + 1 < argc) {
            f.json = argv[++i];
        } else if (std::strcmp(a, "--help") == 0) {
            usage(argv[0]);
            return 0;
        } else {
            std::fprintf(stderr, "unknown argument: %s\n", a);
            usage(argv[0]);
            return 2;
        }
    }
    if (f.prompt.empty()) {
        std::fprintf(stderr, "no prompt tokens given\n");
        usage(argv[0]);
        return 2;
    }

    hypercore_engine_t* engine = nullptr;
    const std::string json = flags_to_json(f);
    hypercore_status_t st = hypercore_engine_init(&engine, json.c_str());
    if (st != HYPERCORE_STATUS_SUCCESS) {
        std::fprintf(stderr, "Fatal: boundary initialization failed (status=%d)\n", (int) st);
        return 1;
    }

    hypercore_caps_t caps;
    std::memset(&caps, 0, sizeof caps);
    caps.struct_size = sizeof caps;
    if (hypercore_engine_get_caps(engine, &caps) != HYPERCORE_STATUS_SUCCESS) {
        std::fprintf(stderr, "Fatal: caps unavailable\n");
        hypercore_engine_destroy(engine);
        return 1;
    }
    std::printf("engine: batch_slots=%u max_context=%u pool_workers=%d\n",
                caps.max_batch_slots, caps.max_context_len, caps.active_pool_workers);

    if ((uint64_t) f.prompt.size() + f.max_new > caps.max_context_len) {
        std::fprintf(stderr, "prompt + max_new exceeds the reported context\n");
        hypercore_engine_destroy(engine);
        return 2;
    }

    hypercore_request_config_t cfg;
    std::memset(&cfg, 0, sizeof cfg);
    cfg.struct_size = sizeof cfg;
    cfg.temperature = f.temperature;
    cfg.top_p = f.top_p;
    cfg.top_k = f.top_k;
    cfg.max_new_tokens = f.max_new;
    cfg.pcie_frac = -1.0f;   // -1 is the "use the calibrated default" sentinel
    cfg.spec_min_p = -1.0f;

    uint64_t emitted = 0;
    st = hypercore_engine_submit(engine, 1, &cfg, f.prompt.data(), f.prompt.size(), print_chunk, &emitted);
    if (st == HYPERCORE_STATUS_BUSY) {
        std::fprintf(stderr, "engine is at its declared slot limit; retry\n");
        hypercore_engine_destroy(engine);
        return 3;
    }
    if (st != HYPERCORE_STATUS_SUCCESS) {
        std::fprintf(stderr, "submit failed (status=%d)\n", (int) st);
        hypercore_engine_destroy(engine);
        return 1;
    }

    /* submit() is asynchronous: the callback fires on the request's own thread. A real host
       keeps its event loop running; this one has to wait for the terminal frame, otherwise
       destroy() joins the thread mid-stream. */
    std::this_thread::sleep_for(std::chrono::milliseconds(300));

    hypercore_engine_destroy(engine);
    return emitted > 0 ? 0 : 1;
}
