"""tests/test_extract_build_options.py - reading a release's setup.py without running it.

The Build tab lists the model, quant, context, KV cache, image and experimental
choices a release accepts. Those come from setup.py's argparse calls, several of
which build their choice lists from module-level tables (FAMILIES, MODELS,
PREVIEW_FAMILIES) rather than from a literal list, so the extractor has to resolve
those. A fixture setup.py is written in a temp directory; no clone, no network.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from tools.extract_build_options import (
    argparse_choices,
    manifest,
    module_constants,
    setup_group,
)

#: A cut-down setup.py with the shapes a real one uses: literal choices, a
#: comprehension over a dict, a starred merge of two tables, and a metavar that
#: stands for a choice list.
SETUP = '''
import argparse

FAMILIES = {"qwen": {"title": "Qwen"}, "swift": {"title": "Swift"}}
PREVIEW_FAMILIES = {"qwen36": {"title": "Qwen3.6"}}
MODELS = {"Q2_0": {"about": "2-bit"}, "IQ3_XXS": {"about": "3-bit"}}
SOURCES = ("auto", "modelscope", "huggingface")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", choices=list(FAMILIES) + list(PREVIEW_FAMILIES),
                    help="which model family")
    ap.add_argument("--model", choices=list(dict.fromkeys([*MODELS, *(m for f in PREVIEW_FAMILIES.values() for m in f["models"])])),
                    help="which quant")
    ap.add_argument("--context", type=int, help="context length")
    ap.add_argument("--kv", choices=["int8", "q4_0"], help="KV cache precision")
    ap.add_argument("--images", choices=["qwen-image", "no"], help=argparse.SUPPRESS)
    ap.add_argument("--image-quant", choices=["Q4_K_M", "Q8_0"])
    ap.add_argument("--experimental-speed-projection", metavar="on|off|GGUF",
                    help="EXPERIMENTAL, off by default")
    ap.add_argument("--port", type=int, help="the server's port")
    ap.add_argument("--models-dir", help="where the packs go")
    return ap.parse_args()
'''

CMAKE = '''
cmake_minimum_required(VERSION 3.16)
project(Strata VERSION 0.1.99)
option(STRATA_ENABLE_CUDA "Build the CUDA targets" OFF)
add_executable(strata src/main.cpp)
'''


def fixture(root: Path) -> Path:
    (root / "setup.py").write_text(SETUP, encoding="utf-8")
    (root / "CMakeLists.txt").write_text(CMAKE, encoding="utf-8")
    return root


class ChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.choices = argparse_choices(fixture(self.tmp))
        self.by_flag = {c["flag"]: c for c in self.choices}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_computed_choice_list_is_resolved(self) -> None:
        self.assertEqual(self.by_flag["--family"]["choices"], ["qwen", "swift", "qwen36"])
        self.assertEqual(self.by_flag["--model"]["choices"], ["Q2_0", "IQ3_XXS"])

    def test_a_metavar_is_read_as_a_choice_list(self) -> None:
        self.assertEqual(self.by_flag["--experimental-speed-projection"]["choices"], ["on", "off", "GGUF"])

    def test_a_literal_choice_list_is_taken_verbatim(self) -> None:
        self.assertEqual(self.by_flag["--kv"]["choices"], ["int8", "q4_0"])

    def test_a_number_flag_has_no_choices_but_keeps_its_type(self) -> None:
        self.assertEqual(self.by_flag["--context"]["type"], "int")
        self.assertIsNone(self.by_flag["--context"]["choices"])

    def test_the_help_text_survives(self) -> None:
        self.assertIn("KV cache", self.by_flag["--kv"]["help"])

    def test_every_flag_gets_a_group(self) -> None:
        groups = {c["group"] for c in self.choices}
        self.assertTrue({"model", "context", "kv-cache", "images", "experimental"} <= groups)
        self.assertIn("other", groups)  # --port, --models-dir

    def test_only_the_choice_groups_are_shown(self) -> None:
        for flag in ("--family", "--model", "--context", "--kv", "--images",
                    "--image-quant", "--experimental-speed-projection"):
            self.assertTrue(self.by_flag[flag]["visible"], f"{flag} should be listed")
        for flag in ("--port", "--models-dir"):
            self.assertFalse(self.by_flag[flag]["visible"], f"{flag} is housekeeping, not a choice")

    def test_group_names_come_from_the_flag(self) -> None:
        self.assertEqual(setup_group("--experimental-speed-projection"), "experimental")
        self.assertEqual(setup_group("--image-quant"), "images")
        self.assertEqual(setup_group("--vision-tokens"), "images")
        self.assertEqual(setup_group("--kv-streaming"), "kv-cache")
        self.assertEqual(setup_group("--rope-scale"), "context")
        self.assertEqual(setup_group("--gpu"), "hardware")
        self.assertEqual(setup_group("--models-dir"), "other")


class ConstantsTests(unittest.TestCase):
    def test_module_tables_are_collected(self) -> None:
        import ast
        consts = module_constants(ast.parse(SETUP))
        self.assertEqual(sorted(consts["SOURCES"]), ["auto", "huggingface", "modelscope"])
        self.assertIn("qwen", consts["FAMILIES"])

    def test_an_unreadable_value_is_none_not_a_crash(self) -> None:
        import ast
        consts = module_constants(ast.parse("X = compute(1)\nY = [1, 2]\n"))
        self.assertIsNone(consts["X"])
        self.assertEqual(consts["Y"], [1, 2])


class ManifestTests(unittest.TestCase):
    def test_the_manifest_carries_both_halves(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            m = manifest(fixture(tmp))
            self.assertEqual(m["version"], "0.1.99")
            self.assertEqual([o["name"] for o in m["cmake_options"]], ["STRATA_ENABLE_CUDA"])
            self.assertEqual(next(o["flag"] for o in m["setup_choices"]), "--family")
            self.assertEqual(m["targets"], ["strata"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_missing_file_is_empty_not_an_error(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            m = manifest(tmp)
            self.assertEqual(m["cmake_options"], [])
            self.assertEqual(m["setup_choices"], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
