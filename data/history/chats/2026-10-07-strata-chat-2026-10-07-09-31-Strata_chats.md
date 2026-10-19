# strata-chat-2026-10-07-09-31.md

2026-10-07 · 1212 KB · 14 requests · topics: deployment-permissions, engine-internals, hardware-environment, mcp-tooling, memory-vram, performance-benchmarks, prompting-behaviour, setup-configuration, testing-quality, web-ui-monitor

## What was asked
- A [Camoufox](https://camoufox.com/stealth/should_be_linked': True) or [Invisible Playwright](https://github.com/feder-cr/invisible_playwrightshould_be_linked': True) is a custom-compiled, C++ source-patched version of Firefox designed to bypass advanced anti-bot and web-scraping protections.
- Write a mcp server that employs the browser project for future use
- restarted test new tool
- How fast is Strata on this PC?
- did you use the strata built in mcp server?
- what tools does the built in strata mcp server provide
- where is the models config file
- i noticed that in the web UI About tab the model config is exposed. All the current settings are default, how does that correlate to the actual config file.
- review all the settings and suggest improvements
- apply the safe ones now
- restart the server
- why does the model read the prompt after every turn? Is the prompt not in memory?

## What came of it
- The distinguishing mechanism is where the spoofing happens — C++ rather than injected JS — which is what defeats `Object.getOwnPropertyDescriptor` / `toString()`-returns-`[native code]` checks.
- Sync Async The `browser` object is a `playwright.sync_api.Browser` / `playwright.async_api.Browser` - every Playwright method works as-is.
- Full list of pinnable keys, how pinning interacts with the Bayesian sampler, and common patterns are in [docs/pinning.md](docs/pinning.md).
- The installed command is `invisible-playwright`, with a hyphen.
- `python -m invisible_playwright` works identically and needs nothing on PATH.
- Nothing browser-related is imported at start-up: `BrowserSession` opens the browser on the first tool that needs it, holds it under a lock, and records `engine`, `seed` (so a run can be replayed), `last_url` and `last_error`.
- Guardrails are the same as `web_mcp`: http(s) only, no userinfo, hosts that resolve to private/loopback/link-local/reserved addresses are refused before the browser is opened.
- I installed the engine on this box — `pip install --break-system-packages invisible-playwright` → wrapper 0.27.0, engine fetched — and the server reached the launch step, which reports: `apt-get install xvfb` timed out against the tool's 90 s cap, so the real-engine path is not yet exercised — the live case skips with that sentence rather than failing.
- Run `STRATA_TEST_LIVE=1 python3 serve/mcp/tests/run_all.py --server browser` once `xvfb` is present to close that gap.
- Camoufox (`pip install camoufox`) and stock Playwright Firefox are the other two paths the adapter will pick up automatically.
- Playwright's sync API is thread-affine, but `stdio()` dispatches each `tools/call` on its own thread (copied from `web_mcp.py`).
- Playwright's sync API is thread-affine, but `stdio()` dispatches each `tools/call` on its own thread (inherited from `web_mcp.py`, where that's harmless because Firefox/Marionette is just a socket).
- A single worker owns the engine; only `status` answers on the caller thread: `close()` runs `_reset` through the worker (so the browser is closed on the thread that opened it), then stops the worker — the next call builds a session on a fresh thread with a fresh fingerprint.
- Verified directly: New case `test_browser__calls_share_one_thread` chains fetch → links → probe → screenshot, and the live case now does the same against the real engine.
- Every tool, driven against the real patched Firefox: Two honest caveats: `webgl.vendor`/`renderer` come back `null` in headless without a display, so the probe reports what's actually there rather than assuming a value; and Camoufox isn't installed here, so `invisible_playwright` (wrapper 0.27.0) is the engine the adapter selects — the `camoufox` and `playwright` branches stay as fallbacks.
