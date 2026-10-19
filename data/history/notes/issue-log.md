# Recurring failures and what they turned out to be

**"no frame for 30.0s" / StreamStalled.** The supervisor used the 30 s submit-ack timeout as the
per-frame wall. A 116k-token prompt takes ~31 s to read, so a healthy request was declared stalled
in the middle of its prefill. The silence wall now belongs to the supervisor (`self.silence_s`,
from the config's `engine_silence_s`, default 300 s; 0 waits as long as the request needs) and
`stream()` measures from the last frame. `timeout_s` is only the ack wall.

**The agent dropdown showed only the system prompt.** The server looked for `ROOT/prompt.json`;
the profiles are in `prompts/prompt.json`. `find_prompt_file` now checks both.

**Monitor cards blank (Reused, Tok/s, hit rate, prefill).** The terminal summary's `prompt_read`
and `prefill_ms` were parsed nowhere, so the derived fields never existed. `derive_monitor_field`
in `serve/ipc_parser.py` now computes what the worker does not report.

**"Exception occurred during processing of request".** An unhandled exception inside `do_POST`
left a half-written SSE body and the stdlib traceback. The handler now records the failure, closes
the stream with an error event, and returns a 500 JSON only when nothing was sent yet.

**401 from the web page.** A server started with an API key serves a page that does not know it.
The page now carries the key it requires.

**Worker socket EADDRINUSE.** A Unix socket file outlives the process that bound it; the next bind
on the same name fails. Two supervisors sharing a name also share the `{socket}.config.json` side
file. Socket names are pid- or port-namespaced for this reason.

**A stalled link is not a dead link.** `WorkerUnreachable` and `StreamStalled` are different
exceptions and the HTTP layer must distinguish them; conflating them caused needless engine
restarts.

**The reason a health route carries is a state, not a label.** A stand-in's reason is true only
while nothing has been asked for. Once a real engine has been taken and it is not running, the
cause belongs to that engine, and the only thing a worker adapter can say is its death note - it
has no `status()`. Reporting the stand-in's sentence after that is reporting a fact that has
already stopped being true.
