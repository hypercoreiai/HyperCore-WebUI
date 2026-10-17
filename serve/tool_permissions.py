"""Whether a document tool may run, decided before it runs.

serve/docs_mcp.py does not call decide() in this phase. An edit, write, or delete
proceeds on the first call, with no user prompt. The function stays for the
/home/ai-agent/ migration, where the rule below turns back on.

When it is called: ownership, not path. A file this process created is allowed, and
so is a file under a scratch directory. Anything else is ("deny", reason).
"""
from __future__ import annotations

from pathlib import Path

# Directories whose contents are working material: free to delete, no questions asked.
SCRATCH_DIRS = {"scratch", "tmp", "_tmp", "downloads", "drafts"}

# Files the user maintains: configuration and instructions.
PROTECTED_NAMES = {"prompt.json", "mcp.yaml", "agents.md", "claude.md", "gemini.md", "rules",
                   "settings.json", "config.json"}

# The session's own tracking file: the agent is expected to rewrite it every loop.
AGENT_STATE_NAMES = {".mcp_test_state.json"}

_created: set[str] = set()


def note_created(path: Path) -> None:
    """Record that this process wrote the file, so later edits and deletes are allowed."""
    _created.add(str(path.resolve()))


def note_removed(path: Path) -> None:
    _created.discard(str(path.resolve()))


def is_known(path: Path) -> bool:
    return str(path.resolve()) in _created


def decide(action: str, target: Path, root: Path) -> tuple[str, str]:
    """allow or deny for a destructive action ("edit", "delete" or "overwrite") on target."""
    try:
        relative = target.resolve().relative_to(root.resolve())
    except ValueError:
        return "deny", "the path is outside the workspace root"
    parts = [part.lower() for part in relative.parts]
    if parts and parts[0] in SCRATCH_DIRS:
        return "allow", ""
    if str(target.resolve()) in _created:
        return "allow", ""
    if target.name.lower() in AGENT_STATE_NAMES:
        return "allow", ""
    if target.name.lower() in PROTECTED_NAMES:
        return "deny", (
            f"{target.name} is configuration or instructions the user maintains. "
            "Pass force=true if the user asked for this change."
        )
    return "deny", (
        f"this session did not create {target.name}. Pass force=true if the user asked you "
        "to edit, delete or replace it."
    )
