"""Line ranges, outlines and text edits, shared by the document tools.

Read and edit share one coordinate convention: a line number right-aligned in
six characters, a tab, then the file's own content - the `cat -n` format. Both
sides of the pair use it, so an edit can point at a line the model has already
seen instead of re-reading the file.

Files above AUTO_OUTLINE_SIZE are not truncated blindly: the read tool returns
their structure - Python definitions, Markdown headings, C++ functions, config
keys - with line numbers, and the model reads a range from there. That outline
is a *successful* response, and the text says so, because the natural model
behaviour is to retry the identical call.

Nothing here writes to disk; the callers in serve/docs_mcp.py and serve/rag.py
do the I/O and the permission checks.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any, overload

# Above this many bytes a file is read as an outline rather than as content.
AUTO_OUTLINE_SIZE = 16_384
# A single requested line range is capped so one call cannot fill the context.
MAX_RANGE_CHARS = 20_000
# How many outline entries one page returns; more are reachable with `offset`.
OUTLINE_PAGE = 60
LINE_NUMBER_WIDTH = 6

_PY_EXT = {"py"}
_MD_EXT = {"md", "markdown", "rst", "adoc", "org"}
_CPP_EXT = {"c", "h", "cpp", "cc", "hpp", "cu", "cuh", "sycl"}
_CFG_EXT = {"yaml", "yml", "toml", "ini", "conf", "cfg", "cnf", "properties", "json"}
_SH_EXT = {"sh", "bash", "zsh"}

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_RST_UNDERLINE = re.compile(r"^(?P<text>.+)\n(?P<mark>[=-]{3,})$", re.MULTILINE)
_CPP_DEF = re.compile(
    r"^[A-Za-z_][\w\s*:<>,&\[\]-]*?([A-Za-z_]\w*)\s*\([^()]*\)\s*"
    r"(?:const|noexcept|override)?\s*\{"
)
_SH_DEF = re.compile(r"^([A-Za-z_]\w*)\s*\(\s*\)\s*\{")
_YAML_KEY = re.compile(r"^\s*([A-Za-z_][\w.-]*)\s*:(?!\s*\|)")
_TOML_SECTION = re.compile(r"^\[([^\]]+)\]")


@dataclass(frozen=True)
class OutlineEntry:
    line: int
    kind: str
    text: str


def line_count(text: str) -> int:
    """Number of lines, consistent with how read_with_ranges numbers them."""
    return len(text.splitlines())


def resolve_line_range(start_line: Any, end_line: Any) -> tuple[int, int]:
    """Turn optional tool arguments into a 1-indexed, inclusive (start, end).

    The model sometimes passes 0 despite the schema saying 1-indexed, or an end
    below the start; callers get a range that always covers at least one line.
    """
    start = int(start_line) if start_line is not None else 1
    end = int(end_line) if end_line is not None else 2**31 - 1
    return max(1, start), max(max(1, start), end)


def format_with_line_numbers(text: str, start_line: int = 1) -> str:
    """`cat -n` style: number right-aligned in six characters, tab, content."""
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(f"{n:>{LINE_NUMBER_WIDTH}}\t{line}" for n, line in enumerate(lines, start=start_line))


def extract_outline(path: str, text: str) -> list[OutlineEntry]:
    """Structure of a file as (line, kind, text) entries, or [] if none is found."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in _PY_EXT:
        return _python_outline(text)
    if ext in _MD_EXT:
        return _markdown_outline(text)
    if ext in _CPP_EXT:
        return _cpp_outline(text)
    if ext in _SH_EXT:
        return _shell_outline(text)
    if ext in _CFG_EXT:
        return _config_outline(path, text)
    return []


def render_outline(entries: list[OutlineEntry], offset: int = 0, per_page: int = OUTLINE_PAGE) -> tuple[str, bool]:
    """Render one page of an outline. Returns (text, more_entries_exist)."""
    items = entries[offset:]
    page = items[:per_page]
    out = []
    for entry in page:
        out.append(f"{entry.line:>6}  {entry.kind}  {entry.text}")
    return "\n".join(out), len(items) > per_page


def _python_outline(text: str) -> list[OutlineEntry]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    entries: list[OutlineEntry] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entries.append(OutlineEntry(node.lineno, "def", f"def {node.name}({_arg_repr(node.args)})"))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(_base_name(b) for b in node.bases)
            entries.append(OutlineEntry(node.lineno, "class", f"class {node.name}" + (f"({bases})" if bases else "")))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    entries.append(OutlineEntry(child.lineno, "  method", f"def {child.name}({_arg_repr(child.args)})"))
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            entries.append(OutlineEntry(node.lineno, "const", f"{node.targets[0].id} ="))
    return entries


