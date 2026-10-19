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


class ReleaseManifestTests(unittest.TestCase):
    """The manifest of a release cloned in tmp/, when one is there.

    tmp/ holds the checkout a build is about to run against, so the shipped manifest
    has to describe it: a release that adds a CMake option or a setup.py choice only
    reaches the Build tab's dropdowns if the extractor reads it out of that clone.
    """

    CLONE = ROOT / "tmp" / "strata-v0.2.0-beta"
    MANIFEST = ROOT / "config" / "build_options.json"

    def setUp(self) -> None:
        if not (self.CLONE / "CMakeLists.txt").is_file():
            self.skipTest(f"no 0.2.0 clone to read in {self.CLONE}")
        self.m = manifest(self.CLONE)

    def test_the_release_version_is_read_from_its_own_CMakeLists(self) -> None:
        self.assertEqual(self.m["version"], "0.1.45.3", "0.2.0 ships an engine at 0.1.45.3")

    def test_a_new_option_shows_up_without_us_naming_it(self) -> None:
        """The extractor reads the option out of the clone; nothing lists it by hand."""
        names = {o["name"] for o in self.m["cmake_options"]}
        self.assertIn("STRATA_MMQ_TQ_OVERLAY", names,
                      "the Gyro trellis MMQ overlay is a build option of this release")
        overlay = next(o for o in self.m["cmake_options"] if o["name"] == "STRATA_MMQ_TQ_OVERLAY")
        self.assertEqual(overlay["kind"], "bool")
        self.assertEqual(overlay["default"], "ON")

    def test_the_new_model_choices_are_collected(self) -> None:
        flags = {c["flag"]: c["choices"] for c in self.m["setup_choices"]}
        self.assertIn("gyro", flags.get("--family", []))
        self.assertIn("NVFP4-QAD", flags.get("--model", []))
        self.assertTrue(any("Gyro-" in c for c in flags.get("--model", [])),
                        "the Gyro-S/Gyro-M models are choices, not a guess")

    def test_the_new_test_targets_are_listed(self) -> None:
        for target in ("gdn_chunk_parity", "tq_cpu_parity", "pf_wmma_parity", "pool_plan_test"):
            self.assertIn(target, self.m["targets"], f"{target} is a target of this release")

    def test_the_shipped_manifest_matches_the_clone_it_came_from(self) -> None:
        """config/build_options.json is the offline fallback, so it must not be stale."""
        import json
        data = json.loads(self.MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], self.m["version"],
                         "the manifest on disk is for the clone in tmp/")
        self.assertEqual({o["name"] for o in data["cmake_options"]},
                         {o["name"] for o in self.m["cmake_options"]})
        self.assertEqual(data["source"], str(self.CLONE), "the manifest says where it came from")

    def test_the_change_list_is_written_to_the_file_not_only_printed(self) -> None:
        import subprocess
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "manifest.json"
        prev = tmp / "previous.json"
        prev.write_text(__import__("json").dumps({"version": "0.1.45", "cmake_options": [],
                                                  "setup_choices": []}), encoding="utf-8")
        try:
            done = subprocess.run([sys.executable, str(ROOT / "tools" / "extract_build_options.py"),
                                   str(self.CLONE), "--out", str(out), "--diff", str(prev)],
                                  capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            written = __import__("json").loads(out.read_text(encoding="utf-8"))
            self.assertIn("STRATA_MMQ_TQ_OVERLAY", written["changes"]["added_options"],
                          "the diff is in the file the Build tab reads, not just in stdout")
            self.assertEqual(written["changes"]["removed_options"], [], "nothing was dropped")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
