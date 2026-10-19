# Prompting, agents and turn behaviour

Accumulated from 21 transcripts, 34 entries. Built 2026-10-19. Newer chats append here; within a section the entries are grouped by the thing they are about, newest statement first.


## Current state

Maintained by hand; the builder keeps it in place.

**`prompt.json` is the profile file.** One top-level `system_prompt`, then one object per agent.
The server's `/prompts` returns the base plus the profiles; the web app builds the dropdown from
that. The file lives beside the server or in `prompts/` — `find_prompt_file(ROOT)` checks both.
A config that points at only one of them is why the dropdown showed a single entry.

**The system prompt carries two standing tasks (prompt.json v9).** Every chat, whichever profile
is chosen, is told to (1) distil the transcripts in `data/history/_old_chats/` into `chats/`,
`topics/` and `notes/` with `tools/build_history.py`, and (2) keep the state files
(`.mcp_test_state.json`, `data/history/notes/`, `CHANGELOG.md`) matching what has actually been
done, closing anything left in a queue with a reason. Both have an explicit "before calling it
done" check. The section is placed after `resources` so the tasks name the files they operate on,
and `tests/test_prompt_standing_tasks.py` checks that the file parses, that exactly two tasks are
present, that every path they name exists in the repository, and that the rendered prompt stays
under the 20,000-character ceiling `serve/prompts.py` cuts profiles at (it is 8,309 characters).

**Thinking is opt-in per request.** `reasoning_effort` and `reasoning_budget_tokens` control it;
a request that reaches max tokens while still thinking has no answer, and the server says so
rather than returning an empty completion. `effort_position` decides where the effort instruction
is placed in the prompt.

**The turn-tail check.** When a turn would end with `finish=stop`, the server appends one short
user turn asking whether every task in the turn is done. A yes ends the turn; a no, an ignored
question, or more work keeps it open and the model continues, up to `turn_tail_checks` times
(default 3, capped 10). It is wired into the OpenAI chat, Anthropic and Responses routes. A turn
that ends on a length stop, a cancel, a forced tool call, or with no text at all is not checked.
Each check is another read of the conversation, so it costs prompt time.

**Refusal and persona are configured, not inherent.** The uncensored profiles exist because the
base model refuses things it should not; the profiles are the mechanism, not the model.


## What is true now

how the thing works, as the chats established it


**--turn-tail-check** · 6 chats
- What was already there `tools/turn_tail_measure.py` (261 lines, untracked at session start) is the comparison harness: it runs one multi-part prompt against a control server and against a second server it starts in attach mode against the same model with `--turn-tail-check` on, so no second copy of a tens-of-GB model is loaded.
  <sub>2026-10-18 · 2026-10-18-12-19-continue.md</sub>
