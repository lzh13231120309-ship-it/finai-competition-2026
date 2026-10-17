"""内部一致性适配层：把"研报自身能否自洽"的各类确定性复算汇总成统一结构。

依赖隔离：errorcheck 工作流（report_formula_review / report_audit 等）只在本文件被调用，
其接口一旦变化只需改这里，report_quality_v2 与 report_features 不受影响。
复算对象全部来自报告原文自身（不引入外部数据）：
  1. 显式公式（A×B=C）            —— 调 report_formula_review.review_formulas
  2. 同比增长率复算（由 a 增至 b，同比 p%）
  3. 目标价 = 倍数 × EPS
  4. 目标价/现价/上涨空间三者自洽
  5. 资产 = 负债 + 所有者权益
  6. 同一指标同一期间在全文出现多个不同数值（"疑似冲突"，只进复核队列，不单独触发一票否决）
每条结果带原文片段、行号、规则 ID；"确认错误"=用报告自己给出的操作数复算后不符。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .report_text import normalize_line

NUM = r'\d[\d,]*(?:\.\d+)?'
_UNIT_SCALE = {'亿元': Decimal(1e8), '亿': Decimal(1e8), '百万元': Decimal(1e6), '万元': Decimal(1e4), '千元': Decimal(1e3), '元': Decimal(1),
               '亿美元': Decimal(1e8), '万亿元': Decimal(1e12)}


def _d(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace(',', ''))
    except (InvalidOperation, AttributeError):
        return None


def _decimals(s: str) -> int:
    return len(s.split('.')[1]) if '.' in s else 0


def _snip(t: str, n: int = 90) -> str:
    return t if len(t) <= n else t[:n] + '…'


def normalize_for_checks(text: str) -> list[tuple[int, str]]:
    """逐行规范化但保留原始行号。"""
    out = []
    for i, ln in enumerate(text.replace('\r\n', '\n').replace('\r', '\n').replace('\f', '\n').split('\n'), 1):
        t = normalize_line(ln)
        if t:
            out.append((i, t))
    return out


def _sentences(lines: list[tuple[int, str]]):
    """把相邻行拼成句（PDF 折行）：以句末标点切分，保留起始行号。"""
    buf, start = '', 0
    for no, t in lines:
        if not buf:
            start = no
        buf += t
        if re.search(r'[。！？；]$', t) or len(buf) > 400:
            for s in re.split(r'(?<=[。！？；])', buf):
                if s.strip():
                    yield start, s.strip()
            buf = ''
    if buf:
        for s in re.split(r'(?<=[。！？；])', buf):
            if s.strip():
                yield start, s.strip()


GROW_UP = '增长|增加|提升|上升|增至|提高|上涨|增|升'
GROW_DN = '下降|下滑|降低|减少|降至|下跌|降|减|萎缩'
# F1  由 a 增至 b，同比 p%
F1 = re.compile(rf'(?:由|从)\s*(?P<a>{NUM})\s*(?P<u1>亿元|万元|百万元|万吨|万辆|亿美元|亿|元|%)?\s*(?P<dir>{GROW_UP}|{GROW_DN})(?:至|到|为)\s*(?P<b>{NUM})\s*(?P<u2>亿元|万元|百万元|万吨|万辆|亿美元|亿|元|%)?[^。；]{{0,12}}?(?:，|,)?\s*(?:同比|较上年|较去年|增速|增幅|降幅|变动)\s*(?P<d2>{GROW_UP}|{GROW_DN}|[+\-−])?\s*(?P<p>{NUM})\s*%')
# F2  b ，同比 p%（上年同期 a）
F2 = re.compile(rf'(?P<b>{NUM})\s*(?P<u2>亿元|万元|百万元|万吨|万辆|亿美元|亿|元)[^。；]{{0,16}}?同比\s*(?P<d2>{GROW_UP}|{GROW_DN}|[+\-−])?\s*(?P<p>{NUM})\s*%[^。；]{{0,12}}?[（(]?(?:上年同期|去年同期|上年|去年同期值|上期)[为：:]?\s*(?P<a>{NUM})\s*(?P<u1>亿元|万元|百万元|万吨|万辆|亿美元|亿|元)?')
UPSIDE = re.compile(rf'(?:现价|收盘价|当前(?:股)?价|最新(?:收盘)?价|股价)[^。；，]{{0,6}}?(?P<px>{NUM})\s*元[^。；]{{0,30}}?目标价[^。；，]{{0,4}}?(?P<tp>{NUM})\s*元[^。；]{{0,30}}?(?:上涨|上行|涨幅|空间|潜在)[^。；]{{0,6}}?(?P<up>{NUM})\s*%')
PE_EPS = re.compile(rf'(?P<pe>{NUM})\s*倍\s*(?:20\d{{2}}\s*年?\s*)?(?:PE|市盈率|P/E)?[^。；]{{0,24}}?EPS[^。；0-9]{{0,8}}(?P<eps>{NUM})\s*元[^。；]{{0,30}}?目标价[^。；0-9]{{0,5}}(?P<tp>{NUM})\s*元')
PE_EPS2 = re.compile(rf'EPS[^。；0-9]{{0,8}}(?P<eps>{NUM})\s*元[^。；]{{0,24}}?(?P<pe>{NUM})\s*倍[^。；]{{0,30}}?目标价[^。；0-9]{{0,5}}(?P<tp>{NUM})\s*元')
BAL = re.compile(rf'(?:资产总计|总资产)[为：:]?\s*(?P<a>{NUM})\s*(?P<u>亿元|万元|百万元|元)[^。；]{{0,30}}?(?:负债(?:合计|总额|总计)?)[为：:]?\s*(?P<l>{NUM})\s*(?:亿元|万元|百万元|元)?[^。；]{{0,30}}?(?:所有者权益|股东权益|净资产)(?:合计)?[为：:]?\s*(?P<e>{NUM})\s*(?:亿元|万元|百万元|元)?')
METRIC_VAL = re.compile(rf'(?P<p>(?:19|20)\d{{2}}\s*年?(?:全年|上半年|前三季度|一季度|[1-4]Q|H[12])?|\d{{2}}[AE])(?:公司)?(?:实现|录得|达到|预计|预测|为)?(?P<m>营业收入|营收|归母净利润|扣非归母净利润|净利润|毛利率|净利率|经营(?:活动)?现金流净额|EPS|每股收益)(?:同比)?(?:约|达|为|至|实现|预计)?\s*(?P<v>{NUM})\s*(?P<u>亿元|万元|百万元|%|元)')


def _tol_pct(a: str, b: str) -> Decimal:
    """a,b 为带舍入的原文字符串，返回增速允许误差（百分点）。"""
    da, db = _d(a), _d(b)
    if not da or not db:
        return Decimal('1')
    ea = Decimal('0.5') * Decimal(10) ** (-_decimals(a)) / abs(da)
    eb = Decimal('0.5') * Decimal(10) ** (-_decimals(b)) / abs(db)
    return (ea + eb) * (db / da) * 100 + Decimal('0.06')


def internal_consistency(text: str, review_formulas=None) -> dict:
    lines = normalize_for_checks(text)
    sents = list(_sentences(lines))
    passes, failures, suspected = [], [], []

    def rec(ok, rule, line, sent, detail):
        item = {'rule': rule, 'line': line, 'text': _snip(sent), 'detail': detail, 'confirmed': True}
        (passes if ok else failures).append(item)

    for line, s in sents:
        for rx in (F1, F2):
            for m in rx.finditer(s):
                if re.search(r'复合|CAGR|年均|平均增', s[max(0, m.start() - 8):m.end() + 14]):
                    continue
                a, b, p = _d(m['a']), _d(m['b']), _d(m['p'])
                if not a or b is None or p is None or a == 0:
                    continue
                u1, u2 = m.groupdict().get('u1'), m.groupdict().get('u2')
                if u1 and u2 and u1 != u2 and not ({u1, u2} <= {'亿元', '亿'}):
                    continue
                calc = (b / a - 1) * 100
                d2 = m.groupdict().get('d2') or ''
                dn = bool(re.fullmatch(GROW_DN, d2)) or d2 in ('-', '−')
                dir_word = m.groupdict().get('dir')
                if rx is F1 and dir_word and re.fullmatch(GROW_DN, dir_word) and not d2:
                    dn = True
                stated = -p if dn else p
                if rx is F1 and dir_word and re.fullmatch(GROW_DN, dir_word) and calc > 0:
                    rec(False, 'K.growth', line, s, f'文字为“{dir_word}”，但 {m["b"]} 高于 {m["a"]}（复算{calc:+.1f}%）')
                    continue
                if rx is F1 and dir_word and re.fullmatch(GROW_UP, dir_word) and calc < 0:
                    rec(False, 'K.growth', line, s, f'文字为“{dir_word}”，但 {m["b"]} 低于 {m["a"]}（复算{calc:+.1f}%）')
                    continue
                tol = _tol_pct(m['a'], m['b'])
                ok = abs(calc - stated) <= tol
                rec(ok, 'K.growth', line, s, f'由 {m["a"]} 到 {m["b"]} 复算同比 {calc:+.2f}%，原文 {stated:+}%（容差±{tol:.2f}pp）')
    for line, s in sents:
        for rx in (PE_EPS, PE_EPS2):
            m = rx.search(s)
            if m:
                pe, eps, tp = _d(m['pe']), _d(m['eps']), _d(m['tp'])
                if pe and eps and tp:
                    calc = pe * eps
                    rel = Decimal('0.5') / pe + Decimal('0.005') / eps + Decimal('0.005') / tp + Decimal('0.01')
                    ok = abs(calc - tp) / tp <= rel
                    rec(ok, 'K.target', line, s, f'{m["pe"]}倍×EPS {m["eps"]} = {calc:.2f}，原文目标价 {m["tp"]}（容差±{rel*100:.1f}%）')
                break
        m = UPSIDE.search(s)
        if m:
            px, tp, up = _d(m['px']), _d(m['tp']), _d(m['up'])
            if px and tp and up is not None and px > 0:
                calc = (tp / px - 1) * 100
                tol = Decimal('0.5') / tp * 100 + Decimal('0.5') / px * 100 + Decimal('0.6')
                rec(abs(calc - up) <= tol, 'K.upside', line, s, f'目标价{m["tp"]}/现价{m["px"]}-1={calc:.1f}%，原文空间 {m["up"]}%')
        m = BAL.search(s)
        if m:
            a, l, e = _d(m['a']), _d(m['l']), _d(m['e'])
            if a and l is not None and e is not None:
                tol = Decimal('0.5') * Decimal(10) ** (-max(_decimals(m['a']), _decimals(m['l']), _decimals(m['e']))) * 2 + a * Decimal('0.002')
                rec(abs(a - l - e) <= tol, 'K.balance', line, s, f'资产 {m["a"]} vs 负债+权益 {l + e}')
    t_pass, t_fail, t_n = _table_checks(lines)
    passes += t_pass
    failures += t_fail
    # 显式公式
    f_checks = f_bad = f_pass = []
    if review_formulas is None:
        try:
            from .report_formula_review import review_formulas as review_formulas  # errorcheck 适配点
        except Exception:  # pragma: no cover
            review_formulas = None
    if review_formulas:
        try:
            fr = review_formulas('\n'.join(t for _, t in lines) if False else _formula_text(text))
        except Exception:  # pragma: no cover
            fr = {'checks': []}
        f_checks = [c for c in fr.get('checks', []) if c.get('status') in ('pass', 'fail')]
        f_pass = [{'rule': 'K.formula', 'line': c['line'], 'text': _snip(c['expression']), 'detail': f"复算 {c['calculated_value']} vs 原文 {c['stated_value']}"} for c in f_checks if c['status'] == 'pass']
        f_bad = [{'rule': 'K.formula', 'line': c['line'], 'text': _snip(c['expression']), 'detail': f"复算 {c['calculated_value']} ≠ 原文 {c['stated_value']}（容差 {c['tolerance']}）"} for c in f_checks if c['status'] == 'fail']
    # 疑似冲突
    seen: dict = {}
    for line, s in sents:
        for m in METRIC_VAL.finditer(s):
            pre = s[max(0, m.start() - 14):m.start()]
            if re.search(r'业务|板块|产品|分部|系列|子公司|品类|[-–—至、/]$', pre + '') or re.match(r'[、,/]', s[m.end():m.end() + 1]):
                continue
            per = m['p'].replace(' ', '')
            per = re.sub(r'年$', '', per)
            key = (re.sub(r'扣非归母净利润|归母净利润', '净利润', m['m']).replace('营收', '营业收入').replace('每股收益', 'EPS'), per, m['u'] if m['u'] == '%' else 'amt', 'E' if re.search(r'预计|预测|[0-9]{2}E|E$', m['p'] + s[max(0, m.start() - 4):m.start()]) else 'A')
            v = _d(m['v'])
            if v is None:
                continue
            if m['u'] != '%':
                v = v * _UNIT_SCALE.get(m['u'], Decimal(1))
            seen.setdefault(key, []).append((v, m['v'], m['u'], line, s))
    for key, vals in seen.items():
        base = vals[0]
        for v in vals[1:]:
            ref = max(abs(base[0]), abs(v[0]))
            if ref == 0:
                continue
            if abs(base[0] - v[0]) / ref > Decimal('0.02') and abs(base[0] - v[0]) > (Decimal('0.5') if key[2] == '%' else 0):
                suspected.append({'rule': 'K.conflict', 'line': v[3], 'text': _snip(v[4]),
                                  'detail': f'{key[0]} {key[1]}：第{base[3]}行为 {base[1]}{base[2]}，第{v[3]}行为 {v[1]}{v[2]}'})
                break
    return {'checks': len(passes) + len(failures), 'passes': passes, 'failures': failures,
            'formula_checked': len(f_checks), 'formula_failed': len(f_bad), 'formula_hits': f_pass, 'formula_bad': f_bad,
            'suspected': suspected[:10], 'balance_failures': [x for x in failures if x['rule'] == 'K.balance'],
            'confirmed_errors': [x for x in failures if x.get('confirmed', True)] + [dict(x, confirmed=True) for x in f_bad]}


YEAR_TOK = re.compile(r'^(?:(?:19|20)\d{2}\s*年?\s*[AEae]?|\d{2}\s*[AE]|[HQ][1-4]|(?:19|20)\d{2}[AaEe])$')
CELL_NUM = re.compile(r'^[-+−–]?\(?[\d,]+(?:\.\d+)?\)?\s*[%％xX倍]?$|^[-–—]$|^n/?a$', re.I)
GROW_LABEL = re.compile(r'同比|增长率|增速|YoY|yoy')


def _val(c: str):
    c = c.strip()
    if c in ('-', '–', '—') or c.lower().startswith('n'):
        return None
    neg = c.startswith(('-', '−', '–', '(')) or c.startswith('（')
    d = _d(re.sub(r'[^\d.,]', '', c))
    if d is None:
        return None
    return -d if neg else d


def _parse_tables(lines):
    """把“年份表头 + 逐行标签/数值”的 PDF 表格抽成 [(label, [values], raw_cells, line)]。"""
    tables = []
    i, n_l = 0, len(lines)
    while i < n_l:
        ln, t = lines[i]
        toks = t.split()
        if len(toks) >= 3 and all(YEAR_TOK.match(x) for x in toks):
            hdr, j = toks, i + 1
        elif YEAR_TOK.match(t):
            j = i
            hdr = []
            while j < n_l and YEAR_TOK.match(lines[j][1]):
                hdr.append(lines[j][1]); j += 1
            if len(hdr) < 3:
                i += 1
                continue
        else:
            i += 1
            continue
        n = len(hdr)
        rows, label, label_line, cells = [], None, 0, []
        miss = 0
        k = j
        while k < n_l and miss < 4:
            ln2, t2 = lines[k]
            parts = t2.split()
            if len(parts) > n and all(CELL_NUM.match(x) or not re.search(r'\d', x) for x in parts[-n:]) and re.search(r'[\u4e00-\u9fff A-Za-z/（）()%]', ' '.join(parts[:-n])):
                if label is not None and len(cells) >= n:
                    rows.append((label, cells[:n], label_line))
                label, label_line, cells = ' '.join(parts[:-n]), ln2, parts[-n:]
                rows.append((label, cells[:n], label_line)); label, cells = None, []
            elif CELL_NUM.match(t2):
                cells.append(t2)
                if label is not None and len(cells) == n:
                    rows.append((label, cells[:n], label_line)); label, cells = None, []
            elif re.search(r'[\u4e00-\u9fff]', t2) and len(t2) <= 24 and not re.search(r'[。；]', t2):
                label, label_line, cells = t2, ln2, []
                miss = 0
            else:
                miss += 1
                if len(t2) > 30:
                    break
            k += 1
        if rows:
            tables.append(rows)
        i = max(k, i + 1)
    return tables


_SYN = [(r'(?:营业总收入|营业收入|销售收入|主营业务收入|营业总收入合计|收入|营收)', '营收'),
        (r'(?:归属于母公司(?:股东的)?净利润|归属母公司(?:股东)?净利润|归母净利润|归母净利)', '归母净利润'),
        (r'(?:净利润|净利)', '净利润'), (r'(?:毛利|营业利润|利润总额|EBITDA)', None)]


def _core(name: str) -> str:
    n = re.sub(r'同比增长率|同比增速|同比增长|增长率|增速|同比|YoY|yoy|[%％()（）\s]|[百万千亿元万]+(?=$)', '', name)
    n = re.sub(r'^[(（]?(?:百万元|亿元|万元)[)）]?', '', n)
    for k, v in _SYN:
        if re.fullmatch(k, n):
            return v or n
    return n


def _find_amount_row(parsed, idx):
    nm, label, cells, vals, ln = parsed[idx]
    core = _core(nm)
    prev = parsed[idx - 1]
    pn = prev[0]
    if not core:   # “同比(%)” 紧跟金额行
        if GROW_LABEL.search(pn) or re.search(r'率|比|ROE|ROIC|P/?E|P/?B|倍', pn):
            return None
        return prev
    for cand in reversed(parsed[:idx]):
        if GROW_LABEL.search(cand[0]) or re.search(r'率|比|ROE|ROIC|P/?E|P/?B', cand[0]) and '增' not in cand[0]:
            continue
        if _core(cand[0]) == core:
            return cand
    return None


def _tol(a, b):
    ea = Decimal('0.5') * Decimal(10) ** (-0) if False else None
    return None


def _table_checks(lines):
    passes, failures, n = [], [], 0
    for rows in _parse_tables(lines):
        parsed = []
        for label, cells, ln in rows:
            parsed.append((re.sub(r'[（(].*?[）)]', '', label).strip(), label, cells, [_val(c) for c in cells], ln))
        for idx, (nm, label, cells, vals, ln) in enumerate(parsed):
            if GROW_LABEL.search(nm) and idx > 0:
                if re.search(r'右轴|左轴|图', label):
                    continue
                prev = _find_amount_row(parsed, idx)
                if prev is None or any('%' in c for c in prev[2]):
                    continue
                pn, plabel, pcells, pvals, pln = prev
                pct = '%' in label or '％' in label or any('%' in c for c in cells)
                if not pct:
                    continue
                for i in range(1, len(vals)):
                    a, b, g = pvals[i - 1], pvals[i], vals[i]
                    if a is None or b is None or g is None or a <= 0 or b < 0:
                        continue
                    calc = (b / a - 1) * 100
                    tol = _tol_pct(re.sub(r'[^\d.]', '', pcells[i - 1]), re.sub(r'[^\d.]', '', pcells[i])) + Decimal(str(0.5 * 10 ** -_decimals(re.sub(r'[^\d.]', '', cells[i]))))
                    ok = abs(calc - g) <= tol
                    n += 1
                    item = {'rule': 'K.table_growth', 'line': ln, 'text': f'{plabel} → {label}：第{i+1}列 {pcells[i-1]}→{pcells[i]}，表内同比 {cells[i]}',
                            'detail': f'复算 {calc:.2f}% vs 表内 {g}%（容差±{tol:.2f}pp）'}
                    (passes if ok else failures).append(item)
        eps = next((p for p in parsed if re.search(r'EPS|每股收益', p[0]) and not GROW_LABEL.search(p[0])), None)
        pe = next((p for p in parsed if re.search(r'P/?E|市盈率', p[0], re.I) and not GROW_LABEL.search(p[0])), None)
        if eps and pe:
            prods = []
            for ce, ve, cp, vp in zip(eps[2], eps[3], pe[2], pe[3]):
                if ve and vp and ve > 0 and vp > 0:
                    rel = Decimal('0.5') * Decimal(10) ** (-_decimals(re.sub(r'[^\d.]', '', ce))) / ve + Decimal('0.5') * Decimal(10) ** (-_decimals(re.sub(r'[^\d.]', '', cp))) / vp
                    prods.append((ve * vp, rel))
            if len(prods) >= 3:
                lo = min(p for p, _ in prods); hi = max(p for p, _ in prods)
                tol = max(r for _, r in prods) * 2 + Decimal('0.005')
                n += 1
                ok = (hi - lo) / hi <= tol
                item = {'rule': 'K.table_pe_eps', 'line': pe[4], 'text': f'{eps[1]} × {pe[1]}', 'detail': f'各年 EPS×PE（即隐含股价）范围 {lo:.2f}–{hi:.2f}，相对离散 {(hi-lo)/hi*100:.1f}%（容差 {tol*100:.1f}%）'}
                (passes if ok else failures).append(item)
    by_row: dict = {}
    for f in failures:
        if f['rule'] == 'K.table_growth':
            by_row.setdefault(f['text'].split('：')[0], []).append(f)
    for f in failures:
        f['confirmed'] = False
        if f['rule'] == 'K.table_growth':
            grp = by_row[f['text'].split('：')[0]]
            sig = all(len(re.sub(r'\D', '', x).lstrip('0')) >= 3 for g in grp for pair in re.findall(r'(\d[\d,.]*)→(\d[\d,.]*)', g['text']) for x in pair)
            f['confirmed'] = len(grp) >= 2 and sig
        elif f['rule'] == 'K.table_pe_eps':
            f['confirmed'] = bool(re.search(r'相对离散 (?:1[5-9]|[2-9]\d)', f['detail']))
    return passes, failures, n


def _formula_text(text: str) -> str:
    # 逐行规范化但保留空行，使公式复核报告的行号等于原文行号
    return '\n'.join(normalize_line(l) for l in text.replace('\r\n', '\n').replace('\r', '\n').replace('\f', '\n').split('\n'))