def _arg_repr(args: ast.arguments) -> str:
    parts = [a.arg for a in args.args]
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    if args.kwonlyargs:
        parts.append("*")
        parts.extend(a.arg for a in args.kwonlyargs)
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    return ", ".join(parts)


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _markdown_outline(text: str) -> list[OutlineEntry]:
    entries: list[OutlineEntry] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _HEADING.match(line)
        if match:
            entries.append(OutlineEntry(number, "heading", f"{'#' * len(match.group(1))} {match.group(2)}"))
        else:
            code = line.strip()
            if code.startswith("```") and len(code) > 3:
                entries.append(OutlineEntry(number, "code", code[3:].strip()))
    return entries


def _cpp_outline(text: str) -> list[OutlineEntry]:
    entries = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _CPP_DEF.match(line)
        if match:
            entries.append(OutlineEntry(number, "func", match.group(1)))
        elif line.startswith("#include"):
            entries.append(OutlineEntry(number, "include", line[8:].strip()))
    return entries


def _shell_outline(text: str) -> list[OutlineEntry]:
    entries = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _SH_DEF.match(line)
        if match:
            entries.append(OutlineEntry(number, "func", match.group(1)))
    return entries


def _config_outline(path: str, text: str) -> list[OutlineEntry]:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext == "json":
        try:
            data = json.loads(text)
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        return [OutlineEntry(1, "key", key) for key in data]
    entries = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ext == "toml":
            match = _TOML_SECTION.match(line)
            if match:
                entries.append(OutlineEntry(number, "section", match.group(1)))
        else:
            match = _YAML_KEY.match(line)
            if match:
                entries.append(OutlineEntry(number, "key", match.group(1)))
    return entries


@dataclass
class Edit:
    old_text: str
    new_text: str
    line: int | None = None


def parse_edits(raw: Any) -> tuple[list[Edit], str]:
    """Accept a list of edit objects, a single edit, or a JSON string of either.

    Some models stringify nested arguments; rather than failing the call we take
    the string as JSON. Returns (edits, error) - error is "" when parsed.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [], "edits must be a list of {old_text, new_text} objects"
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return [], "edits must be a non-empty list of {old_text, new_text} objects"
    edits: list[Edit] = []
    for item in raw:
        if not isinstance(item, dict):
            return [], f"each edit must be an object, got {type(item).__name__}"
        old = item.get("old_text", item.get("old", ""))
        new = item.get("new_text", item.get("new", ""))
        if not isinstance(old, str) or not isinstance(new, str):
            return [], "old_text and new_text must be strings"
        line = item.get("line")
        if line is not None:
            try:
                line = int(line)
            except (TypeError, ValueError):
                return [], f"line must be an integer, got {line!r}"
        edits.append(Edit(old, new, line))
    return edits, ""


def _match_positions(text: Any, needle: Any) -> list[int]:
    """Every position of needle in text; both are str or both are bytes."""
    positions: list[int] = []
    start = 0
    while True:
        at = text.find(needle, start)
        if at < 0:
            return positions
        positions.append(at)
        start = at + 1


def _line_of(text: Any, position: int) -> int:
    newline = b"\n" if isinstance(text, bytes) else "\n"
    return text.count(newline, 0, position) + 1


def strip_line_number_prefix(text: str) -> str:
    """Remove the `cat -n` prefix a model may have copied from read_file output."""
    lines = []
    for line in text.splitlines():
        stripped = re.sub(r"^\s*\d+\t", "", line)
        lines.append(stripped)
    return "\n".join(lines)


@overload
def apply_edits(text: str, edits: list[Edit]) -> tuple[str, list[str]]: ...


@overload
def apply_edits(text: bytes, edits: list[Edit]) -> tuple[bytes, list[str]]: ...


def apply_edits(text: Any, edits: list[Edit]) -> tuple[Any, list[str]]:
    """Apply edits in order. Raises ValueError with a usable message on failure,
    so the caller writes nothing when any edit is bad.

    Given bytes the match is byte by byte: a file that is not valid UTF-8 keeps the
    bytes the model could not read, instead of having them rewritten to U+FFFD.
    """
    current = text
    as_bytes = isinstance(current, bytes)
    report: list[str] = []
    for index, edit in enumerate(edits, start=1):
        if not edit.old_text:
            raise ValueError(f"edit {index}: old_text cannot be empty")
        old = edit.old_text.encode("utf-8") if as_bytes else edit.old_text
        new = edit.new_text.encode("utf-8") if as_bytes else edit.new_text
        positions = _match_positions(current, old)
        used = old
        if not positions:
            stripped = strip_line_number_prefix(edit.old_text)
            if stripped != edit.old_text:
                candidate = stripped.encode("utf-8") if as_bytes else stripped
                positions = _match_positions(current, candidate)
                if positions:
                    used = candidate
                    report.append(f"edit {index}: the line-number prefix was removed before matching")
        if not positions:
            raise ValueError(f"edit {index}: old_text not found in the file. Read the file again and copy the text exactly.")
        if len(positions) > 1:
            if edit.line is None:
                lines = ", ".join(str(_line_of(current, p)) for p in positions[:8])
                raise ValueError(
                    f"edit {index}: old_text matches {len(positions)} places (lines {lines}). "
                    "Add more surrounding context, or pass line=<one of them>."
                )
            chosen = [p for p in positions if _line_of(current, p) == edit.line]
            if not chosen:
                lines = ", ".join(str(_line_of(current, p)) for p in positions[:8])
                raise ValueError(f"edit {index}: line={edit.line} has no match; matches are at lines {lines}.")
            positions = [chosen[0]]
        current = current[: positions[0]] + new + current[positions[0] + len(used) :]
        report.append(f"edit {index}: replaced 1 occurrence at line {_line_of(current, positions[0])}")
    return current, report


def read_with_ranges(text: str, start_line: int, end_line: int, path: str = "") -> tuple[str, str]:
    """Return (numbered text of the range, continuation note or "")."""
    lines = text.splitlines()
    total = len(lines)
    if start_line > total:
        return "", f"the file has {total} lines; start_line={start_line} is past the end."
    end = min(end_line, total)
    slice_text = "\n".join(lines[start_line - 1 : end])
    body = format_with_line_numbers(slice_text, start_line)
    note = ""
    if len(body) > MAX_RANGE_CHARS:
        body = body[:MAX_RANGE_CHARS]
        last = start_line
        for line in body.splitlines():
            match = re.match(r"^\s*(\d+)\t", line)
            if match:
                last = int(match.group(1))
        note = (
            f"\n\n[showing lines {start_line}-{last} of {total}; the range is longer than "
            f"{MAX_RANGE_CHARS} characters. Continue with start_line={last + 1}]"
        )
    elif end < total:
        note = f"\n\n[showing lines {start_line}-{end} of {total}; continue with start_line={end + 1}]"
    owner = enclosing_symbol(path, text, start_line, end)
    if owner:
        note += f"\n[lines {start_line}-{end} are inside {owner}]"
    return body, note


def symbol_ranges(path: str, text: str) -> list[tuple[int, int, str]]:
    """(start_line, end_line, label) for every symbol the file contains.

    Python is read with ast, so the ranges are exact. Everything else falls back to
    the outline entries, where a symbol runs to the line before the next one.
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in _PY_EXT:
        return _python_ranges(text)
    if ext in _CPP_EXT or ext in _SH_EXT:
        return _brace_ranges(text, ext)
    return _ranges_from_outline(path, text)


