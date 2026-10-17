"""Conservative, reproducible internal report rubric (not a competition/CFA rubric).

Automated checks assess observable facts only. Human interpretation never receives
points for headings or keyword counts, and unavailable evidence is not a pass.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

RULES_VERSION = 'internal-report-quality-1.0'
WEIGHTS = {'accuracy': 30, 'evidence': 20, 'method': 20,
           'argument': 15, 'risk': 10, 'presentation': 5}
NAMES = {'accuracy': '数据与事实准确性', 'evidence': '证据可追溯',
         'method': '方法与可复算', 'argument': '论证与反证',
         'risk': '风险和不确定性', 'presentation': '表达与披露'}
_EVIDENCE = re.compile(r'(?<![0-9a-z])[0-9a-f]{8,64}:p\d+:[bt]\d+(?![0-9a-z])|(?<![0-9a-z])E\d{3,}(?![0-9a-z])', re.I)
_GUARANTEE = re.compile(r'保证(?:收益|盈利|赚钱|回报)|收益保证|保本保收益|稳赚(?:不赔)?|包赚|必涨|必赚|无风险(?:收益|投资|套利)|确定性(?:收益|回报)|必(?:将|定)(?:上涨|涨停|大涨|翻倍)|肯定(?:会)?(?:上涨|涨停)|一定会(?:上涨|涨)')
_PASS = {'verified', 'pass', 'passed', 'ok', 'calculated'}
_FAIL = {'contradicted', 'error', 'fail', 'failed'}


def _rows(value):
    return [v for v in (value or []) if isinstance(v, dict)] if isinstance(value, list) else []


def _sources():
    path = Path(__file__).with_name('report_quality_sources.json')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []


def _known_evidence(*runs):
    known = set()
    for run in runs:
        if not isinstance(run, dict):
            continue
        for name in ('facts', 'metrics', 'checks', 'signals', 'findings', 'review_queue', 'evidence'):
            for row in _rows(run.get(name)):
                if row.get('evidence_id'):
                    known.add(str(row['evidence_id']))
                for field in ('evidence_ids', 'support_evidence_ids'):
                    known.update(str(v) for v in row.get(field) or [])
    return known


def _registered(evidence_id, known):
    if evidence_id in known:
        return True
    doc, sep, locator = evidence_id.partition(':')
    if not sep or len(doc) < 8:
        return False
    return any(k.partition(':')[2] == locator and
               (k.partition(':')[0].startswith(doc) or doc.startswith(k.partition(':')[0]))
               for k in known if ':' in k)


def _guarantees(text):
    """Negation must govern this occurrence within this clause, never a whole report."""
    results = []
    for clause in re.split(r'[。！？；;\n，,]|但(?:是)?|然而|不过', text):
        for match in _GUARANTEE.finditer(clause):
            prefix = clause[:match.start()]
            # Only immediate grammatical negation: unrelated disclaimers cannot
            # neutralize a later promise, even within one sentence.
            negated = re.search(r'(?:不|未|不能|不会|不得|禁止|拒绝|无法|没有|并非|不是|不作|不做|不提供|不承诺|不应)(?:作出|提供|承诺|宣称|声称|给予|存在|任何|所谓|作)?\s*[“"\']?\s*$', prefix)
            suffix = clause[match.end():]
            denied_after = re.match(r'[”"\']?(?:的)?(?:说法|承诺|表述)?(?:并非事实|并不成立|不成立|不可信|不可取|是不当|是错误|属于错误)', suffix)
            if not negated and not denied_after:
                results.append({'code': 'return_guarantee', 'severity': 'critical',
                                'matched': match.group(), 'text': clause.strip(),
                                'reason': '收益确定性承诺需修订；否定仅按当前短语作用域判断。'})
    return results


def _check(key, weight, passed, detail):
    return {'id': key, 'weight': weight,
            'status': 'unassessable' if passed is None else ('pass' if passed else 'fail'),
            'score': None if passed is None else (weight if passed else 0),
            'detail': detail, 'assessment_type': 'machine_verifiable'}


def _dimension(key, checks, human_reason=None):
    maximum = WEIGHTS[key]
    assessed = sum(c['weight'] for c in checks if c['score'] is not None)
    earned = sum(c['score'] for c in checks if c['score'] is not None)
    status = 'assessed' if assessed == maximum else ('partial' if assessed else 'unassessable')
    if human_reason and not assessed:
        status = 'human_review_required'
    return {'key': key, 'name': NAMES[key], 'max_score': maximum,
            'score': round(earned / assessed * maximum, 2) if assessed else None,
            'assessed_weight': round(assessed, 4), 'assessed_score': round(earned, 4),
            'coverage': round(assessed / maximum, 4), 'status': status,
            'basis': '仅按列出的机器检查在已评估范围归一化；未检查部分不计分。',
            'human_review': human_reason, 'checks': checks}


def evaluate_quality(text, claims: dict, run=None, valuation=None, forensic=None, formula_review=None) -> dict:
    """Evaluate auditable subsets of a 100-point internal rubric.

    ``claims`` contains ``claims`` rows and optional ``stats``. Verified and
    contradicted rows must originate in a separate claim verification engine;
    the report itself cannot register evidence or certify its own facts.
    """
    text = text if isinstance(text, str) else ''
    claims = claims if isinstance(claims, dict) else {}
    rows = _rows(claims.get('claims'))
    stats = claims.get('stats') if isinstance(claims.get('stats'), dict) else {}
    known = _known_evidence(run, forensic)
    flags = _guarantees(text)
    total = len(rows)
    # Row-level results take precedence over summaries, avoiding inflated counts.
    verified = sum(c.get('status') in _PASS and bool(c.get('evidence_ids'))
                   and all(_registered(str(e), known) for e in c['evidence_ids']) for c in rows)
    contradicted = sum(c.get('status') in _FAIL for c in rows)
    resolved = verified + contradicted
    checked = total
    if not rows:
        def count(key):
            value = stats.get(key, 0)
            return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0
        checked = count('checked')
        # Aggregate counts alone cannot certify source-backed accuracy.
        verified, contradicted = 0, count('contradicted')
        total = max(checked, count('verified') + contradicted + count('unverifiable'))
        resolved = min(verified + contradicted, total)
    for row in rows:
        if row.get('status') in _FAIL:
            flags.append({'code': 'critical_contradiction', 'severity': 'critical',
                          'claim_id': row.get('claim_id'), 'text': row.get('claim_text', row.get('text')),
                          'reason': row.get('reason') or '财务主张与已核验资料矛盾，需修订后交付。'})
    if not rows and contradicted:
        flags.append({'code': 'critical_contradiction', 'severity': 'critical',
                      'reason': '已核验主张摘要包含矛盾，需取得明细并修订。', 'count': contradicted})
    cited = set(_EVIDENCE.findall(text))
    cited.update(str(e) for row in rows for e in row.get('evidence_ids') or [])
    unknown = sorted(e for e in cited if not _registered(e, known))
    if unknown:
        flags.append({'code': 'unknown_citation', 'severity': 'critical', 'evidence_ids': unknown,
                      'reason': '引用未在输入证据登记中定位，待补材料；不能据此认定故意伪造。'})
    accuracy = []
    if total:
        accuracy.append(_check('verified_claim_fraction', 30 * resolved / total,
                               None if not resolved else True,
                               {'verified': verified, 'contradicted': contradicted, 'total': total,
                                'unresolved': total - resolved}))
        if resolved:
            accuracy[-1]['score'] = 30 * verified / total
        if resolved < total:
            accuracy.append(_check('unresolved_claims', 30 * (total - resolved) / total, None,
                                   '待核验主张不按通过计分。'))
    evidence = []
    if total or cited:
        # Presence and registry membership are observable, not evidence sufficiency.
        traceable = sum(bool(row.get('evidence_ids')) and all(_registered(str(e), known)
                        for e in row['evidence_ids']) for row in rows)
        eligible = total if rows else len(cited)
        successes = traceable if rows else len(cited) - len(unknown)
        evidence = [_check('registered_claim_evidence', 10, bool(eligible),
                           {'traceable': successes, 'total': eligible, 'unknown': unknown})]
        evidence[0]['score'] = 10 * successes / eligible if eligible else 0
    method = []
    valuation = valuation if isinstance(valuation, dict) else {}
    base = (valuation.get('scenarios') or {}).get('base') or {}
    parameters = base.get('parameters') or valuation.get('parameters') or {}
    provenance = valuation.get('assumption_sources') or valuation.get('inputs_used') or {}
    if valuation:
        missing = [k for k, v in parameters.items() if v is None or not isinstance(provenance.get(k), dict)
                   or provenance[k].get('source') in (None, 'default_assumption')]
        output = base.get('valuation') or valuation.get('valuation') or {}
        method.append(_check('valuation_inputs_provenance', 5,
                             bool(parameters) and not missing,
                             {'missing_or_default_inputs': missing, 'parameter_count': len(parameters)}))
        method.append(_check('valuation_computation_status', 5,
                             bool(output) and valuation.get('ok') is True,
                             {'ok': valuation.get('ok'), 'output_present': bool(output)}))
        if missing or not parameters or not output:
            flags.append({'code': 'valuation_reproduction_gap', 'severity': 'warning',
                          'reason': '核心估值缺少可复算输入、来源或结果；需人工核对模型导出。',
                          'missing_inputs': missing})
    elif isinstance(run, dict):
        calculations = [c for c in _rows(run.get('checks')) if c.get('formula') and c.get('inputs')]
        if calculations:
            method.append(_check('reproducible_calculations', 10, True,
                                 {'count': len(calculations), 'passed': sum(c.get('status') in _PASS for c in calculations)}))
            method[-1]['score'] = 10 * sum(c.get('status') in _PASS for c in calculations) / len(calculations)
    if not method and isinstance(formula_review, dict):
        equations = [c for c in formula_review.get('checks', []) if c.get('status') in {'pass', 'fail'}]
        if equations:
            method.append(_check('explicit_report_arithmetic', 10, True, {'count': len(equations), 'basis': '只复算报告操作数，未评价其原始证据和方法适用性'}))
            method[-1]['score'] = 10 * sum(c['status'] == 'pass' for c in equations) / len(equations)
    risk = []
    substantive = [s.strip() for s in re.split(r'[。；\n]', text) if len(s.strip()) >= 12 and not s.strip().startswith('#')]
    if substantive:
        unresolved = [c for c in rows if c.get('status') not in _PASS | _FAIL]
        uncertainty = [s for s in substantive if re.search(r'未核验|无法核验|待核验|缺少|缺失|不确定|假设', s)
                       and re.search(r'数据|证据|资料|输入|预测|估值|范围|结论|覆盖|口径', s)]
        # A concrete driver linked to a consequence, not the heading "风险".
        risk_links = [s for s in substantive if re.search(r'需求|利率|汇率|回款|应收|债务|流动性|成本|增长|毛利', s)
                      and re.search(r'下降|上升|恶化|不足|波动|延迟|违约', s)
                      and re.search(r'导致|影响|使得|可能使|将使|压低|增加|降低|减少', s)]
        risk = [_check('no_return_guarantees', 1, not any(f['code'] == 'return_guarantee' for f in flags),
                       '只检查确定性承诺，不推断投资建议整体合规。'),
                _check('specific_uncertainty_disclosure', 1, bool(uncertainty),
                       {'disclosures': uncertainty, 'unresolved_claim_ids': [c.get('claim_id') for c in unresolved]}),
                _check('risk_driver_consequence', 2, bool(risk_links), {'statements': risk_links})]
    dimensions = [_dimension('accuracy', accuracy),
                  _dimension('evidence', evidence, '原文支持程度、来源独立性和充分性需人工评价。'),
                  _dimension('method', method, '模型适用性、公式经济逻辑和预测假设需人工复算审阅。'),
                  _dimension('argument', [], '人工判断证据是否支持结论、因果解释与重要反证；章节和关键词不证明逻辑。'),
                  _dimension('risk', risk, '风险重要性、遗漏、披露充分性需人工判断。'),
                  _dimension('presentation', [], '人工判断表达清晰、事实预测分离、利益冲突和披露完整性。')]
    assessed_weight = sum(d['assessed_weight'] for d in dimensions)
    assessed_score = sum(d['assessed_score'] for d in dimensions)
    coverage = resolved / total if total else None
    if not text.strip():
        flags.append({'code': 'empty_report', 'severity': 'critical', 'reason': '报告为空，无法评价。'})
    sources = _sources()
    return {'rules_version': RULES_VERSION, 'rubric': {'name': '本项目内部研报质量建议量表',
             'weights': WEIGHTS.copy(), 'max_score': 100, 'official_competition_rule': False,
             'cfa_prescribed_weights': False,
             'note': '权重为本项目内部建议，CFA/PCAOB等资料仅作原则参考，不是官方赛事或CFA评分。'},
            'report_hash': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'dimensions': dimensions, 'assessed_weight': round(assessed_weight, 4),
            'assessed_score': round(assessed_score, 4), 'assessment_coverage': round(assessed_weight / 100, 4),
            'claim_coverage': round(coverage, 4) if coverage is not None else None,
            'provisional_score': round(100 * assessed_score / assessed_weight, 2) if assessed_weight else None,
            'overall_total': None, 'status': 'blocked' if any(f['severity'] == 'critical' for f in flags) else 'incomplete',
            'delivery_blocked': any(f['severity'] == 'critical' for f in flags), 'flags': flags,
            'sources': sources, 'source_registry': 'docs/kb-sources-reports.json',
            'limitations': ['归一化暂评分仅代表已检查范围，不能与完整核验报告直接排名。',
                            '人工维度未评审，总分为null；登记证据存在不等于内容真实或充分。',
                            '核查覆盖率不是置信概率，未发现错误不等于已证明正确。']}
