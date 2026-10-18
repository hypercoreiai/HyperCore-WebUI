"""serve/prompts.py - the agent prompts the web app offers, read from prompt.json.

`find_prompt_file(ROOT)` is where the server gets its file: `prompt.json` beside the server, or the one
in the `prompts/` folder beside it, which is where this repo ships it. Either way the file is one JSON
object: a `system_prompt` that every chat starts with, and one entry per agent after it
(`expert_coding_agent`, `expert_research_agent`, ...). `GET /prompts` hands that list to the web app,
which builds its agent dropdown from it - so adding a profile to the file adds a choice to the menu with
nothing to change in the page or in the code. A profile is any top-level entry; its text is the entry
rendered as Markdown, and the name shown in the menu is its `agent_profile.name` when it has one, else the
key written out. A key starting with `_` is not a profile: it is where the file keeps its own notes and
version.

A profile may use `{{current_date}}`, `{{current_year}}` and `{{current_month}}`. They are filled from the
server clock when the prompt is rendered, so a profile never hard-codes a year that goes stale.

The file is re-read when it changes (its mtime and size), so a new profile appears without restarting the
server. A missing or broken file is not an error: the chat simply has no prompts to inject.
"""
from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path

SYSTEM_KEY = "system_prompt"
MAX_PROMPT_CHARS = 20_000        # a profile longer than this is sent cut off, not whole, to the model
PROMPT_FILE = "prompt.json"
PROMPT_PLACES = ("", "prompts")  # beside the server first (where a user keeps their own), then prompts/

# Filled from the server clock at render time. A profile that needs today's date writes the token
# instead of a year, so it stays correct when the year changes; the catalog is re-rendered when the
# date rolls over.
TEMPORAL_TOKENS = {"{{current_date}}": "%Y-%m-%d",
                   "{{current_year}}": "%Y",
                   "{{current_month}}": "%Y-%m"}

_cache: dict[str, tuple[float, int, datetime.date, dict]] = {}
_lock = threading.Lock()


def _title(key: str) -> str:
    """A JSON key as a heading: role_and_persona -> "Role and persona"."""
    return " ".join(word for word in key.replace("_", " ").replace("-", " ").split()).title()


def _render(value: object, level: int, indent: int) -> list[str]:
    """A JSON value as Markdown: objects become headings, arrays bullets, scalars plain lines."""
    pad = "  " * indent
    out: list[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            if sub in (None, "", [], {}):
                continue
            if level <= 3:
                out.append(f"{'#' * (level + 1)} {_title(key)}")
                out.extend(_render(sub, level + 1, 0))
            else:                                   # too deep for a heading: a bold label
                out.append(f"{pad}- **{_title(key)}**:")
                out.extend(_render(sub, level + 1, indent + 1))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                out.append(f"{pad}-")
                out.extend(_render(item, level, indent + 1))
            else:
                out.append(f"{pad}- {item}")
    elif value is not None:
        out.append(f"{pad}{value}")
    return out


def _clock() -> datetime.datetime:
    """The server's local clock, tz-aware so the date it reports is unambiguous."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone()


def _stamp(text: str) -> str:
    """The temporal tokens in a profile, filled from the server clock."""
    now = _clock()
    for token, fmt in TEMPORAL_TOKENS.items():
        text = text.replace(token, now.strftime(fmt))
    return text


def render_prompt(value: object) -> str:
    """An agent or system prompt as the text the model reads."""
    text = "\n".join(_render(value, 1, 0)).strip()
    if len(text) > MAX_PROMPT_CHARS:
        text = text[:MAX_PROMPT_CHARS].rstrip() + "\n[... the rest of this profile is not shown]"
    return _stamp(text)


def _catalog_from(data: object, path: str) -> dict:
    if not isinstance(data, dict):
        return {"path": path, "system": render_prompt(data), "agents": []}
    agents = []
    for key, value in data.items():
        if key == SYSTEM_KEY or key.startswith("_") or value in (None, "", {}, []):
            continue
        profile = value.get("agent_profile") if isinstance(value, dict) else None
        if not isinstance(profile, dict):
            profile = {}
        agents.append({
            "key": key,
            "name": str(profile.get("name") or _title(key)),
            "role": str(profile.get("role") or ""),
            "capabilities": [_title(c) if isinstance(c, str) else str(c)
                            for c in (profile.get("capabilities") or [])],
            "prompt": render_prompt(value),
        })
    return {"path": path, "system": render_prompt(data.get(SYSTEM_KEY)), "agents": agents}


def find_prompt_file(root: Path | str) -> str:
    """The prompt.json this server reads: the first of the places it is kept that has one.

    A file beside the server wins over the one in `prompts/`, so a profile set kept next to the server is
    not silently replaced by the shipped one. With neither, the path beside the server is returned, and
    `catalog` reports it as missing rather than guessing.
    """
    root = Path(root)
    for place in PROMPT_PLACES:
        p = root / place / PROMPT_FILE if place else root / PROMPT_FILE
        if p.is_file():
            return str(p)
    return str(root / PROMPT_FILE)


def catalog(path: Path | str) -> dict:
    """{"path", "system", "agents": [{"key", "name", "role", "capabilities", "prompt"}]}.

    Re-reads the file when it changed; {"system": "", "agents": []} when there is nothing to read.
    """
    p = Path(path)
    key = str(p)
    try:
        stat = p.stat()
    except OSError:
        return {"path": key, "system": "", "agents": [], "error": f"{p.name} is not there"}
    today = _clock().date()
    with _lock:
        cached = _cache.get(key)
        if (cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size
                and cached[2] == today):
            return cached[3]
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return {"path": key, "system": "", "agents": [], "error": f"could not read {p.name}: {e}"}
        result = _catalog_from(data, key)
        _cache[key] = (stat.st_mtime, stat.st_size, today, result)
        return result
