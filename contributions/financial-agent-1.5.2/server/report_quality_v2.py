"""研报质量评估 v2 的编排层：把"子指标"汇总成"维度分 + 总分 + 复核队列"。

为什么单独一层
--------------
`report_features.py` 只到"子指标"层：每个子指标给出 0–5 分、命中证据、规则 ID，
不适用的给 `score=None`。但**"总分怎么算"是必须显式声明口径的事**，藏在某个
调用方里就会导致同一个报告在不同入口得到不同分数。所以汇总规则集中在这里，
且是纯函数（同文本同规则版本 → 同结果）。

三条口径纪律
------------
1. **不适用 ≠ 0 分**。`score=None` 的子指标从加权均值里剔除，不计入分母；
   同时必须报出**覆盖率**（适用权重 / 总权重），否则"覆盖率 30% 却拿 4.5 分"
   会被读成"报告很好"。
2. **总分是项目内部量表，不是竞赛官方评分**。输出里显式标注，避免被当成官方结论。
3. **复核队列与分数分开给**。低分子指标、内部一致性失败项、绝对化措辞命中，
   都进 `review_queue` 并带原文行号——分数是给人看趋势的，复核项才是能落地的动作。
"""
from __future__ import annotations

from collections import OrderedDict

from .report_consistency_adapter import internal_consistency
from .report_features import RULES_VERSION, all_features
from .report_text import preprocess

SCALE_NOTE = '项目内部量表（0–5），不是竞赛官方评分；未评部分不计入分母，须看覆盖率。'

# 维度权重：论证与证据是研报的核心，事实一致性次之，风险与可操作性再次。
# 权重只影响维度之间的相对重要度，不改变任何子指标的得分。
DIM_WEIGHTS = OrderedDict([
    ('logic', 0.30),      # 论证：结构、因果、估值方法、假设、反证
    ('evidence', 0.25),   # 证据：来源标注、数字密度、图表来源、观点—证据对应
    ('fact', 0.20),       # 事实：内部一致性、显式公式复算、单位期间口径
    ('risk', 0.15),       # 风险：具体性、量化、联动、合规措辞
    ('action', 0.10),     # 可操作性：结论、跟踪指标、催化、触发条件、待核验
])

# 子指标相对权重：同一维度内默认等权，少数子指标因为"缺了就基本没法用"而加重。
SUB_WEIGHTS = {
    'arg.claim_evidence': 1.5,   # 观点没有证据支撑，整篇报告的论证价值大打折扣
    'data.source_rate': 1.5,     # 来源标注率是"可核查性"的地基
    'val.method': 1.2,
    'val.range': 1.2,
    'consist.arith': 1.5,        # 内部自相矛盾是最硬的扣分项
    'consist.formula': 1.2,
    'risk.specific': 1.2,
}

# 低于这个分就进复核队列（0–5 制）。取 2.5 = 量表中点，含义是"没到及格线"。
REVIEW_THRESHOLD = 2.5


def assess_quality(text: str, *, review_formulas=None) -> dict:
    """对一份研报正文做质量评估。

    text : 研报正文（Markdown / 纯文本均可；表格行用 "a | b | c" 会被拆成数字格）
    返回 : {'ok', 'total', 'coverage', 'dimensions', 'subs', 'review_queue', ...}
    """
    doc = preprocess(text)
    consistency = internal_consistency(text, review_formulas=review_formulas)
    subs = all_features(doc, consistency)

    by_dim: dict[str, list] = {}
    for s in subs:
        by_dim.setdefault(s['dim'], []).append(s)

    dimensions = OrderedDict()
    for dim, items in by_dim.items():
        usable = [s for s in items if s.get('applicable') and s.get('score') is not None]
        weights = [SUB_WEIGHTS.get(s['id'], 1.0) for s in usable]
        wsum = sum(weights)
        dimensions[dim] = {
            'name': _DIM_NAME.get(dim, dim),
            'weight': DIM_WEIGHTS.get(dim),
            'score': round(sum(s['score'] * w for s, w in zip(usable, weights)) / wsum, 2) if wsum else None,
            'applicable': len(usable),
            'total_subs': len(items),
        }

    # 总分 = 以"子指标权重"为权重的加权均值；维度权重只用于展示维度分，
    # 不参与总分，避免"维度内子指标个数不同"造成隐性加权。
    usable = [s for s in subs if s.get('applicable') and s.get('score') is not None]
    weights = [SUB_WEIGHTS.get(s['id'], 1.0) for s in usable]
    wsum = sum(weights)
    total = round(sum(s['score'] * w for s, w in zip(usable, weights)) / wsum, 2) if wsum else None
    all_w = sum(SUB_WEIGHTS.get(s['id'], 1.0) for s in subs)
    coverage = round(wsum / all_w, 4) if all_w else 0.0

    review = []
    for s in subs:
        if s.get('applicable') and s.get('score') is not None and s['score'] < REVIEW_THRESHOLD:
            review.append({'kind': 'low_sub', 'id': s['id'], 'dim': s['dim'], 'name': s['name'],
                           'score': s['score'], 'why': s['why'],
                           'evidence': [h.get('text') for h in (s.get('hits') or [])[:3]]})
        elif not s.get('applicable'):
            review.append({'kind': 'not_applicable', 'id': s['id'], 'dim': s['dim'], 'name': s['name'],
                           'why': s['why']})
    for item in (consistency.get('failures') or []):
        review.append({'kind': 'consistency_failure', 'id': 'consist.arith',
                       'why': item.get('why') or item.get('note') or '', 'evidence': [item.get('text')]})
    for item in (consistency.get('formula_bad') or []):
        review.append({'kind': 'formula_failure', 'id': 'consist.formula',
                       'why': item.get('why') or '', 'evidence': [item.get('text')]})

    return {
        'ok': True,
        'rules_version': RULES_VERSION,
        'scale_note': SCALE_NOTE,
        'total': total,
        'coverage': coverage,
        'coverage_note': (f'已评子指标权重占比 {coverage:.0%}；'
                          f'未评项多为"该报告本来没有这类内容"（如无图表、无显式算式），'
                          f'不代表得分低。总分只在已评部分内可比。'),
        'dimensions': dimensions,
        'subs': subs,
        'review_queue': review,
        'n_review': len(review),
        'text_stats': {'chars': doc.raw_len, 'lines': len(doc.lines),
                       'sentences': len(doc.sentences), 'headings': len(doc.headings)},
        'consistency': {'checks': consistency.get('checks'), 'failures': len(consistency.get('failures') or []),
                        'formula_checked': consistency.get('formula_checked'),
                        'formula_failed': consistency.get('formula_failed')},
    }


_DIM_NAME = {
    'logic': '论证与逻辑',
    'evidence': '证据与可核查性',
    'fact': '事实与内部一致',
    'risk': '风险揭示与合规',
    'action': '可操作性与跟踪',
}
