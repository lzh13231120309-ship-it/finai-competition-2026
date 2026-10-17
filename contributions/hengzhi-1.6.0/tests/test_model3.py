"""平衡三表联动预测的回归测试。

重点覆盖三件事：
1. 期初表能从事实重建并**自身配平**（含总括行倒挤与科目去重）；
2. 预测表四项勾稽恒等成立（资产=负债+权益、现金滚动、留存收益滚存、现金非负）；
3. 三表反推的 FCFF 在**核心口径**下与驱动法逐字一致 —— 这是模型自洽的充要证据。
"""

import json
import unittest
from decimal import Decimal as D

from server import model3


def fact(code, value, period='2025-12-31', scope='consolidated', status='verified',
         unit='CNY'):
    return {'code': code, 'value': str(value), 'period': period, 'scope': scope,
            'status': status, 'value_unit': unit,
            'evidence_ids': [f'deadbeef:p1:b{code}']}


def metric(code, value, period='2025-12-31', scope='consolidated', unit='ratio'):
    return {'code': code, 'value': str(value), 'period': period, 'scope': scope,
            'unit': unit, 'status': 'calculated'}


def balanced_facts(revenue='1000'):
    """一份**定义上就平**的最小报表：资产 1000 = 负债 400 + 权益 600。

    刻意让每个总括行都等于其明细之和，期初表因此不需要倒挤，
    便于把"倒挤"与"配平"两件事分开测试。

    营运资本刻意**不为 0**（非现金流动资产 240 − 流动负债 200 = 40）：
    若取 0，首年 ΔNWC 与 NWC 都等于 0，"是否用期初余额做起点"这件事
    就会被测试静默放过 —— 那正是曾经把首年 ΔNWC 算成 NWC 绝对额的那个 bug。
    """
    return [
        fact('assets', '1000'), fact('current_assets', '300'), fact('noncurrent_assets', '700'),
        fact('liabilities', '400'), fact('current_liabilities', '200'),
        fact('noncurrent_liabilities', '200'), fact('equity', '600'), fact('parent_equity', '600'),
        fact('share_capital', '100'),
        fact('cash', '60'), fact('receivables', '50'), fact('inventory', '100'),
        fact('prepayments', '60'), fact('other_receivables', '30'),
        fact('fixed_assets', '600'), fact('intangibles', '100'),
        fact('payables', '80'), fact('contract_liabilities', '50'),
        fact('tax_payables', '30'), fact('other_payables', '40'),
        fact('deferred_tax_liabilities', '200'),
        fact('revenue', revenue), fact('cost_of_revenue', '400'),
    ]


def base_params(**overrides):
    params = {
        'years': 3, 'revenue_growth': D('0.10'), 'gross_margin': D('0.60'),
        'opex_ratio': D('0.20'), 'tax_rate': D('0.25'),
        'da_to_revenue': D('0.05'), 'capex_to_revenue': D('0.08'),
        'receivable_days': D('18.25'), 'inventory_days': D('91.25'),
        'payable_days': D('73'), 'cost_of_debt': D('0.04'), 'cash_yield': D('0.02'),
    }
    params.update(overrides)
    return params


