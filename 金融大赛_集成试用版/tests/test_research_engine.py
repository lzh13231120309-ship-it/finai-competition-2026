from pathlib import Path
import sys,json
import pytest
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT.parent/'第一阶段_金融提取')]
from server import research_engine as e
from finance_extract.native import parse_native

def record(metric,period,value,**kwargs):
    return {'issuer':'A','scope':'合并','currency':'CNY','basis':'原报','name':metric,'period':period,'status':'parsed',
        'base_value':str(value),'base_unit':'元','source_ref':{'field_id':f'{metric}/{period}'},'review_reasons':[],**kwargs}
def assumptions():
    return {'schema':'finai.valuation.assumptions.v1','issuer':'A','industry':'industrial','currency':'CNY',
        'valuation_date':'2025-01-01','base_period':'2024','basis':'用户明确假设','net_debt_basis':'教学净债务调整假设',
        'growth_rates':[.05]*5,'ebit_margin':.15,'tax_rate':.25,'da_ratio':.02,'capex_ratio':.04,'nwc_ratio':.15,
        'wacc':.1,'terminal_growth':.02,'shares_outstanding':100,'net_debt':10,
        'wacc_grid':[.08,.1,.12],'terminal_growth_grid':[.01,.02,.03],
        'comparables':[{'name':'B','pe':12,'pb':1.2,'as_of':'2025-01-01','basis':'同比同行业'},
                       {'name':'C','pe':18,'pb':1.8,'as_of':'2025-01-01','basis':'同比同行业'}]}
def facts():return [record('revenue','2024',1200),record('net_profit_parent','2024',120),record('equity_parent','2024-year-end',1000)]

def test_growth_and_zero_negative():
    r=e.analysis([record('revenue','2023',100),record('revenue','2024',120),record('net_profit_parent','2023',-10),record('net_profit_parent','2024',20),record('operating_cash_flow','2023',0),record('operating_cash_flow','2024',1)])
    assert next(c for c in r['changes'] if c['metric']=='revenue')['percent_change']=='20.000000'
    assert next(c for c in r['changes'] if c['metric']=='net_profit_parent')['sign_transition']=='loss_to_profit'
    assert next(c for c in r['changes'] if c['metric']=='operating_cash_flow')['percent_change'] is None

def test_quarters_require_cumulative_difference():
    r=e.analysis([record('revenue','2024-Q1',10),record('revenue','2024-H1',25),record('revenue','2024-9M',45),record('revenue','2024',70)])
    q={c['period']:c['value'] for c in r['derived_quarters']};assert q=={'2024-Q2':'15.000000','2024-Q3':'20.000000','2024-Q4':'25.000000'}
    assert next(c for c in r['changes'] if c['mode']=='qoq' and c['period']=='2024-Q3')['percent_change']=='33.333333'

def test_balance_stocks_not_derived_as_flows():
    r=e.analysis([record('assets_total','2024-Q1',10),record('assets_total','2024-H1',25)])
    assert not r['derived_quarters']

@pytest.mark.parametrize('change',[{'scope':'母公司'},{'currency':'USD'},{'basis':'调整后'},{'issuer':'B'},{'period':'2024-H1'}])
def test_no_cross_scope_growth(change):
    previous=record('revenue','2023',100);current=record('revenue','2024',120,**{k:v for k,v in change.items() if k!='period'})
    if 'period' in change:current['period']=change['period']
    assert not e.analysis([previous,current])['changes']

def test_conflict_missing_and_model_candidates_excluded():
    items=[record('revenue','2024',120),record('revenue','2024',130),record('cash','2024',2,origin='model_semantic_candidate'),record('assets_total','本期',100)]
    r=e.analysis(items);assert r['usable_metric_groups']==0 and len(r['input_issues'])==3

def test_equity_parent_not_total_equity_and_signals():
    rows=[record('assets_total','2024',200),record('liabilities_total','2024',80),record('equity_parent','2024',100),record('net_profit_parent','2024',20),record('operating_cash_flow','2024',-2),record('net_profit_parent_adjusted','2024',5)]
    r=e.analysis(rows);assert not r['reconciliations'];assert {x['code'] for x in r['signals']}=={'profit_cash_divergence','non_recurring_profit'}
    r=e.analysis(rows+[record('equity_total','2024',120)]);assert r['reconciliations'][0]['passed']

def test_dcf_independent_formula_and_bridge():
    a=assumptions();r=e.valuation(facts(),a);d=r['dcf']
    # Independent float calculation checks all FCFF years and a differently constructed terminal year.
    rev=1200;nwc=180;pv=0
    for t in range(1,6):
        rev*=1.05;delta=rev*.15-nwc;nwc=rev*.15;fcf=rev*.15*.75+rev*.02-rev*.04-delta
        pv+=fcf/1.1**t;assert float(d['forecast'][t-1]['fcff'])==pytest.approx(fcf,abs=1e-5)
    stable=rev*1.02*(.15*.75+.02-.04)-rev*.02*.15
    ev=pv+stable/(.1-.02)/1.1**5
    assert float(d['enterprise_value'])==pytest.approx(ev,abs=1e-5)
    assert float(d['price_per_share'])==pytest.approx((ev-10)/100,abs=1e-5)
    assert len(d['sensitivity'])==9
    assert r['relative'][0]['median_multiple']=='15.000000'

