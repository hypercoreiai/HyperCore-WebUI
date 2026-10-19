# strata-chat-2026-10-07-17-37.md

2026-10-07 · 807 KB · 9 requests · topics: deployment-permissions, hardware-environment, issue-log, memory-vram, performance-benchmarks, prompting-behaviour

## What was asked
- lm-eval to benchmark your level of intelligence and performance: git clone --depth 1 https://github.com/EleutherAI/lm-evaluation-harness
- what if i set lm-eval up on another computer in the LAN, can you test against that
- /home/ai-user0/venv/lm-evaluation-harness is installed on 192.168.74.9. you can shortcut the logon with ssh user0
- switch cuda to 11.8 for pascal
- did you try https://huggingface.co/EleutherAI/pythia-1b/tree/main
- explain HF token so Qwen/*
- there are no qwen tokens. i have a meta and a llama, qwen has never required me to request access
- how did you score on the same series of tests?
- i thought the whole reason you setup lm-eval was to test yourself and provide me the results

## What came of it
- `/home` root is not writable, so it lives under `ai-user0`.
- Also, `Qwen/` repos return 401 from the HF CDN in this environment (gpt2 / pythia / opt are reachable).
- Raw JSON: `/home/ai-user0/lm-eval-runs/suite_2026-10-07T15-19-40.147324.json`, `gsm_2026-10-07T15-20-09.289994.json`.
- Artifact `/home/ai-user0/lm-eval-bench/summarise_results.py` (runs, prints the table above; ruff flags only a shebang-not-executable and import-order nit).
- The Strata process on the GPU is the engine serving me, and it exposes no `/v1/chat/completions`.
- So the numbers above are a verified end-to-end run of the harness on a toy model, not a self-assessment — and with `n=20` per task the scores are directional, not leaderboard-grade (throughput was ~1 loglikelihood req/s for the 1.4B model, ~10 req/s for 125M).
- `ssh`, `scp`, `nc`, `ping` are available, so either topology is workable once a port is open.
- Peer serves the model, harness runs here (preferred — I drive it, results land in `/home/ai-user0/lm-eval-runs`).
- Note: `boolq/piqa/hellaswag/arc_` are loglikelihood tasks and need logprobs, so use `local-completions`/`openai-completions` against vLLM or llama.cpp; a pure chat endpoint only supports generation tasks like gsm8k/mmlu-generation.
- Harness runs on the peer, I drive it over SSH — works if you open :22 on `.9`.
- State is written to `/home/ai-user0/lm-eval-bench/state.json` (loop 3, `verified: true`, queue of 3 items) — status: blocked on the peer, not on this box.
- I confirmed it non-interactively: Everything else is healthy. Key exchange succeeded (`sntrup761x25519` / chacha20), and `Host '192.168.74.9' is known and matches the ED25519 host key` — so the transport is fine, only the identity isn't authorized.
- (Correction to my earlier sweep: port 22 is open on `.9`; my `nc -z` check gave a false negative.) The likely source of the confusion: `/home/ai-user0/.ssh/authorized_keys` on this box contains a key for `[email]`.
- Two side notes: `sshpass` isn't installed here, so I can't answer an interactive password prompt from the terminal tool — key auth is the clean path for me.
- And `/home/ai-user0/.ssh/config` exists but is `root:root 600`, so ssh can't read it; harmless, but worth `chown` if you intended to use it.
