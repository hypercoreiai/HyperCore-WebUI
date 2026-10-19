# Deployment, paths and permissions

Accumulated from 85 transcripts, 230 entries. Built 2026-10-19. Newer chats append here; within a section the entries are grouped by the thing they are about, newest statement first.


## Current state

Maintained by hand; the builder keeps it in place.

**Two users.** `ai-user0` owns the work; `ai-agent` is the account the MCP servers run as. The
sudoers grant for `ai-agent` was a no-op (`ai-agent ALL=(ai-agent) NOPASSWD: ALL`) until real
entries were added. Groups now include `dev`, `docker`, `user0`, `systemd-journal`.

**`/home` itself is not writable.** Only `/home/<user>/…` is. Anything that defaults to a cache
or output path under `/home` fails: ruff wanted `/home/.ruff_cache`, pytest wanted a basetemp,
`docs__create_file` with a relative path resolved to `/home`. The fix is always to default to a
per-user directory, not to grant write on `/home`.

**The GPU box is `192.168.74.2`; the client is on the same LAN.** MCP servers that need to be
reachable from a container run as streamable HTTP on their own port (the scrape server on 8766)
over the `ai-bridge` Docker network. There is no nginx on this host, so nginx rules in config
files are inert.

**The server is started by the run script, not by a service manager.** `restart.sh status|stop|start
<model>` is the operational entry point; it finds the pid by matching `serve/server.py` and the
port. A config change takes effect only after that restart — this is the single most common cause
of "I changed it and nothing happened".

**Git ownership.** Repos owned by `ai-user0` but accessed by `ai-agent` produce "dubious
ownership"; the fix is `git config --global --add safe.directory <path>`, not chown.


## What is true now

how the thing works, as the chats established it


**--balance** · 5 chats
- So `--balance` on black_box would silently degrade to `--gen`: `balance_score(170.0, None)` returns `170.0` (verified).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>
- `history.py`: transfer columns are stored in a trial's metrics, and `other_objective_history()` (Maya's cross-objective loader) feeds configurations measured under `gen`/`latency`/`prefill` into a `--balance` study, re-scored for the objective running.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>
- Rows written before this change have no transfer columns, so `balance_score` returns the plain rate for them; that is why the 2292.8 outlier wins a `--balance` dry run.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>
- `bayesian_tune.py` — `--balance` as a fifth objective: No telemetry → discount is zero, so a stub or an older build behaves exactly like `--decode`.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>
- Run `--balance` against `build/strata` when a GPU is free and compare the winner to `--decode`.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**/home** · 5 chats
- `/home` root is not writable, so it lives under `ai-user0`.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-17-37.md</sub>
- The default cache/target paths point at a non-writable root. `/home` itself is not writable, only `/home/ai-user0/0…`.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md · also in 2026-10-03-strata-chat-2026-10-03-18-25.md</sub>
- I also created `/home/.ruff_cache` owned by ai-agent with ACLs for user0, and set `RUFF_CACHE_DIR: /home/ai-user0/.terminal/cache` in `mcp.yaml` — `/home` itself is not writable, which is why ruff failed with "Failed to initialize cache at /home/.ruff_cache".
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>
- `docs__create_file` path default is the main bug. Relative paths resolve against `/home`, which is not writable, so the most natural call (`path: "notes.md"`) fails with `[Errno 13] Permission denied`.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md · also in 2026-10-03-strata-chat-2026-10-03-15-05.md</sub>
- directly under `/home`, which is not writable — the same permission wall that made `docs__create_file` fail on `tool_test/notes.md`.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>

