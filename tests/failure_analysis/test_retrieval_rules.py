import inspect
import unittest
from dataclasses import replace

from experiments.failure_analysis.models import MappingError
from experiments.failure_analysis.retrieval_models import (
    QueryStep,
    RetrievalDecision,
    RetrievalEvidenceBundle,
)
from experiments.failure_analysis.retrieval_rules import suggest_prelabel


class RetrievalRulesTest(unittest.TestCase):
    def bundle(self, *query_steps, **changes):
        bundle = RetrievalEvidenceBundle.fixture_for_test()
        last_step = max((query.step for query in query_steps), default=0)
        values = {
            'query_steps': tuple(query_steps),
            'trajectory_steps': max(2, last_step),
        }
        values.update(changes)
        return replace(bundle, **values)

    def test_public_interface_has_exactly_one_bundle_parameter(self):
        parameters = inspect.signature(suggest_prelabel).parameters

        self.assertEqual(tuple(parameters), ('bundle',))

    def test_default_decision_is_conservative_and_requires_review(self):
        decision = suggest_prelabel(self.bundle())

        self.assertIsInstance(decision, RetrievalDecision)
        self.assertEqual(decision.retrieval_primary_subtype, 'INDETERMINATE')
        self.assertEqual(decision.retrieval_outcome, 'UNOBSERVED')
        self.assertEqual(decision.boundary_flag, 'NONE')
        self.assertEqual(decision.confidence, 'low')
        self.assertNotEqual(decision.confidence, 'high')
        self.assertEqual(decision.decision_status, 'needs_review')
        self.assertIsNone(decision.first_divergence_step)
        self.assertEqual(decision.gold_evidence_basis, '')
        self.assertIn('objective prelabel', decision.rationale.lower())
        self.assertIn('semantic review', decision.rationale.lower())

    def test_empty_success_adds_tag_step_and_observation_without_semantic_primary(self):
        decision = suggest_prelabel(
            self.bundle(QueryStep(1, 'SELECT name FROM services', '  []\n', True))
        )

        self.assertEqual(decision.auxiliary_tags, ('EMPTY_RESULT',))
        self.assertEqual(decision.relevant_sql_steps, (1,))
        self.assertIn('step=1', decision.sql_evidence)
        self.assertIn('SELECT name FROM services', decision.sql_evidence)
        self.assertIn('step=1', decision.observation_evidence)
        self.assertIn('[]', decision.observation_evidence)
        self.assertEqual(decision.retrieval_primary_subtype, 'INDETERMINATE')

    def test_failed_query_adds_error_tag_and_error_evidence(self):
        decision = suggest_prelabel(
            self.bundle(QueryStep(2, 'SELECT missing FROM table_x', 'no such column', False))
        )

        self.assertEqual(decision.auxiliary_tags, ('SQL_ERROR_PRESENT',))
        self.assertEqual(decision.relevant_sql_steps, (2,))
        self.assertIn('step=2', decision.sql_evidence)
        self.assertIn('SELECT missing FROM table_x', decision.sql_evidence)
        self.assertIn('step=2', decision.observation_evidence)
        self.assertIn('no such column', decision.observation_evidence)

    def test_normalized_second_and_later_duplicate_queries_are_recorded(self):
        decision = suggest_prelabel(
            self.bundle(
                QueryStep(1, ' SELECT  *\nFROM alerts; ', '[{"id": 1}]', True),
                QueryStep(2, 'select * from ALERTS', '[{"id": 1}]', True),
                QueryStep(3, 'SELECT * FROM alerts;', '[{"id": 1}]', True),
            )
        )

        self.assertEqual(decision.auxiliary_tags, ('REPEATED_QUERY',))
        self.assertEqual(decision.relevant_sql_steps, (2, 3))
        self.assertNotIn(1, decision.relevant_sql_steps)
        self.assertEqual(decision.retrieval_primary_subtype, 'INDETERMINATE')

    def test_all_successful_observable_results_empty_has_empty_outcome(self):
        decision = suggest_prelabel(
            self.bundle(
                QueryStep(1, 'SELECT 1', '[]', True),
                QueryStep(2, 'SELECT 2', '\n [] ', True),
                QueryStep(3, 'SELECT broken', 'syntax error', False),
                QueryStep(4, 'SELECT unknown', '', None),
            )
        )

        self.assertEqual(decision.retrieval_outcome, 'EMPTY')

    def test_empty_and_nonempty_successful_results_have_mixed_outcome(self):
        decision = suggest_prelabel(
            self.bundle(
                QueryStep(1, 'SELECT 1', '[]', True),
                QueryStep(2, 'SELECT 2', '[{"value": 2}]', True),
            )
        )

        self.assertEqual(decision.retrieval_outcome, 'MIXED')

    def test_error_unknown_and_no_query_bundles_are_unobserved(self):
        bundles = (
            self.bundle(),
            self.bundle(QueryStep(1, 'SELECT broken', 'syntax error', False)),
            self.bundle(QueryStep(1, 'SELECT unknown', 'pending', None)),
        )

        for bundle in bundles:
            with self.subTest(query_steps=bundle.query_steps):
                self.assertEqual(suggest_prelabel(bundle).retrieval_outcome, 'UNOBSERVED')

    def test_tags_are_deterministic_sorted_and_as_dict_remains_deduplicated(self):
        bundle = self.bundle(
            QueryStep(1, 'SELECT 1', '[]', True),
            QueryStep(2, ' select 1; ', 'duplicate failed', False),
        )

        first = suggest_prelabel(bundle)
        second = suggest_prelabel(bundle)

        self.assertEqual(first, second)
        self.assertEqual(
            first.auxiliary_tags,
            ('EMPTY_RESULT', 'REPEATED_QUERY', 'SQL_ERROR_PRESENT'),
        )
        self.assertEqual(
            first.as_dict()['auxiliary_tags'],
            ['EMPTY_RESULT', 'REPEATED_QUERY', 'SQL_ERROR_PRESENT'],
        )
        self.assertEqual(first.relevant_sql_steps, (1, 2))

    def test_each_sql_and_observation_evidence_segment_is_clipped_deterministically(self):
        bundle = self.bundle(
            QueryStep(1, 'SELECT ' + ('column_name, ' * 100), 'error-' + ('x' * 1000), False),
            QueryStep(2, 'SELECT 2', '[]', True),
        )

        first = suggest_prelabel(bundle)
        second = suggest_prelabel(bundle)

        self.assertEqual(first.sql_evidence, second.sql_evidence)
        self.assertEqual(first.observation_evidence, second.observation_evidence)
        self.assertTrue(all(len(line) <= 240 for line in first.sql_evidence.splitlines()))
        self.assertTrue(all(len(line) <= 240 for line in first.observation_evidence.splitlines()))
        self.assertNotIn('x' * 1000, first.observation_evidence)
        self.assertIn('step=1', first.sql_evidence)
        self.assertIn('step=2', first.sql_evidence)

    def test_validated_submission_at_step_limit_adds_step_limit_tag(self):
        decision = suggest_prelabel(
            self.bundle(
                trajectory_steps=7,
                submitted=True,
                submitted_at_step_limit=True,
            )
        )

        self.assertEqual(decision.auxiliary_tags, ('STEP_LIMIT',))

    def test_query_count_and_last_sql_step_never_infer_step_limit(self):
        cases = (
            self.bundle(
                *(QueryStep(step, f'SELECT {step}', '[]', True) for step in range(1, 16)),
                trajectory_steps=15,
                submitted=True,
                submitted_at_step_limit=False,
            ),
            self.bundle(
                QueryStep(15, 'SELECT final', '[]', True),
                trajectory_steps=15,
                submitted=True,
                submitted_at_step_limit=False,
            ),
            self.bundle(
                QueryStep(1, 'SELECT only', '[]', True),
                trajectory_steps=15,
                submitted=True,
                submitted_at_step_limit=False,
            ),
        )

        for bundle in cases:
            with self.subTest(query_count=len(bundle.query_steps)):
                self.assertNotIn('STEP_LIMIT', suggest_prelabel(bundle).auxiliary_tags)

    def test_invalid_step_limit_contract_raises_mapping_error(self):
        fixture = RetrievalEvidenceBundle.fixture_for_test()
        invalid_bundles = (
            replace(fixture, submitted=False, submitted_at_step_limit=True),
            replace(fixture, trajectory_steps=0, submitted=True, submitted_at_step_limit=False),
            replace(fixture, trajectory_steps=0, submitted=True, submitted_at_step_limit=True),
            replace(fixture, trajectory_steps=True),
            replace(fixture, trajectory_steps=-1),
            replace(fixture, submitted=1),
            replace(fixture, submitted_at_step_limit=0),
        )

        for bundle in invalid_bundles:
            with self.subTest(bundle=bundle), self.assertRaises(MappingError):
                suggest_prelabel(bundle)


if __name__ == '__main__':
    unittest.main()
