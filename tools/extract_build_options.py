"""tools/extract_build_options.py - read a Strata checkout and report how it can be built.

Run against the clone in tmp/ before a build. Two jobs:

  1. Produce the manifest the Build tab's dropdowns are built from: every CMake
     option() / set(... CACHE ...) and every setup.py argparse choice.
  2. Diff that manifest against the one from the previous version, so the tab can
     say what changed between releases.

Nothing here is hard-coded against a particular Strata version: the options come
from the files in the clone, so a new release's new options appear automatically.

    python tools/extract_build_options.py tmp/strata-0.1.45 --out config/build_options.json
    python tools/extract_build_options.py tmp/strata-0.1.45 --diff config/build_options.json
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

# Only the options that affect the engine we run. Test targets and internal
# bookkeeping are parsed but marked hidden so the UI does not fill up with them.
BUILD_RELEVANT = re.compile(
    r"STRATA_(ENABLE_CUDA|ENABLE_HIP|ENABLE_SYCL|HIP_GFX906|CUDA_VERSION|PORTABLE|"
    r"ISA_FLOOR|MMQ_KQUANTS|MMQ_DIRECT|MMQ_TAB|MMQ_KV|PREFILL_MMQ|Q6K_EXPERTS|"
    r"Q4K_VRAM_EXPERTS|NATIVE_EXPERTS|GGML_DIR|BUILD_TESTS|BUILD_CONVERSATION_TESTS|WERROR)"
)
HIDDEN = re.compile(r"(BUILD_TESTS|BUILD_CONVERSATION_TESTS|WERROR|GGML_DIR)")

OPTION_RE = re.compile(r"^\s*option\(\s*([A-Z0-9_]+)\s+\"([^\"]*)\"\s+([A-Za-z0-9_(){}$]+)\s*\)", re.M)
CACHE_RE = re.compile(r"^\s*set\(\s*([A-Z0-9_]+)\s+\"([^\"]*)\"\s+CACHE\s+(\w+)\s+\"([^\"]*)\"\s*\)", re.M)
PROJECT_RE = re.compile(r"project\(\s*(\w+)\s+VERSION\s+([0-9.]+)", re.I)
TARGET_RE = re.compile(r"add_executable\(\s*([A-Za-z0-9_-]+)", re.M)


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def cmake_options(root: Path) -> list[dict[str, Any]]:
    text = read(root / "CMakeLists.txt")
    out: list[dict[str, Any]] = []
    for name, help_text, default in OPTION_RE.findall(text):
        out.append({
            "name": name,
            "kind": "bool" if default.upper() in ("ON", "OFF") else "string",
            "default": default,
            "help": help_text.strip(),
            "visible": bool(BUILD_RELEVANT.search(name)) and not HIDDEN.search(name),
        })
    for name, default, ctype, help_text in CACHE_RE.findall(text):
        out.append({
            "name": name,
            "kind": "string" if ctype.upper() == "STRING" else "int",
            "default": default,
            "help": help_text.strip(),
            "visible": bool(BUILD_RELEVANT.search(name)) and not HIDDEN.search(name),
        })
    return sorted(out, key=lambda o: o["name"])


def argparse_choices(root: Path) -> list[dict[str, Any]]:
    """The setup.py flags, read with ast so we get the choices lists verbatim."""
    src = read(root / "setup.py")
    if not src:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    out: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "add_argument"):
            continue
        flags = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if not flags:
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        choices_node = kw.get("choices")
        if isinstance(choices_node, ast.List):
            choices = [c.value for c in choices_node.elts if isinstance(c, ast.Constant)]
        elif isinstance(choices_node, (ast.Call, ast.Name)):
            choices = None          # a computed set (FAMILIES, MODELS): the UI fills it at runtime
        else:
            choices = None
        type_node = kw.get("type")
        out.append({
            "flag": flags[-1],
            "choices": choices,
            "type": getattr(type_node, "id", None) if isinstance(type_node, ast.Name) else None,
            "default": kw.get("default") if isinstance(kw.get("default"), (str, int, float, bool)) else None,
            "help": kw.get("help") if isinstance(kw.get("help"), str) else "",
        })
    return out


def targets(root: Path) -> list[str]:
    return sorted(set(TARGET_RE.findall(read(root / "CMakeLists.txt"))))


def manifest(root: Path) -> dict[str, Any]:
    text = read(root / "CMakeLists.txt")
    m = PROJECT_RE.search(text)
    return {
        "source": str(root),
        "project": m.group(1) if m else "strata",
        "version": m.group(2) if m else None,
        "cmake_options": cmake_options(root),
        "setup_choices": argparse_choices(root),
        "targets": targets(root),
    }


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    old_names = {o["name"] for o in old.get("cmake_options", [])}
    new_names = {o["name"] for o in new.get("cmake_options", [])}
    old_flags = {o["flag"] for o in old.get("setup_choices", [])}
    new_flags = {o["flag"] for o in new.get("setup_choices", [])}
    changed = [
        o for o in new["cmake_options"]
        if o["name"] in old_names and o["default"] != next(
            x["default"] for x in old["cmake_options"] if x["name"] == o["name"])
    ]
    return {
        "from": old.get("version"),
        "to": new.get("version"),
        "added_options": sorted(new_names - old_names),
        "removed_options": sorted(old_names - new_names),
        "changed_defaults": changed,
        "added_flags": sorted(new_flags - old_flags),
        "removed_flags": sorted(old_flags - new_flags),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clone", help="path to the Strata checkout to read")
    ap.add_argument("--out", help="write the manifest here")
    ap.add_argument("--diff", help="compare against this previous manifest")
    a = ap.parse_args()

    m = manifest(Path(a.clone))
    if not m["cmake_options"]:
        print(f"no CMake options found in {a.clone}/CMakeLists.txt", file=sys.stderr)
        return 1
    if a.out:
        Path(a.out).write_text(json.dumps(m, indent=2), encoding="utf-8")
    if a.diff:
        try:
            prev = json.loads(Path(a.diff).read_text(encoding="utf-8"))
            m["changes"] = diff(prev, m)
        except (OSError, ValueError) as exc:
            m["changes"] = {"error": f"could not read the previous manifest: {exc}"}
    print(json.dumps(m, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
