"""表格之外的抽取通道：正文叙述（中标/合同/权益变动封面/质权人补全）与增减持事件表。

设计要点
- 每个字段仍带 raw/page/loc/rule/confidence；找不到就 null 并说明原因，不猜。
- 中标金额只认“中标/预中标/中选/成交 + 金额”这类措辞；“资金预算”“采购预算”不是中标金额（实测踩过）。
- 多项目公告（一份公告列多个标包/项目）先按“项目名称：”或“项目为“…””切段，再逐段抽取，
  避免把第二个项目的金额填给第一个项目。
"""
from __future__ import annotations

import re
from decimal import Decimal

from . import announcement_extract as ax
from . import announcement_tables as T
from .announcement_doc import AnnDoc, clean_cell, is_null_cell
from .announcement_records import dec_out, fld, null_fld, norm_key

_CUR = r'(?:人民币|美元|港元|港币|欧元|日元)?'
_UNIT = {'元': 1, '万元': 10 ** 4, '亿元': 10 ** 8, '百万元': 10 ** 6, '亿美元': 10 ** 8, '万美元': 10 ** 4, '美元': 1,
         '万港元': 10 ** 4, '港元': 1, '亿港元': 10 ** 8}


def _page_of(doc: AnnDoc, pos: int):
    return doc.page_at(pos)


def subject_company(doc: AnnDoc, abbr: str | None = None) -> str | None:
    """公告主体的公司全称：优先“（以下简称“简称”）”与证券简称相同的全称；其次首部独占一行的公司名。"""
    text = doc.text
    if abbr:
        m = re.search(r'([一-鿿（）()]{4,30}?(?:股份有限公司|有限公司))[（(]以下简称[“"]?[^）)]{0,12}?' + re.escape(abbr), text)
        if m:
            return m.group(1)
    head = doc.pages[0]['text'].split('\n')[:14] if doc.pages else []
    for ln in head:
        s = re.sub(r'\s+', '', ln)
        if len(s) > 40 or re.search(r'证券代码|证券简称|公告编号|股票代码|保证|编号|重要内容|特别提示|详见|近日', s):
            continue
        m = re.match(r'^([一-鿿（）()A-Za-z0-9·]{4,40}?(?:股份有限公司|有限责任公司|有限公司|集团公司))$', s)
        if m:
            return m.group(1)
    m = re.search(r'([一-鿿（）()]{4,30}?(?:股份有限公司|有限公司))（以下简称', text)
    return m.group(1) if m else None


def _amount_yuan(num: str, unit: str) -> tuple[Decimal | None, str]:
    v = ax._dec(num)
    if v is None:
        return None, '人民币'
    cur = '人民币'
    if '美元' in unit:
        cur = '美元'
    elif '港元' in unit or '港币' in unit:
        cur = '港元'
    base = unit.replace('美元', '元').replace('港元', '元').replace('港币', '元')
    factor = _UNIT.get(base) or _UNIT.get(unit) or 1
    return v * factor, cur


def _iso(text: str) -> str | None:
    return ax.iso_date(text) if text else None


# ================================================================== 中标

_BID_AMOUNT = re.compile(
    r'(?:预计)?(?:项目)?(?:预?中标|预?中选|成交)(?:含税)?(?:金额|规模)(?:合计|总计)?(?:预计|约为|约|为)?[:：]?\s*(?:人民币)?\s*'
    r'(?:约)?([\d,，]+(?:\.\d+)?)\s*(亿元|万元|元)')
_BID_AMOUNT2 = re.compile(r'(?:中标|中选)(?:含税)?金额(?:合计)?(?:约为|约|为)?\s*(?:人民币)?\s*(?:约)?([\d,，]+(?:\.\d+)?)\s*(亿元|万元|元)')


def _bid_name(seg: str) -> tuple[str | None, str]:
    pats = [
        (r'项目名称[：:]\s*([^\n；;。]+)', 'R-BID-NAME-LABEL'),
        (r'确定[^，。]{0,20}?为[“"]([^”"]+)[”"]的中标', 'R-BID-NAME-QUOTE'),
        (r'参与了[“"]([^”"]+)[”"]投标', 'R-BID-NAME-QUOTE2'),
        (r'《([^》]+?)(?:_?中[标选]候选人公示|中标结果公示|中标公告)》', 'R-BID-NAME-BOOK'),
        (r'项目为[“"]([^”"]+)[”"]', 'R-BID-NAME-QUOTE3'),
        (r'(?:近日|日前)?(?:中标|中选)(?!人|候选|金额|通知|结果|单位)([^，。；\n（]{4,70}?(?:项目|工程|采购|标段|招标))(?=[，。；（])', 'R-BID-NAME-VERB'),
    ]
    for pat, rule in pats:
        m = re.search(pat, seg)
        if m:
            name = re.sub(r'\s+', '', m.group(1)).strip('；;，,')
            return name, rule
    return None, ''


