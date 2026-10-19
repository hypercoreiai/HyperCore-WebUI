# How this box is set up

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
