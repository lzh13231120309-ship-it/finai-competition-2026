"""繁简归一、英文/HKIFRS 科目别名、英文单位与日期识别。

为什么必须有这一层：
A 股以外的大量真实材料是**繁体 + 英文双语**（H 股年报、红筹、B 股、外企），
而且往往按 IFRS/HKFRS 列报，科目名与 A 股准则差异很大。
只认简体中文的词典在这些材料上会提取到 0 条事实 —— 不是"算错了"，
而是"根本没认出来"，且失败是静默的，最容易被误判成模型能力不行。

实测样本：农夫山泉（9633.HK）2025 年报，扫描版、繁體中文/English 对照、
人民币千元列报，旧词典命中 0 条。

本模块零第三方依赖（应用是离线打包的，不能随便加包）：
- 繁简转换用 str.translate 表，覆盖财务报表实际会出现的字。
- 英文别名直接并入 financial_analysis.ALIASES。
- 单位与日期识别覆盖 RMB'000 / HK$ million / 人民幣千元 / 31 December 2025。
"""
from __future__ import annotations

import calendar
import re
from datetime import date

# ---------------------------------------------------------------- 繁 → 简

from .trad_to_simp_data import TRAD_TO_SIMP_FULL

# 领域覆盖层：叠在内置完整表之上，只改"按字面通用转换会和本词典对不上"的几个字。
_TRAD_OVERRIDES = {
    # 港式年报写「帳目／帳面值／應收帳款」，而 Mainland 简体与词典统一用「账」。
    # OpenCC 字表给的是「帳 → 帐」（对应帐篷的帐），在这个语境下是错的。
    '帳': '账',
}

# 曾经这里是一张手写的 179 字表，理由是"财务文本用字有限，不值得全集"。
# 事实证伪了它：漏掉「貿」之后，H 股年报里最常见的「貿易應收款項」整条
# 科目静默消失 —— 提取结果少一项，不报任何错，最难发现。
# 现在改为内置 OpenCC 全集（3109 字，纯数据、零依赖）。
_TRAD_TO_SIMP = {**TRAD_TO_SIMP_FULL, **_TRAD_OVERRIDES}

_TRANS_TABLE = str.maketrans(_TRAD_TO_SIMP)

# 全角括号/顿号等统一成半角，便于后续正则
_PUNCT_TABLE = str.maketrans({'（': '(', '）': ')', '，': ',', '．': '.', '：': ':', '－': '-'})


def to_simplified(text) -> str:
    """繁体转简体（只覆盖财务披露用字），并统一全角标点。"""
    s = str(text or '')
    if not s:
        return s
    return s.translate(_TRANS_TABLE).translate(_PUNCT_TABLE)


# ---------------------------------------------------------------- 双语标签切分

# 中英之间的空白必须一起吃掉：真实双语单元格是「貿易應收款項 Trade receivables」，
# 边界处是空格而不是字母，严格相邻的断言会一个都切不开（实测踩过）。
_SPLIT_RE = re.compile(r'(?<=[\u4e00-\u9fff])\s*(?=[A-Za-z])|(?<=[A-Za-z0-9\)])\s*(?=[\u4e00-\u9fff])')


def subject_candidates(text) -> list[str]:
    """给出一个标签的所有候选写法。

    双语报表会把中英文挤在同一格（如「貿易應收款項 Trade receivables」），
    整串既不是中文科目也不是英文科目。所以先试整串，再按中英边界切开分别试。
    """
    s = to_simplified(text).strip()
    if not s:
        return []
    out = [s]
    for part in _SPLIT_RE.split(s):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


# ---------------------------------------------------------------- 英文/HKIFRS 科目别名

