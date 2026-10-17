/*
 * hypercore/hypercore_api.h - the public C contract of libhypercore_api.so.
 *
 * Everything above this header is internal. The spine (pool, experts, suffix drafter) is
 * linked into the artifact with whole-archive and hidden visibility, so the only symbols an
 * embedder can reach are the entry points below.
 *
 * Two entry points are shipped, and both are this contract:
 *   in-process  - link libhypercore_api.so and call the functions below directly.
 *   out-of-process - run hypercore-worker and speak the frames in src/interface/ipc_frame.h
 *                    over a UNIX domain socket. The worker calls nothing in this header that
 *                    is not listed here; the frame types are the same values as the entry
 *                    points, and the terminal frame carries a hypercore_request_summary_t.
 * The out-of-process form is the one that survives an engine abort: the crash takes the child,
 * not the host.
 *
 * Forward compatibility rule, applied to every struct here: a caller declares how much of a
 * struct it knows by setting struct_size. The engine writes only the bytes the caller declared.
 * A field added later is therefore invisible to an older caller rather than a layout break.
 */
#ifndef HYPERCORE_HYPERCORE_API_H
#define HYPERCORE_HYPERCORE_API_H

#include <stdint.h>
#include <stddef.h>

#if defined(_WIN32) || defined(__CYGWIN__)
    #ifdef HYPERCORE_BUILD_DLL
        #define HYPERCORE_API __declspec(dllexport)
    #else
        #define HYPERCORE_API __declspec(dllimport)
    #endif
#else
    #if __GNUC__ >= 4
        #define HYPERCORE_API __attribute__((visibility("default")))
    #else
        #define HYPERCORE_API
    #endif
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    HYPERCORE_STATUS_SUCCESS     = 0,
    HYPERCORE_STATUS_INVALID_ARG = -1,
    HYPERCORE_STATUS_BUSY        = -2,  /* every batch slot is in use; nothing was queued */
    HYPERCORE_STATUS_UNSUPPORTED = -3,  /* the request needs a capability this build lacks */
    HYPERCORE_STATUS_NOT_FOUND   = -4,  /* request_id is not an active request */
    HYPERCORE_STATUS_ENGINE_DEAD = -5   /* the engine process is gone (out-of-process mode only):
                                          the child exited or the link closed. Distinct from BUSY:
                                          BUSY means retry, ENGINE_DEAD means re-init. */
} hypercore_status_t;

/* How a stream ended. 0 is not a valid outcome: it means the summary block was never
 * written, which is what an older caller sees when struct_size is smaller than the
 * summary the engine produced.
 *
 * Note for embedders pinned to an earlier draft: CANCEL was absent in the first frozen
 * header and ERROR held value 3. Any artifact built against that draft must be rebuilt. */
typedef enum {
    HYPERCORE_STOP_REASON_UNSPECIFIED = 0,
    HYPERCORE_STOP_REASON_EOS         = 1,  /* the model produced the end-of-stream token */
    HYPERCORE_STOP_REASON_LIMIT       = 2,  /* max_new_tokens reached */
    HYPERCORE_STOP_REASON_CANCEL      = 3,  /* broken by hypercore_engine_cancel */
    HYPERCORE_STOP_REASON_ERROR       = 4   /* internal engine fault */
} hypercore_stop_reason_t;

typedef struct hypercore_engine_t hypercore_engine_t;

/* The wire-facing structs below are explicitly packed. Byte offsets are part of the
 * contract, not a consequence of the compiler's alignment decisions, so a Rust, Go or
 * Python embedder can hard-code them.
 * struct_size is a protocol version tag, not a field count: a reader declares the size it
 * was compiled against and the engine writes no more than that. Fields are appended at the
 * end only - an offset already handed out never moves.
 */
#pragma pack(push, 1)

/* What this engine instance can do, and what it is configured to do.
 *
 * active_pool_workers is a resolved number, never the -1 "engine decides" sentinel: a caller
 * that reads -1 as "zero workers" is the failure mode this field exists to prevent.
 *
 * instance_pcie_frac / instance_spec_min_p are the resolved Level-1 baseline. They are a
 * read-only snapshot, not a request: a caller that wants to know what the engine is running
 * with must not have to submit a probe request to find out. A value of -1.0f here means the
 * instance was never given one, i.e. the engine decides per request. */
/* The expert tier, appended at the end (#Monitor).
 *
 * These are the numbers the engine already computes when it sizes the VRAM expert cache, and
 * until now they only ever reached a human through the engine's startup text. A Monitor tab
 * that cannot see them shows "-" for "Experts in VRAM" on a machine that has a cache, which
 * reads as a missing feature rather than a missing wire field.
 *
 * expert_slots is what the cache holds NOW; expert_slots_full is what it was sized for. They
 * differ whenever the VRAM reserve shrinks during a run, and reporting only one of them is how
 * a monitor ends up four times wrong on a multi-GPU config.
 *
 * expert_source names how the host-side experts are held, because the two arrangements have
 * completely different failure modes and the same-looking "0 experts" line: 0 = none, 1 = the
 * pack's experts.bin mapped read-only (--mmap-experts), 2 = the arena copy, 3 = resident in
 * page-locked RAM (--resident-experts). */
