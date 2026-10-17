"""Safe arithmetic and narrowly scoped conclusion checks. No eval or network."""
import ast
import re
from decimal import Decimal,InvalidOperation

UNITS={'亿元':(Decimal('1e8'),1),'百万元':(Decimal('1e6'),1),'万元':(Decimal('1e4'),1),'千元':(Decimal('1e3'),1),'元':(Decimal(1),1),'%':(Decimal('.01'),0),'％':(Decimal('.01'),0),'':(Decimal(1),0)}
TOKEN=re.compile(r'(?P<n>\d+(?:,\d{3})*(?:\.\d+)?)(?:\s*(?P<u>亿元|百万元|万元|千元|元|%|％))?')
EQUATION=re.compile(r'(?P<left>[-+−]?[\d（(][\d\s.,()+\-*/×÷−（）亿元百万千%％]{0,220}?)\s*[=＝]\s*(?P<right>[+\-−]?\d+(?:,\d{3})*(?:\.\d+)?\s*(?:亿元|百万元|万元|千元|元|%|％)?)')

class FormulaError(ValueError):pass

def arithmetic(expression):
 values={}
 def replace(m):
  key=f'v{len(values)}';scale,degree=UNITS[m['u'] or ''];values[key]=(Decimal(m['n'].replace(',',''))*scale,degree);return key
 source=TOKEN.sub(replace,expression.translate(str.maketrans({'×':'*','÷':'/','−':'-','（':'(','）':')'})))
 try:tree=ast.parse(source.strip(),mode='eval')
 except SyntaxError as exc:raise FormulaError('表达式格式不支持') from exc
 if len(list(ast.walk(tree)))>80:raise FormulaError('表达式过于复杂')
 def visit(node):
  if isinstance(node,ast.Expression):return visit(node.body)
  if isinstance(node,ast.Name) and node.id in values:return values[node.id]
  if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.USub,ast.UAdd)):
   v,d=visit(node.operand);return (-v if isinstance(node.op,ast.USub) else v,d)
  if isinstance(node,ast.BinOp) and isinstance(node.op,(ast.Add,ast.Sub,ast.Mult,ast.Div)):
   a,da=visit(node.left);b,db=visit(node.right)
   if isinstance(node.op,(ast.Add,ast.Sub)):
    if da!=db:raise FormulaError('加减运算单位维度不一致')
    return (a+b if isinstance(node.op,ast.Add) else a-b,da)
   if isinstance(node.op,ast.Mult):return a*b,da+db
   if b==0:raise FormulaError('分母为零，不能计算')
   return a/b,da-db
  raise FormulaError('仅支持数字、单位、四则运算及括号')
 return visit(tree)

def review_formulas(text):
 checks=[];issues=[]
 for line,raw in enumerate(text.splitlines(),1):
  for match in EQUATION.finditer(raw):
   left,right=match['left'].strip(),match['right'].strip()
   # Isolated equality is not a calculation: model assumptions like WACC=10%.
   if not re.search(r'[+\-*/×÷−]',left):continue
   try:
    value,degree=arithmetic(left);expected,expected_degree=arithmetic(right)
    if degree!=expected_degree:raise FormulaError('等号两侧单位维度不一致')
    token=TOKEN.search(right);digits=len(token['n'].split('.')[-1]) if '.' in token['n'] else 0
    tolerance=Decimal(1).scaleb(-digits)*UNITS[token['u'] or ''][0]/2
    passed=abs(value-expected)<=tolerance
    check={'line':line,'expression':match.group(),'status':'pass' if passed else 'fail','calculated_value':str(value),'stated_value':str(expected),'tolerance':str(tolerance),'basis':'仅复算明确列出的公式，不证明输入来自原始财报'}
    checks.append(check)
    if not passed:issues.append({'severity':'hard','type':'显式公式计算错误','line':line,'detail':match.group(),'advice':f'按报告列出的操作数计算为{value}（金额为基础元，比例为小数）；核对公式、操作数和单位。'})
   except (FormulaError,InvalidOperation) as exc:
    checks.append({'line':line,'expression':match.group(),'status':'unverifiable','reason':str(exc)})
    issues.append({'severity':'medium','type':'公式无法复算','line':line,'detail':match.group(),'advice':str(exc)})
 return {'checks':checks,'issues':issues}

def review_conclusions(text):
 issues=[]
 patterns=[(r'(?:风险分|风险评分|法证分数).{0,18}(?:就是|等于|代表|表示|意味着).{0,10}(?:造假|舞弊)概率','风险分误作概率','风险分仅用于规则排序，没有概率校准；删除造假概率表述。'),
           (r'(?:模型|DCF|估值结果).{0,20}(?:证明|保证|确保).{0,15}(?:必涨|必然上涨|确定收益|一定盈利)','预测当作确定事实','估值是依赖输入与情景的计算结果，不能证明确定收益。')]
 for line,raw in enumerate(text.splitlines(),1):
  for clause in re.split(r'[。；;，,]|但是|但|然而|不过',raw):
   for pattern,label,advice in patterns:
    m=re.search(pattern,clause)
    if not m:continue
    if re.search(r'(?:不|并非|不能)(?:就是|等于|代表|表示|意味着|证明|保证|确保)',m.group()):continue
    before=clause[max(0,m.start()-8):m.start()]
    if re.search(r'不能说|不得说|不要把|并非|不是|不应称|禁止声称|错误表述[：:]?$',before):continue
    issues.append({'severity':'medium','type':label,'line':line,'detail':clause.strip(),'advice':advice,'requires_human_review':True})
 return {'issues':issues,'limits':'窄范围表述提示；不自动判断欺诈或全部因果论证。'}