EN_ALIASES = {
    'revenue': ['Revenue', 'Turnover', 'Total revenue', 'Operating revenue', 'Sales', 'Revenue from operations'],
    'cost_of_revenue': ['Cost of sales', 'Cost of revenue', 'Cost of goods sold', 'Cost of inventories sold'],
    'gross_profit': ['Gross profit', 'Gross profit margin amount'],
    'total_operating_cost': ['Total operating costs', 'Total operating expenses'],
    'net_profit': ['Net profit', 'Profit for the year', 'Profit for the period', 'Net income',
                   'Profit for the year and total comprehensive income'],
    'parent_net_profit': ['Profit attributable to owners of the Company', 'Profit attributable to owners of the parent',
                          'Profit attributable to shareholders of the Company',
                          'Profit attributable to the shareholders of the Company',
                          'Net profit attributable to shareholders', 'Profit attributable to equity holders of the Company'],
    'nonrecurring_adj_parent_profit': ['Profit attributable to owners of the Company excluding non-recurring items',
                                       'Recurring net profit attributable to shareholders'],
    'minority_profit': ['Profit attributable to non-controlling interests', 'Profit attributable to minority interests',
                        'Profit attributable to non-controlling shareholders'],
    'operating_profit': ['Operating profit', 'Profit from operations', 'Profit from operating activities'],
    'profit_before_tax': ['Profit before tax', 'Profit before taxation', 'Profit before income tax'],
    'income_tax': ['Income tax expense', 'Income tax', 'Taxation', 'Tax expense'],
    'selling_expenses': ['Selling and distribution expenses', 'Distribution expenses', 'Selling expenses',
                         'Selling and marketing expenses', 'Selling and distribution costs'],
    'administrative_expenses': ['Administrative expenses', 'General and administrative expenses',
                                'Administrative and general expenses'],
    'research_expenses': ['Research and development expenses', 'Research and development costs',
                          'R&D expenses', 'Research expenses'],
    'financial_expenses': ['Finance costs', 'Finance expenses', 'Financial expenses', 'Finance costs net'],
    'interest_expense': ['Interest expense', 'Interest on borrowings', 'Interest expenses'],
    'interest_income': ['Interest income', 'Bank interest income'],
    'taxes_surcharges': ['Taxes and surcharges', 'Taxes and other levies'],
    'investment_income': ['Investment income', 'Income from investments'],
    'other_income': ['Other income', 'Other gains', 'Other net income', 'Other income and gains'],
    'fair_value_gain': ['Fair value gains', 'Fair value gains net', 'Changes in fair value'],
    'financial_assets_fvtpl': ['Financial assets at fair value through profit or loss',
                               'Financial assets at FVTPL',
                               'Financial assets at fair value through profit and loss'],
    'credit_impairment': ['Impairment losses on financial assets', 'Credit impairment losses',
                          'Impairment losses under expected credit loss model'],
    'asset_impairment': ['Impairment losses', 'Asset impairment losses',
                         'Impairment losses on property plant and equipment',
                         'Impairment losses on goodwill'],
    'asset_disposal_gain': ['Gain on disposal of assets', 'Gains on disposal of property plant and equipment',
                            'Gain on disposal of subsidiaries'],
    'nonoperating_income': ['Non-operating income'],
    'nonoperating_expense': ['Non-operating expenses'],
    'assets': ['Total assets', 'Total assets of the Group'],
    'liabilities': ['Total liabilities', 'Total liabilities of the Group'],
    'equity': ['Total equity', 'Total equity of the Group', 'Total equity and liabilities'],
    'parent_equity': ['Equity attributable to owners of the Company',
                      'Total equity attributable to owners of the parent',
                      'Equity attributable to the shareholders of the Company',
                      'Equity attributable to equity holders of the Company'],
    'minority_equity': ['Non-controlling interests', 'Minority interests', 'Non-controlling shareholders'],
    'cash': ['Cash and cash equivalents', 'Cash and bank balances', 'Bank balances and cash',
             'Cash at bank and on hand', 'Cash and cash equivalents and restricted cash'],
    'receivables': ['Trade receivables', 'Accounts receivable', 'Trade receivables net',
                    'Trade and bills receivables', 'Trade receivables and bills receivable'],
    'notes_receivable': ['Bills receivable', 'Notes receivable', 'Trade bills receivable'],
    'receivables_financing': ['Receivables financing'],
    'other_receivables': ['Other receivables', 'Other receivables and prepayments',
                          # 跨行折成「Prepayments, other receivables / and other assets」后的完整形式
                          'Prepayments other receivables and other assets',
                          'Other receivables and other assets'],
    'inventory': ['Inventories', 'Inventory', 'Inventories net'],
    'prepayments': ['Prepayments', 'Prepaid expenses', 'Prepayments and other current assets',
                    'Prepayments deposits and other receivables'],
    'current_assets': ['Total current assets', 'Current assets'],
    'noncurrent_assets': ['Total non-current assets', 'Total non current assets', 'Non-current assets',
                          'Non current assets'],
    'current_liabilities': ['Total current liabilities', 'Current liabilities'],
    'noncurrent_liabilities': ['Total non-current liabilities', 'Total non current liabilities',
                               'Non-current liabilities', 'Non current liabilities'],
    'payables': ['Trade payables', 'Accounts payable', 'Trade and other payables',
                 # 注意单复数：港式年报用的是 "Trade and bills payables"（复数），
                 # 而旧词条写的是 "bills payable"（单数），差一个 s 就整行漏认。
                 'Trade and bills payables', 'Trade payables and bills payables',
                 'Trade payables and bills payable',
                 'Bills payable and trade payables', 'Trade and other payables and accruals'],
    'short_term_debt': ['Short-term borrowings', 'Short term borrowings', 'Short-term loans',
                        'Current bank borrowings', 'Bank borrowings current',
                        'Interest-bearing bank borrowings current',
                        'Interest-bearing borrowings current', 'Borrowings current'],
    'long_term_debt': ['Long-term borrowings', 'Long term borrowings', 'Long-term loans',
                       'Non-current bank borrowings', 'Bank borrowings non-current',
                       'Interest-bearing borrowings non-current', 'Borrowings non-current'],
    'borrowings': ['Interest-bearing borrowings', 'Interest bearing borrowings',
                   'Interest-bearing bank borrowings', 'Interest bearing bank borrowings',
                   'Bank borrowings', 'Borrowings'],
    'bonds_payable': ['Bonds payable', 'Corporate bonds payable'],
    'lease_liabilities': ['Lease liabilities', 'Finance lease liabilities'],
    'fixed_assets': ['Property plant and equipment', 'Property, plant and equipment', 'Fixed assets'],
    'intangibles': ['Intangible assets', 'Intangible assets other than goodwill'],
    'goodwill': ['Goodwill'],
    'contract_liabilities': ['Contract liabilities', 'Deferred revenue'],
    'retained_earnings': ['Retained earnings', 'Retained profits', 'Accumulated profits',
                          'Retained earnings accumulated losses'],
    'operating_cash_flow': ['Net cash generated from operating activities',
                            'Net cash flows from operating activities',
                            'Net cash generated from operations',
                            'Net cash provided by operating activities',
                            'Net cash inflow from operating activities'],
    'investing_cash_flow': ['Net cash used in investing activities',
                            'Net cash flows from investing activities',
                            'Net cash used in investing activities net',
                            'Net cash outflow from investing activities'],
    'financing_cash_flow': ['Net cash used in financing activities',
                            'Net cash flows from financing activities',
                            'Net cash generated from financing activities',
                            'Net cash inflow from financing activities'],
    'capex': ['Purchases of property plant and equipment', 'Purchase of property plant and equipment',
              'Purchases of property, plant and equipment', 'Capital expenditure',
              'Payments for property plant and equipment',
              'Purchases of property plant and equipment and intangible assets',
              # 港式/IFRS 标准写法带 "items of"：「Purchases of items of property, plant and equipment」。
              # 少这个词整条资本开支就认不出来 —— 而 capex 同时喂 FCFF 与 R_CAP_01，
              # 静默缺失的代价远大于多写一条别名。实测福布斯/农夫山泉年报踩过。
              'Purchases of items of property plant and equipment',
              'Purchase of items of property plant and equipment',
              'Additions to property plant and equipment',
              'Acquisition of property plant and equipment'],
    # 注意：不要把 'Cash generated from operations' 放进 cash_from_sales。
    # 港式的「經營所得現金 / Cash generated from operations」是"经营活动所得现金"
    # （税前、息前口径），不是"销售商品、提供劳务收到的现金"。混用会让
    # 收现比 = 经营所得现金 / 营业收入（实测约 0.49），触发 R_SETTLE_01 误报。
    'cash_from_sales': ['Cash received from sales of goods and rendering of services',
                        'Proceeds from sales of goods and services',
                        'Cash receipts from customers', 'Receipts from customers'],
    'cash_paid_suppliers': ['Cash paid for goods and services', 'Purchases of goods and services'],
    'cash_equivalents_start': ['Cash and cash equivalents at beginning of the year',
                               'Cash and cash equivalents at beginning of the period',
                               'Cash and cash equivalents at 1 January'],
    'cash_equivalents_end': ['Cash and cash equivalents at end of the year',
                             'Cash and cash equivalents at end of the period',
                             'Cash and cash equivalents at 31 December'],
    'cash_equivalents_change': ['Net increase in cash and cash equivalents',
                                'Net decrease in cash and cash equivalents',
                                'Net increase decrease in cash and cash equivalents'],
    'fx_cash_effect': ['Effect of foreign exchange rate changes', 'Exchange differences',
                       'Effect of exchange rate changes on cash and cash equivalents'],
    'depreciation': ['Depreciation of property plant and equipment', 'Depreciation'],
    'depreciation_rou_assets': ['Depreciation of right-of-use assets',
                                'Depreciation of right of use assets',
                                'Depreciation of leased assets'],
    'amortization': ['Amortisation of intangible assets', 'Amortization of intangible assets',
                     'Amortisation and depreciation'],
    'ebitda': ['EBITDA', 'Adjusted EBITDA'],
    'basic_eps': ['Basic earnings per share', 'Basic EPS', 'Earnings per share basic',
                  'Basic earnings per share attributable to ordinary equity holders of the parent'],
    'diluted_eps': ['Diluted earnings per share', 'Diluted EPS', 'Earnings per share diluted'],

    # ---- 港式/IFRS 资产负债表的单体科目 ----
    # 这批科目过去全部缺失。缺一条的代价并不对称：它们既是"小计推导总额"的输入，
    # 也是勾稽恒等式与减值/资本化类规则的输入。缺一条就可能让整页勾稽判不过，
    # 或者让相关规则静默失明 —— 而系统当时会报"该科目未披露"，
    # 与"披露了但没认出来"混为一谈，评审无法区分。
    # 实测农夫山泉 2025 年报「綜合財務狀況表」：這 13 个科目一个都没认出来。
    'right_of_use_assets': ['Right-of-use assets', 'Right of use assets', 'Right-of-use asset',
                            'Leased assets'],
    'deferred_tax_assets': ['Deferred tax assets', 'Deferred income tax assets'],
    'deferred_tax_liabilities': ['Deferred tax liabilities', 'Deferred income tax liabilities'],
    'other_noncurrent_assets': ['Other non-current assets', 'Other non current assets',
                                'Other long-term assets'],
    'other_payables': ['Other payables and accruals', 'Other payables', 'Accruals and other payables',
                       'Other payables and accrued expenses', 'Other payables and accrued liabilities'],
    'tax_payables': ['Tax payables', 'Taxes payable', 'Income tax payable',
                     'Current tax liabilities', 'Current tax payable'],
    'restricted_cash': ['Restricted cash', 'Restricted bank balances', 'Restricted cash and bank balances'],
    'pledged_deposits': ['Pledged deposits', 'Pledged bank deposits', 'Pledged time deposits'],
    'long_term_deposits': ['Long-term bank deposits', 'Long term bank deposits',
                           'Long-term time deposits', 'Term deposits'],
    'deferred_income': ['Deferred income', 'Deferred government grants', 'Government grants deferred'],
    'net_assets': ['Net assets'],
    'share_capital': ['Share capital', 'Issued share capital'],
    'reserves': ['Reserves'],
    'long_term_investments': ['Long-term investments', 'Interests in associates',
                              'Investments in associates', 'Investment in associates'],
    'deferred_expenses': ['Long-term deferred expenses', 'Deferred expenses'],
}