class OpeningBalanceTests(unittest.TestCase):
    def test_balanced_opening(self):
        opening = model3.opening_balance(balanced_facts(), [])
        self.assertTrue(opening['balanced'])
        self.assertEqual(opening['residual'], 0)
        self.assertEqual(opening['period'], '2025-12-31')

    def test_totals_match_reported(self):
        opening = model3.opening_balance(balanced_facts(), [])
        self.assertEqual(opening['total_assets'], D('1000'))
        self.assertEqual(opening['total_liabilities'], D('400'))
        self.assertEqual(opening['equity'], D('600'))

    def test_retained_earnings_is_parent_equity_minus_capital(self):
        opening = model3.opening_balance(balanced_facts(), [])
        self.assertEqual(opening['retained_earnings'], D('500'))

    def test_debt_prefers_metric_over_facts(self):
        """borrowings 与 short_term_debt 常是同一笔钱的两个标签，直接相加会翻倍。"""
        facts = balanced_facts() + [fact('borrowings', '100'), fact('short_term_debt', '100')]
        opening = model3.opening_balance(facts, [metric('interest_bearing_debt', '100', unit='CNY')])
        self.assertEqual(opening['debt'], D('100'))
        self.assertEqual(opening['sources']['interest_bearing_debt']['source'], 'metric')

    def test_debt_fallback_does_not_double_count(self):
        """指标不可用时走降级路径：总括行优先，明细未显著超出时不重复计入。"""
        facts = balanced_facts() + [fact('borrowings', '100'), fact('short_term_debt', '100')]
        opening = model3.opening_balance(facts, [])
        self.assertEqual(opening['debt'], D('100'), 'borrowings 与 short_term_debt 被重复计入')

    def test_debt_fallback_uses_details_when_total_undercounts(self):
        """明细之和显著大于总括行时说明总括行漏了口径，应改用明细。"""
        facts = balanced_facts() + [fact('borrowings', '100'),
                                    fact('short_term_debt', '80'), fact('long_term_debt', '60')]
        opening = model3.opening_balance(facts, [])
        self.assertEqual(opening['debt'], D('140'))

    def test_metric_picked_by_period_and_scope(self):
        """同一指标有合并与母公司两条，必须按口径取，不能取到母公司数。"""
        facts = balanced_facts()
        metrics = [
            metric('interest_bearing_debt', '100', scope='consolidated', unit='CNY'),
            metric('interest_bearing_debt', '5', scope='parent', unit='CNY'),
        ]
        opening = model3.opening_balance(facts, metrics)
        self.assertEqual(opening['debt'], D('100'))

    def test_lease_liability_not_double_counted(self):
        """租赁负债属有息负债，已含在指标里，不能再进非流动经营负债。"""
        facts = balanced_facts() + [fact('lease_liabilities', '20')]
        opening = model3.opening_balance(facts, [])
        self.assertNotIn('lease_liabilities_noncurrent', opening['ncl'])
        self.assertEqual(opening['debt'], D('20'))

    def test_unbalanced_input_is_reported(self):
        """总括行与明细不一致时用倒挤保证配平，但必须留下说明。"""
        facts = balanced_facts()
        facts.append(fact('other_payables', '60'))   # 负债明细多出 20，总括行不变
        opening = model3.opening_balance(facts, [])
        self.assertTrue(opening['balanced'], '倒挤机制应保证期初表仍然配平')
        self.assertTrue(opening['residual_notes'], '倒挤必须有说明，不能静默')

    def test_missing_revenue_reports_unavailable(self):
        facts = [f for f in balanced_facts() if f['code'] != 'revenue']
        opening = model3.opening_balance(facts, [])
        self.assertIsNone(opening['revenue'])


