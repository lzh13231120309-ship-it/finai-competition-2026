"""财报分析缺口补齐（环比 / 口径变化 / 业绩原因定位）的确定性测试。

这三项的共同纪律：缺数据不猜、基数非正不报百分比、成因不由程序判定。
"""
from decimal import Decimal

from server import financial_gaps as fg


def fact(code, value, period, ptype='ytd', **kw):
    d = {'fact_id': f'{code}-{period}-{ptype}', 'code': code, 'raw_value': str(value),
         'value': str(value), 'period': period, 'period_type': ptype, 'scope': 'consolidated',
         'currency': 'CNY', 'company': '甲', 'unit': '元', 'scale': 1, 'status': 'verified',
         'source': {'document_id': 'docA'}, 'evidence_ids': [f'e-{code}-{period}']}
    d.update(kw)
    return d


# ------------------------------------------------------------------ 环比

def test_qoq_from_disclosed_single_quarters():
    facts = [fact('revenue', 100, '2025-03-31', 'quarter'),
             fact('revenue', 130, '2025-06-30', 'quarter')]
    metrics, reviews = fg.qoq_metrics(facts)
    assert not reviews
    qoq = {m['period']: m for m in metrics}
    assert qoq['2025Q2']['value'] == str(Decimal('0.3'))
    assert qoq['2025Q2']['unit'] == 'ratio'
    assert qoq['2025Q2']['derived'] is True
    assert qoq['2025Q2']['formula'].startswith('(')


def test_qoq_derives_single_quarter_from_ytd():
    # Q2 单季 = H1 累计 300 − Q1 累计 150 = 150；Q1 单季 = 150 → 环比 0
    facts = [fact('net_profit', 150, '2025-03-31'),
             fact('net_profit', 300, '2025-06-30')]
    metrics, _ = fg.qoq_metrics(facts)
    qoq = {m['period']: m for m in metrics}
    assert qoq['2025Q2']['value'] == str(Decimal('0'))
    assert qoq['2025Q2']['operands']['current_method'] == 'ytd_difference'
    assert qoq['2025Q2']['operands']['current_single_quarter'] == '150'


def test_qoq_skips_when_prior_ytd_missing():
    # 只有 9M 累计、没有 H1 累计 → 无法推 Q3 单季 → 不产出，不猜
    facts = [fact('revenue', 480, '2025-09-30')]
    metrics, reviews = fg.qoq_metrics(facts)
    assert metrics == [] and reviews == []


def test_qoq_nonpositive_base_reports_amount_only():
    facts = [fact('parent_net_profit', -20, '2025-12-31', 'quarter'),
             fact('parent_net_profit', 10, '2026-03-31', 'quarter')]
    metrics, reviews = fg.qoq_metrics(facts)
    codes = {m['code'] for m in metrics}
    assert 'parent_net_profit_qoq' not in codes          # 不给百分比
    change = next(m for m in metrics if m['code'] == 'parent_net_profit_qoq_change')
    assert change['value'] == '30' and change['unit'] == 'amount'
    assert change['period'] == '2026Q1'
    assert reviews and reviews[0]['code'] == 'qoq_base_nonpositive'
    assert '非正数' in reviews[0]['message']


def test_qoq_ignores_instant_and_non_adjacent():
    facts = [fact('total_assets', 500, '2025-03-31', 'instant'),
             fact('total_assets', 600, '2025-06-30', 'instant'),
             # 相隔两个季度，不算相邻
             fact('revenue', 100, '2025-03-31', 'quarter'),
             fact('revenue', 90, '2025-09-30', 'quarter')]
    metrics, reviews = fg.qoq_metrics(facts)
    assert metrics == [] and reviews == []


def test_qoq_cross_year_q4_to_q1():
    facts = [fact('revenue', 100, '2025-12-31', 'quarter'),
             fact('revenue', 120, '2026-03-31', 'quarter')]
    metrics, _ = fg.qoq_metrics(facts)
    assert len(metrics) == 1
    assert metrics[0]['period'] == '2026Q1'
    assert '跨年' in metrics[0]['formula']


def test_qoq_keeps_dependency_and_evidence():
    facts = [fact('revenue', 100, '2025-03-31', 'quarter'),
             fact('revenue', 130, '2025-06-30', 'quarter')]
    metrics, _ = fg.qoq_metrics(facts)
    m = metrics[0]
    assert set(m['dependency_fact_ids']) == {'revenue-2025-03-31-quarter', 'revenue-2025-06-30-quarter'}
    assert m['evidence_ids']


# ------------------------------------------------------------------ 口径变化

def test_restatement_change_reports_delta_but_not_cause():
    facts = [fact('revenue', 1000, '2024-12-31', source={'document_id': 'doc2024'}),
             fact('revenue', 1050, '2024-12-31', restated=True, source={'document_id': 'doc2025'})]
    changes = fg.restatement_changes(facts)
    assert len(changes) == 1
    c = changes[0]
    assert c['delta'] == '50'
    assert c['delta_ratio'] == str(Decimal('0.05'))
    assert c['cause'] is None                    # 程序不判成因
    assert '成因须人工读附注确认' in c['note']
    assert [v['restated'] for v in c['values']] == [False, True]
    assert c['values'][0]['documents'] == ['doc2024']


