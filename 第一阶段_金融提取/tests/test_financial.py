import pytest
from finance_extract.numbers import quantity, infer_unit
from finance_extract.tables import read_tables
from finance_extract.extract import extract_middle

@pytest.mark.parametrize('raw,unit,value,base,status', [
    ('1,234.50', '万元', '1234.50', '12345000.00', 'parsed'),
    ('（12.30）', '元', '-12.30', '-12.30', 'parsed'),
    ('０', '股', '0', '0', 'parsed'),
    ('—', '元', None, None, 'missing'),
    ('1,23', '元', None, None, 'invalid'),
    ('1O0', '元', None, None, 'invalid'),
    ('2.84%', None, '2.84', '2.84', 'parsed'),
    ('10', None, '10', None, 'unit_unknown'),
    ('3.2亿元', '万元', '3.2', '320000000.0', 'parsed'),
])
def test_quantities(raw, unit, value, base, status):
    q = quantity(raw, unit)
    assert (q['value'], q['base_value'], q['status']) == (value, base, status)

def test_nearest_unit():
    assert infer_unit('单位：万元。前表结束。单位：元 币种：人民币') == '元'

def block(content, index=0, kind='text'):
    return {'type': kind, 'index': index, 'content': [{'type': 'text', 'content': content}]}

def middle(*contents):
    return {'pages': [{'page_idx': 0, 'blocks': list(contents)}]}

def test_span_headers_do_not_swap_before_after():
    html = '<table><tr><td rowspan="2">股东名称</td><td colspan="2">变动前</td><td colspan="2">变动后</td></tr><tr><td>持股数量（万股）</td><td>比例（%）</td><td>持股数量（万股）</td><td>比例（%）</td></tr><tr><td>甲公司</td><td>100</td><td>10%</td><td>80</td><td>8%</td></tr></table>'
    result = extract_middle(middle(block(html, kind='table')), 'test')
    f = result['events'][0]['fields']
    assert f['before_shares']['base_value'] == '1000000'
    assert f['after_shares']['base_value'] == '800000'
    assert f['before_ratio']['value'] == '10'

def test_repair_label_keeps_source_rows():
    html = '<table><tr><td>项目</td><td>2024年</td></tr><tr><td>营业收入</td><td>100</td></tr><tr><td>经营活动产生的现金</td><td>30</td></tr><tr><td>流量净额</td><td></td></tr></table>'
    result = extract_middle(middle(block('单位：万元'), block(html, 1, 'table')), 'test')
    f = [f for f in result['fields'] if f['name'] == 'operating_cash_flow'][0]
    assert f['base_value'] == '300000'
    assert f['evidence']['source_rows'] == [3, 4]
    assert 'reconstructed_split_label' in f['review_reasons']
    assert result['tables'][0]['rows'][2][0] == '经营活动产生的现金'

def test_missing_not_zero_and_growth_not_level():
    html = '<table><tr><td>项目</td><td>2024年</td><td>同比增长（%）</td></tr><tr><td>营业收入</td><td>—</td><td>5.3</td></tr></table>'
    r = extract_middle(middle(block('单位：元'), block(html, 1, 'table')), 'test')
    assert len(r['fields']) == 1 and r['fields'][0]['value'] is None

def test_conflicting_numeric_rows_not_joined():
    html = '<table><tr><td>项目</td><td>2024年</td></tr><tr><td>经营活动产生的现金</td><td>30</td></tr><tr><td>流量净额</td><td>40</td></tr></table>'
    r = extract_middle(middle(block(html, kind='table')), 'test')
    assert not r['fields']

def test_announcement_does_not_use_date_as_amount():
    r = extract_middle(middle(block('2025年3月24日，中标金额：39,447,774.00元，尚未签订正式合同。')), 'test')
    assert r['events'][0]['fields']['award_amount']['base_value'] == '39447774.00'

def test_output_html_is_not_interpreted_as_webpage():
    html = '<table><tr><td><script>alert(1)</script>股东名称</td><td>本次质押股数</td></tr><tr><td>甲公司</td><td>100</td></tr></table>'
    assert read_tables(html)[0][1][0]['text'] == '甲公司'

def test_equity_questionnaire_not_a_shareholding_event():
    html = '<table><tr><td>变动前比例</td><td>变动后比例</td></tr><tr><td>是否披露资金来源</td><td>是</td></tr><tr><td>未来12个月是否增持</td><td>否</td></tr></table>'
    r = extract_middle(middle(block(html, kind='table')), 'test')
    assert not r['events']

def test_equity_header_date_preserves_change_direction():
    from finance_extract.extract import period
    assert period('变动前 2024年11月15日') == 'before_change'
    assert period('变动后 2025年9月19日') == 'after_change'
