"""Editable valuation artifact. Amounts remain in yuan; Excel stores cached engine results."""
from pathlib import Path
import json
import math
import xlsxwriter
from xlsxwriter.utility import xl_rowcol_to_cell as cell

DRIVERS = [
    ('revenue_base','基期营业收入（元）',None), ('revenue_growth','收入增长率','pct'),
    ('gross_margin','毛利率','pct'), ('opex_ratio','经营费用率','pct'),
    ('tax_rate','所得税率','pct'), ('da_to_revenue','折旧摊销 / 收入','pct'),
    ('capex_to_revenue','资本开支 / 收入','pct'), ('receivable_days','应收账款周转天数',None),
    ('inventory_days','存货周转天数',None), ('payable_days','应付账款周转天数',None),
    ('wacc','折现率','pct'), ('terminal_growth','永续增长率','pct'),
    ('net_debt','净债务（元）',None), ('minority_interest','少数股东权益（元）',None),
    ('non_operating_assets','非经营性资产（元）',None), ('shares','股数（股）',None),
    ('impairment_to_revenue','首年减值 / 收入','pct'),
    ('near_term_fcff_haircut','前两年现金流折减','pct'),
    ('sustainable_profit_discount','持续盈利折减','pct'), ('terminal_roic','终值投入资本回报率','pct'),
    ('book_equity','期初账面权益（元）',None), ('net_income','首年净利润（元）',None),
    ('cost_of_equity','权益资本成本','pct'), ('roe','初始 ROE','pct'),
    ('payout_ratio','分红支付率','pct'), ('terminal_roe','稳态 ROE','pct'), ('years','预测年数',None),
]
ALIASES={'revenue_growth':'growth','gross_margin':'gm','opex_ratio':'opex','tax_rate':'tax','da_to_revenue':'da_r','capex_to_revenue':'capex_r','receivable_days':'ar_days','inventory_days':'inv_days','payable_days':'ap_days','terminal_growth':'g','net_debt':'netdebt','minority_interest':'minority','non_operating_assets':'nonop','near_term_fcff_haircut':'haircuts','sustainable_profit_discount':'profit_discount'}


def _number(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError):
        return None


def _text(v):
    return json.dumps(v,ensure_ascii=False,default=str) if isinstance(v,(dict,list)) else str(v if v is not None else '')


