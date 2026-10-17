"""serve/chat_archive.py - write a chat transcript into data/history/_old_chats/.

The Chat tab has two ways out of a conversation.  The download button writes the
Markdown to the browser's own downloads folder; the archive button posts the same
Markdown to POST /history/archive, which lands it beside the transcripts the
history builder already reads (data/history/_old_chats/), so the next
tools/build_history.py run picks the chat up.

Naming follows the files already in that folder: YYYY-MM-DD-HH-MM-<slug>.md.
Nothing is ever overwritten - a second chat in the same minute gets -2, -3.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Overridable so a test, or a machine with the history elsewhere, can point it
#: at its own directory without touching the code.
OLD_CHATS_DIR = Path(os.environ.get("HYPERCORE_OLD_CHATS") or ROOT / "data" / "history" / "_old_chats")

#: A transcript is a chat, not a log file.  The guard is there so a stray POST
#: (or a client sending a whole corpus) cannot fill the history folder.
MAX_CHAT_CHARS = 1_000_000

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}$")


def _stamp(now: datetime) -> str:
    """The minute-stamp the history files use, in UTC."""
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d-%H-%M")


def _slug(text: str) -> str:
    """One safe filename fragment from a free-text title."""
    cleaned = _SLUG_RE.sub("-", str(text or "").strip())[:60].strip("-.")
    return cleaned or "chat"


def archive_chat(chat: str, title: str = "", directory: Path | None = None,
                 now: datetime | None = None) -> dict[str, object]:
    """Write `chat` (Markdown) into the history folder and describe the file.

    Raises TypeError on a non-string, ValueError on an empty or oversized
    transcript, OSError if the folder cannot be created or written.  Returns
    {"path", "name", "bytes", "new"}.
    """
    if not isinstance(chat, str):
        raise TypeError(f"chat: expected text, got {type(chat).__name__}")
    if not chat.strip():
        raise ValueError("chat: there is nothing to save yet")
    if len(chat) > MAX_CHAT_CHARS:
        raise ValueError(f"chat: {len(chat)} characters is more than {MAX_CHAT_CHARS}; trim it first")

    directory = Path(directory or OLD_CHATS_DIR)
    directory.mkdir(parents=True, exist_ok=True)

    stamp = _stamp(now or datetime.now(timezone.utc))
    name = f"{stamp}-{_slug(title)}.md"
    path = directory / name
    new = not path.exists()
    if not new:
        # Same minute, same title: keep both rather than lose one.
        for n in range(2, 1000):
            path = directory / f"{stamp}-{_slug(title)}-{n}.md"
            if not path.exists():
                name = path.name
                new = True
                break
        else:
            raise OSError(f"too many chats in the same minute in {directory}")

    path.write_text(chat.rstrip("\n") + "\n", encoding="utf-8")
    return {"path": str(path), "name": name, "bytes": path.stat().st_size, "new": new}
