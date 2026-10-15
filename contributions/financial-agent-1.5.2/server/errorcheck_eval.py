"""纠错核查评测器：把"勘误清单(findings)"与"注入金标准(gold)"对账，输出分类别指标。

指标定义（每份草稿独立匹配，再汇总）：
- 定位召回 recall_loc：金标准错误被某条 finding 定位到（引用文字与注入点重合且含注入值）
- 类别召回 recall_cat：定位到且类别一致（revenue/profit 视作同一"金额"族；其余需一致）
- 精确率 precision：finding 中"命中金标准"的比例（同一金标准被多条 finding 命中只算一条 TP；
  命中自然噪声清单的 finding 不算误报也不算 TP；命中 fp_controls（正确句）或未命中任何东西 = 误报）
- 误报密度 fp_per_kchar：每千字（草稿正文）误报条数
- 来源定位准确率 src_acc：对 detect_mode 含 source 的已定位错误，finding.source 的 (文档种类, 页码) 与金标准一致的比例
- 建议可执行率 fix_ok：已定位错误中，建议文本能直接执行（含正确值/正确表述）的比例
findings 最小字段：category, quote（含出错位置的原文片段）, claimed（被指出的原文值/词）, tier,
                   expected（核实值，文本）, suggestion（修改建议文本）, source{doc,page}（可空）, page（研报页，可空）
"""
from __future__ import annotations

import difflib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

AMOUNT_FAMILY = {'revenue', 'profit'}


def norm(s):
    s = (s or '').translate(str.maketrans('０１２３４５６７８９．，％（）＋－／：', '0123456789.,%()+-/:'))
    s = s.replace('−', '-').replace('—', '-').replace(',', '').replace('，', '')
    return re.sub(r'\s+', '', s)


def _lcs(a, b):
    if not a or not b:
        return 0
    m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return m.size


def _nums(s):
    out = []
    for m in re.finditer(r'-?\d+(?:\.\d+)?', norm(s)):
        try:
            out.append(Decimal(m.group(0)))
        except InvalidOperation:
            pass
    return out


def cat_ok(fc, gc):
    if fc == gc:
        return True
    if fc in AMOUNT_FAMILY and gc in AMOUNT_FAMILY:
        return True
    return False