def _segments(text: str) -> list[tuple[int, str]]:
    """按项目切段：多个“项目名称：”或“项目为“…””时分段。"""
    marks = [m.start() for m in re.finditer(r'项目名称[：:]', text)]
    if len(marks) < 2:
        marks = [m.start() for m in re.finditer(r'项目为[“"]', text)]
    if len(marks) < 2:
        return [(0, text)]
    # 向前扩到上一行行首（“（1）”类序号标题属于该段）
    starts = []
    for m in marks:
        ls = text.rfind('\n', 0, m)
        starts.append(ls + 1 if ls >= 0 else 0)
    segs = []
    for i, st in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        segs.append((st, text[st:end]))
    return segs


def build_bid(doc: AnnDoc, info: dict) -> list[dict]:
    text = doc.text
    title = ''.join(doc.pages[0]['text'].split('\n')[:7]) if doc.pages else ''
    company = subject_company(doc, (info.get('证券简称') or {}).get('value'))
    records = []
    for st, seg in _segments(text):
        name, rule = _bid_name(seg)
        if not name:
            continue
        f = {}
        f['项目名称'] = fld(name, name, _page_of(doc, st), f'正文@{st}', rule, 0.9)
        # 招标人
        m = re.search(r'招标人(?:名称)?(?:为|[：:])\s*([^\n；;。，]+)', seg) or re.search(r'发包人[：:]\s*([^\n；;。，]+)', seg)
        if m:
            f['招标人'] = fld(re.sub(r'\s+', '', m.group(1)), m.group(0), _page_of(doc, st), f'正文@{st + m.start()}', 'R-BID-BUYER', 0.95)
        else:
            f['招标人'] = null_fld('原文未用“招标人”明示', 'R-BID-BUYER')
        # 中标人
        m = re.search(r'中标人[：:]\s*([^\n；;。，]+)', seg)
        if m and not re.search(r'中标人(?:名单|补充)', m.group(0)):
            f['中标人'] = fld(re.sub(r'\s+', '', m.group(1)), m.group(0), _page_of(doc, st), f'正文@{st + m.start()}', 'R-BID-WINNER-LABEL', 0.95)
        else:
            sub = re.search(r'(?:全资|控股)?(?:子公司|孙公司)\s*([一-鿿（）()]{4,30}?(?:有限公司|股份有限公司|分公司))(?:（[^）]*）)?\s*(?:近日|日前|于近日)?\s*(?:收到|中标|已中标|被确定)', text)
            sub2 = re.search(r'(?:近日|日前)[,，]?[^，。]{0,30}?所属子公司([一-鿿（）()]{4,30}?(?:有限公司|股份有限公司))中标', text)
            if sub or sub2:
                mm = sub or sub2
                f['中标人'] = fld(mm.group(1), mm.group(0), _page_of(doc, mm.start()), f'正文@{mm.start()}', 'R-BID-WINNER-SUB', 0.85)
            elif company:
                f['中标人'] = fld(company, company, 1, '公告首部公司名', 'R-BID-WINNER-SUBJECT', 0.7)
            else:
                f['中标人'] = null_fld('未找到中标主体', 'R-BID-WINNER')
        # 金额
        amt = None
        for rx in (_BID_AMOUNT, _BID_AMOUNT2):
            m = rx.search(seg)
            if m:
                v, _ = _amount_yuan(m.group(1), m.group(2))
                amt = (v, m)
                break
        if amt:
            f['中标金额_元'] = fld(dec_out(amt[0]), amt[1].group(0), _page_of(doc, st + amt[1].start()), f'正文@{st + amt[1].start()}', 'R-BID-AMOUNT', 0.95)
        else:
            reason = '仅披露预算/未披露中标金额' if re.search(r'预算', seg) else '原文未披露金额'
            f['中标金额_元'] = null_fld(reason, 'R-BID-AMOUNT')
        # 阶段
        cand = bool(re.search(r'候选人|预中标|公示期', title + seg[:200] + text[:300]))
        won_notice = bool(re.search(r'收到[^。]{0,12}《?中标通知书|已确定公司[^。]{0,20}中标|中标结果公示|中标公告|重大工程中标|收到中标结果', text))
        stage = '中标候选人' if (cand and not won_notice) else ('中标' if (won_notice or re.search(r'中标', title)) else None)
        f['阶段'] = fld(stage, '', 1, '标题/正文关键词', 'R-BID-STAGE', 0.85) if stage else null_fld('无法判定阶段', 'R-BID-STAGE')
        m = re.search(r'工期[：:]\s*(\d+)\s*日历天', seg)
        f['工期_日历天'] = fld(int(m.group(1)), m.group(0), _page_of(doc, st + m.start()), f'正文@{st + m.start()}', 'R-BID-DAYS') if m else null_fld('未披露日历天工期', 'R-BID-DAYS')
        records.append({'event_type': 'bid', 'fields': f})
    return records


