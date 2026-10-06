"""Adversarial finance checks with independent numerical expectations."""
from pathlib import Path
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT.parent/'第一阶段_金融提取')]
from finance_extract.native import parse_native
from server import research_engine as e, presentation as p,report_documents as wd
from test_research_engine import record,assumptions,facts

def bridge_rows():
    return [record('revenue','2023',1000000),record('revenue_cost','2023',600000),record('revenue','2024',1200000),record('revenue_cost','2024',840000),
            record('accounts_receivable','2023-year-end',100000),record('accounts_receivable','2024-year-end',200000),
            record('inventory','2023-year-end',60000),record('inventory','2024-year-end',120000)]

def test_gross_profit_bridge_independent_numbers():
    r=e.analysis(bridge_rows());b=r['profit_bridges'][0]
    assert b['gross_profit_change']=='-40000.000000' and b['revenue_effect']=='80000.000000' and b['margin_effect']=='-120000.000000'
    assert b['margin_change_pp']=='-10.000000' and b['reconciled'] and len(b['sources'])==4
    assert '业务因果' in b['interpretation']

def test_turnover_uses_average_opening_closing_and_calendar_days():
    r=e.analysis(bridge_rows());rows={x['metric']:x for x in r['turnover'] if x['period']=='2024'}
    assert rows['receivable_days']['value']=='45.750000' and rows['receivable_days']['days_in_period']==366
    assert float(rows['inventory_days']['value'])==pytest.approx((60000+120000)/2/840000*366,abs=1e-6)
    assert len(r['investigation_questions'])==2

def test_missing_opening_not_replaced_by_closing():
    r=e.analysis([record('revenue','2024',1000),record('accounts_receivable','2024-year-end',200)])
    x=next(x for x in r['turnover'] if x['metric']=='receivable_days')
    assert x['status']=='insufficient_or_invalid_input' and x['value'] is None

@pytest.mark.parametrize('change',[{'currency':'USD'},{'scope':'母公司'},{'basis':'重述'},{'issuer':'B'}])
def test_bridge_cannot_mix_currency_scope_basis_issuer(change):
    rows=bridge_rows();rows[1].update(change)
    assert not e.analysis(rows)['profit_bridges']

def test_conflicting_year_end_aliases_do_not_become_average():
    r=e.analysis(bridge_rows()+[record('accounts_receivable','2024',999999)])
    x=next(x for x in r['turnover'] if x['metric']=='receivable_days' and x['period']=='2024')
    assert x['value'] is None

def test_cash_ratio_uses_consolidated_profit_when_available():
    rows=[record('net_profit','2024',100),record('net_profit_parent','2024',80),record('operating_cash_flow','2024',90)]
    ratio=next(x for x in e.analysis(rows)['ratios'] if x['metric']=='cash_profit_ratio')
    assert ratio['value']=='0.900000' and ratio['denominator_metric']=='net_profit'
    assert '净利润的0.90' in ' '.join(p.analysis_paragraphs(e.analysis(rows)))

def test_chat_key_table_cannot_mix_company():
    rows=[record('revenue','2023',100),record('revenue','2024',120),record('revenue','2023',1,issuer='B'),record('revenue','2024',1000,issuer='B'),record('net_profit_parent','2023',10),record('net_profit_parent','2024',12)]
    text=p.chat(e.analysis(rows),{'report.docx':'/word','result.json':'/data'})
    assert '99,900.00' not in text and '20.00%' in text

@pytest.mark.parametrize('bad',['2024-02-30','2023-02-29','2024-13-01'])
def test_impossible_dates_never_participate(bad):
    assert e.period_key(bad) is None and e.analysis([record('cash',bad,100)])['usable_metric_groups']==0

def test_utf16_native_input_preserves_chinese_and_value(tmp_path):
    f=tmp_path/'utf16.csv';f.write_text('issuer,metric,period,raw,unit,currency,scope,basis\n示例,revenue,2024,100,万元,CNY,合并,原报\n',encoding='utf-16')
    d=parse_native(f,f.name,'h');assert d['fields'][0]['issuer']=='示例' and d['fields'][0]['base_value']=='1000000'

