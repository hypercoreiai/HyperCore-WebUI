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

OPTION_RE = re.compile(r"^\s*option\(\s*([A-Z0-9_]+)\s+\"([^\"]*)\"\s+([A-Za-z0-9_(){}$]+)\s*\)", re.MULTILINE)
CACHE_RE = re.compile(r"^\s*set\(\s*([A-Z0-9_]+)\s+\"([^\"]*)\"\s+CACHE\s+(\w+)\s+\"([^\"]*)\"\s*\)", re.MULTILINE)
PROJECT_RE = re.compile(r"project\(\s*(\w+)\s+VERSION\s+([0-9.]+)", re.IGNORECASE)
TARGET_RE = re.compile(r"add_executable\(\s*([A-Za-z0-9_-]+)", re.MULTILINE)


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


def _items(value: Any) -> list[Any]:
    """The elements of a table: a dict gives its keys, a sequence gives its items."""
    if isinstance(value, dict):
        return list(value.keys())
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    if value is None:
        return []
    return [value]


def _literal(node: ast.AST, consts: dict[str, Any]) -> Any:
    """Evaluate the shapes setup.py uses to build its choice tables.

    Dict and list displays, comprehensions over them, starred unpacking, list() /
    sorted() / set() / dict.fromkeys() and + concatenation. Anything else returns
    None, and the caller reports the choices as unknown rather than guessing.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out: list[Any] = []
        for elt in node.elts:
            if isinstance(elt, ast.Starred):
                out.extend(_items(_literal(elt.value, consts)))
            else:
                out.append(_literal(elt, consts))
        return out
    if isinstance(node, ast.Dict):
        out_dict: dict[Any, Any] = {}
        for key, value in zip(node.keys, node.values):
            if key is None:                       # {**other}
                for k, v in (_literal(value, consts) or {}).items():
                    out_dict[k] = v
                continue
            out_dict[_literal(key, consts)] = _literal(value, consts)
        return out_dict
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        rows: list[dict[str, Any]] = [{}]
        for gen in node.generators:
            if not isinstance(gen.target, ast.Name):
                return None
            items = _items(_literal(gen.iter, consts))[:MAX_CHOICE_EXPANSION]
            rows = [{**row, gen.target.id: item}
                    for row in rows for item in items][:MAX_CHOICE_EXPANSION]
        if isinstance(node, ast.DictComp):
            return {(_literal(node.key, dict(consts, **row))): _literal(node.value, dict(consts, **row))
                    for row in rows}
        return [_literal(node.elt, dict(consts, **row)) for row in rows]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _items(_literal(node.left, consts)) + _items(_literal(node.right, consts))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("list", "sorted", "set", "tuple", "frozenset"):
        return _items(_literal(node.args[0], consts)) if node.args else []
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "fromkeys":
        return _items(_literal(node.args[0], consts)) if node.args else []
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def module_constants(tree: ast.Module) -> dict[str, Any]:
    """Module-level names bound to literal tables: {name: value}.

    setup.py builds --family and --model from FAMILIES, MODELS and PREVIEW_FAMILIES,
    so the choice lists are only readable if those tables are collected first.
    """
    consts: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        else:
            continue
        for name in targets:
            consts[name] = _literal(node.value, consts)
    return consts


def _choice_list(node: ast.AST | None, consts: dict[str, Any]) -> list[str] | None:
    """The accepted values of a flag, or None when they cannot be read statically."""
    if node is None:
        return None
    value = _literal(node, consts)
    return [v for v in dict.fromkeys(_items(value)) if isinstance(v, str)] or None


def _metavar_choices(metavar: Any) -> list[str] | None:
    """metavar="on|off|GGUF" is how setup.py spells a choice list it cannot put in
    choices= (the last value stands for a file path). The parts are returned as
    written; the caller labels an uppercase part as "a file"."""
    if not isinstance(metavar, str):
        return None
    parts = [p.strip() for p in metavar.split("|") if p.strip()]
    return parts if len(parts) > 1 else None


#: A choice table is small; this is only a guard against a comprehension over
#: something that is not a table at all.
MAX_CHOICE_EXPANSION = 200

#: The setup.py groups the Build tab lists: what a person chooses before building.
#: The rest (paths, ports, source mirrors) are counted and reported, not listed.
SETUP_GROUPS = ("model", "context", "kv-cache", "images", "experimental")


def setup_group(flag: str) -> str:
    """What a setup.py flag is about, from its own name."""
    name = flag.lstrip("-")
    if name in ("family", "model"):
        return "model"
    if name.startswith(("image", "vision")):
        return "images"
    if name == "kv" or name.startswith("kv"):
        return "kv-cache"
    if "context" in name or "rope" in name:
        return "context"
    if "experimental" in name:
        return "experimental"
    if name in ("gpu", "gpus", "low-ram", "cuda"):
        return "hardware"
    return "other"


def argparse_choices(root: Path) -> list[dict[str, Any]]:
    """The setup.py flags, read with ast so the choices lists come verbatim.

    Each entry carries the group it belongs to (model, context, kv-cache, images,
    experimental, hardware, other) and whether the Build tab should show it: the
    five groups a person chooses before building are shown, the housekeeping flags
    (paths, ports, source mirrors) are counted and reported instead of listed.
    """
    src = read(root / "setup.py")
    if not src:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    consts = module_constants(tree)

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
        flag = flags[-1]
        choices = _choice_list(kw.get("choices"), consts)
        metavar = _literal(kw.get("metavar"), consts)
        if choices is None:
            choices = _metavar_choices(metavar)
        group = setup_group(flag)
        out.append({
            "flag": flag,
            "group": group,
            "choices": choices,
            "metavar": metavar if isinstance(metavar, str) else None,
            "type": getattr(kw["type"], "id", None) if isinstance(kw.get("type"), ast.Name) else None,
            "default": _literal(kw["default"], consts) if "default" in kw else None,
            "help": _literal(kw["help"], consts) if isinstance(kw.get("help"), ast.Constant) else "",
            "visible": group in SETUP_GROUPS,
        })
    return out


def targets(root: Path) -> list[str]:
    return sorted(set(TARGET_RE.findall(read(root / "CMakeLists.txt"))))


def manifest(root: Path) -> dict[str, Any]:
    text = read(root / "CMakeLists.txt")
    m = PROJECT_RE.search(text)
    return {
        #: where this manifest came from, as an absolute path: a relative one means
        # nothing to whoever reads the file later, and the Build tab's tests check it.
        "source": str(Path(root).resolve()),
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
    # The change list is part of the manifest the tab reads, so it is computed before
    # the file is written: writing first would leave the diff out of the file.
    if a.diff:
        try:
            prev = json.loads(Path(a.diff).read_text(encoding="utf-8"))
            m["changes"] = diff(prev, m)
        except (OSError, ValueError) as exc:
            m["changes"] = {"error": f"could not read the previous manifest: {exc}"}
    if a.out:
        Path(a.out).write_text(json.dumps(m, indent=2), encoding="utf-8")
    print(json.dumps(m, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