# ================================================================== 合同

_CT_AMOUNT = re.compile(
    r'(?:合同(?:总)?(?:金额|价款|总价|价格)|总金额|订单金额|签约合同价|协议(?:总)?金额)[^\d\n]{0,14}?'
    r'(?:人民币|美元|港元)?\s*([\d,，]+(?:\.\d+)?)\s*(亿美元|万美元|亿港元|万港元|亿元|万元|美元|港元|元)')


def build_contract(doc: AnnDoc, info: dict) -> list[dict]:
    text = doc.text
    f = {}
    m = re.search(r'(?:签订|签署|订立)(?:了)?[^《》\n]{0,30}?《([^》]{2,60}?(?:合同|协议)[^》]{0,10})》', text) or \
        re.search(r'(?:签订|签署)了?\s*《([^》]{2,60}?(?:合同|协议)[^》]{0,10})》', text)
    if m:
        f['合同名称'] = fld(m.group(1), m.group(0), _page_of(doc, m.start()), f'正文@{m.start()}', 'R-CT-NAME-BOOK', 0.9)
    else:
        m = re.search(r'(?:签订|签署)(?:了)?\s*([^，。；《》（(\n]{2,40}?(?:合同|协议))(?=[，。；（(,])', text)
        if m:
            f['合同名称'] = fld(re.sub(r'\s+', '', m.group(1)), m.group(0), _page_of(doc, m.start()), f'正文@{m.start()}', 'R-CT-NAME-VERB', 0.75)
        else:
            f['合同名称'] = null_fld('未找到合同名称', 'R-CT-NAME')
    for label, key in (('甲方', '甲方'), ('乙方', '乙方')):
        mm = re.search(label + r'[：:]\s*([^\n；;。，]+)', text)
        f[key] = fld(re.sub(r'\s+', '', mm.group(1)), mm.group(0), _page_of(doc, mm.start()), f'正文@{mm.start()}', f'R-CT-{label}', 0.95) if mm else None
    if f.get('甲方') is None or f.get('乙方') is None:
        # 叙事式写法：「A…（全资子公司）B…联合 C 于 X 日 与 D…（或其指定主体）签订…合同」。
        # 没有"甲方："标签时按签约句式回填：与后为对方（甲方），子公司/联合方为己方（乙方）。
        # 取**实体全称**（截到"有限公司/股份有限公司"等结尾），去掉（以下简称…）括注。
        ents = r'[\u4e00-\u9fa5A-Za-z0-9·]{2,30}?(?:股份有限公司|有限责任公司|有限公司|集团公司|研究院|银行|中心)'
        sign = re.search(r'与\s*(' + ents + r')(?:[（(]|或|及|和|、|\s|,|。)[^。]{0,80}?(?:签订|签署|订立)', text)
        if sign and f.get('甲方') is None:
            f['甲方'] = fld(sign.group(1), sign.group(0), _page_of(doc, sign.start()),
                            f'正文@{sign.start()}', 'R-CT-PARTY-与签订', 0.85)
        sub = re.search(r'全资子公司\s*(' + ents + r')', text) or \
            re.search(r'(' + ents + r')(?:[（(][^）)]{0,20}[)）])?\s*(?:联合|于\s*\d{4})', text)
        if sub and f.get('乙方') is None:
            f['乙方'] = fld(sub.group(1), sub.group(0), _page_of(doc, sub.start()),
                            f'正文@{sub.start()}', 'R-CT-PARTY-己方', 0.7)
    for key in ('甲方', '乙方'):
        if f.get(key) is None:
            f[key] = null_fld(f'原文未标注{key}', f'R-CT-{key}')
    mm = _CT_AMOUNT.search(text)
    if mm:
        v, cur = _amount_yuan(mm.group(1), mm.group(2))
        f['合同金额'] = fld(dec_out(v), mm.group(0), _page_of(doc, mm.start()), f'正文@{mm.start()}', 'R-CT-AMOUNT', 0.95)
        f['币种'] = fld(cur, mm.group(0), _page_of(doc, mm.start()), f'正文@{mm.start()}', 'R-CT-CUR', 0.95)
    else:
        f['合同金额'] = null_fld('原文未披露合同金额', 'R-CT-AMOUNT')
        f['币种'] = null_fld('无金额故无币种', 'R-CT-CUR')
    mm = re.search(r'合同签署(?:时间|日期)[：:]\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)', text) or \
        re.search(r'于\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)\s*与[^。]{0,90}?(?:签订|签署)', text)
    f['签订日期'] = fld(_iso(mm.group(1)), mm.group(0), _page_of(doc, mm.start()), f'正文@{mm.start()}', 'R-CT-DATE', 0.9) if mm else null_fld('原文无明确签订日期', 'R-CT-DATE')
    return [{'event_type': 'contract', 'fields': f}]


