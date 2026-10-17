"""报告自检引擎的回归测试。

这些用例的价值在于**固定住已经踩过的坑**：每一个断言背后都对应一次真实的误报或漏报。
没有它们，下一次改匹配逻辑时，很容易把"零误报"重新变成"六处误报"。
"""

import unittest
from decimal import Decimal

from server import report_audit as RA

E8 = Decimal('100000000')          # 亿元
DOC = 'baffc40b1939f3d4'           # 文档短哈希，和真实报告里的引用形式一致


def fact(code, value, period='2025-12-31', ptype='instant', scope='consolidated',
         label=None, evidence=None):
    return {'fact_id': f'{code}:{period}', 'code': code, 'value': str(value),
            'raw_label': label or code, 'period': period, 'period_type': ptype,
            'scope': scope, 'status': 'verified',
            'evidence_ids': evidence or [f'{DOC}:p1:b1']}


def metric(code, value, period='2025-12-31', unit='ratio'):
    return {'code': code, 'value': str(value), 'period': period, 'scope': 'consolidated',
            'unit': unit, 'status': 'calculated', 'evidence_ids': [f'{DOC}:p1:b2']}


class NumberTraceTests(unittest.TestCase):
    """正向检查：报告里的金额必须能在工具产出里找到。"""

    def setUp(self):
        self.pool = RA.build_pool([fact('revenue', 52552910000, ptype='annual'),
                                   fact('capex', -6481304000, ptype='annual')], [])

    def test_yi_unit_is_normalised_and_traced(self):
        out = RA.audit_numbers('2025 年收入 525.53 亿元，同比增长。', self.pool)
        self.assertEqual(out['untraced'], [])
        self.assertEqual(out['traced'], 1)
        self.assertIn('revenue', out['examples'][0]['matched'])

    def test_sign_insensitive_for_cash_outflow(self):
        """现金流量表的资本开支在工具里是负数，报告写正数，不算错。

        回归背景：旧实现按原值匹配，把「资本开支 64.81 亿」判成"凭空多出来的数"，
        实测一次报出 3 处误报。
        """
        out = RA.audit_numbers('资本开支 64.81 亿，同比基本持平。', self.pool)
        self.assertEqual(out['untraced'], [])

    def test_conversion_factor_is_not_a_financial_number(self):
        """「千元」×1,000 是单位换算说明，不是财务数字。

        回归背景：旧实现把它当成未溯源金额，每次自检都固定报 2 处误报。
        """
        out = RA.audit_numbers('单位归一由程序完成（「千元」×1,000 归一到元）。', self.pool)
        self.assertEqual(out['checked'], 0)
        self.assertEqual(out['untraced'], [])

    def test_untraced_amount_is_reported(self):
        out = RA.audit_numbers('2025 年收入 999.99 亿元。', self.pool)
        self.assertEqual(len(out['untraced']), 1)
        self.assertEqual(out['untraced'][0]['raw'], '999.99亿元')

    def test_sum_of_two_subjects_is_accepted(self):
        """「现金及长期存款合计 222.65 亿」是两科目之和，不该判成编造。"""
        pool = RA.build_pool([fact('cash', 11178000000),
                              fact('long_term_deposits', 11088000000)], [])
        out = RA.audit_numbers('现金及长期银行存款合计 222.65 亿元。', pool)
        self.assertEqual(out['untraced'], [])
        self.assertIn('+', out['examples'][0]['matched'])

    def test_untraced_fact_is_not_trusted(self):
        """待复核事实不进可信池，否则等于自证。"""
        item = fact('revenue', 12345000000)
        item['status'] = 'needs_review'
        pool = RA.build_pool([item], [])
        self.assertEqual(pool, [])


