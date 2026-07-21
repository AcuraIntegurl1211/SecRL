import json
import tempfile
import unittest
from pathlib import Path

from experiments.failure_analysis.identity import (
    canonical_json,
    load_json,
    map_logs,
    question_identity,
)
from experiments.failure_analysis.models import InputError, MappingError
from tests.failure_analysis.helpers import agent_entry, env_entry, question


class IdentityTest(unittest.TestCase):
    def test_canonical_json_is_sorted_compact_and_unicode_preserving(self):
        self.assertEqual(canonical_json({"é": 1, "a": 2}), '{"a":2,"é":1}')

    def test_question_identity_contains_stable_sha256_values(self):
        identity = question_identity("incident_5", 0, question("q"))
        self.assertEqual(identity.incident, "incident_5")
        self.assertEqual(identity.question_index, 0)
        self.assertEqual(len(identity.question_fingerprint_sha256), 64)
        self.assertEqual(len(identity.question_text_fingerprint_sha256), 64)

    def test_agent_and_env_map_independently_when_shuffled(self):
        first = question("first")
        second = question("second")
        mapped = map_logs(
            "incident_5",
            [agent_entry(second), agent_entry(first)],
            [env_entry(first), env_entry(second)],
            [first, second],
        )
        self.assertEqual([item.identity.question_index for item in mapped], [0, 1])
        self.assertEqual([item.agent_source_index for item in mapped], [1, 0])
        self.assertEqual([item.env_source_index for item in mapped], [0, 1])

    def test_duplicate_nodes_do_not_merge_distinct_questions(self):
        first = question("first", nodes=["same"])
        second = question("second", nodes=["same"])
        mapped = map_logs(
            "incident_5",
            [agent_entry(first), agent_entry(second)],
            [env_entry(first), env_entry(second)],
            [first, second],
        )
        self.assertEqual(len(mapped), 2)
        self.assertNotEqual(
            mapped[0].identity.question_fingerprint_sha256,
            mapped[1].identity.question_fingerprint_sha256,
        )

    def test_missing_agent_entry_is_rejected(self):
        item = question("only")
        with self.assertRaisesRegex(MappingError, "agent.*missing"):
            map_logs("incident_5", [], [env_entry(item)], [item])

    def test_extra_env_entry_is_rejected(self):
        item = question("only")
        extra = question("extra")
        with self.assertRaisesRegex(MappingError, "env.*extra"):
            map_logs(
                "incident_5",
                [agent_entry(item)],
                [env_entry(item), env_entry(extra)],
                [item],
            )

    def test_duplicate_canonical_question_is_rejected(self):
        item = question("duplicate")
        with self.assertRaisesRegex(MappingError, "duplicate canonical"):
            map_logs(
                "incident_5",
                [agent_entry(item), agent_entry(item)],
                [env_entry(item), env_entry(item)],
                [item, item],
            )

    def test_ambiguous_text_only_mapping_is_rejected(self):
        first = question("same text", answer="first")
        second = question("same text", answer="second")
        agent = [
            agent_entry(first),
            {"question_dict": {"question": "same text"}, "reward": 0},
        ]
        with self.assertRaisesRegex(MappingError, "ambiguous.*text"):
            map_logs(
                "incident_5",
                agent,
                [env_entry(first), env_entry(second)],
                [first, second],
            )

    def test_invalid_json_reports_the_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(InputError, str(path)):
                load_json(path)

    def test_top_level_object_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "object.json"
            path.write_text(json.dumps({"question": "q"}), encoding="utf-8")
            with self.assertRaisesRegex(InputError, "top-level list"):
                load_json(path)

    def test_non_dictionary_list_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "member.json"
            path.write_text(
                json.dumps([{"question": "q"}, 7]),
                encoding="utf-8",
            )