class ThreeStatementTests(unittest.TestCase):
    def _run(self, **overrides):
        return model3.build(balanced_facts(), [], base_params(**overrides),
                            funding={'payout_ratio': D('0.5')})

    def test_all_tie_outs_pass(self):
        result = self._run()
        self.assertTrue(result['ok'])
        failed = [c['name'] for c in result['checks'] if not c['ok']]
        self.assertEqual(failed, [], f'勾稽未通过：{failed}')

    def test_balance_sheet_identity_exact(self):
        result = self._run()
        for row in result['statements']:
            self.assertEqual(row['balance_residual'], 0)

    def test_cash_flow_reconciles_with_balance_sheet(self):
        """资产负债表现金与现金流量表期末现金恒等 —— 配平机制正确的充要证据。"""
        result = self._run()
        for row in result['statements']:
            self.assertLessEqual(abs(row['cash_flow_gap']), model3.TOLERANCE)

    def test_retained_earnings_rolls_forward(self):
        result = self._run()
        prev = D('500')
        for row in result['statements']:
            income = row['income_statement']
            # 关联顺序与引擎一致：留存收益的增量是「归母净利 − 分红」这一笔。
            # 写成 (prev + parent) − dividend 会在第 28 位有效数字上产生差异 ——
            # 不是模型算错，是 Decimal 默认 28 位精度下的结合律差异。
            expected = prev + (income['parent_net_profit'] - income['dividend'])
            self.assertEqual(row['balance_sheet']['retained_earnings'], expected)
            prev = expected

    def test_revenue_follows_growth(self):
        result = self._run()
        revenues = [row['income_statement']['revenue'] for row in result['statements']]
        self.assertEqual(revenues[0], D('1100'))
        self.assertEqual(revenues[1], D('1210'))

    def test_paths_override_params(self):
        paths = {'revenue_growth': [D('0.20'), D('0.20'), D('0.20')]}
        result = model3.build(balanced_facts(), [], base_params(), paths=paths,
                              funding={'payout_ratio': D('0.5')})
        self.assertEqual(result['statements'][0]['income_statement']['revenue'], D('1200'))

    def test_missing_path_falls_back_to_params_not_zero(self):
        """路径缺省必须回落到校准参数 —— 回落成 0 会让折旧与资本开支凭空消失，
        三表照样配平，但模型与这家公司已无关系。"""
        result = self._run()
        flow = result['statements'][0]['cash_flow']
        self.assertGreater(flow['depreciation_amortization'], 0)
        self.assertGreater(flow['capex'], 0)

    def test_cash_plug_mode_for_net_cash_company(self):
        result = self._run()
        self.assertEqual(result['mode'], 'cash_plug')

    def test_debt_plug_mode_when_levered(self):
        facts = balanced_facts() + [fact('borrowings', '800')]
        result = model3.build(facts, [], base_params(), funding={'payout_ratio': D('0.5')})
        self.assertEqual(result['mode'], 'debt_plug')

    def test_interest_income_grows_with_cash(self):
        """三表联动的直接体现：现金积累带来利息收入上升。"""
        result = self._run()
        incomes = [row['income_statement']['interest_income'] for row in result['statements']]
        self.assertGreater(incomes[0], 0)
        self.assertGreater(incomes[-1], incomes[0])

    def test_impairment_only_hits_first_year(self):
        result = self._run(impairment_to_revenue=D('0.02'))
        self.assertGreater(result['statements'][0]['income_statement']['impairment'], 0)
        self.assertEqual(result['statements'][1]['income_statement']['impairment'], 0)

    def test_negative_cash_is_flagged_by_check(self):
        """check() 必须能报出现金为负 —— 那是配平机制失效的信号，不能静默。

        注意不能指望 cash_plug 模式自己造出负现金：该模式下现金是被解出的
        平衡项，revolver 会把缺口补到最低持有量，现金按构造就不会为负。
        所以这里直接构造一个负现金的行来验证检查逻辑本身有效。
        """
        built = self._run()
        rows = [dict(row) for row in built['statements']]
        rows[0] = dict(rows[0])
        rows[0]['balance_sheet'] = dict(rows[0]['balance_sheet'], cash=D('-1'))
        check = [c for c in model3.check({'rows': rows}) if c['name'] == 'cash_non_negative'][0]
        self.assertFalse(check['ok'])
        self.assertIn('1', check['detail'])

    def test_cash_never_negative_when_growth_outpaces_funding(self):
        """增长远超内生现金生成能力时，现金仍不得为负 —— 缺口必须由借款吸收。"""
        result = model3.build(balanced_facts(), [],
                              base_params(revenue_growth=D('1.0'), capex_to_revenue=D('3.0')),
                              funding={'payout_ratio': D('0.9'), 'mode': 'cash_plug',
                                       'minimum_cash': D('0')})
        for row in result['statements']:
            self.assertGreaterEqual(row['balance_sheet']['cash'], 0)
        self.assertGreater(result['statements'][0]['balance_sheet']['debt'],
                           result['opening_balance']['debt'] or D('0'),
                           '现金不足以支撑增长时应触发借款')

    def test_interest_is_charged_on_average_balance_by_default(self):
        """默认按平均余额计息 —— 这制造了"多借→利息升→净利降"的循环引用，
        必须迭代求解。按期初余额计息会让压力情景的利息被系统性低估。"""
        facts = balanced_facts() + [fact('borrowings', '30')]
        result = model3.build(facts, [], base_params(), funding={'payout_ratio': D('0.5')})
        self.assertEqual(result['mode'], 'cash_plug')
        self.assertEqual(result['interest_basis'], 'average_balance')
        first = result['statements'][0]
        debt_open = result['opening_balance']['debt']
        cash_open = result['opening_balance']['cash']
        # 用 1e-6 的绝对容差而不是精确相等：利息是在循环里用**上一轮**余额算的，
        # 停下来那一刻与最终余额相差不超过 CONVERGENCE_TOLERANCE（1e-9），
        # 因此不可能逐位相等。容差取 1e-6 已比该残差宽三个数量级，
        # 又比"计息口径搞错"（差异在元级）窄六个数量级。
        self.assertAlmostEqual(
            float(first['income_statement']['interest_expense']),
            float((debt_open + first['balance_sheet']['debt']) / D(2) * D('0.04')),
            delta=1e-6)
        self.assertAlmostEqual(
            float(first['income_statement']['interest_income']),
            float((cash_open + first['balance_sheet']['cash']) / D(2) * D('0.02')),
            delta=1e-6)

    def test_beginning_basis_is_available_for_reconciliation(self):
        """保留期初余额口径，便于与只按期初余额计息的常见简化模型对账。
        杠杆公司下借款逐年变化，两个口径必须给出不同的利息费用。"""
        facts = balanced_facts() + [fact('borrowings', '800')]
        params = base_params(revenue_growth=D('0.30'))
        average = model3.build(facts, [], params, funding={'payout_ratio': D('0.5')})
        beginning = model3.build(facts, [], params,
                                 funding={'payout_ratio': D('0.5'),
                                          'interest_basis': 'beginning'})
        self.assertEqual(average['mode'], 'debt_plug')
        self.assertEqual(average['interest_basis'], 'average_balance')
        self.assertEqual(beginning['interest_basis'], 'beginning_balance')
        first_beginning = beginning['statements'][0]
        self.assertEqual(first_beginning['income_statement']['interest_expense'],
                         beginning['opening_balance']['debt'] * D('0.04'))
        self.assertNotEqual(average['statements'][0]['income_statement']['interest_expense'],
                            first_beginning['income_statement']['interest_expense'])

    def test_noncurrent_asset_rollforward_ties_out(self):
        result = self._run()
        check = [c for c in result['checks']
                 if c['name'] == 'noncurrent_asset_rollforward'][0]
        self.assertTrue(check['ok'], check['detail'])

    def test_excess_depreciation_is_clamped_without_breaking_the_identity(self):
        """折旧率畸高时把**折旧**下调到净额恰为 0，而不是把净额硬钳到 0 ——
        后者会让资产滚存与现金流量表加回折旧不再恒等，缺口藏进现金差异里。"""
        result = self._run(da_to_revenue=D('2.0'))
        self.assertTrue(result['ok'])
        fa_check = [c for c in result['checks']
                    if c['name'] == 'noncurrent_asset_rollforward'][0]
        self.assertTrue(fa_check['ok'], fa_check['detail'])
        self.assertTrue(any('折旧摊销' in w for w in result['warnings']),
                        '下调折旧必须留下告警，不能静默')

    def test_revolver_raises_debt_when_cash_short(self):
        result = model3.build(balanced_facts(), [],
                              base_params(revenue_growth=D('0.8'), capex_to_revenue=D('1.5')),
                              funding={'payout_ratio': D('0.9'), 'minimum_cash': D('500')})
        self.assertTrue(result['converged'])
        debts = [row['balance_sheet']['debt'] for row in result['statements']]
        self.assertGreater(debts[0], 0, '现金不足时应触发借款')


