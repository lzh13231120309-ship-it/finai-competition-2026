"""公告表格的语义归一：列名同义词库、纵向键值表转置、单元格数值/日期/百分数解析。

沪市公告的“解除质押/质押”常是**纵向键值表**（左列是字段名、右列是值，多个股东时右侧多列），
深市/北交所则是**横向表**（首行为表头，一行一笔）。两种版式经过 `normalize_table` 后
统一成 “每行一条事件、每列一个语义键(key)”，下游只需认语义键，不再关心版式。

语义键（key）说明见 LABEL_RULES；匹配顺序即优先级，**顺序不可随意调换**（例如
“本次质押前质押股份数量”必须先于“本次质押股份数量”判为累计列）。
"""
from __future__ import annotations

import re
from decimal import Decimal

from . import announcement_extract as ax
from .announcement_doc import AnnTable, clean_cell, is_data_cell, is_null_cell

# 标签(已去空白)的归类规则：(key, 正则)。first-match-wins。
LABEL_RULES: list[tuple[str, str]] = [
    ('holder', r'^(?:股东名称(?:/股东身份)?|股东姓名|股东|姓名/名称|名称|姓名|出质人|增持主体名称|增持主体|减持主体|信息披露义务人)$'),
    ('ignore', r'已质押|未质押|限售和冻结|限售股|标记数量|标记合计|是否为控股|是否为限售|是否为第一大|股东身份|控股股东或第一大|是否为控股股东'),
    ('chg_by_method', r'权益变动方式/'),
    ('chg_method', r'减持方式及对应减持数量|增持股份方式及数量|^(?:变动方式|减持方式|增持方式|权益变动方式)$'),
    ('chg_period', r'^(?:减持期间|本次增持实施期间|增持期间|变动时间|变动期间|减持时间|增持时间)$'),
    ('chg_price', r'减持均价|增持均价|成交均价|平均(?:减持|增持)?价'),
    ('chg_pricerange', r'减持价格区间|增持价格区间|价格区间'),
    ('chg_amount', r'减持总金额|增持金额|本次增持股份金额|增持总金额|已增持总金额|已减持总金额'),
    ('cur_shares', r'^(?:当前持股数量|本次减持后持股数量)'),
    ('cur_pct', r'^(?:当前持股比例|本次减持后持股比例)'),
    ('chg_pct', r'^(?:减持比例|已增持比例|已减持比例|本次增持股份比例（占总股本）|本次增持股份比例|增持比例)|本次增持股份/占总股本比例|减持股份/占总股本比例'),
    ('chg_shares', r'^(?:减持数量|增持数量|已增持数量|已减持数量|本次增持股份数量|本次减持股份数量|变动数量|认购股份数量|认购数量|受让股份数量|转让股份数量|减持股数|增持股数)|本次增持股份/股数|本次减持股份/股数|变动数量'),
    ('bef_pct', r'(?:本次)?(?:权益)?变动前.*(?:比例)|(?:增持|减持)(?:计划)?实施前.*持股比例|减持前.*比例|增持前.*比例'),
    ('aft_pct', r'(?:本次)?(?:权益)?变动后.*(?:比例)|减持后.*比例|增持后.*比例'),
    ('bef_shares', r'(?:本次)?(?:权益)?变动前.*(?:股数|持股数量|持股数|持有股份)|(?:增持|减持)(?:计划)?实施前.*持股数量|减持前.*(?:股数|持股数量)|增持前.*(?:股数|持股数量)'),
    ('aft_shares', r'(?:本次)?(?:权益)?变动后.*(?:股数|持股数量|持股数|持有股份)|减持后.*(?:股数|持股数量)|增持后.*(?:股数|持股数量)'),
    ('share_nature', r'^股份性质$'),
    ('cum_pct_t', r'(?:剩余被质押.*占公司总股本|累计.*占公司总股本|合计占公司总股本|剩余质押.*占公司总股本)'),
    ('cum_pct_h', r'(?:剩余被质押.*占其所持|累计.*占其所持|合计占其所持|累计.*占其持股|剩余质押.*占其所持|剩余被质押.*占其持股)'),
    ('cum_after', r'(?:剩余被质押股份数量|质押后.*质押(?:股份)?数|后累计质押|后的质押股份数|展期后累计|解除后.*累计|解除质押后.*(?:质押(?:股份)?数|剩余)|质押股份数量（含本次）)'),
    ('cum_before', r'(?:质押前.*质押(?:股份)?数|前累计质押|前的质押股份数|展期前累计|解除质押前.*(?:累计|质押))'),
    ('cum_after', r'累计(?:被)?(?:质押|冻结|拍卖)'),
    ('hold_shares', r'^(?:持股数量|持股总数|持有数量|持有股份数量|持有股份数|持股数|当前持股数量|增持前持股数量|持股数量（单位：股）)'),
    ('hold_pct', r'^(?:持股比例|持有比例|当前持股比例|增持前持股比例)'),
    ('rel_date', r'(?:解除质押(?:日期|时间|完成时间)|解质时间|质押解除(?:日期|日)|解除日期|解除时间|解押日期|解质押日期)'),
    ('new_end', r'(?:延期后|展期后).*(?:到期|期限)|(?:到期日|期限).*(?:延期后|展期后)'),
    ('orig_end', r'原(?:质押)?到期日|原质押期限'),
    ('start', r'(?:质押)?起始日期?|质押开始日|质押登记日|质押日期|起始日'),
    ('end', r'(?:质押)?到期日|质押期限|质押结束|到期日期'),
    ('pledgee', r'质权人|质押权人|质权银行|质押方'),
    ('purpose', r'质押(?:融资)?(?:资金)?用途|资金用途|融资用途'),
    ('supp', r'是否(?:为)?补充质押'),
    ('ev_shares_rel', r'(?:本次)?解除质押.*(?:数量|股数|股份)|本次解质(?:股份|数量|股数)?|解质(?:股份|数量|股数)'),
    ('ev_shares', r'(?:本次)?(?:延期|展期)?(?:股份)?质押(?:展期|延期)?(?:股份)?(?:数量|股数|股份)|本次质押(?:股份)?(?:数量|股数)?|质押数量|质押股数|质押股份数量|本次(?:延期|展期)质押'),
    ('pct_h', r'占其所持(?:股份|公司股份)?(?:总数的?)?(?:的)?比例|占其持有(?:公司)?股份(?:总数)?(?:的)?比例|占所持股份比例|占其所持'),
    ('pct_t', r'占公司总股本(?:的)?比例|占总股本(?:的)?比例|占公司总股本'),
    # 冻结
    ('fz_shares', r'(?:本次)?冻结(?:/标记/拍卖等)?(?:股份)?(?:数量|股数)|冻结股份数量|本次冻结|被冻结股份数量'),
    ('fz_applicant', r'冻结申请人|申请人|拍卖人|执行法院|法院'),
    ('fz_reason', r'^(?:原因|冻结原因|冻结类型|冻结性质|冻结类别)$'),
    ('fz_start', r'冻结起始日|冻结起始日期|冻结开始日'),
    ('fz_end', r'冻结到期日|冻结终止日|冻结期限|冻结结束日'),
]
_RULES = [(k, re.compile(p)) for k, p in LABEL_RULES]
_SPACE = re.compile(r'[\s　]+')


