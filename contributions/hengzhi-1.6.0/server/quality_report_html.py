"""研报质量评估 v2 → 单文件可交互 HTML 报告。

设计约束（按项目哲学）：
1. **离线可用**：不引用任何 CDN / 外部字体 / 外部脚本，全部内联。
2. **不新增数字**：只渲染 `report_quality_v2.assess_quality` 的结果，
   不重算分数、不补全 `None`、不造排名。
3. **纪律写进页面**：覆盖率、内部量表声明、`None ≠ 0 分`、长度基线提示
   都必须出现在页面上，而不是只写在代码注释里。
4. **可追溯**：每个子指标展开后能看到命中的原文行号与规则 ID。

对外只有一个函数：`render_quality_html(result, *, report_name='', baseline=None)`。
"""
from __future__ import annotations

import html
import json
from decimal import Decimal

DIM_ORDER = ('logic', 'evidence', 'fact', 'risk', 'action')
DIM_FALLBACK = {
    'logic': '论证与逻辑', 'evidence': '证据支撑', 'fact': '事实准确',
    'risk': '风险披露', 'action': '可操作性',
}

# 长度基线：来自 scripts/eval_quality_pairs.py 在 31 对人工盲评标签上的实测。
# 放在这里是为了让页面自己就能说清"准确率必须配基线"这件事。
BASELINE = {
    'all': {'pairs': 31, 'model': 0.968, 'length': 0.871},
    'depth': {'pairs': 17, 'model': 1.0, 'length': 1.0},
    'same_tier': {'pairs': 8, 'model': 0.875, 'length': 1.0},
    'anti_length': {'pairs': 4, 'model': 1.0, 'length': 0.0},
    'decision_support': {'pairs': 2, 'model': 1.0, 'length': 1.0},
}
BASELINE_NOTE = (
    '长度基线 = 仅按报告字数排序的配对准确率。实测在 31 对盲评标签上长度基线已达 87.1%，'
    '所以不给基线的"排序准确率"没有说服力。'
)


