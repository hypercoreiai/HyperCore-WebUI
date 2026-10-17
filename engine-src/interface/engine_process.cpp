// src/interface/engine_process.cpp - see engine_process.h for the design notes.
#include "engine_process.h"

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <unistd.h>
#include <csignal>
#include <sys/wait.h>

namespace hypercore::iface {
namespace {

constexpr double DEFAULT_STARTUP_TIMEOUT_S = 180.0;

/* A summary field the engine did not report. 0 would read as "zero hits", which is a
   measurement; this is the absence of one, and the reader is expected to show it as such. */
constexpr uint32_t NOT_MEASURED = 0xFFFFFFFFu;

std::vector<std::string> split_tokens(const std::string& line) {
    std::vector<std::string> out;
    size_t i = 0;
    while (i < line.size()) {
        while (i < line.size() && (line[i] == ' ' || line[i] == '\t')) ++i;
        size_t j = i;
        while (j < line.size() && line[j] != ' ' && line[j] != '\t' && line[j] != '\r' && line[j] != '\n') ++j;
        if (j > i) out.push_back(line.substr(i, j - i));
        i = j;
    }
    return out;
}

long long field_ll(const std::vector<std::string>& f, size_t i, long long fallback) {
    if (i >= f.size()) return fallback;
    char* end = nullptr;
    long long v = std::strtoll(f[i].c_str(), &end, 10);
    if (end == f[i].c_str()) return fallback;   // not a number (e.g. a float field read as int)
    return v;
}

double field_d(const std::vector<std::string>& f, size_t i, double fallback) {
    if (i >= f.size()) return fallback;
    char* end = nullptr;
    double v = std::strtod(f[i].c_str(), &end);
    if (end == f[i].c_str()) return fallback;
    return v;
}

/* The engine's own words for how a stream ended, mapped to the contract's enum. Anything
   unrecognised is an error rather than a silent LIMIT: a stop reason the caller misreads is
   worse than one it can see. */
hypercore_stop_reason_t map_finish(const std::string& finish) {
    if (finish == "stop")   return HYPERCORE_STOP_REASON_EOS;
    if (finish == "length") return HYPERCORE_STOP_REASON_LIMIT;
    if (finish == "cancel") return HYPERCORE_STOP_REASON_CANCEL;
    return HYPERCORE_STOP_REASON_ERROR;
}

}  // namespace

bool EngineProcess::read_line(std::string& out) {
    if (!out_) return false;
    char buf[4096];
    if (!std::fgets(buf, sizeof buf, out_)) return false;
    out.assign(buf);
    while (!out.empty() && (out.back() == '\n' || out.back() == '\r')) out.pop_back();
    return true;
}

/* The engine's self-description lines: `INFO key=value ...` at startup and `VRAM key=value ...`
   whenever the tier is resized. Both are key=value, so one parser covers them; anything that is
   not one of those two prefixes is a request line and is left alone.

   Captured on every read, not only before READY: the VRAM line comes from a resize that happens
   during a run, and a capability reply asked after that must report the tier the engine is
   actually on, not the one it started with. */
void EngineProcess::note_config_line(const std::string& line) {
    if (line.rfind("INFO ", 0) != 0 && line.rfind("VRAM ", 0) != 0) return;
    for (const std::string& tok : split_tokens(line)) {
        const size_t eq = tok.find('=');
        if (eq == std::string::npos) continue;
        const std::string key = tok.substr(0, eq);
        long long v = field_ll({tok.substr(eq + 1)}, 0, 0);
        if (key == "expert_slots")               expert_slots_ = v;
        else if (key == "expert_slots_full")     expert_slots_full_ = v;
        else if (key == "expert_cache_mib")      expert_cache_mib_ = v;
        else if (key == "expert_cache_full_mib") expert_cache_full_mib_ = v;
        else if (key == "expert_source")         expert_source_ = v;
        else if (key == "vram_free_mib")         vram_free_mib_ = v;
        else if (key == "prompt_chunk")          prompt_chunk_ = v;
        else continue;
        expert_seen_ = true;
    }
}

bool EngineProcess::start(const std::string& exe, const std::vector<std::string>& args,
                         double startup_timeout_s, std::string& err) {
    if (exe.empty()) { err = "engine_process: no executable named"; return false; }
    if (alive_) shutdown();

    int to_child[2], from_child[2];
    if (pipe(to_child) != 0 || pipe(from_child) != 0) {
        err = "engine_process: pipe() failed";
        return false;
    }

    pid_t pid = fork();
    if (pid < 0) {
        close(to_child[0]); close(to_child[1]); close(from_child[0]); close(from_child[1]);
        err = "engine_process: fork() failed";
        return false;
    }

    if (pid == 0) {
        // The child: stdin is the request line, stdout is the token stream. stderr stays
        // attached to the worker's, so the engine's diagnostics land in the supervisor's log.
        dup2(to_child[0], 0);
        dup2(from_child[1], 1);
        close(to_child[0]); close(to_child[1]); close(from_child[0]); close(from_child[1]);

        std::vector<std::string> argv_storage;
        argv_storage.push_back(exe);
        argv_storage.push_back("--serve");
        for (const std::string& a : args) argv_storage.push_back(a);

        std::vector<char*> argv;
        argv.reserve(argv_storage.size() + 1);
        for (std::string& s : argv_storage) argv.push_back(s.data());
        argv.push_back(nullptr);

        execv(exe.c_str(), argv.data());
        _exit(127);   // execv only returns on failure
    }

    close(to_child[0]);
    close(from_child[1]);
    in_  = fdopen(to_child[1], "w");
    out_ = fdopen(from_child[0], "r");
    if (!in_ || !out_) {
        close(to_child[1]); close(from_child[0]);
        kill(pid, SIGTERM);
        err = "engine_process: could not open the engine's pipes";
        return false;
    }
    pid_ = pid;

    /* READY is the end of model loading; everything before it is the engine setting up the
       weights, the arena and the VRAM tier. A startup that takes a minute or two is normal,
       so the wait is long by design - the supervisor's own watchdog is what catches an engine
       that goes quiet mid-request. */
    const double limit = startup_timeout_s > 0.0 ? startup_timeout_s : DEFAULT_STARTUP_TIMEOUT_S;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::duration<double>(limit);
    while (std::chrono::steady_clock::now() < deadline) {
        std::string line;
        if (!read_line(line)) {
            int status = 0;
            waitpid(pid_, &status, 0);
            alive_ = false;
            err = "engine_process: the engine exited before it was ready (exit " +
                  std::to_string(WIFEXITED(status) ? WEXITSTATUS(status) : -1) + ")";
            return false;
        }
        note_config_line(line);
        if (line.rfind("READY ", 0) == 0) {
            auto f = split_tokens(line);
            max_context_ = field_ll(f, 1, 0);
            alive_ = true;
            return true;
        }
        if (line.rfind("INFO ", 0) == 0) {
            for (const std::string& tok : split_tokens(line)) {
                size_t eq = tok.find('=');
                if (eq == std::string::npos) continue;
                const std::string key = tok.substr(0, eq), val = tok.substr(eq + 1);
                if (key == "model")        version_ = val;
                if (key == "batch_slots")  batch_slots_ = field_ll({val}, 0, 0);
                if (key == "max_context")  max_context_ = field_ll({val}, 0, max_context_);
            }
        }
        if (line.rfind("ERR ", 0) == 0) {
            last_err_ = line.substr(4);
            kill(pid_, SIGTERM);
            waitpid(pid_, nullptr, 0);
            alive_ = false;
            err = "engine_process: the engine refused to start: " + last_err_;
            return false;
        }
    }

    kill(pid_, SIGTERM);
    waitpid(pid_, nullptr, 0);
    alive_ = false;
    err = "engine_process: the engine did not report READY within " + std::to_string((int) limit) + "s";
    return false;
}

bool EngineProcess::run(const std::vector<int32_t>& prompt, uint32_t max_new,
                       hypercore_stream_cb callback, void* user_data,
                       hypercore_request_summary_t& out, std::string& err) {
    std::lock_guard<std::mutex> lock(req_mu_);

    if (!alive_) { err = "engine_process: the engine is not running"; return false; }

    std::string line = "GEN " + std::to_string(max_new);
    line += " ";
    for (size_t i = 0; i < prompt.size(); ++i) {
        if (i) line += ",";
        line += std::to_string(prompt[i]);
    }
    line += "\n";
    if (std::fwrite(line.data(), 1, line.size(), in_) != line.size()) {
        alive_ = false;
        err = "engine_process: could not write the request to the engine";
        return false;
    }
    std::fflush(in_);

    const auto submitted = std::chrono::steady_clock::now();
    auto first_token = std::chrono::steady_clock::now();
    bool saw_first_token = false;

    uint32_t emitted = 0;
    uint32_t reused = 0;
    uint64_t prefill_ms = 0, decode_ms = 0;
    uint32_t accepted = 0, offered = 0;
    /* The expert-tier accounting. NOT_MEASURED is what an engine that does not report a field
       leaves behind; the reader turns it into None so a Monitor cell shows "-" instead of 0%. */
    uint32_t hits = NOT_MEASURED, lookups = NOT_MEASURED, offloaded = NOT_MEASURED;
    uint32_t read_n = NOT_MEASURED, ram_blobs = NOT_MEASURED, file_blobs = NOT_MEASURED, file_kb = 0;
    hypercore_stop_reason_t reason = HYPERCORE_STOP_REASON_UNSPECIFIED;
    bool done = false;

    while (!done) {
        std::string l;
        if (!read_line(l)) {
            alive_ = false;
            err = "engine_process: the engine closed its output mid-request";
            return false;
        }
        if (l.empty()) continue;
        note_config_line(l);

        if (l.rfind("T ", 0) == 0) {
            const int32_t token = (int32_t) field_ll(split_tokens(l), 1, -1);
            if (!saw_first_token) {
                first_token = std::chrono::steady_clock::now();
                saw_first_token = true;
            }
            hypercore_output_chunk_t chunk;
            memset(&chunk, 0, sizeof chunk);
            chunk.token_id = token;
            chunk.prompt_reused = reused;
            chunk.summary = nullptr;
            callback(&chunk, user_data);
            ++emitted;
        } else if (l.rfind("RESUME ", 0) == 0 || l.rfind("REUSED ", 0) == 0) {
            // The engine did not read these prompt tokens again; the conversation cache held them.
            reused = (uint32_t) field_ll(split_tokens(l), 1, (long long) reused);
        } else if (l.rfind("ERR ", 0) == 0) {
            last_err_ = l.substr(4);
            reason = HYPERCORE_STOP_REASON_ERROR;
            done = true;
        } else if (l.rfind("DONE ", 0) == 0) {
            /* DONE <generated> <prompt> <prompt_ms> <decode_ms> <finish> <drafts_accepted>
                   <drafts_offered> <reused> <hits> <lookups> <ram blobs> <file blobs>
                   <file MB> <prompt tokens read> <offloaded> [chain fields]
                   - the engine's own accounting. The fields after <reused> are appended, so an
                   older engine stops at <reused>; those numbers are then NOT_MEASURED rather
                   than 0, because a zero is a measurement and a missing field is not. */
            auto f = split_tokens(l);
            emitted  = (uint32_t) field_ll(f, 1, (long long) emitted);
            prefill_ms = (uint64_t) field_d(f, 3, 0.0);
            decode_ms  = (uint64_t) field_d(f, 4, 0.0);
            reason = map_finish(f.size() > 5 ? f[5] : "");
            accepted = (uint32_t) field_ll(f, 6, 0);
            offered  = (uint32_t) field_ll(f, 7, 0);
            reused   = (uint32_t) field_ll(f, 8, (long long) reused);
            if (f.size() > 10) {
                hits     = (uint32_t) field_ll(f, 9, (long long) NOT_MEASURED);
                lookups  = (uint32_t) field_ll(f, 10, (long long) NOT_MEASURED);
                ram_blobs = (uint32_t) field_ll(f, 11, (long long) NOT_MEASURED);
                file_blobs = (uint32_t) field_ll(f, 12, (long long) NOT_MEASURED);
                file_kb    = (uint32_t) (field_d(f, 13, 0.0) * 1000.0);
                read_n     = (uint32_t) field_ll(f, 14, (long long) NOT_MEASURED);
                offloaded  = (uint32_t) field_ll(f, 15, (long long) NOT_MEASURED);
            }
            done = true;
        }
        /* PP, YIELDED and the progress lines are the engine's own telemetry; the summary is
           complete at DONE, so they are not needed here. */
    }

    memset(&out, 0, sizeof out);
    out.struct_size = sizeof out;
    out.produced_tokens = emitted;
    out.prefill_ms = prefill_ms;
    out.decode_ms = decode_ms;
    out.drafts_accepted = accepted;
    out.drafts_offered = offered;
    out.stop_reason = reason;
    out.cache_hits = hits;
    out.cache_lookups = lookups;
    out.offloaded = offloaded;
    out.prompt_read = read_n;
    out.ram_blobs = ram_blobs;
    out.file_blobs = file_blobs;
    out.file_kb = file_kb;
    /* Time to the first token is measured here, at the boundary, because the engine reports
       the prompt and the decode but not the gap between them. */
    out.time_to_first_token_ms = saw_first_token
        ? (uint64_t) std::chrono::duration_cast<std::chrono::milliseconds>(first_token - submitted).count()
        : 0;

    if (reason == HYPERCORE_STOP_REASON_ERROR) return false;
    return true;
}

void EngineProcess::request_stop() {
    if (!alive_ || !in_) return;
    static const char stop[] = "STOP\n";
    std::fwrite(stop, 1, sizeof stop - 1, in_);
    std::fflush(in_);
}

void EngineProcess::shutdown() {
    /* Only write QUIT if the engine is still there. After it has exited its stdin is closed,
       and a write to a closed pipe is SIGPIPE - which is how the worker used to die when a
       model failed to load, hiding the real error behind an exit code of 141. */
    if (in_ && alive_) {
        static const char quit[] = "QUIT\n";
        std::fwrite(quit, 1, sizeof quit - 1, in_);
        std::fflush(in_);
    }
    if (in_) { std::fclose(in_); in_ = nullptr; }
    if (pid_ > 0) {
        int status = 0;
        if (waitpid(pid_, &status, 0) < 0) kill(pid_, SIGTERM);
        pid_ = -1;
    }
    if (out_) { std::fclose(out_); out_ = nullptr; }
    alive_ = false;
}

EngineProcess::~EngineProcess() { shutdown(); }

}  // namespace hypercore::iface