class CountAuditTests(unittest.TestCase):
    """计数对账：报告写的「N 项」必须等于工具字段。"""

    def setUp(self):
        self.run = {'facts': [fact('revenue', 1), fact('revenue', 2),
                              fact('revenue', 3)],
                    'metrics': [metric('gross_margin', '0.6')],
                    'checks': [{'status': 'pass'}] * 23,
                    'review_queue': [{'id': i} for i in range(26)]}
        self.forensic = {'findings': [{'rule_id': 'R1'}]}

    def test_mismatch_is_caught(self):
        """实测抓过的硬错误：标题写 19 项，工具是 26 项。"""
        out = RA.audit_counts('待复核事项 | 19 项（明细见第八节）', self.run, self.forensic)
        self.assertEqual(len(out['mismatches']), 1)
        self.assertEqual(out['mismatches'][0]['report_says'], 19)
        self.assertEqual(out['mismatches'][0]['tool_says'], 26)

    def test_matching_count_passes(self):
        out = RA.audit_counts('待复核事项 | 26 项', self.run, self.forensic)
        self.assertEqual(out['mismatches'], [])
        self.assertEqual(out['matches'][0]['field'], '待复核事项数')

    def test_verified_count_wins_over_generic_fact_count(self):
        """「已核验财务事实 | 3 条（其中 verified 3 条）」两个数都要对上。"""
        out = RA.audit_counts('已核验财务事实 | 3 条（其中 verified 3 条）',
                              self.run, self.forensic)
        fields = {m['field'] for m in out['matches']}
        self.assertIn('事实总数', fields)
        self.assertIn('已核验事实数', fields)
        self.assertEqual(out['mismatches'], [])

    def test_sub_count_after_failure_word_is_ignored(self):
        """「23 项，失败 0 项」里的 0 是子计数，不是勾稽检查总数。

        回归背景：旧实现把 0 当成勾稽检查数，与 23 比对后误报"计数不符"。
        """
        out = RA.audit_counts('三表勾稽检查 | 23 项，失败 0 项', self.run, self.forensic)
        self.assertEqual([m['field'] for m in out['matches']], ['勾稽检查数'])
        self.assertEqual(out['mismatches'], [])

    def test_no_run_means_not_available(self):
        out = RA.audit_counts('待复核 19 项', None, None)
        self.assertFalse(out['available'])


class CitationTests(unittest.TestCase):
    """引用核验：直接对应「伪造引用」这条一票否决。"""

    def setUp(self):
        self.run = {'facts': [fact('revenue', 1, evidence=[f'{DOC}:p11:b32'])]}

    def test_known_citation_passes(self):
        out = RA.audit_citations(f'见证据 {DOC}:p11:b32。', self.run)
        self.assertEqual(out['unknown'], [])
        self.assertEqual(out['citation_coverage'], '1.0000')

    def test_fabricated_citation_is_caught(self):
        out = RA.audit_citations(f'见证据 {DOC}:p999:b99。', self.run)
        self.assertEqual(out['unknown'], [f'{DOC}:p999:b99'])

    def test_full_hash_citation_matches_short_prefix(self):
        """报告里用短前缀，工具里存全量哈希，必须按前缀认出来。"""
        full = DOC + '0' * 48
        run = {'facts': [fact('revenue', 1, evidence=[f'{full}:p11:b32'])]}
        out = RA.audit_citations(f'见证据 {DOC}:p11:b32。', run)
        self.assertEqual(out['unknown'], [])

    def test_no_citation_is_flagged_in_note(self):
        out = RA.audit_citations('报告里没有任何证据引用。', self.run)
        self.assertEqual(out['cited'], 0)
        self.assertIn('未出现任何', out['note'])


class VetoTests(unittest.TestCase):
    """一票否决扫描：目标价、收益保证、确定性荐股。"""

    def test_target_price_is_caught(self):
        out = RA.audit_veto('我们给出目标价 35 元。')
        self.assertTrue(out)
        self.assertIn('目标价', out[0]['type'])

    def test_return_guarantee_is_caught(self):
        out = RA.audit_veto('该标的保证收益，稳赚不赔。')
        self.assertTrue(out)

    def test_buy_rating_is_caught(self):
        out = RA.audit_veto('给予买入评级。')
        self.assertTrue(out)

    def test_disclaimer_is_exempt(self):
        """规范的免责声明本来就该出现这些字眼，不能判成违规。"""
        out = RA.audit_veto('本报告不构成投资建议，不给出目标价，也不提供收益承诺。')
        self.assertEqual(out, [])

    def test_plain_analysis_is_clean(self):
        out = RA.audit_veto('三情景权益价值区间为 2,771 亿元至 3,243 亿元。')
        self.assertEqual(out, [])


