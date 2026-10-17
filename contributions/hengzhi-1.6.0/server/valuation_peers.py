"""Comparable valuation from explicit, sourced and comparable peer snapshots.

No invented market multiples: samples, exclusions and equity bridge are kept.
"""
from decimal import Decimal, InvalidOperation, localcontext


def _d(value):
    try:
        result=Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation,ValueError,TypeError): return None


def _s(value): return format(value,'f') if value is not None else None


def _quantile(values, fraction):
    ordered=sorted(values);position=Decimal(len(ordered)-1)*fraction
    lo=int(position);hi=min(lo+1,len(ordered)-1)
    return ordered[lo]+(ordered[hi]-ordered[lo])*(position-lo)


def comparable_valuation(target:dict, peers:list, max_age_days=31):
    from datetime import date
    currency=target.get('currency');period=target.get('period');scope=target.get('scope','consolidated')
    industry=target.get('industry');accepted=[];excluded=[];seen=set();seen_symbols=set();warnings=[]
    def iso_date(raw):
        if not isinstance(raw,str) or len(raw)!=10:raise ValueError('日期必须为YYYY-MM-DD')
        parsed=date.fromisoformat(raw)
        if parsed.isoformat()!=raw:raise ValueError('日期必须为YYYY-MM-DD')
        return parsed
    valuation_date=iso_date(target['valuation_date']) if target.get('valuation_date') else date.today()
    if valuation_date>date.today():raise ValueError('估值日期不能在未来')
    reference_date=iso_date(target.get('market_as_of') or target.get('as_of')) if target.get('market_as_of') or target.get('as_of') else valuation_date
    if reference_date>valuation_date:raise ValueError('目标市场快照日期晚于估值日期')
    if not isinstance(max_age_days,int) or isinstance(max_age_days,bool) or max_age_days<0:raise ValueError('max_age_days必须是非负整数')
    if (valuation_date-reference_date).days>max_age_days:warnings.append('目标市场快照已过期；同行比较沿用显式历史快照，不能称为当前估值。')
    target_company=str(target.get('company') or '').strip().casefold()
    target_symbol=str(target.get('symbol') or '').strip().casefold()
    if not industry or industry=='auto': warnings.append('目标行业未明确，同行行业可比性需人工确认。')
    if len(peers)>200:raise ValueError('同行样本最多200家公司')
    parsed_dates=[]
    for peer in peers:
        reasons=[];company=str(peer.get('company') or peer.get('symbol') or '').strip()
        if not company:reasons.append('缺少公司标识')
        elif company.casefold() in seen:reasons.append('公司重复')
        else:seen.add(company.casefold())
        symbol=str(peer.get('symbol') or '').strip().casefold()
        if symbol and symbol in seen_symbols:reasons.append('证券代码重复')
        if symbol:seen_symbols.add(symbol)
        if (target_company and company.casefold()==target_company) or (target_symbol and symbol==target_symbol):reasons.append('目标公司不能作为自身同行')
        if not peer.get('source'):reasons.append('缺少数据来源')
        if not currency or peer.get('currency')!=currency:reasons.append('币种不一致或缺失')
        if not period or peer.get('period')!=period:reasons.append('财务期间不一致或缺失')
        if peer.get('scope')!=scope:reasons.append('报表口径不一致或缺失')
        if industry and industry!='auto' and peer.get('industry')!=industry:reasons.append('行业不一致或缺失')
        try:
            as_of=iso_date(peer.get('as_of'))
            if as_of>valuation_date:reasons.append('同行市场快照日期晚于估值日期')
            elif abs((reference_date-as_of).days)>max_age_days:reasons.append('同行市场报价与目标估值快照不同步或已过期')
        except ValueError:as_of=None;reasons.append('缺少有效市场数据日期')
        if reasons:excluded.append({'company':company,'reasons':reasons});continue
        parsed_dates.append(as_of);accepted.append(dict(peer,company=company))
    if parsed_dates:
        latest=max(parsed_dates);kept=[]
        for peer in accepted:
            if (latest-date.fromisoformat(peer['as_of'])).days>max_age_days:
                excluded.append({'company':peer['company'],'reasons':['市场报价日期与同行样本不同步']})
            else:kept.append(peer)
        accepted=kept
    financial=any(word in str(industry or '').lower() for word in ('bank','insurance','securit','financial','银行','保险','证券','金融'))
    specifications={'pe':('market_cap','net_income',False), 'pb':('market_cap','book_equity',False),
                    'ev_ebitda':('enterprise_value','ebitda',True),'ev_sales':('enterprise_value','revenue',True)}
    methods={}
    with localcontext() as context:
        context.prec=28
        for method,(numerator,denominator,enterprise) in specifications.items():
            samples=[];unusable=[]
            if financial and enterprise:
                methods[method]={'ok':False,'n':0,'reason':'金融企业债务与经营活动难以分离，企业价值倍数不适用'};continue
            for peer in accepted:
                num=_d(peer.get(numerator));den=_d(peer.get(denominator))
                if num is None and enterprise:
                    cap=_d(peer.get('market_cap'));debt=_d(peer.get('net_debt'));minority=_d(peer.get('minority_interest'));nonop=_d(peer.get('non_operating_assets'))
                    if None not in (cap,debt,minority,nonop):num=cap+debt+minority-nonop
                if num is None or den is None or num<=0 or den<=0:
                    unusable.append({'company':peer['company'],'reason':'分子或分母缺失/非正，不能构成有效倍数'});continue
                samples.append({'company':peer['company'],'multiple':num/den,'source':peer['source'],'as_of':peer['as_of']})
            values=[sample['multiple'] for sample in samples]
            removed=[]
            if len(values)>=5:
                q1=_quantile(values,Decimal('.25'));q3=_quantile(values,Decimal('.75'));iqr=q3-q1
                if iqr>0:
                    kept=[]
                    for sample in samples:
                        if sample['multiple']<q1-Decimal('1.5')*iqr or sample['multiple']>q3+Decimal('1.5')*iqr:removed.append({**sample,'multiple':_s(sample['multiple'])})
                        else:kept.append(sample)
                    samples=kept;values=[sample['multiple'] for sample in samples]
            amount=_d(target.get(denominator));n=len(values)
            result={'ok':bool(values and amount is not None and amount>0),'n':n,'samples':[{**s,'multiple':_s(s['multiple'])} for s in samples],'unusable':unusable,'outliers':removed,
                    'currency':currency,'period':period,'formula':f'{numerator}/{denominator}','sample_quality':'adequate' if n>=3 else 'thin'}
            if not values:
                result['reason']='没有有效可比倍数';methods[method]=result;continue
            multiples={name:_quantile(values,fraction) for name,fraction in [('low',Decimal('.25')),('median',Decimal('.5')),('high',Decimal('.75'))]}
            result.update(median_multiple=_s(multiples['median']),p25_multiple=_s(multiples['low']),p75_multiple=_s(multiples['high']))
            if amount is None or amount<=0:
                result['reason']='目标公司对应基数缺失或非正';methods[method]=result;continue
            implied={name:multiple*amount for name,multiple in multiples.items()}
            if enterprise:
                result['enterprise_values']={name:_s(value) for name,value in implied.items()}
                bridge=[_d(target.get(k)) for k in ('net_debt','minority_interest','non_operating_assets')]
                equity={name:value-bridge[0]-bridge[1]+bridge[2] for name,value in implied.items()} if None not in bridge else None
                if equity is None:result['bridge_missing']=[k for k,v in zip(('net_debt','minority_interest','non_operating_assets'),bridge) if v is None]
            else:equity=implied
            result['equity_values']={name:_s(value) for name,value in equity.items()} if equity else None
            shares=_d(target.get('shares'))
            result['per_share_values']={name:_s(value/shares) for name,value in equity.items()} if equity and shares and shares>0 else None
            if n<3:warnings.append(f'{method}仅有{n}个有效样本，分位区间只供参考。')
            methods[method]=result
    return {'ok':any(method.get('ok') for method in methods.values()),'methods':methods,'accepted_peers':accepted,'excluded_peers':excluded,
        'warnings':warnings,'currency':currency,'period':period,'scope':scope,'valuation_date':valuation_date.isoformat(),'market_reference_date':reference_date.isoformat(),'note':'同行数据是有来源的输入快照，未自动证明真实可靠；方法间不自动平均，区间反映样本分位，不能解释为价格概率。'}
