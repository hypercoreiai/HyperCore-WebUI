# data/history: the accumulated development state

What was learned across the chat transcripts, distilled by topic instead of by chat. A new chat appends to the topics it touched; nothing has to be re-read from the raw transcripts to know the current state.

## The pipeline, in order
1. `_old_chats/*.md` - the raw transcripts. The Chat tab's "Save to history" button writes here (serve/chat_archive.py), so the archive is the input, not the output.
2. `chats/<date>-<name>.md` - one transcript distilled: what was asked, what came of it.
3. `topics/<topic>.md` - every fact, decision, measurement, warning and open item about a topic, grouped by the thing it is about, each dated and attributed.
4. `notes/<topic>.md` - the current state of that topic, hand-maintained, kept by the builder and inserted at the top of the topic file.
5. `index.json` and `sources.md` - the machine-readable index and the reverse index, category by transcript.

## Why the extraction is agnostic
- A sentence is kept only when it names something concrete (a file, a flag, an identifier, a number with a unit) and states a fact about it. Thinking out loud, questions and tool output are dropped: the working is not the finding.
- API keys, account addresses and handles are replaced with markers before anything is read, so the history can be shared without leaking a secret or a person.
- Every path is rewritten to the one that is actually reachable from this repository, so a fact learned under an old project name is still applicable.
- Restatements of the same finding are merged into one entry, newest wording kept, with every transcript that said it retained as provenance.
- Identical transcripts found in more than one source folder are read once.

## Path aliases the builder applies
A transcript written against an older tree names paths this checkout does not have. Each alias is rewritten to the directory that is actually reachable here; anything not in the table is only made absolute.
- `HyperCore-Tune` -> /home/ai-user0/HyperCore-WebUI/optuna_tune
- `HyperCore-Tune-Maya` -> /home/ai-user0/HyperCore-WebUI/optuna_tune
- `HyperCore-Tune-maya` -> /home/ai-user0/HyperCore-WebUI/optuna_tune
- `Maya-data` -> /home/ai-user0/Maya-data
- `Q2_0` -> /home/ai-user0/Q2_0
- `Strata` -> /home/ai-user0/Strata
- `Strata-data` -> /home/ai-user0/Strata-data
- `Strata_chats` -> /home/ai-user0/HyperCore-WebUI/data/history/_old_chats
- `black_box` -> /home/ai-user0/HyperCore-WebUI
- `black_box_10132026` -> /home/ai-user0/HyperCore-WebUI
- `black_box_chats` -> /home/ai-user0/HyperCore-WebUI/data/history/_old_chats
- `blackbox` -> /home/ai-user0/HyperCore-WebUI
- `cp_black_box` -> /home/ai-user0/HyperCore-WebUI
- `project-maya` -> /home/ai-user0/project-maya

`_old_chats` is the archive the Chat tab writes to; the engine and its model data are not another project, they are what this project runs, so their paths are kept and made absolute.

## Layout
- `topics/<topic>.md` - one file per category; entries are dated, attributed, and grouped by the thing they are about
- `notes/<topic>.md` - the hand-written current state, inserted at the top of each topic
- `chats/<date>-<name>.md` - one file per transcript: what was asked, what came of it
- `index.json` - the machine-readable version of both
- `sources.md` - the reverse index, category by transcript

## Categories
- `topics/deployment-permissions.md` - Deployment, paths and permissions (230 entries)
- `topics/mcp-tooling.md` - MCP servers and tools (212 entries)
- `topics/web-ui-monitor.md` - The web app and the Monitor (192 entries)
- `topics/engine-internals.md` - Engine internals (178 entries)
- `topics/memory-vram.md` - Memory, VRAM and RAM (163 entries)
- `topics/setup-configuration.md` - Setup, run configs and defaults (163 entries)
- `topics/hardware-environment.md` - Hardware and environment (144 entries)
- `topics/testing-quality.md` - Testing and code quality (129 entries)
- `topics/issue-log.md` - Issues, root causes and fixes (60 entries)
- `topics/performance-benchmarks.md` - Performance and benchmarks (40 entries)
- `topics/prompting-behaviour.md` - Prompting, agents and turn behaviour (34 entries)
- `topics/general-knowledge.md` - General knowledge outside the project (20 entries)
- `topics/open-decisions.md` - Open decisions and deferred work (18 entries)

## Rebuild
```bash
python3 tools/build_history.py            # from the default transcript trees
python3 tools/build_history.py --topics           # counts only, writes nothing
python3 tools/build_history.py --source a,b       # read two folders in one pass
python3 tools/build_history.py --out /path/to/history
```

The extraction is deterministic: the transcripts are the source of truth. Anything added by hand to a topic file is lost on a rebuild; the current state lives in `notes/<topic>.md`, which the builder reads and keeps.

