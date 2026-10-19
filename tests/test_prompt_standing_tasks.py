#!/usr/bin/env python3
"""The two standing tasks in the system prompt.

prompt.json is read by serve/prompts.py and injected into every chat, so a task
written there has to (a) parse, (b) survive the renderer, (c) stay inside the
character ceiling a profile is cut at, and (d) name paths that actually exist in
this repository. A standing task that points at a file that is not there is a
task the model will fail every time it tries to carry it out.

Run: python3 -m pytest tests/test_prompt_standing_tasks.py
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from serve import prompts

ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / "prompts" / "prompt.json"

# What each task must name. Kept as fragments so the wording can change without
# the test becoming a copy of the prompt.
TASK_1 = ["data/history/_old_chats/", "data/history/chats/", "data/history/topics/",
          "data/history/notes/", "tools/build_history.py"]
TASK_2 = [".mcp_test_state.json", "data/history/notes/", "CHANGELOG.md"]


class StandingTasks(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(PROMPT.read_text(encoding="utf-8"))
        self.system = self.data["system_prompt"]
        self.tasks = self.system["standing_tasks"]

    def test_the_file_parses_and_has_two_tasks(self):
        self.assertEqual(len([k for k in self.tasks if k.startswith("task_")]), 2,
                        "the system prompt must carry exactly two standing tasks")

    def test_task_1_names_the_archive_and_the_three_outputs(self):
        text = json.dumps(self.tasks["task_1_distil_the_chat_archive"])
        for fragment in TASK_1:
            self.assertIn(fragment, text, f"task 1 must name {fragment}")

    def test_task_2_names_the_state_files(self):
        text = json.dumps(self.tasks["task_2_keep_the_state_files_current"])
        for fragment in TASK_2:
            self.assertIn(fragment, text, f"task 2 must name {fragment}")

    def test_every_path_a_task_names_is_real(self):
        """A standing task the model cannot act on is worse than no standing task.

        The transcript archive and the notes folder are machine-local (data/* is
        gitignored), so a fresh clone cannot be checked against them: they are only
        checked when the box running the test has them.
        """
        machine_local = ("data/history/", ".mcp_test_state.json")
        for fragment in TASK_1 + TASK_2:
            path = ROOT / fragment
            if fragment.startswith(machine_local) and not path.exists():
                continue
            if fragment.endswith("/"):
                self.assertTrue(path.is_dir(), f"{fragment} is not a directory here")
            else:
                self.assertTrue(path.is_file(), f"{fragment} is not a file here")
        self.assertTrue((ROOT / "tools" / "build_history.py").is_file(),
                        "the distiller task names a tool that is not in the repository")

    def test_the_rendered_system_prompt_is_not_cut_off(self):
        system = prompts.catalog(prompts.find_prompt_file(ROOT))["system"]
        self.assertNotIn("[... the rest of this profile is not shown]", system)
        self.assertLess(len(system), prompts.MAX_PROMPT_CHARS)

    def test_the_tasks_survive_the_renderer(self):
        system = prompts.catalog(prompts.find_prompt_file(ROOT))["system"]
        for fragment in TASK_1 + TASK_2:
            self.assertIn(fragment, system)

    def test_no_em_dash_in_any_prompt(self):
        """v5 of this file bans U+2014 in text the model is asked to imitate. The rule that
        states the ban may quote the character; nothing else may."""
        for key, value in self.data.items():
            if key.startswith("_"):
                continue
            for line in prompts.render_prompt(value).splitlines():
                if "\u2014" in line:
                    self.assertIn("em dash", line.lower(),
                        f"{key} uses an em dash outside the rule that bans it: {line}")

    def test_the_tasks_do_not_displace_the_users_request(self):
        purpose = self.tasks["purpose"]
        self.assertIn("the request in front of you comes first", purpose)
        self.assertIn("tool budget", purpose)

    def test_every_profile_still_renders(self):
        catalog = prompts.catalog(prompts.find_prompt_file(ROOT))
        self.assertTrue(catalog["agents"], "no agent profiles were read")
        for agent in catalog["agents"]:
            self.assertTrue(agent["prompt"], f"{agent['key']} renders to nothing")
            self.assertNotIn("[... the rest of this profile is not shown]", agent["prompt"],
                            f"{agent['key']} is over the character ceiling")


if __name__ == "__main__":
    unittest.main()
