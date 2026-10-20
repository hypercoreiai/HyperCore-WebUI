"""tests/test_build_history.py - the distillation step of data/history/.

tools/build_history.py turns _old_chats/ into chats/, topics/ and notes/. These tests
cover the two rules that decide whether the result is usable by the next person: a path
must resolve to something that actually exists here, and a sentence must be a finding
rather than a remark. No GPU, no server, no transcripts needed beyond a few fixtures.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_history import (
    HOME,
    PROJECT,
    chat_filename,
    clean,
    extract,
    kind,
    relocate,
    sentences,
)


class PathTests(unittest.TestCase):
    """Every path in the history has to be reachable from this repository."""

    def test_the_old_project_name_resolves_to_this_one(self) -> None:
        self.assertEqual(relocate("~/black_box/serve/server.py"),
                         str(PROJECT / "serve" / "server.py"))
        self.assertEqual(relocate("/home/ai-user0/black_box/tools/run_tests.sh"),
                         str(PROJECT / "tools" / "run_tests.sh"))
        self.assertEqual(relocate("black_box/optuna_tune/results"),
                         str(PROJECT / "optuna_tune" / "results"))

    def test_the_tuner_is_now_in_this_repository(self) -> None:
        self.assertEqual(relocate("~/HyperCore-Tune-maya/bayesian_tune.py"),
                         str(PROJECT / "optuna_tune" / "bayesian_tune.py"))

    def test_transcript_trees_point_at_the_archive(self) -> None:
        archive = PROJECT / "data" / "history" / "_old_chats"
        self.assertEqual(relocate("~/black_box_chats/chat.md"), str(archive / "chat.md"))
        self.assertEqual(relocate("~/Strata_chats/chat.md"), str(archive / "chat.md"))

    def test_the_engine_and_its_data_stay_absolute(self) -> None:
        # The engine is not "another project": this project runs it, so the paths are kept
        # and made absolute rather than dropped.
        # Asserted against HOME, not a literal home: the same rule has to hold on the
        # CI runner, where HOME is /home/runner and not this machine's.
        self.assertEqual(relocate("~/Strata-data/packs/q2_0/tokenizer"),
                         str(HOME / "Strata-data" / "packs" / "q2_0" / "tokenizer"))
        self.assertEqual(relocate("~/Strata/data/expert-profile.bin"),
                         str(HOME / "Strata" / "data" / "expert-profile.bin"))

    def test_relative_paths_in_the_repository_are_left_alone(self) -> None:
        for s in ("engine/strata", "config/run-q2_0.json", "serve/web/app.js"):
            self.assertEqual(relocate(s), s)

    def test_an_approximation_sign_is_not_a_home_path(self) -> None:
        # "~34%" is "about 34%", not a directory. It used to become
        # "<HOME>/34%", which is why the IPCC line in the history read as a path.
        for s in ("a decline of ~34% (10-43%) by 2100", "about ~10 K", "a ~2 GB model",
                  "run it in ~3 s", "the ~5% that failed"):
            self.assertEqual(relocate(s), s, f"{s} was read as a path")
        # A name after the tilde is still a home path.
        self.assertEqual(relocate("~/black_box/serve"), str(PROJECT / "serve"))
        self.assertEqual(relocate("~/Strata/data"), str(HOME / "Strata" / "data"))

    def test_trailing_punctuation_is_not_part_of_the_path(self) -> None:
        out = relocate("the file is at ~/black_box/tests/test_x.py.")
        self.assertTrue(out.endswith("tests/test_x.py"), out)

    def test_no_tilde_survives(self) -> None:
        sample = ("run ~/black_box/run.sh, read ~/.config/hypercore/settings.json and "
                 "compare with ~/work/notes.md")
        self.assertNotIn("~", relocate(sample))

    def test_a_tilde_without_a_slash_is_still_resolved(self) -> None:
        self.assertEqual(relocate("check ~HyperCore-Tune-maya for the sweep code"),
                         f"check {PROJECT / 'optuna_tune'} for the sweep code")

    def test_the_project_aliases_exist_in_the_repository(self) -> None:
        # These two resolve inside the repository, so they are checkable in a fresh clone
        # as well as here.
        for alias in ("~/black_box/serve", "~/HyperCore-Tune-maya"):
            self.assertTrue(Path(relocate(alias)).exists(), relocate(alias))

    @unittest.skipUnless(
        (PROJECT / "data" / "history" / "_old_chats").is_dir(),
        "the transcript archive is machine-local (data/* is gitignored): a fresh clone has none"
    )
    def test_the_archive_alias_exists_here(self) -> None:
        self.assertTrue(Path(relocate("~/black_box_chats")).exists(), relocate("~/black_box_chats"))

    @unittest.skipUnless(
        (HOME / "Strata-data").is_dir(),
        "the engine home is machine-local: a CI runner has no ~/Strata-data, so this half is "
        "a property of this box, not of the code"
    )
    def test_the_engine_home_aliases_exist_here(self) -> None:
        for alias in ("~/Strata-data", "~/Strata"):
            self.assertTrue(Path(relocate(alias)).exists(), relocate(alias))


class SentenceTests(unittest.TestCase):
    """What counts as a finding, and which section it belongs to."""

    def test_a_concrete_statement_is_kept(self) -> None:
        text = "## HyperCore\n\n`serve/chat_archive.py` writes the transcript into `data/history/_old_chats/`.\n"
        self.assertTrue(any("chat_archive" in s for s in sentences(text)))

    def test_reasoning_and_questions_are_dropped(self) -> None:
        text = ("## HyperCore\n\n"
                "I should probably check the other folder first.\n\n"
                "Is the port still 8081?\n\n"
                "Let me look at the logs.\n")
        self.assertEqual(sentences(text), [])

    def test_tool_output_and_code_are_not_read_as_findings(self) -> None:
        text = ("## HyperCore\n\n"
                "```python\nprint('hello')\n```\n\n"
                "<details><summary>Tool docs__edit_file</summary>2 edits applied</details>\n")
        self.assertEqual(sentences(text), [])

    def test_a_fact_is_filed_under_the_right_heading(self) -> None:
        self.assertEqual(kind("open item: the GPU test still has to run"), "open")
        self.assertEqual(kind("do not set pcie_frac above 1.0"), "gotchas")
        self.assertEqual(kind("the sweep reached 61.4 tok/s on the 4090"), "numbers")
        self.assertEqual(kind("the archive button was wired to POST /history/archive"), "decisions")
        self.assertEqual(kind("`engine/strata` reads one pack per run"), "current")

    def test_a_figure_from_outside_is_not_a_local_measurement(self) -> None:
        self.assertEqual(kind("according to McKinsey's State of AI survey, 88% of organisations use AI"),
                         "external")
        self.assertEqual(kind("IPCC AR6 gives a likely decline of ~34% by 2100"), "external")
        # Numbers this machine produced stay where they belong.
        self.assertEqual(kind("the 40 ms fixture is deterministic, 10/10 runs"), "numbers")
        self.assertEqual(kind("decode ran at 41.2 tok/s with 18.4 GB resident"), "numbers")

    def test_keys_and_handles_are_scrubbed(self) -> None:
        self.assertIn("[key]", clean("the key sk-abcdefghij123456 is in the config"))
        self.assertIn("[email]", clean("write to ops@example.com about the host"))


class DedupTests(unittest.TestCase):
    """The same transcript sitting in two trees must be counted once."""

    def test_identical_transcripts_are_read_once(self) -> None:
        body = ("# chat\n\n## You\n\nFix the archive button in the composer.\n\n"
                "## HyperCore\n\n`serve/web/index.html` now carries an `archive-btn` "
                "next to the download button.\n")
        tmp = Path(tempfile.mkdtemp())
        a, b = tmp / "a", tmp / "b"
        for d in (a, b):
            d.mkdir()
            (d / "chat.md").write_text(body, encoding="utf-8")
        import build_history
        saved = build_history.SOURCES
        try:
            build_history.SOURCES = [a, b]
            data = extract()
        finally:
            build_history.SOURCES = saved
        names = list(data["sources"])
        self.assertEqual(len(names), 1, names)
        self.assertEqual(data["sources"][names[0]]["copies"], ["b/chat.md"])

    def test_a_transcript_is_named_by_its_date_once(self) -> None:
        self.assertEqual(chat_filename("2026-10-03-strata-chat-2026-10-03-12-03.md", "2026-10-03"),
                         "2026-10-03-strata-chat-2026-10-03-12-03.md")
        self.assertEqual(chat_filename("hypercore-chat-2026-10-13-22-03.md", "2026-10-13"),
                         "2026-10-13-hypercore-chat-2026-10-13-22-03.md")
        self.assertEqual(chat_filename("unknown-context.md", "unknown"), "unknown-context.md")


if __name__ == "__main__":
    unittest.main()