class FcffCrossCheckTests(unittest.TestCase):
    def test_core_fcff_matches_driver_formula(self):
        """核心口径 FCFF 必须与「NOPAT + D&A − Capex − ΔNWC(应收/存货/应付)」一致。

        三表从利润表与资产负债表间接推，驱动法直接沿公式算，
        两条路径必须给出同一个数 —— 这是模型自洽的充要证据。
        """
        result = model3.build(balanced_facts(), [], base_params(),
                              funding={'payout_ratio': D('0.5')})
        for row, item in zip(result['statements'], result['fcff_cross_check']):
            ebit = row['income_statement']['ebit']
            nopat = ebit - max(ebit, D(0)) * D('0.25')
            expected = (nopat + item['depreciation_amortization'] + item['impairment']
                        - item['capex'] - item['core_delta_nwc'])
            self.assertEqual(item['core_fcff'], expected)

    def test_first_year_delta_nwc_uses_opening(self):
        """首年 ΔNWC 必须以期初营运资本为起点，不能用 NWC 绝对额冒充变动。"""
        result = model3.build(balanced_facts(), [], base_params(),
                              funding={'payout_ratio': D('0.5')})
        first = result['fcff_cross_check'][0]
        self.assertIsNotNone(result['opening_nwc'])
        self.assertNotEqual(first['delta_nwc'], first['nwc'], '首年 ΔNWC 被算成了 NWC 绝对额')

    def test_full_and_core_differ_by_non_core_working_capital(self):
        result = model3.build(balanced_facts(), [], base_params(),
                              funding={'payout_ratio': D('0.5')})
        for item in result['fcff_alignment']['core_vs_full']:
            self.assertEqual(item['gap'], item['full_fcff'] - item['core_fcff'])

    def test_alignment_with_driver_fcff(self):
        first = model3.build(balanced_facts(), [], base_params(),
                             funding={'payout_ratio': D('0.5')})
        driver = [item['core_fcff'] for item in first['fcff_cross_check']]
        result = model3.build(balanced_facts(), [], base_params(),
                              funding={'payout_ratio': D('0.5')}, driver_fcff=driver)
        comparison = result['fcff_alignment']['driver_comparison']
        self.assertTrue(comparison['aligned_after_first_year'])

    def test_alignment_reports_gap_when_driver_differs(self):
        driver = [D('1'), D('1'), D('1')]
        result = model3.build(balanced_facts(), [], base_params(),
                              funding={'payout_ratio': D('0.5')}, driver_fcff=driver)
        comparison = result['fcff_alignment']['driver_comparison']
        self.assertFalse(comparison['aligned_after_first_year'])


class SerialisationTests(unittest.TestCase):
    def test_output_is_json_serialisable(self):
        result = model3.build(balanced_facts(), [], base_params(),
                              funding={'payout_ratio': D('0.5')})
        json.dumps(result, ensure_ascii=False, default=str)


if __name__ == '__main__':
    unittest.main()
