"""A 股公开数据 → 项目内部"事实/指标"格式的适配器（估值审计与离线复现用）。

为什么需要它
------------
估值引擎的输入是 `financial_analysis` 从 PDF 抽出来的"已核验事实"。评审方提供的材料
可能是 PDF，也可能是结构化数据；我们自己做真实性审计时则需要一批**多行业的真实 A 股
三表**。`tests/real_corpus/valuation/fetch_ashare.py` 用 akshare 取数落盘成 JSON
（新浪三表 + 东财现金流量表 + 前复权日 K），本模块把这种 JSON 转成引擎认识的事实列表，
让"现有引擎"和"新引擎"吃同一份输入，做改前/改后对比。

约定
----
- 金额单位：元（数据源本身即元），币种 CNY，口径 consolidated，period 为 YYYY-12-31。
- 每条事实都带 evidence_ids = ['akshare:<表>:<科目>:<年>']，可回溯到数据源字段。
- 缺失科目不写，不用 0 填；新浪对"无此科目"返回空值，空值不入库。
- 股数不在这里推算（见 valuation_pro.infer_shares）；这里只提供股本(元)，不假设面值。
"""
from __future__ import annotations

import json
from pathlib import Path

CORPUS = Path(__file__).resolve().parent.parent / 'tests' / 'real_corpus' / 'valuation' / 'data'

# 数据集键 -> (报表, 事实代码)。代码沿用 financial_analysis 的科目体系；
# 体系里没有的科目（长期股权投资、一年内到期的非流动负债等）使用本模块定义的扩展代码，
# 这些扩展代码被 valuation_pro 识别，旧引擎会忽略它们——这正是旧引擎漏计债务的来源之一。
IS_CODES = {
    'revenue': 'revenue', 'cogs': 'cost_of_revenue', 'taxes_surcharges': 'taxes_surcharges',
    'rd': 'research_expenses', 'selling': 'selling_expenses', 'admin': 'administrative_expenses',
    'fin_exp': 'financial_expenses', 'interest_exp': 'interest_expense', 'interest_inc': 'interest_income',
    'invest_income': 'investment_income', 'assoc_income': 'associate_income', 'fv_gain': 'fair_value_gain',
    'other_income': 'other_income', 'asset_impair': 'asset_impairment', 'credit_impair': 'credit_impairment',
    'op_profit': 'operating_profit', 'pbt': 'profit_before_tax', 'tax': 'income_tax',
    'net_profit': 'net_profit', 'parent_np': 'parent_net_profit', 'minority_pl': 'minority_profit',
    'eps': 'basic_eps',
}
BS_CODES = {
    'cash': 'cash', 'trading_fa': 'financial_assets_fvtpl', 'ar': 'receivables', 'notes_recv': 'notes_receivable',
    'recv_financing': 'receivables_financing', 'prepay': 'prepayments', 'other_recv': 'other_receivables',
    'inventory': 'inventory', 'contract_assets': 'contract_assets', 'ca_total': 'current_assets',
    'nca_total': 'noncurrent_assets', 'fixed_assets': 'fixed_assets', 'cip': 'construction_in_progress',
    'rou': 'right_of_use_assets', 'intangibles': 'intangibles', 'goodwill': 'goodwill',
    'lt_equity_inv': 'long_term_equity_investment', 'invest_property': 'investment_property',
    'other_equity_inv': 'other_equity_investment', 'other_nc_fa': 'other_noncurrent_financial_assets',
    'dta': 'deferred_tax_assets', 'total_assets': 'assets', 'ap': 'payables', 'notes_pay': 'notes_payable',
    'contract_liab': 'contract_liabilities', 'advance': 'advance_receipts', 'tax_pay': 'tax_payables',
    'other_pay': 'other_payables', 'cl_total': 'current_liabilities', 'ncl_total': 'noncurrent_liabilities',
    'st_debt': 'short_term_debt', 'lt_debt': 'long_term_debt', 'bonds': 'bonds_payable',
    'lease_liab': 'lease_liabilities', 'noncur_due_liab': 'noncurrent_due_liabilities',
    'st_bonds': 'short_term_bonds', 'trading_fl': 'trading_financial_liabilities',
    'dtl': 'deferred_tax_liabilities', 'total_liab': 'liabilities', 'share_capital': 'share_capital',
    'other_equity_instr': 'other_equity_instruments', 'perpetual': 'perpetual_bonds', 'preferred': 'preferred_shares',
    'retained': 'retained_earnings', 'parent_equity': 'parent_equity', 'minority_equity': 'minority_equity',
    'total_equity': 'equity', 'loans_net': 'loans_net', 'deposits': 'customer_deposits',
    'ins_reserve': 'insurance_reserves',
}
CF_CODES = {
    'cfo': 'operating_cash_flow', 'cfi': 'investing_cash_flow', 'cff': 'financing_cash_flow',
    'capex': 'capex', 'fa_depr': 'depreciation', 'ia_amort': 'amortization',
    'rou_depr': 'rou_depreciation', 'lpe_amort': 'long_term_prepaid_amortization',
    'ending_cash': 'cash_equivalents_end',
}


