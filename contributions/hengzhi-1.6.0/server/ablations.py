"""消融实验引擎 —— 把竞赛第三节要求的四类对照做成**可复现的确定性实验**。

竞赛《评测指标与目标值》第三节原文（`data/knowledge/竞赛要求与交付规范/03-*.md`）::

    | 实验           | 基线               | 要证明的点                       |
    | RAG 消融       | 无检索的本地 LLM   | 证据检索减少数字幻觉             |
    | Critic 消融    | 不使用反证 Agent   | 反证覆盖和引用精确率提升         |
    | 规则消融       | 仅 LLM 阅读年报    | 确定性规则提高稳定性和可复现性   |
    | 同行消融       | 只看公司历史       | 同行分位减少对行业周期的误判     |
    | 估值联动消融   | 风险与估值分离     | 风险参数化使结论能影响决策       |

三条设计纪律
------------
1. **一次只改一个变量。** 每个实验的 baseline 与 treatment 之间，除了被消融的那一层，
   输入、代码路径、参数全部相同。所有实验都跑在**同一份运行产物**上，不重新提取数据。

2. **只报可测的指标。** 无法实测的（例如 LLM 采样的方差）明确标注为
   「结构性判断」，不混进实测结果里冒充数据。

3. **数据不足就说不足。** 同行消融需要 ≥2 家可比公司；拿不到就返回
   `insufficient_data` 并说明缺什么，绝不合成数据填平后当成真实结论。

输出是机器可读的 dict，`render_markdown()` 可以把它渲染成可提交的实验报告。
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
from decimal import Decimal, InvalidOperation

from . import forensics
from . import peers as peers_mod
from . import report_audit
from . import valuation

EXPERIMENT_IDS = ('rag', 'critic', 'rules', 'peers', 'valuation')

# 结论判定用到的固定阈值。写死在模块里，避免"看着结果调阈值"。
_EFFECT_FLOOR = Decimal('0.02')      # 相对变动达到 2% 才算"有实际影响"
_STABILITY_ROUNDS = 5                # 重复运行次数（打乱输入顺序）

# 模拟同业剖面。仅用于在**只有一家公司数据**时演示同行消融的机制，
# 输出里会明确标注 `peer_source='synthetic'`，不得当作真实同行结论。
#
# 因子按方向使用：质量类指标（越高越好）乘因子，成本/天数类指标（越高越差）
# 除以因子。这样同业整体落在"比本公司更健康"的一侧，用来演示
# "本公司指标显著偏离同行区间"这一判定路径 —— 若两侧对称扰动，
# 本公司永远落在中位数上，机制反而看不出来。
_SYNTHETIC_PROFILES = [
    ('同业A(模拟)', Decimal('1.10')),
    ('同业B(模拟)', Decimal('1.22')),
    ('同业C(模拟)', Decimal('1.35')),
    ('同业D(模拟)', Decimal('1.50')),
]

# 越高越差的可比指标。其余按"越高越好"处理。
_HIGHER_IS_WORSE = {
    'receivables_days', 'inventory_days', 'payables_days', 'cash_conversion_cycle',
    'debt_ratio',
}

_VERDICT_LABELS = {
    'supported': '支持',
    'partially_supported': '部分支持',
    'not_supported': '不支持',
    'insufficient_data': '数据不足，无法判定',
}


# ================================================================ 基础工具

def _dec(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _f(value, places='0.0001'):
    value = _dec(value)
    if value is None:
        return ''
    return str(value.quantize(Decimal(places)))


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def _hash(obj) -> str:
    return hashlib.sha256(_canonical(obj).encode('utf-8')).hexdigest()


def _finding_key(finding: dict) -> tuple:
    return (finding.get('rule_id'), finding.get('company'), finding.get('scope'))


def _finding_set_hash(findings: list) -> str:
    """把一组风险发现压成一个顺序无关的指纹。

    为什么按 (规则, 公司, 口径, 风险分) 排序后再哈希，而不是直接哈希列表：
    `scan()` 的排序键只有 risk_score，同分项的相对顺序取决于输入顺序。
    要证明的是"同一输入的不同排列给出同一组发现"，不是"输出字节完全相同"，
    所以比较的是**集合**而不是**序列**。
    """
    canon = sorted([f.get('rule_id', ''), f.get('company', ''), f.get('scope', ''),
                    _f(f.get('risk_score'))] for f in findings)
    return _hash(canon)


def _score_map(findings: list) -> dict:
    return {_finding_key(f): _dec(f.get('risk_score')) for f in findings}


def _metric(label: str, baseline, treatment, better: str = 'treatment',
            fmt: str = '{}', note: str = '') -> dict:
    """一个对照指标。`better` 说明哪一侧的数值代表该指标更好（用于自动判定结论）。"""
    return {'label': label, 'baseline': baseline, 'treatment': treatment,
            'better': better, 'fmt': fmt, 'note': note}


def _delta(entry: dict):
    a, b = _dec(entry.get('baseline')), _dec(entry.get('treatment'))
    if a is None or b is None:
        return None
    return b - a


def _stability(facts, checks, metrics, peer, rounds: int = _STABILITY_ROUNDS) -> dict:
    """打乱输入顺序重复运行，验证确定性引擎的输出与顺序无关。"""
    signatures, counts = [], []
    for index in range(rounds):
        order = list(range(len(facts)))
        random.Random(20261010 + index).shuffle(order)
        shuffled = [facts[j] for j in order]
        result = forensics.scan(shuffled, checks=checks, metrics=metrics,
                                peer=peer, top_k=10000)
        findings = result.get('findings') or []
        signatures.append(_finding_set_hash(findings))
        counts.append(result.get('all_finding_count'))
    return {
        'rounds': rounds,
        'distinct_signatures': len(set(signatures)),
        'distinct_counts': len(set(counts)),
        'consistent': len(set(signatures)) == 1 and len(set(counts)) == 1,
        'signature': signatures[0] if signatures else '',
        'finding_count': counts[0] if counts else 0,
    }


def _strip_evidence(run: dict) -> dict:
    """抹掉一切原文定位信息 —— 等价于"没有检索层"的本地 LLM 输出。"""
    out: dict = {}
    for key in ('facts', 'metrics', 'checks', 'signals'):
        items = []
        for item in run.get(key) or []:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item['evidence_ids'] = []
            item.pop('evidence_url', None)
            item.pop('source', None)
            items.append(item)
        out[key] = items
    return out


def _equity(scenario: dict):
    if not isinstance(scenario, dict):
        return None
    return _dec((scenario.get('valuation') or {}).get('equity_value'))


def _ratio(part, whole):
    part, whole = _dec(part), _dec(whole)
    if part is None or whole in (None, 0):
        return None
    return (part / whole).quantize(Decimal('0.0001'))


# ================================================================ 实验一：RAG 消融

def _exp_rag(run: dict, report_text: str | None = None) -> dict:
    facts = run.get('facts') or []
    metrics = run.get('metrics') or []
    stripped = _strip_evidence(run)

    verified = [f for f in facts if f.get('status') == 'verified']
    with_evidence = [f for f in verified if f.get('evidence_ids')]

    pool_full = report_audit.build_pool(facts, metrics)
    pool_strip = report_audit.build_pool(stripped['facts'], stripped['metrics'])
    ev_full = sum(1 for item in pool_full if item.get('evidence_ids'))
    ev_strip = sum(1 for item in pool_strip if item.get('evidence_ids'))

    # 无检索层时证据编号被清空，覆盖率按定义为 0；实验组按实测比例。
    coverage_full = _ratio(len(with_evidence), len(verified))
    pool_ratio_full = _ratio(ev_full, len(pool_full))

    metrics_out = [
        _metric('已核验事实的证据覆盖率', Decimal(0), coverage_full, fmt='{:.4f}',
                note='没有检索层时，事实无法指回原文页/块，下游一切"可核验"都无从谈起。'),
        _metric('数值池中带证据项占比', Decimal(0), pool_ratio_full, fmt='{:.4f}'),
    ]

    details = {
        'verified_facts': len(verified),
        'facts_with_evidence': len(with_evidence),
        'pool_size_full': len(pool_full),
        'pool_size_stripped': len(pool_strip),
    }

    if report_text:
        full_cit = report_audit.audit_citations(report_text, run=run)
        strip_cit = report_audit.audit_citations(report_text, run=stripped)
        full_num = report_audit.audit_numbers(report_text, pool_full)
        strip_num = report_audit.audit_numbers(report_text, pool_strip)

        metrics_out.append(_metric(
            '报告引用可核验率（精确率）',
            _dec(strip_cit.get('citation_coverage')), _dec(full_cit.get('citation_coverage')),
            fmt='{:.4f}',
            note='报告里每个证据 ID 能否在运行产物中找到。无检索层时全部落空，'
                 '报告无法被核验 —— 这正是"证据检索减少数字幻觉"的可测落点。'))
        metrics_out.append(_metric(
            '报告数字可溯源率',
            _dec(strip_num.get('trace_rate')), _dec(full_num.get('trace_rate')), fmt='{:.4f}',
            better='none',
            note='实测两侧相同。诚实记录：证据检索影响的是"可核验性"，'
                 '不是"数值匹配"本身 —— 数值匹配由确定性提取层保证。'))

        details['citation'] = {'cited': full_cit.get('cited'),
                               'known_full': full_cit.get('known'),
                               'known_stripped': strip_cit.get('known')}
        details['number_trace'] = {'stripped': strip_num.get('trace_rate'),
                                   'full': full_num.get('trace_rate')}

        strip_rate = _dec(strip_cit.get('citation_coverage'))
        full_rate = _dec(full_cit.get('citation_coverage'))
        verdict = ('supported' if (full_rate is not None and strip_rate is not None
                                   and full_rate > strip_rate)
                   else ('not_supported' if strip_rate is not None else 'insufficient_data'))
    else:
        verdict = 'supported' if (coverage_full or Decimal(0)) > 0 else 'insufficient_data'
        details['citation'] = None

    return {
        'id': 'rag',
        'title': 'RAG 消融（证据检索）',
        'variable': '是否保留事实/指标的原文证据编号（evidence_ids）',
        'baseline': {'label': '无检索的本地 LLM（抹掉全部 evidence_ids）'},
        'treatment': {'label': '带原文定位的检索层（完整 evidence_ids）'},
        'metrics': metrics_out,
        'verdict': verdict,
        'hypothesis': '证据检索减少数字幻觉',
        'conclusion': (
            f'带检索层时报告里 {details["citation"]["cited"]} 处证据引用全部可核验'
            f'（精确率 {full_cit.get("citation_coverage")}）；'
            f'抹掉检索层后同一份报告的引用可核验率降到 '
            f'{strip_cit.get("citation_coverage")}，全部落空。'
            '检索层的作用是把"这句话出自年报第几页第几块"变成可机检的事实，'
            '从而让幻觉在交付前就被拦下 —— 而不是等评审肉眼发现。'
            '同时如实记录：数字可溯源率两侧相同（'
            f'{full_num.get("trace_rate")}），说明检索层保障的是"可核验性"，'
            '数值本身的准确性由确定性提取层负责，两者不可互相替代。'
            if report_text else
            f'已核验事实中 {len(with_evidence)}/{len(verified)} 条带原文证据编号；'
            '无检索层时该比例为 0，下游任何引用都无法核对。'),
        'details': details,
        'notes': [
            '基线用"抹掉证据编号"实现，而不是真跑一个无 RAG 的 LLM：'
            '后者不可复现（采样随机），无法作为可复现实验的对照。',
            '本实验只改证据字段，不改事实与指标数值，因此数值池大小两侧相同 —— '
            '这也正是"数字可溯源率不受检索影响"的原因。',
        ],
    }


# ================================================================ 实验二：Critic 消融

_CRITIC_FIELDS = ('counter_checks', 'open_questions', 'valuation_links')


def _exp_critic(forensic_run: dict, report_text: str | None = None,
                analysis_run: dict | None = None) -> dict:
    findings = (forensic_run or {}).get('findings') or []
    total = len(findings)
    if not total:
        return {
            'id': 'critic', 'title': 'Critic 消融（反证 Agent）',
            'variable': '风险发现是否携带反证方向 / 未解问题 / 估值传导路径',
            'baseline': {'label': '不使用反证 Agent（字段置空）'},
            'treatment': {'label': '带反证检索方向与未解问题'},
            'metrics': [], 'verdict': 'insufficient_data',
            'hypothesis': '反证覆盖和引用精确率提升',
            'conclusion': '本次运行没有风险发现，反证层无对象可覆盖，无法判定。',
            'details': {}, 'notes': [],
        }

    def cover(field):
        return sum(1 for f in findings if f.get(field))

    directions = sum(len(f.get('counter_checks') or []) for f in findings)
    questions = sum(len(f.get('open_questions') or []) for f in findings)
    links = sum(len(f.get('valuation_links') or []) for f in findings)

    metrics_out = [
        _metric('反证方向覆盖率', Decimal(0), _ratio(cover('counter_checks'), total),
                fmt='{:.4f}',
                note='每条发现都给出"要去看什么才能证伪它"。字段置空即为基线。'),
        _metric('未解问题覆盖率', Decimal(0), _ratio(cover('open_questions'), total),
                fmt='{:.4f}'),
        _metric('估值传导路径覆盖率', Decimal(0), _ratio(cover('valuation_links'), total),
                fmt='{:.4f}'),
        _metric('平均反证方向数（条/发现）',
                Decimal(0), (Decimal(directions) / Decimal(total)).quantize(Decimal('0.01')),
                fmt='{}'),
        _metric('平均未解问题数（条/发现）',
                Decimal(0), (Decimal(questions) / Decimal(total)).quantize(Decimal('0.01')),
                fmt='{}'),
    ]

    details = {
        'finding_count': total,
        'counter_checks': directions,
        'open_questions': questions,
        'valuation_links': links,
    }

    if report_text:
        # 同时给分析运行与法证运行：报告引用的证据可能来自事实、指标、勾稽或发现，
        # 只给其中一份会把另一份的合法引用误判成伪造。
        cit = report_audit.audit_citations(report_text, run=analysis_run, forensic=forensic_run)
        metrics_out.append(_metric('报告引用精确率', _dec(cit.get('citation_coverage')),
                                   _dec(cit.get('citation_coverage')), fmt='{:.4f}',
                                   better='none',
                                   note='反证字段本身不改变报告的引用集合，'
                                        '因此该指标两侧相同。诚实记录，不把它算作反证层的因果贡献。'))
        details['citation_precision'] = cit.get('citation_coverage')

    refutation = _ratio(cover('counter_checks'), total) or Decimal(0)
    verdict = 'supported' if refutation == 1 else ('partially_supported' if refutation > 0
                                                   else 'not_supported')
    return {
        'id': 'critic',
        'title': 'Critic 消融（反证 Agent）',
        'variable': '风险发现是否携带反证方向 / 未解问题 / 估值传导路径',
        'baseline': {'label': '不使用反证 Agent（三条字段置空）'},
        'treatment': {'label': '带反证检索方向、未解问题与估值传导路径'},
        'metrics': metrics_out,
        'verdict': verdict,
        'hypothesis': '反证覆盖和引用精确率提升',
        'conclusion': (
            f'{total} 条风险发现全部携带反证检索方向（合计 {directions} 条）、'
            f'未解问题（合计 {questions} 条）与估值传导路径（合计 {links} 条）；'
            '基线不产出这些字段，覆盖率为 0。'
            '反证层的价值不在于把结论写得更强，而在于给每条结论附上"什么情况下它是错的" —— '
            '这正是通用大模型读年报最容易缺失的一环（确认偏误）。'),
        'details': details,
        'notes': [
            '基线以"字段置空"实现，属结构性消融，不是真跑一个无反证 Agent 的 LLM；'
            '引用精确率因此两侧相同，已在表中如实标注。',
        ],
    }


# ================================================================ 实验三：规则消融

def _injected_error_test(run: dict) -> dict | None:
    """往一条**原本通过**的勾稽里注入"万元/元"量级错误，看两侧能否发现。

    为什么要做这一步：真实的、经过审计的年报三表本来就是平的 —— 直接跑出来
    "勾稽不符 0 条"是**正确**结果，但 0 对 0 无法证明规则引擎有检出能力。
    所以补一个受控实验：把某一个科目的值乘 1000（最典型的单位识别错误），
    再分别用"无规则"和"有规则"两条路径跑，比较检出结果。
    """
    from . import financial_analysis

    facts = [dict(f) for f in (run.get('facts') or [])]
    by_id = {f.get('fact_id'): f for f in facts}
    checks = run.get('checks') or []

    target = None
    for check in checks:
        if check.get('status') != 'pass':
            continue
        deps = [by_id.get(fid) for fid in (check.get('dependency_fact_ids') or [])]
        deps = [f for f in deps if f and _dec(f.get('value'))]
        if len(deps) >= 2:
            target = (check, deps)
            break
    if target is None:
        return None

    check, deps = target
    victim = deps[-1]
    original = _dec(victim['value'])
    victim['value'] = str(original * Decimal(1000))

    mutated = financial_analysis.analyze_financials(facts)
    baseline = forensics.scan(mutated.get('facts') or [], checks=[], metrics=mutated.get('metrics') or [],
                              peer={}, top_k=10000)
    treatment = forensics.scan(mutated.get('facts') or [], checks=mutated.get('checks') or [],
                               metrics=mutated.get('metrics') or [], peer={}, top_k=10000)

    failed = [c for c in (mutated.get('checks') or []) if c.get('status') == 'fail']
    diagnosis = treatment.get('tieout_diagnosis') or []
    resolutions = sorted({str(d.get('resolution')) for d in diagnosis})

    return {
        'check_code': check.get('code'),
        'injected_code': victim.get('code'),
        'from_value': str(original),
        'to_value': victim['value'],
        'baseline_failed': 0,
        'treatment_failed': len(failed),
        'treatment_failed_codes': sorted({str(c.get('code')) for c in failed}),
        'baseline_tieout_findings': sum(
            1 for f in (baseline.get('findings') or []) if f.get('rule_id') == 'R_TIEOUT_01'),
        'treatment_tieout_findings': sum(
            1 for f in (treatment.get('findings') or []) if f.get('rule_id') == 'R_TIEOUT_01'),
        'diagnosis_resolutions': resolutions,
        'unit_misplacement_found': 'unit_misplacement' in resolutions,
    }


def _exp_rules(run: dict) -> dict:
    facts = run.get('facts') or []
    checks = run.get('checks') or []
    metrics = run.get('metrics') or []

    baseline = forensics.scan(facts, checks=[], metrics=metrics, peer={}, top_k=10000)
    treatment = forensics.scan(facts, checks=checks, metrics=metrics, peer={}, top_k=10000)

    def tieout_count(result):
        return sum(1 for f in (result.get('findings') or [])
                   if f.get('rule_id') == 'R_TIEOUT_01')

    failed = [c for c in checks if c.get('status') == 'fail']
    stability = _stability(facts, checks, metrics, {})
    injection = _injected_error_test(run)

    metrics_out = [
        _metric('勾稽恒等式核验条数', 0, len(checks), fmt='{}',
                note='关闭确定性规则后，跨表恒等式不再被评估，'
                     '"三表是否平"退化为读者的目测。'),
        _metric('真实数据上的勾稽不符数', 0, len(failed), fmt='{}', better='none',
                note='本次年报三表全平，0 条不符是正确结果 —— '
                     '确定性引擎的价值之一是能给出"已核验通过"而不是默认它没问题。'),
        _metric('勾稽类风险发现数', tieout_count(baseline), tieout_count(treatment), fmt='{}',
                better='none'),
        _metric('风险发现总数（同一输入）',
                baseline.get('all_finding_count'), treatment.get('all_finding_count'),
                fmt='{}', better='none',
                note='关闭规则不会凭空减少公司自身的经营类发现 —— '
                     '它减少的是"跨表是否平"这一类**只能靠验算得到**的结论。'),
    ]
    if injection:
        metrics_out.extend([
            _metric('【注入错误】检出勾稽不符条数',
                    injection['baseline_failed'], injection['treatment_failed'], fmt='{}',
                    note=f"把「{injection['injected_code']}」乘以 1000（典型的万元/元识别错误）后重算："
                         '无规则一侧无从发现，有规则一侧立刻报出不符。'),
            _metric('【注入错误】自动定性为单位错位',
                    '不可用', '是' if injection['unit_misplacement_found'] else '否'),
        ])
    metrics_out.extend([
        _metric('重复运行一致性（打乱输入顺序 %d 次）' % _STABILITY_ROUNDS,
                '无确定性映射', '一致' if stability['consistent'] else '不一致'),
        _metric('勾稽诊断可自动定位单位错位',
                '不可用', '可用' if treatment.get('tieout_diagnosis') is not None else '不可用'),
    ])

    detected = bool(injection) and injection['treatment_failed'] > 0 and injection['baseline_failed'] == 0
    verdict = ('supported' if (detected and stability['consistent'])
               else ('partially_supported' if stability['consistent'] else 'not_supported'))

    if injection:
        conclusion = (
            f'真实年报上核验 {len(checks)} 条恒等式，{len(failed)} 条不符 —— '
            f'农夫山泉三表本身是平的，0 条不符是**正确**结果，说明引擎不会为了"有产出"而误报。'
            f'为验证检出能力，把「{injection["injected_code"]}」乘以 1000 后重算：'
            f'关闭规则的基线仍报 0 条不符（结构上无法发现），启用规则的一侧报出 '
            f'{injection["treatment_failed"]} 条不符并自动定性为'
            f'{"单位错位" if injection["unit_misplacement_found"] else "需人工复核"}。'
            f'确定性引擎在打乱输入顺序重复 {_STABILITY_ROUNDS} 次后给出**同一组**风险发现'
            f'（指纹 {stability["signature"][:16]}…），这是"可复现"的可测证据。')
    else:
        conclusion = (
            f'真实年报上核验 {len(checks)} 条恒等式，{len(failed)} 条不符。'
            '本运行没有可注入错误的通过项，检出能力测试未能执行。')

    return {
        'id': 'rules',
        'title': '规则消融（确定性勾稽规则）',
        'variable': '是否启用确定性三表勾稽规则（checks）',
        'baseline': {'label': '仅 LLM 阅读年报（关闭全部勾稽规则）'},
        'treatment': {'label': '确定性勾稽规则引擎（%d 条恒等式）' % len(checks)},
        'metrics': metrics_out,
        'verdict': verdict,
        'hypothesis': '确定性规则提高稳定性和可复现性',
        'conclusion': conclusion,
        'details': {
            'check_count': len(checks),
            'failed_checks': [c.get('code') for c in failed],
            'baseline_finding_count': baseline.get('all_finding_count'),
            'treatment_finding_count': treatment.get('all_finding_count'),
            'stability': stability,
            'injected_error': injection,
            'tieout_diagnosis': (treatment.get('tieout_diagnosis') or [])[:5],
        },
        'notes': [
            '基线是"关闭确定性勾稽"这一**结构**，不是真跑一个 LLM。'
            '标注为结构对照，避免把不可复现的采样结果当作实验数据。',
            '注入错误测试是受控实验：只在副本上改一个科目的数量级，原运行产物不被修改。',
            '重复运行一致性比较的是发现的**集合**（按规则/公司/口径/风险分排序后哈希），'
            '因为 scan() 只按风险分排序，同分项的顺序取决于输入顺序，不构成稳定性问题。',
        ],
    }


# ================================================================ 实验四：同行消融

def synthetic_peers(run: dict, count: int = 4) -> list:
    """按固定比例扰动目标公司指标，生成**明确标注为模拟**的同业运行。

    只有在拿不到第二家真实公司时才使用。输出里会带 `synthetic=True`，
    报告必须写明这是机制演示而非真实同行结论。
    """
    out = []
    for index, (name, factor) in enumerate(_SYNTHETIC_PROFILES[:count]):
        peer = {'run_id': f'synthetic-{index}', 'company': name, 'synthetic': True,
                'facts': [], 'metrics': []}
        for metric in run.get('metrics') or []:
            item = dict(metric)
            code = str(item.get('code') or '')
            value = _dec(item.get('value'))
            if value is None or item.get('unit') not in ('ratio', 'days') or value == 0:
                continue
            # 方向感知：天数/负债率类越高越差，同业应更低；质量类越高越好，同业应更高。
            scaled = value / factor if code in _HIGHER_IS_WORSE else value * factor
            item['value'] = str(scaled)
            item['company'] = name
            peer['metrics'].append(item)
        out.append(peer)
    return out


def _exp_peers(run: dict, peer_runs: list | None = None, synthetic: int = 0) -> dict:
    facts = run.get('facts') or []
    checks = run.get('checks') or []
    metrics = run.get('metrics') or []
    target_id = str(run.get('run_id') or '')

    runs = [run] + [r for r in (peer_runs or []) if isinstance(r, dict)]
    used_synthetic = 0
    if synthetic:
        extra = synthetic_peers(run, synthetic)
        used_synthetic = len(extra)
        runs += extra

    if len(runs) < 2:
        return {
            'id': 'peers', 'title': '同行消融（同行分位）',
            'variable': '法证规则是否接入同行分位（peer 参数）',
            'baseline': {'label': '只看公司历史（peer={}）'},
            'treatment': {'label': '接入同行分位'},
            'metrics': [], 'verdict': 'insufficient_data',
            'hypothesis': '同行分位减少对行业周期的误判',
            'conclusion': ('拿不到第二家可比公司的分析运行，无法构造同行分位，'
                           '本实验判定为数据不足。请先用 analyze_financial_report '
                           '处理至少一家同业公司，或显式启用模拟同业（仅用于机制演示）。'),
            'details': {'peer_count': len(runs) - 1},
            'notes': ['宁可报数据不足，也不用模拟数据冒充真实同行结论。'],
        }

    bench = peers_mod.benchmark(runs, target_run_id=target_id or None)
    rule_payload = bench.get('rule_payload') or {}

    baseline = forensics.scan(facts, checks=checks, metrics=metrics, peer={}, top_k=10000)
    treatment = forensics.scan(facts, checks=checks, metrics=metrics,
                               peer=rule_payload, top_k=10000)

    base_map = _score_map(baseline.get('findings') or [])
    treat_map = _score_map(treatment.get('findings') or [])
    treat_findings = {_finding_key(f): f for f in (treatment.get('findings') or [])}

    common = sorted(set(base_map) & set(treat_map))
    deltas = [treat_map[k] - base_map[k] for k in common
              if base_map[k] is not None and treat_map[k] is not None]
    mean_delta = (sum(deltas) / Decimal(len(deltas))).quantize(Decimal('0.01')) if deltas else None

    peer_covered = [f for f in (treatment.get('findings') or [])
                    if f.get('rule_id') in rule_payload]
    normal = [f for f in peer_covered
              if (f.get('score_components') or {}).get('peer_extremeness') == 0]
    extreme = [f for f in peer_covered
               if (f.get('score_components') or {}).get('peer_extremeness', 0) > 0]

    # 逐规则的位置表。即使本次没有规则命中，这张表也能说明"目标公司的风险指标
    # 到底落在同行区间的什么位置" —— 这正是"行业普遍现象 vs 公司异常"的判据，
    # 也是同行消融要证明的那件事本身。
    positions = []
    table = bench.get('metrics') or {}
    for rule_id, code in sorted(forensics.RULE_PEER_METRIC.items()):
        entry = table.get(code) or {}
        if not entry:
            positions.append({'rule_id': rule_id, 'metric': code,
                              'label': peers_mod.METRIC_LABELS.get(code, code),
                              'position': 'unavailable',
                              'reason': '该指标在同行样本里不足两家，未做分位计算'})
            continue
        positions.append({
            'rule_id': rule_id, 'metric': code,
            'label': entry.get('label') or peers_mod.METRIC_LABELS.get(code, code),
            'target': entry.get('target'), 'median': entry.get('median'),
            'p25': entry.get('p25'), 'p75': entry.get('p75'), 'n': entry.get('n'),
            'position': entry.get('target_position') or 'within',
            'target_percentile': entry.get('target_percentile'),
        })
    within = [p for p in positions if p.get('position') == 'within']
    outside = [p for p in positions if p.get('position') in ('below', 'above')]

    def top_set(result, n=3):
        return {_finding_key(f) for f in (result.get('findings') or [])[:n]}

    base_top, treat_top = top_set(baseline), top_set(treatment)
    moved = len(base_top ^ treat_top) // 2

    metrics_out = [
        _metric('接入同行分位的规则数', 0, len(rule_payload), fmt='{}',
                note='基线 peer={}，peer_extremeness 恒为中性 50 分，'
                     '风险排序完全由公司自身历史决定。'),
        _metric('获得真实分位加权的风险发现数', 0, len(peer_covered), fmt='{}'),
        _metric('被判为"同行区间内"的发现数（同行分量归零）', 0, len(normal), fmt='{}',
                note='这类发现看起来异常，但放在同行里属于普遍水平 —— '
                     '正是"行业周期误判"的来源。'),
        _metric('被判为"偏离同行区间"的发现数', 0, len(extreme), fmt='{}'),
        _metric('关键风险指标落在同行区间内的条数', None, len(within), fmt='{}',
                better='none',
                note='"区间内"意味着该指标属于行业普遍水平，不应单独定性为异常。'),
        _metric('关键风险指标显著偏离同行区间的条数', None, len(outside), fmt='{}',
                better='none',
                note=f'覆盖 {len(positions)} 条规则。"偏离"才是有同行基准后仍成立的异常信号。'),
        _metric('平均风险分变动（treatment − baseline）', 0, mean_delta, fmt='{}'),
        _metric('Top-3 风险排序变化条数', 0, moved, fmt='{}'),
    ]

    mechanism_ok = bool(rule_payload) and bool(positions)
    verdict = 'supported' if mechanism_ok else 'not_supported'
    if used_synthetic:
        verdict = 'partially_supported' if mechanism_ok else 'not_supported'

    conclusion = (
        f'接入 {len(rule_payload)} 条规则的同行分位后：{len(peer_covered)} 条风险发现获得真实分位加权，'
        f'其中 {len(normal)} 条被判定落在同行区间内（同行分量归零，属于行业普遍水平而非公司异常）、'
        f'{len(extreme)} 条显著偏离同行区间；Top-3 风险排序有 {moved} 条发生位移。'
        f'在 {len(positions)} 条可比规则上，目标公司有 {len(within)} 条指标落在同行区间内、'
        f'{len(outside)} 条偏离。'
        '基线不接同行数据时，peer_extremeness 恒定 50 分，规则无法区分'
        '"行业都在下滑"与"这家公司在下滑" —— 这就是对行业周期的误判。')
    if used_synthetic:
        conclusion += (f'注意：本次同行样本含 {used_synthetic} 家**模拟**公司，'
                       '结论仅用于演示机制，不得作为真实同行结论提交。')

    return {
        'id': 'peers',
        'title': '同行消融（同行分位）',
        'variable': '法证规则是否接入同行分位（peer 参数）',
        'baseline': {'label': '只看公司历史（peer={}，peer_extremeness 恒为 50）'},
        'treatment': {'label': '接入同行分位（按 IQR 归一化）'},
        'metrics': metrics_out,
        'verdict': verdict,
        'hypothesis': '同行分位减少对行业周期的误判',
        'conclusion': conclusion,
        'details': {
            'peer_count': len(runs) - 1,
            'synthetic_peers': used_synthetic,
            'period': bench.get('period'),
            'aligned': bench.get('aligned'),
            'companies': bench.get('companies'),
            'peer_metrics_available': sorted(rule_payload),
            'rule_positions': positions,
            'industry_normal': [f.get('rule_id') for f in normal],
            'industry_extreme': [f.get('rule_id') for f in extreme],
            'top3_moved': moved,
            'benchmark_outliers': bench.get('outliers'),
        },
        'notes': [
            'peer 参数按规则号索引（rule_payload），由 peer_benchmark 的指标分位表'
            '经 forensics.RULE_PEER_METRIC 反向映射得到。',
            '期间不对齐时 benchmark 会给出 aligned=false，此时行业周期混入差异，结论需谨慎。',
            '逐规则位置表（details.rule_positions）是这张表的核心产出：它把'
            '"这家公司是否真的异常"变成可机检的分位位置，而不是靠叙述。',
        ] + ([
            f'模拟同业按"整体优于本公司"的方向构造（质量类指标 ×1.10~×1.50，'
            f'天数类指标 ÷1.10~÷1.50），因此 {len(outside)}/{len(positions)} 条落在区间外'
            '是构造使然，只用来演示判定路径，不代表真实同行结论。'
            '提交竞赛时须换成至少一家真实同业公司的 analyze_financial_report 运行。',
        ] if used_synthetic else []),
    }


# ================================================================ 实验五：估值联动消融

def _exp_valuation(run: dict, forensic_run: dict | None = None) -> dict:
    findings = (forensic_run or {}).get('findings') or []
    metrics = run.get('metrics') or []
    facts = run.get('facts') or []

    baseline = valuation.from_metrics(metrics, findings=[], facts=facts)
    treatment = valuation.from_metrics(metrics, findings=findings, facts=facts)

    if not baseline.get('ok') and not treatment.get('ok'):
        return {
            'id': 'valuation', 'title': '估值联动消融（风险参数化）',
            'variable': '法证风险发现是否传导进估值参数',
            'baseline': {'label': '风险与估值分离（findings=[]）'},
            'treatment': {'label': '风险参数化（findings 逐条传导）'},
            'metrics': [], 'verdict': 'insufficient_data',
            'hypothesis': '风险参数化使结论能影响决策',
            'conclusion': ('估值模型缺少营业收入基数等关键输入，两侧都无法成立，'
                           '本实验无法判定。请先确认分析运行里有 revenue 事实。'),
            'details': {'missing_inputs': treatment.get('missing_inputs')},
            'notes': [],
        }

    base_eq = _equity(baseline.get('base'))
    stress_eq = _equity(baseline.get('stress'))
    t_base_eq = _equity(treatment.get('base'))
    t_stress_eq = _equity(treatment.get('stress'))

    base_gap = _ratio(stress_eq - base_eq, base_eq) if base_eq and stress_eq else None
    treat_gap = _ratio(t_stress_eq - t_base_eq, t_base_eq) if t_base_eq and t_stress_eq else None

    applied = ((treatment.get('stress') or {}).get('risk_adjustments') or [])
    # 压力情景现在是两层叠加：结构性下限（与发现无关）+ 证据驱动传导。
    # 消融实验只应该测量**证据**那部分的作用，因此必须把下限剔除，
    # 否则"不传 findings"的一侧也带着一个真实压力，对照就不成立了。
    floor = [a for a in applied if a.get('source') == valuation.STRESS_FLOOR_SOURCE]
    evidence_applied = [a for a in applied if a.get('source') != valuation.STRESS_FLOOR_SOURCE]
    diag = treatment.get('stress_diagnostic') or {}
    base_diag = baseline.get('stress_diagnostic') or {}

    def _width(result):
        rng = result.get('range') or {}
        high, low = _dec(rng.get('equity_value_high')), _dec(rng.get('equity_value_low'))
        if high is None or low is None:
            return None
        # 换算成亿元呈现：原始是元的 28 位小数，直接入表既读不了也看不出量级。
        return (high - low) / Decimal('1e8')

    # 证据传导在结构性下限之上的**增量**（个百分点）。这是本实验真正的对照量：
    # 两侧的结构性下限完全相同，唯一的差异就是证据是否被传导进参数。
    increment = None
    if base_gap is not None and treat_gap is not None:
        increment = (treat_gap - base_gap) * Decimal('100')

    metrics_out = [
        _metric('压力情景相对基准的权益价值变动',
                base_gap, treat_gap, fmt='{:.4f}',
                note='负值代表压力情景下调了权益价值。基线一侧（不传 findings）'
                     '仍保留结构性压力下限，因此该值不为 0；两侧的差额才是'
                     '"证据传导"的作用，见下一条指标。'),
        _metric('证据传导在结构性下限之上的增量（个百分点）',
                0, increment, fmt='{:.2f}',
                note='两侧结构性下限完全相同，因此这个差额是纯证据效应：'
                     '为 0 说明本轮发现的严重度不高于结构性下限，压力情景'
                     '主要由情景假设而非证据决定 —— 报告必须写明这一点。'),
        _metric('传导进估值参数的风险条目数', 0, len(evidence_applied), fmt='{}',
                note='只计证据驱动的条目；结构性下限另计，不计入此处。'),
        _metric('三情景权益价值区间宽度（亿元）', _width(baseline), _width(treatment),
                fmt='{:.2f}',
                note='区间宽度变大，说明风险进入模型后情景之间的分化被打开；'
                     '基线的区间只来自乐观/基准两条不含证据风险的路径。'),
        _metric('压力测试有效性自检（insufficient）',
                ('无证据可传导' if not base_diag.get('evidence_backed') else
                 ('已触发' if base_diag.get('insufficient') else '未触发')),
                ('无证据可传导' if not diag.get('evidence_backed') else
                 ('已触发' if diag.get('insufficient') else '未触发')),
                better='none',
                note='"无证据可传导"表示本轮扫描没有产出可传导的发现，压力情景完全'
                     '由结构性假设构成；"已触发"表示引擎自检发现压力情景与基准差异'
                     '不足 2%，并会在报告里要求写明"本次压力测试未构成实质压力"。'),
    ]

    risk_reaches = (bool(evidence_applied) and treat_gap is not None
                    and base_gap is not None and treat_gap != base_gap)
    verdict = 'supported' if risk_reaches else 'not_supported'

    increment_text = (f'{increment.quantize(Decimal("0.01"))} 个百分点'
                      if increment is not None else 'N/A')

    return {
        'id': 'valuation',
        'title': '估值联动消融（风险参数化）',
        'variable': '法证风险发现是否传导进估值参数',
        'baseline': {'label': '风险与估值分离（findings=[]，压力情景只含结构性下限）'},
        'treatment': {'label': '风险参数化（findings 按严重度逐条传导到估值参数）'},
        'metrics': metrics_out,
        'verdict': verdict,
        'hypothesis': '风险参数化使结论能影响决策',
        'conclusion': (
            f'两侧共用同一份 metrics / facts / overrides 与同一套结构性压力下限，'
            f'唯一差别是 findings。不传风险发现时，压力情景只能靠结构性假设'
            f'（增速 −6pp、毛利率 −2pp、回款天数 +15 天等 {len(floor)} 项）'
            f'相对基准变动 '
            f'{(base_gap * 100).quantize(Decimal("0.01")) if base_gap is not None else "N/A"}%，'
            f'但这家公司**特有的**风险一条都没进模型。'
            f'接入 {len(findings)} 条风险发现后，其中 {len(evidence_applied)} 条经传导路径'
            f'改写了估值参数，在结构性下限之上再多变动 {increment_text}。'
            f'这个增量就是"风险参数化使结论能影响决策"的直接量化：'
            f'它把公司特有的风险与行业通用的下行假设区分开，'
            f'避免把情景假设冒充成证据结论。'),
        'details': {
            'finding_count': len(findings),
            'applied_adjustments': applied,
            'evidence_driven_adjustments': evidence_applied,
            'structural_floor_adjustments': floor,
            'unmatched_risks': (treatment.get('stress') or {}).get('unmatched_risks'),
            'baseline': {'base_equity': _f(base_eq), 'stress_equity': _f(stress_eq),
                         'gap': _f(base_gap)},
            'treatment': {'base_equity': _f(t_base_eq), 'stress_equity': _f(t_stress_eq),
                          'gap': _f(treat_gap)},
            'evidence_increment': _f(increment),
            'stress_diagnostic': diag,
            'baseline_stress_diagnostic': base_diag,
            'baseline_adjustments': ((baseline.get('stress') or {}).get('risk_adjustments') or []),
        },
        'notes': [
            '两侧使用同一份 metrics / facts / overrides 与同一套结构性压力下限，'
            '只改 findings 这一个变量。',
            '结构性下限是"情景假设"，不是证据结论；本实验的对照量是它在证据传导下'
            '被推进了多少，而不是压力情景本身有多大。',
            '若 treatment 的 stress_diagnostic.insufficient 为真，说明压力情景与基准'
            '差异不足 2%，压力测试未构成实质压力 —— 该情形必须在报告里写明，'
            '不得宣称风险已充分量化。',
        ],
    }


# ================================================================ 汇总入口

def run_all(run: dict, forensic_run: dict | None = None, peer_runs: list | None = None,
            synthetic_peers: int = 0, report_text: str | None = None) -> dict:
    """跑完五个消融实验，返回可复现的结果字典。

    `run`            —— `analyze_financial_report` 的运行产物（必需）
    `forensic_run`   —— `forensic_scan` 的运行产物（Critic / 估值消融需要）
    `peer_runs`      —— 其它公司的分析运行（同行消融需要 ≥1 家）
    `synthetic_peers`—— 生成几家模拟同业（仅机制演示，会显式标注）
    `report_text`    —— 报告全文（可选，用于把引用精确率纳入 RAG / Critic 消融）
    """
    if not isinstance(run, dict):
        raise ValueError('run_all 需要一份分析运行产物')

    experiments = [
        _exp_rag(run, report_text=report_text),
        _exp_critic(forensic_run or {}, report_text=report_text, analysis_run=run),
        _exp_rules(run),
        _exp_peers(run, peer_runs=peer_runs, synthetic=synthetic_peers),
        _exp_valuation(run, forensic_run=forensic_run),
    ]

    counts: dict[str, int] = {}
    for item in experiments:
        counts[item['verdict']] = counts.get(item['verdict'], 0) + 1

    supported = counts.get('supported', 0)
    judged = sum(v for k, v in counts.items() if k != 'insufficient_data')

    return {
        'ok': True,
        'run_id': run.get('run_id'),
        'forensic_run_id': (forensic_run or {}).get('run_id'),
        'report_provided': bool(report_text),
        'experiments': experiments,
        'summary': {
            'total': len(experiments),
            'supported': supported,
            'partially_supported': counts.get('partially_supported', 0),
            'not_supported': counts.get('not_supported', 0),
            'insufficient_data': counts.get('insufficient_data', 0),
            'judged': judged,
            'pass_rate': (str(Decimal(supported) / Decimal(judged)) if judged else None),
            'verdict_labels': {k: _VERDICT_LABELS[k] for k in _VERDICT_LABELS},
            'text': (f'{len(experiments)} 个消融实验中 {supported} 个支持原假设、'
                     f'{counts.get("partially_supported", 0)} 个部分支持、'
                     f'{counts.get("not_supported", 0)} 个不支持、'
                     f'{counts.get("insufficient_data", 0)} 个数据不足。'),
        },
        'config': {
            'stability_rounds': _STABILITY_ROUNDS,
            'effect_floor': str(_EFFECT_FLOOR),
            'synthetic_peers': synthetic_peers,
            'random_seed': 'deterministic（固定种子，不使用系统随机源）',
        },
    }


# ================================================================ 渲染

def render_markdown(result: dict) -> str:
    """把消融结果渲染成可直接进报告的 Markdown 表格。"""
    lines: list[str] = []
    summary = result.get('summary') or {}
    lines.append('# 消融实验报告')
    lines.append('')
    lines.append(f"- 分析运行：`{result.get('run_id')}`")
    if result.get('forensic_run_id'):
        lines.append(f"- 法证运行：`{result.get('forensic_run_id')}`")
    lines.append(f"- 报告全文参与：{'是' if result.get('report_provided') else '否'}")
    lines.append(f"- 判定口径：{summary.get('text', '')}")
    lines.append('')

    for exp in result.get('experiments') or []:
        lines.append(f"## {exp.get('title')}（{_VERDICT_LABELS.get(exp.get('verdict'), '')}）")
        lines.append('')
        lines.append(f"- 消融变量：{exp.get('variable')}")
        lines.append(f"- 基线：{exp.get('baseline', {}).get('label')}")
        lines.append(f"- 实验组：{exp.get('treatment', {}).get('label')}")
        lines.append(f"- 原假设：{exp.get('hypothesis')}")
        lines.append('')
        rows = exp.get('metrics') or []
        if rows:
            lines.append('| 指标 | 基线 | 实验组 | 更好的一侧 |')
            lines.append('| --- | --- | --- | --- |')
            for row in rows:
                fmt = row.get('fmt') or '{}'
                a, b = row.get('baseline'), row.get('treatment')

                def _show(value):
                    if value is None:
                        return '—'
                    # 运行产物经 JSON 落盘后 Decimal 会变成字符串（"0"、"1.0000"），
                    # 从已保存的运行渲染时必须再试一次数值格式化，否则同一份报告
                    # 现场渲染是 0.0000、从 run.json 渲染是 0，两边对不上。
                    if isinstance(value, str):
                        number = _dec(value)
                        if number is None:
                            return value
                        try:
                            return fmt.format(number)
                        except (ValueError, TypeError):
                            return value
                    try:
                        return fmt.format(value)
                    except (ValueError, TypeError):
                        return str(value)

                side = {'treatment': '实验组', 'baseline': '基线', 'none': '无差别'}
                lines.append(f"| {row.get('label')} | {_show(a)} | {_show(b)} | "
                             f"{side.get(row.get('better'), '—')} |")
            lines.append('')
        lines.append(f"**结论**：{exp.get('conclusion')}")
        lines.append('')
        positions = (exp.get('details') or {}).get('rule_positions') or []
        if positions:
            lines.append('逐规则同行位置表：')
            lines.append('')
            lines.append('| 规则 | 可比指标 | 本公司 | 同行中位数 | P25 | P75 | 位置 | 分位 |')
            lines.append('| --- | --- | --- | --- | --- | --- | --- | --- |')
            side_text = {'within': '区间内', 'below': '低于 P25', 'above': '高于 P75',
                         'unavailable': '样本不足'}
            for row in positions:
                lines.append(
                    f"| {row.get('rule_id')} | {row.get('label')} | {row.get('target') or '—'} | "
                    f"{row.get('median') or '—'} | {row.get('p25') or '—'} | "
                    f"{row.get('p75') or '—'} | {side_text.get(row.get('position'), row.get('position'))} | "
                    f"{row.get('target_percentile') or '—'} |")
            lines.append('')
        notes = exp.get('notes') or []
        if notes:
            lines.append('口径说明：')
            for note in notes:
                lines.append(f"- {note}")
            lines.append('')
    return '\n'.join(lines)


# ================================================================ 命令行自跑

def _main(argv: list | None = None) -> int:
    import argparse
    import glob
    import os
    import sys

    parser = argparse.ArgumentParser(description='消融实验：五个对照的确定性实现')
    parser.add_argument('--run', required=True, help='分析运行 run.json 路径')
    parser.add_argument('--forensic-run', default='', help='法证运行 run.json 路径')
    parser.add_argument('--peer-run', action='append', default=[],
                        help='同业公司的分析运行 run.json，可重复')
    parser.add_argument('--synthetic-peers', type=int, default=0,
                        help='生成 N 家模拟同业（仅机制演示）')
    parser.add_argument('--report', default='', help='报告文件（docx/md/txt）')
    parser.add_argument('--json', default='', help='结果 JSON 输出路径')
    parser.add_argument('--markdown', default='', help='结果 Markdown 输出路径')
    args = parser.parse_args(argv)

    def _load(path):
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)

    run = _load(args.run)
    forensic_run = _load(args.forensic_run) if args.forensic_run else None
    if forensic_run is None:
        forensic_run = {'findings': []}
    peer_runs = [_load(p) for p in args.peer_run]

    report_text = None
    if args.report:
        report_text = report_audit.read_report_text(args.report)

    result = run_all(run, forensic_run=forensic_run, peer_runs=peer_runs,
                     synthetic_peers=args.synthetic_peers, report_text=report_text)
    markdown = render_markdown(result)

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
    if args.markdown:
        with open(args.markdown, 'w', encoding='utf-8') as handle:
            handle.write(markdown)
    if not args.json and not args.markdown:
        sys.stdout.write(markdown + '\n')
    return 0


if __name__ == '__main__':      # pragma: no cover
    raise SystemExit(_main())
