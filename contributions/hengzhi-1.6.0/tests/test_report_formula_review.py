import pytest
from server.report_formula_review import arithmetic,review_formulas,review_conclusions,FormulaError

def test_percentage_and_chinese_units():
 assert review_formulas('毛利率=(100-60)/100=40.00%')['checks'][0]['status']=='pass'
 assert review_formulas('合计=1亿元+5000万元=1.50亿元')['checks'][0]['status']=='pass'

def test_wrong_formula_and_negatives():
 r=review_formulas('增长=(120-100)/100=30.00%')
 assert r['issues'][0]['type']=='显式公式计算错误'
 assert review_formulas('差额=(60-100)/100=-40.00%')['checks'][0]['status']=='pass'

def test_zero_and_dimension_conflicts_are_pending():
 assert review_formulas('比率=10/0=0%')['checks'][0]['status']=='unverifiable'
 assert review_formulas('合计=1亿元+10%=1亿元')['checks'][0]['status']=='unverifiable'

def test_arbitrary_code_cannot_execute():
 with pytest.raises(FormulaError):arithmetic('__import__("os").system("true")')
 with pytest.raises(FormulaError):arithmetic('2**1000000')
 assert not review_formulas('print("hello")')['checks']

def test_model_assumptions_are_not_equations():
 assert not review_formulas('WACC=10%')['checks']

def test_display_rounding():
 assert review_formulas('1/3=33.33%')['checks'][0]['status']=='pass'

def test_local_negation_cannot_hide_later_claim():
 assert not review_conclusions('风险分不表示造假概率。')['issues']
 assert review_conclusions('不是投资建议，但是风险分代表造假概率。')['issues']
 assert not review_conclusions('不能说风险分代表造假概率。')['issues']

def test_predictions_are_conditional():
 assert review_conclusions('DCF模型证明股价必然上涨。')['issues']
 assert not review_conclusions('DCF在假设成立时提供情景价值区间。')['issues']

def test_signed_first_operand_is_not_dropped():
 assert review_formulas('-100+60=-40')['checks'][0]['status']=='pass'