# ================================================================== 权益变动封面

def build_equity_cover(doc: AnnDoc) -> list[dict]:
    head = '\n'.join(p['text'] for p in doc.pages[:2])[:3000]
    f = {}

    def grab(label_rx, key, rule, post=None):
        m = re.search(label_rx, head)
        if not m:
            f[key] = null_fld('封面无该项', rule)
            return
        val = re.sub(r'\s+', '', m.group(1)).strip('，。；')
        if post:
            val = post(val)
        f[key] = fld(val, m.group(0).strip(), 1, f'封面@{m.start()}', rule, 0.95)

    grab(r'信息披露义务人(?:之一|（一）|\(一\)|（甲）)?[：:]\s*([^\n]+)', '信息披露义务人', 'R-EQ-OBLIGOR')
    grab(r'上市公司名称[：:]\s*([^\n]+)', '上市公司名称', 'R-EQ-COMPANY')
    grab(r'股票代码[：:]\s*([0-9]{5,6})', '股票代码', 'R-EQ-CODE')
    grab(r'(?:权益|股份|股权)变动性质[：:]\s*([^\n]+?)(?=\s*(?:签署日期|\n|$))', '权益变动性质', 'R-EQ-NATURE')
    m = re.search(r'签署日期[：:]\s*([^\n]+)', head)
    iso = _iso(m.group(1)) if m else None
    f['签署日期'] = fld(iso, m.group(0), 1, f'封面@{m.start()}', 'R-EQ-DATE') if iso else null_fld('签署日期不完整或缺失', 'R-EQ-DATE', m.group(0) if m else '')
    if f['信息披露义务人']['value'] is None and f['上市公司名称']['value'] is None:
        return []
    return [{'event_type': 'equity_report', 'fields': f}]


# ================================================================== 增减持 / 权益变动 事件表

def _num_cell(raw: str, label: str):
    v, why = T.parse_int_shares(raw, label)
    return v


def _split_period(raw: str):
    ds = [d for _, _, d in ax._iter_dates(raw) if d]
    if not ds:
        return None, None
    return ds[0], ds[-1]


def _methods_from_text(raw: str) -> list[str]:
    """“集中竞价减持，774,672股大宗交易减持，85,000股” -> 仅取数量非 0 的方式。"""
    out = []
    for m in re.finditer(r'(集中竞价|大宗交易|协议转让|二级市场|竞价交易)[^，,\d]{0,6}[，,]?\s*([\d,]+)\s*股', raw):
        if ax._dec(m.group(2)) and ax._dec(m.group(2)) != 0:
            out.append({'二级市场': '集中竞价'}.get(m.group(1), m.group(1)))
    if not out:
        for m in re.finditer(r'(集中竞价|大宗交易|协议转让|竞价交易)', raw):
            out.append(m.group(1))
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _holder_key_match(a: str, b: str) -> bool:
    x, y = norm_key(a), norm_key(b)
    return bool(x and y and (x == y or (min(len(x), len(y)) >= 3 and (x in y or y in x))))