def load_dossier(code: str, root: Path | None = None) -> dict:
    path = Path(root or CORPUS) / f'{code}.json'
    return json.loads(path.read_text(encoding='utf-8'))


def load_market(root: Path | None = None) -> dict:
    return json.loads((Path(root or CORPUS) / '_market.json').read_text(encoding='utf-8'))


def _num_str(value) -> str:
    from decimal import Decimal
    return format(Decimal(repr(float(value))), 'f')


def dossier_to_facts(dossier: dict, years=None) -> list[dict]:
    """把一份 dossier 转成事实列表。years 缺省取全部年度。"""
    facts = []
    company = dossier.get('name', '')
    for table, mapping, instant in (('IS', IS_CODES, False), ('BS', BS_CODES, True), ('CF', CF_CODES, False)):
        for year, rec in sorted((dossier.get(table) or {}).items()):
            if years and year not in years:
                continue
            for key, code in mapping.items():
                value = rec.get(key)
                if value is None:
                    continue
                per_share = key == 'eps'
                facts.append({
                    'fact_id': f"ashare:{dossier['code']}:{table}:{key}:{year}",
                    'code': code, 'raw_label': key, 'company': company,
                    'period': f'{year}-12-31', 'period_type': 'instant' if instant else 'annual',
                    'scope': 'consolidated', 'currency': 'CNY', 'value': _num_str(value), 'raw_value': _num_str(value),
                    'unit': 'CNY/share' if per_share else '元', 'scale': 1,
                    'value_unit': 'CNY/share' if per_share else '元',
                    'evidence_ids': [f"akshare:{table}:{key}:{year}"], 'source_tier': 'primary',
                    'status': 'verified', 'confidence': 0.95,
                    'source': {'document_id': f"akshare:{dossier['code']}", 'statement': table},
                })
    # 注意：不再按"面值 1 元"由股本推算股数。实测紫金矿业面值 0.1 元，按 1 元推算会把市值少算 10 倍；
    # 股数由 valuation_pro.infer_shares 用 归母净利润/EPS 交叉验证面值后推断，并标注为推断（E2）。
    return facts


def last_price(dossier: dict, as_of: str | None = None) -> tuple[str, float]:
    rows = dossier.get('prices') or []
    if as_of:
        rows = [r for r in rows if r[0] <= as_of]
    return rows[-1][0], rows[-1][1]