def test_restatement_ignores_single_value_and_scope_split():
    same = [fact('revenue', 100, '2024-12-31'), fact('revenue', 100, '2024-12-31',
                                                    source={'document_id': 'doc2'})]
    assert fg.restatement_changes(same) == []    # 值相同不算变化
    # 合并 vs 母公司是两个口径，不是"同一格两个值"
    scoped = [fact('revenue', 100, '2024-12-31', scope='consolidated'),
              fact('revenue', 60, '2024-12-31', scope='parent')]
    assert fg.restatement_changes(scoped) == []


def test_restatement_zero_base_has_no_ratio():
    facts = [fact('other_income', 0, '2024-12-31'), fact('other_income', 10, '2024-12-31')]
    changes = fg.restatement_changes(facts)
    assert changes[0]['delta_ratio'] is None


def test_restatement_requires_known_scope_and_same_unit():
    # 口径未知 → 不当作口径变化（可能只是合并/母公司混用），进 suspects
    unknown = [fact('cash', 100, '2024-12-31', scope='unknown'),
               fact('cash', 200, '2024-12-31', scope='unknown')]
    assert fg.restatement_changes(unknown) == []
    suspects = fg.restatement_suspects(unknown)
    assert len(suspects) == 1
    assert suspects[0]['reason'] == 'scope_unknown'
    assert '不得' in suspects[0]['note']
    # 单位不同 → 不可比（0.28 是比率，110 亿是金额），连 suspects 都不该出现
    mixed = [fact('cash', 100, '2024-12-31', unit='元'),
             fact('cash', 0.28, '2024-12-31', unit='%')]
    assert fg.restatement_changes(mixed) == []
    assert fg.restatement_suspects(mixed) == []
    # 量级（scale）不同 → 万元 vs 元，属于单位换算问题，不是重述
    scaled = [fact('cash', 100, '2024-12-31', scale=1),
              fact('cash', 10000, '2024-12-31', scale=10000)]
    assert fg.restatement_changes(scaled) == []


def test_restatement_flags_rebuilt_table():
    facts = [fact('financial_expenses', 190, '2025-12-31',
                  source={'document_id': 'docA', 'rebuilt_table': True}),
             fact('financial_expenses', 9713660.99, '2025-12-31',
                  source={'document_id': 'docA', 'rebuilt_table': True})]
    c = fg.restatement_changes(facts)[0]
    assert c['rebuilt_table'] is True
    assert '重建' in c['note'] and '行列错位' in c['note']


def test_gap_analysis_reports_suspects_separately():
    facts = [fact('cash', 100, '2024-12-31', scope='unknown'),
             fact('cash', 200, '2024-12-31', scope='unknown')]
    metrics, _reviews, changes, _reasons, notes = fg.gap_analysis(facts)
    assert changes == []
    joined = ' '.join(notes)
    assert 'restatement_suspects' in joined and '误报' in joined


# ------------------------------------------------------------------ 年报分季度表

QUARTER_DOC = {
    'document_id': 'docFY', 'filename': '年报.pdf',
    'tables': [{'table_id': 'docFY:p8:t1', 'page': 8, 'rows': [
        ['项目', '第一季度', '第二季度', '第三季度', '第四季度'],
        ['营业收入', '100', '120', '90', '70'],
        ['归属于上市公司股东的净利润', '10', '15', '11', '8'],
        ['基本每股收益（元/股）', '0.1', '0.15', '0.11', '0.08'],
    ]}],
}


def _annual(code, value, document_id='docFY'):
    return {'fact_id': f'a-{code}', 'code': code, 'value': str(value), 'period': '2025-12-31',
            'period_type': 'annual', 'scope': 'consolidated', 'currency': 'CNY', 'company': '甲',
            'unit': '元', 'scale': 1, 'status': 'verified', 'source_tier': 'summary',
            'source': {'document_id': document_id}, 'evidence_ids': ['ev']}


def test_quarter_facts_reconcile_and_bind_distinct_periods():
    facts = [_annual('revenue', 380), _annual('parent_net_profit', 44)]
    out, reviews, notes = fg.quarter_facts_from_document(QUARTER_DOC, facts)
    rev = [f for f in out if f['code'] == 'revenue']
    assert [f['period'] for f in rev] == ['2025-03-31', '2025-06-30', '2025-09-30', '2025-12-31']
    assert [f['value'] for f in rev] == ['100', '120', '90', '70']
    assert all(f['period_type'] == 'quarter' and f['status'] == 'verified' for f in rev)
    assert all(f['cross_check']['result'] == 'consistent_within_rounding' for f in rev)
    assert all(f['evidence_ids'] for f in rev)
    assert not reviews
    # 每股收益不在映射内（季度 EPS 之和与全年对不上），不应产出
    assert not [f for f in out if f['code'] == 'basic_eps']
    assert not notes


