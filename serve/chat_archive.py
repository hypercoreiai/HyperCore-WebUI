"""serve/chat_archive.py - write a chat transcript into data/history/_old_chats/.

The Chat tab has two ways out of a conversation.  The download button writes the
Markdown to the browser's own downloads folder; the archive button posts the same
Markdown to POST /history/archive, which lands it beside the transcripts the
history builder already reads (data/history/_old_chats/), so the next
tools/build_history.py picks the chat up.

Naming follows the files already in that folder: YYYY-MM-DD-HH-MM-<slug>.md.
Nothing is ever overwritten - a second chat in the same minute gets -2, -3.

Saving a chat also runs the standing task the system prompt carries: the archived
transcript is distilled into data/history/chats/, topics/ and notes/.  That is a
tens-of-seconds pass over every transcript, so it is not done inside the request.
queue_distil() starts it in a daemon thread and returns at once; a second archive
while one is running does not start a second.  GET /history/distil reports the
state, POST /history/distil runs it now.  HYPERCORE_AUTO_DISTIL=0 turns the
automatic pass off and leaves the button as a plain save.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Overridable so a test, or a machine with the history elsewhere, can point it
#: at its own directory without touching the code.
OLD_CHATS_DIR = Path(os.environ.get("HYPERCORE_OLD_CHATS") or ROOT / "data" / "history" / "_old_chats")

#: The history root. _old_chats/ is the input; chats/, topics/ and notes/ are what
#: tools/build_history.py writes beside it.
HIST = OLD_CHATS_DIR.parent

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


# ---------------------------------------------------------------- distil the archive
DISTIL_SCRIPT = Path(os.environ.get("HYPERCORE_DISTIL_SCRIPT") or ROOT / "tools" / "build_history.py")
_DISTIL_TIMEOUT = os.environ.get("HYPERCORE_DISTIL_TIMEOUT_S")
DISTIL_TIMEOUT_S = float(_DISTIL_TIMEOUT) if _DISTIL_TIMEOUT else 180.0
AUTO_DISTIL = os.environ.get("HYPERCORE_AUTO_DISTIL", "1").strip().lower() not in {"0", "false", "off", "no"}

_distil_lock = threading.Lock()
_distil_state: dict[str, object] = {"running": False, "last": None}


def run_builder() -> dict[str, object]:
    """Distil every transcript now, in this process, and report what the builder said.

    Returns {"ok", "output", "error", "seconds", "started", "script"}.  A missing
    builder is a result, not an exception: an archive must still succeed.
    """
    started = datetime.now(timezone.utc)
    if not DISTIL_SCRIPT.is_file():
        result = {"ok": False, "error": f"the history builder is not at {DISTIL_SCRIPT}", "output": ""}
    else:
        try:
            proc = subprocess.run([sys.executable, str(DISTIL_SCRIPT)], cwd=str(ROOT),
                                  capture_output=True, text=True, timeout=DISTIL_TIMEOUT_S, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = {"ok": False, "error": str(exc), "output": ""}
        else:
            out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
            result = {"ok": proc.returncode == 0,
                      "output": out[-400:],
                      "error": None if proc.returncode == 0 else out[-400:] or f"builder exit {proc.returncode}"}
    result.update({"started": started.isoformat(timespec="seconds"),
                   "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
                   "script": str(DISTIL_SCRIPT)})
    return result


def _distil_and_record() -> None:
    result = run_builder()
    with _distil_lock:
        _distil_state["running"] = False
        _distil_state["last"] = result


def queue_distil() -> dict[str, object]:
    """Start the rebuild in the background. The archive response does not wait for it."""
    if not AUTO_DISTIL:
        return {"queued": False, "reason": "automatic distillation is off (HYPERCORE_AUTO_DISTIL=0)"}
    with _distil_lock:
        if _distil_state["running"]:
            return {"queued": False, "reason": "a rebuild is already running"}
        _distil_state["running"] = True
    threading.Thread(target=_distil_and_record, daemon=True).start()
    return {"queued": True, "reason": "the transcript files are being rebuilt"}


def distil_status() -> dict[str, object]:
    """Whether a rebuild is running, and the result of the last one."""
    with _distil_lock:
        return {"auto": AUTO_DISTIL, "running": bool(_distil_state["running"]),
                "last": _distil_state["last"], "script": str(DISTIL_SCRIPT)}


def history_status() -> dict[str, object]:
    """What is in the history folder right now, for the Monitor.

    Counts only; nothing here reads the transcripts, so it is cheap enough to poll.
    The transcript count is the local folder's; the builder also reads the other
    source folders, so the index's own count is reported separately as `distilled`.
    """
    def count_markdown(directory: Path) -> int:
        #: Only the documents count. A helper script or a scratch file sitting in
        #: the folder is not a transcript, and reporting it as one makes the
        #: Monitor lie about how much memory the agent has.
        if not directory.is_dir():
            return 0
        return sum(1 for p in directory.glob("*.md") if p.is_file())

    transcripts = sorted(OLD_CHATS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    distilled = None
    index = HIST / "index.json"
    if index.is_file():
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
            distilled = {"built": data.get("built"), "transcripts": data.get("transcripts"),
                        "topics": data.get("topics")}
        except (OSError, ValueError):
            distilled = None
    return {
        "old_chats": str(OLD_CHATS_DIR),
        "transcripts": count_markdown(OLD_CHATS_DIR),
        "summaries": count_markdown(HIST / "chats"),
        "topics": count_markdown(HIST / "topics"),
        "notes": count_markdown(HIST / "notes"),
        "newest": transcripts[0].name if transcripts else None,
        "distilled": distilled,
        "distil": distil_status(),
    }