def peers_to_candidates(peer_file: dict) -> list[dict]:
    """把 fetch_peers.py 的输出转成相对估值的候选同行（统一字段、统一口径）。

    股数 = 归母净利润 / 基本每股收益（加权，不假设面值）；市值 = 收盘价 × 股数；
    账面权益 = 每股净资产 × 股数；EBITDA、净债务类项目取自新浪三表 + 东财折旧摊销（口径与目标公司的桥一致）。
    银行类同行没有三表细项，仅提供 PE/PB 所需字段。
    """
    from decimal import Decimal as D
    out = []
    for p in peer_file.get('peers', []):
        eps, np_ = p.get('eps'), p.get('net_profit')
        if not eps or not np_ or eps == 0 or np_ / eps <= 0:
            continue
        shares = D(repr(np_)) / D(repr(eps))
        mcap = shares * D(repr(p['price']))
        row = {'code': p['code'], 'name': p['name'], 'industry': p.get('industry'),
               'revenue': D(repr(p['revenue'])), 'market_cap': mcap, 'net_income': D(repr(np_)),
               'book_equity': D(repr(p['bvps'])) * shares,
               'np_growth': (D(repr(p['np_yoy'])) / 100) if p.get('np_yoy') is not None else None,
               'price': p['price'], 'price_date': p.get('price_date'), 'shares': shares}
        IS, BS, CF = p.get('IS') or {}, p.get('BS') or {}, p.get('CF') or {}
        if IS and BS:
            g = lambda d, k: D(repr(d.get(k) or 0))
            ebit = g(IS, 'op_profit') + g(IS, 'fin_exp') - g(IS, 'invest_income') - g(IS, 'fv_gain')
            da = g(CF, 'fa_depr') + g(CF, 'rou_depr') + g(CF, 'ia_amort') + g(CF, 'lpe_amort')
            row['ebitda'] = ebit + da
            row['ebit'] = ebit
            debt = (g(BS, 'st_debt') + g(BS, 'noncur_due_liab') + g(BS, 'lt_debt') + g(BS, 'bonds') + g(BS, 'st_bonds')
                    + g(BS, 'lease_liab'))
            pref = g(BS, 'other_equity_instr')
            cashlike = (g(BS, 'cash') + g(BS, 'lt_equity_inv') + g(BS, 'trading_fa') + g(BS, 'other_equity_inv')
                        + g(BS, 'other_nc_fa') + g(BS, 'invest_property'))
            row['net_debt_like'] = debt + pref + g(BS, 'minority_equity') - cashlike
        out.append(row)
    return out


INDUSTRY_TAG = {  # 目标公司 → (行业提示词, 东财行业键) ；行业提示词用于路由（金融/周期），行业键用于选同行
    '600660': ('汽车零部件 重资产制造', '汽车零部件'), '603288': ('调味发酵品 消费', '调味发酵品Ⅱ'),
    '600276': ('化学制药 医药高研发', '化学制药'), '688111': ('软件开发 轻资产软件', '软件开发'),
    '601899': ('工业金属 周期资源 铜金', '工业金属'), '601088': ('煤炭开采 周期资源', '煤炭开采'),
    '600588': ('软件开发 亏损软件', '软件开发'), '000002': ('房地产开发 高杠杆', '房地产开发'),
    '600036': ('银行', '银行Ⅱ'),
}


def pro_inputs(code: str, valuation_date: str = '2026-10-09', root: Path | None = None):
    """为 valuation_pro.build_pro_model 准备 (facts, options)：事实、行情、基准、无风险利率、同行。"""
    from decimal import Decimal as D
    d = load_dossier(code, root)
    mk = load_market(root)
    px_date, px = last_price(d, valuation_date)
    rf_date, rf = [r for r in mk['cn10y'] if r[0] <= valuation_date][-1]
    tag, ind = INDUSTRY_TAG.get(code, (d.get('tag', ''), None))
    peers_path = Path(root or CORPUS) / 'peers' / f'{code}.json'
    cands = peers_to_candidates(json.loads(peers_path.read_text(encoding='utf-8'))) if peers_path.exists() else []
    options = {
        'company': d['name'], 'code': code, 'industry': tag, 'industry_key': ind, 'valuation_date': valuation_date,
        'price': px, 'price_date': px_date,
        'stock_prices': [r for r in d['prices'] if r[0] <= valuation_date],
        'benchmark_prices': [r for r in mk['csi300'] if r[0] <= valuation_date],
        'rf': {'value': D(repr(rf)) / 100, 'source': 'akshare bond_zh_us_rate 中国国债收益率10年', 'as_of': rf_date},
        'peer_candidates': cands, 'unit': '元', 'currency': 'CNY',
    }
    return dossier_to_facts(d), options