@pytest.mark.parametrize('text',['metric,period,raw, raw ,unit\nrevenue,2024,100,999,元\n','metric,period,raw,unit\nrevenue,2024,100,元,999\n'])
def test_duplicate_headers_or_extra_columns_are_rejected(tmp_path,text):
    f=tmp_path/'bad.csv';f.write_text(text,encoding='utf-8')
    with pytest.raises(ValueError,match='重复|列数'):parse_native(f,f.name,'h')

def test_short_csv_row_preserves_missing_not_none_literal(tmp_path):
    f=tmp_path/'short.csv';f.write_text('issuer,metric,period,raw,unit,currency,scope\nA,revenue,2024\n',encoding='utf-8')
    d=parse_native(f,f.name,'h');assert d['fields'][0]['status']=='missing' and d['fields'][0]['raw']==''

def test_losing_company_keeps_pb_when_dcf_invalid(tmp_path):
    a=assumptions();a['ebit_margin']=-.05
    rows=[record('revenue','2024',1200),record('net_profit_parent','2024',-60),record('equity_parent','2024-year-end',1000)]
    r=e.valuation(rows,a)
    assert r['dcf']['status']=='nonpositive_terminal_cash_flow' and len(r['dcf']['forecast'])==5 and 'price_per_share' not in r['dcf']
    assert next(x for x in r['relative'] if x['method']=='PB')['status']=='ok'
    assert next(x for x in r['relative'] if x['method']=='PE')['status']!='ok'
    wd.write_research(r,tmp_path/'losing.docx')

def test_bad_cash_flow_sensitivity_point_preserves_baseline(tmp_path):
    a=assumptions();a.update(ebit_margin=.03,tax_rate=0,da_ratio=0,capex_ratio=0,nwc_ratio=1,wacc_grid=[.08,.12],terminal_growth_grid=[.01,.02,.05])
    r=e.valuation(facts(),a);d=r['dcf']
    assert d['status']=='ok' and d['sensitivity_valid_points']==4 and d['sensitivity_excluded_points']==2
    assert len(d['sensitivity'])==6 and all(x['price_per_share'] is None for x in d['sensitivity'] if x['status']!='ok')
    valid=[float(x['price_per_share']) for x in d['sensitivity'] if x['status']=='ok']
    assert float(d['sensitivity_price_low'])==min(valid) and float(d['sensitivity_price_high'])==max(valid)
    wd.write_research(r,tmp_path/'partial.docx')

def test_all_sensitivity_points_invalid_does_not_erase_base(tmp_path):
    a=assumptions();a.update(ebit_margin=.03,tax_rate=0,da_ratio=0,capex_ratio=0,nwc_ratio=1,terminal_growth=0,wacc_grid=[.08,.12],terminal_growth_grid=[.04,.05],shares_outstanding=None,net_debt=None)
    r=e.valuation(facts(),a,allow_partial=True);assert r['dcf']['status']=='ok' and r['dcf']['sensitivity_valid_points']==0
    assert r['dcf']['sensitivity_enterprise_low'] is None and '未形成有效区间' in p.chat(r,{'report.docx':'/word','result.json':'/data'})
    wd.write_research(r,tmp_path/'none.docx')

def test_explicit_bad_wacc_remains_error():
    a=assumptions();a['wacc']=.01
    with pytest.raises(ValueError):e.valuation(facts(),a)

def test_conflicting_explicit_and_derived_quarter_never_compared():
    rows=[record('revenue','2024-Q1',10),record('revenue','2024-H1',25),record('revenue','2024-Q2',99),record('revenue','2024-9M',45),record('revenue','2023-Q2',10)]
    r=e.analysis(rows)
    assert any(i['code']=='quarter_reconciliation_conflict' for i in r['input_issues'])
    assert not any(c['period']=='2024-Q2' or c['comparison_period']=='2024-Q2' for c in r['changes'])

def test_generic_table_duplicate_labels_preserved_not_dict_overwritten(tmp_path):
    f=tmp_path/'generic.csv';f.write_text('项目,2024,2024\n营业收入,100,200\n',encoding='utf-8')
    result=parse_native(f,f.name,'h')
    assert '100' in result['blocks'][0]['text'] and '200' in result['blocks'][0]['text']

@pytest.mark.parametrize('text',['请分析这份财报，解释毛利变化','帮我看看年报中的业绩','我不要估值，我要分析财报'])
def test_natural_report_analysis_enters_workflow(text):
    from server.agent import financial_intent
    assert financial_intent(text)=='analysis'