# 繁体写法补进中文词典（转简体后仍需能碰到，这里给的是转简后的形式）
HKIFRS_ZH_ALIASES = {
    'revenue': ['营业额', '收益', '总收入'],
    'cost_of_revenue': ['营业成本', '销售成本', '销售及服务成本'],
    'gross_profit': ['毛利', '毛利额'],
    'net_profit': ['年内溢利', '期内溢利', '本年度溢利', '溢利', '净利润', '年度利润'],
    'parent_net_profit': ['公司拥有人应占溢利', '本公司拥有人应占溢利', '母公司拥有人应占溢利',
                          '股东应占溢利', '股本持有人应占溢利', '本公司拥有人应占利润'],
    'minority_profit': ['非控股权益应占溢利', '少数股东应占溢利'],
    'operating_profit': ['经营利润', '经营活动所得利润'],
    'profit_before_tax': ['除税前溢利', '所得税前溢利', '税前利润'],
    'income_tax': ['所得税开支', '税项', '所得税费用'],
    'selling_expenses': ['销售及分销开支', '分销开支', '销售开支'],
    'administrative_expenses': ['行政开支', '管理费用'],
    'research_expenses': ['研发开支', '研究与开发开支', '研发费用'],
    'financial_expenses': ['融资成本', '财务费用', '融资开支'],
    'interest_expense': ['利息开支', '利息支出'],
    'interest_income': ['利息收入'],
    'other_income': ['其他收入', '其他收益及亏损'],
    'assets': ['资产总额', '总资产', '资产总值'],
    'liabilities': ['负债总额', '总负债', '负债总值'],
    'equity': ['权益总额', '总权益', '权益总值'],
    'parent_equity': ['母公司拥有人应占权益', '公司拥有人应占权益', '股东应占权益'],
    'minority_equity': ['非控股权益', '少数股东权益'],
    'cash': ['现金及现金等价物', '现金及银行结余', '银行结余及现金', '现金及现金等价物及受限制现金'],
    'receivables': ['贸易应收款项', '应收账款', '贸易及其他应收款项',
                    '贸易应收款项及应收票据', '应收票据及应收账款'],
    'other_receivables': ['其他应收款项', '预付款项、其他应收款项及其他资产',
                          '其他应收款项及其他资产'],
    'financial_assets_fvtpl': ['按公平值计入损益的金融资产', '以公允价值计量且其变动计入当期损益的金融资产'],
    'inventory': ['存货', '存货净额'],
    'current_assets': ['流动资产总额', '流动资产合计', '流动资产'],
    'noncurrent_assets': ['非流动资产总额', '非流动资产合计', '非流动资产'],
    'current_liabilities': ['流动负债总额', '流动负债合计', '流动负债'],
    'noncurrent_liabilities': ['非流动负债总额', '非流动负债合计', '非流动负债'],
    'payables': ['贸易应付款项', '应付账款', '贸易及其他应付款项',
                 # 港式资产负债表的固定写法是「貿易應付款項及應付票據」。
                 # 少「及应付票据」这四个字就整行认不出来，而应付账款是
                 # 现金循环周期与占款分析的核心科目。
                 '贸易应付款项及应付票据', '应付票据及应付账款',
                 '贸易及其他应付款项及应付票据'],
    'short_term_debt': ['短期借款', '短期银行借款', '计息银行借款即期部分', '银行借款即期部分'],
    'long_term_debt': ['长期借款', '长期银行借款', '计息银行借款非即期部分', '银行借款非即期部分'],
    'borrowings': ['計息借貸', '计息借贷', '計息銀行借貸', '计息银行借贷', '銀行借貸', '银行借贷'],
    'lease_liabilities': ['租賃負債', '租赁负债'],
    'fixed_assets': ['物业厂房及设备', '固定资产', '物业、厂房及设备'],
    'intangibles': ['无形资产'],
    'goodwill': ['商誉'],
    'contract_liabilities': ['合约负债', '合同负债'],
    'retained_earnings': ['保留盈利', '留存收益', '未分配利润'],
    'operating_cash_flow': ['经营活动所得现金净额', '经营活动产生的现金流量净额', '经营活动所得现金流量净额'],
    'investing_cash_flow': ['投资活动所用现金净额', '投资活动产生的现金流量净额'],
    'financing_cash_flow': ['融资活动所用现金净额', '筹资活动产生的现金流量净额', '融资活动所得现金净额'],
    # 港式现金流量表把资本开支写作「購買物業、廠房及設備項目」，
    # 比资产负债表的「物業、廠房及設備」多一个「項目」。归一化会去掉顿号与空格，
    # 但不会去掉"项目"，所以必须单独列一条，否则整条 capex 认不出来。
    'capex': ['购买物业厂房及设备', '购置物业厂房及设备', '购建固定资产',
              '购买物业厂房及设备项目', '购置物业厂房及设备项目',
              '购买物业厂房及设备及其他长期资产', '购买物业厂房及设备之付款'],
    'cash_equivalents_start': ['年初现金及现金等价物', '期初现金及现金等价物'],
    'cash_equivalents_end': ['年末现金及现金等价物', '期末现金及现金等价物'],
    'cash_equivalents_change': ['现金及现金等价物增加净额', '现金及现金等价物净增加额'],
    'depreciation': ['物业厂房及设备折旧', '固定资产折旧'],
    # 港式现金流量表的「調整項」区把折旧拆成两行：物業廠房及設備折舊 + 使用權資產折舊。
    # 两行都映射到 depreciation 时会构成 duplicate_conflict，**两条一起**被降级，
    # 于是现金流量表分析连折旧都拿不到，而且报告只显示"该科目存在冲突值"。
    # 它们本来就是两个不同科目，分开登记才是如实描述。
    'depreciation_rou_assets': ['使用权资产折旧'],
    'amortization': ['无形资产摊销'],
    'basic_eps': ['每股基本盈利', '基本每股收益'],
    'diluted_eps': ['每股摊薄盈利', '稀释每股收益'],

    # ---- 港式/IFRS 资产负债表的单体科目（同 EN_ALIASES 的同名代码）----
    'right_of_use_assets': ['使用权资产'],
    'deferred_tax_assets': ['递延税项资产', '递延所得税资产'],
    'deferred_tax_liabilities': ['递延税项负债', '递延所得税负债'],
    'other_noncurrent_assets': ['其他非流动资产'],
    'other_payables': ['其他应付款项及应计费用', '其他应付款及应计费用', '其他应付款项',
                       '其他应付款及应计开支'],
    'tax_payables': ['应付税项', '应付税费', '应交税费', '应交税金', '应付所得税'],
    'restricted_cash': ['受限资金', '受限制资金', '受限制现金', '使用受限的银行存款'],
    'pledged_deposits': ['质押存款', '已质押存款', '抵押存款'],
    'long_term_deposits': ['长期银行存款', '长期定期存款', '定期存款'],
    'deferred_income': ['递延收益', '递延政府补助'],
    'net_assets': ['资产净额'],
    'share_capital': ['股本'],
    'reserves': ['储备'],
    'long_term_investments': ['长期股权投资', '于联营公司的权益', '联营公司投资', '长期投资'],
    'deferred_expenses': ['长期待摊费用', '递延费用'],
}