def _python_ranges(text: str) -> list[tuple[int, int, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    ranges: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            label = getattr(node, "name", "")
            kind = "class" if isinstance(node, ast.ClassDef) else "def"
            ranges.append((node.lineno, node.end_lineno or node.lineno, f"{kind} {label}"))
    return ranges


def _brace_ranges(text: str, ext: str) -> list[tuple[int, int, str]]:
    """C/C++/CUDA and shell functions: the body runs to the matching closing brace."""
    lines = text.splitlines()
    ranges = []
    for index, line in enumerate(lines):
        match = _SH_DEF.match(line) if ext in _SH_EXT else _CPP_DEF.match(line)
        if not match:
            continue
        depth = 0
        end = len(lines)
        for i in range(index, len(lines)):
            depth += lines[i].count("{") - lines[i].count("}")
            if depth == 0:
                end = i + 1
                break
        ranges.append((index + 1, end, f"func {match.group(1)}"))
    return ranges


def _ranges_from_outline(path: str, text: str) -> list[tuple[int, int, str]]:
    entries = extract_outline(path, text)
    total = line_count(text)
    ranges = []
    for index, entry in enumerate(entries):
        end = entries[index + 1].line - 1 if index + 1 < len(entries) else total
        if end >= entry.line:
            ranges.append((entry.line, end, f"{entry.kind} {entry.text}"))
    return ranges


def enclosing_symbol(path: str, text: str, start_line: int, end_line: int) -> str | None:
    """The innermost symbol that contains the range, or None. Used so a partial read
    says what it is a piece of, instead of leaving the model to work that out."""
    best: str | None = None
    best_span = 0
    for start, end, label in symbol_ranges(path, text):
        if start <= start_line and end_line <= end:
            span = end - start
            if best is None or span < best_span:
                best, best_span = label, span
    return best


def coerce_sequence(value: object) -> list[str]:
    """Accept a list, a JSON array in a string, or a newline/comma separated string.

    Models sometimes stringify an array argument. Anything that is not a sequence of
    items raises, so a bad call fails with a message rather than running the empty
    pipeline.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        return [part.strip() for part in re.split(r"[\n;]", text) if part.strip()]
    raise ValueError(f"expected a list of items, got {type(value).__name__}")


def clamp_int(value: object, low: int, high: int, default: int) -> int:
    """Bound a numeric argument the model may have omitted, mis-typed, or overshot."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value)
        except ValueError:
            return default
    else:
        return default
    return max(low, min(high, number))
