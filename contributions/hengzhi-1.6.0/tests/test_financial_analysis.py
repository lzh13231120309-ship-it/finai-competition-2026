import copy
import json
import unittest
from decimal import Decimal

try:
    from server.financial_analysis import extract_financial_facts, analyze_financials
except ImportError:
    extract_financial_facts = analyze_financials = None


def document(rows, context='合并利润表\n单位：万元 币种：人民币', text='2025年年度报告'):
    return {'document_id': 'doc1', 'filename': 'annual.pdf', 'sha256': 'abc',
            'page_count': 1, 'pages': [], 'text': text, 'warnings': [],
            'tables': [{'table_id': 't1', 'page': 1, 'context': context,
                        'rows': rows, 'method': 'pdfplumber', 'confidence': .97}]}


def fact(code, value, period='2025-12-31', kind='annual', scope='consolidated', **extra):
    result = {'fact_id': code + '-' + period + '-' + scope, 'code': code,
              'value': str(value), 'period': period, 'period_type': kind,
              'scope': scope, 'currency': 'CNY', 'company': '示例公司',
              'status': 'verified', 'scale': 1, 'unit': '元',
              'evidence_ids': ['t1'], 'confidence': .97}
    result.update(extra)
    return result


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(extract_financial_facts), 'financial extraction is not implemented')

    def test_units_negative_parentheses_missing_and_headers(self):
        result = extract_financial_facts(document([
            ['项目', '2025年度', '2024年度'], ['营业收入', '1,234.50', '1,000'],
            ['净利润', '(25.50)', '—'], ['经营活动产生的现金流量净额', '100', '90']]), company='示例公司')
        revenue = next(f for f in result['facts'] if f['code'] == 'revenue' and f['period'] == '2025-12-31')
        self.assertEqual(revenue['value'], '12345000')
        self.assertEqual(revenue['period_type'], 'annual')
        self.assertEqual(revenue['scope'], 'consolidated')
        self.assertEqual(revenue['scale'], 10000)
        self.assertTrue(revenue['evidence_ids'])
        profits = [f for f in result['facts'] if f['code'] == 'net_profit']
        self.assertEqual(len(profits), 1)
        self.assertEqual(profits[0]['value'], '-255000')
        json.dumps(result)

    def test_unknown_metadata_is_not_assumed(self):
        result = extract_financial_facts(document([['项目', '本期'], ['营业收入', '100']], context='', text=''))
        f = result['facts'][0]
        self.assertEqual((f['period'], f['scope'], f['currency']), ('', 'unknown', 'unknown'))
        self.assertEqual(f['status'], 'needs_review')
        self.assertTrue(result['review_queue'])

    def test_multi_level_ytd_and_quarter_columns(self):
        result = extract_financial_facts(document([
            ['项目', '2025年', '2025年', '2024年'],
            ['', '1-9月', '7-9月', '1-9月'], ['营业收入', '900', '300', '800']],
            text='2025年第三季度报告'))
        self.assertEqual([(f['period'], f['period_type']) for f in result['facts']],
                         [('2025-09-30', 'ytd'), ('2025-09-30', 'quarter'), ('2024-09-30', 'ytd')])

    def test_balance_sheet_is_instant_parent_scope(self):
        result = extract_financial_facts(document([['项目', '2025年12月31日'], ['资产总计', '100']],
             context='母公司资产负债表 单位：亿元'))
        f = result['facts'][0]
        self.assertEqual((f['scope'], f['period_type'], f['value']), ('parent', 'instant', '10000000000'))

    def test_conflicting_duplicates_are_retained_for_review(self):
        doc = document([['项目', '2025年度'], ['营业收入', '100']])
        other = copy.deepcopy(doc['tables'][0]); other['table_id'] = 't2'
        other['rows'][1][1] = '110'; other['context'] += ' 追溯调整后'
        doc['tables'].append(other)
        result = extract_financial_facts(doc)
        self.assertEqual(len(result['facts']), 2)
        self.assertTrue(all(f['status'] == 'needs_review' for f in result['facts']))
        self.assertTrue(any('重述' in w or '调整' in w for w in result['warnings']))
        self.assertFalse(analyze_financials(result['facts'])['metrics'])

    def test_requested_scope_does_not_relabel_parent_statement(self):
        result = extract_financial_facts(document([['项目', '2025年度'], ['营业收入', '100']],
            context='母公司利润表 单位：万元'), scope='consolidated')
        self.assertFalse(result['facts'])
        self.assertTrue(result['warnings'])

    def test_explicit_company_overrides_conflicting_caller_identity_with_review(self):
        result = extract_financial_facts(document([['项目', '2025年度'], ['营业收入', '100']],
            text='乙股份有限公司\n2025年年度报告'), company='甲股份有限公司')
        self.assertEqual(result['facts'][0]['company'], '乙股份有限公司')
        self.assertEqual(result['facts'][0]['status'], 'needs_review')
        self.assertTrue(result['review_queue'])

    def test_half_year_balance_bare_year_header_uses_june(self):
        result = extract_financial_facts(document([['项目', '2025年', '2024年'], ['资产总计', '100', '90']],
            context='合并资产负债表 单位：万元', text='2025年半年度报告'))
        self.assertEqual(result['facts'][0]['period'], '2025-06-30')
        self.assertEqual(result['facts'][1]['period'], '')
        self.assertEqual(result['facts'][1]['status'], 'needs_review')

    def test_bare_year_balance_without_reporting_end_is_reviewed(self):
        result = extract_financial_facts(document([['项目', '2025年'], ['资产总计', '100']],
            context='合并资产负债表 单位：万元', text=''))
        self.assertEqual(result['facts'][0]['status'], 'needs_review')

    def test_parent_profit_label_in_context_does_not_change_consolidated_scope(self):
        result = extract_financial_facts(document([['项目', '2025年度'], ['归属于母公司所有者的净利润', '100']],
                 context='合并利润表 单位：万元 归属于母公司所有者的净利润'))
        self.assertEqual(result['facts'][0]['scope'], 'consolidated')
        self.assertEqual(result['facts'][0]['status'], 'verified')

    def test_same_period_different_scopes_do_not_conflict(self):
        doc = document([['项目', '2025年度'], ['营业收入', '100']])
        other = copy.deepcopy(doc['tables'][0]); other['table_id'] = 't2'
        other['context'] = '母公司利润表 单位：万元'; other['rows'][1][1] = '40'
        doc['tables'].append(other)
        result = extract_financial_facts(doc)
        self.assertTrue(all(f['status'] == 'verified' for f in result['facts']))
        self.assertEqual({f['scope'] for f in result['facts']}, {'consolidated', 'parent'})

    def test_quarter_report_role_headers_and_bare_years(self):
        result = extract_financial_facts(document([
            ['项目', '本报告期', '年初至报告期末'], ['营业收入', '300', '900']],
            text='2025年第三季度报告'))
        self.assertEqual([(f['period'], f['period_type']) for f in result['facts']],
                         [('2025-09-30', 'quarter'), ('2025-09-30', 'ytd')])
        result = extract_financial_facts(document([
            ['项目', '2025年', '2024年'], ['营业收入', '900', '800']], text='2025年第三季度报告'))
        self.assertEqual([(f['period'], f['period_type']) for f in result['facts']],
                         [('2025-09-30', 'ytd'), ('2024-09-30', 'ytd')])

    def test_full_chinese_date_range_and_half_year(self):
        result = extract_financial_facts(document([
            ['项目', '2025年1月1日至2025年6月30日'], ['营业收入', '500']], text='2025年半年度报告'))
        self.assertEqual(result['facts'][0]['period'], '2025-06-30')
        self.assertEqual(result['facts'][0]['period_type'], 'ytd')

    def test_cell_evidence_is_used_for_both_label_and_number(self):
        doc = document([['项目', '2025年度'], ['营业收入', '100']])
        doc['tables'][0]['cell_evidence_ids'] = [['head-label', 'head-year'], ['label-proof', 'amount-proof']]
        extracted = extract_financial_facts(doc)['facts'][0]
        self.assertIn('label-proof', extracted['evidence_ids'])
        self.assertIn('amount-proof', extracted['evidence_ids'])

    def test_compile_company_name_does_not_supply_unit(self):
        result = extract_financial_facts(document([['项目', '2025年度'], ['营业收入', '100']],
            context='合并利润表 编制单位：元通有限公司'))
        self.assertEqual(result['facts'][0]['unit'], 'unknown')
        self.assertEqual(result['facts'][0]['status'], 'needs_review')

    def test_percentage_cannot_become_monetary_fact(self):
        result = extract_financial_facts(document([['项目', '2025年度'], ['营业收入', '20%']]))
        self.assertEqual(result['facts'][0]['value'], '0.2')
        self.assertEqual(result['facts'][0]['unit'], '%')
        self.assertEqual(result['facts'][0]['status'], 'needs_review')

    def test_per_share_units_do_not_use_table_scale(self):
        result = extract_financial_facts(document([['项目', '2025年度'], ['基本每股收益（元/股）', '1.25']]))
        self.assertEqual(result['facts'][0]['value'], '1.25')
        self.assertEqual(result['facts'][0]['scale'], 1)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(analyze_financials), 'financial analysis is not implemented')

    def metric(self, result, code):
        return next(m for m in result['metrics'] if m['code'] == code)

    def test_growth_margins_and_evidence(self):
        result = analyze_financials([fact('revenue', 120), fact('revenue', 100, '2024-12-31'),
                                    fact('cost_of_revenue', 60), fact('net_profit', 12)])
        self.assertEqual(Decimal(self.metric(result, 'revenue_yoy')['value']), Decimal('.2'))
        self.assertEqual(Decimal(self.metric(result, 'gross_margin')['value']), Decimal('.5'))
        self.assertEqual(Decimal(self.metric(result, 'net_margin')['value']), Decimal('.1'))
        self.assertTrue(self.metric(result, 'gross_margin')['formula'])
        self.assertEqual(len(self.metric(result, 'gross_margin')['dependency_fact_ids']), 2)
        self.assertTrue(self.metric(result, 'gross_margin')['evidence_ids'])
        json.dumps(result)

    def test_zero_and_negative_growth_base_are_not_misleading(self):
        zero = analyze_financials([fact('revenue', 10), fact('revenue', 0, '2024-12-31')])
        self.assertIsNone(self.metric(zero, 'revenue_yoy')['value'])
        # 负基数：外部数据源（F10 同比列）对负基数照算，为可对账这里也给值；
        # 但必须进复核队列警示"增减语义翻转"，正文引用时改述绝对额。
        negative = analyze_financials([fact('net_profit', 10), fact('net_profit', -5, '2024-12-31')])
        yoy = self.metric(negative, 'net_profit_yoy')
        self.assertIsNotNone(yoy['value'])
        self.assertTrue(negative['review_queue'])
        self.assertTrue(any('负数' in (r.get('message') or '') for r in negative['review_queue']))

    def test_balance_sheet_tieout_and_rounding(self):
        facts = [fact('assets', 1000000, kind='instant', scale=10000),
                 fact('liabilities', 600000, kind='instant', scale=10000),
                 fact('equity', 399999, kind='instant', scale=10000)]
        result = analyze_financials(facts)
        self.assertEqual(result['checks'][0]['status'], 'pass')
        self.assertEqual(result['facts'], facts)
        facts[2]['value'] = '350000'
        result = analyze_financials(facts)
        self.assertEqual(result['checks'][0]['status'], 'fail')
        self.assertTrue(result['review_queue'])
        self.assertEqual(result['facts'][2]['value'], '350000')

    def test_roe_uses_average_matching_parent_equity(self):
        result = analyze_financials([fact('parent_net_profit', 15),
                     fact('parent_equity', 100, kind='instant'),
                     fact('parent_equity', 50, '2024-12-31', kind='instant')])
        self.assertEqual(Decimal(self.metric(result, 'roe')['value']), Decimal('.2'))
        missing = analyze_financials([fact('parent_net_profit', 15), fact('parent_equity', 100, kind='instant')])
        self.assertFalse(any(m['code'] == 'roe' for m in missing['metrics']))

    def test_scope_currency_and_review_data_are_not_mixed(self):
        facts = [fact('revenue', 100), fact('net_profit', 10, scope='parent'),
                 fact('cost_of_revenue', 20, currency='USD'),
                 fact('operating_cash_flow', 30, status='needs_review')]
        result = analyze_financials(facts)
        self.assertFalse(any(m['code'] in {'net_margin', 'gross_margin', 'cash_conversion'} for m in result['metrics']))

    def test_quarter_derived_only_from_matching_ytd(self):
        result = analyze_financials([fact('revenue', 900, '2025-09-30', 'ytd'),
                                    fact('revenue', 600, '2025-06-30', 'ytd')])
        q = self.metric(result, 'revenue_quarter')
        self.assertEqual(q['value'], '300')
        self.assertEqual(q['period_type'], 'quarter')
        self.assertEqual(len(q['dependency_fact_ids']), 2)
        bad = analyze_financials([fact('revenue', 900, '2025-09-30', 'ytd'),
                                 fact('revenue', 600, '2025-03-31', 'ytd')])
        self.assertFalse(any(m['code'] == 'revenue_quarter' for m in bad['metrics']))

    def test_working_capital_turnover_uses_period_average(self):
        result = analyze_financials([fact('cost_of_revenue', 300),
                 fact('inventory', 100, kind='instant'), fact('inventory', 50, '2024-12-31', 'instant')])
        self.assertEqual(Decimal(self.metric(result, 'inventory_turnover')['value']), Decimal('4'))

    def test_duplicate_input_conflict_is_rejected(self):
        result = analyze_financials([fact('revenue', 100), fact('revenue', 110), fact('net_profit', 10)])
        self.assertFalse(any(m['code'] == 'net_margin' for m in result['metrics']))
        self.assertTrue(result['review_queue'])

    def test_positive_profit_weak_cash_signal_has_rule_and_evidence(self):
        result = analyze_financials([fact('net_profit', 100), fact('operating_cash_flow', 40)])
        signal = next(s for s in result['signals'] if s['code'] == 'weak_profit_cash_conversion')
        self.assertEqual(signal['threshold'], '0.5')
        self.assertEqual(signal['observed_value'], '0.4')
        self.assertTrue(signal['heuristic'])
        self.assertEqual(len(signal['input_facts']), 2)
        self.assertEqual(len(signal['dependency_fact_ids']), 2)
        self.assertTrue(signal['evidence_ids'])
        self.assertTrue(signal['rule'])

    def test_cash_signal_requires_positive_profit_and_matching_scope(self):
        result = analyze_financials([fact('net_profit', -100), fact('operating_cash_flow', 40)])
        self.assertFalse(any(s['code'] == 'weak_profit_cash_conversion' for s in result['signals']))
        mismatch = analyze_financials([fact('net_profit', 100), fact('operating_cash_flow', 40, scope='parent')])
        self.assertFalse(any(s['code'] == 'weak_profit_cash_conversion' for s in mismatch['signals']))

    def test_working_capital_growth_signal_matches_source_dates(self):
        result = analyze_financials([fact('revenue', 110), fact('revenue', 100, '2024-12-31'),
                    fact('receivables', 160, kind='instant'), fact('receivables', 100, '2024-12-31', 'instant'),
                    fact('inventory', 150, kind='instant'), fact('inventory', 100, '2024-12-31', 'instant')])
        signals = [s for s in result['signals'] if s['code'].endswith('_growth_ahead_of_revenue')]
        self.assertEqual(len(signals), 2)
        self.assertEqual({s['observed_value'] for s in signals}, {'0.5', '0.4'})
        self.assertTrue(all(len(s['dependency_fact_ids']) == 4 for s in signals))
        self.assertTrue(all(s['threshold'] == '0.2' for s in signals))

    def test_working_capital_growth_signal_rejects_mismatches_and_zero_base(self):
        for extra in ({'company': '乙公司'}, {'scope': 'parent'}, {'currency': 'USD'}):
            facts = [fact('revenue', 110), fact('revenue', 100, '2024-12-31'),
                     fact('receivables', 160, kind='instant', **extra),
                     fact('receivables', 100, '2024-12-31', 'instant', **extra)]
            self.assertFalse(any(s['code'].endswith('_growth_ahead_of_revenue') for s in analyze_financials(facts)['signals']))
        zero = analyze_financials([fact('revenue', 110), fact('revenue', 100, '2024-12-31'),
                     fact('receivables', 160, kind='instant'), fact('receivables', 0, '2024-12-31', 'instant')])
        self.assertFalse(any(s['code'].endswith('_growth_ahead_of_revenue') for s in zero['signals']))

    def test_nonrecurring_share_uses_disclosed_parent_and_adjusted_profit(self):
        result = analyze_financials([fact('parent_net_profit', 100), fact('nonrecurring_adj_parent_profit', 60)])
        self.assertEqual(self.metric(result, 'nonrecurring_profit_share')['value'], '0.4')
        signal = next(s for s in result['signals'] if s['code'] == 'nonrecurring_profit_concentration')
        self.assertEqual(signal['threshold'], '0.3')
        self.assertEqual(len(signal['input_facts']), 2)
        negative = analyze_financials([fact('parent_net_profit', -100), fact('nonrecurring_adj_parent_profit', -160)])
        self.assertFalse(any(s['code'] == 'nonrecurring_profit_concentration' for s in negative['signals']))
        mismatch = analyze_financials([fact('parent_net_profit', 100), fact('nonrecurring_adj_parent_profit', 60, currency='USD')])
        self.assertFalse(any(m['code'] == 'nonrecurring_profit_share' for m in mismatch['metrics']))

    def test_anonymous_documents_do_not_mix_companies(self):
        result = analyze_financials([fact('revenue', 100, company='', source={'document_id': 'a'}),
                                    fact('net_profit', 10, company='', source={'document_id': 'b'})])
        self.assertFalse(any(m['code'] == 'net_margin' for m in result['metrics']))
        same = analyze_financials([fact('revenue', 100, company='', source={'document_id': 'a'}),
                                   fact('net_profit', 10, company='', source={'document_id': 'a'})])
        self.assertEqual(self.metric(same, 'net_margin')['value'], '0.1')

    def test_anonymous_reports_do_not_create_cross_report_growth(self):
        result = analyze_financials([fact('revenue', 100, company='', source={'document_id': 'a'}),
                                    fact('revenue', 80, '2024-12-31', company='', source={'document_id': 'b'})])
        self.assertFalse(any(m['code'] == 'revenue_yoy' for m in result['metrics']))

    def test_invalid_date_cannot_crash_analysis(self):
        result = analyze_financials([fact('revenue', 100, '2025-99-30'), fact('net_profit', 10, '2025-99-30')])
        self.assertFalse(result['metrics'])
        self.assertTrue(result['review_queue'])

    def test_restated_and_unrestated_ytd_are_not_subtracted(self):
        result = analyze_financials([fact('revenue', 900, '2025-09-30', 'ytd', restated=True),
                                    fact('revenue', 600, '2025-06-30', 'ytd', restated=False)])
        self.assertFalse(any(m['code'] == 'revenue_quarter' for m in result['metrics']))
        self.assertTrue(result['review_queue'])

    def test_no_false_cash_funds_tieout(self):
        result = analyze_financials([fact('cash', 100, kind='instant'),
                                    fact('cash_equivalents_end', 50)])
        self.assertFalse(any('cash' in c['code'] for c in result['checks']))