# ---------------------------------------------------------------- 单位与币种

_UNIT_SCALES = {
    '元': 1, '千元': 1000, '万元': 10000, '百万元': 1000000, '亿元': 100000000,
    '千港元': 1000, '百万港元': 1000000, '亿港元': 100000000,
    '千美元': 1000, '百万美元': 1000000,
}

# (正则, 币种, 单位名, 倍率)
_UNIT_PATTERNS = [
    (re.compile(r"(?:RMB|CNY)\s*['’]?\s*0{3}\b", re.I), 'CNY', '千元', 1000),
    (re.compile(r"(?:RMB|CNY)\s*(?:in\s+)?(?:thousand|'000)", re.I), 'CNY', '千元', 1000),
    (re.compile(r"(?:RMB|CNY)\s*(?:in\s+)?million", re.I), 'CNY', '百万元', 1000000),
    (re.compile(r"(?:HK\$|HKD)\s*['’]?\s*0{3}\b", re.I), 'HKD', '千港元', 1000),
    (re.compile(r"(?:HK\$|HKD)\s*(?:in\s+)?(?:thousand|'000)", re.I), 'HKD', '千港元', 1000),
    (re.compile(r"(?:HK\$|HKD)\s*(?:in\s+)?million", re.I), 'HKD', '百万港元', 1000000),
    (re.compile(r"(?:US\$|USD)\s*['’]?\s*0{3}\b", re.I), 'USD', '千美元', 1000),
    (re.compile(r"(?:US\$|USD)\s*(?:in\s+)?million", re.I), 'USD', '百万美元', 1000000),
    (re.compile(r"人民币\s*(千元|万元|百万元|亿元)"), 'CNY', None, None),
    (re.compile(r"(?:港元|港币)\s*(千元|万元|百万元|亿元)"), 'HKD', None, None),
    # (?<!编制) 是必须的：「编制单位：元通有限公司」里"单位：元"会被误判成"单位：元"，
    # 于是把一张元表判成"元"、或把万元表判成元表。这是踩过的坑。
    (re.compile(r"(?<!编制)单位\s*[:：]\s*(?:人民币|港元|港币|美元)?\s*(亿元|百万元|千元|万元|元)"), None, None, None),
    (re.compile(r"人民币?百万元"), 'CNY', '百万元', 1000000),
]

