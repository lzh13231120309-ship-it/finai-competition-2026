"""公告结构化提取的输出契约（字段名、类型、各事件类型的计分字段）。

字段命名沿用竞赛评分契约风格（tools/score_extraction.py 与《00-测试说明与评分标准》）：
- 股数单位“股”（后缀 _股），金额单位“元”（后缀 _元），比例为百分数值（6.25% 填 6.25，后缀 _百分）；
- 日期 YYYY-MM-DD；原文未披露一律 null（绝不编造）；
- 减持类变动股数为负数，增持/受让为正数。

事件类型（event_type）：
  pledge 新增质押（含补充质押，是否补充质押=是） / release 解除质押 / extension 质押展期
  holding 股东持股与累计质押（累计口径）
  bid 中标/中标候选人公示 / contract 重大合同、框架协议
  shareholding_change 增减持/权益变动前后持股 / equity_report 权益变动报告书封面
  freeze 司法冻结、轮候冻结、司法拍卖
文档族（family）：pledge | bid | contract | freeze | equity_report | shareholding_change | other
"""
from __future__ import annotations

# 字段类型：int=整数(股/元)  pct=百分数  date=ISO 日期  name=机构/人名  text=自由文本  methods=方式集合  cur=币种
FIELD_TYPES = {
    '股东名称': 'name', '质权人': 'name', '原质权人': 'name',
    '质押股数_股': 'int', '解除质押股数_股': 'int', '展期股数_股': 'int',
    '占所持比例_百分': 'pct', '占总股本比例_百分': 'pct',
    '质押起始日': 'date', '质押到期日': 'date', '解押日期': 'date', '原质押到期日': 'date', '展期后到期日': 'date',
    '质押用途': 'text', '是否补充质押': 'text',
    '持股数量_股': 'int', '持股比例_百分': 'pct', '累计质押股数_股': 'int',
    '累计占所持比例_百分': 'pct', '累计占总股本比例_百分': 'pct',
    '项目名称': 'text', '招标人': 'name', '中标人': 'name', '中标金额_元': 'int', '阶段': 'text', '工期_日历天': 'int',
    '合同名称': 'text', '甲方': 'name', '乙方': 'name', '合同金额': 'int', '币种': 'cur', '签订日期': 'date',
    '变动方式': 'methods', '变动股数_股': 'int', '变动比例_百分': 'pct', '变动期间起': 'date', '变动期间止': 'date',
    '成交均价_元': 'dec', '变动前持股_股': 'int', '变动前持股比例_百分': 'pct', '变动后持股_股': 'int',
    '变动后持股比例_百分': 'pct',
    '信息披露义务人': 'name', '上市公司名称': 'name', '股票代码': 'text', '权益变动性质': 'text', '签署日期': 'date',
    '冻结类型': 'text', '冻结股数_股': 'int', '起始日': 'date', '到期日': 'date', '冻结申请人': 'name',
    '总股本_股': 'int', '证券代码': 'text', '证券简称': 'text', '公告编号': 'text', '公告日期': 'date',
}

SCHEMAS = {
    'pledge': ['股东名称', '质押股数_股', '占所持比例_百分', '占总股本比例_百分', '质权人', '质押起始日',
               '质押到期日', '质押用途', '是否补充质押'],
    'release': ['股东名称', '解除质押股数_股', '占所持比例_百分', '占总股本比例_百分', '质权人', '质押起始日', '解押日期'],
    'extension': ['股东名称', '展期股数_股', '占所持比例_百分', '占总股本比例_百分', '质权人', '质押起始日',
                  '原质押到期日', '展期后到期日', '质押用途'],
    'holding': ['股东名称', '持股数量_股', '持股比例_百分', '累计质押股数_股', '累计占所持比例_百分', '累计占总股本比例_百分'],
    'bid': ['项目名称', '招标人', '中标人', '中标金额_元', '阶段', '工期_日历天'],
    'contract': ['合同名称', '甲方', '乙方', '合同金额', '币种', '签订日期'],
    'shareholding_change': ['股东名称', '变动方式', '变动股数_股', '变动比例_百分', '变动期间起', '变动期间止',
                            '成交均价_元', '变动前持股_股', '变动前持股比例_百分', '变动后持股_股', '变动后持股比例_百分'],
    'equity_report': ['信息披露义务人', '上市公司名称', '股票代码', '权益变动性质', '签署日期'],
    'freeze': ['股东名称', '冻结类型', '冻结股数_股', '占所持比例_百分', '占总股本比例_百分', '起始日', '到期日', '冻结申请人'],
}

# 记录配对时用作“身份键”的字段（先按它找对应记录）
NAME_KEYS = {
    'pledge': '股东名称', 'release': '股东名称', 'extension': '股东名称', 'holding': '股东名称',
    'bid': '中标人', 'contract': '合同名称', 'shareholding_change': '股东名称', 'equity_report': '信息披露义务人',
    'freeze': '股东名称',
}

FAMILIES = ('pledge', 'bid', 'contract', 'freeze', 'equity_report', 'shareholding_change', 'other')
EVENT_FAMILY = {'pledge': 'pledge', 'release': 'pledge', 'extension': 'pledge', 'holding': 'pledge',
                'bid': 'bid', 'contract': 'contract', 'freeze': 'freeze',
                'equity_report': 'equity_report', 'shareholding_change': 'shareholding_change'}
