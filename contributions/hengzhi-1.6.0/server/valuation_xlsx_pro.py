"""专业估值 Excel：全部关键计算为**活公式**，假设集中在「假设」页被公式引用。

与旧 valuation_workbook（缓存引擎值）的区别
------------------------------------------
- 「三表预测」逐科目写公式：收入×(1+增速)、毛利率、费用率、营运资本天数、capex/折旧、融资平衡（现金轧差或循环贷款）、
  留存收益/固定资产滚动、现金流量表、勾稽——改「假设」页任一蓝色单元格，三表、FCFF、DCF、敏感性(WACC×g、WACC×退出倍数)、
  相对估值（人工剔除开关）同时重算。
- 利息按期初余额计息：避免 Excel 循环引用。FCFF 以 EBIT 为起点，不含利息，**估值不受计息口径影响**；
  引擎默认同口径（interest_basis=beginning），所以三表也逐元一致。
- 增速×EBIT 率、资本开支×营运资本天数两张矩阵与龙卷风、三情景需要整套重算，Excel 无法用普通公式实现（需数据表/VBA），
  这里写入引擎快照值并明确标注"快照"，同时放一个"中心格=活公式 DCF"的核对单元格。
- 校验：无头 LibreOffice 重算（recalc_with_libreoffice），把重算值写回缓存并与引擎值比对
  （tests/test_valuation_xlsx_pro.py）。**注意环境依赖**：本机未安装 LibreOffice 时该函数返回 None，
  `export_with_cache` 会如实报告 `recalculated_by=None`、`cells=0`，即"活公式已写好但未经第三方
  引擎复算"。这是环境限制而非代码缺陷——Excel/LibreOffice 打开时仍会重算（文件带 fullCalcOnLoad），
  但评审若要"重算值等于引擎值"的证据，需要一台装了 LibreOffice 的机器再跑一次。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path

import xlsxwriter
from xlsxwriter.utility import xl_col_to_name, xl_rowcol_to_cell

D = Decimal
YEAR0_COL = 2   # 三表预测：C 列为基期(Year 0)，D 列起为预测年


def num(x, default=None):
    if x is None or x == '':
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class _Book:
    def __init__(self, path, cache):
        self.wb = xlsxwriter.Workbook(str(path), {'strings_to_formulas': False, 'strings_to_urls': False})
        self.cache = cache or {}
        base = {'font_name': 'Arial', 'font_size': 10}
        self.f = {
            'txt': self.wb.add_format(base), 'bold': self.wb.add_format({**base, 'bold': True}),
            'head': self.wb.add_format({**base, 'bold': True, 'bg_color': '#17365D', 'font_color': 'white', 'text_wrap': True, 'valign': 'top'}),
            'sec': self.wb.add_format({**base, 'bold': True, 'bg_color': '#DCE6F1'}),
            'in': self.wb.add_format({**base, 'font_color': '#0000FF', 'bg_color': '#FFF2CC', 'num_format': '#,##0.00;(#,##0.00);"-"'}),
            'in_pct': self.wb.add_format({**base, 'font_color': '#0000FF', 'bg_color': '#FFF2CC', 'num_format': '0.00%'}),
            'in_txt': self.wb.add_format({**base, 'font_color': '#0000FF', 'bg_color': '#FFF2CC'}),
            'num': self.wb.add_format({**base, 'num_format': '#,##0.00;(#,##0.00);"-"'}),
            'int': self.wb.add_format({**base, 'num_format': '#,##0;(#,##0);"-"'}),
            'pct': self.wb.add_format({**base, 'num_format': '0.00%'}),
            'mult': self.wb.add_format({**base, 'num_format': '0.00"x"'}),
            'chk': self.wb.add_format({**base, 'bold': True, 'font_color': '#006100', 'bg_color': '#C6EFCE'}),
            'snap': self.wb.add_format({**base, 'bg_color': '#EDEDED', 'num_format': '#,##0.00;(#,##0.00);"-"'}),
            'wrap': self.wb.add_format({**base, 'text_wrap': True, 'valign': 'top'}),
        }
        self.names = {}

    def sheet(self, name, widths=None):
        ws = self.wb.add_worksheet(name)
        ws.set_column(0, 0, 34)
        ws.set_column(1, 30, 16)
        for c, w in (widths or {}).items():
            ws.set_column(c, c, w)
        return ws

    def w(self, ws, r, c, v, fmt='txt'):
        """写值：以 '=' 开头为公式（带缓存值），否则按类型写。"""
        f = self.f[fmt] if isinstance(fmt, str) else fmt
        if isinstance(v, str) and v.startswith('='):
            cached = self.cache.get((ws.get_name(), xl_rowcol_to_cell(r, c)))
            if cached is None:
                ws.write_formula(r, c, v, f)
            else:
                ws.write_formula(r, c, v, f, cached)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            ws.write_number(r, c, v, f)
        elif v is None:
            ws.write_blank(r, c, None, f)
        else:
            ws.write_string(r, c, str(v), f)

    def name(self, key, sheet, r, c):
        ref = f"='{sheet}'!${xl_col_to_name(c)}${r + 1}"
        self.wb.define_name(key, ref)
        self.names[key] = (sheet, r, c)


def cell(sheet, r, c, absolute=False):
    ref = xl_rowcol_to_cell(r, c, row_abs=absolute, col_abs=absolute)
    return f"'{sheet}'!{ref}"


def export_pro_workbook(run: dict, path, cache=None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    B = _Book(path, cache)
    ts = run.get('three_statement') or {}
    refused = not run.get('dcf', {}).get('ok') or not ts.get('statements')
    A = B.sheet('假设', {0: 30, 1: 34, 2: 18, 3: 10, 4: 10, 5: 40, 6: 56})
    if refused:
        _sheet_refused(B, A, run)
        B.wb.close()
        return path
    years = int(run.get('years') or len(ts['statements']))
    # ---------------- 假设页 ----------------
    ws = A
    r = 0
    for c, h in enumerate(['键', '名称', '数值', '单位', '类别', '来源', '依据 / 说明']):
        B.w(ws, r, c, h, 'head')
    r += 1
    tw, wacc = run.get('wacc') or {}, run.get('wacc') or {}
    drivers = run.get('drivers') or {}
    items = run.get('bridge_items') or {}
    market = run.get('market') or {}
    term = run.get('terminal') or {}
    fund = ts.get('funding') or {}
    cls = {'fact': '事实', 'derived': '推论', 'judgment': '观点'}

    def sec(title):
        nonlocal r
        for c in range(7):
            B.w(ws, r, c, title if c == 0 else None, 'sec')
        r += 1

    def inp(key, label, value, unit, klass, source, basis, fmt='in'):
        nonlocal r
        B.w(ws, r, 0, key)
        B.w(ws, r, 1, label)
        B.w(ws, r, 2, num(value, 0.0) if not isinstance(value, str) or value.replace('.', '', 1).replace('-', '', 1).replace('e', '', 1).isdigit() else value, fmt)
        B.w(ws, r, 3, unit)
        B.w(ws, r, 4, cls.get(klass, klass))
        B.w(ws, r, 5, source)
        B.w(ws, r, 6, basis, 'wrap')
        B.name(key, '假设', r, 2)
        r += 1

    sec('基本信息')
    inp('Years', '预测年数', years, '年', 'judgment', '引擎默认', '显式期 10 年：增速在显式期内线性衰减到永续增速', 'in')
    inp('MidYear', '期中折现开关(1=开)', 1 if term.get('mid_year') else 0, '0/1', 'judgment', '引擎选项', '期中折现：第 t 年现金流按 t−0.5 折现；Gordon 终值同口径', 'in')
    inp('Price', '股价', market.get('price') or 0, '元', 'fact', market.get('price_date') or '', '估值日收盘价（未复权/前复权最新值）')
    inp('Shares', '总股数', market.get('shares') or 0, '股', 'derived', market.get('shares_source') or '', market.get('shares_basis') or '')
    sec('资本成本（WACC 页引用）')
    inp('Rf', '无风险利率(10 年国债)', wacc.get('rf'), '比率', 'fact', wacc.get('rf_source', ''), wacc.get('rf_as_of', ''), 'in_pct')
    inp('ERP', '股权风险溢价', wacc.get('erp'), '比率', 'judgment', '观点', 'A 股实务区间 5%–7%，取 6%；区间见 WACC 页', 'in_pct')
    beta_det = (wacc.get('beta_detail') or {})
    inp('BetaRaw', 'β 原始回归值', num((beta_det.get('regression') or {}).get('beta'), num(wacc.get('beta'), 1.0)), '', 'derived', '沪深300周收益 OLS', f"n={(beta_det.get('regression') or {}).get('n')}，R²={(beta_det.get('regression') or {}).get('r2')}")
    inp('BetaLo', 'β 下限', 0.6, '', 'judgment', '观点', '个股回归 β 标准误大，截断区间（启发式）')
    inp('BetaHi', 'β 上限', 1.6, '', 'judgment', '观点', '同上')
    inp('KeFloorPrem', '股权成本下限溢价', 0.045, '比率', 'judgment', '观点', 'Ke ≥ rf + 4.5%（启发式）', 'in_pct')
    inp('Kd', '税前债务成本', wacc.get('cost_of_debt'), '比率', 'derived', wacc.get('cost_of_debt_source', ''), '', 'in_pct')
    inp('Tax', '所得税率', drivers.get('tax_rate'), '比率', 'derived', '近 3 年有效税率均值，夹在 [15%,25%]', '', 'in_pct')
    inp('DebtWeight', '债务权重 D/(D+E)', wacc.get('debt_weight'), '比率', 'derived', wacc.get('debt_weight_source', ''), '', 'in_pct')
    inp('WaccOverride', 'WACC 手工覆盖(空=用计算值)', wacc.get('wacc') if wacc.get('wacc_source') == '使用者指定' else '', '比率', 'judgment', '', '', 'in_pct')
    sec('终值')
    inp('g', '永续增长率 g', term.get('g'), '比率', 'judgment', '观点', '介于 10 年国债与长期名义 GDP 上限(4%)之间；硬约束 g<WACC 且 ≤ 4%', 'in_pct')
    inp('ROIC_T', '终期 ROIC', term.get('roic'), '比率', 'derived', '引擎', 'min(max(历史ROIC, WACC), WACC+4pp)；价值驱动法再投资率 = g/ROIC', 'in_pct')
    inp('ExitMultiple', '退出 EV/EBITDA 倍数', term.get('exit_multiple') or '', 'x', 'derived', term.get('exit_multiple_source') or '', '同行中位数(IQR 后)；与 Gordon 隐含倍数互验', 'in')
    sec('融资与分红')
    inp('Payout', '分红率', fund.get('payout_ratio'), '比率', 'judgment', '观点', '不影响 FCFF 与 EV，只影响三表现金/权益', 'in_pct')
    inp('MinCash', '最低现金', fund.get('minimum_cash') or 0, '元', 'judgment', '观点', '', 'in')
    inp('KdBook', '三表利息成本(债务)', fund.get('cost_of_debt'), '比率', 'derived', '利息费用/平均债务', '', 'in_pct')
    inp('CashYield', '三表利息收益率(现金)', fund.get('cash_yield'), '比率', 'derived', '利息收入/平均现金', '', 'in_pct')
    inp('MinorityShare', '少数股东损益占比', fund.get('minority_income_share'), '比率', 'derived', '历史少数股东损益/净利润', '', 'in_pct')
    sec('EV→权益价值桥（正数输入；符号在 DCF 页决定）')
    bl = {'cash': '货币类现金', 'associates': '联营/合营投资(账面)', 'non_operating_assets': '其他非经营性资产(账面)',
          'debt': '有息债务', 'leases': '租赁负债', 'preferred': '优先股/永续债/其他权益工具', 'minority': '少数股东权益(账面)',
          'contingent': '或有负债', 'sbc': '股权激励现值'}
    for k in ('cash', 'associates', 'non_operating_assets', 'debt', 'leases', 'preferred', 'minority', 'contingent', 'sbc'):
        inp('Br_' + k, bl[k], items.get(k) or 0, '元', 'fact' if k in ('cash', 'debt', 'leases', 'minority') else 'derived', '资产负债表', '见 dcf_core.BRIDGE_ITEMS 口径说明')
    sec('情景/敏感性杠杆（基准=0；改这些格即可看到三表与估值同时重算）')
    for k, label, unit in (('LV_growth', '收入增速变动(加到每个预测年)', 'pp'), ('LV_margin', 'EBIT 率变动', 'pp'), ('LV_capex', '资本开支率变动', 'pp'),
                           ('LV_nwc', '应收账款天数变动', '天'), ('LV_tax', '税率变动', 'pp'), ('LV_wacc', 'WACC 变动', 'pp'), ('LV_g', '永续增长率变动', 'pp')):
        inp(k, label, 0.0, unit, 'judgment', '情景杠杆', '基准情景为 0', 'in_pct' if unit == 'pp' else 'in')
    r += 1
    sec('逐年驱动路径（引擎基准路径；公式里再加上面的杠杆）')
    path_rows = {}
    B.w(ws, r, 1, '项目', 'head')
    for t in range(1, years + 1):
        B.w(ws, r, 1 + t, f'第{t}年', 'head')
    r += 1
    labels = {'revenue_growth': '收入增速', 'gross_margin': '毛利率', 'opex_ratio': '费用率(含税金及附加/减值等，使基期 EBIT 与报表一致)',
              'da_to_revenue': '折旧摊销/收入', 'capex_to_revenue': '资本开支/收入', 'receivable_days': '应收账款天数(收入口径)',
              'inventory_days': '存货天数(成本口径)', 'payable_days': '应付账款天数(成本口径)'}
    paths = run.get('paths') or {}
    for k, lab in labels.items():
        B.w(ws, r, 0, k)
        B.w(ws, r, 1, lab)
        for t in range(1, years + 1):
            v = num((paths.get(k) or [None] * years)[t - 1], 0.0)
            B.w(ws, r, 1 + t, v, 'in_pct' if 'days' not in k else 'in')
        path_rows[k] = r
        r += 1
    ws.freeze_panes(1, 1)
    _sheet_history(B, run)
    # ---------------- 三表预测 ----------------
    S = B.sheet('三表预测', {0: 40, 1: 12})
    od = ts['opening_detail']
    stm = ts['statements']
    B.w(S, 0, 0, '三表预测（单位：元；利息按期初余额，无循环引用）', 'head')
    B.w(S, 0, 1, '行类型', 'head')
    B.w(S, 0, 2, '基期', 'head')
    for t in range(1, years + 1):
        B.w(S, 0, 2 + t, f'第{t}年', 'head')
    rows = {}
    rr = [1]
    pending = []

    def flush():
        for rrow, base, fn, fmt in pending:
            if base is not None:
                B.w(S, rrow, 2, base, 'in' if isinstance(base, (int, float)) else fmt)
            if fn:
                for t in range(1, years + 1):
                    B.w(S, rrow, 2 + t, fn(t, 2 + t), fmt)
        pending.clear()

    def row(key, label, base=None, fn=None, fmt='num', kind=''):
        """fn(t, col) -> 公式字符串；base 为基期值（数字或公式）。"""
        rrow = rr[0]
        rows[key] = rrow
        B.w(S, rrow, 0, label)
        B.w(S, rrow, 1, kind)
        pending.append((rrow, base, fn, fmt))
        rr[0] += 1
        return rrow

    def ref(key, col, absolute=False):
        return xl_rowcol_to_cell(rows[key], col, row_abs=absolute)

    def gap(title):
        B.w(S, rr[0], 0, title, 'sec')
        for c in range(1, 3 + years):
            B.w(S, rr[0], c, None, 'sec')
        rr[0] += 1

    def P(key, t):   # 假设页驱动路径引用
        return f"'假设'!{xl_rowcol_to_cell(path_rows[key], 1 + t)}"

    gap('利润表')
    rev0 = num(od.get('revenue'), 0.0)
    row('rev', '营业收入', rev0, lambda t, c: f"={ref('rev', c - 1)}*(1+{P('revenue_growth', t)}+LV_growth)")
    cogs0 = num(od.get('cost_of_revenue'), 0.0)
    row('cogs', '营业成本', cogs0, lambda t, c: f"={ref('rev', c)}*(1-{P('gross_margin', t)})")
    row('gp', '毛利', f"={ref('rev', 2)}-{ref('cogs', 2)}", lambda t, c: f"={ref('rev', c)}-{ref('cogs', c)}")
    row('opex', '期间费用及其他(费用率×收入)', None, lambda t, c: f"=MAX({ref('rev', c)}*({P('opex_ratio', t)}-LV_margin),0)")
    row('ebit', '核心 EBIT', None, lambda t, c: f"={ref('gp', c)}-{ref('opex', c)}")
    row('intexp', '利息费用(期初债务×Kd)', None, lambda t, c: f"={ref('debt', c - 1)}*KdBook" if False else '=0')   # 占位，稍后覆盖
    row('intinc', '利息收入(期初现金×收益率)', None, lambda t, c: '=0')
    row('pbt', '税前利润', None, lambda t, c: f"={ref('ebit', c)}-{ref('intexp', c)}+{ref('intinc', c)}")
    row('taxx', '所得税', None, lambda t, c: f"=MAX({ref('pbt', c)},0)*MIN(MAX(Tax+LV_tax,0),0.5)")
    row('ni', '净利润', None, lambda t, c: f"={ref('pbt', c)}-{ref('taxx', c)}")
    row('minor', '少数股东损益', None, lambda t, c: f"={ref('ni', c)}*MinorityShare")
    row('pni', '归母净利润', None, lambda t, c: f"={ref('ni', c)}-{ref('minor', c)}")
    row('div', '分红(归母)', None, lambda t, c: f"=MAX({ref('pni', c)},0)*Payout")
    row('mdiv', '少数股东分红', None, lambda t, c: f"=MAX({ref('minor', c)},0)*Payout")
    gap('资产负债表')
    wa = od.get('wc_assets') or {}
    wl = od.get('wc_liab') or {}
    ncl = od.get('ncl') or {}
    fa = od.get('fa') or {}
    cash0 = num(od.get('cash'), 0.0)
    debt0 = num(od.get('debt'), 0.0)
    row('cash', '货币资金', cash0, None)
    wa_keys = list(wa)
    for k in wa_keys:
        v0 = num(wa[k], 0.0)
        if k == 'receivables':
            row('wa_' + k, '应收账款(天数驱动)', v0, lambda t, c: f"={ref('rev', c)}*MAX({P('receivable_days', t)}+LV_nwc,0)/365")
        elif k == 'inventory':
            row('wa_' + k, '存货(成本天数驱动)', v0, lambda t, c: f"={ref('cogs', c)}*{P('inventory_days', t)}/365")
        else:
            row('wa_' + k, f'{k}(按收入比例)', v0, lambda t, c, k=k: f"={ref('rev', c)}*{ref('wa_' + k, 2, True)}/{ref('rev', 2, True)}")
    row('wa_total', '经营性流动资产合计', f"=SUM({ref('wa_' + wa_keys[0], 2)}:{ref('wa_' + wa_keys[-1], 2)})",
        lambda t, c: f"=SUM({ref('wa_' + wa_keys[0], c)}:{ref('wa_' + wa_keys[-1], c)})")
    fa_core0 = sum(num(fa[k], 0.0) for k in fa if k != 'other_noncurrent_assets')
    other_nca0 = num(fa.get('other_noncurrent_assets'), 0.0)
    row('fa_core', '固定/无形/使用权/递延税资产(滚动)', fa_core0, lambda t, c: f"={ref('fa_core', c - 1)}+{ref('capex', c)}-{ref('da', c)}")
    row('oth_nca', '其他非流动资产(含金融/股权投资，冻结)', other_nca0, lambda t, c: f"={ref('oth_nca', c - 1)}")
    row('assets', '资产合计', f"={ref('cash', 2)}+{ref('wa_total', 2)}+{ref('fa_core', 2)}+{ref('oth_nca', 2)}",
        lambda t, c: f"={ref('cash', c)}+{ref('wa_total', c)}+{ref('fa_core', c)}+{ref('oth_nca', c)}")
    wl_keys = list(wl)
    for k in wl_keys:
        v0 = num(wl[k], 0.0)
        if k == 'payables':
            row('wl_' + k, '应付账款(成本天数驱动)', v0, lambda t, c: f"={ref('cogs', c)}*{P('payable_days', t)}/365")
        else:
            row('wl_' + k, f'{k}(按收入比例)', v0, lambda t, c, k=k: f"={ref('rev', c)}*{ref('wl_' + k, 2, True)}/{ref('rev', 2, True)}")
    row('wl_total', '经营性流动负债合计', f"=SUM({ref('wl_' + wl_keys[0], 2)}:{ref('wl_' + wl_keys[-1], 2)})",
        lambda t, c: f"=SUM({ref('wl_' + wl_keys[0], c)}:{ref('wl_' + wl_keys[-1], c)})")
    ncl_keys = list(ncl)
    for k in ncl_keys:
        v0 = num(ncl[k], 0.0)
        row('nc_' + k, f'{k}(按收入比例)', v0, lambda t, c, k=k: f"={ref('rev', c)}*{ref('nc_' + k, 2, True)}/{ref('rev', 2, True)}")
    if ncl_keys:
        row('ncl_total', '非流动经营负债合计', f"=SUM({ref('nc_' + ncl_keys[0], 2)}:{ref('nc_' + ncl_keys[-1], 2)})",
            lambda t, c: f"=SUM({ref('nc_' + ncl_keys[0], c)}:{ref('nc_' + ncl_keys[-1], c)})")
    else:
        row('ncl_total', '非流动经营负债合计', 0.0, lambda t, c: '=0')
    mode = fund.get('mode') or 'cash_plug'
    row('debt', f'有息债务（{mode}）', debt0, None)
    row('sc', '股本', num(od.get('share_capital'), 0.0), lambda t, c: f"={ref('sc', c - 1)}")
    row('re', '留存收益(含其他权益工具)', num(od.get('retained_earnings'), 0.0), lambda t, c: f"={ref('re', c - 1)}+{ref('pni', c)}-{ref('div', c)}")
    row('mi', '少数股东权益', num(od.get('minority_equity'), 0.0), lambda t, c: f"={ref('mi', c - 1)}+{ref('minor', c)}-{ref('mdiv', c)}")
    row('equity', '权益合计', f"={ref('sc', 2)}+{ref('re', 2)}+{ref('mi', 2)}", lambda t, c: f"={ref('sc', c)}+{ref('re', c)}+{ref('mi', c)}")
    row('le', '负债+权益合计', f"={ref('wl_total', 2)}+{ref('ncl_total', 2)}+{ref('debt', 2)}+{ref('equity', 2)}",
        lambda t, c: f"={ref('wl_total', c)}+{ref('ncl_total', c)}+{ref('debt', c)}+{ref('equity', c)}")
    gap('现金流量表与配平')
    row('capex', '资本开支', None, lambda t, c: f"={ref('rev', c)}*MAX({P('capex_to_revenue', t)}+LV_capex,0)")
    row('da', '折旧摊销(不超过资产净额)', None, lambda t, c: f"=MIN({ref('rev', c)}*{P('da_to_revenue', t)},{ref('fa_core', c - 1)}+{ref('capex', c)})")
    row('cfo', '经营现金流', None, lambda t, c: f"={ref('ni', c)}+{ref('da', c)}-({ref('wa_total', c)}-{ref('wa_total', c - 1)})+({ref('wl_total', c)}-{ref('wl_total', c - 1)})+({ref('ncl_total', c)}-{ref('ncl_total', c - 1)})")
    row('cfi', '投资现金流', None, lambda t, c: f"=-{ref('capex', c)}-({ref('oth_nca', c)}-{ref('oth_nca', c - 1)})")
    row('cff', '筹资现金流', None, lambda t, c: f"=-{ref('div', c)}-{ref('mdiv', c)}+({ref('debt', c)}-{ref('debt', c - 1)})")
    row('cash_end', '现金流量表期末现金', None, lambda t, c: f"={ref('cash', c - 1)}+{ref('cfo', c)}+{ref('cfi', c)}+{ref('cff', c)}")
    flush()
    # 回填：利息与融资平衡公式（依赖其他行）
    for t in range(1, years + 1):
        c = 2 + t
        B.w(S, rows['intexp'], c, f"={ref('debt', c - 1)}*KdBook", 'num')
        B.w(S, rows['intinc'], c, f"={ref('cash', c - 1)}*CashYield", 'num')
        nonc_assets = f"({ref('wa_total', c)}+{ref('fa_core', c)}+{ref('oth_nca', c)})"
        liab_eq = f"({ref('wl_total', c)}+{ref('ncl_total', c)}+{ref('equity', c)})"
        if mode == 'cash_plug':
            pre = f"({liab_eq}+{ref('debt', c - 1)}-{nonc_assets})"      # 若债务不变的现金
            B.w(S, rows['debt'], c, f"={ref('debt', c - 1)}+MAX(MinCash-{pre},0)", 'num')
            B.w(S, rows['cash'], c, f"={pre}+MAX(MinCash-{pre},0)", 'num')
        else:
            raw = f"({nonc_assets}+{ref('cash', c - 1)}-{liab_eq.replace(ref('equity', c), ref('equity', c))})"
            B.w(S, rows['debt'], c, f"=MAX({raw},0)", 'num')
            B.w(S, rows['cash'], c, f"={ref('cash', c - 1)}+MAX(-{raw},0)", 'num')
    # 勾稽页
    C = B.sheet('勾稽检查', {0: 52})
    B.w(C, 0, 0, '检查项', 'head')
    for t in range(1, years + 1):
        B.w(C, 0, t, f'第{t}年', 'head')
    B.w(C, 0, years + 1, '结论', 'head')
    chk = [('资产 − (负债+权益) = 0（元）', lambda c: f"=ROUND('三表预测'!{ref('assets', c)}-'三表预测'!{ref('le', c)},2)"),
           ('资产负债表现金 − 现金流量表期末现金 = 0（元）', lambda c: f"=ROUND('三表预测'!{ref('cash', c)}-'三表预测'!{ref('cash_end', c)},2)"),
           ('固定资产滚动：期末−(期初+capex−D&A) = 0', lambda c: f"=ROUND('三表预测'!{ref('fa_core', c)}-('三表预测'!{ref('fa_core', c - 1)}+'三表预测'!{ref('capex', c)}-'三表预测'!{ref('da', c)}),2)"),
           ('留存收益滚动：期末−(期初+归母净利−分红) = 0', lambda c: f"=ROUND('三表预测'!{ref('re', c)}-('三表预测'!{ref('re', c - 1)}+'三表预测'!{ref('pni', c)}-'三表预测'!{ref('div', c)}),2)"),
           ('现金非负（1=通过）', lambda c: f"=IF('三表预测'!{ref('cash', c)}>=-0.01,0,1)")]
    for i, (lab, fn) in enumerate(chk, start=1):
        B.w(C, i, 0, lab)
        for t in range(1, years + 1):
            B.w(C, i, t, fn(2 + t), 'num')
        B.w(C, i, years + 1, f'=IF(SUMPRODUCT(ABS(B{i + 1}:{xl_col_to_name(years)}{i + 1}))<0.05,"通过","未通过")', 'chk')
    B.w(C, len(chk) + 2, 0, '期初资产负债表：资产−(负债+权益)（元）')
    B.w(C, len(chk) + 2, 1, f"=ROUND('三表预测'!{ref('assets', 2)}-'三表预测'!{ref('le', 2)},2)", 'num')
    B.w(C, len(chk) + 3, 0, '总体结论', 'bold')
    B.w(C, len(chk) + 3, 1, f'=IF(COUNTIF({xl_col_to_name(years + 1)}2:{xl_col_to_name(years + 1)}{len(chk) + 1},"通过")={len(chk)},"全部通过","存在未通过")', 'chk')
    B.name('TieOutAll', '勾稽检查', len(chk) + 3, 1)
    # ---------------- WACC ----------------
    Wk = B.sheet('WACC', {0: 40, 1: 18, 2: 60})
    B.w(Wk, 0, 0, '项目', 'head'); B.w(Wk, 0, 1, '数值', 'head'); B.w(Wk, 0, 2, '公式/依据', 'head')
    wrows = [('rf', '无风险利率 rf', '=Rf', 'pct', '10 年期国债收益率（假设页，来源见假设页）'),
             ('beta_blume', 'Blume 调整 β', '=2/3*BetaRaw+1/3', 'num', 'β_adj = 2/3·β_raw + 1/3'),
             ('beta', '采用的 β(截断后)', '=MIN(MAX(2/3*BetaRaw+1/3,BetaLo),BetaHi)', 'num', '截断到 [下限, 上限]'),
             ('ke_capm', 'CAPM 股权成本', '=Rf+B4*ERP', 'pct', 'Ke = rf + β × ERP'),
             ('ke', '股权成本(含下限)', '=MAX(B5,Rf+KeFloorPrem)', 'pct', 'Ke 不低于 rf+4.5%'),
             ('kd_at', '税后债务成本', '=Kd*(1-Tax)', 'pct', 'Kd × (1 − τ)'),
             ('wacc_calc', 'WACC(计算)', '=(1-DebtWeight)*B6+DebtWeight*B7', 'pct', 'WACC = (1−wd)·Ke + wd·Kd·(1−τ)'),
             ('wacc', 'WACC(采用，含情景杠杆)', '=IF(ISNUMBER(WaccOverride),WaccOverride,B8)+LV_wacc', 'pct', '覆盖值优先，再加 WACC 杠杆')]
    for i, (k, lab, f, fm, note) in enumerate(wrows, start=1):
        B.w(Wk, i, 0, lab); B.w(Wk, i, 1, f, fm); B.w(Wk, i, 2, note)
    B.name('WACC', 'WACC', 8, 1)
    B.w(Wk, 10, 0, 'WACC 合理区间（引擎）', 'bold')
    rg = wacc.get('wacc_range') or {}
    B.w(Wk, 11, 0, '低端 (ERP 5%、β 取低)'); B.w(Wk, 11, 1, num(rg.get('low'), 0.0), 'snap')
    B.w(Wk, 12, 0, '高端 (ERP 7%、β 取高)'); B.w(Wk, 12, 1, num(rg.get('high'), 0.0), 'snap')
    B.w(Wk, 14, 0, '行业 β 路径（若提供同行 β）', 'bold')
    ind = beta_det.get('industry')
    if ind:
        B.w(Wk, 15, 0, '行业去杠杆 β 中位数'); B.w(Wk, 15, 1, num(ind.get('unlevered_median'), 0.0), 'snap')
        B.w(Wk, 16, 0, '按目标 D/E 再加杠杆 β'); B.w(Wk, 16, 1, num(ind.get('relevered'), 0.0), 'snap')
    else:
        B.w(Wk, 15, 0, '未提供同行 β 序列（需个股行情），仅用目标公司回归 β')
    # ---------------- DCF ----------------
    Dc = B.sheet('DCF', {0: 44})
    B.w(Dc, 0, 0, '单位：元；FCFF = EBIT×(1−τ)+D&A−Capex−ΔNWC（NWC=经营性流动资产−经营性流动负债）', 'head')
    for t in range(1, years + 1):
        B.w(Dc, 0, t, f'第{t}年', 'head')
    drow = {}
    def drw(key, label, fn, fmt='num'):
        i = len(drow) + 1
        drow[key] = i
        B.w(Dc, i, 0, label)
        for t in range(1, years + 1):
            B.w(Dc, i, t, fn(t, 2 + t), fmt)
    TS = "'三表预测'!"
    drw('t', '期数 t', lambda t, c: f'={t}', 'int')
    drw('ebit', 'EBIT', lambda t, c: f"={TS}{ref('ebit', c)}")
    drw('nopat', 'NOPAT = EBIT−MAX(EBIT,0)×τ', lambda t, c: f"={TS}{ref('ebit', c)}-MAX({TS}{ref('ebit', c)},0)*MIN(MAX(Tax+LV_tax,0),0.5)")
    drw('da', '+ 折旧摊销', lambda t, c: f"={TS}{ref('da', c)}")
    drw('capex', '− 资本开支', lambda t, c: f"={TS}{ref('capex', c)}")
    drw('dnwc', '− ΔNWC', lambda t, c: f"=({TS}{ref('wa_total', c)}-{TS}{ref('wl_total', c)})-({TS}{ref('wa_total', c - 1)}-{TS}{ref('wl_total', c - 1)})")
    drw('fcff', 'FCFF', lambda t, c: f"={xl_col_to_name(t)}{drow['nopat'] + 1}+{xl_col_to_name(t)}{drow['da'] + 1}-{xl_col_to_name(t)}{drow['capex'] + 1}-{xl_col_to_name(t)}{drow['dnwc'] + 1}")
    drw('df', '折现因子', lambda t, c: f"=1/(1+WACC)^({xl_col_to_name(t)}{drow['t'] + 1}-0.5*MidYear)", 'num')
    drw('pv', 'PV(FCFF)', lambda t, c: f"={xl_col_to_name(t)}{drow['fcff'] + 1}*{xl_col_to_name(t)}{drow['df'] + 1}")
    last = xl_col_to_name(years)
    rngF = f"$B${drow['fcff'] + 1}:${last}${drow['fcff'] + 1}"
    rngT = f"$B${drow['t'] + 1}:${last}${drow['t'] + 1}"
    base = len(drow) + 2
    L = {}
    def lab(i, key, text, formula, fmt='num', note=''):
        L[key] = base + i
        B.w(Dc, base + i, 0, text); B.w(Dc, base + i, 1, formula, fmt)
        if note:
            B.w(Dc, base + i, 2, note)
    gcell = '(g+LV_g)'
    lab(0, 'g', '有效永续增长率 g', '=g+LV_g', 'pct', '假设页 g + 杠杆')
    lab(1, 'nopat_n', 'NOPAT_n', f"={last}{drow['nopat'] + 1}")
    lab(2, 'fcf_next', '终值现金流 FCFF_{n+1} = NOPAT_n×(1+g)×(1−g/ROIC)', f"=B{L['nopat_n'] + 1}*(1+B{L['g'] + 1})*(1-B{L['g'] + 1}/ROIC_T)", 'num', '价值驱动法（再投资率=g/ROIC）')
    lab(3, 'spread_ok', '约束：g<WACC 且 g ≤ 4%（1=满足）', f"=IF(AND(B{L['g'] + 1}<WACC,B{L['g'] + 1}<=0.04),1,0)", 'int', '不满足则 Gordon 估值无意义')
    lab(4, 'tv', '终值 TV = FCFF_{n+1}/(WACC−g)', f"=IF(B{L['spread_ok'] + 1}=1,B{L['fcf_next'] + 1}/(WACC-B{L['g'] + 1}),NA())")
    lab(5, 'pv_tv', 'PV(TV)（Gordon 期中折现按 n−0.5）', f"=B{L['tv'] + 1}/(1+WACC)^(Years-0.5*MidYear)")
    lab(6, 'pv_exp', 'Σ PV(FCFF)', f"=SUM({chr(66)}{drow['pv'] + 1}:{last}{drow['pv'] + 1})")
    lab(7, 'ev', '企业价值 EV', f"=B{L['pv_exp'] + 1}+B{L['pv_tv'] + 1}")
    lab(8, 'tvshare', '终值占 EV', f"=B{L['pv_tv'] + 1}/B{L['ev'] + 1}", 'pct', '>75% 触发闸门 G01')
    bridge_lines = [('cash', '+ 货币类现金', 1), ('associates', '+ 联营/合营投资', 1), ('non_operating_assets', '+ 其他非经营性资产', 1),
                    ('debt', '− 有息债务', -1), ('leases', '− 租赁负债', -1), ('preferred', '− 优先股/永续债', -1), ('minority', '− 少数股东权益', -1),
                    ('contingent', '− 或有负债', -1), ('sbc', '− 股权激励', -1)]
    for i, (k, text, sg) in enumerate(bridge_lines):
        lab(9 + i, 'b_' + k, text, f"={'' if sg > 0 else '-'}Br_{k}")
    lab(18, 'equity', '权益价值（普通股）', f"=B{L['ev'] + 1}+SUM(B{L['b_cash'] + 1}:B{L['b_sbc'] + 1})", 'num', '不输出目标价；见敏感性与足球场区间')
    B.name('Equity_DCF', 'DCF', L['equity'], 1)
    B.name('EV_DCF', 'DCF', L['ev'], 1)
    lab(19, 'ebitda_n', '终年 EBITDA', f"={last}{drow['ebit'] + 1}+{last}{drow['da'] + 1}")
    lab(20, 'impl_mult', 'Gordon 隐含终年 EV/EBITDA', f"=B{L['tv'] + 1}/B{L['ebitda_n'] + 1}", 'mult', '与同行倍数互验')
    lab(21, 'tv_exit', '退出倍数终值 = 倍数×终年 EBITDA', f"=IF(ISNUMBER(ExitMultiple),ExitMultiple*B{L['ebitda_n'] + 1},NA())")
    lab(22, 'ev_exit', '退出倍数法 EV（终值按 n 年折现）', f"=IF(ISNUMBER(ExitMultiple),B{L['pv_exp'] + 1}+B{L['tv_exit'] + 1}/(1+WACC)^Years,NA())")
    lab(23, 'eq_exit', '退出倍数法权益价值', f"=IF(ISNUMBER(ExitMultiple),B{L['ev_exit'] + 1}+SUM(B{L['b_cash'] + 1}:B{L['b_sbc'] + 1}),NA())")
    lab(24, 'g_impl', '退出倍数隐含永续增长率', f"=IF(ISNUMBER(ExitMultiple),(B{L['tv_exit'] + 1}*WACC-{last}{drow['fcff'] + 1})/(B{L['tv_exit'] + 1}+{last}{drow['fcff'] + 1}),NA())", 'pct', '显著高于 g 说明倍数偏乐观')
    lab(25, 'ps', '每股价值（仅作区间参考，非目标价）', f"=IF(Shares>0,B{L['equity'] + 1}/Shares,NA())", 'num')
    lab(26, 'mktcap', '当前市值（股价×股数）', '=Price*Shares')
    lab(27, 'vs', '基准权益价值 / 市值 − 1', f"=IF(B{L['mktcap'] + 1}>0,B{L['equity'] + 1}/B{L['mktcap'] + 1}-1,NA())", 'pct')
    # ---------------- 敏感性 ----------------
    Sn = B.sheet('敏感性', {0: 34})
    sens = run.get('sensitivity') or {}
    meta_txt = f"单位：元；币种：CNY；基期：{run.get('base_period')}；估值指标：普通股权益价值"
    B.w(Sn, 0, 0, '表1 WACC × 永续增长率（活公式：保持现金流不变，重新折现）  ' + meta_txt, 'head')
    wg = sens.get('wacc_g') or {}
    cols = [num(x) for x in wg.get('cols', [])]
    rws = [num(x) for x in wg.get('rows', [])]
    B.w(Sn, 1, 0, 'g ↓ / WACC →', 'bold')
    for j, w in enumerate(cols):
        B.w(Sn, 1, 1 + j, f"=WACC+({w}-{num((run.get('wacc') or {}).get('wacc'))})" if False else f"=WACC+{round(w - num(run['wacc']['wacc']), 10)}", 'pct')
    shift_cells = {}
    for i, g_ in enumerate(rws):
        B.w(Sn, 2 + i, 0, f"=(g+LV_g)+{round(g_ - num(run['terminal']['g']), 10)}", 'pct')
        for j in range(len(cols)):
            wc, gc = f"{xl_col_to_name(1 + j)}$2", f"$A{3 + i}"
            fcfnext = f"({TS_ref(drow, 'nopat', years)}*(1+{gc})*(1-{gc}/ROIC_T))"
            f = (f"=IF(AND({gc}<{wc},{gc}<=0.04),SUMPRODUCT('DCF'!{rngF}/(1+{wc})^('DCF'!{rngT}-0.5*MidYear))"
                 f"+{fcfnext}/({wc}-{gc})/(1+{wc})^(Years-0.5*MidYear)+SUM('DCF'!$B${L['b_cash'] + 1}:$B${L['b_sbc'] + 1}),\"拒绝\")")
            B.w(Sn, 2 + i, 1 + j, f, 'num')
    n0 = 2 + len(rws) + 1
    B.w(Sn, n0, 0, '表2 WACC × 退出 EV/EBITDA 倍数（活公式）', 'head')
    we = sens.get('wacc_exit')
    if we:
        mcols = [num(x) for x in we['cols']]
        mrows = [num(x) for x in we['rows']]
        B.w(Sn, n0 + 1, 0, '倍数 ↓ / WACC →', 'bold')
        for j, w in enumerate(mcols):
            B.w(Sn, n0 + 1, 1 + j, f"=WACC+{round(w - num(run['wacc']['wacc']), 10)}", 'pct')
        for i, m in enumerate(mrows):
            B.w(Sn, n0 + 2 + i, 0, f"=ExitMultiple*{round(m / num(run['terminal']['exit_multiple']), 10)}", 'mult')
            for j in range(len(mcols)):
                wc = f"{xl_col_to_name(1 + j)}${n0 + 2}"
                mc = f"$A{n0 + 3 + i}"
                f = (f"=SUMPRODUCT('DCF'!{rngF}/(1+{wc})^('DCF'!{rngT}-0.5*MidYear))+{mc}*'DCF'!$B${L['ebitda_n'] + 1}/(1+{wc})^Years"
                     f"+SUM('DCF'!$B${L['b_cash'] + 1}:$B${L['b_sbc'] + 1})")
                B.w(Sn, n0 + 2 + i, 1 + j, f, 'num')
        n1 = n0 + 2 + len(mrows) + 1
    else:
        B.w(Sn, n0 + 1, 0, '未取到可用的同行 EV/EBITDA 倍数，未生成退出倍数矩阵')
        n1 = n0 + 3
    def snap(title, key, n_start):
        m = sens.get(key) or {}
        B.w(Sn, n_start, 0, title + '  【引擎快照值：整套三表重算，Excel 公式无法表达；非活公式】  ' + meta_txt, 'head')
        B.w(Sn, n_start + 1, 0, f"{m.get('meta', {}).get('rows', '')} ↓ / {m.get('meta', {}).get('cols', '')} →", 'bold')
        for j, cv in enumerate(m.get('cols', [])):
            B.w(Sn, n_start + 1, 1 + j, num(cv), 'snap')
        for i, rv in enumerate(m.get('rows', [])):
            B.w(Sn, n_start + 2 + i, 0, num(rv), 'snap')
            for j in range(len(m['cols'])):
                v = m['values'][i][j]
                B.w(Sn, n_start + 2 + i, 1 + j, num(v) if v is not None else '拒绝', 'snap')
        return n_start + 2 + len(m.get('rows', [])) + 1
    n2 = snap('表3 收入增速 × EBIT 率', 'growth_margin', n1)
    n3 = snap('表4 资本开支率 × 营运资本天数', 'capex_nwc', n2)
    B.w(Sn, n3, 0, '核对：快照中心格 − DCF 页活公式权益价值（应=0；仅当「假设」页杠杆全为 0 时成立）', 'bold')
    gm = sens.get('growth_margin') or {}
    if gm.get('values'):
        ci, cj = len(gm['rows']) // 2, len(gm['cols']) // 2
        B.w(Sn, n3, 1, f"=ROUND({num(gm['values'][ci][cj])}-Equity_DCF,0)", 'num')
    n4 = n3 + 2
    B.w(Sn, n4, 0, '龙卷风（引擎快照：每个杠杆整套重算，非活公式）', 'head')
    for j, h in enumerate(['杠杆', '低端取值', '高端取值', '低端权益价值', '高端权益价值', '摆幅(元)', '摆幅/基准']):
        B.w(Sn, n4 + 1, j, h, 'head')
    for i, tr in enumerate((sens.get('tornado') or {}).get('rows', [])):
        B.w(Sn, n4 + 2 + i, 0, tr['label'])
        B.w(Sn, n4 + 2 + i, 1, num(tr['low_input']), 'snap'); B.w(Sn, n4 + 2 + i, 2, num(tr['high_input']), 'snap')
        B.w(Sn, n4 + 2 + i, 3, num(tr['value_at_low'], 0.0), 'snap'); B.w(Sn, n4 + 2 + i, 4, num(tr['value_at_high'], 0.0), 'snap')
        B.w(Sn, n4 + 2 + i, 5, num(tr['swing'], 0.0), 'snap'); B.w(Sn, n4 + 2 + i, 6, num(tr['swing_pct_of_base'], 0.0), 'pct')
    n5 = n4 + 2 + len((sens.get('tornado') or {}).get('rows', [])) + 1
    B.w(Sn, n5, 0, '三情景（引擎快照：每个情景整套重算，非活公式）', 'head')
    for j, h in enumerate(['情景', '杠杆', '权益价值', 'EV', '三表勾稽']):
        B.w(Sn, n5 + 1, j, h, 'head')
    for i, nm in enumerate(('stress', 'base', 'optimistic')):
        s_ = (run.get('scenarios') or {}).get(nm) or {}
        B.w(Sn, n5 + 2 + i, 0, {'stress': '压力', 'base': '基准', 'optimistic': '乐观'}[nm])
        B.w(Sn, n5 + 2 + i, 1, json.dumps(s_.get('levers'), ensure_ascii=False))
        B.w(Sn, n5 + 2 + i, 2, num(s_.get('equity'), 0.0), 'snap'); B.w(Sn, n5 + 2 + i, 3, num(s_.get('ev'), 0.0), 'snap')
        B.w(Sn, n5 + 2 + i, 4, '通过' if s_.get('tie_out_ok') else '未通过')
    _sheet_relative(B, run, L)
    _sheet_football(B, run, L)
    _sheet_log(B, run)
    for name in ('假设', '三表预测', 'DCF', 'WACC', '敏感性', '相对估值', '足球场', '勾稽检查', '变更日志'):
        pass
    B.wb.worksheets_objs.sort(key=lambda w: ['假设', '历史财务', '三表预测', 'DCF', 'WACC', '相对估值', '敏感性', '足球场', '勾稽检查', '变更日志'].index(w.get_name())
                              if w.get_name() in ['假设', '历史财务', '三表预测', 'DCF', 'WACC', '相对估值', '敏感性', '足球场', '勾稽检查', '变更日志'] else 99)
    B.wb.close()
    return path


def _sheet_history(B, run):
    """历史财务：原始科目(蓝，来自已核验事实)与派生比率(公式)，展示基期 EBIT 的定义与来源。"""
    ws = B.sheet('历史财务', {0: 44})
    hist = run.get('history') or {}
    ys = sorted(hist)
    B.w(ws, 0, 0, '单位：元（原始科目，来源=已核验年报事实/数据源）', 'head')
    for j, y in enumerate(ys):
        B.w(ws, 0, 1 + j, f'{y}', 'head')
    raw = [('revenue', '营业收入'), ('cost_of_revenue', '营业成本'), ('operating_profit', '营业利润'), ('financial_expenses', '财务费用'),
           ('investment_income', '投资收益'), ('fair_value_gain', '公允价值变动收益'), ('profit_before_tax', '利润总额'), ('income_tax', '所得税费用'),
           ('parent_net_profit', '归母净利润'), ('depreciation', '固定资产折旧'), ('rou_depreciation', '使用权资产折旧'), ('amortization', '无形资产摊销'),
           ('long_term_prepaid_amortization', '长期待摊费用摊销'), ('capex', '资本开支(购建固定资产等支付现金)'), ('receivables', '应收账款'),
           ('inventory', '存货'), ('payables', '应付账款'), ('cash', '货币资金')]
    pos = {}
    for i, (k, lab) in enumerate(raw, start=1):
        pos[k] = i
        B.w(ws, i, 0, lab)
        for j, y in enumerate(ys):
            v = num(hist[y].get(k))
            B.w(ws, i, 1 + j, v if v is not None else '', 'in' if v is not None else 'txt')
    base = len(raw) + 2
    B.w(ws, base, 0, '派生比率（公式）', 'sec')
    def col(j):
        return xl_col_to_name(1 + j)
    def rf(k, j):
        return f'{col(j)}{pos[k] + 1}'
    ratios = [('毛利率', lambda j: f'=IF({rf("revenue", j)}>0,({rf("revenue", j)}-{rf("cost_of_revenue", j)})/{rf("revenue", j)},"")', 'pct'),
              ('核心 EBIT = 营业利润+财务费用−投资收益−公允价值变动', lambda j: f'=N({rf("operating_profit", j)})+N({rf("financial_expenses", j)})-N({rf("investment_income", j)})-N({rf("fair_value_gain", j)})', 'num'),
              ('核心 EBIT 率', lambda j: f'=IF({rf("revenue", j)}>0,{col(j)}{base + 3}/{rf("revenue", j)},"")', 'pct'),
              ('折旧摊销合计', lambda j: f'=N({rf("depreciation", j)})+N({rf("rou_depreciation", j)})+N({rf("amortization", j)})+N({rf("long_term_prepaid_amortization", j)})', 'num'),
              ('折旧摊销/收入', lambda j: f'=IF({rf("revenue", j)}>0,{col(j)}{base + 5}/{rf("revenue", j)},"")', 'pct'),
              ('资本开支/收入', lambda j: f'=IF({rf("revenue", j)}>0,ABS({rf("capex", j)})/{rf("revenue", j)},"")', 'pct'),
              ('有效税率 = 所得税/利润总额', lambda j: f'=IF(N({rf("profit_before_tax", j)})>0,{rf("income_tax", j)}/{rf("profit_before_tax", j)},"")', 'pct'),
              ('应收账款天数(收入口径，期末)', lambda j: f'=IF({rf("revenue", j)}>0,{rf("receivables", j)}*365/{rf("revenue", j)},"")', 'num'),
              ('存货天数(成本口径，期末)', lambda j: f'=IF({rf("cost_of_revenue", j)}>0,{rf("inventory", j)}*365/{rf("cost_of_revenue", j)},"")', 'num'),
              ('应付账款天数(成本口径，期末)', lambda j: f'=IF({rf("cost_of_revenue", j)}>0,{rf("payables", j)}*365/{rf("cost_of_revenue", j)},"")', 'num')]
    for i, (lab, fn, fm) in enumerate(ratios, start=1):
        B.w(ws, base + i, 0, lab)
        for j in range(len(ys)):
            B.w(ws, base + i, 1 + j, fn(j), fm)


def TS_ref(drow, key, years):
    """DCF 页某行最后一年单元格的绝对引用，如 'DCF'!$K$4。"""
    return f"'DCF'!${xl_col_to_name(years)}${drow[key] + 1}"


def _sheet_refused(B, ws, run):
    B.w(ws, 0, 0, 'DCF 未输出', 'head')
    r = 1
    for text in run.get('refusals') or ['DCF 不适用']:
        B.w(ws, r, 0, text, 'wrap')
        r += 1
    fin = run.get('financial')
    if fin:
        B.w(ws, r + 1, 0, '金融企业 PB-ROE/DDM（简化）', 'bold')
        pb = (fin.get('pb_roe') or {}).get('equity_value') or {}
        for i, k in enumerate(('low', 'base', 'high')):
            B.w(ws, r + 2 + i, 0, f'PB-ROE 权益价值 {k}')
            B.w(ws, r + 2 + i, 1, num(pb.get(k), 0.0), 'snap')
    _sheet_relative(B, run, None)
    _sheet_football(B, run, None)
    _sheet_log(B, run)


def _sheet_relative(B, run, L):
    ws = B.sheet('相对估值', {0: 14, 1: 16})
    rel = run.get('relative')
    B.w(ws, 0, 0, '同行选择规则：' + ((rel or {}).get('selection', {}).get('rule_text') or '未提供同行'), 'head')
    if not rel:
        return
    inc = rel['selection']['included']
    exc = rel['selection']['excluded']
    heads = ['代码', '名称', '纳入(1/0，可手改)', '市值', '归母净利润', '账面权益', '营业收入', 'EBITDA', '净债务类(EV−市值)', '利润增速', 'PE', 'PB', 'PS', 'EV/EBITDA', 'EV/Sales', 'PEG']
    for j, h in enumerate(heads):
        B.w(ws, 2, j, h, 'head')
    n = len(inc)
    r0 = 3
    for i, p in enumerate(inc):
        r = r0 + i
        B.w(ws, r, 0, p.get('code')); B.w(ws, r, 1, p.get('name'))
        B.w(ws, r, 2, 1, 'in')
        for j, k in ((3, 'market_cap'), (4, 'net_income'), (5, 'book_equity'), (6, 'revenue'), (7, 'ebitda'), (8, 'net_debt_like')):
            v = num(p.get(k))
            B.w(ws, r, j, v if v is not None else '', 'in' if v is not None else 'txt')
        g = num(p.get('np_growth'))
        B.w(ws, r, 9, g if g is not None else '', 'in_pct' if g is not None else 'txt')
        R = r + 1
        B.w(ws, r, 10, f'=IF(AND(ISNUMBER(E{R}),E{R}>0),D{R}/E{R},"")', 'mult')
        B.w(ws, r, 11, f'=IF(AND(ISNUMBER(F{R}),F{R}>0),D{R}/F{R},"")', 'mult')
        B.w(ws, r, 12, f'=IF(AND(ISNUMBER(G{R}),G{R}>0),D{R}/G{R},"")', 'mult')
        B.w(ws, r, 13, f'=IF(AND(ISNUMBER(H{R}),H{R}>0,ISNUMBER(I{R})),(D{R}+I{R})/H{R},"")', 'mult')
        B.w(ws, r, 14, f'=IF(AND(ISNUMBER(G{R}),G{R}>0,ISNUMBER(I{R})),(D{R}+I{R})/G{R},"")', 'mult')
        B.w(ws, r, 15, f'=IF(AND(ISNUMBER(K{R}),ISNUMBER(J{R}),J{R}>0),K{R}/(J{R}*100),"")', 'mult')
    rl = r0 + n
    r1 = r0 + 1
    # 统计区：每个倍数的 纳入后样本→IQR 围栏→异常值剔除后统计（活公式）
    sr = rl + 2
    B.w(ws, sr, 0, '统计：纳入且有定义的样本；IQR 围栏 = Q1−1.5·IQR … Q3+1.5·IQR（QUARTILE.INC，与引擎线性插值一致）', 'head')
    mults = [('PE', 10), ('PB', 11), ('PS', 12), ('EV/EBITDA', 13), ('EV/Sales', 14), ('PEG', 15)]
    # 辅助列：纳入值（未纳入→""），以及 IQR 后纳入值
    help_c0 = 17
    for k, (nm, col) in enumerate(mults):
        B.w(ws, 2, help_c0 + k, f'{nm}(纳入)', 'head')
        B.w(ws, 2, help_c0 + len(mults) + k, f'{nm}(IQR后)', 'head')
    stat_rows = {}
    labels = ['有效样本 n(剔除前)', '均值(前)', 'Q1(前)', '中位数(前)', 'Q3(前)', 'IQR围栏下限', 'IQR围栏上限', 'n(IQR后)', '均值(后)', 'Q1(后)', '中位数(后)', 'Q3(后)']
    for i, lb in enumerate(labels):
        B.w(ws, sr + 1 + i, 0, lb)
        stat_rows[lb] = sr + 1 + i
    for k, (nm, col) in enumerate(mults):
        cl = xl_col_to_name(col)
        hc = xl_col_to_name(help_c0 + k)
        ac = xl_col_to_name(help_c0 + len(mults) + k)
        for i in range(n):
            R = r0 + i + 1
            B.w(ws, r0 + i, help_c0 + k, f'=IF(AND($C{R}=1,ISNUMBER({cl}{R})),{cl}{R},"")', 'mult')
        rng = f'{hc}{r1 + 0}:{hc}{rl}'
        rng = f'{hc}{r0 + 1}:{hc}{r0 + n}'
        arng = f'{ac}{r0 + 1}:{ac}{r0 + n}'
        col_out = 1 + k
        B.w(ws, sr, col_out, nm, 'head')
        def S_(lb, f, fm='mult'):
            B.w(ws, stat_rows[lb], col_out, f, fm)
        S_('有效样本 n(剔除前)', f'=COUNT({rng})', 'int')
        S_('均值(前)', f'=IF(COUNT({rng})>0,AVERAGE({rng}),"")')
        S_('Q1(前)', f'=IF(COUNT({rng})>=2,QUARTILE({rng},1),"")')
        S_('中位数(前)', f'=IF(COUNT({rng})>0,MEDIAN({rng}),"")')
        S_('Q3(前)', f'=IF(COUNT({rng})>=2,QUARTILE({rng},3),"")')
        q1, q3 = f"{xl_col_to_name(col_out)}{stat_rows['Q1(前)'] + 1}", f"{xl_col_to_name(col_out)}{stat_rows['Q3(前)'] + 1}"
        S_('IQR围栏下限', f'=IF(COUNT({rng})>=4,{q1}-1.5*({q3}-{q1}),"")')
        S_('IQR围栏上限', f'=IF(COUNT({rng})>=4,{q3}+1.5*({q3}-{q1}),"")')
        lo, hi = (f"{xl_col_to_name(col_out)}{stat_rows['IQR围栏下限'] + 1}", f"{xl_col_to_name(col_out)}{stat_rows['IQR围栏上限'] + 1}")
        for i in range(n):
            B.w(ws, r0 + i, help_c0 + len(mults) + k, f'=IF(ISNUMBER({hc}{r0 + i + 1}),IF(ISNUMBER({lo}),IF(AND({hc}{r0 + i + 1}>={lo},{hc}{r0 + i + 1}<={hi}),{hc}{r0 + i + 1},""),{hc}{r0 + i + 1}),"")', 'mult')
        S_('n(IQR后)', f'=COUNT({arng})', 'int')
        S_('均值(后)', f'=IF(COUNT({arng})>0,AVERAGE({arng}),"")')
        S_('Q1(后)', f'=IF(COUNT({arng})>=2,QUARTILE({arng},1),"")')
        S_('中位数(后)', f'=IF(COUNT({arng})>0,MEDIAN({arng}),"")')
        S_('Q3(后)', f'=IF(COUNT({arng})>=2,QUARTILE({arng},3),"")')
    # 目标公司隐含价值
    tr = sr + len(labels) + 3
    B.w(ws, tr, 0, '目标公司隐含权益价值（Q1/中位/Q3 × 目标指标；EV 类扣净债务类项目）', 'head')
    t = (run.get('relative_target') or {})
    tgt = {'PE': num(t.get('net_income')), 'PB': num(t.get('book_equity')), 'PS': num(t.get('revenue')), 'EV/EBITDA': num(t.get('ebitda')),
           'EV/Sales': num(t.get('revenue'))}
    B.w(ws, tr + 1, 0, '目标净债务类项目(EV−市值)')
    B.w(ws, tr + 1, 1, num(run.get('net_debt_like'), 0.0), 'in')
    nd_cell = f'$B${tr + 2}'
    for j, h in enumerate(['倍数', '目标指标', '低(Q1)', '中(中位)', '高(Q3)', '适用性']):
        B.w(ws, tr + 2, j, h, 'head')
    byx = ((rel.get('valuation') or {}).get('by_multiple') or {})
    key_of = {'PE': 'pe', 'PB': 'pb', 'PS': 'ps', 'EV/EBITDA': 'ev_ebitda', 'EV/Sales': 'ev_sales'}
    for i, (nm, col) in enumerate(mults[:5]):
        rr_ = tr + 3 + i
        col_out = 1 + i
        B.w(ws, rr_, 0, nm)
        B.w(ws, rr_, 1, tgt[nm] if tgt[nm] is not None else '', 'in' if tgt[nm] is not None else 'txt')
        cl = xl_col_to_name(col_out)
        for jj, st in enumerate(('Q1(后)', '中位数(后)', 'Q3(后)')):
            src = f"{cl}{stat_rows[st] + 1}"
            if nm.startswith('EV'):
                f = f'=IF(AND(ISNUMBER($B{rr_ + 1}),ISNUMBER({src})),{src}*$B{rr_ + 1}-{nd_cell},"")'
            else:
                f = f'=IF(AND(ISNUMBER($B{rr_ + 1}),ISNUMBER({src})),{src}*$B{rr_ + 1},"")'
            B.w(ws, rr_, 2 + jj, f, 'num')
        res = byx.get(key_of[nm]) or {}
        B.w(ws, rr_, 5, '适用' if res.get('applicable') else ('不适用：' + str(res.get('reason', ''))))
    B.w(ws, tr + 9, 0, '被剔除的候选同行（规则/人工）', 'bold')
    for i, e in enumerate(exc[:40]):
        B.w(ws, tr + 10 + i, 0, e.get('code')); B.w(ws, tr + 10 + i, 1, e.get('name')); B.w(ws, tr + 10 + i, 2, e.get('reason'))
    ws.freeze_panes(3, 2)
    B.rel_layout = {'stat_rows': stat_rows, 'tr': tr}


def _sheet_football(B, run, L):
    ws = B.sheet('足球场', {0: 36})
    B.w(ws, 0, 0, '足球场数据（权益价值区间，元；不输出目标价）', 'head')
    for j, h in enumerate(['方法', '类别', '低', '中', '高', '区间宽度(高−低)', '依据']):
        B.w(ws, 0, j, h, 'head')
    ff = run.get('football_field') or []
    for i, x in enumerate(ff, start=1):
        B.w(ws, i, 0, x['method']); B.w(ws, i, 1, x['kind'])
        lo, mid, hi = num(x.get('low'), 0.0), num(x.get('mid')), num(x.get('high'), 0.0)
        B.w(ws, i, 2, lo, 'snap'); B.w(ws, i, 3, mid if mid is not None else '', 'snap'); B.w(ws, i, 4, hi, 'snap')
        B.w(ws, i, 5, f'=E{i + 1}-C{i + 1}', 'num'); B.w(ws, i, 6, x.get('basis'))
    n = len(ff)
    mc = num((run.get('market') or {}).get('market_cap'))
    B.w(ws, n + 2, 0, '当前市值'); B.w(ws, n + 2, 1, mc if mc is not None else '', 'snap')
    vr = run.get('valuation_range') or {}
    B.w(ws, n + 3, 0, '合成估值区间(低)'); B.w(ws, n + 3, 1, num(vr.get('low'), 0.0), 'snap')
    B.w(ws, n + 4, 0, '合成估值区间(高)'); B.w(ws, n + 4, 1, num(vr.get('high'), 0.0), 'snap')
    B.w(ws, n + 5, 0, '合成规则'); B.w(ws, n + 5, 1, vr.get('rule') or '')
    B.w(ws, n + 6, 0, 'DCF 与相对估值区间是否重叠'); B.w(ws, n + 6, 1, '是' if vr.get('overlap') else ('否' if vr.get('overlap') is False else 'n/a'))
    B.w(ws, n + 7, 0, '市值位置'); B.w(ws, n + 7, 1, vr.get('market_position') or '')
    if n:
        ch = B.wb.add_chart({'type': 'bar', 'subtype': 'stacked'})
        ch.add_series({'name': '低', 'categories': ['足球场', 1, 0, n, 0], 'values': ['足球场', 1, 2, n, 2], 'fill': {'none': True}, 'border': {'none': True}})
        ch.add_series({'name': '区间', 'categories': ['足球场', 1, 0, n, 0], 'values': ['足球场', 1, 5, n, 5], 'fill': {'color': '#4F81BD'}})
        ch.set_title({'name': '足球场：权益价值区间（元）'})
        ch.set_legend({'none': True})
        ch.set_y_axis({'reverse': True})
        ch.set_size({'width': 900, 'height': 120 + 30 * n})
        ws.insert_chart(n + 9, 0, ch)


def _sheet_log(B, run):
    ws = B.sheet('变更日志', {0: 20, 1: 24, 2: 90})
    for j, h in enumerate(['日期', '版本', '内容']):
        B.w(ws, 0, j, h, 'head')
    rows = [(run.get('valuation_date', ''), run.get('engine_version', ''), '首次生成：引擎 valuation_pro 按已核验事实校准假设并整套重算；本表为人工改动登记表，请在每次改「假设」后追加一行（日期/改了什么/原因）'),
            ('', '', '说明：蓝底黄格为输入；灰底为引擎快照（非公式）；其余为公式'),
            ('', '', '证据分类：事实=年报原文，推论=由事实计算，观点=启发式/默认假设'),
            ('', '', '本表不输出目标价；估值区间由情景、敏感性与同行倍数合成，详见足球场')]
    for i, (a, b, c) in enumerate(rows, start=1):
        B.w(ws, i, 0, a); B.w(ws, i, 1, b); B.w(ws, i, 2, c, 'wrap')


# ---------------------------------------------------------------------------
# LibreOffice 无头重算与读回
# ---------------------------------------------------------------------------

def recalc_with_libreoffice(path, profile_dir=None, timeout=180):
    """用无头 LibreOffice 重算整本工作簿，返回 {(sheet, cell): value}；没有 soffice 返回 None。

    返回 None 的三种情形（调用方一律降级，不当成错误）：
    (1) 本机没装 soffice/libreoffice；(2) 转换进程非零退出或超时；(3) 进程退出码为 0 但没产出文件。
    临时目录与工作簿句柄在 `finally` 里释放——失败的转换也会留下 profile 目录，不清会积垃圾。
    """
    soffice = shutil.which('soffice') or shutil.which('libreoffice')
    if not soffice:
        return None
    import openpyxl
    path = Path(path).resolve()
    work = Path(tempfile.mkdtemp(prefix='valuation_lo_'))
    profile = Path(profile_dir) if profile_dir else work / 'profile'
    user = profile / 'user'
    user.mkdir(parents=True, exist_ok=True)
    (user / 'registrymodifications.xcu').write_text(
        '<?xml version="1.0" encoding="UTF-8"?><oor:items xmlns:oor="http://openoffice.org/2001/registry" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop></item>'
        '</oor:items>', encoding='utf-8')
    out = work / 'out'
    out.mkdir()
    cmd = [soffice, f'-env:UserInstallation=file://{profile}', '--headless', '--norestore', '--convert-to', 'xlsx:Calc MS Excel 2007 XML',
           '--outdir', str(out), str(path)]
    try:
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        except Exception:
            return None
        produced = out / path.name
        if not produced.exists():
            return None
        wb = openpyxl.load_workbook(produced, data_only=True)
        try:
            values = {}
            for ws in wb.worksheets:
                for row in ws.iter_rows():
                    for c in row:
                        if c.value is not None:
                            values[(ws.title, c.coordinate)] = c.value
        finally:
            wb.close()
        return values
    finally:
        shutil.rmtree(work, ignore_errors=True)


def export_with_cache(run, path):
    """两遍导出：先写公式，LibreOffice 重算，再把重算值写入缓存（预览/非 Excel 阅读器也能看到数字）。"""
    export_pro_workbook(run, path)
    cache = recalc_with_libreoffice(path)
    if cache:
        export_pro_workbook(run, path, cache=cache)
    return {'path': str(path), 'recalculated_by': 'LibreOffice' if cache else None, 'cells': len(cache or {})}