_CURRENCY_PATTERNS = [
    (re.compile(r'人民[币幣]|RMB|Renminbi|\bCNY\b', re.I), 'CNY'),
    (re.compile(r'港元|港[币幣]|HK\$|\bHKD\b|Hong Kong dollars', re.I), 'HKD'),
    (re.compile(r'美元|US\$|\bUSD\b|United States dollars', re.I), 'USD'),
    (re.compile(r'欧元|EUR|Euro', re.I), 'EUR'),
]


def detect_currency_i18n(text) -> set[str]:
    s = to_simplified(text)
    return {code for pattern, code in _CURRENCY_PATTERNS if pattern.search(s)}


def detect_unit_i18n(text) -> tuple[str | None, int]:
    """返回 (单位名, 倍率)。识别不了就返回 (None, 1)。"""
    s = to_simplified(text)
    for pattern, currency, unit, scale in _UNIT_PATTERNS:
        m = pattern.search(s)
        if not m:
            continue
        if unit is None:
            group = m.group(1) if m.groups() else None
            if group is None:
                continue
            unit = group
            scale = _UNIT_SCALES.get(group)
            if scale is None:
                continue
        return unit, scale
    return None, 1


# ---------------------------------------------------------------- 英文日期

_MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}

_EN_DATE_RE = re.compile(r'(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?\s*,?\s*(20\d{2})')
_EN_FY_RE = re.compile(r'\bFY\s?' + r'(20\d{2})', re.I)


