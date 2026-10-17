import unittest

from server.report_quality import evaluate_quality

E = 'abcdef1234567890:p1:b2'
RUN = {'facts': [{'status': 'verified', 'evidence_ids': [E]}]}


def claim(status='verified', evidence=None, **kwargs):
    return {'claim_id': 'C001', 'status': status,
            'claim_text': '公司收入100亿元', 'evidence_ids': evidence or [], **kwargs}


def dimension(result, key):
    return next(d for d in result['dimensions'] if d['key'] == key)


class ReportQualityTests(unittest.TestCase):
    def test_empty_has_no_full_scores_and_is_blocked(self):
        out = evaluate_quality('', {})
        self.assertIsNone(out['overall_total'])
        self.assertIsNone(out['provisional_score'])
        self.assertEqual(out['assessed_weight'], 0)
        self.assertTrue(out['delivery_blocked'])
        self.assertTrue(all(d['score'] is None for d in out['dimensions']))

    def test_unverifiable_is_not_accuracy_pass(self):
        out = evaluate_quality('公司收入100亿元。', {'claims': [claim('unverifiable')]})
        self.assertIsNone(dimension(out, 'accuracy')['score'])
        self.assertEqual(dimension(out, 'evidence')['score'], 0)
        self.assertLess(out['assessment_coverage'], 1)
        self.assertIsNone(out['overall_total'])

    def test_verified_label_without_registered_evidence_is_unassessable(self):
        out = evaluate_quality('公司收入100亿元。', {'claims': [claim('verified')]})
        self.assertIsNone(dimension(out, 'accuracy')['score'])
        self.assertEqual(dimension(out, 'evidence')['score'], 0)

    def test_critical_contradiction_blocks_high_other_scores(self):
        out = evaluate_quality('收入100亿元。', {'claims': [claim('contradicted', [E], severity='critical')]}, RUN)
        self.assertEqual(dimension(out, 'accuracy')['score'], 0)
        self.assertTrue(out['delivery_blocked'])
        self.assertIn('critical_contradiction', [f['code'] for f in out['flags']])

    def test_accuracy_is_subset_with_unresolved_claims(self):
        out = evaluate_quality('公司收入100亿元。', {'claims': [claim('verified', [E]), claim('unverifiable')]}, RUN)
        dim = dimension(out, 'accuracy')
        self.assertEqual(dim['score'], 30)
        self.assertEqual(dim['assessed_weight'], 15)
        self.assertEqual(out['claim_coverage'], .5)
        self.assertEqual(dim['status'], 'partial')

    def test_financial_contradiction_blocks_without_severity_label(self):
        out = evaluate_quality('公司收入100亿元。', {'claims': [claim('contradicted')]})
        self.assertTrue(out['delivery_blocked'])

    def test_unknown_citation_blocked_not_called_fraud(self):
        out = evaluate_quality('引用abcdef1234567890:p999:b2。', {})
        self.assertTrue(out['delivery_blocked'])
        flag = next(f for f in out['flags'] if f['code'] == 'unknown_citation')
        self.assertIn('不能据此认定', flag['reason'])

    def test_short_hash_requires_exact_locator(self):
        run = {'facts': [{'evidence_ids': ['abcdef1234567890' + '0' * 48 + ':p1:b2']}]}
        out = evaluate_quality('证据 ' + E, {'claims': [claim('verified', [E])]}, run)
        self.assertFalse(out['delivery_blocked'])

    def test_uncertainty_is_concrete_and_improves_observable_disclosure(self):
        base = evaluate_quality('公司财务数据尚待后续分析整理。', {})
        disclosed = evaluate_quality('关键预测输入数据缺失，估值结论无法核验。', {})
        self.assertGreater(dimension(disclosed, 'risk')['score'], dimension(base, 'risk')['score'])
        self.assertIsNone(disclosed['overall_total'])

    def test_specific_risk_links_driver_to_consequence(self):
        out = evaluate_quality('需求下降可能导致收入减少并压低现金流。', {})
        check = next(c for c in dimension(out, 'risk')['checks'] if c['id'] == 'risk_driver_consequence')
        self.assertEqual(check['status'], 'pass')

    def test_guarantee_negation_is_local(self):
        safe = ['本报告不保证收益。', '我们不得承诺稳赚不赔。', '保证收益的说法不成立。']
        unsafe = ['本报告不构成投资建议，但保证收益。',
                  '不承诺目标价，保证收益。', '不是一般产品，本产品保证收益。',
                  '我们不保证收益，但这只股票必涨。']
        for text in safe:
            with self.subTest(text=text):
                self.assertFalse(evaluate_quality(text, {})['delivery_blocked'])
        for text in unsafe:
            with self.subTest(text=text):
                self.assertTrue(evaluate_quality(text, {})['delivery_blocked'])

    def test_headings_never_prove_argument_or_risk(self):
        out = evaluate_quality('# 结论\n# 反证\n# 风险不确定性\n# 敏感性\n# 方法可复算', {})
        self.assertIsNone(dimension(out, 'argument')['score'])
        self.assertIsNone(dimension(out, 'method')['score'])
        self.assertIsNone(dimension(out, 'risk')['score'])
        self.assertEqual(dimension(out, 'argument')['status'], 'human_review_required')

    def test_valuation_labels_do_not_prove_reproduction(self):
        out = evaluate_quality('本报告采用DCF模型与敏感性分析。', {}, valuation={'ok': True})
        self.assertEqual(dimension(out, 'method')['score'], 0)
        self.assertIn('valuation_reproduction_gap', [f['code'] for f in out['flags']])
        self.assertIsNone(out['overall_total'])

    def test_failed_calculation_is_not_method_pass(self):
        out = evaluate_quality('公司现金流需要计算复核。', {}, run={'checks': [
            {'formula': 'a/b', 'inputs': {'a': 2, 'b': 0}, 'status': 'fail'}]})
        self.assertEqual(dimension(out, 'method')['score'], 0)

    def test_rubric_sources_and_details_are_reproducible(self):
        out = evaluate_quality('报告需复核关键收入数据。', {'claims': [claim('verified', [E])]}, RUN)
        self.assertEqual(sum(out['rubric']['weights'].values()), 100)
        self.assertFalse(out['rubric']['official_competition_rule'])
        self.assertFalse(out['rubric']['cfa_prescribed_weights'])
        self.assertTrue(out['sources'])
        for dim in out['dimensions']:
            for key in ('score', 'max_score', 'basis', 'status', 'checks'):
                self.assertIn(key, dim)
        self.assertEqual(out, evaluate_quality('报告需复核关键收入数据。', {'claims': [claim('verified', [E])]}, RUN))


if __name__ == '__main__':
    unittest.main()
