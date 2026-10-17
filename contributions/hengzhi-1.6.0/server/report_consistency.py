"""Cross-section contradictions and comparable-ratio recomputation, all local."""
from decimal import Decimal,InvalidOperation

def num(value):
 try:
  n=Decimal(str(value));return n if n.is_finite() else None
 except (InvalidOperation,ValueError):return None

def check_consistency(claims,valuation=None):
 issues=[];groups={};checks=[]
 for row in claims:
  key=(row.get('code'),row.get('period'),row.get('scope'),row.get('scenario'))
  if key[0] and (key[1] or key[3]) and num(row.get('value')) is not None:
   groups.setdefault(key,[]).append(row)
 for key,rows in groups.items():
  # Interval intersection is linear even for long reports with repeated values.
  a=max(rows,key=lambda r:num(r['value'])-(num(r.get('display_tolerance')) or Decimal(0)))
  b=min(rows,key=lambda r:num(r['value'])+(num(r.get('display_tolerance')) or Decimal(0)))
  low=num(a['value'])-(num(a.get('display_tolerance')) or Decimal(0))
  high=num(b['value'])+(num(b.get('display_tolerance')) or Decimal(0))
  if low>high:
   issues.append({'severity':'hard','type':'报告内部数值矛盾','line':min(a.get('line',0),b.get('line',0)),'related_line':max(a.get('line',0),b.get('line',0)),'detail':f'{key[0]}在第{a.get("line")}行与第{b.get("line")}行不一致','advice':'核对正文和表格的期间、口径与原文，统一为同一依据；不要仅选择看起来合理的数值。','claim_ids':[a.get('id'),b.get('id')]})
 formulas={'gross_margin':('revenue','cost_of_revenue','difference'),'net_margin':('net_profit','revenue','ratio'),'debt_ratio':('liabilities','assets','ratio'),'current_ratio':('current_assets','current_liabilities','ratio')}
 for (code,period,scope,scenario),rows in groups.items():
  if code not in formulas or scenario:continue
  first,second,kind=formulas[code]
  operands=[groups.get((field,period,scope,None),[]) for field in (first,second)]
  if any(not values or len({r['value'] for r in values})!=1 for values in operands):continue
  a,b=[num(values[0]['value']) for values in operands]
  denominator=a if kind=='difference' else b
  if denominator is None or denominator<=0:continue
  expected=(a-b)/a if kind=='difference' else a/b
  ta,tb=[num(values[0].get('display_tolerance')) or Decimal(0) for values in operands]
  if (a-ta if kind=='difference' else b-tb)<=0:continue
  corners=[(x-y)/x if kind=='difference' else x/y for x in (a-ta,a+ta) for y in (b-tb,b+tb)]
  minimum,maximum=min(corners),max(corners)
  for row in rows:
   difference=abs(num(row['value'])-expected);tolerance=num(row.get('display_tolerance')) or Decimal('.00005')
   passed=minimum-tolerance<=num(row['value'])<=maximum+tolerance
   checks.append({'code':code,'period':period,'scope':scope,'status':'pass' if passed else 'fail','expected_value':str(expected),'rounding_range':[str(minimum),str(maximum)],'claim_id':row.get('id'),'operand_claim_ids':[v[0].get('id') for v in operands],'basis':'仅复算报告自身操作数，不能证明操作数真实'})
   if not passed:issues.append({'severity':'hard','type':'报告比率不可复算','line':row.get('line'),'detail':f'{code}按同期间同口径操作数复算为{expected}','advice':'检查分子、分母、单位与舍入；若采用不同公式，应明确列出并解释。'})
 valuation=valuation or {}
 for name,scenario in (valuation.get('scenarios') or {}).items():
  params=scenario.get('parameters') or {};wacc=num(params.get('wacc'));g=num(params.get('terminal_growth'))
  if wacc is not None and g is not None:
   passed=wacc>g
   checks.append({'code':'terminal_discount_spread','scenario':name,'status':'pass' if passed else 'fail','wacc':str(wacc),'terminal_growth':str(g)})
   if not passed:issues.append({'severity':'hard','type':'终值折现条件不成立','detail':f'{name}情景WACC须高于永续增长率','advice':'修订并重新计算模型，不能继续引用现有永续终值。'})
 return {'checks':checks,'issues':issues,'limits':'只核查已识别且期间/口径明确的主张；没有错误不代表整篇报告一致。'}