class AmbiguousLabelTests(unittest.TestCase):
    """「同一标签、两张报表、不同概念」的消歧回归。

    港式年报把綜合損益表与綜合全面收益表分作两张表，两张表里都有一行写作
    「母公司擁有人應佔」。旧词典只收了带"溢利"的写法，原文不带，于是
    两条都匹配不上 —— 归母净利润整块静默丢失（实测农夫山泉 2025 年报 2024 年
    归母净利润 12,123,304 千元在原文第 115 页，却没进 facts）。
    这里固定住修复后的行为：損益表那行 → parent_net_profit，
    全面收益表那行 → parent_total_comprehensive_income，两者不再互相冲突。
    """

    def setUp(self):
        self.assertTrue(callable(extract_financial_facts), 'financial extraction is not implemented')

    def _rows(self, label):
        return [['项目', '2025年', '2024年'],
                ['年內溢利  PROFIT FOR THE YEAR', '15,868,274', '12,123,304'],
                [label, '15,868,224', '12,123,713']]

    @staticmethod
    def _raw(result, period='2025-12-31'):
        return {f['code']: f['raw_value'] for f in result['facts'] if f['period'] == period}

    def test_profit_or_loss_table_maps_to_parent_net_profit(self):
        result = extract_financial_facts(document(
            self._rows('母公司擁有人應佔  Attributable to owners of the parent'),
            context='綜合損益表 CONSOLIDATED STATEMENT OF PROFIT OR LOSS\n人民幣千元'))
        codes = self._raw(result)
        self.assertEqual(codes.get('parent_net_profit'), '15,868,224')
        self.assertNotIn('parent_total_comprehensive_income', codes)

    def test_comprehensive_income_table_maps_to_its_own_code(self):
        result = extract_financial_facts(document(
            self._rows('母公司擁有人應佔  Attributable to owners of the parent'),
            context='綜合全面收益表 CONSOLIDATED STATEMENT OF COMPREHENSIVE INCOME\n人民幣千元'))
        codes = self._raw(result)
        self.assertEqual(codes.get('parent_total_comprehensive_income'), '15,868,224')
        self.assertNotIn('parent_net_profit', codes)

    def test_both_tables_together_do_not_conflict(self):
        """两张表都出现时，两个代码各拿各的值，不产生 duplicate_conflict。"""
        merged = document(
            self._rows('母公司擁有人應佔  Attributable to owners of the parent'),
            context='綜合損益表 CONSOLIDATED STATEMENT OF PROFIT OR LOSS\n人民幣千元')
        second = document(
            self._rows('母公司擁有人應佔  Attributable to owners of the parent'),
            context='綜合全面收益表 CONSOLIDATED STATEMENT OF COMPREHENSIVE INCOME\n人民幣千元')
        second['tables'][0]['table_id'] = 't2'
        merged['tables'].append(second['tables'][0])
        result = extract_financial_facts(merged)
        got = {}
        for f in result['facts']:
            if f['period'] == '2025-12-31' and f['code'].startswith('parent_'):
                got.setdefault(f['code'], set()).add(f['raw_value'])
        self.assertEqual(got.get('parent_net_profit'), {'15,868,224'})
        self.assertEqual(got.get('parent_total_comprehensive_income'), {'15,868,224'})
        self.assertFalse(any('parent_net_profit' in str(r.get('message', ''))
                             for r in result.get('review_queue', [])))

    def test_unknown_context_does_not_guess(self):
        """上下文判不出是哪张表时，宁可两条都不认，也不猜一个。"""
        result = extract_financial_facts(document(
            self._rows('母公司擁有人應佔  Attributable to owners of the parent'),
            context='人民幣千元'))
        codes = {f['code'] for f in result['facts']}
        self.assertNotIn('parent_net_profit', codes)
        self.assertNotIn('parent_total_comprehensive_income', codes)


if __name__ == '__main__':
    unittest.main()