def parse_english_period(text) -> tuple[str, str] | None:
    """识别 '31 December 2025' / 'year ended 31 December 2025' / 'FY2025'。

    返回 (ISO 结束日, 期间类型或 '')；识别不了返回 None 交给原逻辑。
    """
    s = str(text or '')
    marks: list[date] = []
    for m in _EN_DATE_RE.finditer(s):
        day, mon, year = int(m.group(1)), _MONTHS.get(m.group(2).lower()), int(m.group(3))
        if not mon or not 1 <= day <= 31:
            continue
        try:
            marks.append(date(year, mon, min(day, calendar.monthrange(year, mon)[1])))
        except ValueError:
            continue
    if marks:
        low = s.lower()
        if len(marks) >= 2 and marks[0].day == 1 and marks[0].month == 1 and marks[0] < marks[-1]:
            end = marks[-1]
            if end.month == 12 and end.day == 31:
                return end.isoformat(), 'annual'
            return end.isoformat(), 'ytd'
        end = marks[-1]
        # 「是不是一段期间」要由期间词决定，而不是由日期本身决定：
        # 资产负债表表头就是光秃秃的「31 December 2025」（时点），
        # 利润表表头才是「year ended 31 December 2025」（期间）。
        # 光看日期把两者都判成 annual，会让时点数据被当成年度流量用。
        explicit_period = bool(re.search(
            r'\b(?:year|period|months?|quarter|weeks?)s?\s+(?:then\s+)?ended\b'
            r'|\bfor\s+the\s+(?:year|period|quarter|months?)\b'
            r'|\bended\b|\bcumulative\b', low))
        if end.month == 12 and end.day == 31:
            return end.isoformat(), 'annual' if explicit_period else ''
        if end.month in (6, 9) and end.day == calendar.monthrange(end.year, end.month)[1]:
            return end.isoformat(), 'ytd' if explicit_period else ''
        return end.isoformat(), ''
    m = _EN_FY_RE.search(s)
    if m:
        year = int(m.group(1))
        low = s.lower()
        return f'{year}-12-31', '' if ('as at' in low or 'at ' in low) else 'annual'
    return None