- `tools/test_setup_config.py`: new `ServerDefaults` class (new install gets all three; `--host 127.0.0.1 --api-key s3cret --no-turn-tail-check` wins; an existing config keeps its own values; the run script carries `--turn-tail-check` and honours a config that turns it off).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>
- Opt-in via `--turn-tail-check` or `"turn_tail_check": true`; the question text is overridable with `--turn-tail-prompt` / `turn_tail_prompt`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>
- One caveat: the turn-tail check is off by default — the running server was started without `--turn-tail-check`, so it won't fire until you add the flag or set `"turn_tail_check": true` in the profile.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>
- Turn-tail check (`--turn-tail-check`, or `"turn_tail_check": true` in the model's config — on by default for anything `setup` writes): when a turn would end, the server appends one short question asking the model to check its answer against the request.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md · also in hypercore-chat-2026-10-11-08-01.md</sub>
- `run_flags()` / `write_run_script()` read the config and emit `--turn-tail-check` in `run-<model>.sh` / `.bat`; a config that says `false` is respected.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-07-09.md</sub>

**--no-turn-tail-check** · 2 chats
- To check / turn it off: `GET /v1/status` reports `"turn_tail": {"enabled", "max_checks", "custom_prompt"}`; set `"turn_tail_check": false` or `"turn_tail_checks": 0` in the run config, or launch with `--no-turn-tail-check`.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>
- `--no-turn-tail-check` as a per-model setting. The flag exists and is remembered in the config; a global off switch for a whole install is not.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-21-39.md</sub>

**ni_hypercore-q2_0.json** · 2 chats
- A reply that produced zero tokens keeps `finish == "length"`, so the #530 hint at 2787 fires and blames `reasoning_budget_tokens` (24756 in `ni_hypercore-q2_0.json`) — even though nothing was generated.
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>
- The `[hypercore] the reply reached max tokens while still thinking` line is just the advisory at `serve/server.py:2668`, and `ni_hypercore-q2_0.json` has no `reasoning_budget_tokens` key (checked with `jq`), so nothing caps the thinking by default.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-22-55.md</sub>

**--api-key**
- `--no-turn-tail-check` is the off switch; `--host` / `--api-key` still win.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-07-09.md</sub>

**--apply_chat_template**
- Concrete next step: `Qwen/Qwen3-0.6B` is a real instruct model that fits the 1060 and needs no auth — I can run it with `--apply_chat_template` on generation tasks (gsm8k, mmlu) and loglikelihood tasks at `batch_size 2`.
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>

**--tail-role-token**
- You're on 0.1.40, so it will apply; on startup it prints `effort_position end: a request's non-default reasoning effort goes right before the answer, so changing it keeps the cached conversation` and adds `--tail-role-token` to the args.
  <sub>unknown · unknown-context.md · also in strata-chat-2026-10-07-09-31.md</sub>

**.calibrate_progress.log**
- `.calibrate_progress.log` shows: The calibrator starts its own worker while the live server (pid 998337, started 14:04, `--port 8081 --turn-tail-check`) already holds the model, so the second engine cannot get VRAM.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-14-18.md</sub>

**/health**
- Live state: pid 793996 on port 8081, started from `run-q2_0.sh` with `--host 0.0.0.0 --turn-tail-check`; `/health` reports `api_key: true`, and a streamed request emitted `tail_check {"result":"continue","checks":1,"max_checks":3}`, so the turn-tail check is active.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>

**/prompts**
- Docs — a bullet in README "Using it", an Agent prompts section in `docs/DETAILS.md` (with the shape of a profile and the rules: any top‑level key after `system_prompt` is an agent, name comes from `agent_profile.name`, empty entries are skipped, selection is remembered per browser, >20,000 chars is sent cut off), and a `/prompts` row in the endpoint table.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-56.md</sub>

**_answer**
- The thinking budget is enforced in tokens (`produced >= req_think_budget` → force `</think>`), and the loop guard uses different windows while thinking (`STRATA_GLM_LOOP_THINK` 256 vs `_ANSWER` 1024).
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**also important max_new**
- Also important: with `max_new 512` and a thinking budget, the reasoning block must actually be produced.
  <sub>2026-10-15 · strata-chat-2026-10-15-19-50.md</sub>

**anthropic_thinking**
- Four have real consequences on this machine: `reasoning_budget_tokens`, `anthropic_thinking`, `sampling.` are genuinely optional — leaving them unset inherits the model's defaults, which is fine.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**bytetokenizer**
- Against the real `serve/chat_template.jinja` and the seven servers in `mcp.yaml`, using the `ByteTokenizer` from the test harness (one id per byte, so these are character counts; the model's BPE gives roughly a quarter): The number moves with the surface, which is the point.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>

**chat template renders**
- The chat template renders the tool block first, then the profile, then history (`serve/chat_template.jinja:72`), and reuse is prefix-strict — anything that changes at the front invalidates everything after it.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**chat_template.jinja**
- `chat_template.jinja` splits on the literal `<think>`/`</think>` strings, which is consistent with them being atomic vocab entries.
  <sub>2026-10-15 · strata-chat-2026-10-15-21-07.md</sub>

**context**
- New tests assert the request body contains no `temperature`, `top_p`, `top_k`, `reasoning_effort`, `seed` or `context` field, and that a caller can still set the cap.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**messages**
- On load or New Chat, insert a system message into the chat showing the injected system prompt + selected agent prompt (rendered as a collapsed card), and set the `messages` array to include `{role: "system", ...}` so apiMessages() includes it as the first message in the API request.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-56.md</sub>

**needs conditions transcript**
- That needs the conditions from the transcript: a ~90k-token shared prefix (`prompt 88903 tokens = 0 reused + 89031 read` is in the log right now from the other client), thinking enabled, and `reasoning_budget_tokens: 24756` reached — at which point `serve/server.py:2682` injects `"\n\nI have thought about this long enough; time to give my answer.\n</think>\n\n"` into the resumed prefix.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**ni_run-q2_0.json**
- Your live archive still uses `ni_run-q2_0.json` configs — do you want the personal variants kept as an example, or should the live system be migrated to the standard names?
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**ni_strata-q2_0.shared-settings.json**
- Diff against the backup — exactly the four changes, nothing else: Plus the profile alignment: `strata-q2_0.shared-settings.json` copied to `ni_strata-q2_0.shared-settings.json`, so the Chat-tab defaults (temperature 0.7, top_p 0.95, top_k 20, reasoning_effort high) now belong to the profile that's actually running.
  <sub>unknown · unknown-context.md</sub>

**reasoning_budget_tokens**
- It is a reporting artifact; the real signal is the worker's exit, and `reasoning_budget_tokens` (24756) has nothing to do with it.
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>

**reasoning_effort high second**
- `reasoning_effort=high` is the second-biggest cost after prefill: round 14 alone produced 1,467 reasoning tokens.
  <sub>2026-10-04 · strata-chat-2026-10-04-03-57.md</sub>

**reasoning_wrap_up**
- The config has `reasoning_budget_tokens: 24756` → thinking is force-closed with an injected `REASONING_WRAP_UP` string, which is appended to the prompt for the next pass.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**thinking budget line**
- The "thinking budget" line is a mis-attribution, not a diagnosis. `serve/server.py:2502` initialises `finish = "length"`; it is only overwritten by a stop token, a cancel, a disconnect, or an engine error.
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>

**yi_strata-q2_0.shared-settings.json**
- The values in `yi_strata-q2_0.shared-settings.json` (temperature 0.7, top_p 0.95, top_k 20, reasoning_effort high) are simply dead.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md · also in strata-chat-2026-10-07-09-31.md</sub>

## Decisions taken

what was chosen, and why, oldest first


**browser**
- Switching agent profiles, adding an MCP server (I added `browser` this session — the tool catalog is part of the system prompt), or changing reasoning effort when it's rendered at the top of the prompt.
  <sub>unknown · unknown-context.md · also in strata-chat-2026-10-07-09-31.md</sub>

## Gotchas

things that bit once and will bite again


**/home/ai-user0/hypercore-webui**
- And in `/home/ai-user0/HyperCore-WebUI` it is on in every run config: The Strata config that's running now has no such key, so the tail check is off here (`svc.turn_tail_check = False` unless the config or the flag sets it).
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>
