# strata-chat-2026-10-04-03-57.md

2026-10-04 · 894 KB · 16 requests · topics: deployment-permissions, mcp-tooling, prompting-behaviour, testing-quality

## What was asked
- prompt (264177 tokens) leaves no room to answer in the context (262144); requests are never truncated
- 4,072 tokens · 91.4 tok/s · projection on · 15 tool calls · stopped at the limit of 8 tool rounds (mcp.max_rounds)
- write the trimmed mcp.yaml
- change mcp.max_rounds to 20
- retest all tools
- 5,027 tokens · 86.5 tok/s · projection on · 43 tool calls · stopped at the limit of 8 tool rounds (mcp.max_rounds)
- what do you see in the image
- is there a way to automatically reset after the limit has been hit besides me typing continue
- Is there a way to cache important data from the context and then removing all the excess tokens to prevent premature exit
- implement C
- retest all tools
- review the console text, and comment on if the current system is working efficiently

## What came of it
- <details><summary>Tool home / read</summary> ``` /home/ai-user0/Strata/docs/DETAILS.md The technical side of Strata: every measured number, the API, images, all settings and how the engine works.
- KV streaming (engine 0.1.5): at 64K and more, setup keeps the context's KV cache in RAM and only the part the attention reads in VRAM (`--kv-resident 32768`), so more experts fit on the GPU.
- The cache still follows the conversation (`--adapt-every`): a swap copies the evicted expert back from VRAM into the RAM place of the one that replaces it, so the RAM copy keeps holding exactly what the GPU does not.
- With a page-locked copy the GPU also takes its usual share of the misses over PCIe (`--pcie-frac`), as with enough RAM; `--pcie-frac 0` (or `STRATA_RESIDENT_PIN=0`) gives the mapp [showing 8000 of 8001 characters read; the rest is not included.
- Keep `max_rounds` at 8 or lower — raising it makes the prompt grow further; the fix is fewer/smaller results, not more rounds.
- If you genuinely need a bigger window, re-run setup with a larger `--context`; 262,144 is the model's trained ceiling, so anything above it needs rope scaling, which setup derives for you (`START-HERE.bat --setup --context 393216` picks yarn and the factor).
- Check the RAM headroom first — at 262K the context is already the memory limit for the larger quants (`docs/DETAILS.md`, speed tables).
- Restart after editing `mcp.yaml`; `GET /mcp` reports the live `max_rounds`/`max_result_chars` and `GET /props` reports `max_context`, so you can confirm what the server is actually using.
- Written to `/home/ai-user0/Strata/mcp.yaml` — parses cleanly, and every key is one `serve/mcp.py` actually reads (`mcp.py` lines 629–637).
- What changed Two comments were added at the top explaining the context budget, and one above `TERMINAL_MAX_OUT`.
- Next step: restart Strata — `mcp.yaml` is read at startup.
- If 6k is too tight for your workloads, the safe ceiling at a 262K context is roughly `(262144 − 8 − prompt − answer) / rounds` characters; a 32K prompt with a 4K answer over 8 rounds gives about 28k chars per round, so 6k is deliberately conservative.
- `run terminal {"command": "cp ai-agent/tool-test/native/main.cpp ai-agent/tool-test/…"` — the command is cut off with an ellipsis.
- This is the `cp` I ran to make `main2.cpp`.
- The footer also confirms the point from before: the running server still has `max_rounds = 8`, even though `/home/ai-user0/Strata/mcp.yaml` now says 20 — `mcp.yaml` is read once at startup.