def build_change(doc: AnnDoc, tables: list, family: str, title_dir: int) -> list[dict]:
    """从含变动列/前后持股列的表格还原“股东-变动”记录。title_dir: -1 减持 / +1 增持 / 0 未知（权益变动）。"""
    recs: list[dict] = []        # {'name','page','data':{...}}

    def find_rec(name):
        for r in recs:
            if _holder_key_match(r['name'], name):
                return r
        return None

    def _is_hold_only(tt):
        ks = {k for k in T.column_keys(tt) if k}
        return not (ks & {'chg_shares', 'chg_by_method', 'chg_pct', 'bef_shares', 'aft_shares', 'cur_shares', 'chg_method'})

    ordered_tables = sorted(tables, key=lambda tt: (1 if _is_hold_only(tt) else 0))
    for t in ordered_tables:
        keys = T.column_keys(t)
        keyset = {k for k in keys if k}
        if 'holder' not in keyset:
            continue
        if not keyset & {'chg_shares', 'chg_by_method', 'chg_pct', 'bef_shares', 'aft_shares', 'cur_shares', 'chg_method', 'hold_shares'}:
            continue
        if any(re.match(r'^(?:计划|拟)(?:的)?(?:增持|减持)', l.split('/')[-1]) for l in t.labels):
            continue            # 计划类表格（计划金额/方式/期间）不是已发生的变动
        if family == 'shareholding_change' and keyset & {'ev_shares', 'ev_shares_rel', 'fz_shares'}:
            continue
        idx = {}
        for i, k in enumerate(keys):
            if k and k not in idx:
                idx[k] = i
        by_method_cols = [i for i, k in enumerate(keys) if k == 'chg_by_method']
        hold_only = not (keyset & {'chg_shares', 'chg_by_method', 'chg_pct', 'bef_shares', 'aft_shares', 'cur_shares', 'chg_method'})
        prev_cells = None
        pending: dict[str, list] = {}
        rows = list(enumerate(t.rows))
        for r, row in rows:
            cells = list(row) + [''] * (len(keys) - len(row))
            first = cells[idx['holder']].strip() if cells[idx['holder']].strip() else ''
            method_cell = cells[idx['chg_method']] if 'chg_method' in idx and idx['chg_method'] < len(cells) else ''
            nature = cells[idx['share_nature']] if 'share_nature' in idx else ''
            page = t.row_pages[r] if r < len(t.row_pages) else t.page
            is_total = T.is_total_label(first) or T.is_total_label(method_cell.strip())
            # 减持表中“合计”行里的姓名碎片：(青岛海发产业投资 / 控股有限公司)
            if is_total and first and not T.is_total_label(first) and prev_cells is not None and len(first) < 12:
                first = (prev_cells[idx['holder']].strip() + first)
            elif T.is_total_label(first) and not first.startswith(('合计', '总计')) is False:
                pass
            if T.is_total_label(first) and 'chg_method' not in idx:
                continue
            if T.is_total_label(first):
                continue
            if not first:
                if prev_cells is None:
                    continue
                first = prev_cells[idx['holder']].strip()
                inherited = True
            else:
                inherited = False
            if nature and re.match(r'^其中|^(?:无限售|有限售)条件(?:的)?股份$', nature.strip()):
                prev_cells = prev_cells or cells
                continue            # “其中：无限售条件股份”等分项行不取
            name = T.clean_name(first.split('/')[0])
            if not name:
                continue
            rec = find_rec(name)
            if rec is None:
                if hold_only:
                    continue        # 仅有“持股”的表（前基本情况）只补充已有记录，不新建
                rec = {'name': name, 'page': page, 'data': {}, 'rows': []}
                recs.append(rec)
            elif len(name) > len(rec['name']):
                rec['name'] = name
            d = rec['data']

            def put(key, value, raw, label_i, rule, conf=1.0):
                if value is None:
                    return
                if key in d and d[key]['value'] is not None and d[key].get('prio', 0) >= conf:
                    return
                d[key] = {'value': value, 'raw': raw, 'page': page,
                          'loc': f'表{t.table_id} 行{r + 1} 列{label_i + 1}「{t.labels[label_i]}」', 'rule': rule,
                          'confidence': conf, 'prio': conf}

            def cell(k):
                i = idx.get(k)
                return (i, cells[i]) if i is not None and i < len(cells) else (None, '')

            # 变动前/后
            for ks, out, kind in (('bef_shares', '变动前持股_股', 's'), ('hold_shares', '变动前持股_股', 's'),
                                  ('bef_pct', '变动前持股比例_百分', 'p'), ('hold_pct', '变动前持股比例_百分', 'p'),
                                  ('aft_shares', '变动后持股_股', 's'), ('cur_shares', '变动后持股_股', 's'),
                                  ('aft_pct', '变动后持股比例_百分', 'p'), ('cur_pct', '变动后持股比例_百分', 'p')):
                i, raw = cell(ks)
                if i is None or not raw.strip():
                    continue
                if ks in ('hold_shares', 'hold_pct') and not hold_only and ('bef_shares' in idx or 'bef_pct' in idx):
                    continue
                if ks in ('hold_shares', 'hold_pct') and ('cur_shares' in idx) and not hold_only:
                    # 结果表里的“持股数量”不再用来当变动前（键值表里会有）
                    pass
                if kind == 's':
                    v, why = T.parse_int_shares(raw, t.labels[i])
                else:
                    v, why, _ = T.parse_pct(raw, t.labels[i])
                if v is not None:
                    put(out, v, raw, i, 'R-CHG-BEFAFT', 0.95 if not inherited else 0.8)
            # 变动数量
            sign = title_dir
            i, raw = cell('chg_shares')
            if i is not None and raw.strip() and not is_null_cell(raw):
                v, why = T.parse_int_shares(raw, t.labels[i])
                if v is not None:
                    if re.search(r'^[-−－]', clean_cell(raw)):
                        v = -abs(v)
                    elif sign == -1 or re.search(r'减持', t.labels[i]):
                        v = -abs(v)
                    put('变动股数_股', v, raw, i, 'R-CHG-SHARES', 0.95)
            elif by_method_cols:
                tot = Decimal(0)
                used = []
                for ci in by_method_cols:
                    if ci < len(cells) and cells[ci].strip():
                        v, _ = T.parse_int_shares(cells[ci], t.labels[ci])
                        if v:
                            tot += v
                            used.append(t.labels[ci].split('/')[-1])
                if tot != 0 or used:
                    put('变动股数_股', tot, ','.join(cells[ci] for ci in by_method_cols), by_method_cols[0], 'R-CHG-BYMETHOD', 0.9)
                    if used:
                        put('变动方式', '、'.join(used), '', by_method_cols[0], 'R-CHG-METHOD-COL', 0.9)
            if 'chg_shares' not in idx and not by_method_cols and 'chg_method' in idx:
                mi, mraw = cell('chg_method')
                nums = [ax._dec(x) for x in re.findall(r'([\d,，]{4,})\s*股', mraw or '')]
                nums = [n for n in nums if n]
                if nums and not T.is_total_label(mraw):
                    tot = sum(nums)
                    put('变动股数_股', -tot if sign == -1 else tot, mraw, mi, 'R-CHG-SHARES-TEXT', 0.85)
            i, raw = cell('chg_pct')
            if i is not None and raw.strip() and not is_null_cell(raw):
                v, why, _ = T.parse_pct(raw, t.labels[i])
                if v is not None:
                    put('变动比例_百分', abs(v), raw, i, 'R-CHG-PCT', 0.95)
            i, raw = cell('chg_period')
            if i is not None and raw.strip():
                a, b = _split_period(raw)
                if a:
                    put('变动期间起', a, raw, i, 'R-CHG-PERIOD', 0.95)
                    put('变动期间止', b, raw, i, 'R-CHG-PERIOD', 0.95)
            i, raw = cell('chg_price')
            if i is not None and raw.strip() and not is_null_cell(raw):
                v = ax._dec(re.sub(r'[^\d.]', '', raw)) if re.search(r'\d', raw) else None
                put('成交均价_元', v, raw, i, 'R-CHG-AVGPRICE', 0.95)
            i, raw = cell('chg_method')
            if i is not None and raw.strip() and not is_null_cell(raw):
                if T.is_total_label(raw):
                    pass
                else:
                    ms = _methods_from_text(raw) or ([raw.strip()] if len(raw.strip()) <= 12 else [])
                    if ms:
                        existing = d.get('变动方式', {}).get('value')
                        merged = '、'.join(dict.fromkeys(([existing] if existing else []) + ms))
                        put('变动方式', merged, raw, i, 'R-CHG-METHOD', 0.95)
                        d['变动方式']['value'] = merged
            rec['rows'].append(r)
            prev_cells = cells
    out = []
    for rec in recs:
        d = rec['data']
        if not any(k in d for k in ('变动股数_股', '变动后持股_股', '变动前持股_股')):
            continue
        if hold_like_only(d):
            continue
        f = {'股东名称': fld(rec['name'], rec['name'], rec['page'], '表格股东列', 'R-CHG-HOLDER')}
        for k in ('变动方式', '变动股数_股', '变动比例_百分', '变动期间起', '变动期间止', '成交均价_元',
                  '变动前持股_股', '变动前持股比例_百分', '变动后持股_股', '变动后持股比例_百分'):
            if k in d:
                x = d[k]
                f[k] = fld(dec_out(x['value']) if isinstance(x['value'], Decimal) else x['value'], x['raw'], x['page'], x['loc'], x['rule'], x['confidence'])
            else:
                f[k] = null_fld('表格未提供', 'R-CHG')
        # 变动量缺失时由前后持股推算（标注推算，置信度降低；不覆盖已披露值）
        if f['变动股数_股']['value'] is None and f['变动前持股_股']['value'] is not None and f['变动后持股_股']['value'] is not None:
            diff = Decimal(str(f['变动后持股_股']['value'])) - Decimal(str(f['变动前持股_股']['value']))
            f['变动股数_股'] = fld(dec_out(diff), '后-前', f['变动后持股_股']['page'], '推算：变动后持股-变动前持股', 'R-CHG-DERIVED', 0.8, derived=True)
        out.append({'event_type': 'shareholding_change', 'fields': f})
    fill_change_from_text(doc, out, title_dir)
    if family == 'equity_report':
        # 封面“股份变动性质：股份减少（协议转让）”给出的方式，只补给有实际变动量的股东
        head = '\n'.join(p['text'] for p in doc.pages[:1])[:2500]
        m = re.search(r'(?:权益|股份|股权)变动性质[：:][^\n（(]*[（(]([^）)\n]+)[）)]', head)
        if m and re.search(r'协议转让|大宗交易|集中竞价|认购|受让', m.group(1)):
            for rec in out:
                chg = rec['fields'].get('变动股数_股', {}).get('value')
                if chg and rec['fields'].get('变动方式', {}).get('value') is None:
                    rec['fields']['变动方式'] = fld(m.group(1), m.group(0), 1, '封面·变动性质括注', 'R-CHG-COVER-METHOD', 0.8)
    return out


