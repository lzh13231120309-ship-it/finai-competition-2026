"""Regression coverage for semantic financial report assertions."""
import unittest

from server.report_audit import build_pool
from server.report_claims import review_claims

DOC = 'baffc40b1939f3d4'


def fact(code, value, year=2025, scope='consolidated', evidence=1, status='verified'):
    return {'code': code, 'value': str(value), 'period': f'{year}-12-31',
            'scope': scope, 'status': status,
            'evidence_ids': [f'{DOC}:p1:b{evidence}'] if evidence else []}


class ReportClaimTests(unittest.TestCase):
    def check(self, text, facts, metrics=None, run=None):
        return review_claims(text, build_pool(facts, metrics or []), run)

    def test_wrong_field_same_amount_is_not_support(self):
        result = self.check('2025年合并净利润为10亿元。', [fact('revenue', 10**9)])
        self.assertEqual(result['claims'][0]['status'], 'unverifiable')
        self.assertEqual(result['claims'][0]['code'], 'net_profit')
        result = self.check('2025年合并净利润为10亿元。',
                            [fact('revenue', 10**9), fact('net_profit', 2*10**8, evidence=2)])
        claim = result['claims'][0]
        self.assertEqual(claim['status'], 'contradicted')
        self.assertEqual(claim['expected_value'], '200000000')
        self.assertIn('2亿元', claim['advice'])

    def test_wrong_year_does_not_match_same_number(self):
        result = self.check('2024年合并营业收入10亿元。',
                            [fact('revenue', 10**9), fact('revenue', 8*10**8, year=2024)])
        self.assertEqual(result['claims'][0]['status'], 'contradicted')
        self.assertEqual(result['claims'][0]['period'], '2024-12-31')

    def test_wrong_scope_does_not_match_same_number(self):
        result = self.check('2025年母公司口径营业收入10亿元。',
                            [fact('revenue', 10**9), fact('revenue', 3*10**8, scope='parent')])
        self.assertEqual(result['claims'][0]['status'], 'contradicted')
        self.assertEqual(result['claims'][0]['scope'], 'parent')

    def test_unit_conversion_rounding_and_decimal_boundary(self):
        for amount in ('525.53亿元', '5255291万元', '52,552,910,000元'):
            claim = self.check('2025年合并营业收入' + amount, [fact('revenue', 52552910000)])['claims'][0]
            self.assertEqual(claim['status'], 'verified', amount)
        claim = self.check('2025年合并营业收入525.54亿元', [fact('revenue', 52552910000)])['claims'][0]
        self.assertEqual(claim['status'], 'contradicted')

    def test_table_years_units_and_alias(self):
        report = '# 合并财务报表\n单位：万元\n| 项目 | 2025年 | 2024年 |\n|---|---:|---:|\n| 营收 | 100000 | 80000 |\n'
        result = self.check(report, [fact('revenue', 10**9), fact('revenue', 8*10**8, year=2024)])
        self.assertEqual(result['stats']['verified'], 2)
        self.assertEqual([c['period'] for c in result['claims']], ['2025-12-31', '2024-12-31'])
        self.assertEqual(result['claims'][0]['line'], 5)

    def test_citation_exists_but_belongs_to_different_fact(self):
        result = self.check(f'2025年合并营业收入10亿元 [{DOC}:p1:b2]。',
                            [fact('revenue', 10**9), fact('net_profit', 10**9, evidence=2)])
        claim = result['claims'][0]
        self.assertEqual(claim['status'], 'contradicted')
        self.assertIn('不支持', claim['reason'])
        self.assertEqual(claim['evidence_ids'], [f'{DOC}:p1:b1'])

    def test_unknown_citation_and_no_evidence_are_unverifiable(self):
        for report, facts in [(f'2025年合并营收10亿元 [{DOC}:p9:b9]', [fact('revenue', 10**9)]),
                              ('2025年合并营收10亿元', [fact('revenue', 10**9, evidence=None)])]:
            self.assertEqual(self.check(report, facts)['claims'][0]['status'], 'unverifiable')

    def test_missing_or_ambiguous_context_is_not_guessed(self):
        for report in ('营业收入10亿元', '2025年营业收入10亿元', '合并营业收入10亿元',
                       '2025年合并及母公司口径营业收入10亿元'):
            self.assertEqual(self.check(report, [fact('revenue', 10**9)])['claims'][0]['status'], 'unverifiable')

    def test_pending_source_facts_do_not_support(self):
        pending = fact('revenue', 10**9, status='needs_review')
        self.assertEqual(review_claims('2025年合并营收10亿元', [pending])['claims'][0]['status'], 'unverifiable')

    def test_negative_amount_and_ratio_are_preserved(self):
        facts = [fact('net_profit', -10**8)]
        metric = {'code': 'gross_margin', 'value': '.125', 'unit': 'ratio',
                  'period': '2025-12-31', 'scope': 'consolidated', 'status': 'calculated',
                  'evidence_ids': [f'{DOC}:p1:b3']}
        report = '2025年合并净利润（1）亿元，毛利率12.50%。'
        result = self.check(report, facts, [metric])
        self.assertEqual(result['stats']['verified'], 2)
        self.assertEqual([c['value'] for c in result['claims']], ['-100000000', '0.125'])
        result = self.check('2025年合并净利润1亿元', facts)
        self.assertEqual(result['claims'][0]['status'], 'contradicted')

    def test_derived_yoy_uses_matched_annual_period_and_direction(self):
        facts = [fact('revenue', 8*10**8), fact('revenue', 10**9, year=2024, evidence=2)]
        result = self.check('2025年合并营业收入同比下降20%。', facts)
        self.assertEqual(result['claims'][0]['status'], 'verified')
        self.assertEqual(result['claims'][0]['code'], 'revenue_yoy')
        self.assertEqual(len(result['claims'][0]['evidence_ids']), 2)
        result = self.check('2025年合并营业收入同比增长20%。', facts)
        self.assertEqual(result['claims'][0]['status'], 'contradicted')

    def test_run_context_and_half_year_exact_date(self):
        run = {'quality': {'reporting_period': '2025-12-31', 'reporting_scope': 'consolidated'}}
        result = self.check('营业收入10亿元', [fact('revenue', 10**9)], run=run)
        self.assertEqual(result['stats']['verified'], 1)
        half_year = fact('revenue', 6*10**8)
        half_year['period'] = '2025-06-30'
        self.assertEqual(self.check('2025年上半年合并营收6亿元', [half_year])['stats']['verified'], 1)

    def test_qualified_profit_does_not_match_total_profit(self):
        result = self.check('2025年合并扣非净利润1亿元', [fact('net_profit', 10**8)])
        self.assertEqual(result['claims'][0]['status'], 'unverifiable')
        self.assertEqual(result['claims'][0]['code'], 'nonrecurring_adj_parent_profit')

    def test_yoy_comparison_year_does_not_replace_claim_year(self):
        facts = [fact('revenue', 1250000000), fact('revenue', 1000000000, year=2024, evidence=2)]
        result = self.check('2025年合并营业收入12.5亿元，较2024年同比增长25%。', facts)
        self.assertEqual(result['stats']['verified'], 2)
        self.assertEqual(result['claims'][1]['period'], '2025-12-31')

    def test_table_label_unit_overrides_global_amount_unit(self):
        metric = {'code': 'gross_margin', 'value': '.125', 'unit': 'ratio',
                  'period': '2025-12-31', 'scope': 'consolidated', 'status': 'calculated',
                  'evidence_ids': [f'{DOC}:p1:b3']}
        report = '# 合并报表\n单位：万元\n| 项目 | 2025年 |\n|---|---|\n| 毛利率（%） | 12.50 |'
        self.assertEqual(self.check(report, [], [metric])['stats']['verified'], 1)

    def test_respective_parallel_values_keep_field_identity(self):
        facts = [fact('revenue', 1000000000), fact('net_profit', 100000000, evidence=2)]
        result = self.check('2025年合并营业收入与净利润分别为10亿元和1亿元。', facts)
        self.assertEqual(result['stats']['verified'], 2)
        self.assertEqual([c['code'] for c in result['claims']], ['revenue', 'net_profit'])

    def test_ratio_multiple_is_not_an_amount(self):
        metric = {'code': 'current_ratio', 'value': '1.5', 'unit': 'ratio',
                  'period': '2025-12-31', 'scope': 'consolidated', 'status': 'calculated',
                  'evidence_ids': [f'{DOC}:p1:b3']}
        result = self.check('2025年合并流动比率1.50倍', [], [metric])
        self.assertEqual(result['stats']['verified'], 1)

    def test_currency_conflict_never_awards_verification(self):
        result = self.check('# 单位：美元\n2025年合并营收10亿元', [fact('revenue', 10**9)],
                            run={'currency': 'CNY'})
        self.assertEqual(result['claims'][0]['status'], 'unverifiable')
        self.assertIn('币种', result['claims'][0]['reason'])

    def test_explicit_company_header_conflict(self):
        result = self.check('公司名称：乙股份有限公司\n2025年合并营收10亿元',
                            [fact('revenue', 10**9)], run={'company': '甲股份有限公司'})
        self.assertEqual(result['claims'][0]['status'], 'unverifiable')
        self.assertIn('主体', result['claims'][0]['reason'])

    def test_quarter_cannot_use_annual_same_end_date(self):
        annual = fact('revenue', 10**9)
        for entry in (dict(annual, period_type='annual'), annual):
            result = review_claims('2025年第四季度合并营收10亿元', [entry])
            self.assertEqual(result['claims'][0]['status'], 'unverifiable')
        quarter = dict(annual, period_type='quarter')
        self.assertEqual(review_claims('2025年第四季度合并营收10亿元', [quarter])['stats']['verified'], 1)

    def test_model_results_are_conditional_and_scenario_specific(self):
        pool = [{'source': 'valuation', 'code': 'equity_value', 'label': 'equity_value(base)',
                 'value': '1000000000', 'kind': 'amount', 'period': None, 'scope': None, 'evidence_ids': []},
                {'source': 'valuation', 'code': 'equity_value', 'label': 'equity_value(stress)',
                 'value': '700000000', 'kind': 'amount', 'period': None, 'scope': None, 'evidence_ids': []}]
        result = review_claims('基准情景股权价值10亿元；压力情景股权价值10亿元；股权价值10亿元。', pool)
        self.assertEqual(len(result['claims']), 3)
        self.assertEqual(result['claims'][0]['model_status'], 'conditional_model_result_match')
        self.assertEqual(result['claims'][0]['status'], 'unverifiable')
        self.assertEqual(result['claims'][1]['status'], 'contradicted')
        self.assertEqual(result['claims'][1]['expected_value'], '700000000')
        self.assertEqual(result['claims'][2]['model_status'], 'unverifiable')
        self.assertEqual(result['stats']['verified'], 0)

    def test_model_table_and_parameters(self):
        pool = [{'source': 'valuation', 'code': 'wacc', 'label': 'wacc(base)', 'value': '.08', 'kind': 'ratio'},
                {'source': 'valuation', 'code': 'terminal_growth', 'label': 'terminal_growth(base)', 'value': '.02', 'kind': 'ratio'}]
        report = '| 参数 | 基准 |\n|---|---|\n| WACC（%） | 8 |\n| 永续增长率（%） | 2 |'
        result = review_claims(report, pool)
        self.assertEqual(len(result['claims']), 2)
        self.assertTrue(all(c['model_status'] == 'conditional_model_result_match' for c in result['claims']))
        self.assertEqual(result['stats']['verified'], 0)

    def test_chinese_adjacent_wrong_citation_is_recognized(self):
        result = self.check(f'2025年合并营业收入10亿元，依据{DOC}:p1:b2。',
                            [fact('revenue', 10**9), fact('net_profit', 10**8, evidence=2)])
        self.assertEqual(result['claims'][0]['status'], 'contradicted')
        self.assertIn('不支持', result['claims'][0]['reason'])

    def test_full_and_short_document_hashes_match_same_page_block(self):
        full = DOC + 'a' * (64 - len(DOC))
        source = fact('revenue', 10**9)
        source['evidence_ids'] = [f'{full}:p1:b1']
        self.assertEqual(self.check(f'2025年合并营业收入10亿元，依据{DOC}:p1:b1。', [source])['stats']['verified'], 1)
        source['evidence_ids'] = [f'{DOC}:p1:b1']
        self.assertEqual(self.check(f'2025年合并营业收入10亿元，依据{full}:p1:b1。', [source])['stats']['verified'], 1)
        self.assertEqual(self.check(f'2025年合并营业收入10亿元，依据{full}:p2:b1。', [source])['stats']['verified'], 0)

    def test_model_explicit_currency_conflict_is_unverifiable(self):
        pool = [{'source': 'valuation', 'code': 'equity_value', 'label': 'equity_value(base)',
                 'value': '1000000000', 'kind': 'amount', 'currency': 'CNY'}]
        result = review_claims('# 单位：美元\n基准情景股权价值10亿元。', pool)
        self.assertEqual(result['claims'][0]['model_status'], 'unverifiable')
        self.assertIn('币种', result['claims'][0]['reason'])

    def test_stats_and_json_serialization(self):
        import json
        result = self.check('2025年合并营收10亿元，净利润3亿元；毛利率40%。',
                            [fact('revenue', 10**9), fact('net_profit', 2*10**8, evidence=2)])
        self.assertEqual(result['stats'], {'checked': 3, 'verified': 1, 'contradicted': 1,
                                         'unverifiable': 1, 'coverage': 2/3})
        json.dumps(result)


if __name__ == '__main__':
    unittest.main()
