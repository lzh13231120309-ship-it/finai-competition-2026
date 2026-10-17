from server.report_consistency import check_consistency
from server import report_claims

def row(code,value,line=1,tolerance='0'):
 return dict(id=str(line),code=code,value=value,period='2025-12-31',scope='consolidated',line=line,display_tolerance=tolerance)

def test_cross_section_conflict_without_source():
 r=check_consistency([row('revenue','100',1),row('revenue','200',8)])
 assert r['issues'][0]['related_line']==8

def test_rounded_values_and_distinct_periods_are_not_conflicts():
 b=row('revenue','101',2,'1');assert not check_consistency([row('revenue','100',1,'1'),b])['issues']
 b['period']='2024-12-31';b['value']='200';assert not check_consistency([row('revenue','100'),b])['issues']

def test_margin_uses_same_period_operands():
 rows=[row('revenue','100'),row('cost_of_revenue','60'),row('gross_margin','.3',3,'.00005')]
 r=check_consistency(rows);assert r['checks'][0]['expected_value']=='0.4';assert r['issues'][0]['type']=='报告比率不可复算'
 rows[1]['scope']='parent';assert not check_consistency(rows)['checks']

def test_wacc_growth_condition():
 assert check_consistency([],{'scenarios':{'base':{'parameters':{'wacc':'.03','terminal_growth':'.04'}}}})['issues']

def test_table_rows_with_explicit_dates_are_not_headers():
 pool=[dict(code='revenue',value='100000000',period='2025-12-31',scope='consolidated',kind='amount',source='fact',evidence_ids=['a123456789abcdef:p1:b1'])]
 r=report_claims.review_claims('# 合并\n| 科目 | 期间 | 金额 |\n| 营业收入 | 2025年 | 1亿元 |',pool)
 assert r['stats']['checked']>0

def test_operand_rounding_uncertainty_is_propagated():
 rows=[row('revenue','100',1,'10'),row('cost_of_revenue','60',2,'10'),row('gross_margin','.3',3,'.00005')]
 assert not check_consistency(rows)['issues']
