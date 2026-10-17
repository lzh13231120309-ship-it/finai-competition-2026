"""可复现凭证（repro.json）。

竞赛《评测体系与目标值》第九节要求：每次运行生成 `repro.json`，记录
Git 提交、模型版本、Prompt 哈希、数据 manifest 哈希、参数、随机种子、
起止时间与输出文件哈希；答辩时展示同一 `run_id` 如何被复现。

本项目的数值计算全部是确定性 Python（Decimal 运算 + 规则引擎），**不使用随机数**。
所以"可复现"的判据非常干净：

    同一份输入 + 同一份代码 + 同一份 Prompt  →  逐位相同的输出哈希

`output_hashes` 就是对"事实 / 指标 / 勾稽"三组结果做规范化后取 sha256。
规范化这一步是必须的：原始记录里含 `evidence_url`（内嵌 task_id）这类
每次运行都不同的字段，直接哈希会导致假的不一致。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# 代码指纹与 Prompt 哈希在一次进程里不会变，缓存起来避免每次保存运行都重算。
_FINGERPRINT_CACHE: dict = {}
_PROMPT_CACHE: dict = {}

# 参与指纹的源码目录：server/ 下的全部 .py。
_SOURCE_SUFFIX = '.py'


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _hash_json(value) -> str:
    """稳定哈希：键排序 + 紧凑分隔符，保证同一份数据永远得到同一个哈希。"""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                      separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def code_fingerprint() -> dict:
    """server/ 下源码文件的指纹。

    不依赖 git —— 打包后的应用没有 `.git` 目录，但源码文件一定在。
    这样"代码版本"依然可验证，对得上竞赛"记录模型与 Prompt 版本"的要求。
    """
    if 'code' in _FINGERPRINT_CACHE:
        return _FINGERPRINT_CACHE['code']
    folder = Path(__file__).resolve().parent
    combined = hashlib.sha256()
    files: dict[str, str] = {}
    for path in sorted(folder.glob(f'*{_SOURCE_SUFFIX}')):
        digest = _sha256_file(path)
        if digest is None:
            continue
        files[path.name] = digest[:16]
        combined.update(path.name.encode('utf-8'))
        combined.update(digest.encode('utf-8'))
    result = {'files': len(files), 'combined': combined.hexdigest()[:32], 'sources': files}
    _FINGERPRINT_CACHE['code'] = result
    return result


def prompt_hash() -> str:
    if 'value' in _PROMPT_CACHE:
        return _PROMPT_CACHE['value']
    try:
        from . import config
        prompt = str(config.load().get('system_prompt') or '')
    except Exception:                                     # pragma: no cover
        prompt = ''
    value = hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:32]
    _PROMPT_CACHE['value'] = value
    return value


def model_info() -> dict:
    try:
        from . import config
        cfg = config.load()
    except Exception:                                     # pragma: no cover
        return {}
    return {'provider': cfg.get('provider'), 'model': cfg.get('model'),
            'temperature': cfg.get('temperature'), 'max_tokens': cfg.get('max_tokens')}


def normalize_facts(facts: list | None) -> list:
    """只保留跨运行稳定的字段。

    `evidence_url` 内嵌 task_id，`fact_id` 由内容派生但排序后仍需稳定 ——
    这里只取"内容字段"，把标识类字段排除在外，哈希才代表"事实本身"。
    """
    keys = ('code', 'raw_label', 'company', 'period', 'period_type', 'scope',
            'currency', 'value', 'raw_value', 'unit', 'scale', 'status',
            'source_tier', 'statement')
    rows = [{k: item.get(k) for k in keys} for item in facts or []]
    rows.sort(key=lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True, default=str))
    return rows


def normalize_metrics(metrics: list | None) -> list:
    keys = ('code', 'period', 'scope', 'value', 'unit', 'status', 'formula')
    rows = [{k: item.get(k) for k in keys} for item in metrics or []]
    rows.sort(key=lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True, default=str))
    return rows


def normalize_checks(checks: list | None) -> list:
    keys = ('code', 'period', 'scope', 'status', 'expected', 'actual', 'difference')
    rows = [{k: item.get(k) for k in keys} for item in checks or []]
    rows.sort(key=lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True, default=str))
    return rows


def normalize_findings(findings: list | None) -> list:
    keys = ('rule_id', 'company', 'periods', 'severity', 'risk_score',
            'score_components', 'metric_codes')
    rows = [{k: item.get(k) for k in keys} for item in findings or []]
    rows.sort(key=lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True, default=str))
    return rows


def output_hashes(run: dict) -> dict:
    """对运行的核心结果取哈希。这就是"复现判据"。"""
    out: dict[str, str] = {}
    if run.get('facts') is not None:
        out['facts'] = _hash_json(normalize_facts(run.get('facts')))
        out['fact_count'] = len(run.get('facts') or [])
    if run.get('metrics') is not None:
        out['metrics'] = _hash_json(normalize_metrics(run.get('metrics')))
        out['metric_count'] = len(run.get('metrics') or [])
    if run.get('checks') is not None:
        out['checks'] = _hash_json(normalize_checks(run.get('checks')))
        out['check_count'] = len(run.get('checks') or [])
    if run.get('findings') is not None:
        out['findings'] = _hash_json(normalize_findings(run.get('findings')))
        out['finding_count'] = len(run.get('findings') or [])
    for key in ('grade', 'peer_payload', 'range'):
        if run.get(key) is not None:
            out[key] = _hash_json(run[key])
    return out


def build(payload: dict) -> dict:
    """为一次运行生成 repro 凭证。"""
    documents = payload.get('documents') or []
    inputs = [{'filename': d.get('filename'), 'sha256': d.get('sha256'),
               'parser_version': d.get('parser_version'),
               'document_id': d.get('document_id')}
              for d in documents if isinstance(d, dict)]
    # 没有 documents 字段时（如法证/估值运行），从父运行继承输入指纹
    return {
        'run_id': payload.get('run_id'),
        'kind': payload.get('kind'),
        'tool_version': payload.get('tool_version'),
        'code_fingerprint': code_fingerprint(),
        'prompt_hash': prompt_hash(),
        'model': model_info(),
        'inputs': inputs,
        'parameters': payload.get('parameters') or {},
        'created_at': payload.get('created_at'),
        'random_seed': 'deterministic（本引擎不使用随机数）',
        'output_hashes': output_hashes(payload),
        'parent_run_id': payload.get('parent_run_id') or '',
        'note': ('同一份输入 + 同一份代码 + 同一份 Prompt 应得到逐位相同的 '
                 'output_hashes。用 `finance_cli repro` 重跑对比即可验证。'),
    }


def compare(old: dict, new: dict) -> dict:
    """对比两次运行的输出哈希。"""
    old_hashes = (old or {}).get('output_hashes') or {}
    new_hashes = (new or {}).get('output_hashes') or {}
    keys = sorted(set(old_hashes) | set(new_hashes))
    differences = []
    for key in keys:
        before, after = old_hashes.get(key), new_hashes.get(key)
        if before != after:
            differences.append({'field': key, 'before': before, 'after': after})
    code_ok = ((old or {}).get('code_fingerprint') or {}).get('combined') == \
              ((new or {}).get('code_fingerprint') or {}).get('combined')
    prompt_ok = (old or {}).get('prompt_hash') == (new or {}).get('prompt_hash')
    return {
        'reproducible': not differences,
        'differences': differences,
        'code_unchanged': code_ok,
        'prompt_unchanged': prompt_ok,
        'checked_fields': keys,
        'note': ('代码或 Prompt 有变动时，输出哈希变化是预期的；'
                 '两者都没变而哈希不同，才是真正的不确定性问题。'),
    }
