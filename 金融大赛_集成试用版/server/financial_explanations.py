"""Auditable numerical explanations, not inferred business causes."""
import datetime
from decimal import Decimal as D
from . import research_engine as e

def build(values,evidence):
    bridges=[];turnover=[];questions=[]
    annual=sorted({k[:5] for k in values if k[5]=='revenue' and e.period_key(k[4])[1]=='FY'},key=str)
    for base in annual:
        issuer,scope,currency,basis,period=base;prefix=base[:4];year=int(period)
        prior=(*prefix,str(year-1))
        def fact(b,metric):return values.get((*b,metric)),evidence.get((*b,metric),[])
        def balance(y,metric):
            keys=[k for k in values if k[:4]==prefix and k[4] in (str(y),str(y)+'-year-end',str(y)+'-12-31') and k[5]==metric]
            if not keys or len({values[k] for k in keys})!=1:return None,[]
            return values[keys[0]],[r for k in keys for r in evidence[k]]
        rev,revrefs=fact(base,'revenue');cost,costrefs=fact(base,'revenue_cost')
        prev,prevrefs=fact(prior,'revenue');prevcost,prevcostrefs=fact(prior,'revenue_cost')
        common={'issuer':issuer,'scope':scope,'currency':currency,'basis':basis,'period':period}
        if all(x is not None for x in (rev,cost,prev,prevcost)) and rev>0 and prev>0 and cost>=0 and prevcost>=0:
            gm0=(prev-prevcost)/prev;gm1=(rev-cost)/rev
            scale=(rev-prev)*gm0;margin=rev*(gm1-gm0);delta=(rev-cost)-(prev-prevcost)
            bridges.append({**common,'comparison_period':str(year-1),'method':'gross_profit_bridge','gross_profit_change':e.fmt(delta),
                'revenue_effect':e.fmt(scale),'margin_effect':e.fmt(margin),'margin_change_pp':e.fmt((gm1-gm0)*100),'reconciled':abs(scale+margin-delta)<=D('.000001'),
                'formula':'Δ毛利=(本期收入-上期收入)×上期毛利率+本期收入×(本期毛利率-上期毛利率)',
                'interpretation':'固定分解顺序的会计恒等式；收入贡献包含价格与销量等变化，毛利率贡献包含结构及成本等变化，未证明业务因果。',
                'sources':revrefs+costrefs+prevrefs+prevcostrefs})
        days=(datetime.date(year+1,1,1)-datetime.date(year,1,1)).days
        for metric,name,denom,denomrefs in [('accounts_receivable','receivable_days',rev,revrefs),('inventory','inventory_days',cost,costrefs)]:
            closing,closingrefs=balance(year,metric);opening,openingrefs=balance(year-1,metric)
            item={**common,'metric':name,'days_in_period':days,'opening':e.fmt(opening),'closing':e.fmt(closing),'denominator':e.fmt(denom),
                'sources':openingrefs+closingrefs+denomrefs,'approximation':'期初期末平均余额；应收以营业收入代理赊销收入、存货以营业成本代理匹配成本，季节性及行业适用性需复核。'}
            if any(x is None for x in (opening,closing,denom)) or denom<=0 or opening<0 or closing<0:
                item.update(status='insufficient_or_invalid_input',value=None,reason='缺少同口径年初/年末余额或有效年度分母；不以年末余额冒充年度平均。')
            else:item.update(status='ok',value=e.fmt((opening+closing)/2/denom*days),formula='(年初余额+年末余额)/2/年度收入或成本×本年天数')
            turnover.append(item)
            if closing is not None and opening is not None and prev is not None and rev>0 and prev>0 and opening>0:
                growth=(closing-opening)/opening*100;rg=(rev-prev)/prev*100
                if growth-rg>D(20):
                    label='应收账款' if metric=='accounts_receivable' else '存货'
                    questions.append({**common,'metric':metric,'type':'testable_hypothesis','observation':f'{label}余额增长{growth:.2f}%，收入增长{rg:.2f}%，差距{growth-rg:.2f}个百分点。',
                        'question':'应收增加来自信用期、客户结构还是坏账风险？请核对账龄、信用政策及期后回款。' if metric=='accounts_receivable' else '存货增加来自备货、销售放缓还是跌价？请核对存货分类、订单及减值附注。',
                        'threshold_basis':'超过收入20个百分点是待校准的筛查阈值，不能证明经营原因或财务舞弊。','sources':openingrefs+closingrefs+revrefs+prevrefs})
    return {'profit_bridges':bridges,'turnover':turnover,'investigation_questions':questions}