# ---------------------------------------------------------------- 口径

_SCOPE_PATTERNS = [
    (re.compile(r'合并(?:资产负债表|利润表|现金流量表|财务报表|报表|口径)'), 'consolidated'),
    (re.compile(r'(?:母公司|母企业)(?:资产负债表|利润表|现金流量表|财务报表|报表|口径)'), 'parent'),
    (re.compile(r'consolidated\s+(?:statement|financial|balance|income|cash)', re.I), 'consolidated'),
    (re.compile(r'\bconsolidated\b', re.I), 'consolidated'),
    (re.compile(r'\bthe\s+Company\s+(?:statement|balance|financial|only)', re.I), 'parent'),
    (re.compile(r'\bcompany\s+level\b', re.I), 'parent'),
]


def detect_scope_i18n(text) -> set[str]:
    s = to_simplified(text)
    return {scope for pattern, scope in _SCOPE_PATTERNS if pattern.search(s)}


_PARENT_TITLE = re.compile(
    r'本公司(?:财务|资产|负债|状况|权益)[^\n]{0,8}表'
    r'|statement\s+of\s+financial\s+position\s+of\s+the\s+company'
    r'|company\s+statement\s+of'
    r'|statement\s+of\s+financial\s+position\s+of\s+[^\n]{0,20}\bcompany\b', re.I)