_DATE_RX = r'\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日'


def fill_change_from_text(doc: AnnDoc, recs: list, title_dir: int):
    """用“…（日期[至日期]）…通过…集中竞价…增持/减持公司股份 N 股”这类叙述，按股数 N 与记录对上号，补期间与方式。"""
    text = doc.text
    for m in re.finditer(rf'(?:自|于)?\s*({_DATE_RX})\s*(?:至|—|－|-|~|～)?\s*({_DATE_RX})?[，,]?[^。]{{0,60}}?'
                         rf'(集中竞价|大宗交易|协议转让|竞价交易)[^。]{{0,30}}?(?:累计)?(?:首次)?(?:增持|减持)(?:公司|已回购)?(?:股份|股票)?\s*([\d,，]{{4,}})\s*股', text):
        n = ax._dec(m.group(4))
        d1, d2 = _iso(m.group(1)), _iso(m.group(2)) if m.group(2) else None
        for rec in recs:
            sv = rec['fields'].get('变动股数_股', {}).get('value')
            if sv is None or n is None or abs(Decimal(str(sv))) != n:
                continue
            pg = _page_of(doc, m.start())
            if rec['fields'].get('变动期间起', {}).get('value') is None and d1:
                rec['fields']['变动期间起'] = fld(d1, m.group(0)[:60], pg, f'正文@{m.start()}', 'R-CHG-TEXT-PERIOD', 0.85)
                rec['fields']['变动期间止'] = fld(d2 or d1, m.group(0)[:60], pg, f'正文@{m.start()}', 'R-CHG-TEXT-PERIOD', 0.85)
            if rec['fields'].get('变动方式', {}).get('value') is None:
                rec['fields']['变动方式'] = fld(m.group(3).replace('竞价交易', '集中竞价'), m.group(0)[:60], pg, f'正文@{m.start()}', 'R-CHG-TEXT-METHOD', 0.85)