def _num(value):
    """把 Decimal/float/None 统一成可 JSON 序列化的值，None 保持 None。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value, digits=2, dash='—'):
    v = _num(value)
    return dash if v is None else f'{v:.{digits}f}'


def _pct(value, digits=1, dash='—'):
    v = _num(value)
    return dash if v is None else f'{v * 100:.{digits}f}%'


def _e(text):
    return html.escape(str(text if text is not None else ''), quote=True)


def _score_tone(score):
    """0–5 分 → 语气色。None 单独处理（不适用 ≠ 低分）。"""
    v = _num(score)
    if v is None:
        return 'na'
    if v >= 4:
        return 'good'
    if v >= 3:
        return 'ok'
    if v >= 2.5:
        return 'warn'
    return 'bad'


def _radar_svg(dimensions, size=260):
    """五维雷达图。分数 0–5，不适用维度画成虚线空心点。"""
    cx = cy = size / 2
    radius = size / 2 - 42
    n = len(DIM_ORDER)
    import math

    def point(i, ratio):
        ang = -math.pi / 2 + 2 * math.pi * i / n
        return cx + radius * ratio * math.cos(ang), cy + radius * ratio * math.sin(ang)

    rings = []
    for ratio in (0.25, 0.5, 0.75, 1.0):
        pts = ' '.join(f'{x:.1f},{y:.1f}' for x, y in (point(i, ratio) for i in range(n)))
        rings.append(f'<polygon points="{pts}" fill="none" stroke="var(--border-strong)" stroke-width="1"/>')

    axes = []
    for i in range(n):
        x, y = point(i, 1.0)
        axes.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="var(--border-strong)" stroke-width="1"/>')

    values, labels, dots = [], [], []
    for i, dim in enumerate(DIM_ORDER):
        info = dimensions.get(dim) or {}
        score = _num(info.get('score'))
        ratio = 0.0 if score is None else max(0.0, min(score, 5.0)) / 5.0
        x, y = point(i, ratio)
        values.append(f'{x:.1f},{y:.1f}')
        if score is not None:
            dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="var(--accent)"/>')
        lx, ly = point(i, 1.22)
        name = info.get('name') or DIM_FALLBACK.get(dim, dim)
        txt = f'{name} {_fmt(score)}' if score is not None else f'{name} 未评'
        labels.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" dominant-baseline="middle" '
            f'font-size="11" fill="var(--text-2)">{_e(txt)}</text>')

    return (
        f'<svg viewBox="0 0 {size} {size}" width="100%" height="{size}" role="img" '
        f'aria-label="五维雷达图">'
        + ''.join(rings) + ''.join(axes)
        + f'<polygon points="{" ".join(values)}" fill="var(--accent-soft)" stroke="var(--accent)" stroke-width="2"/>'
        + ''.join(dots) + ''.join(labels)
        + '</svg>'
    )


def _sub_row(sub):
    sid = sub.get('id') or ''
    name = sub.get('name') or sid
    dim = sub.get('dim') or ''
    score = _num(sub.get('score'))
    tone = _score_tone(score)
    if score is None:
        bar_w, score_txt = 0.0, '不适用'
    else:
        bar_w = max(0.0, min(score, 5.0)) / 5.0 * 100
        score_txt = f'{score:.2f}'

    why = sub.get('why') or ''
    hits = sub.get('hits') or []
    counter = sub.get('counter') or []
    metrics = sub.get('metrics') or {}

    detail = []
    if why:
        detail.append(f'<div class="why">{_e(why)}</div>')
    if metrics:
        detail.append('<div class="metrics">' + ''.join(
            f'<span class="metric"><b>{_e(k)}</b>{_e(v)}</span>' for k, v in metrics.items()) + '</div>')
    if hits:
        detail.append('<div class="ev-group"><div class="ev-title">命中（支持该分数的原文）</div><ul class="ev">'
                      + ''.join(f'<li><span class="rule">{_e(h.get("rule"))}</span>'
                                f'<span class="line">L{_e(h.get("line"))}</span>'
                                f'<span class="txt">{_e(h.get("text"))}</span></li>' for h in hits)
                      + '</ul></div>')
    if counter:
        detail.append('<div class="ev-group"><div class="ev-title">反例（未满足该规则的行）</div><ul class="ev">'
                      + ''.join(f'<li><span class="rule">{_e(h.get("rule"))}</span>'
                                f'<span class="line">L{_e(h.get("line"))}</span>'
                                f'<span class="txt">{_e(h.get("text"))}</span></li>' for h in counter)
                      + '</ul></div>')
    if not detail:
        detail.append('<div class="why dim">该项无可展示的命中原文。</div>')

    has_detail = bool(why or hits or counter or metrics)
    return (
        f'<div class="sub" data-dim="{_e(dim)}" data-score="{"" if score is None else f"{score:.2f}"}" '
        f'data-applicable="{str(bool(sub.get("applicable", score is not None))).lower()}">'
        f'<button class="sub-head" type="button" {"disabled" if not has_detail else ""}>'
        f'<span class="sub-name">{_e(name)}<code class="sid">{_e(sid)}</code></span>'
        f'<span class="sub-bar"><span class="sub-fill tone-{tone}" style="width:{bar_w:.1f}%"></span></span>'
        f'<span class="sub-score tone-{tone}">{score_txt}</span>'
        f'<span class="chev">▸</span></button>'
        f'<div class="sub-detail">{"".join(detail)}</div>'
        f'</div>'
    )


def _dim_block(dim, info, subs):
    name = info.get('name') or DIM_FALLBACK.get(dim, dim)
    score = _num(info.get('score'))
    weight = _num(info.get('weight'))
    applicable = info.get('applicable')
    total_subs = info.get('total_subs')
    tone = _score_tone(score)
    rows = ''.join(_sub_row(s) for s in subs if (s.get('dim') == dim))
    na = [s for s in subs if s.get('dim') == dim and _num(s.get('score')) is None]
    na_note = (f'<div class="na-note">其中 {len(na)} 项不适用（该报告没有这类内容），'
               f'已从加权均值中剔除，不按 0 分计。</div>') if na else ''
    return (
        f'<section class="dim" data-dim="{_e(dim)}">'
        f'<header class="dim-head">'
        f'<h3>{_e(name)}</h3>'
        f'<span class="dim-meta">权重 {_pct(weight, 0)} · 已评 {_e(applicable)}/{_e(total_subs)} 项</span>'
        f'<span class="dim-score tone-{tone}">{_fmt(score)}</span>'
        f'</header>{na_note}<div class="subs">{rows}</div></section>'
    )


def _review_block(review_queue):
    if not review_queue:
        return '<p class="dim">复核队列为空：本次没有低于阈值的子指标。</p>'
    low = [r for r in review_queue if r.get('kind') == 'low_sub']
    na = [r for r in review_queue if r.get('kind') != 'low_sub']

    def card(r):
        ev = r.get('evidence') or []
        ev_html = ('<ul class="ev">' + ''.join(f'<li><span class="txt">{_e(x)}</span></li>' for x in ev) + '</ul>') if ev else ''
        return (f'<div class="rq-card"><div class="rq-head"><b>{_e(r.get("name") or r.get("id"))}</b>'
                f'<span class="rq-score tone-{_score_tone(r.get("score"))}">{_fmt(r.get("score"))}</span></div>'
                f'<div class="why">{_e(r.get("why") or "")}</div>{ev_html}</div>')

    parts = []
    if low:
        parts.append(f'<h4>低于阈值的子指标（{len(low)} 项，优先修改）</h4>'
                     + ''.join(card(r) for r in low))
    if na:
        parts.append(f'<h4>不适用项（{len(na)} 项，不是低分）</h4>'
                     + '<p class="dim">该报告本来没有这类内容，已从加权均值中剔除。</p>'
                     + ''.join(card(r) for r in na))
    return ''.join(parts)


def _baseline_block(baseline):
    data = baseline or BASELINE
    rows = []
    label = {'all': '全部', 'depth': 'depth（深度差异）', 'same_tier': 'same_tier（同档次细比）',
             'anti_length': 'anti_length（长度与质量反向）', 'decision_support': 'decision_support'}
    for key in ('all', 'depth', 'same_tier', 'anti_length', 'decision_support'):
        item = data.get(key)
        if not item:
            continue
        model = _num(item.get('model'))
        length = _num(item.get('length'))
        worse = model is not None and length is not None and model < length
        rows.append(
            f'<tr{" class=\"worse\"" if worse else ""}><td>{_e(label.get(key, key))}</td>'
            f'<td>{_e(item.get("pairs"))}</td>'
            f'<td>{_pct(model)}</td><td>{_pct(length)}</td>'
            f'<td>{"低于基线（已知短板）" if worse else "—"}</td></tr>')
    return (
        f'<p class="dim">{_e(BASELINE_NOTE)}</p>'
        '<table class="tbl"><thead><tr><th>子集</th><th>对数</th><th>本模型</th><th>长度基线</th><th>结论</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
        '<p class="dim">注意：depth 子集上本模型与长度基线都是 100%，该子集<b>不构成证据</b>；'
        '真正的证据是 anti_length；same_tier 上本模型低于长度基线，是已知短板。</p>'
    )


def render_quality_html(result: dict, *, report_name: str = '', baseline=None,
                        errorcheck: dict | None = None) -> str:
    """把 assess_quality 的结果渲染成单文件 HTML。不新增任何数字。"""
    if not isinstance(result, dict) or not result.get('ok'):
        raise ValueError('render_quality_html 需要 assess_quality 的成功结果')

    dims = result.get('dimensions') or {}
    subs = result.get('subs') or []
    review = result.get('review_queue') or []
    total = _num(result.get('total'))
    coverage = _num(result.get('coverage'))
    n_review = result.get('n_review')
    stats = result.get('text_stats') or {}
    consistency = result.get('consistency') or {}

    ordered = [d for d in DIM_ORDER if d in dims] + [d for d in dims if d not in DIM_ORDER]
    dim_html = ''.join(_dim_block(d, dims[d] or {}, subs) for d in ordered)

    tone = _score_tone(total)
    coverage_txt = _pct(coverage)
    coverage_warn = ('<span class="flag">覆盖率偏低，总分只在已评部分内可比</span>'
                     if coverage is not None and coverage < 0.8 else '')

    cons = (f'内部算术复算 {_e(consistency.get("checks"))} 处，'
            f'不一致 {_e(consistency.get("failures"))} 处；'
            f'显式公式复算 {_e(consistency.get("formula_checked"))} 处，'
            f'不通过 {_e(consistency.get("formula_failed"))} 处。')

    ec_block = ''
    if errorcheck:
        s = errorcheck.get('summary') or {}
        ec_block = (
            '<section class="card"><h2>配套：研报纠错核查</h2>'
            f'<p>检出 <b>{_e(s.get("n_findings"))}</b> 条可复算的确认错误，'
            f'另有 <b>{_e(s.get("n_suspects"))}</b> 条待人工核验的疑点。'
            '待人工疑点不得当作"报告错误"。</p>'
            '<p class="dim">质量分与纠错清单是两条独立的证据链：前者说"写得如何"，'
            '后者说"哪里算错了"，不要用质量分高低推断错误多少。</p></section>')

    payload = json.dumps({
        'total': total, 'coverage': coverage, 'n_review': n_review,
        'dimensions': {k: {kk: _num(vv) if kk in ('score', 'weight') else vv
                           for kk, vv in (v or {}).items()} for k, v in dims.items()},
    }, ensure_ascii=False)

    title = f'研报质量评估 · {report_name}' if report_name else '研报质量评估'

    return f'''<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>
:root {{
  --bg:#f7f7f9; --panel:#fff; --panel-2:#fbfbfc; --border:#e6e6ea; --border-strong:#d8d8de;
  --text:#1c1c1e; --text-2:#6b6b73; --text-3:#9a9aa3;
  --accent:#4f46e5; --accent-soft:#eef0ff; --accent-text:#4338ca;
  --green:#0f9d63; --green-soft:#e8f7f0; --amber:#b3730b; --amber-soft:#fdf4e3;
  --red:#c0392b; --red-soft:#fdecea;
  --shadow:0 1px 2px rgba(16,16,20,.05), 0 6px 24px rgba(16,16,20,.06);
  --radius:12px; --radius-sm:9px;
  --font:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Hiragino Sans GB","Microsoft YaHei",Arial,sans-serif;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
}}
html[data-theme="dark"] {{
  --bg:#17171a; --panel:#1f1f23; --panel-2:#232328; --border:#303038; --border-strong:#3d3d47;
  --text:#ececf1; --text-2:#a5a5b0; --text-3:#75757f;
  --accent:#7c74ff; --accent-soft:#2a2a45; --accent-text:#b4aeff;
  --green:#3ecf8e; --green-soft:#1d3a2e; --amber:#e0a94a; --amber-soft:#3a3021;
  --red:#ff6b5e; --red-soft:#3a2422;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 28px rgba(0,0,0,.3);
}}
@media (prefers-color-scheme: dark) {{
  html:not([data-theme="light"]) {{
    --bg:#17171a; --panel:#1f1f23; --panel-2:#232328; --border:#303038; --border-strong:#3d3d47;
    --text:#ececf1; --text-2:#a5a5b0; --text-3:#75757f;
    --accent:#7c74ff; --accent-soft:#2a2a45; --accent-text:#b4aeff;
    --green:#3ecf8e; --green-soft:#1d3a2e; --amber:#e0a94a; --amber-soft:#3a3021;
    --red:#ff6b5e; --red-soft:#3a2422;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 28px rgba(0,0,0,.3);
  }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:var(--font);
  font-size:14px; line-height:1.6; -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1080px; margin:0 auto; padding:28px 20px 72px; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
h2 {{ font-size:16px; margin:0 0 12px; }}
h3 {{ font-size:14px; margin:0; }}
h4 {{ font-size:13px; margin:18px 0 8px; color:var(--text-2); }}
.subtitle {{ color:var(--text-3); font-size:12px; margin-bottom:20px; }}
code {{ font-family:var(--mono); font-size:11px; background:var(--panel-2);
  border:1px solid var(--border); padding:1px 5px; border-radius:5px; }}
.dim {{ color:var(--text-3); font-size:12px; }}
.card {{ background:var(--panel); border:1px solid var(--border); border-radius:var(--radius);
  box-shadow:var(--shadow); padding:18px; margin-bottom:16px; }}
.hero {{ display:flex; gap:24px; flex-wrap:wrap; align-items:center; }}
.hero .radar {{ flex:0 0 280px; }}
.hero .kpis {{ flex:1 1 300px; min-width:260px; }}
.kpi {{ display:flex; justify-content:space-between; align-items:baseline;
  padding:9px 0; border-bottom:1px dashed var(--border); }}
.kpi:last-child {{ border-bottom:none; }}
.kpi .k {{ color:var(--text-2); font-size:13px; }}
.kpi .v {{ font-family:var(--mono); font-size:18px; font-weight:600; }}
.tone-good {{ color:var(--green); }} .tone-ok {{ color:var(--accent-text); }}
.tone-warn {{ color:var(--amber); }} .tone-bad {{ color:var(--red); }}
.tone-na {{ color:var(--text-3); }}
.flag {{ display:inline-block; background:var(--amber-soft); color:var(--amber);
  border-radius:6px; padding:2px 8px; font-size:11px; margin-left:6px; }}
.notice {{ background:var(--accent-soft); border:1px solid var(--accent); color:var(--accent-text);
  border-radius:var(--radius-sm); padding:12px 14px; font-size:12.5px; margin-bottom:16px; }}
.notice b {{ color:inherit; }}
.dim-head {{ display:flex; align-items:center; gap:10px; padding:10px 0;
  border-bottom:1px solid var(--border); }}
.dim-head h3 {{ flex:1 1 auto; }}
.dim-meta {{ color:var(--text-3); font-size:11.5px; }}
.dim-score {{ font-family:var(--mono); font-size:16px; font-weight:600; min-width:44px; text-align:right; }}
.na-note {{ font-size:11.5px; color:var(--text-3); padding:6px 0 0; }}
.subs {{ padding:4px 0 8px; }}
.sub {{ border-bottom:1px solid var(--border); }}
.sub:last-child {{ border-bottom:none; }}
.sub-head {{ display:grid; grid-template-columns:1fr 180px 52px 16px; gap:10px; align-items:center;
  width:100%; background:none; border:none; padding:9px 2px; text-align:left; color:inherit;
  font-family:inherit; font-size:13px; cursor:pointer; border-radius:6px; }}
.sub-head:disabled {{ cursor:default; }}
.sub-head:not(:disabled):hover {{ background:var(--panel-2); }}
.sub-name {{ display:flex; align-items:center; gap:7px; min-width:0; }}
.sub-name .sid {{ color:var(--text-3); flex:0 0 auto; }}
.sub-bar {{ height:7px; background:var(--panel-2); border:1px solid var(--border);
  border-radius:99px; overflow:hidden; }}
.sub-fill {{ display:block; height:100%; border-radius:99px; }}
.sub-fill.tone-good {{ background:var(--green); }} .sub-fill.tone-ok {{ background:var(--accent); }}
.sub-fill.tone-warn {{ background:var(--amber); }} .sub-fill.tone-bad {{ background:var(--red); }}
.sub-fill.tone-na {{ background:transparent; }}
.sub-score {{ font-family:var(--mono); font-size:12.5px; text-align:right; }}
.chev {{ color:var(--text-3); font-size:11px; transition:transform .15s; }}
.sub.open .chev {{ transform:rotate(90deg); }}
.sub-detail {{ display:none; padding:4px 2px 14px; }}
.sub.open .sub-detail {{ display:block; }}
.why {{ font-size:12.5px; color:var(--text-2); margin-bottom:6px; }}
.metrics {{ display:flex; gap:14px; flex-wrap:wrap; margin-bottom:8px; }}
.metric {{ font-size:12px; color:var(--text-2); }}
.metric b {{ color:var(--text-3); font-weight:500; margin-right:5px; }}
.ev-group {{ margin-top:8px; }}
.ev-title {{ font-size:11.5px; color:var(--text-3); margin-bottom:4px; }}
ul.ev {{ list-style:none; margin:0; padding:0; }}
ul.ev li {{ display:flex; gap:8px; font-size:12px; padding:4px 8px; border-left:2px solid var(--border);
  margin-bottom:3px; background:var(--panel-2); border-radius:0 6px 6px 0; }}
ul.ev .rule {{ font-family:var(--mono); font-size:10.5px; color:var(--accent-text); flex:0 0 auto; }}
ul.ev .line {{ font-family:var(--mono); font-size:10.5px; color:var(--text-3); flex:0 0 auto; }}
ul.ev .txt {{ color:var(--text-2); min-width:0; }}
.rq-card {{ border:1px solid var(--border); border-left:3px solid var(--amber);
  border-radius:var(--radius-sm); padding:12px 14px; margin-bottom:10px; background:var(--panel-2); }}
.rq-head {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:4px; }}
.rq-score {{ font-family:var(--mono); font-weight:600; }}
table.tbl {{ width:100%; border-collapse:collapse; font-size:12.5px; margin-top:8px; }}
table.tbl th, table.tbl td {{ text-align:left; padding:7px 9px; border-bottom:1px solid var(--border); }}
table.tbl th {{ color:var(--text-3); font-weight:500; font-size:11.5px; }}
table.tbl tr.worse td {{ background:var(--amber-soft); }}
.toolbar {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-bottom:14px; }}
.btn {{ font-family:inherit; font-size:12px; color:var(--text-2); background:var(--panel);
  border:1px solid var(--border); border-radius:8px; padding:6px 11px; cursor:pointer; }}
.btn:hover {{ border-color:var(--border-strong); color:var(--text); }}
.btn.on {{ background:var(--accent-soft); border-color:var(--accent); color:var(--accent-text); }}
footer {{ margin-top:28px; color:var(--text-3); font-size:11.5px; }}
@media print {{
  body {{ background:#fff; }} .toolbar {{ display:none; }} .card {{ box-shadow:none; break-inside:avoid; }}
  .sub-detail {{ display:block !important; }}
}}
@media (max-width:720px) {{
  .sub-head {{ grid-template-columns:1fr 90px 46px 14px; }}
  .hero .radar {{ flex:1 1 100%; }}
}}
</style>
</head>
<body>
<div class="wrap">
  <h1>{_e(title)}</h1>
  <div class="subtitle">引擎规则版本 <code>{_e(result.get("rules_version"))}</code>
    　报告字数 {_e(stats.get("chars"))}　段落 {_e(stats.get("lines"))}</div>

  <div class="notice">
    <b>这是项目内部量表，不是竞赛官方评分。</b> {_e(result.get("scale_note") or "")}
    引用总分必须同时给出覆盖率；未评项<b>不是 0 分</b>，不得据此自造补全后的总分或排名。
  </div>

  <section class="card hero">
    <div class="radar">{_radar_svg(dims)}</div>
    <div class="kpis">
      <div class="kpi"><span class="k">总分（0–5，仅在已评部分内可比）</span>
        <span class="v tone-{tone}">{_fmt(total)}</span></div>
      <div class="kpi"><span class="k">评估覆盖率</span>
        <span class="v">{coverage_txt}{coverage_warn}</span></div>
      <div class="kpi"><span class="k">复核队列</span><span class="v">{_e(n_review)}</span></div>
      <div class="kpi"><span class="k">内部一致性</span><span class="v" style="font-size:12.5px">{cons}</span></div>
      <p class="dim" style="margin:10px 0 0">{_e(result.get("coverage_note") or "")}</p>
    </div>
  </section>

  <div class="toolbar">
    <button class="btn" type="button" data-sort="dim">按维度分组</button>
    <button class="btn" type="button" data-sort="low">低分优先</button>
    <button class="btn" type="button" data-sort="high">高分优先</button>
    <button class="btn" type="button" id="toggleNa">显示/隐藏不适用项</button>
    <button class="btn" type="button" id="toggleTheme">切换主题</button>
    <button class="btn" type="button" id="expandAll">展开/收起全部</button>
  </div>

  <section class="card" id="dims">{dim_html}</section>

  <section class="card">
    <h2>复核队列</h2>
    {_review_block(review)}
  </section>

  <section class="card">
    <h2>配对排序能力与长度基线</h2>
    {_baseline_block(baseline)}
  </section>

  {ec_block}

  <section class="card">
    <h2>评估边界</h2>
    <p>机器只评<b>可观察</b>的东西：来源标注率、数字密度、观点—证据对应、反证条件、
       绝对化措辞、内部一致。以下维度<b>未经自动评估</b>，仍需人工评审：</p>
    <ul>
      <li>行业适用性（这个估值方法适不适合这个行业）</li>
      <li>因果链的实质成立性（连接词出现了 ≠ 因果成立）</li>
      <li>利益冲突与独立性判断</li>
      <li>是否使用了报告日之后才公布的数据（事后信息）</li>
    </ul>
  </section>

  <footer>
    由本地确定性引擎生成，未调用语言模型，未联网。页面内所有数字直接取自
    <code>report_quality_v2.assess_quality</code> 的结果，未做任何重算或补全。
  </footer>
</div>

<script>
(function () {{
  var DATA = {payload};
  var root = document.documentElement;

  // 折叠/展开单个子指标
  document.querySelectorAll('.sub-head').forEach(function (head) {{
    head.addEventListener('click', function () {{
      if (head.disabled) return;
      head.parentElement.classList.toggle('open');
    }});
  }});

  // 排序
  var dimsWrap = document.getElementById('dims');
  document.querySelectorAll('[data-sort]').forEach(function (btn) {{
    btn.addEventListener('click', function () {{
      var mode = btn.getAttribute('data-sort');
      document.querySelectorAll('[data-sort]').forEach(function (b) {{ b.classList.remove('on'); }});
      btn.classList.add('on');
      var groups = Array.prototype.slice.call(dimsWrap.querySelectorAll('.dim'));
      groups.forEach(function (g) {{
        var subs = Array.prototype.slice.call(g.querySelectorAll('.sub'));
        subs.sort(function (a, b) {{
          var sa = parseFloat(a.getAttribute('data-score'));
          var sb = parseFloat(b.getAttribute('data-score'));
          if (isNaN(sa)) sa = mode === 'high' ? -1 : 99;
          if (isNaN(sb)) sb = mode === 'high' ? -1 : 99;
          return mode === 'high' ? sb - sa : sa - sb;
        }});
        var box = g.querySelector('.subs');
        subs.forEach(function (s) {{ box.appendChild(s); }});
      }});
      if (mode !== 'dim') {{
        groups.sort(function (a, b) {{
          var sa = (DATA.dimensions[a.getAttribute('data-dim')] || {{}}).score;
          var sb = (DATA.dimensions[b.getAttribute('data-dim')] || {{}}).score;
          if (sa === null || sa === undefined) sa = mode === 'high' ? -1 : 99;
          if (sb === null || sb === undefined) sb = mode === 'high' ? -1 : 99;
          return mode === 'high' ? sb - sa : sa - sb;
        }});
        groups.forEach(function (g) {{ dimsWrap.appendChild(g); }});
      }}
    }});
  }});

  // 显示/隐藏不适用项
  document.getElementById('toggleNa').addEventListener('click', function () {{
    var hidden = root.getAttribute('data-hide-na') === '1';
    root.setAttribute('data-hide-na', hidden ? '0' : '1');
    document.querySelectorAll('.sub[data-applicable="false"]').forEach(function (s) {{
      s.style.display = hidden ? '' : 'none';
    }});
  }});

  // 主题
  var saved = null;
  try {{ saved = localStorage.getItem('qr-theme'); }} catch (e) {{}}
  if (saved) root.setAttribute('data-theme', saved);
  document.getElementById('toggleTheme').addEventListener('click', function () {{
    var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try {{ localStorage.setItem('qr-theme', next); }} catch (e) {{}}
  }});

  // 展开/收起全部
  document.getElementById('expandAll').addEventListener('click', function () {{
    var subs = Array.prototype.slice.call(document.querySelectorAll('.sub'));
    var anyClosed = subs.some(function (s) {{ return !s.classList.contains('open'); }});
    subs.forEach(function (s) {{ s.classList.toggle('open', anyClosed); }});
  }});
}})();
</script>
</body>
</html>
'''


def write_quality_html(path, result: dict, *, report_name: str = '', baseline=None,
                       errorcheck: dict | None = None):
    """写出 HTML 文件，返回 Path。"""
    from pathlib import Path
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_quality_html(result, report_name=report_name, baseline=baseline,
                                       errorcheck=errorcheck), encoding='utf-8')
    return out
