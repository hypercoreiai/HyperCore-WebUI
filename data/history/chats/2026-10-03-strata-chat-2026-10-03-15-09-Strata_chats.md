# strata-chat-2026-10-03-15-09.md

2026-10-03 · 1078 KB · 5 requests · topics: mcp-tooling, testing-quality

## What was asked
- Run a simple test on all your tools and report any improvements that may make them more effective
- make a list of terminal tools that would allow you to develop complex scripts and object oriented programs.
- build the first 2 tiers
- test terminal tools
- retest terminal tools

## What came of it
- Use `--disable-api-nodes` to disable the optional paid [Comfy API nodes](https://docs.comfy.org/tutorials/api-nodes/overview) and force all built-in functionality to stay offline.
- `docs__create_file` path default is the main bug. Relative paths resolve against `/home`, which is not writable, so the most natural call (`path: "notes.md"`) fails with `[Errno 13] Permission denied`.
- The same ambiguity exists for `docs__list_directory`, whose default `.` is `/home`.
- No delete, move, or append in the docs tool. I couldn't clean up my test artifacts — `/home/ai-user0/tmp/tool_test_notes.md` and `/home/ai-user0/tmp/newsub/nested.md` are still there.
- `scrape__remove_downloads` shows the pattern works; `docs__delete_file` is the obvious gap.
- An `append` mode would also avoid re-emitting a whole file to change one line, which is expensive for long documents.
- Video/audio metadata is missing. `scrape__status` reports `ffprobe: false`, so `file_info` can't give duration or codec for the media types the tool claims to handle.
- Overlapping tool families. `home__read` handles pdf/docx/config; `docs__read_file` is text-only.
- Inconsistent error shapes. `Security Error: [Errno 13] Permission denied: …`, `error: path must be a directory inside /home`, and `Error: File … already exists` are three different conventions.
- `web__status` is read-only. It can tell you Firefox isn't running but nothing can start or stop it, and there's no way to go back to a previous page or fetch raw HTML/`llms.txt` — that page's own "Fetch the complete documentation index at: /llms.txt" line was visible but unreachable as a structured call.
- <details><summary>Tool home / search</summary> ``` [universal] /home/ai-user0/.pyenv/README.md … Each Python version is installed into its own directory under `$(pyenv root)/versions`.
- `$(pyenv … [universal] /home/ai-user0/.pyenv/COMMANDS.md … root directory where versions and shims are kept.
- Displays the directories where the given Python versions are installed, separated by colons … [linux] /home/ai-user0/.pyenv/man/man1/pyenv.1 … Python installation\.
- .fi .P For example, you might have these versions … [universal] /home/ai-user0/.pyenv/plugins/python-build/README.md … You may also tab-complete available Python versions if your pyenv installation is properly configured.
- The `docs__create_file` bug I found earlier is a symptom of this: I had no way to `mkdir` or check writability, so I guessed.