def match_one(f, g):
    """finding f 是否定位到 gold g。"""
    gnew = norm(g.get('new_text'))
    gctx = norm(g.get('context'))
    fq = norm(f.get('quote'))
    fc = norm(f.get('claimed'))
    if g['subtype'] == 'missing_risk_warning':
        return f.get('category') == 'compliance' and ('风险提示' in (f.get('claimed') or '') or '风险提示' in (f.get('quote') or '') or 'missing_risk' in (f.get('subcategory') or ''))
    if g['category'] == 'compliance' and gctx:
        return _lcs(fq, gctx) >= min(10, max(4, len(gctx) // 2)) or (gctx[:12] in fq)
    if g['subtype'].startswith('table_row_unit') or g['subtype'] == 'table_header_year' or g['subtype'].endswith('table_cell') or g['subtype'].endswith('table_hist') or g['subtype'] == 'table_growth_vs_levels' or g['subtype'] == 'sum_ne_total':
        # 表格行：引用应包含注入后的整格值与行标签
        if gnew and gnew in fq + fc and _lcs(fq, gctx) >= min(8, max(3, len(gctx) // 2)):
            return True
        return False
    if gnew and (gnew in fc or gnew in fq):
        # 引用与注入点上下文重合
        return _lcs(fq, gctx) >= min(10, max(4, len(gctx) // 2))
    return False


def evaluate_draft(findings, gold, natural=None, draft_chars=None, tiers=None):
    """返回该草稿的明细：每条金标准的命中情况 + finding 的归属。"""
    natural = natural or []
    inj = gold['injections']
    used = {}
    for fi, f in enumerate(findings):
        if tiers and f.get('tier') not in tiers:
            continue
    rows = []
    for g in inj:
        best = None
        for fi, f in enumerate(findings):
            if tiers and f.get('tier') not in tiers:
                continue
            if match_one(f, g):
                if best is None or (cat_ok(f.get('category'), g['category']) and not cat_ok(findings[best].get('category'), g['category'])):
                    best = fi
        rows.append((g, best))
    tp_idx = {b for _, b in rows if b is not None}
    # 一条 finding 可能同时命中多个金标准，视为各自召回（引用同一长句时），误报只看"是否命中任何金标准"
    fp = []
    ctrl = gold.get('fp_controls') or []
    for fi, f in enumerate(findings):
        if tiers and f.get('tier') not in tiers:
            continue
        hit_any = any(match_one(f, g) for g in inj)
        if hit_any:
            continue
        if any(_lcs(norm(f.get('quote')), norm(n.get('context'))) >= min(12, max(4, len(norm(n.get('context'))) // 2)) for n in natural):
            continue
        is_ctrl = any(norm(c['text'])[:14] in norm(f.get('quote')) or _lcs(norm(f.get('quote')), norm(c['text'])) >= 12 for c in ctrl)
        fp.append({'finding': f, 'is_control': is_ctrl})
    return rows, fp


def _fix_actionable(f, g):
    sug = norm((f.get('suggestion') or '') + ' ' + (f.get('expected') or ''))
    truth = norm(g.get('truth'))
    if g['category'] == 'compliance':
        return bool(re.search(r'删除|删去|补充|改为|调整为', f.get('suggestion') or ''))
    if not truth:
        return False
    if truth in sug:
        return True
    tn = _nums(g.get('truth'))
    en = _nums((f.get('expected') or '') + ' ' + (f.get('suggestion') or ''))
    if tn and en:
        t = tn[0]
        tol = max(abs(t) * Decimal('0.0006'), Decimal('0.0501') if '.' in norm(g.get('truth')) and len(norm(g.get('truth')).split('.')[-1]) <= 1 else Decimal('0.0051'))
        return any(abs(e - t) <= tol for e in en)
    return False


def evaluate_corpus(per_doc, root):
    """per_doc: {code: {'findings': [...], 'chars': 正文字数}}；root: tests/real_corpus/errorcheck。返回汇总指标（分 split、分类别）。"""
    root = Path(root)
    nat_path = root / 'natural_noise.json'
    natural_all = json.load(open(nat_path)) if nat_path.is_file() else {}
    out = {}
    for split in ('dev', 'heldout', 'all'):
        agg = {'n_gold': 0, 'loc': 0, 'cat': 0, 'fix': 0, 'src_n': 0, 'src_ok': 0, 'tp_findings': 0, 'n_findings': 0, 'fp': 0, 'fp_ctrl': 0,
               'chars': 0, 'natural': 0, 'by_cat': {}, 'misses': [], 'fps': []}
        for code, item in per_doc.items():
            findings = item['findings']
            gold = json.load(open(root / 'gold' / f'{code}.json'))
            if split != 'all' and gold['split'] != split:
                continue
            nat = natural_all.get(code, [])
            rows, fp = evaluate_draft(findings, gold, nat)
            chars = item.get('chars') or 1000
            for g, fi in rows:
                c = agg['by_cat'].setdefault(g['category'], {'n': 0, 'loc': 0, 'cat': 0, 'fix': 0, 'src_n': 0, 'src_ok': 0})
                agg['n_gold'] += 1
                c['n'] += 1
                if fi is None:
                    agg['misses'].append({'code': code, 'id': g['id'], 'category': g['category'], 'subtype': g['subtype'], 'new_text': g['new_text'][:40], 'context': g['context'][:70]})
                    continue
                f = findings[fi]
                agg['loc'] += 1
                c['loc'] += 1
                if cat_ok(f.get('category'), g['category']):
                    agg['cat'] += 1
                    c['cat'] += 1
                    if _fix_actionable(f, g):
                        agg['fix'] += 1
                        c['fix'] += 1
                if 'source' in g['detect_mode']:
                    srcs = [s for s in g['source_refs'] if 'page' in s]
                    if srcs:
                        agg['src_n'] += 1
                        c['src_n'] += 1
                        fs = f.get('source') or {}
                        if any(fs.get('page') == s['page'] and (fs.get('doc') or '').endswith(s['doc'][-2:]) for s in srcs):
                            agg['src_ok'] += 1
                            c['src_ok'] += 1
            agg['n_findings'] += len(findings)
            agg['fp'] += len(fp)
            agg['fp_ctrl'] += sum(1 for x in fp if x['is_control'])
            for x in fp:
                agg['fps'].append({'code': code, 'category': x['finding'].get('category'), 'tier': x['finding'].get('tier'), 'quote': (x['finding'].get('quote') or '')[:80],
                                   'claimed': x['finding'].get('claimed'), 'reason': x['finding'].get('reason') or x['finding'].get('subcategory')})
            agg['chars'] += chars
        n = max(agg['n_gold'], 1)
        agg['recall_loc'] = round(agg['loc'] / n, 4)
        agg['recall_cat'] = round(agg['cat'] / n, 4)
        agg['fix_ok_rate'] = round(agg['fix'] / max(agg['loc'], 1), 4)
        agg['fix_ok_overall'] = round(agg['fix'] / n, 4)
        agg['src_acc'] = round(agg['src_ok'] / max(agg['src_n'], 1), 4)
        tp = agg['n_findings'] - agg['fp']
        agg['precision'] = round(tp / max(agg['n_findings'], 1), 4)
        agg['fp_per_kchar'] = round(agg['fp'] / max(agg['chars'] / 1000.0, 1e-9), 3)
        out[split] = agg
    return out


def format_report(res, title=''):
    lines = [f'# {title}']
    for split in ('dev', 'heldout', 'all'):
        a = res.get(split)
        if not a or not a['n_gold']:
            continue
        lines.append(f"\n## {split}  金标准 {a['n_gold']} 处 / finding {a['n_findings']} 条")
        lines.append(f"- 定位召回 {a['recall_loc']:.1%}（{a['loc']}/{a['n_gold']}），类别正确召回 {a['recall_cat']:.1%}，建议可执行率(占已定位) {a['fix_ok_rate']:.1%}，来源定位准确率 {a['src_acc']:.1%}（{a['src_ok']}/{a['src_n']}）")
        lines.append(f"- 精确率 {a['precision']:.1%}，误报 {a['fp']} 条（其中命中正确句 {a['fp_ctrl']}），误报密度 {a['fp_per_kchar']:.2f} 条/千字")
        lines.append('\n| 类别 | n | 定位 | 类别正确 | 建议可执行 | 来源定位 |')
        lines.append('|---|---|---|---|---|---|')
        for k, c in sorted(a['by_cat'].items()):
            lines.append(f"| {k} | {c['n']} | {c['loc']/c['n']:.0%} | {c['cat']/c['n']:.0%} | {c['fix']/max(c['loc'],1):.0%} | {c['src_ok']}/{c['src_n']} |")
    return '\n'.join(lines)