class CoverageTests(unittest.TestCase):
    """反向检查：必需科目是否在报告里出现。"""

    def setUp(self):
        self.pool = RA.build_pool([
            fact('revenue', 52552910000, ptype='annual'),
            fact('net_profit', 15868274000, ptype='annual'),
            fact('assets', 65169000000),
        ], [])

    def test_missing_required_subject_is_reported(self):
        out = RA.audit_coverage('2025 年收入 525.53 亿元，年内溢利 158.68 亿元。', self.pool)
        codes = [m['code'] for m in out['required_missing']]
        self.assertIn('assets', codes)
        self.assertNotIn('revenue', codes)

    def test_all_present_when_numbers_appear(self):
        text = '收入 525.53 亿元，净利润 158.68 亿元，资产总计 651.69 亿元。'
        out = RA.audit_coverage(text, self.pool)
        self.assertEqual(out['required_missing'], [])
        self.assertEqual(out['required_coverage_rate'], '1.0000')


class GradeTests(unittest.TestCase):
    """五维度打分与竞赛指标对照。"""

    def test_clean_report_scores_five_on_facts(self):
        result = RA.audit_report('收入 525.53 亿元。',
                                 {'facts': [fact('revenue', 52552910000, ptype='annual')],
                                  'metrics': [], 'checks': [{'status': 'pass'}]})
        facts_dim = next(d for d in result['grade']['dimensions'] if d['name'] == '事实正确')
        self.assertEqual(facts_dim['score'], 5)

    def test_count_mismatch_drops_fact_score(self):
        run = {'facts': [fact('revenue', 52552910000, ptype='annual')], 'metrics': [],
               'checks': [], 'review_queue': [{'id': 1}] * 26}
        result = RA.audit_report('收入 525.53 亿元。待复核事项 19 项。', run)
        facts_dim = next(d for d in result['grade']['dimensions'] if d['name'] == '事实正确')
        self.assertLess(facts_dim['score'], 5)
        self.assertEqual(result['hard_issue_count'], 1)

    def test_competition_metrics_map_is_complete(self):
        result = RA.audit_report('收入 525.53 亿元。',
                                 {'facts': [fact('revenue', 52552910000, ptype='annual')],
                                  'metrics': [], 'checks': []})
        names = [m['指标'] for m in result['competition_metrics']]
        self.assertIn('引用精确率', names)
        self.assertIn('数值可溯源率', names)
        self.assertIn('报告质量平均分', names)
        self.assertIn('一票否决项', names)


class DocxReadTests(unittest.TestCase):
    """docx 兜底读取：某些文件用 python-docx 读出来表格是空的。"""

    def test_missing_file_raises(self):
        with self.assertRaises(ValueError):
            RA.audit_file('/tmp/不存在的报告-xyz.docx')


class EndToEndTests(unittest.TestCase):
    """端到端：一份"看起来完整但藏了一处计数错误"的报告。"""

    def test_full_audit_catches_the_planted_error(self):
        cited = [f'{DOC}:p11:b32']
        run = {'facts': [fact('revenue', 52552910000, ptype='annual', evidence=cited),
                         fact('net_profit', 15868274000, ptype='annual', evidence=cited),
                         fact('parent_net_profit', 15868274000, ptype='annual', evidence=cited),
                         fact('operating_cash_flow', 21141652000, ptype='annual', evidence=cited),
                         fact('assets', 65169000000, evidence=cited),
                         fact('equity', 39470000000, evidence=cited)],
               'metrics': [metric('gross_margin', '0.6053')],
               'checks': [{'status': 'pass'}] * 23,
               'review_queue': [{'id': i} for i in range(26)]}
        text = (
            '一、结论摘要\n'
            '2025 年收入 525.53 亿元，毛利率 60.53%。\n'
            '三表勾稽检查 | 23 项，失败 0 项\n'
            '待复核事项 | 19 项\n'
            f'见证据 {DOC}:p11:b32。\n'
            '本报告不构成投资建议。\n'
        )
        result = RA.audit_report(text, run)
        self.assertEqual(result['hard_issue_count'], 1)
        self.assertIn('计数不符', result['issues'][0]['type'])
        self.assertIn('19', result['issues'][0]['detail'])
        # 金额全部可溯源、引用全部存在、无一票否决
        self.assertEqual(result['numbers']['untraced'], [])
        self.assertEqual(result['citations']['unknown'], [])
        self.assertEqual(result['grade']['veto_items'], [])


if __name__ == '__main__':
    unittest.main()