def classify_label(label: str) -> str | None:
    s = _SPACE.sub('', label or '')
    s = re.sub(r'[（(][^）)]*[）)]$', '', s) if re.search(r'[（(](?:股|万股|%|元|万元|单位[：:]\s*股)[）)]$', s) else s
    if not s:
        return None
    for key, rx in _RULES:
        if rx.search(s):
            return key
    return None


def unit_scale_shares(label: str, cell: str) -> int:
    """股数的量级：标签或单元格含“万股/亿股”时放大；默认“股”。"""
    text = f'{label}|{cell}'
    if '亿股' in text:
        return 10 ** 8
    if '万股' in text:
        return 10 ** 4
    return 1


def parse_int_shares(cell: str, label: str = '') -> tuple[Decimal | None, str]:
    """返回 (股数, 原因)。解析不了返回 (None, 原因)。"""
    c = clean_cell(cell)
    if not c:
        return None, 'empty'
    if is_null_cell(c):
        return None, 'null_symbol'
    scale = unit_scale_shares(label, c)
    m = re.search(r'[-−－]?\d[\d,，]*(?:\.\d+)?', c)
    if not m:
        return None, 'not_number'
    d = ax._dec(m.group(0))
    if d is None:
        return None, 'not_number'
    return d * scale, ''


def parse_pct(cell: str, label: str = '') -> tuple[Decimal | None, str, bool]:
    """返回 (百分数值, 原因, 带%号)。表格里的纯数字按“已是百分数值”处理，是否为小数形式由勾稽层判断。"""
    c = clean_cell(cell)
    if not c:
        return None, 'empty', False
    if is_null_cell(c):
        return None, 'null_symbol', False
    has_sign = bool(re.search(r'[%％]', c))
    m = re.search(r'[-−－]?\d[\d,，]*(?:\.\d+)?', c)
    if not m:
        return None, 'not_number', has_sign
    d = ax._dec(m.group(0))
    return d, '', has_sign