def hold_like_only(d: dict) -> bool:
    return set(d) <= {'变动前持股_股', '变动前持股比例_百分'} and False


# ================================================================== 质押叙述补全

def pledge_text_fill(doc: AnnDoc, events: list, holdings: list):
    """表格缺“质权人”时，从正文“质押给X”补；仅当该文只有一个解除/质押事件时才补，避免张冠李戴。"""
    text = doc.text
    review = []
    singles = [e for e in events if e['event_type'] in ('release', 'pledge', 'extension')]
    for e in singles:
        pf = e['fields'].get('质权人')
        if pf and pf.get('value') is not None:
            continue
        cands = re.findall(r'(?:质押给|质押予|质押于|在)\s*([一-鿿（）()]{4,40}?(?:银行[一-鿿（）()]{0,12}?(?:分行|支行)|证券股份有限公司|信托[一-鿿]{0,10}?有限公司|有限公司|股份有限公司|基金管理有限公司|分行|支行))(?=[，。；、\s\d办将]|解除|办理)', text)
        names = list(dict.fromkeys(cands))
        same_holder = [x for x in singles if x['fields']['股东名称']['value'] == e['fields']['股东名称']['value']]
        if len(names) == 1 and len(same_holder) == 1:
            e['fields']['质权人'] = fld(names[0], names[0], 1, '正文“质押给…”', 'R-PLG-TEXT-PLEDGEE', 0.8)
    return review


