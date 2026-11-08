import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from io import BytesIO
from unittest.mock import patch

from skill_engine import ask_profile, recover_skill, run_skill


class SkillEngineTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.memory = self.directory / "profiles.sqlite3"
        self.model_calls = []

    def choose(self, model, prompt, allowed, report):
        self.model_calls.append(set(allowed))
        return sorted(allowed)[0]

    def test_two_skills_share_a_database_without_sharing_profiles(self):
        text = self.directory / "note.txt"
        data = self.directory / "settings.json"

        def write_note(profile):
            text.write_text("проверено" if profile else "ошибка", encoding="utf-8")
            return text

        def write_json(profile):
            data.write_text(json.dumps({"enabled": bool(profile)}), encoding="utf-8")
            return data

        jobs = [
            ("notes", "write", write_note,
             lambda path: path.read_text(encoding="utf-8") == "проверено"),
            ("settings", "write", write_json,
             lambda path: json.loads(path.read_text(encoding="utf-8"))["enabled"] is True),
        ]
        for source in ("qwen2.5:14b", "memory"):
            for agent, skill, action, verify in jobs:
                result = run_skill(
                    agent=agent, skill=skill, action=action, verify=verify,
                    profiles_for_error=lambda error: {"rewrite": "Use a fresh copy"},
                    memory_path=self.memory, choose=self.choose,
                )
                self.assertEqual(result.status, "verified")
                self.assertEqual(result.recovery["source"], source)
        self.assertEqual(self.model_calls, [{"rewrite"}, {"rewrite"}])

    def test_verified_first_attempt_skips_model_and_memory(self):
        result = run_skill(
            agent="demo", skill="read", action=lambda profile: 7, verify=lambda value: value == 7,
            profiles_for_error=lambda error: {"again": "Retry the read"},
            memory_path=self.memory, choose=self.choose,
        )
        self.assertEqual(result.evidence, 7)
        self.assertEqual(self.model_calls, [])
        self.assertFalse(self.memory.exists())

    def test_panel_only_result_is_not_saved(self):
        result = recover_skill(
            memory_path=self.memory, fingerprint={"operation": "panel"}, context={},
            profiles={"again": "Open the panel"}, choose=self.choose,
            retry=lambda profile: ("panel_verified", "opened"),
        )
        self.assertEqual(result.status, "panel_verified")
        self.assertFalse(self.memory.exists())

    def test_unrecognized_model_profile_is_not_executed(self):
        result = recover_skill(
            memory_path=self.memory, fingerprint={"operation": "read"}, context={},
            profiles={"again": "Retry"}, choose=lambda *args: "shell",
            retry=lambda profile: self.fail("An unknown profile was executed"),
        )
        self.assertEqual(result.status, "unverified")
        self.assertFalse(self.memory.exists())

    def test_failure_after_side_effect_blocks_another_attempt(self):
        kwargs = dict(memory_path=self.memory, fingerprint={"operation": "export"}, context={},
                      profiles={"export": "Export once", "read": "Read status"}, choose=self.choose)
        self.assertEqual(recover_skill(**kwargs, retry=lambda profile: ("verified", 1)).status, "verified")

        def fail(profile):
            self.assertEqual(profile, "export")
            raise RuntimeError("Submitted before failure")

        result = recover_skill(**kwargs, retry=fail, profiles_after_error=lambda error: {})
        self.assertEqual(result.status, "unverified")
        self.assertEqual(len(self.model_calls), 1)
        self.assertEqual(result.attempts[0]["source"], "memory")

    def test_failed_memory_falls_back_only_to_still_allowed_profile(self):
        kwargs = dict(memory_path=self.memory, fingerprint={"operation": "read"}, context={},
                      profiles={"a": "First", "b": "Second", "c": "Third"}, choose=self.choose)
        recover_skill(**kwargs, retry=lambda profile: ("verified", profile))

        def retry(profile):
            if profile == "a":
                raise RuntimeError("Stored profile failed")
            return "verified", profile

        result = recover_skill(**kwargs, retry=retry, profiles_after_error=lambda error: {"c": "Third"})
        self.assertEqual(result.evidence, "c")
        self.assertEqual(self.model_calls[-1], {"c"})
        self.assertEqual([attempt["source"] for attempt in result.attempts], ["memory", "qwen2.5:14b"])

    def test_failed_model_choice_gets_a_different_allowed_profile_and_learns_it(self):
        prompts = []

        def choose(model, prompt, allowed, report):
            prompts.append(prompt)
            return self.choose(model, prompt, allowed, report)

        def retry(profile):
            if profile == "a":
                raise RuntimeError("Selection did not change")
            return "verified", profile

        kwargs = dict(memory_path=self.memory, fingerprint={"operation": "select"}, context={},
                      profiles={"a": "First", "b": "Second", "c": "Third"}, choose=choose,
                      retry=retry, profiles_after_error=lambda error: {"b": "Second"})
        result = recover_skill(**kwargs)
        self.assertEqual(result.evidence, "b")
        self.assertEqual(self.model_calls, [{"a", "b", "c"}, {"b"}])
        self.assertIn("Selection did not change", prompts[1])
        self.assertEqual(recover_skill(**kwargs).recovery["source"], "memory")
        self.assertEqual(len(prompts), 2)

    def test_recovery_stops_after_two_attempts(self):
        def retry(profile):
            raise RuntimeError(profile)

        profiles = {"a": "First", "b": "Second", "c": "Third"}
        result = recover_skill(memory_path=self.memory, fingerprint={"operation": "select"},
                               context={}, profiles=profiles, choose=self.choose, retry=retry,
                               profiles_after_error=lambda error: profiles)
        self.assertEqual(result.status, "unverified")
        self.assertEqual([attempt["profile"] for attempt in result.attempts], ["a", "b"])
        self.assertFalse(self.memory.exists())

    def test_ollama_stop_reason_and_stop_profile_are_distinct(self):
        cases = [
            ("stop", '{"profile":"alternate"}', "alternate", "received"),
            ("stop", '{"profile":"stop"}', "stop", "declined"),
            ("length", '{"profile":"alternate"}', None, "unavailable"),
            ("stop", '{"profile":"shell"}', None, "unavailable"),
            ("stop", "", None, "unavailable"),
        ]
        for reason, content, expected, status in cases:
            with self.subTest(reason=reason, content=content):
                events = []
                answer = {"done": True, "done_reason": reason, "message": {"content": content}}
                with patch("skill_engine.urllib.request.urlopen",
                           return_value=BytesIO(json.dumps(answer).encode())) as urlopen:
                    result = ask_profile("qwen2.5:14b", "Choose", {"alternate"},
                                         lambda stage, outcome, **details: events.append(outcome))
                request = json.loads(urlopen.call_args.args[0].data)
                self.assertEqual(request["format"]["properties"]["profile"]["enum"], ["alternate", "stop"])
                self.assertEqual(result, expected)
                self.assertEqual(events[-1], status)


if __name__ == "__main__":
    unittest.main()