def test_quarter_facts_mismatch_is_recorded_but_not_used():
    facts = [_annual('revenue', 999)]           # 四季之和 380 ≠ 999
    out, reviews, _notes = fg.quarter_facts_from_document(QUARTER_DOC, facts)
    rev = [f for f in out if f['code'] == 'revenue']
    assert all(f['status'] == 'needs_review' for f in rev)
    assert reviews and reviews[0]['code'] == 'quarter_sum_mismatch'
    # needs_review 不参与环比
    metrics, _ = fg.qoq_metrics(out)
    assert metrics == []


def test_quarter_facts_skipped_without_annual_reference():
    out, _reviews, notes = fg.quarter_facts_from_document(QUARTER_DOC, [])
    assert out == []
    assert any('无法确定分季度表的年份' in n for n in notes)


def test_quarter_facts_from_documents_and_qoq_end_to_end():
    facts = [_annual('revenue', 380)]
    out, _reviews, _notes = fg.quarter_facts_from_documents([QUARTER_DOC], facts)
    metrics, _ = fg.qoq_metrics(facts + out)
    qoq = {m['period']: m for m in metrics if m['code'] == 'revenue_qoq'}
    assert qoq['2025Q2']['value'] == str(Decimal('0.2'))     # 120/100 − 1
    assert qoq['2025Q3']['value'] == str(Decimal('-0.25'))   # 90/120 − 1
    assert qoq['2025Q4']['value'] == str(Decimal(str(Decimal('70') / Decimal('90') - 1)))
    assert all(m['derived'] is True for m in metrics)


def test_quarter_facts_accept_q_prefixed_headers_and_skip_blanks():
    doc = {'document_id': 'd2', 'filename': 'x.pdf', 'tables': [
        {'table_id': 'd2:p1:t1', 'page': 1, 'rows': [
            ['项目', 'Q1', 'Q2', 'Q3', 'Q4'],
            ['营业收入', '10', '--', '30', '40'],
        ]}]}
    out, _reviews, notes = fg.quarter_facts_from_document(doc, [_annual('revenue', 80, 'd2')])
    assert out == []                                  # 缺一个季度 → 不足四季度，跳过
    assert any('不足四季度' in n for n in notes)


# ------------------------------------------------------------------ 业绩原因定位

DOC = {'filename': '年报.pdf', 'pages': [
    {'page': 12, 'text': '', 'blocks': [
        {'text': '一、经营情况讨论与分析', 'evidence_id': 'E1'},
        {'text': '报告期内营业收入同比下降 3.5%，主要原因是下游需求偏弱。', 'evidence_id': 'E2'}]},
    {'page': 13, 'text': '', 'blocks': [{'text': '公司治理结构', 'evidence_id': 'E3'}]},
]}


def test_reason_candidates_are_located_with_page_and_evidence():
    out = fg.reason_candidates(DOC)
    assert out
    pages = {c['page'] for c in out}
    assert pages == {12}
    assert all(c['evidence_id'] for c in out)
    assert any('主要原因是' == c['keyword'] for c in out)


def test_reason_candidates_do_not_summarize():
    out = fg.reason_candidates(DOC)
    # 只返回原文摘录，不产出任何结论性字段
    for c in out:
        assert set(c) == {'page', 'evidence_id', 'keyword', 'quote', 'source_document'}
    joined = ' '.join(c['quote'] for c in out)
    assert '需求偏弱' in joined          # 原文在
    assert '因此' not in joined and '结论' not in joined


def test_reason_candidates_handles_page_text_without_blocks():
    doc = {'filename': 'a.pdf', 'pages': [{'page': 1, 'text': '业绩变动原因：销量下降'}]}
    out = fg.reason_candidates(doc)
    assert len(out) == 1 and out[0]['page'] == 1


def test_reason_candidates_empty_for_non_dict():
    assert fg.reason_candidates(None) == []
    assert fg.reason_candidates({'pages': []}) == []


# ------------------------------------------------------------------ 统一入口

def test_gap_analysis_notes_explain_absence():
    facts = [fact('revenue', 100, '2025-03-31', 'quarter')]
    metrics, reviews, changes, reasons, notes = fg.gap_analysis(facts)
    assert metrics == [] and changes == []
    joined = ' '.join(notes)
    assert '缺任一期就不推算' in joined
    assert '没有重述/口径变化的证据' in joined


def test_gap_analysis_flags_unparsed_reason_section():
    facts = [fact('revenue', 100, '2025-03-31', 'quarter'),
             fact('revenue', 130, '2025-06-30', 'quarter')]
    _, _, _, reasons, notes = fg.gap_analysis(facts, {'filename': 'x.pdf', 'pages': []})
    assert reasons == []
    assert any('不得写成"公司未披露"' in n for n in notes)
