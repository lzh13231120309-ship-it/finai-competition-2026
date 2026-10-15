"""A 股公告确定性结构化提取引擎：类型识别 -> 表格/正文事件切分 -> 字段映射 -> 数值归一 -> 跨字段勾稽。

与 announcement_extract（只给“候选值”）的分工：
- announcement_extract 负责**机械归一**：大写金额、万元/亿元/万股、日期、百分数、“—”空值、合计行；
- 本模块在它之上做**语义归位**：这张表是“解除质押”还是“新增质押”？这个 6.33% 是“占其所持”
  还是“占总股本”？这是“本次”还是“累计”？—— 全部由确定性规则完成，每个字段输出

    {'value': 归一值, 'raw': 原文, 'page': 页码, 'loc': 表/行/列 或 正文偏移, 'confidence': 0~1, 'rule': 规则ID}

缺失字段输出 value=None 并给 reason（宁缺毋滥，绝不猜）。勾稽不符的不改数值，只进 review。
同输入同输出：全程无随机、无时间依赖、按稳定顺序遍历。
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

from . import announcement_extract as ax
from . import announcement_tables as T
from .announcement_doc import AnnDoc, clean_cell, is_null_cell, load_any
from .announcement_schema import EVENT_FAMILY, SCHEMAS

ENGINE_VERSION = 'announcement-records-v1'


# ------------------------------------------------------------------ 字段构造

def fld(value, raw='', page=None, loc='', rule='', conf=1.0, **extra) -> dict:
    d = {'value': value, 'raw': raw, 'page': page, 'loc': loc, 'confidence': round(float(conf), 2), 'rule': rule}
    d.update(extra)
    return d


def null_fld(reason: str, rule='', raw='') -> dict:
    return {'value': None, 'raw': raw, 'page': None, 'loc': '', 'confidence': 0.0, 'rule': rule, 'reason': reason}


def dec_out(d: Decimal | None):
    """Decimal -> JSON 友好的 int/float（整数不带小数尾巴）。"""
    if d is None:
        return None
    if d == d.to_integral_value():
        return int(d)
    return float(format(d.normalize(), 'f'))


def norm_key(name: str) -> str:
    s = re.sub(r'[\s（）()·\-－—*]+', '', str(name or ''))
    return s


# ------------------------------------------------------------------ 文档级信息

_RE_CODE = re.compile(r'(?:证券|股票|A\s*股)代码[:：]?\s*([0-9]{5,6})')
_RE_ABBR = re.compile(r'(?:证券|股票|A\s*股)简称[:：]?\s*([^\s公编证股\d][^\s]{1,10}?)(?=\s*(?:公告编号|编号|证券|转债|债券|H股|$)|公告|临|\d{4}-|编号)')
_RE_NO = re.compile(r'(?:公告)?编号[:：]?\s*((?:临)?\s*\d{4}\s*[-—－]\s*\d{1,4}\s*号?)')
_RE_TOTAL = [
    re.compile(r'公司总股本(?:为|是|：|:)?\s*([\d,，]+(?:\.\d+)?)\s*(万股|亿股|股)'),
    re.compile(r'总股本(?:为|是|：|:)\s*([\d,，]+(?:\.\d+)?)\s*(万股|亿股|股)'),
]


def doc_info(doc: AnnDoc) -> dict:
    head = '\n'.join(p['text'] for p in doc.pages[:1])[:700]
    info = {}
    m = _RE_CODE.search(head)
    if m:
        info['证券代码'] = fld(m.group(1), m.group(0), 1, 'p1 页眉', 'R-DOC-CODE')
    m = _RE_ABBR.search(head)
    if m:
        info['证券简称'] = fld(re.sub(r'\s+', '', m.group(1)), m.group(0), 1, 'p1 页眉', 'R-DOC-ABBR', 0.9)
    m = _RE_NO.search(head)
    if m:
        info['公告编号'] = fld(re.sub(r'\s+', '', m.group(1)), m.group(0), 1, 'p1 页眉', 'R-DOC-NO')
    # 公告日期：文末落款最后一个日期
    tail = doc.pages[-1]['text'][-300:] if doc.pages else ''
    dates = ax._iter_dates(tail)
    if dates:
        pos, raw, iso = dates[-1]
        if iso:
            info['公告日期'] = fld(iso, raw, doc.pages[-1]['page'], '文末落款', 'R-DOC-DATE', 0.9)
    text = doc.text
    for rx in _RE_TOTAL:
        m = rx.search(text)
        if m:
            unit = {'股': 1, '万股': 10 ** 4, '亿股': 10 ** 8}[m.group(2)]
            v = ax._dec(m.group(1))
            if v is not None:
                info['总股本_股'] = fld(dec_out(v * unit), m.group(0), doc.page_at(m.start()), f'正文@{m.start()}', 'R-DOC-TOTAL', 0.95)
                break
    return info


# ------------------------------------------------------------------ 文档类型

def classify_doc(doc: AnnDoc, tables: list) -> tuple[str, str]:
    """返回 (family, 依据)。标题关键词优先，其次表格角色。"""
    head = doc.pages[0]['text'][:900] if doc.pages else ''
    lines = [ln for ln in head.split('\n') if ln.strip()]
    cand = [ln for ln in lines if not re.search(r'证券代码|证券简称|公告编号|股票代码|股票简称|编号：|A\s*股代码|H\s*股|债券|转债|本公司|保证', ln)]
    parts = []
    for ln in cand[:6]:
        parts.append(ln)
        if re.search(r'(?:公告|报告书|通知|公示|意见|承诺函|说明)\s*$', ln):
            break
    title = re.sub(r'\s+', '', ''.join(parts))[:200]
    if '权益变动报告书' in title or '收购报告书' in title:
        return 'equity_report', '标题含权益变动报告书'
    keys_all = [set(k for k in T.column_keys(t) if k) for t in tables]
    has_pledge_tab = any(k & {'ev_shares', 'ev_shares_rel'} for k in keys_all)
    has_freeze_tab = any('fz_shares' in k for k in keys_all)
    rules = [
        ('bid', r'中标|中选|候选人|成交'),
        ('freeze', r'冻结|拍卖|查封'),
        ('pledge', r'质押|解质'),
        ('contract', r'合同|协议书|框架协议|采购协议'),
        ('shareholding_change', r'减持|增持|权益变动|受让|转让|持股变动|一致行动'),
    ]
    hits = [(fam, m.start()) for fam, rx in rules for m in [re.search(rx, title)] if m]
    if hits:
        fams = {h[0] for h in hits}
        # 同时含质押与冻结：以表格角色定
        if 'pledge' in fams and 'freeze' in fams:
            if has_pledge_tab and not has_freeze_tab:
                return 'pledge', '标题含质押+冻结，表格为质押角色'
            if has_freeze_tab and not has_pledge_tab:
                return 'freeze', '标题含质押+冻结，表格为冻结角色'
            return 'pledge', '标题含质押+冻结，按质押处理'
        # 中标优先于合同（“重大合同中标进展”）
        for fam in ('bid', 'freeze', 'pledge', 'contract', 'shareholding_change'):
            if fam in fams:
                return fam, f'标题关键词({fam})'
    if has_pledge_tab:
        return 'pledge', '表格含质押列'
    if has_freeze_tab:
        return 'freeze', '表格含冻结列'
    return 'other', '未命中任何类型规则'


# ------------------------------------------------------------------ 表格 -> 事件

_CONT_INHERIT = {'holder', 'start', 'end', 'new_end', 'orig_end', 'pledgee', 'purpose', 'supp', 'rel_date', 'fz_applicant', 'fz_reason', 'fz_start', 'fz_end'}
_NUMERIC_KEYS = {'ev_shares', 'ev_shares_rel', 'fz_shares', 'hold_shares', 'cum_after'}


def _cell(table, r, c, rows=None):
    row = (rows or table.rows)[r]
    return row[c] if c < len(row) else ''


def _prepare_rows(table, keys):
    """展开合并单元格：续行(无股东名且有数值)继承上一行的身份类列。返回 [(row_idx, cells, inherited_cols)]"""
    out, last = [], None
    holder_col = keys.index('holder') if 'holder' in keys else None
    for r, row in enumerate(table.rows):
        cells = list(row) + [''] * (len(keys) - len(row))
        first_text = cells[0].strip() if cells else ''
        if T.is_total_label(first_text) or (holder_col is not None and T.is_total_label(cells[holder_col])):
            continue          # 合计行不是股东，也不是事件
        nonempty = [c for c in cells if c.strip() and not is_null_cell(c)]
        if not nonempty:
            continue
        holder_blank = holder_col is not None and not cells[holder_col].strip()
        inherited = set()
        if holder_blank and last is not None:
            # 续行：必须含数值列，否则可能是脚注/碎片
            has_num = any(keys[i] in _NUMERIC_KEYS and cells[i].strip() for i in range(len(keys)))
            if not has_num:
                continue
            for i, k in enumerate(keys):
                if k in _CONT_INHERIT and not cells[i].strip():
                    cells[i] = last[i]
                    inherited.add(i)
        elif last is not None and holder_col is not None and cells[holder_col].strip():
            pass
        out.append((r, cells, inherited))
        last = cells
    return out


def _title_hint(doc: AnnDoc) -> dict:
    head = ''.join(doc.pages[0]['text'].split('\n')[:8]) if doc.pages else ''
    return {'ext': bool(re.search(r'展期|延期', head)), 'supp': '补充质押' in head}


def build_pledge_family(doc: AnnDoc, tables: list, hint: dict):
    events, holdings, review = [], {}, []
    for t in tables:
        keys = T.column_keys(t)
        keyset = {k for k in keys if k}
        if not keyset & {'holder'} or not keyset & {'ev_shares', 'ev_shares_rel', 'hold_shares', 'cum_after', 'fz_shares'}:
            continue
        if 'fz_shares' in keyset:
            continue
        rows = _prepare_rows(t, keys)
        idx = {}
        for i, k in enumerate(keys):
            if k and k not in idx:
                idx[k] = i
        has_event = bool(keyset & {'ev_shares', 'ev_shares_rel'})
        has_hold = bool(keyset & {'hold_shares', 'cum_after'})
        transposed = getattr(t, 'transposed', False)
        for r, cells, inh in rows:
            page = t.row_pages[r] if r < len(t.row_pages) else t.page

            def get(key, kind='raw'):
                i = idx.get(key)
                return (i, cells[i]) if i is not None and i < len(cells) else (None, '')

            def loc(i):
                return f'表{t.table_id} 行{r + 1} 列{i + 1}「{t.labels[i]}」' + ('(合并单元格继承)' if i in inh else '')

            hname_i, hname = get('holder')
            name = T.clean_name(hname)
            if not name:
                continue
            name_f = fld(name, hname, page, loc(hname_i), 'R-TBL-HOLDER')
            if has_event:
                ev_key = 'ev_shares_rel' if 'ev_shares_rel' in idx else 'ev_shares'
                shares_i, shares_raw = get(ev_key)
                sh, why = T.parse_int_shares(shares_raw, t.labels[shares_i] if shares_i is not None else '')
                if sh is not None and sh != 0 or (sh == 0 and shares_raw.strip()):
                    ev_type = 'release' if ev_key == 'ev_shares_rel' else 'pledge'
                    supp_i, supp_raw = get('supp')
                    if ev_type == 'pledge':
                        is_ext = bool(keyset & {'new_end', 'orig_end'}) or bool(re.search(r'展期|延期', t.labels[shares_i]))
                        if not is_ext and hint['ext'] and not re.search(r'是', supp_raw or '') and not hint['supp']:
                            is_ext = True
                        if is_ext:
                            ev_type = 'extension'
                    f = {}
                    share_name = {'release': '解除质押股数_股', 'pledge': '质押股数_股', 'extension': '展期股数_股'}[ev_type]
                    f['股东名称'] = name_f
                    f[share_name] = fld(dec_out(sh), shares_raw, page, loc(shares_i), 'R-TBL-SHARES',
                                        1.0 if shares_i not in inh else 0.8)
                    for key, out in (('pct_h', '占所持比例_百分'), ('pct_t', '占总股本比例_百分')):
                        i, raw = get(key)
                        if i is None:
                            f[out] = null_fld('表格无该列', 'R-TBL-PCT')
                            continue
                        v, why2, sign = T.parse_pct(raw, t.labels[i])
                        f[out] = fld(dec_out(v), raw, page, loc(i), 'R-TBL-PCT', 1.0 if v is not None else 0.0) \
                            if v is not None else null_fld(why2 or 'empty', 'R-TBL-PCT', raw)
                    i, raw = get('pledgee')
                    pname = 'pledgee'
                    f['质权人'] = fld(T.clean_name(raw), raw, page, loc(i), 'R-TBL-PLEDGEE', 0.95 if i in inh else 1.0) if i is not None and raw.strip() and not is_null_cell(raw) else null_fld('表格无质权人', 'R-TBL-PLEDGEE')
                    i, raw = get('start')
                    f['质押起始日'] = _date_field(raw, page, loc(i) if i is not None else '', 'R-TBL-START')
                    if ev_type == 'release':
                        i, raw = get('rel_date')
                        f['解押日期'] = _date_field(raw, page, loc(i) if i is not None else '', 'R-TBL-RELDATE')
                    elif ev_type == 'extension':
                        i, raw = get('orig_end')
                        f['原质押到期日'] = _date_field(raw, page, loc(i) if i is not None else '', 'R-TBL-ORIGEND')
                        i2, raw2 = get('new_end') if 'new_end' in idx else get('end')
                        f['展期后到期日'] = _date_field(raw2, page, loc(i2) if i2 is not None else '', 'R-TBL-NEWEND')
                        i3, raw3 = get('purpose')
                        f['质押用途'] = _text_field(raw3, page, loc(i3) if i3 is not None else '', 'R-TBL-PURPOSE')
                    else:
                        i, raw = get('end')
                        f['质押到期日'] = _date_field(raw, page, loc(i) if i is not None else '', 'R-TBL-END')
                        i3, raw3 = get('purpose')
                        f['质押用途'] = _text_field(raw3, page, loc(i3) if i3 is not None else '', 'R-TBL-PURPOSE')
                        i4, raw4 = get('supp')
                        f['是否补充质押'] = fld(('是' if '是' in raw4 and '否' not in raw4 else '否'), raw4, page, loc(i4), 'R-TBL-SUPP') if i4 is not None and raw4.strip() else null_fld('表格无该列', 'R-TBL-SUPP')
                    events.append({'event_type': ev_type, 'fields': f, '_table': t.table_id, '_row': r})
            if has_hold:
                f = {'股东名称': name_f}
                spec = (('hold_shares', '持股数量_股', 'shares'), ('hold_pct', '持股比例_百分', 'pct'),
                        ('cum_after', '累计质押股数_股', 'shares'))
                for key, out, kind in spec:
                    i, raw = get(key)
                    if i is None:
                        continue
                    if kind == 'shares':
                        v, why = T.parse_int_shares(raw, t.labels[i])
                    else:
                        v, why, _ = T.parse_pct(raw, t.labels[i])
                    f[out] = fld(dec_out(v), raw, page, loc(i), 'R-TBL-HOLD', 1.0) if v is not None else null_fld(why or 'empty', 'R-TBL-HOLD', raw)
                # 累计占比：显式 cum_pct_* 优先；否则（表内无事件列）通用 pct_h/pct_t 即累计占比
                for out, ck, gk in (('累计占所持比例_百分', 'cum_pct_h', 'pct_h'), ('累计占总股本比例_百分', 'cum_pct_t', 'pct_t')):
                    key = ck if ck in idx else (gk if (not has_event and gk in idx) else None)
                    if key is None:
                        continue
                    i, raw = get(key)
                    v, why, _ = T.parse_pct(raw, t.labels[i])
                    f[out] = fld(dec_out(v), raw, page, loc(i), 'R-TBL-CUMPCT', 1.0) if v is not None else null_fld(why or 'empty', 'R-TBL-CUMPCT', raw)
                if len(f) > 1:
                    prio = 1 if has_event else 2          # 含事件列的键值表(剩余被质押)优先级低于专门的累计表
                    key = norm_key(name)
                    for k0 in holdings:                    # 简称/全称视为同一股东（西藏永冠 / 西藏永冠投资有限公司）
                        if k0 == key or (min(len(k0), len(key)) >= 3 and (k0 in key or key in k0)):
                            key = k0
                            break
                    prev = holdings.get(key)
                    if prev is None:
                        holdings[key] = {'event_type': 'holding', 'fields': f, '_table': t.table_id, '_row': r, '_prio': prio}
                    else:
                        if prio > prev['_prio']:
                            merged = dict(f)
                            for k, v in prev['fields'].items():
                                if merged.get(k, {}).get('value') is None and v.get('value') is not None:
                                    merged[k] = v
                            prev['fields'], prev['_prio'] = merged, prio
                        else:
                            for k, v in f.items():
                                if prev['fields'].get(k, {}).get('value') is None and v.get('value') is not None:
                                    prev['fields'][k] = v
                        if len(name) > len(prev['fields']['股东名称']['value']):
                            prev['fields']['股东名称'] = name_f
    return events, list(holdings.values()), review


def _date_field(raw, page, loc, rule):
    raw = clean_cell(raw)
    if not raw:
        return null_fld('表格该格为空', rule)
    if is_null_cell(raw):
        return null_fld('原文以“—”表示无', rule, raw)
    iso = T.parse_date(raw)
    if iso:
        return fld(iso, raw, page, loc, rule, 1.0)
    return null_fld(f'原文为“{raw}”，不是具体日期', rule, raw)


def _text_field(raw, page, loc, rule):
    raw = clean_cell(raw)
    if not raw or is_null_cell(raw):
        return null_fld('表格无该项', rule, raw)
    return fld(raw, raw, page, loc, rule, 1.0)


# ------------------------------------------------------------------ 勾稽

def _v(rec, name):
    f = rec['fields'].get(name)
    return f.get('value') if isinstance(f, dict) else None


def cross_checks(events: list, holdings: list, info: dict) -> list:
    """跨字段勾稽。不符只进 review，不改数。"""
    out = []
    total = info.get('总股本_股', {}).get('value')
    by_holder = {norm_key(_v(h, '股东名称')): h for h in holdings}

    def short(name):
        key = norm_key(name)
        if key in by_holder:
            return by_holder[key]
        for k, h in by_holder.items():
            if k and key and (k in key or key in k) and min(len(k), len(key)) >= 2:
                return h
        return None

    def issue(level, code, msg, rec=None, fields=()):
        out.append({'level': level, 'code': code, 'message': msg,
                    'holder': _v(rec, '股东名称') if rec else None, 'fields': list(fields)})

    # 隐含总股本：优先正文，其次由持股数/持股比例反推（取中位数）
    implied = []
    for h in holdings:
        hs, hp = _v(h, '持股数量_股'), _v(h, '持股比例_百分')
        if hs and hp:
            implied.append(float(hs) / float(hp) * 100)
    if total is None and implied:
        implied.sort()
        total = implied[len(implied) // 2]
    for rec in events:
        kind = rec['event_type']
        sh = _v(rec, {'release': '解除质押股数_股', 'pledge': '质押股数_股', 'extension': '展期股数_股'}[kind])
        pt, ph = _v(rec, '占总股本比例_百分'), _v(rec, '占所持比例_百分')
        for name in ('占总股本比例_百分', '占所持比例_百分'):
            v = _v(rec, name)
            if v is not None and not (0 <= v <= 100.0001):
                issue('error', 'PCT_RANGE', f'{name}={v} 不在 0–100', rec, [name])
        if sh and pt is not None and total:
            calc = float(sh) / float(total) * 100
            if abs(calc - float(pt)) > max(0.02, 0.01 * float(pt)):
                issue('warn', 'PCT_TOTAL_MISMATCH', f'股数/总股本={calc:.2f}% 与披露的占总股本比例 {pt}% 不一致', rec, [name for name in ('占总股本比例_百分',)])
        h = short(_v(rec, '股东名称'))
        if sh and ph is not None and h and _v(h, '持股数量_股'):
            calc = float(sh) / float(_v(h, '持股数量_股')) * 100
            if abs(calc - float(ph)) > max(0.02, 0.01 * float(ph)):
                issue('warn', 'PCT_HOLD_MISMATCH', f'股数/持股数={calc:.2f}% 与披露的占其所持比例 {ph}% 不一致', rec, ['占所持比例_百分'])
        if kind == 'pledge' and h and _v(h, '累计质押股数_股') is not None and sh and float(_v(h, '累计质押股数_股')) + 1 < float(sh):
            issue('error', 'CUM_LT_EVENT', f'累计质押 {_v(h, "累计质押股数_股")} 小于本次质押 {sh}', rec, ['累计质押股数_股'])
        s, e = _v(rec, '质押起始日'), _v(rec, '质押到期日') or _v(rec, '展期后到期日')
        if s and e and e <= s:
            issue('error', 'END_BEFORE_START', f'到期日 {e} 不晚于起始日 {s}', rec, ['质押到期日'])
        if kind == 'release':
            rd = _v(rec, '解押日期')
            if s and rd and rd < s:
                issue('error', 'RELEASE_BEFORE_START', f'解押日 {rd} 早于起始日 {s}', rec, ['解押日期'])
    for h in holdings:
        hs, hc = _v(h, '持股数量_股'), _v(h, '累计质押股数_股')
        if hs is not None and hc is not None and float(hc) > float(hs) + 1:
            issue('error', 'CUM_GT_HOLD', f'累计质押 {hc} 大于持股 {hs}', h, ['累计质押股数_股'])
        for n in ('持股比例_百分', '累计占所持比例_百分', '累计占总股本比例_百分'):
            v = _v(h, n)
            if v is not None and not (0 <= v <= 100.0001):
                issue('error', 'PCT_RANGE', f'{n}={v} 不在 0–100', h, [n])
        if hs and hc is not None and _v(h, '累计占所持比例_百分') is not None:
            calc = float(hc) / float(hs) * 100
            if abs(calc - float(_v(h, '累计占所持比例_百分'))) > max(0.02, 0.01 * calc):
                issue('warn', 'CUM_PCT_MISMATCH', f'累计/持股={calc:.2f}% 与披露 {_v(h, "累计占所持比例_百分")}% 不一致', h, ['累计占所持比例_百分'])
    return out


def sum_checks(tables: list) -> list:
    """合计行 = 分项和（对表头含“本次/股数”的事件表）。"""
    out = []
    for t in tables:
        keys = T.column_keys(t)
        idx = {k: i for i, k in reversed(list(enumerate(keys))) if k}
        for key in ('ev_shares', 'ev_shares_rel'):
            if key not in idx:
                continue
            col = idx[key]
            total_row = None
            parts = []
            for row in t.rows:
                cells = list(row) + [''] * (len(keys) - len(row))
                if T.is_total_label(cells[0]) or T.is_total_label(cells[idx.get('holder', 0)]):
                    total_row = cells
                elif cells[col].strip():
                    v, _ = T.parse_int_shares(cells[col], t.labels[col])
                    if v is not None:
                        parts.append(v)
            if total_row is not None and parts:
                tv, _ = T.parse_int_shares(total_row[col], t.labels[col])
                if tv is not None and tv != sum(parts):
                    out.append({'level': 'warn', 'code': 'TOTAL_ROW_MISMATCH',
                                'message': f'表{t.table_id}合计 {tv} ≠ 分项和 {sum(parts)}（可能跨页断行或四舍五入）',
                                'holder': None, 'fields': []})
    return out


# ------------------------------------------------------------------ 冻结

def build_freeze(doc: AnnDoc, tables: list):
    events = []
    for t in tables:
        keys = T.column_keys(t)
        keyset = {k for k in keys if k}
        if 'fz_shares' not in keyset or 'holder' not in keyset:
            continue
        idx = {}
        for i, k in enumerate(keys):
            if k and k not in idx:
                idx[k] = i
        for r, cells, inh in _prepare_rows(t, keys):
            page = t.row_pages[r] if r < len(t.row_pages) else t.page

            def get(key):
                i = idx.get(key)
                return (i, cells[i]) if i is not None and i < len(cells) else (None, '')

            def loc(i):
                return f'表{t.table_id} 行{r + 1} 列{i + 1}「{t.labels[i]}」' + ('(合并单元格继承)' if i in inh else '')
            hi, hraw = get('holder')
            name = T.clean_name(hraw)
            if not name:
                continue
            f = {'股东名称': fld(name, hraw, page, loc(hi), 'R-FZ-HOLDER')}
            i, raw = get('fz_shares')
            v, why = T.parse_int_shares(raw, t.labels[i])
            if v is None:
                continue
            f['冻结股数_股'] = fld(dec_out(v), raw, page, loc(i), 'R-FZ-SHARES', 1.0 if i not in inh else 0.8)
            for key, out in (('pct_h', '占所持比例_百分'), ('pct_t', '占总股本比例_百分')):
                i, raw = get(key)
                if i is None:
                    f[out] = null_fld('表格无该列', 'R-FZ-PCT')
                    continue
                pv, why2, _ = T.parse_pct(raw, t.labels[i])
                f[out] = fld(dec_out(pv), raw, page, loc(i), 'R-FZ-PCT') if pv is not None else null_fld(why2, 'R-FZ-PCT', raw)
            sk = 'fz_start' if 'fz_start' in idx else 'start'
            ek = 'fz_end' if 'fz_end' in idx else 'end'
            i, raw = get(sk)
            f['起始日'] = _date_field(raw, page, loc(i) if i is not None else '', 'R-FZ-START')
            i, raw = get(ek)
            f['到期日'] = _date_field(raw, page, loc(i) if i is not None else '', 'R-FZ-END')
            i, raw = get('fz_applicant')
            f['冻结申请人'] = _text_field(raw, page, loc(i) if i is not None else '', 'R-FZ-APPLICANT')
            i, raw = get('fz_reason')
            ftype = _text_field(raw, page, loc(i) if i is not None else '', 'R-FZ-TYPE')
            f['冻结类型'] = ftype
            events.append({'event_type': 'freeze', 'fields': f, '_table': t.table_id, '_row': r})
    return events


# ------------------------------------------------------------------ 总入口

def _strip_private(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if not k.startswith('_')}


def extract_doc(doc: AnnDoc) -> dict:
    tables = T.normalize_tables(doc.tables)
    info = doc_info(doc)
    family, why = classify_doc(doc, tables)
    hint = _title_hint(doc)
    events, holdings, review = [], [], []
    if family in ('pledge', 'freeze', 'other'):
        ev, hd, rv = build_pledge_family(doc, tables, hint)
        events.extend(ev)
        holdings.extend(hd)
        review.extend(rv)
    if family in ('freeze', 'other'):
        events.extend(build_freeze(doc, tables))
    from . import announcement_narrative as N
    extra = N.extract_narrative(doc, tables, family, info, events)
    events.extend(extra['events'])
    holdings.extend(extra.get('holdings', []))
    review.extend(extra.get('review', []))
    family = extra.get('family', family)
    records = [_strip_private(r) for r in events + holdings]
    checks = cross_checks([r for r in events if r['event_type'] in ('pledge', 'release', 'extension')], holdings, info)
    checks += sum_checks([t for t in tables])
    checks += extra.get('checks', [])
    return {
        'engine': ENGINE_VERSION,
        'filename': doc.filename,
        'source_kind': doc.source_kind,
        'doc_type': family,
        'doc_type_reason': why,
        'doc': info,
        'records': records,
        'checks': checks,
        'review': review + [c for c in checks if c['level'] in ('warn', 'error')],
        'warnings': list(doc.warnings),
        'tables_seen': [t.table_id for t in tables],
    }


def extract_file(path: str | Path) -> dict:
    doc = load_any(path)
    res = extract_doc(doc)
    res['source_sha256'] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return res


def to_json(result: dict) -> str:
    """稳定序列化：同一输入逐字节一致。"""
    return json.dumps(result, ensure_ascii=False, indent=1, sort_keys=False, default=str)