typedef struct hypercore_caps_t {
    uint32_t struct_size;
    uint32_t backend_type;           /* 0 = cpu, 1 = cuda, 2 = hip */
    uint32_t max_batch_slots;        /* concurrent requests this instance accepts */
    uint32_t max_context_len;
    int32_t  active_pool_workers;    /* cores the pool is actually configured for */
    float    instance_pcie_frac;     /* resolved Level-1 baseline, or -1.0f if unset */
    float    instance_spec_min_p;    /* resolved Level-1 baseline, or -1.0f if unset */
    uint32_t expert_slots;           /* VRAM expert-cache slots in use */
    uint32_t expert_slots_full;      /* slots the cache was sized for */
    uint32_t expert_cache_mib;       /* MiB of VRAM the expert cache holds now */
    uint32_t expert_cache_full_mib;  /* MiB it would hold at full size */
    uint32_t vram_free_mib;          /* free VRAM at the moment the tier was sized */
    uint32_t expert_source;          /* 0 none, 1 mmap, 2 arena, 3 resident RAM */
    uint32_t prompt_chunk;           /* tokens per prompt chunk */
} hypercore_caps_t;

/* Written once, in the terminal chunk of a stream. The first 40 bytes are the frozen
 * core; the absolute byte offsets below are locked for every embedder.
 *
 * The three timings decompose, and only because they start at different instants:
 *
 *   prefill_ms                = scheduled_at -> first token   (prompt evaluation)
 *   time_to_first_token_ms    = accepted     -> first token   (what the client waited)
 *   queue hold                = ttft - prefill                (derived, gate congestion)
 *
 * prefill_ms starts at the scheduling point, not at acceptance (the legacy Monitor's
 * prompt_ms). Folding the queue hold into it would make slot contention and a slow
 * prompt the same number, which is exactly the distinction the Monitor is for. A node
 * with fragmented VRAM spikes ttft while prefill stays flat.
 *
 * decode_ms is the generation window: prefill_end to the last token. It includes the
 * first speculative draft cycle - drafter construction, candidate verification, sampler
 * logic - so a change in lookahead length moves decode_ms and never prefill_ms. That is
 * the point: prefill_ms must not depend on sampling parameters, or a prompt cost and a
 * sampling cost become the same number.
 *
 * Neither timing is a substitute for the other. 0 in ttft means no token was delivered,
 * not that the first token was instantaneous.
 *
 * Forward compatibility: a reader accepts a summary whose declared struct_size is
 * greater than or equal to what it understands. It reads the prefix it knows and
 * discards the tail. An appended field therefore never breaks an older caller; a
 * removed one would.
 *
 * The block below the 40-byte prefix is the decode's expert-tier accounting (#Monitor).
 * It is per-request, not cumulative: cache_hits/cache_lookups are the hits and lookups of
 * THIS request, exactly as the engine's own "decode expert cache hit rate" line reports them.
 * A reader that does not know these fields reads a 40-byte summary and is unaffected.
 * A field the worker did not fill is 0 here and reported as None by the reader, never as a
 * measured zero - see decode_terminal in serve/ipc_parser.py.
 */
typedef struct hypercore_request_summary_t {
    uint32_t             struct_size;          /* 0-3   protocol version tag */
    uint32_t             produced_tokens;      /* 4-7   */
    uint64_t             prefill_ms;           /* 8-15  scheduled_at -> prompt evaluation end */
    uint64_t             decode_ms;            /* 16-23 prefill_end -> last token, drafts included */
    uint32_t             drafts_accepted;      /* 24-27 */
    uint32_t             drafts_offered;       /* 28-31 */
    hypercore_stop_reason_t stop_reason;       /* 32-35 */
    uint32_t             time_to_first_token_ms; /* 36-39 replaces the reserved padding;
                                                 the 40-byte footprint does not change */
    uint32_t             cache_hits;           /* 40-43 decode lookups already in the VRAM tier */
    uint32_t             cache_lookups;        /* 44-47 decode lookups in that tier in total */
    uint32_t             offloaded;            /* 48-51 routed experts read over PCIe or by another GPU */
    uint32_t             prompt_read;          /* 52-55 prompt tokens this request actually read (#471) */
    uint32_t             ram_blobs;            /* 56-59 expert blobs served from resident RAM */
    uint32_t             file_blobs;           /* 60-63 expert blobs read from the pack files */
    uint32_t             file_kb;              /* 64-67 kilobytes those reads cost */
} hypercore_request_summary_t;

/* Per-request generation settings. -1.0f on a tuning field means "use what the engine
 * instance was configured with"; values outside their domain are rejected, not clamped. */
typedef struct hypercore_request_config_t {
    uint32_t struct_size;
    float    temperature;      /* >= 0 */
    float    top_p;            /* [0, 1] */
    uint32_t top_k;            /* 1..64 */
    uint32_t max_new_tokens;   /* >= 1 */
    float    pcie_frac;        /* [0, 1] or -1 for instance default */
    float    spec_min_p;       /* [0, 1] or -1 for instance default */
} hypercore_request_config_t;

