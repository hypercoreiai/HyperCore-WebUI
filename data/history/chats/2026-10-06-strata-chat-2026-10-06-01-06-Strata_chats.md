# strata-chat-2026-10-06-01-06.md

2026-10-06 · 57 KB · 1 requests · topics: nothing worth keeping

## What was asked
- convert /home/ai-user0/Strata/zprompt.json into an actual json formatted system prompt

## What came of it
- The `terminal` tool runs commands inside a sandbox with these permissions:
- Writes: `/tmp` is writable but is cleared between `terminal` calls{{#if worktrees}}.
- Native Windows project paths are routed through WSL's `/mnt/<drive>/...` filesystem view.
- Writes: `/tmp` inside WSL is writable but is cleared between `terminal` calls{{#if worktrees}}.
- Writes: a per-thread temporary directory exposed via `$TMPDIR`, `$TMP`, and `$TEMP` is writable and persists across `terminal` calls in this thread{{#if worktrees}}, along with these project directories:
- Host-specific network grants cannot be enforced, and such requests are rejected; use `allow_all_hosts: true` when the command genuinely needs network access.
- `allow_all_hosts: true` — lift the network restriction entirely: outbound access to any host over any protocol, so SSH, FTP, and raw sockets work too (unlike `allow_hosts`, which is HTTP/HTTPS-only).
- `fs_write_paths: ["/abs/or/worktree-relative/path", ...]` — allow writes to specific paths (each directory grants its whole subtree).
- `allow_fs_write_all: true` — allow unrestricted filesystem writes except protected Git metadata.
- If a command needs to update `.git`, linked worktree metadata, refs, the index, hooks, local Git config, or other Git metadata, request `unsandboxed: true` with a reason.
- For read-only Git operations, prefer flags that avoid optional metadata writes where possible, such as `git --no-optional-locks status` instead of `git status`.
- `name` and `description` use `{{...}}` and are HTML-escaped as defense in depth.
- <details><summary>Tool home / read</summary> ``` /home/ai-user0/Strata/zprompt.json You are the Zed coding agent running inside the Zed editor.
- text /home/ai-user0/Strata/zprompt.system_prompt.json json "system_prompt": "You are the Zed coding agent running inside the Zed editor.