def pledge_narrative(doc: AnnDoc, info: dict) -> tuple[list[dict], list[dict]]:
    """北交所/无表格叙述体：“股东X本次解除股份质押N股，占其所持股份a%，占公司总股本b%…”"""
    text = doc.text
    events, holdings = [], []
    m = re.search(r'(?:公司接到|公司收到)?股东\s*([一-鿿（）()]{2,30}?)(?:的通知)?[，,]?\s*股东\s*([一-鿿（）()]{2,30}?)本次解除(?:股份|股权)?质押\s*([\d,，]+)\s*股[，,]\s*占其所持股份\s*([\d.]+)\s*%[，,]\s*占公司总股本\s*([\d.]+)\s*%', text)
    if not m:
        m = re.search(r'([一-鿿（）()]{2,30}?)本次解除(?:股份|股权)?质押\s*([\d,，]+)\s*股[，,]\s*占其所持股份\s*([\d.]+)\s*%[，,]\s*占公司总股本\s*([\d.]+)\s*%', text)
        if m:
            name, sh, ph, pt = m.group(1), m.group(2), m.group(3), m.group(4)
        else:
            return events, holdings
    else:
        name, sh, ph, pt = m.group(2), m.group(3), m.group(4), m.group(5)
    name = re.sub(r'^股东', '', name)
    pg = _page_of(doc, m.start())
    f = {'股东名称': fld(name, name, pg, f'正文@{m.start()}', 'R-NARR-HOLDER', 0.85),
         '解除质押股数_股': fld(int(ax._dec(sh)), m.group(0), pg, f'正文@{m.start()}', 'R-NARR-SHARES', 0.9),
         '占所持比例_百分': fld(dec_out(ax._dec(ph)), m.group(0), pg, f'正文@{m.start()}', 'R-NARR-PCT', 0.9),
         '占总股本比例_百分': fld(dec_out(ax._dec(pt)), m.group(0), pg, f'正文@{m.start()}', 'R-NARR-PCT', 0.9)}
    mm = re.search(r'解除日期为\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)', text)
    f['解押日期'] = fld(_iso(mm.group(1)), mm.group(0), pg, f'正文@{mm.start()}', 'R-NARR-RELDATE', 0.9) if mm else null_fld('正文无解除日期', 'R-NARR-RELDATE')
    mm = re.search(r'于\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)\s*被质押[，,]\s*质押权人为\s*([^，。；\n]+)', text)
    if mm:
        f['质押起始日'] = fld(_iso(mm.group(1)), mm.group(0), pg, f'正文@{mm.start()}', 'R-NARR-START', 0.9)
        f['质权人'] = fld(re.sub(r'\s+', '', mm.group(2)), mm.group(0), pg, f'正文@{mm.start()}', 'R-NARR-PLEDGEE', 0.9)
    else:
        f['质押起始日'] = null_fld('正文无起始日', 'R-NARR-START')
        f['质权人'] = null_fld('正文无质权人', 'R-NARR-PLEDGEE')
    events.append({'event_type': 'release', 'fields': f})
    mm = re.search(r'剩余被质押(?:股份|股权)为\s*([\d,，]+)\s*股[，,]\s*占其所持股份的?\s*([\d.]+)\s*%[，,]\s*占公司总股本的?\s*([\d.]+)\s*%', text)
    if mm:
        hf = {'股东名称': f['股东名称'],
              '累计质押股数_股': fld(int(ax._dec(mm.group(1))), mm.group(0), pg, f'正文@{mm.start()}', 'R-NARR-CUM', 0.9),
              '累计占所持比例_百分': fld(dec_out(ax._dec(mm.group(2))), mm.group(0), pg, f'正文@{mm.start()}', 'R-NARR-CUM', 0.9),
              '累计占总股本比例_百分': fld(dec_out(ax._dec(mm.group(3))), mm.group(0), pg, f'正文@{mm.start()}', 'R-NARR-CUM', 0.9)}
        holdings.append({'event_type': 'holding', 'fields': hf})
    return events, holdings


# ================================================================== 总入口

def _direction(doc: AnnDoc) -> int:
    head = ''.join(doc.pages[0]['text'].split('\n')[:8]) if doc.pages else ''
    if re.search(r'减持', head) and not re.search(r'增持', head):
        return -1
    if re.search(r'增持', head) and not re.search(r'减持', head):
        return 1
    return 0


def extract_narrative(doc: AnnDoc, tables: list, family: str, info: dict, events: list) -> dict:
    out = {'events': [], 'holdings': [], 'review': [], 'checks': []}
    if family == 'bid':
        out['events'] = build_bid(doc, info)
    elif family == 'contract':
        out['events'] = build_contract(doc, info)
    elif family == 'equity_report':
        out['events'] = build_equity_cover(doc) + build_change(doc, tables, family, 0)
    elif family == 'shareholding_change':
        out['events'] = build_change(doc, tables, family, _direction(doc))
    elif family == 'pledge':
        pledge_text_fill(doc, events, [])
        if not [e for e in events if e['event_type'] in ('release', 'pledge', 'extension')]:
            ev, hd = pledge_narrative(doc, info)
            out['events'], out['holdings'] = ev, hd
    return out