/* One delivered token. The final chunk carries token_id == -1 and the summary; the summary
 * pointer is valid only for the duration of the callback call - copy it if you need it
 * after the callback returns. */
#pragma pack(pop)

typedef struct hypercore_output_chunk_t {
    int32_t                         token_id;
    uint32_t                        prompt_reused;
    const hypercore_request_summary_t* summary;
} hypercore_output_chunk_t;

typedef void (*hypercore_stream_cb)(const hypercore_output_chunk_t* chunk, void* user_data);

/* --- core API ------------------------------------------------------------- */

/* Create an engine instance.
 *
 * config is the instance configuration, and it may be given in either of two forms.
 * The form is decided by the first non-whitespace character, so the two can never be
 * confused and no heuristic is needed:
 *
 *   '{'  an inline JSON object, forwarded verbatim - byte for byte, whitespace and
 *        key order preserved, so the worker sees the payload the caller wrote.
 *   else a path to a JSON file, read once at init.
 *   NULL no configuration at all (system defaults plus HYPERCORE_* environment).
 *
 * The payload is bounded at 10 MB. The size is checked on the argument and on the file
 * before any buffer is allocated, so an oversized config is refused outright rather than
 * half-read; the file itself is streamed in fixed blocks.
 *
 * A path that does not exist, is not readable, is empty, or does not contain a JSON
 * object is HYPERCORE_STATUS_INVALID_ARG - it is not silently treated as "no config",
 * because a typo in a path or a config file that was created but never written must not
 * run the engine on hidden defaults. Only an absent argument means "no config".
 *
 * Both forms sit at the same precedence level: system defaults, then the environment,
 * then this config. Whichever form was supplied is what the out-of-process worker is
 * handed, so a child never re-reads the file. */
HYPERCORE_API hypercore_status_t hypercore_engine_init(hypercore_engine_t** out_engine,
                                             const char* config);

/* Read back what this engine instance is configured to do.
 *
 * ABI requirement, stated explicitly because it is easy to miss: the caller must set
 * out_caps.struct_size = sizeof(hypercore_caps_t) before the call. A zero or missing
 * size is a hard HYPERCORE_STATUS_INVALID_ARG refusal, not a clamp - an undeclared
 * struct cannot be written into safely. A caller compiled against an older header may
 * declare a smaller size and will receive the leading fields it knows about. */
HYPERCORE_API hypercore_status_t hypercore_engine_get_caps(const hypercore_engine_t* engine,
                                                  hypercore_caps_t* out_caps);

/* Schedule a request. Returns before generation finishes: the callback is invoked from an
 * engine thread, not from the calling thread.
 *
 * request_id is the caller's own tracking token. It must be non-zero and unique among the
 * requests this engine currently has in flight; it is also handed back as user_data to the
 * callback, so a caller can route a stream without a per-call closure. */
/* --- precedence: which value is live ------------------------------------- */

/* Resolve a request config against the instance defaults and write the result to out.
 *
 * Two levels only, and the rule is stated once here so the calibration adapter, the web
 * runtime and the worker cannot each invent their own:
 *
 *   Level 1 - the instance baseline, fixed at hypercore_engine_init() by
 *             system defaults -> HYPERCORE_* environment -> the JSON string.
 *   Level 2 - this request frame. A tuning field set to exactly -1.0f is a sentinel that
 *             says "take Level 1"; any value inside the field's domain overrides it for
 *             this execution only. Anything outside the domain is INVALID_ARG, never clamped.
 *
 * Pure and stateless: it reads the instance baseline already resolved in the engine, so it
 * can be called before submit to know what the engine will actually use. out may be the
 * same buffer as request. */
HYPERCORE_API hypercore_status_t hypercore_engine_resolve(
    const hypercore_engine_t*          engine,
    const hypercore_request_config_t*  request,
    hypercore_request_config_t*        out
);

HYPERCORE_API hypercore_status_t hypercore_engine_submit(
    hypercore_engine_t*               engine,
    uint64_t                       request_id,
    const hypercore_request_config_t* config,
    const int32_t*                 prompt_tokens,
    size_t                         prompt_len,
    hypercore_stream_cb               callback,
    void*                          user_data
);

/* Ask the engine to stop the request identified by request_id. Thread-safe, non-blocking:
 * it sets a flag that the request thread checks between tokens. A cancelled stream still
 * terminates with a summary, carrying stop_reason == HYPERCORE_STOP_REASON_CANCEL and
 * produced_tokens == the tokens already delivered. The batch slot is released by the
 * request thread, not by this call.
 *
 * Returns HYPERCORE_STATUS_NOT_FOUND if request_id is not in flight. */
HYPERCORE_API hypercore_status_t hypercore_engine_cancel(hypercore_engine_t* engine, uint64_t request_id);

/* Destroy the instance. All requests must have finished: the engine waits for in-flight
 * streams rather than abandoning them. */
HYPERCORE_API void hypercore_engine_destroy(hypercore_engine_t* engine);

#ifdef __cplusplus
}
#endif

#endif /* HYPERCORE_HYPERCORE_API_H */
