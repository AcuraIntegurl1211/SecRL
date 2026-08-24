import json
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from experiments.failure_analysis import retrieval_models
from experiments.failure_analysis.retrieval_models import (
    OVERLAY_SCHEMA_VERSION,
    QueryStep,
    RetrievalDecision,
    RetrievalEvidenceBundle,
)


ROOT = Path(__file__).resolve().parents[2]
TAXONOMY = ROOT / 'experiments/failure_analysis/sql_retrieval_taxonomy_v1.json'


class RetrievalModelsTest(unittest.TestCase):
    def test_taxonomy_version_and_primary_subtypes_are_frozen(self):
        data = json.loads(TAXONOMY.read_text(encoding='utf-8'))
        self.assertEqual(data['version'], 'sql_retrieval_taxonomy_v1')
        self.assertEqual(
            data['primary_subtypes'],
            [
                'SOURCE_SELECTION', 'ENTITY_RESOLUTION', 'TEMPORAL_SCOPE',
                'PREDICATE_FILTER', 'RELATIONAL_PATH', 'PROJECTION',
                'AGGREGATION_RANKING', 'SEARCH_COVERAGE', 'RESULT_SELECTION',
                'INDETERMINATE',
            ],
        )
        self.assertEqual(
            data['boundary_flags'],
            ['NONE', 'SQL_EXEC_POSSIBLE', 'REASONING_POSSIBLE', 'DATA_GOLD_POSSIBLE'],
        )
        self.assertEqual(
            data['auxiliary_tags'],
            [
                'EMPTY_RESULT', 'PARTIAL_EVIDENCE', 'NOISY_RESULT', 'WRONG_TABLE',
                'WRONG_COLUMN', 'WRONG_ENTITY', 'WRONG_TIME', 'OVER_FILTER',
                'UNDER_FILTER', 'MISSING_JOIN', 'WRONG_JOIN', 'MISSING_ORDER',
                'WRONG_ORDER', 'WRONG_LIMIT', 'REPEATED_QUERY', 'NO_ADAPTATION',
                'STEP_LIMIT', 'SQL_ERROR_PRESENT', 'GOLD_IN_RESULT',
                'GOLD_NOT_IN_RESULT',
            ],
        )
        self.assertEqual(data['outcomes'], ['EMPTY', 'PARTIAL', 'NOISY', 'WRONG_ROW', 'MIXED', 'UNOBSERVED'])
        self.assertEqual(data['confidence'], ['high', 'medium', 'low', 'indeterminate'])
        self.assertEqual(data['decision_statuses'], ['reviewed', 'needs_review'])

    def test_overlay_schema_and_evidence_bundle_are_immutable(self):
        bundle = RetrievalEvidenceBundle.fixture_for_test()
        self.assertEqual(OVERLAY_SCHEMA_VERSION, 'sql_retrieval_subtyping_v1')
        self.assertEqual(bundle.trajectory_steps, 2)
        self.assertTrue(bundle.submitted)
        self.assertFalse(bundle.submitted_at_step_limit)
        self.assertIsInstance(bundle.query_steps, tuple)
        self.assertIsInstance(bundle.query_steps[0], QueryStep)
        with self.assertRaises(FrozenInstanceError):
            bundle.incident = 'other'
        with self.assertRaises(FrozenInstanceError):
            bundle.submitted = False
        with self.assertRaises(FrozenInstanceError):
            bundle.query_steps[0].sql = 'SELECT 2'

        replaced = replace(
            bundle,
            trajectory_steps=3,
            submitted_at_step_limit=True,
        )
        self.assertEqual(replaced.trajectory_steps, 3)
        self.assertTrue(replaced.submitted)
        self.assertTrue(replaced.submitted_at_step_limit)

    def test_evidence_bundle_recursively_freezes_nested_json_values(self):
        original = {'nested': {'values': [1, {'name': 'before'}]}}
        bundle = replace(
            RetrievalEvidenceBundle.fixture_for_test(),
            golden_answer=original,
        )

        with self.assertRaises(TypeError):
            bundle.golden_answer['nested']['values'][1]['name'] = 'after'
        with self.assertRaises(AttributeError):
            bundle.golden_answer['nested']['values'].append(2)

    def test_evidence_bundle_defensively_copies_caller_owned_values(self):
        original = {'nested': [{'name': 'before'}]}
        bundle = replace(
            RetrievalEvidenceBundle.fixture_for_test(),
            golden_solution=original,
        )

        original['nested'][0]['name'] = 'after'
        original['nested'].append({'name': 'later'})

        self.assertEqual(bundle.golden_solution['nested'][0]['name'], 'before')
        self.assertEqual(len(bundle.golden_solution['nested']), 1)

    def test_evidence_bundle_rejects_unsupported_non_json_values(self):
        with self.assertRaises(TypeError):
            replace(
                RetrievalEvidenceBundle.fixture_for_test(),
                golden_answer={'unsupported': {1, 2}},
            )

    def test_frozen_json_values_can_be_thawed_for_serialization(self):
        original = {'answers': [{'name': 'database'}, None, True, 3.5]}
        bundle = replace(
            RetrievalEvidenceBundle.fixture_for_test(),
            golden_answer=original,
        )
        thaw_json_value = getattr(retrieval_models, 'thaw_json_value', None)
        self.assertIsNotNone(thaw_json_value)

        thawed = thaw_json_value(bundle.golden_answer)

        self.assertEqual(thawed, original)
        self.assertIsInstance(thawed, dict)
        self.assertIsInstance(thawed['answers'], list)
        json.dumps(thawed, allow_nan=False)

    def test_decision_as_dict_sorts_and_deduplicates_tuple_fields(self):
        decision = RetrievalDecision(
            retrieval_primary_subtype='ENTITY_RESOLUTION',
            auxiliary_tags=('WRONG_ENTITY', 'EMPTY_RESULT', 'WRONG_ENTITY'),
            retrieval_outcome='WRONG_ROW',
            boundary_flag='NONE',
            confidence='high',
            decision_status='reviewed',
            first_divergence_step=2,
            relevant_sql_steps=(3, 1, 3, 2),
            sql_evidence='sql',
            observation_evidence='observation',
            gold_evidence_basis='gold',
            rationale='rationale',
        )
        self.assertEqual(decision.as_dict()['auxiliary_tags'], ['EMPTY_RESULT', 'WRONG_ENTITY'])
        self.assertEqual(decision.as_dict()['relevant_sql_steps'], [1, 2, 3])


if __name__ == '__main__':
    unittest.main()