@pytest.mark.parametrize('bad',[{'wacc':.02,'terminal_growth':.03},{'shares_outstanding':-1},{'tax_rate':25},{'growth_rates':[.1]*11},{'terminal_growth_grid':[.02,.1]},{'net_debt':None},{'basis':''}])
def test_reject_invalid_assumptions(bad):
    a=assumptions();a.update(bad)
    with pytest.raises(ValueError):e.valuation(facts(),a)

def test_bank_pb_only_and_outlier_exclusion():
    a=assumptions();a['industry']='bank';r=e.valuation(facts(),a);assert r['dcf']['status']=='unsupported_industry'
    a=assumptions();a['comparables'][1]['pe']=1000;r=e.valuation(facts(),a);assert r['relative'][0]['status']=='nonpositive_base_or_insufficient_peers'

def test_missing_base_not_annualize_quarter():
    with pytest.raises(ValueError):e.valuation([record('revenue','2024-Q1',100)],assumptions())

def test_native_preserves_missing_bad_numbers_and_units(tmp_path):
    p=tmp_path/'input.csv';p.write_text('issuer,metric,period,raw,unit,currency,scope,basis\nA,revenue,2024,1200,万元,CNY,合并,原报\nA,operating_cash_flow,2024,—,万元,CNY,合并,原报\nA,cash,2024,1O0,元,CNY,合并,原报\n',encoding='utf-8')
    r=parse_native(p,p.name,'h');assert r['fields'][0]['base_value']=='12000000'
    assert r['fields'][1]['status']=='missing' and r['fields'][2]['status']=='invalid'
    assert all(f['raw'] in r['blocks'][i]['text'] for i,f in enumerate(r['fields']))

def test_detection_of_module_intent():
    from server.agent import financial_intent
    assert financial_intent('请提取公告，不要进行估值或财报分析。')=='extraction'
    assert financial_intent('请做财务报告分析和同比')=='analysis'
    assert financial_intent('请做自动化估值')=='valuation'

def test_quarter_report_separates_current_quarter_from_year_to_date():
    from finance_extract.extract import extract_middle
    middle={'pages':[{'page_idx':0,'blocks':[{'type':'text','content':'测试股份有限公司2025年第三季度报告'},
        {'type':'text','content':'单位：元 人民币'},
        {'type':'table','content':'<table><tr><td>项目</td><td>本报告期</td><td>上年同期</td><td>本报告期比上年同期增减</td><td>年初至报告期末</td><td>上年同期</td></tr><tr><td>营业收入</td><td>650</td><td>439</td><td>48%</td><td>1658</td><td>1300</td></tr></table>'}]}]}
    result=extract_middle(middle,'example.pdf','abc')
    assert [(f['period'],f['raw']) for f in result['fields']]==[('2025-Q3','650'),('2024-Q3','439'),('2025-9M','1658'),('2024-9M','1300')]

def test_currency_unknown_cannot_merge_files_but_can_compare_same_source():
    one=record('revenue','2023',100,currency=None,source_ref={'sha256':'A'})
    two=record('revenue','2024',120,currency=None,source_ref={'sha256':'B'})
    assert not e.analysis([one,two])['changes']
    two['source_ref']['sha256']='A';assert e.analysis([one,two])['changes'][0]['percent_change']=='20.000000'

def test_rounding_tolerance_cannot_hide_large_balance_difference():
    rows=[record('assets_total','2024',10000000000,raw='10000000000.00',unit='元'),
        record('liabilities_total','2024',5000000000,raw='5000000000.00',unit='元'),
        record('equity_total','2024',4999999900,raw='4999999900.00',unit='元')]
    c=e.analysis(rows)['reconciliations'][0];assert not c['passed'] and c['tolerance']=='0.015000'

def test_multi_project_award_table_keeps_project_amount_relationship():
    from finance_extract.extract import extract_middle
    middle={'pages':[{'page_idx':0,'blocks':[{'type':'text','content':'单位：万元 人民币'},
        {'type':'table','content':'<table><tr><td>项目名称</td><td>中标金额</td><td>中标单位</td></tr><tr><td>项目甲</td><td>1200</td><td>公司甲</td></tr><tr><td>项目乙</td><td>800</td><td>公司乙</td></tr><tr><td>合计</td><td>2000</td><td></td></tr></table>'}]}]}
    r=extract_middle(middle,'bid.pdf','abc');assert len(r['events'])==2
    assert [(x['fields']['project_name']['value'],x['fields']['award_amount']['base_value']) for x in r['events']]==[('项目甲','12000000'),('项目乙','8000000')]

def test_revenue_dimension_cannot_be_a_percentage():
    r=e.analysis([record('revenue','2024',100,base_unit='%')])
    assert r['usable_metric_groups']==0