def parse_date(cell: str) -> str | None:
    c = clean_cell(cell)
    return ax.iso_date(c) if c else None


def clean_name(s: str) -> str:
    s = clean_cell(s)
    s = re.sub(r'[（(]\s*注\s*\d*\s*[）)]', '', s)
    s = re.sub(r'\s+', '', s)
    return s


def is_total_label(s: str) -> bool:
    s = (s or '').strip()
    if re.match(r'^(?:合\s*计|总\s*计|小\s*计|合\s*計)', s):
        return True
    return bool(len(s) <= 24 and re.search(r'合\s*计\s*[）)]?$', s))


# --------------------------------------------------------------- 转置（纵向键值表）

def _label_hits(cells: list[str]) -> int:
    return sum(1 for c in cells if c and classify_label(c) not in (None,))


def is_transposed(table: AnnTable) -> bool:
    """左列是字段名、其余列是取值（沪市纵向键值表）。

    判据：行数≥3，第 0 列里能被标签词库识别的行占多数；且第 0 列不是数据/名称。
    """
    rows = table.all_rows
    if len(rows) < 3:
        return False
    first_col = [r[0] for r in rows if r and r[0].strip()]
    if len(first_col) < 3:
        return False
    hits = sum(1 for c in first_col if classify_label(c) not in (None, 'holder'))
    holder_first = bool(first_col and classify_label(first_col[0]) == 'holder')
    # 若第 0 行是“股东名称 | 名1 | 名2”且第 0 列大多能识别为标签
    return hits >= max(2, int(0.5 * len(first_col))) and (holder_first or hits >= 3) and \
        not any(is_data_cell(c) for c in first_col)


def transpose(table: AnnTable) -> list[AnnTable]:
    """把键值表转成以字段名为表头的横向表（一列一条记录）。"""
    rows = table.all_rows
    width = max(len(r) for r in rows)
    grid = [r + [''] * (width - len(r)) for r in rows]
    labels = [g[0] for g in grid]
    out_rows = []
    for c in range(1, width):
        col = [g[c] for g in grid]
        if not any(x.strip() for x in col):
            continue
        out_rows.append(col)
    t = AnnTable(table.table_id + 'T', table.page, [labels], out_rows, bbox=table.bbox)
    t.pages = list(table.pages)
    t.row_pages = [table.pages[0]] * len(out_rows)
    t.transposed = True
    return [t]


def table_keys(table: AnnTable) -> list[str | None]:
    return [classify_label(l) for l in table.labels]


def normalize_tables(tables: list[AnnTable]) -> list[AnnTable]:
    """展开键值表；跨页续接的键值碎片(没有“股东名称”起始行)合并回上一张键值表。"""
    out: list[AnnTable] = []
    pending_kv: list[AnnTable] = []

    def flush():
        nonlocal pending_kv
        if not pending_kv:
            return
        merged_rows = []
        for t in pending_kv:
            merged_rows.extend(t.all_rows)
        base = pending_kv[0]
        m = AnnTable(base.table_id, base.page, [], merged_rows, bbox=base.bbox)
        m.all_rows = merged_rows
        m.pages = sorted({p for t in pending_kv for p in t.pages})
        out.extend(transpose(m))
        pending_kv = []

    for t in tables:
        kvish = len(t.all_rows) >= 2 and max(len(r) for r in t.all_rows) <= 3 and \
            sum(1 for r in t.all_rows if r and classify_label(r[0]) not in (None,)) >= 2
        if kvish:
            starts_new = classify_label(t.all_rows[0][0]) == 'holder'
            if starts_new:
                flush()
                pending_kv = [t]
            elif pending_kv:
                pending_kv.append(t)           # 续表碎片
            else:
                pending_kv = [t]
            continue
        flush()
        if is_transposed(t):
            out.extend(transpose(t))
        else:
            out.append(t)
    flush()
    return out


def column_keys(table: AnnTable) -> list[str | None]:
    """列语义键。多级表头先用整串判（“本次权益变动前/持股数量”），不行再用末段（“…/占其所持股份比例”）。"""
    keys = []
    for label in table.labels:
        key = classify_label(label)
        if key is None and label and '/' in label:
            key = classify_label(label.split('/')[-1])
        keys.append(key)
    return keys