def detect_parent_statement(text) -> bool:
    """识别「本公司/母公司单体报表」标题。

    这份年报里既有合并报表（第 117 页）也有公司单体报表（第 226 页），
    两者科目名完全相同，口径判错会把单体数字当成合并数字用。
    """
    return bool(_PARENT_TITLE.search(to_simplified(text)))


# 报表类型。同一个科目在不同报表里可能含义不同，最典型的是「现金」：
# 财务状况表的「現金及銀行結餘」是期末库存现金，
# 现金流量表的「現金及現金等價物」是流量表口径的期末余额，
# 两者数值本来就不相等。不区分报表类型就会把它们当成"同一科目的冲突值"，
# 结果把资产负债表的正确现金一起打成不可用 —— 实测农夫山泉踩过，
# 最终净债务只能退回拿母公司口径的现金去算。
_STATEMENT_PATTERNS = [
    ('cash_flow', re.compile(r'現金流量表|现金流量表|Cash\s+Flow\s+Statement|cash\s+flows?', re.I)),
    ('equity_changes', re.compile(r'權益變動表|权益变动表|Changes\s+in\s+Equity', re.I)),
    ('income_statement', re.compile(
        r'損益表|收益表|利润表|損益及其他全面收益|综合收益表|全面收益表|'
        r'Income\s+Statement|Statement\s+of\s+Profit\s+or\s+Loss|Comprehensive\s+Income', re.I)),
    ('balance_sheet', re.compile(
        r'財務狀況表|财务状况表|资产负债?表|Balance\s+Sheet|Statement\s+of\s+Financial\s+Position', re.I)),
    ('notes', re.compile(r'財務報表附註|财务报表附注|Notes\s+to\s+the\s+Financial\s+Statements', re.I)),
]


def detect_statement(text) -> str:
    """判定表格/页面属于哪张报表，判不出返回 'unknown'。"""
    s = to_simplified(text)
    for name, pattern in _STATEMENT_PATTERNS:
        if pattern.search(s):
            return name
    return 'unknown'


_CURRENCY_MARKERS = [
    (re.compile(r'人民[币幣]|RMB|Renminbi'), 'CNY'),
    (re.compile(r'港元|港[币幣]|HK\$|HKD'), 'HKD'),
    (re.compile(r'美元|US\$|USD'), 'USD'),
    (re.compile(r'欧元|EUR'), 'EUR'),
]


def dominant_currency(text) -> str | None:
    """从整篇文本里判出计价币种。

    单一年度报告通篇只用一个记账本位币。某些附注页不会重复「人民币千元」，
    逐表识别会留下一堆 unknown；用全篇出现频次的主导币种兜底更稳。
    只有主导币种至少是次席的 3 倍才下结论，否则返回 None（宁可 unknown）。
    """
    s = to_simplified(text)
    counts = sorted((len(p.findall(s)), code) for p, code in _CURRENCY_MARKERS)
    counts.reverse()
    if not counts or counts[0][0] == 0:
        return None
    if len(counts) > 1 and counts[0][0] < counts[1][0] * 3:
        return None
    return counts[0][1]