def export_valuation_workbook(run: dict, path: Path) -> Path:
    """Write self-contained formulas and numeric caches, with no external workbook links."""
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    book=xlsxwriter.Workbook(str(path),{'strings_to_formulas':False,'strings_to_urls':False})
    book.set_calc_mode('auto')
    common={'font_name':'Arial','font_size':10}
    fmt=book.add_format(common)
    heading=book.add_format({**common,'bold':True,'bg_color':'#17365D','font_color':'white','text_wrap':True})
    input_fmt=book.add_format({**common,'font_color':'#0000FF','bg_color':'#FFF2CC','num_format':'#,##0.00;(#,##0.00);"-"'})
    input_pct=book.add_format({**common,'font_color':'#0000FF','bg_color':'#FFF2CC','num_format':'0.0%;(0.0%);"-"'})
    num=book.add_format({**common,'num_format':'#,##0.00;(#,##0.00);"-"'})
    pct=book.add_format({**common,'num_format':'0.0%;(0.0%);"-"'})
    link=book.add_format({**common,'font_color':'#008000','num_format':'#,##0.00;(#,##0.00);"-"'})
    sheets={}
    def sheet(name,headers=None):
        ws=book.add_worksheet(name); ws.set_default_row(21); ws.set_row(0,38); ws.set_column(0,0,30,fmt); ws.set_column(1,25,19,fmt); ws.freeze_panes(2,1); sheets[name]=ws
        if headers:
            for c,h in enumerate(headers): ws.write_string(0,c,h,heading)
        return ws
    def formula(ws,r,c,f,value=None,style=None):
        ws.write_formula(r,c,f,style or num,'' if _number(value) is None else float(value))
    readme=sheet('使用说明',['自动估值 · 可编辑模型','说明'])
    notes=[('编辑方式','蓝字黄底为假设输入；逐年预测的驱动格可直接输入数值覆盖默认引用。年度驱动表记录可编辑的逐年差值；Excel 打开后自动重算。'),('金额单位','所有金额以元存储，股数以股存储；没有缩放成万元。'),('币种',run.get('currency') or run.get('model_plan',{}).get('currency') or run.get('inputs_used',{}).get('currency','以输入证据为准')),('模型版本',run.get('engine_version','')),('终值','终值采用下一年 NOPAT × (1 − g / ROIC)，并传导持续盈利折减；g 必须小于折现率及 ROIC。'),('缓存说明','初始公式缓存来自同次估值引擎，可由 Excel 重算。调整输入后需使用支持计算的电子表格软件。'),('诊断快照','风险传导、可比公司、反向估值、蒙特卡洛为本次引擎输出快照；修改假设后需重新运行自动估值以更新这些诊断。'),('方法选择',run.get('model_plan',{})),('折现率来源',run.get('discount_rate',{})),('模型检查',run.get('checks',{})),('警示',run.get('warnings',[]))]
    readme.set_column(1,1,110,book.add_format({**common,'text_wrap':True}))
    for r,(k,v) in enumerate(notes,1):
        readme.set_row(r,42); readme.write_string(r,0,k); readme.write_string(r,1,_text(v))
    scenarios=run.get('scenarios') or {}
    valid=[(key,scenarios[key]) for key in ('base','optimistic','stress') if isinstance(scenarios.get(key),dict)]
    assumptions=sheet('假设输入',['假设 / 单位']+[s.get('label',k) for k,s in valid]+['来源说明'])
    refs={}; params_all={}
    for c,(key,sc) in enumerate(valid,1):
        p=sc.get('parameters') or {}; params_all[key]=p; refs[key]={}
        for r,(name,label,kind) in enumerate(DRIVERS,2):
            assumptions.write_string(r,0,label)
            value=p.get(name,p.get(ALIASES.get(name,'')))
            if name=='years' and value is None: value=len(sc.get('projection') or [])
            refs[key][name]="'假设输入'!"+cell(r,c,True,True)
            n=_number(value)
            if n is not None: assumptions.write_number(r,c,n,input_pct if kind=='pct' else input_fmt)
            else: assumptions.write_blank(r,c,None,input_pct if kind=='pct' else input_fmt)
            assumptions.write_comment(r,c,'来源：'+_text((run.get('assumption_sources') or {}).get(name,'情景参数；详见输入证据')))
    valuation=sheet('DCF估值',['估值项目']+[s.get('label',k) for k,s in valid])
    labels=['状态','显性期 FCFF 现值（元）','下一年 NOPAT（元）','终值 FCFF（元）','终值（元）','终值现值（元）','企业价值（元）','净债务（元）','少数股东权益（元）','非经营性资产（元）','权益价值（元）','每股价值（元 / 股）','终值占企业价值','折现率','永续增长率','终值 ROIC']
    for r,label in enumerate(labels,2): valuation.write_string(r,0,label)
    forecast_sheets={}
    for c,(key,sc) in enumerate(valid,1):
        a=refs[key]; rows=sc.get('projection') or []; name='逐年预测-'+sc.get('label',key); name=name[:31]
        if rows and 'opening_book_equity' in rows[0]:
            ws=sheet(name,['预测年','ROE','期初账面权益（元）','净利润（元）','股利（元）','剩余收益（元）','期末账面权益（元）','折现系数','剩余收益现值（元）'])
            for r,row in enumerate(rows,2):
                t=int(row.get('year',r-1)); ws.write_number(r,0,t)
                x=lambda col:cell(r,col)
                formula(ws,r,1,f'={a["roe"]}+({a["terminal_roe"]}-{a["roe"]})*IF({a["years"]}>1,({x(0)}-1)/MAX(1,{a["years"]}-1),0)',row.get('roe'),input_pct)
                formula(ws,r,2,'='+ (a['book_equity'] if r==2 else cell(r-1,6)),row.get('opening_book_equity'))
                formula(ws,r,3,('='+a['net_income']) if r==2 else f'={x(2)}*{x(1)}',row.get('net_income'))
                formula(ws,r,4,f'={x(3)}*{a["payout_ratio"]}',row.get('dividends'))
                formula(ws,r,5,f'={x(3)}-{x(2)}*{a["cost_of_equity"]}',row.get('residual_income'))
                formula(ws,r,6,f'={x(2)}+{x(3)}-{x(4)}',row.get('closing_book_equity'))
                formula(ws,r,7,f'=IF({a["cost_of_equity"]}>-1,1/(1+{a["cost_of_equity"]})^{x(0)},"")',row.get('discount_factor'))
                formula(ws,r,8,f'=IF(ISNUMBER({x(7)}),{x(5)}*{x(7)},"")',row.get('pv_residual_income'))
            v=sc.get('valuation') or {}; last=len(rows)+2; q="'"+name+"'!"; vcell=lambda r:cell(r,c)
            g=f'({a["terminal_roe"]}*(1-{a["payout_ratio"]}))'
            ok=f'AND({a["cost_of_equity"]}>{g},{a["cost_of_equity"]}>-1,{a["book_equity"]}>0)'
            guarded=lambda exp:f'=IF({ok},{exp},"")'
            ri_labels={3:'显性期剩余收益现值（元）',4:'期初账面权益（元）',5:'终值剩余收益（元）',8:'账面权益 + 剩余收益价值（元）',15:'权益资本成本',17:'稳态 ROE'}
            if c==1:
                for rr,ll in ri_labels.items(): valuation.write_string(rr,0,ll)
            valuation.write_formula(2,c,f'=IF({ok},"有效：剩余收益法","无效：请检查权益成本与终值增长率")',fmt,'有效：剩余收益法' if v.get('ok') else '无效：请检查权益成本与终值增长率')
            formula(valuation,3,c,guarded(f'SUM({q}I3:I{last})'),v.get('pv_explicit_residual_income'))
            formula(valuation,4,c,'='+a['book_equity'],params_all[key].get('book_equity'))
            formula(valuation,5,c,guarded(f'{q}G{last}*({a["terminal_roe"]}-{a["cost_of_equity"]})'),v.get('terminal_residual_income'))
            formula(valuation,6,c,guarded(f'{vcell(5)}/({a["cost_of_equity"]}-{g})'),v.get('terminal_value'))
            formula(valuation,7,c,guarded(f'{vcell(6)}*{q}H{last}'),v.get('pv_terminal_value'))
            formula(valuation,8,c,guarded(f'{vcell(4)}+{vcell(3)}+{vcell(7)}'),v.get('equity_value'))
            formula(valuation,12,c,guarded(vcell(8)),v.get('equity_value'))
            formula(valuation,13,c,f'=IF(AND({ok},{a["shares"]}>0),{vcell(12)}/{a["shares"]},"")',v.get('value_per_share'))
            formula(valuation,14,c,f'=IF(AND({ok},{vcell(12)}<>0),{vcell(7)}/{vcell(12)},"")',v.get('terminal_value_share'),pct)
            formula(valuation,15,c,'='+a['cost_of_equity'],params_all[key].get('cost_of_equity'),pct)
            formula(valuation,16,c,'='+g,v.get('terminal_growth'),pct)
            formula(valuation,17,c,'='+a['terminal_roe'],params_all[key].get('terminal_roe'),pct)
            continue
        headers=['预测年','收入增长率','毛利率','费用率','税率','折旧率','资本开支率','应收天数','存货天数','应付天数','营业收入（元）','EBIT（元）','NOPAT（元）','折旧摊销（元）','资本开支（元）','营运资本（元）','营运资本增加（元）','FCFF（元）','折现系数','FCFF现值（元）']
        ws=sheet(name,headers); forecast_sheets[key]=(name,rows)
        delta_ws=sheet(('年度驱动-'+sc.get('label',key))[:31],['预测年 / 相对初始假设的差值']+headers[1:10])
        for r,row in enumerate(rows,2):
            t=row.get('year',r-1); ws.write_number(r,0,int(t))
            for col,d in enumerate(['revenue_growth','gross_margin','opex_ratio','tax_rate','da_to_revenue','capex_to_revenue','receivable_days','inventory_days','payable_days'],1):
                baseline=_number(params_all[key].get(d,params_all[key].get(ALIASES.get(d,''))))
                actual=_number(row.get(d,baseline))
                delta=(actual-baseline) if actual is not None and baseline is not None else 0
                delta_ws.write_number(r,col,delta,input_pct if col<=6 else input_fmt)
                delta_ws.write_comment(r,col,'引擎逐年路径与初始情景假设的差值。修改此蓝字格调整单年路径；修改假设输入调整整条路径。来源：'+_text((run.get('assumption_sources') or {}).get(d,{})))
                delta_ref="'"+delta_ws.get_name()+"'!"+cell(r,col)
                formula(ws,r,col,'='+a[d]+'+'+delta_ref,actual,input_pct if col<=6 else input_fmt)
            x=lambda col:cell(r,col); prev_rev=a['revenue_base'] if r==2 else cell(r-1,10)
            formula(ws,r,10,f'={prev_rev}*(1+{x(1)})',row.get('revenue'))
            formula(ws,r,11,f'={x(10)}*({x(2)}-{x(3)})-IF({x(0)}=1,{x(10)}*{a["impairment_to_revenue"]},0)',row.get('ebit'))
            formula(ws,r,12,f'={x(11)}-MAX({x(11)},0)*{x(4)}',row.get('nopat'))
            formula(ws,r,13,f'={x(10)}*{x(5)}',row.get('depreciation_amortization',row.get('da')))
            formula(ws,r,14,f'={x(10)}*{x(6)}',row.get('capex'))
            formula(ws,r,15,f'={x(10)}*({x(7)}+({x(8)}-{x(9)})*MAX(0,MIN(1,1-{x(2)})))/365',row.get('nwc'))
            prev_nwc=f'{a["revenue_base"]}*({a["receivable_days"]}+({a["inventory_days"]}-{a["payable_days"]})*MAX(0,MIN(1,1-{a["gross_margin"]})))/365' if r==2 else cell(r-1,15)
            formula(ws,r,16,f'={x(15)}-({prev_nwc})',row.get('delta_nwc'))
            raw=f'({x(12)}+{x(13)}-{x(14)}-{x(16)})'
            near=f'({raw}-ABS({raw})*IF({x(0)}<=2,{a["near_term_fcff_haircut"]},0))'
            formula(ws,r,17,f'={near}-ABS({near})*{a["sustainable_profit_discount"]}',row.get('fcff'))
            formula(ws,r,18,f'=IF({a["wacc"]}>-1,1/(1+{a["wacc"]})^{x(0)},"")',row.get('discount_factor'))
            cached_pv=row.get('pv_fcff')
            if cached_pv is None and _number(row.get('fcff')) is not None and _number(params_all[key].get('wacc')) is not None: cached_pv=float(row['fcff'])/(1+float(params_all[key]['wacc']))**int(t)
            formula(ws,r,19,f'=IF(ISNUMBER({x(18)}),{x(17)}*{x(18)},"")',cached_pv)
        v=sc.get('valuation') or {}; end=len(rows)+1; q="'"+name+"'!"; vcell=lambda r:cell(r,c)
        valid_expr=f'AND({a["wacc"]}>{a["terminal_growth"]},{a["terminal_roic"]}>{a["terminal_growth"]},{a["terminal_roic"]}>0,{a["wacc"]}>-1,{len(rows)}>0)'
        valuation.write_formula(2,c,f'=IF({valid_expr},"有效","无效：请检查折现率、增长率、ROIC和预测期")',fmt,'有效' if v.get('ok') else '无效：请检查折现率、增长率、ROIC和预测期')
        guarded=lambda exp:f'=IF({valid_expr},{exp},"")'
        formula(valuation,3,c,guarded(f'SUM({q}T3:T{end+1})'),v.get('pv_explicit_fcff'))
        terminal_nopat=v.get('terminal_nopat')
        if terminal_nopat is None and rows and _number(rows[-1].get('nopat')) is not None:
            terminal_ebit=float(rows[-1]['revenue'])*(1+float(params_all[key].get('terminal_growth',0)))*(float(rows[-1].get('gross_margin',params_all[key].get('gross_margin',0)))-float(rows[-1].get('opex_ratio',params_all[key].get('opex_ratio',0))))
            terminal_nopat=terminal_ebit-max(terminal_ebit,0)*float(rows[-1].get('tax_rate',params_all[key].get('tax_rate',0)))
        te=f'({q}K{end+1}*(1+{a["terminal_growth"]})*({q}C{end+1}-{q}D{end+1}))'
        formula(valuation,4,c,guarded(f'{te}-MAX({te},0)*{q}E{end+1}'),terminal_nopat)
        tf=f'({vcell(4)}*(1-{a["terminal_growth"]}/{a["terminal_roic"]}))'
        formula(valuation,5,c,guarded(f'{tf}-ABS({tf})*{a["sustainable_profit_discount"]}'),v.get('terminal_fcff'))
        formula(valuation,6,c,guarded(f'{vcell(5)}/({a["wacc"]}-{a["terminal_growth"]})'),v.get('terminal_value'))
        formula(valuation,7,c,guarded(f'{vcell(6)}*{q}S{end+1}'),v.get('pv_terminal_value'))
        formula(valuation,8,c,guarded(f'{vcell(3)}+{vcell(7)}'),v.get('enterprise_value'))
        for r,d in [(9,'net_debt'),(10,'minority_interest'),(11,'non_operating_assets')]: formula(valuation,r,c,'='+a[d],params_all[key].get(d),link)
        formula(valuation,12,c,guarded(f'{vcell(8)}-{vcell(9)}-{vcell(10)}+{vcell(11)}'),v.get('equity_value'))
        formula(valuation,13,c,f'=IF(AND({valid_expr},{a["shares"]}>0),{vcell(12)}/{a["shares"]},"")',v.get('value_per_share'))
        formula(valuation,14,c,f'=IF(AND({valid_expr},{vcell(8)}<>0),{vcell(7)}/{vcell(8)},"")',v.get('terminal_value_share'),pct)
        for r,d in [(15,'wacc'),(16,'terminal_growth'),(17,'terminal_roic')]: formula(valuation,r,c,'='+a[d],params_all[key].get(d),pct)
    evidence=sheet('输入证据',['字段','来源 / 计算方法 / 原文 / 文档 / 页码','来源链接'])
    evidence.set_column(1,1,120,book.add_format({**common,'text_wrap':True}))
    source=(run.get('inputs_used') or {})
    items=list(source.items()) if isinstance(source,dict) else [(str(i),v) for i,v in enumerate(source)]
    items+=list((run.get('assumption_sources') or {}).items()) if isinstance(run.get('assumption_sources',{}),dict) else [('假设来源',run.get('assumption_sources'))]
    for r,(k,v) in enumerate(items,1):
        evidence.write_string(r,0,str(k)); evidence.write_string(r,1,_text(v))
        if isinstance(v,dict):
            url=v.get('url') or v.get('source_url') or v.get('document_url')
            if isinstance(url,str) and url.startswith(('https://','http://')):
                evidence.write_url(r,2,url,string='查看原始来源')
    for tab,key in [('结论校验','valuation_review'),('风险传导','risk_adjustments'),('可比公司','peers'),('反向估值','reverse_dcf'),('蒙特卡洛','monte_carlo')]:
        ws=sheet(tab,['项目','结果 / 说明'])
        data=run.get(key,{}) if key!='risk_adjustments' else {k:s.get(key,[]) for k,s in valid}
        entries=data.items() if isinstance(data,dict) else [('结果',data)]
        for r,(k,v) in enumerate(entries,1): ws.write_string(r,0,str(k)); ws.write_string(r,1,_text(v))
        ws.set_column(1,1,100,book.add_format({**common,'text_wrap':True}))
    sensitivity=sheet('敏感性',['折现率 / 永续增长率'])
    if 'base' in forecast_sheets:
        a=refs['base']; name,rows=forecast_sheets['base']; q="'"+name+"'!"; last=len(rows)+2
        base_w=_number(params_all['base'].get('wacc')); base_g=_number(params_all['base'].get('terminal_growth'))
        if base_w is not None and base_g is not None and rows and 'fcff' in rows[0]:
            for c,delta in enumerate([-.01,-.005,0,.005,.01],1): formula(sensitivity,1,c,f'={a["terminal_growth"]}+({delta})',base_g+delta,input_pct)
            for r,delta in enumerate([-.02,-.01,0,.01,.02],2):
                w=base_w+delta; formula(sensitivity,r,0,f'={a["wacc"]}+({delta})',w,input_pct)
                for c in range(1,6):
                    gcell=cell(1,c,True,False); wcell=cell(r,0,False,True); g=base_g+[-.01,-.005,0,.005,.01][c-1]
                    terminal_ebit=f'({q}K{last}*(1+{gcell})*({q}C{last}-{q}D{last}))'
                    terminal_nopat=f'({terminal_ebit}-MAX({terminal_ebit},0)*{q}E{last})'
                    terminal_raw=f'({terminal_nopat}*(1-{gcell}/{a["terminal_roic"]}))'
                    expr=f'SUMPRODUCT({q}R3:R{last},1/(1+{wcell})^{q}A3:A{last})+({terminal_raw}-ABS({terminal_raw})*{a["sustainable_profit_discount"]})/({wcell}-{gcell})/(1+{wcell})^{len(rows)}-{a["net_debt"]}-{a["minority_interest"]}+{a["non_operating_assets"]}'
                    roic=_number(params_all['base'].get('terminal_roic')); cached=None
                    if roic is not None and w>g and roic>g and w>-1 and roic>0:
                        cached=sum(float(x['fcff'])/(1+w)**int(x['year']) for x in rows)+(float(rows[-1]['nopat'])*(1+g)*(1-g/roic)-abs(float(rows[-1]['nopat'])*(1+g)*(1-g/roic))*float(params_all['base'].get('sustainable_profit_discount',0)))/(w-g)/(1+w)**len(rows)-float(params_all['base'].get('net_debt',0))-float(params_all['base'].get('minority_interest',0))+float(params_all['base'].get('non_operating_assets',0))
                    formula(sensitivity,r,c,f'=IF(AND({wcell}>{gcell},{a["terminal_roic"]}>{gcell},{a["terminal_roic"]}>0,{wcell}>-1),{expr},"")',cached)
    sensitivity.write_string(9,0,'敏感性口径')
    sensitivity.write_string(9,1,'固定已导出逐年经营路径，仅改变折现率和终值增长；引擎全模型敏感性另见报告。')
    sensitivity.conditional_format(2,1,6,5,{'type':'3_color_scale','min_color':'#F8696B','mid_color':'#FFEB84','max_color':'#63BE7B'})
    book.close()
    return path