**/home/ai-user0/strata** · 5 chats
- Do not move `/home/ai-user0/Strata`. It is the live system (PID 1552343, port 8081, HTTP 200), and `ni_run-q2_0.sh` hardcodes `cd "/home/ai-user0/Strata"`.
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md · also in strata-chat-2026-10-10-03-30.md</sub>
- And confirm the live `/home/ai-user0/Strata` keeps its old names so PID 1552343 keeps running.
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>
- 6 failures are identical in the untouched archive `/home/ai-user0/Strata` (verified side-by-side): `test_download_auth` ×5 (no network/HF token here) and `test_amd_is_not_asked` (`/usr` not writable).
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>
- One correction to the target path: there is no `/home/ai-user0/Strata` — the framework lives at `/home/ai-user0/Strata` (that's the path `mcp.yaml` resolves `serve/mcp/.py` against, and `ai-agent` already has write access there — `serve/assist_fast_models.json` and `serve/assist_latency_report.txt` are owned by `ai-agent`).
  <sub>2026-10-09 · strata-chat-2026-10-09-11-32.md</sub>
- `git status` in `/home/ai-user0/Strata` works — no "dubious ownership"
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-59.md</sub>

**/home/ai-user0/hypercore-webui** · 3 chats
- Here's the plan — written to `/home/ai-user0/HyperCore-WebUI-PLAN.md` (the target folder `/home/ai-user0/HyperCore-WebUI` exists but is owned by `user0` and isn't writable by `ai-user0`, so I couldn't put it there).
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>
- Comparison closed: every portable Strata_1.43 update is now in `/home/ai-user0/HyperCore-WebUI`. The only item left is SYCL-specific and needs an Arc build.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-08-16.md</sub>
- The copy was made through `sudo` because `/home/ai-user0/HyperCore-WebUI` is owned by `user0`; some of the directories created during the copy are owned by root.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>

**chmod** · 3 chats
- 51 files in `serve/` and `tools/` are executable with no shebang (EXE002), which is a `chmod` away from clean.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>
- On Linux the binaries are `chmod(0o755)` (`:2600-2602`).
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-44.md</sub>
- `chown`/`chmod` are not permitted by the sandbox's sudo policy.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>

**restart stop reports** · 3 chats
- `./restart.sh stop` is required before either command.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>
- Run `./restart.sh stop` then `./optuna_tune/max_tune.sh --model q2_0 --gen --cntx 131072 --trials 40` and the same pair is rewritten from the fresh winner.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-17-13.md</sub>
- `restart.sh stop` reports "the server stopped" but a new instance comes back within seconds, so it is being relaunched by something outside this session.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-22-55.md</sub>

**./run.sh** · 2 chats
- The run needs the GPU, so I did not start it: The WebUI on port 8081 (PID 336066) still predates the paused-retry code and the Monitor history card, so `./run.sh` needs a restart for those to show.
  <sub>2026-10-18 · 2026-10-18-12-19-continue.md</sub>
- And the WebUI running on port 8081 (PID 336066) predates all of this, so `./run.sh` needs a restart for the hook button and the new health payload to show in the live app.
  <sub>2026-10-18 · 2026-10-18-12-19-continue.md</sub>

**/health** · 2 chats
- Server is up and healthy. `serve/server.py` pid 1185977 on port 8081, worker pid 1185980 alive, `/health` returns `{"status": "ok", "loaded": true}`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-22-55.md</sub>
- Live: `python3 -m serve.hypercore_server --port 8125 --worker build-shared/hypercore-worker --socket /tmp/hc3.sock --config '{"batch":2}'` → `/health` 200, a 20-token completion streams and logs `[MONITOR] … decode_ms=39 tokens=20 drafts=20/100`, `/slots` returns the populated monitor, a dropped client issues `CMD_CANCEL` and `in_flight` returns to 0.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**/home/.ruff_cache** · 2 chats
- As ai-agent: writes to `/home/ai-user0/.terminal`, `/home/ai-user0/0`, and `/home/.ruff_cache` all succeed; `sudo -n apt-get update` succeeds.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>
- Workaround: run it directly — `RUFF_CACHE_DIR=/home/ai-user0/.ruff_cache /home/ai-user0/.pyenv/versions/3.12.9/bin/python3 -m ruff check …` (the default `/home/.ruff_cache` is not writable).
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-25.md</sub>

**/home/ai-user0/.terminal/cache** · 2 chats
- `RUFF_CACHE_DIR` fixes the "Failed to initialize cache at /home/.ruff_cache: Permission denied" failure — `/home/ai-user0/.terminal/cache` is writable by both user0 and ai-agent.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>
- `RUFF_CACHE_DIR` points at `/home/ai-user0/.terminal/cache`, which is writable by both users — the ruff cache failure is gone.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**/opt/ai-workspace/.tmp/pytest-of-ai-user0** · 2 chats
- Needs `TMPDIR` pointed at a writable dir; the default `/opt/ai-workspace/.tmp/pytest-of-ai-user0` is not owned by this user.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>
- The 16 errors are an environment artifact — pytest's temp root `/opt/ai-workspace/.tmp/pytest-of-ai-user0` is owned by `ai-agent` and I can't remove it; with `TMPDIR=/tmp` those 18 web-scrape tests pass.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md · also in strata-chat-2026-10-06-23-05.md</sub>

**/usr/bin/docker** · 2 chats
- `docker` is granted nowhere. Docker is installed and the daemon is running, but `ai-user0` is not in the `docker` group (`id -Gn` → `adm redis postgres systemd-journal dev-sandbox`) and `/usr/bin/docker` isn't in sudoers — so `docker ps` fails with "permission denied ...
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>
- Either add `/usr/bin/docker` to the grants or `usermod -aG docker ai-user0` (needs real root).
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**192.168.74.2** · 2 chats
- The server runs on the GPU box (`192.168.74.2`) so it can bind UDP `33740`, heartbeat the console on `33739`, and call Ollama at `127.0.0.1:11434`.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-06-37.md</sub>
- It runs as streamable HTTP on port 8766 on the GPU box (`192.168.74.2`) so the AnythingLLM container can reach it over the `ai-bridge` Docker network.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-14-03.md · also in 2026-10-03-strata-chat-2026-10-03-14-03.md</sub>

**_read_path** · 2 chats
- `_writable` now returns a writable sibling with the content copied, so a read-only file can't empty the dropdown, and `_read_path` picks the newest existing copy for reading.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>
- `serve/assist.py` now resolves one path per process (`_writable` → `_read_path`), prefers a file every account can write, and `chmod`s the file it picks to 666 so the next account converges on it instead of creating a second list.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**actually error says** · 2 chats
- The plan says root holds only text files and folders — a test.sh script is fine but let's put it in tools/.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>
- Actually the error says edits must be a list of objects — maybe my JSON had unbalanced quotes due to the literal ` </think> ` inside.
  <sub>2026-10-15 · strata-chat-2026-10-15-18-43.md</sub>

**ai-user0** · 2 chats
- One ownership note: `README.md` and `docs/media/header_readme.jpg` are owned by `user0`, not `ai-user0`, and `sudo chown` is refused by the sandbox.
  <sub>2026-10-10 · strata-chat-2026-10-10-19-47.md</sub>
- That's fine for a report, but if you want the agent to revise it in place, it needs to be writable by `ai-user0` (it already is, as owner).
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-18-22.md</sub>

**balance_score** · 2 chats
- Two edits make the feature work on the HyperCore engine: then a prefill-side analogue of `balance_score`, using the prompt path instead of the decode path: `PACK_READ_CEILING` needs to be measured on this machine; the logs show 1853 MB/s as the best observed, and the profile pre-fill runs at 1598 MB/s, so 2000 MB/s is a reasonable starting constant.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>
- The feature is `transfer.py` (commit 626bed2, "Add transfer telemetry and the --balance objective"): `balance_score`, `pareto_front`, `frontier`, `knee`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**code live strata** · 2 chats
- Live engine pid 3318870 reports `interleaved matvec: 205 copies built, 98 calls reused one (303 calls)` and no `CHECK FAILED` line
  <sub>2026-10-14 · hypercore-chat-2026-10-14-12-55.md</sub>
- The new code is live. Strata restarted at 03:44 (PID 2512290) and `GET /mcp` now reports `condense_at: 0.55, condense_after: 2, condense_chars: 1200`.
  <sub>2026-10-04 · strata-chat-2026-10-04-03-57.md</sub>

**docker** · 2 chats
- `serve/mcp/web-scrape-mcp/tests` fails collection with `ModuleNotFoundError: No module named 'webscrape'` — that's the containerised HTTP scraper, not installed (and `docker` is still absent).
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>
- Similarly, Tier 3 (`jq`, `sqlite3`, `docker`, `gdb`) is on the allowlist but has no dedicated tool — `run` is enough for them, since they are single-purpose binaries rather than things needing a custom report.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>

**home user0 venv** · 2 chats
- The files are on 192.168.74.9 in /home/ai-user0/Downloads/twitter-2026-10-07-d0b9e1eb6a91c60a08cffc18f496108e7a8523ce95ec22286c24751a000bd877
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-20-11.md</sub>
- /home/ai-user0/venv/lm-evaluation-harness is installed on 192.168.74.9.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-17-37.md</sub>

**--flag**
- I verified your numbers before answering, because the plan's credibility depends on them: `src/program/generate.cpp` is exactly 11,102 lines, `struct Options` spans 310–642 with 164 `--flag` handlers, `include/strata/` is 108 headers / 12,141 lines, and the core→kernels fan-out is real (22 `kernels/native`, 10 `kernels/s`, 9 `kernels/qsa`, 9 `kernels/kv`, 7 each quantize/elementwise/cpu-pool).
  <sub>2026-10-09 · strata-chat-2026-10-09-16-17.md</sub>

**--out**
- Try to rebuild; if data/history is not writable, report it and offer the `--out` path.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**.dockerignore**
- `.gitignore`, `.dockerignore`, `Makefile`, `LICENSE` write through the document tools; an extensionless ELF is still refused read and write.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**.pyenv**
- As ai-agent, `HOME=/home/ai-user0/t`, which has no `.pyenv`, so `terminal__status` reported only: With `PYENV_ROOT=/home/ai-user0/.pyenv` set, it correctly returns 3.12.2, 3.12.9 and 3.13.13.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**.ssh/id_rsa**
- `serve/mcp/tests/test_docs.py::test_docs__create_secret` asserted `.ssh/id_rsa` can be created, while `serve/test_docs_mcp.py` asserts a credential is not a document.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**/assist/log**
- Assist calls in the Monitor — live and confirmed against the running server (pid 1087489, port 8081): `/assist/log` returns the endpoint, the pinned list, the 67 over-limit models with reasons, and 36 recorded calls.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**/etc/sudoers.d/ai-agent**
- For reference, `/etc/sudoers.d/ai-agent` only allows NOPASSWD for `apt-get`, `pip3 install`, nginx `systemctl`, the ruff_cache mkdir/chmod, and `visudo -c` — `sudo` with that password refuses anything else ("I'm sorry ai-agent.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-59.md</sub>

**/etc/sudoers.d/ai-user0**
- The generated non-comment rule lines are byte-identical to the live `/etc/sudoers.d/ai-user0` (17 lines, `identical: True`).
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-31.md</sub>

**/home/ai-user0/.cache/strata/server.pid**
- Starts detached with `setsid nohup sh <variant>_run-q2_0.sh &`, writes the PID to `/home/ai-user0/.cache/strata/server.pid`, then polls `http://127.0.0.1:<port>/` until it answers, reporting progress every 10 s (a cold model load takes a minute or two, so `READY_S` defaults to 120).
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**/home/ai-user0/.pyenv/versions/3.12.9/lib/python3.12/site-packages**
- `terminal_check` can't see tools installed in the pyenv interpreters. It reported `ruff` missing even though ruff 0.16.10 is installed at `/home/ai-user0/.pyenv/versions/3.12.9/lib/python3.12/site-packages` — it only probes binaries on PATH.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md · also in 2026-10-03-strata-chat-2026-10-03-18-25.md</sub>

**/home/ai-user0/.ssh/authorized_keys**
- (Correction to my earlier sweep: port 22 is open on `.9`; my `nc -z` check gave a false negative.) The likely source of the confusion: `/home/ai-user0/.ssh/authorized_keys` on this box contains a key for `[email]`.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-17-37.md</sub>

**/home/ai-user0/.ssh/config**
- And `/home/ai-user0/.ssh/config` exists but is `root:root 600`, so ssh can't read it; harmless, but worth `chown` if you intended to use it.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-17-37.md</sub>

**/home/ai-user0/.terminal/retest_report.json**
- The default report is `/home/ai-user0/.terminal/retest_report.json`.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-06-01.md</sub>

**/home/ai-user0/0**
- All non-Docker files that referenced `/home/ai-user0/0` are now pointed at `/home/ai-user0/t`.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-59.md</sub>

**/home/ai-user0/downloads**
- `/home/ai-user0/Strata/serve/test_docs_mcp.py` hardcoded `/home/ai-user0/Strata/.venv/bin/python3` and the script path About the sudo password: it wasn't needed — everything was writable as `ai-agent` (the MCP files are owned by `ai-agent`, and `/home/ai-user0/downloads` has an ACL granting `ai-agent` rwx).
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-59.md</sub>

**/home/ai-user0/hypercore-webui/data/history/_old_chats**
- One note: `/home/ai-user0/HyperCore-WebUI/data/history/_old_chats` is owned by `user0` with mode 755, so the agent user (`ai-user0`) cannot create files there directly — I wrote the document via `sudo cp`, so it is `root:root 644`.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-18-22.md</sub>

**/home/ai-user0/hypercore-webui/optuna_tune/history**
- `tools/import_tune_history.py` pulled the measured configurations from `/home/ai-user0/HyperCore-WebUI/optuna_tune/history` and wrote seven `seed-.json` priors for the i9-14900KF / RTX 4090 / q2_0 box (balance, gen, latency, prefill, across 131k–524k contexts) — those are committed so a first sweep isn't cold.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**/home/ai-user0/hypercore-webui/optuna_tune/prompt_transfer.py**
- The feature is the transfer/balance layer in `/home/ai-user0/HyperCore-WebUI/optuna_tune/transfer.py`, not the sampler. Verified by diffing the function sets of `/home/ai-user0/HyperCore-WebUI/optuna_tune/transfer.py` (652 lines) and `/home/ai-user0/HyperCore-WebUI/optuna_tune/prompt_transfer.py` (352 lines).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

**/home/ai-user0/hypercore-webui/plan.md**
- Final spec is at `/home/ai-user0/HyperCore-WebUI/PLAN.md` (v2, 12 sections).
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md · also in strata-chat-2026-10-17-20-23.md</sub>

**/home/ai-user0/index.html**
- It doesn't turn up — a search for "Current State of AI October 2026" returns unrelated hits (`/home/ai-user0/index.html`, llama.cpp docs, Cursor project files), and `home__status` reports the crawl is idle with 0 files indexed at the root, despite 80,425 rows in the database.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-14-03.md</sub>

**/home/ai-user0/lm-eval-bench/state.json**
- State is updated in `/home/ai-user0/lm-eval-bench/state.json` (loop 4, JSON verified, queue: install key → run suite → report).
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>

**/home/ai-user0/lm-eval-bench/summarise_results.py**
- Artifact `/home/ai-user0/lm-eval-bench/summarise_results.py` (runs, prints the table above; ruff flags only a shebang-not-executable and import-order nit).
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-17-37.md</sub>

**/home/ai-user0/lm-eval-runs**
- Peer serves the model, harness runs here (preferred — I drive it, results land in `/home/ai-user0/lm-eval-runs`).
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-17-37.md</sub>

**/home/ai-user0/strata/assist_fast_models.json**
- (b) Two allowlist files, owned by two accounts, drifting apart. `serve/assist_fast_models.json` is `ai-agent`-owned 0644 (10 pinned, 12:23); `/home/ai-user0/Strata/assist_fast_models.json` is `ai-user0`-owned (13 pinned, 14:42).
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**/home/ai-user0/strata/ni_strata-q2_0.json**
- The text-only config exists here: `/home/ai-user0/Strata/ni_strata-q2_0.json` has no `"vision"` section, and `ni_run-q2_0.sh` starts it on the same port 8081.
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-36.md</sub>

**/tmp**
- Writes: `/tmp` inside WSL is writable but is cleared between `terminal` calls{{#if worktrees}}.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-01-06.md · also in 2026-10-06-strata-chat-2026-10-06-01-06.md</sub>

**/tmp/repltest/.terminal/repl_driver.py**
- `tools/test_repl_loop.py` hard-codes `sys.path.insert(0, "/home/ai-user0/HyperCore")` and writes `/tmp/repltest/.terminal/repl_driver.py` at import — that directory belongs to another user, so it raises `PermissionError` during collection.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**/usr/bin/**
- `visudo` is at `/usr/sbin/`, not `/usr/bin/` — my sudoers rule used the wrong path.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**172.17.0.1**
- In the AnythingLLM storage `plugins/anythingllm_mcp_servers.json`: `172.17.0.1` is the Docker bridge to the host on Linux.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-06-37.md</sub>

**192.168.74.3**
- Verified against `mcp.yaml` (lists its five top-level keys), `strata-q2_0.json`, `ps5-control/config.toml` (read `playstation.ip` = `192.168.74.3`), and the RAG index directly: Non-SELECT statements are refused (`DELETE` raises).
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-19-51.md</sub>

**192.168.74.9**
- `serve/web/app.js:161` polls it continuously; your dashboard at `192.168.74.9` was polling it every few seconds, which is why the identical traceback appears ~20 times.
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**_cpuramfallback.__init__**
- The leak was genuine in the running server — `Telemetry.__init__` starts a daemon `_loop` thread and `_CpuRamFallback.__init__` calls `_times()` immediately, so every tick was leaking a handle.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-23-05.md</sub>

**_wait**
- HTTP-transport calls cannot be interrupted `serve/mcp/__init__.py:345-360`: the POST runs on a daemon thread and `_wait` raises `McpTimeout`/`McpCancelled` while that thread keeps running (with a hardcoded `timeout + 5` socket timeout).
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-14-10.md</sub>

**_writable assist falls**
- `_writable()` (assist.py:44) falls back to the copy this process can write, `_read_path()` (assist.py:80) reads whichever was written last, and `_write_shared()` (assist.py:87) mirrors the write — but only to copies it can write.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**ai-agent**
- `ai-user0` cannot write the `ai-agent` file, so the divergence is a filesystem-permission fact, not a code bug.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**allow_hosts**
- `allow_all_hosts: true` — lift the network restriction entirely: outbound access to any host over any protocol, so SSH, FTP, and raw sockets work too (unlike `allow_hosts`, which is HTTP/HTTPS-only).
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-01-06.md</sub>

**android-d7o4t9**
- What works is `ssh [email]`, and it is now passwordless: publickey auth succeeds, `BatchMode=yes` returns `OK` from host `android-d7o4t9`.
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>

**assist.py**
- The live server (pid 1071617, port 8081, started 14:38) still has the pre-convergence `assist.py` loaded; it needs a restart for the new bridge code to take effect.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**ast**
- Largest units measured with `ast`: The project already has the right seams (`quant_store.py`, `build_api.py`, `engine_supervisor.py`, `offline_engine.py`, `strata_hook.py`), so the split is a matter of moving the OpenAI lane, the Anthropic lane, the vision path, and the MCP tool loop out of `server.py` into modules that already have their own tests.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**auth works shortcut**
- Key auth now works — the shortcut resolves differently, but `ssh [email]` is passwordless now.
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>

**bayesian_tune.py**
- The calibration measures decode only — "a prompt balances its CPU and PCIe shares itself, layer by layer." `bayesian_tune.py` scores `gen` on decode and prefill, so the installer's numbers and the sweep's numbers are not the same quantity.
  <sub>2026-10-15 · strata-chat-2026-10-15-14-22.md</sub>

**body-pose**
- Blank fields are real gaps, not parse failures for `body-pose`, `cosmos3-nano`, `cosmos3-nano-reasoner` — those three cards render only via JavaScript, so no parameter/context data is server-side.
  <sub>2026-10-09 · strata-chat-2026-10-09-08-40.md</sub>

**boundary.yml**
- `boundary.yml` stays in the working tree and is gitignored with the reason written in the file, so the gate isn't lost — it just can't land until a token with the `workflow` scope exists.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**cargo_home**
- It works here because the hub was launched interactively, but if `server.py` is ever started by systemd or `sh -c`, `PATH`, `UV_`, `PIP_`, `CARGO_HOME` and `TMPDIR` are all lost — and the script's own comment warns about exactly this for `TMPDIR`/`nvcc`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**cat**
- Logs land in `/home/ai-user0/.terminal/jobs<name>` — note they are plain files, so `docs__read_file` refuses them ("not a document type"); use `process` action=log or a shell `cat`.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-25.md</sub>

**check servers make**
- check all the mcp servers to make sure they link to /home/ai-user0/0 The sudo password for ai-agent is Qazwsx1@3
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-59.md</sub>

**command line valid**
- So the command line is valid, but the parity result itself is not yet verified; it needs the card free (`./restart.sh stop` or stop the Strata server).
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**config/run-q2_0.json**
- One thing you have to do The server running now (PID 399364, port 8081, `config/run-q2_0.json`) is still the pre-fix code: I queried it and it still returns `latest` as a single object.
  <sub>2026-10-18 · 2026-10-18-15-14-continue.md</sub>

**confirms 3448 serve**
- `ps` confirms pid 3448 (`serve/server.py --port 8081`) plus its worker and engine are holding the card.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**contains default target**
- Contains: default.target.wants, pipewire.service.wants, sockets.target.wants, gt-mcp-server.service, pipewire-session-manager.service [universal] /home/ai-user0/.cursor[key]/update-cli-config/SKILL.md … Project overrides only affect the current session; they are not written back to the home config.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-15-05.md</sub>

**continue**
- The per-layer host function that raises `landed` is launched only on the non-DMA path (`generate.cpp:7129-7137`); the DMA branch `continue`s at `:7116`, and `copy_blobs` (`dma_batch.hpp:65`) launches no host function.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**cpu_experts/tok**
- The CPU lane is not a byte stream: `pcie_share` decides how many of those experts are copied over PCIe versus computed on the CPU, so `cpu_experts/tok` and `ram_fetch/tok` trade against each other.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**curl**
- `rg`, `diff`/`patch`, `docker`, `curl`/`wget`, `strace`/`time` were already allowed and are now exercised through `run`/`debug`.
  <sub>2026-10-03 · strata-chat-2026-10-03-19-51.md</sub>

**data/expert-profile.bin**
- For Q2_0 the decode-side balance is degenerate. Every measured run reports `ram_blobs=0 file_blobs=0 offloaded=0` for the decode, because all 13,061 expert slots are pre-filled from `data/expert-profile.bin`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**data/history/**
- What the permission fix let me do `data/history/` is now `ai-user0:dev-sandbox` and writable.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**docs__read_file**
- Logs land in `/home/ai-user0/.terminal/jobs<name>`, which is under the docs root, so `docs__read_file` can read a server's log.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>

**downloads/**
- Permission layer separate from execution — new `serve/tool_permissions.py` Ownership rule, not a path rule: a file this process created, or anything under `scratch/`, `tmp/`, `downloads/`, is free; anything else is refused with a reason and a `force=true` escape hatch.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**engine/**
- `tmp/strata-main` is still on disk. Per the plan, after an engine is built and copied into `engine/`, `tmp/` should be removable and the WebUI should still work.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**fails http 8765**
- If that fails, use `http://192.168.74.2:8765/mcp`.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-06-37.md</sub>

**failure_reason**
- `failure_reason`: match `terminate called … what(): …` and `cannot open` so a bad pack is labelled `PACK_LAYOUT` instead of a blank exit.
  <sub>2026-10-15 · strata-chat-2026-10-15-09-32.md</sub>

**filter.file**
- The Monitor card does surface this (`filter.file` + the "not writable by this user" note), and `_shared_lines()` (assist.py:311) unions the two call logs so the history is one history.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**groups agent data**
- Groups are now `ai-agent bin adm www-data systemd-journal docker dev user0`.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**harness runs peer**
- Harness runs on the peer, I drive it over SSH — works if you open :22 on `.9`.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-17-37.md</sub>

**history/**
- The 512 tokens now include a forced reasoning block, so a `--think-budget 64` sweep's numbers are not comparable with the 17 decode priors and 10 balance configs already in `history/` for this machine at 16384 — those were measured with no think phase.
  <sub>2026-10-16 · strata-chat-2026-10-16-03-29.md</sub>

**hypercore_engine_init**
- `src/interface/hypercore_bridge.cpp` — `load_config_text()`, `is_blank()`, `trimmed()`, `looks_like_json_object()`, placed next to the existing JSON helpers; `hypercore_engine_init` resolves the source before allocating the engine, so a bad path unwinds nothing.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**include/**
- There is no gcov or clang-tidy coverage build in the repo, so there is no line number for `src/` and `include/`.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md</sub>

**integrating tools daytona_bridge_test**
- Integrating the tools/daytona_bridge_test.py verification pass as a blocking stage in your CI pipeline ensures that the hidden visibility boundaries (STRATA_API/HYPERCORE_API), calling conventions, and cross-language compatibility guarantees are proven on a clean, isolated, foreign toolchain before code can be merged.
  <sub>2026-10-10 · strata-chat-2026-10-10-00-02.md</sub>

**interpreter**
- sudoers/mcp.yaml: open items are the operator-rule fallback and the `INTERPRETER` grant.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>

**io_share**
- If the median ratio comes back under 0.5, the `io_share` in the balance score is an upper bound and the discount is overstated; if it is near 1, the 1741 MB/s anchor is confirmed as real bus traffic.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-08.md</sub>

**krhold**
- Settle landed lookahead reads (`kRLoad` → `kRHold`), release promoted slots, adopt orphans (`STRATA_GLM_TIER_GC`).
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**mb/tok**
- Prefill is self-balancing. "Decode speed only: a prompt balances its CPU and PCIe shares itself, layer by layer." Offline, without a GPU: `tools/glm_cache_sim.py` and `tools/glm_tier_replay.py` replay the tiers and report `reads/tok`, `MB/tok`, `off-card ms/tok`, `vram%`, `ram%` for a given pool size and split.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**mcp.py**
- One thing to know before it takes effect: the running server (PID 2491454) still reports the old settings — `GET /mcp` returns `{'max_result_chars': 6000, 'max_rounds': 20}` with no condense keys, because it loaded `mcp.py` before the patch.
  <sub>2026-10-04 · strata-chat-2026-10-04-03-57.md</sub>

**mcp.yaml**
- To make them apply you'd need to start Strata (or just that server) as ai-agent; `mcp.yaml` has no `user:` key, so `command: sudo` is the only in-file option and it conflicts with stdin being the JSON-RPC channel.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**mmvq_il**
- `MMVQ_IL` is safe to turn on. That is the exact line the plan said would settle it: 454 interleaved calls across windows T=1,2,3,4,5,6, every one compared against `native_mmvq`, zero mismatches.
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>

**native_q8_1_interleave**
- The remaining suspect, if the check still reports a mismatch, is the wide layout: `native_q8_1_interleave` writes `cp = native_q8_1_il_cp(ncols)` = 8 lanes for 5–6 columns, and `native_mmvq_il_kernel<I, N, R>` must read that same stride.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**nginx.service**
- The nginx rule is inert — there is no `nginx.service` on this host (docker and containerd are running).
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**ngram**
- It is organised as eleven modules that mirror eleven CMake libraries, and the dependency graph is acyclic and layered: `artifact → plan → core → kernels → engine → prefill → program`, with `spec` and `ngram` as side branches consumed only by the driver.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-08-03.md</sub>

**niko1221/strata**
- Is the HyperCore rename intended to land, or is it abandoned? The GitHub repo is still `Niko1221/Strata` and HEAD is self-consistently "strata" — the rename is only in the uncommitted working tree.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>

**ping**
- `ssh`, `scp`, `nc`, `ping` are available, so either topology is workable once a port is open.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-17-37.md</sub>

**portainer**
- For reference, the API key in `strata-q2_0.json` is `api_key: "0000"`, and AnythingLLM is not running (`docker ps` shows only `portainer`), so the only web UI on this box is Strata on `:8081`.
  <sub>2026-10-04 · strata-chat-2026-10-04-06-37.md</sub>

**q8_1_il.cuh**
- `native_q8_1_il_cp(ncols)` returns 8 lanes for 5–8 columns, and `Q81Il<NC>` in `q8_1_il.cuh` already reads `NC` up to 8.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**readme.md**
- I worked around it by deleting and recreating `README.md` (the directory is world-writable).
  <sub>2026-10-10 · strata-chat-2026-10-10-19-47.md</sub>

**reconstructed_installed.sudoers**
- Strictly, only two files match `sudoers.ai-user0`; the third sudoers file is `reconstructed_installed.sudoers`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**results decode 2026**
- `results/…-decode-2026-10-16_024805.csv` — 10 trials, 9 PASS, 1 TIMEOUT, 16384 ctx / max_new 512: the same request shape as the balance run, so the comparison is clean.
  <sub>2026-10-16 · resume_strata-chat-2026-10-16-03-08.md</sub>

**scp**
- Next step: once `ssh -o BatchMode=yes [email] 'echo ok'` works, I'll drive the benchmark over SSH (topology B) — clone, install, run, and `scp` the results JSON back.
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>

**scrape__file_info**
- When the chat is … [universal] /home/ai-user0/_Notes/strata-chat-2026-10-03-15-09.md … Install it — `scrape__file_info` cannot report duration or codec for the audio/video it downloads.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**serve/**
- Fixing that means editing `serve/mcp/__init__.py`, and `serve/` is `root:root 644` (`stat -c '%a %U:%G' serve/mcp/__init__.py` → `644 root:root`), while the sudoers rules here allow only `sudo apt-get install -y nodejs npm git ripgrep jq`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md · also in strata-chat-2026-10-05-20-19.md</sub>

**serve/mcp.py**
- The Docker version is untouched. If you'd rather keep the container running, the alternative is a `url:` entry — `url: http://web-scrape-mcp:8766/mcp` — which `serve/mcp.py` already supports.
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

**serve/server.py**
- Why you still see it. The fix is uncommitted and the process serving port 8081 (pid 1826973) started at 17:04, while `serve/server.py` was last written at 17:49.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md</sub>

**serve/test_assist.py**
- `serve/test_assist.py`, `serve/test_monitor.py`, `serve/test_frontend.py`: 44 passed (18 subtests), including four new `SharedPaths` tests covering the "file owned by the other account" case, the chmod-to-666 convergence, and "Check lag writes to the file the dropdown reads".
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**server.py**
- Fix: extract the state machines into small exported functions and run them under a stub DOM (the harness already does this for a few), and drop the cross-language `server.py` string matching.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**slot states phase**
- Slot states are the phase vocabulary here: `kRFree / kRHold / kRRelease / kRDemote / kRNew / kRLoad / kRPin`, and per-layer `kFree / kSpare / kResident / kDraining / kLanding`.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**sntrup761x25519**
- I confirmed it non-interactively: Everything else is healthy. Key exchange succeeded (`sntrup761x25519` / chacha20), and `Host '192.168.74.9' is known and matches the ED25519 host key` — so the transport is fine, only the identity isn't authorized.
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md · also in 2026-10-07-strata-chat-2026-10-07-17-37.md</sub>

**sshpass**
- Two side notes: `sshpass` isn't installed here, so I can't answer an interactive password prompt from the terminal tool — key auth is the clean path for me.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-17-37.md</sub>

**strata_install**
- The install is gated. `strata_install` returns a plan first, so the assistant can show you the download size before anything is written to disk.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**sudo returns exactly**
- `sudo -n -l` returns exactly two grant rules: one line expanding to 261 commands, plus `/usr/sbin/visudo -c -f /etc/sudoers.d/ai-user0`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**sudoers file grants**
- The sudoers file grants nothing. `ai-agent ALL=(ai-agent) NOPASSWD: ALL` means "ai-agent may run any command as ai-agent" — a no-op.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**sudoers.ai-user0.expanded**
- Keep `sudoers.ai-user0.expanded` only as the audit view (it's the readable list of what is and isn't granted).
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**terminal**
- Writes: a per-thread temporary directory exposed via `$TMPDIR`, `$TMP`, and `$TEMP` is writable and persists across `terminal` calls in this thread{{#if worktrees}}, along with these project directories:
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-01-06.md</sub>

**terminal__install**
- Minor notes. `terminal__install` with a pyenv path resolves `/home/ai-user0/.pyenv…` to `/home/ai-user0/.pyenv…` (the trees are linked) and installs as a user install — it works, but the reported interpreter isn't the one you asked for.
  <sub>2026-10-04 · strata-chat-2026-10-04-03-57.md</sub>

**terminal__status**
- If you want the actual installed set (git, ruff, mypy, pytest, cmake, clang-tidy, gdb, valgrind, jq, yq, sqlite3, docker, strace) rather than the capability list, `terminal__status` gives versions in one call.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**terminal_check**
- Python tooling that `terminal_check` must auto-detect → install into a pyenv version and `pyenv rehash`, so the shims land on PATH.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**terminal_root**
- Sandboxing — every new tool resolves paths through `inside()`, so they're bound to `TERMINAL_ROOT`; the allowlist gained `ninja, ar, strip, nm, objdump, xxd, clang-tidy, lldb, strace, ltrace, time`.
  <sub>2026-10-03 · strata-chat-2026-10-03-19-51.md</sub>

**terminal_status**
- `terminal_status` could also report the download size limit and whether the state dir is writable, since that's what actually breaks tooling.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**transport type rejected**
- The old SSE transport (`"type": "sse"`) is rejected with an explanatory message.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-16.md</sub>

**update_available**
- On this machine `update_available` was false (engine 0.1.45 installed, 0.1.45 newest), so the note read "The engine is the latest release" and the box stayed blank.
  <sub>2026-10-18 · 2026-10-18-15-14-continue.md</sub>

**vram**
- So the coverage line will not fire for `vram` under the current priors, which is why the balance sweep you pasted printed no `coverage:` line.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

**when flag active**
- When this flag is active, the server bypasses the strict file-presence checks for vocab.json and spins up a dedicated ScriptedWorker proxy backend instead of the real CUDA daemon.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

## Decisions taken

what was chosen, and why, oldest first


**mcp.yaml** · 2 chats
- Not caused by the rename, but worth flagging — 11 pre-existing failures surfaced by the suites: `mcp.yaml` now registers seven servers while `test_hub__shipped_yaml` asserts six; the hub merge API moved (AttributeError in 8 merge cases); the terminal allowlist doesn't catch backtick substitution; and `docs_mcp.create_file(".ssh/authorized_keys")` is accepted where the guard should refuse it.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-07-17.md</sub>
- Two env additions the running server needs regardless: I already added `TERMINAL_ALLOW_EXTRA` and `RUFF_CACHE_DIR` to `mcp.yaml`, but pid 2351071 was started with the old environment, so `terminal__run` still rejects `sudo` ("'sudo' is not on the allowlist") — as you saw in my test above.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**--balance**
- The functions actually wired into the sweep are `balance_score` (`bayesian_tune.py:159`, `:179`), `frontier` (`:1454`), the `--balance` objective (`:883`, `OBJECTIVES` at `:111`), `FAIL_STREAK=3` (`:121`, used at `:662`), and the sha1 digest in `run_log_name` (`:128`).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**--check**
- Outputs: `hypercore_max_tuning_matrix.log` (fixed-width table), `results/matrix-<date>.csv`, one log per run, and on a measured winner `max_config_<model>.sh` (env vars plus a `HC_ARGS` array) and `max_run_<model>.sh` (daemon wrapper with `--check`, `--status`, `--stop`, launching at `--max-context 262144`).
  <sub>2026-10-12 · hypercore-chat-2026-10-12-11-07.md</sub>

**--max-new**
- `optuna_tune/winner_bench.py` (new), one command in the shape of `/home/ai-user0/Strata/setup.sh --setup` / `./bench.sh`: reads the deliverable `max_run_<model>.json`, checks the card with nvidia-smi, boots the engine with that config's own args (`--serve`/`--max-new` added back), and reports per-boot lines plus medians with the off-card share, then the delta against what the sweep recorded.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-23-49.md</sub>

**--port**
- Three bugs it caught and I fixed: `status` printed nothing because `describe()` read `$pid/cmdline` instead of `/proc/$pid/cmdline`; a transient `/proc` entry vanished mid-scan and killed the script under `set -e`; and `--port` parsing used `shift` inside a `for` loop.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**-rwx--r--**
- `diff` of the new README against the previous commit: only the two added lines, nothing else changed (131 vs 129 lines) One note: `README.md` was owned by `user0` with mode `-rwx--r--`, so it was not writable by `ai-user0`.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>

**.new**
- Install path is unchanged and still safe: write to `.new`, validate with `visudo -cf`, then `mv` + `chown root:root` + `chmod 440`; on failure it removes the temp file and dies without touching the live file.
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-31.md</sub>

**.sudoers_state.json**
- Removed (12,145 bytes total, via `scrape__remove_downloads`, which reports counts): Kept in `/home/ai-user0/downloads`: `gen_sudoers.py`, `sudoers.ai-user0.fixed`, `sudoers.ai-user0.expanded`, `test_sudoers.py`, `.sudoers_state.json`, plus a new `sudoers.manifest.md`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**/etc/sudoers.d/ai-agent**
- Those needed the apt privileges I added to `/etc/sudoers.d/ai-agent`.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**/etc/sudoers.d/ai-user0**
- Current (installed): `sudoers.ai-user0.fixed`. It is byte-for-byte the logic running as `/etc/sudoers.d/ai-user0`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**/home/ai-user0/_notes/ai_setup.revised.sh**
- Done — section 5 of `/home/ai-user0/_Notes/ai_setup.revised.sh` (lines 150–222) now emits the audited sudoers configuration from `/home/ai-user0/downloads/sudoers.ai-user0.fixed`. What changed:
  <sub>2026-10-05 · strata-chat-2026-10-05-23-31.md · also in 2026-10-05-strata-chat-2026-10-05-23-31.md</sub>

**/home/ai-user0/strata/mcp.yaml**
- `sudo` is not on the terminal allowlist. I added `TERMINAL_ALLOW_EXTRA: sudo,setfacl,getfacl,visudo,apt-get,nproc` to the `terminal` server env in `/home/ai-user0/Strata/mcp.yaml`, but the running `terminal_mcp.py` process (pid 2351071) was started with the old environment, so it still rejects `sudo`.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**/usr/sbin/visudo**
- The two bugs I flagged earlier are fixed in the installed file: the visudo grant now names `/usr/sbin/visudo` (the real path), and docker is granted — `sudo -n /usr/bin/docker ps` returns an empty container table, exit 0.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**_unpack**
- The zip and `_unpack` are deleted, and on Linux `chmod(0o755)` is applied to `hypercore` and `hypercore-vision` (`:2598-2600`).
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-44.md</sub>

**balance_score**
- Three supporting pieces make the results usable rather than just a number: `knee` is defined and tested (`test_transfer.py:147-149`) but not called by `bayesian_tune.py`; the wired-in parts are `balance_score`, `frontier`, and the objective itself.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**changed source home**
- I changed the source to /home/ai-user0/HyperCore-WebUI/data/history/_old_chats and there are some new files you can add." Inter Now, the plan: 1.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**cve-2024-31083**
- A fix for `CVE-2024-31083` in X.org software in April 2024 introduced a regression, which has been fixed in xwayland 23.2.6 and xorg-server 21.1.13.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-18-39.md</sub>

**docs__delete_file**
- Idempotence and cleanup — `scrape__remove_downloads` reports files and bytes removed; `docs__delete_file` has no equivalent at all, and my test files in `/home/ai-user0/tmp … [universal] /home/ai-user0/strata-chat-2026-10-03-15-09.md … 5.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-16.md · also in 2026-10-03-strata-chat-2026-10-03-18-59.md</sub>

**find build user**
- `find build -user root -type f -delete` removed them, and the directory is writable (`touch third_party/llama.cpp/ggml/src/_probe_write` succeeded).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-14-18.md</sub>

**gen_sudoers.py**
- Keep `gen_sudoers.py` + `sudoers.ai-user0.fixed` as the source of truth; regenerate rather than edit.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**ni_run-q2_0.sh**
- Naming decision. `ni_run-q2_0.sh` is gone by design (T3 replaced it with `restart.sh` + `hypercore_<model>.sh`).
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**permissions user0 sandbox**
- Permissions are now `ai-user0:dev-sandbox` (serve 755, files 644) — all three patches applied and verified.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**py_compile**
- Verification: `py_compile` clean on every edited file; `sh -n restart.sh` clean; the affected files run green in isolation (82 passed in 21.7 s), and the full `serve/` suite is green.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**serve/assist.py**
- Patch applied (`serve/assist.py`): `_write_shared()` mirrors every allowlist write to every copy that exists and is writable, `_shared_lines()` merges the call log across copies (dedup, time-ordered), and `call_log()` reads the merged history.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**socket sanitation supervisor**
- Socket sanitation: supervisor evicts stale socket, pid-private paths, cleanup, tests added to install.sh.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-54.md</sub>

**transfer**
- It reads the engine's own telemetry, turns it into a `Transfer` record, and uses that to score and rank configurations on more than one axis: Wired in at `bayesian_tune.py:109` (import), `:159` and `:179` (`if objective == "balance": return balance_score(...)`), `:1440` (prints the winner's bus share), `:1454` (prints the frontier).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**update setup script**
- update the setup script /home/ai-user0/_Notes/ai_setup.revised.sh with the visudo configuration detailed in /home/ai-user0/downloads/sudoers.ai-user0.fixed
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-31.md</sub>

**verified bugs fixed**
- Verified: Two bugs I fixed in your version: `self._session_counter += 1` inside worker threads is load/add/store, so two threads can land on the same id and overwrite each other's event — now behind a lock, with `_next_session_id()` as the single source.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

## Measured on this machine

numbers actually measured, not estimated


**/home/ai-user0/strata/engine/strata** · 5 chats
- Current state: the card is held again — pid 3441354 `/home/ai-user0/Strata/engine/strata`, 24104/24564 MiB.
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>
- Two separate causes, both verified: a) Right now the card is full. `nvidia-smi` shows 24122 / 24564 MiB used by pid 3431164, `/home/ai-user0/Strata/engine/strata` (started 13:00).
  <sub>2026-10-14 · strata-chat-2026-10-14-13-13.md</sub>
- Blocked, and it is the next step: the card is held by `/home/ai-user0/Strata/engine/strata` (pid 3421829, 23614 MiB of 24564).
  <sub>2026-10-14 · strata-chat-2026-10-14-12-53.md</sub>
- What's blocking the live debug run right now: the card is busy — `/home/ai-user0/Strata/engine/strata` (pid 3336849) holds 24 068 MiB, 38 MiB free.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>
- So the first line the server saw was `[New LWP 187521]`, and the actual reason went to stderr, into the log, unseen: Same underlying condition as the engine failure: the 4090 is full — pid 178997 (`/home/ai-user0/Strata/engine/strata`) holds 24070 of 24564 MiB.
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**word count balance** · 3 chats
- The margin that decided the balance winner — 7.176 vs 7.064, 1.6% — is well inside that.
  <sub>2026-10-16 · resume_strata-chat-2026-10-16-03-08.md</sub>
- The balance sweep only sampled 1280 MiB, so the reserve dimension is untested.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>
- Word-count balance: roughly 90% of the article is Trump's own speech.
  <sub>2026-10-02 · strata-chat-2026-10-02-22-43.md</sub>

**nvidia-smi** · 2 chats
- The card is currently held: `nvidia-smi` reports 170 MiB free with pid 4126570 using 23974 MiB.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-23-49.md</sub>
- `nvidia-smi` reports 834 MiB free on the RTX 4090, and pid 2247278 (`/home/ai-user0/HyperCore-WebUI/serve/server.py --engine hypercore --config /home/ai-user0/HyperCore-WebUI/max_run_q2_0.json --port 8081`) has been up 55 minutes and is still serving requests.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>

**/health**
- There is an engine running. `ps` shows PID 150375 — `/home/ai-user0/Strata/engine/strata`, 24.4 GB RSS, started by `/home/ai-user0/Strata/serve/server.py --port 8080`, and it answers `/health` right now.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**/home/ai-user0/strata/serve/server.py**
- The engine starts and then dies: `/home/ai-user0/Strata/serve/server.py` is running on port 8080 (pid 108248, engine pid 108250) and is holding ~27 GiB.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**/home/ai-user0/vbtest/triage.sh**
- I wrote and tested a helper at `/home/ai-user0/vbtest/triage.sh` (uses ffprobe 8.0.1 + jq, both installed) that reports bitrate per megapixel: Rule of thumb (my heuristic, not from the docs): 1080p files under ~3 Mbps total are already well encoded — skip them; 1080p above ~6 Mbps or 4K above ~15 Mbps are the ones that give you the 50% win.
  <sub>2026-10-04 · strata-chat-2026-10-04-07-56.md</sub>

**between intent leaders**
- The gap between intent (93% of IT leaders plan to deploy agents within two years) and operational reality is the defining story of 2026, not the capability curve.
  <sub>2026-10-03 · strata-chat-2026-10-03-12-03.md</sub>

**build-shared/hypercore-worker**
- Also, `--short --dry-run` no longer runs a real calibration; it says so and falls back to the staged sweep: The next blocker you will hit `nvidia-smi` reports 28 MiB free right now: `serve/server.py` (pid 1646195) is running with `build-shared/hypercore-worker` holding the card.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-13-18.md</sub>

**card free 3679801**
- The card is not free. PID 3679801 (`/home/ai-user0/Strata/engine/strata --serve --pack …/q2_0`) holds 23 804 MiB of 24 564 MiB.
  <sub>2026-10-15 · strata-chat-2026-10-15-09-32.md</sub>

**coulomb pseudopotential conventionally**
- `μ` — the Coulomb pseudopotential — is not. It is conventionally fixed to 0.10 (McMillan) or computed by constrained RPA, which is expensive and has ~30–50% uncertainty.
  <sub>2026-10-07 · strata-chat-2026-10-07-18-16.md</sub>

**handshake succeeds engine**
- The handshake now succeeds; the engine then exits on its own memory guard: There is already a Strata server resident on this box — pid 113114, `/home/ai-user0/Strata/serve/server.py --engine strata --config /home/ai-user0/Strata/strata-q2_0.json --port 8080`, holding 24.8 GB of RAM, with the 4090 at 23.9/24.5 GiB.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**prompts must repeating**
- Prompts must be new. Repeating three prompts raised VRAM hits to 99.2% and left 2.6 experts on the CPU (Maya-L, 9 GPUs) — the CPU lane was measured with essentially nothing on it.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**since subset includes**
- Since 0.1.27 the subset includes every Chinese, Japanese and Korean token (106,299 ids), so answers in those languages are 15-38% faster (Q2_0, RTX 5070).
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

**src/prefill/**
- The verify window is now proven clean, so the fault is in `src/prefill/` (the batched group copy at `prefill.cpp:2614` is where the reported kernel is), and it needs the card free to reproduce — the Strata engine (pid 3443824) currently holds 24104 of 24564 MiB.
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>

**winner_bench bayesian_tune written**
- `--max-new 512` (`winner_bench.py:566`, `bayesian_tune.py:686`, written into the deliverable by `search_space.py:208`) sits just below that line, which is exactly why the rungs land at 10.0-10.2%.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

## From outside this project

figures a chat took from a source we did not run here; check the source before repeating one


**boundary.yml**
- What's left is external, not code: a workflow-scoped token so `boundary.yml` can land, and published releases so the README asset links resolve.
  <sub>2026-10-10 · strata-chat-2026-10-10-08-05.md</sub>

## Gotchas

things that bit once and will bite again


**--out**
- `gen_sudoers.py` writes to `--out` and prints to stdout; the header of both generated files says "Generated by gen_sudoers.py - regenerate, do not hand-edit."
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**/etc/alternatives/visudo**
- `/usr/bin/visudo -c -f /etc/sudoers.d/ai-user0` never matches: `visudo` is at `/usr/sbin/visudo` (`ls -l /usr/sbin/visudo` → symlink to `/etc/alternatives/visudo`); `/usr/bin/visudo` does not exist.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**/etc/sudoers.d**
- It grants the wrong direction if root access was intended, but changing it needs root (`/etc/sudoers.d` is `root:root 0440`, and my sudo grants don't include `install`, `cp`, or `tee` on that path).
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**/home**
- Two defects to fix before it can run: `bash --no-login` isn't a real flag, and `/home` is not writable — the same permission wall that broke my earlier test — so the state directory must not sit directly under the root.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>

**/home/ai-user0/.ssh**
- Or, from the peer side: If it still falls back to password after that, check on the peer: `authorized_keys` must not be group-writable (sshd silently rejects it otherwise), and `/home/ai-user0/.ssh` must not be world-accessible.
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>

**/usr/bin/visudo**
- My sudoers file has a dead line: `visudo` is at `/usr/sbin/visudo`, not `/usr/bin/visudo`, so that rule never matches.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**active**
- With `HYPERCORE_DMA_BATCH` on, `landed` is never advanced — the window is saved only by `active` being cleared at `:7019`.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**ai-agent**
- The breakage is that `mcp.yaml` was never repointed from `ai-agent` to `ai-user0`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**ai-user0**
- If you want to edit it with the docs tools, the directory needs to be made writable for `ai-user0` (`chmod 775` on the folder, which requires root — the scoped sudoers grants don't include a general `chmod`).
  <sub>2026-10-06 · strata-chat-2026-10-06-18-22.md</sub>

**also caught shell**
- Also caught a shell gotcha worth recording: `pkill -f "python3 -m lm_eval"` matched the ssh `bash -c` wrapper itself and killed the run before it started — kill and run must be separate ssh calls.
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>

**delete_file**
- `edit_file` bypasses the ownership guard that `write_file`/`delete_file` enforce `serve/docs_mcp.py:255` and `:276` call `tool_permissions.decide(...)`; `edit_file` (`:206`) does not, and then calls `note_created(target)`, so editing a file the session never created both succeeds and grants the session write/delete rights over it.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**httptransport**
- If you want the 54-era leftovers prevented structurally rather than by a per-class `terminate`, the durable fix is for `HttpTransport` to own its server process the way `StdioTransport` does — worth doing only if you plan to run HTTP-transport servers from `mcp.yaml` in production too.
  <sub>2026-10-10 · strata-chat-2026-10-10-19-47.md</sub>

**mcp.yaml**
- Known gaps — the guard is not a shell parser, gdb needs `-g` for useful output, `ptrace_scope` limits breakpoints, Docker volumes intentionally still reference user0, and `mcp.yaml` requires a restart.
  <sub>2026-10-03 · strata-chat-2026-10-03-19-51.md</sub>

**restart.sh**
- `watchdog_state()`'s own docstring says "The server logs this; it never acts on it", and the only restart entry points are `handler.restart()` (`serve/server.py:1346`) and `restart.sh`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-22-55.md</sub>

**same trust number**
- Same "don't trust one number" rule. Calibrate only keeps a setting that beats the default by >3% (5% for the drafter) in an interleaved re-measurement; the tuner stops after three same-shaped failures and prunes already-seen points, for the same reason — a busy GPU reads as a flat landscape.
  <sub>2026-10-15 · strata-chat-2026-10-15-19-50.md</sub>

**sudo**
- `sudo` is now allowed but still needs a password. `sudo -n apt-get update` returns "interactive authentication is required" — because the server process runs as user0, not ai-agent, so ai-agent's NOPASSWD rules don't apply.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

## Still open

not done, deferred, or waiting on a decision


**condense_at**
- Remaining open items are the sudoers line-41 direction, the INTERPRETER group decision, and the unstarted Medium item — token accounting/compaction in the tool layer, which would sit next to the existing `condense_at` / `condense_chars` settings in `mcp.yaml`.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**ruff_cache_dir**
- Remaining open items are the sudoers line‑41 direction, the INTERPRETER group decision, and the `RUFF_CACHE_DIR`/`TERMINAL_ALLOW_EXTRA` settings that still need a hub restart to take effect.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-21-39.md</sub>
