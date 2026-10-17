"""Offline financial tools for desktop app and cross-harness skill use."""
from __future__ import annotations

import argparse
import json
import shutil
import uuid
from pathlib import Path


def main(argv=None):
    from . import config, finance_service as svc
    parser = argparse.ArgumentParser(description='本地金融文档与财报工具（金额/证据/复核/导出）')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'extract', 'analyze'):
        p = sub.add_parser(name)
        p.add_argument('--input', nargs='+', required=True, help='一个或多个本地源文件')
        p.add_argument('--output', type=Path)
        p.add_argument('--company', default='')
        p.add_argument('--period', default='')
        p.add_argument('--scope', choices=['auto', 'consolidated', 'parent', 'unknown'], default='auto')
        if name == 'inspect':
            p.add_argument('--page', type=int)
            p.add_argument('--query', default='')
    exp = sub.add_parser('export')
    exp.add_argument('--run', type=Path, required=True, help='已保存的run.json')
    exp.add_argument('--format', choices=['xlsx', 'docx', 'json', 'md'], default='xlsx')
    exp.add_argument('--output', type=Path, required=True)
    # forensic / valuation 是"可复现"考核点的落地：一条命令跑完
    # 分析 → 法证 → 估值，不需要靠交互式对话复述中间结果。
    for name, help_text in (('forensic', '在财报分析结果上运行确定性财务法证规则'),
                            ('valuation', '三情景预测与 DCF 估值压力测试（含倍数交叉验证）'),
                            ('model', '完整估值建模：配平三表 + WACC 财报校准 + 方法三角 + 压力情景')):
        p = sub.add_parser(name, help=help_text)
        if name in ('forensic', 'valuation', 'model'):
            p.add_argument('--input', nargs='+', required=(name != 'model'), help='一个或多个本地源文件')
            p.add_argument('--run', default='', help='已有的 analysis run_id（优先于 --input）')
        p.add_argument('--output', type=Path)
        p.add_argument('--company', default='')
        p.add_argument('--period', default='')
        p.add_argument('--scope', choices=['auto', 'consolidated', 'parent', 'unknown'], default='auto')
        p.add_argument('--peer', default='',
                       help='同行数据 JSON，如 {"gross_margin":[0.5,0.55],"receivables_days":[30,35]}')
        if name in ('valuation', 'model'):
            p.add_argument('--overrides', default='',
                           help='驱动变量覆盖 JSON，如 {"wacc":0.11,"revenue_growth":0.05,'
                                '"comparable_pe":20,"comparable_ev_ebitda":15}')
        if name == 'valuation':
            p.add_argument('--no-forensic', action='store_true',
                           help='跳过法证扫描：压力情景将退化为基准情景，仅用于对照')
    # audit 是"交付前自检"的落地：报告写完先跑一遍确定性核对，
    # 再决定能不能交。对应竞赛报告评分量表五维度与一票否决项。
    aud = sub.add_parser('audit', help='对报告做交付前确定性自检（五维度量表 + 一票否决 + 引用核验）')
    aud.add_argument('--report', type=Path, required=True, help='待审计的报告文件（.md/.txt/.docx）')
    aud.add_argument('--input', nargs='+', help='本地源文件（财报）。仅当报告里没有可认领的运行ID时才用')
    aud.add_argument('--run', default='', help='直接指定分析运行ID，优先级最高')
    aud.add_argument('--forensic-run', default='')
    aud.add_argument('--valuation-run', default='')
    aud.add_argument('--output', type=Path)
    aud.add_argument('--company', default='')
    aud.add_argument('--period', default='')
    aud.add_argument('--scope', choices=['auto', 'consolidated', 'parent', 'unknown'], default='auto')
    aud.add_argument('--with-valuation', action='store_true',
                     help='现跑数据源时同时跑三情景估值，把估值数字一并纳入溯源池')
    aud.add_argument('--overrides', default='', help='估值驱动变量覆盖 JSON')
    rep = sub.add_parser('repro', help='重跑并验证可复现性：对比两次运行的输出哈希')
    rep.add_argument('--input', nargs='+', required=True,
                     help='原始财报文件（须与首次运行用的是同一份）')
    rep.add_argument('--run', type=Path, required=True, help='首次运行的 run.json')
    rep.add_argument('--output', type=Path)
    # ablate 是竞赛第三节"消融实验（四个对照）"的落地：一条命令跑完五个对照，
    # 输出可复现的实验报告，不需要靠对话复述中间结果。
    abl = sub.add_parser('ablate', help='跑五个消融对照实验（RAG/Critic/规则/同行/估值联动）')
    abl.add_argument('--input', nargs='+', required=True, help='一个或多个本地源文件（财报）')
    abl.add_argument('--peer-run', action='append', default=[],
                     help='同业公司的 run.json 路径，可重复；同行消融需要 ≥1 家')
    abl.add_argument('--synthetic-peers', type=int, default=0,
                     help='生成 N 家模拟同业（仅机制演示，报告会显式标注）')
    abl.add_argument('--report', type=Path, default=None,
                     help='报告文件（.md/.txt/.docx），提供后把引用精确率纳入 RAG/Critic 消融')
    abl.add_argument('--output', type=Path)
    abl.add_argument('--company', default='')
    abl.add_argument('--period', default='')
    abl.add_argument('--scope', choices=['auto', 'consolidated', 'parent', 'unknown'], default='auto')
    # announce 是"数据结构化提取"的落地：一条命令把真实公告抽成结构化记录并跑勾稽。
    ann = sub.add_parser('announce', help='公告整条记录的结构化抽取（类型识别+事件切分+字段映射+勾稽）')
    ann.add_argument('--input', nargs='+', required=True, help='一个或多个本地公告文件（PDF/图片/文本）')
    ann.add_argument('--output', type=Path)
    # errorcheck 是"研报纠错核查"的落地：报告 vs 原始财报逐条对账 + 报告内部自洽复算。
    err = sub.add_parser('errorcheck', help='研报纠错核查（来源对账 + 内部自洽复算）')
    err.add_argument('--report', type=Path, required=True, help='待核查研报（.md/.txt/.docx/.pdf）')
    err.add_argument('--source', nargs='+', required=True,
                     help='原始财报（季报/半年报/年报），报告引用了哪几期就传哪几份')
    err.add_argument('--output', type=Path)
    # quality 是"研报质量评估"的落地：可确定性检测的子指标 → 维度分 → 总分 + 复核队列。
    qua = sub.add_parser('quality', help='研报质量评估 v2（子指标 0–5 分 + 覆盖率 + 复核队列）')
    qua.add_argument('--report', nargs='+', required=True, help='一份或多份研报（多份会给出排序）')
    qua.add_argument('--output', type=Path)
    qua.add_argument('--html', action='store_true',
                     help='导出可交互 HTML 报告（单文件、离线可用）；与 --output 同用时写进该目录')
    qua.add_argument('--html-dir', type=Path, help='HTML 报告输出目录（默认：--output 目录，或报告同目录）')
    # pro-model 是"自动估值建模"增强内核的落地：驱动来源标注 + 活公式 Excel。
    pro = sub.add_parser('pro-model', help='A股口径自动估值建模（增强内核，导出活公式 Excel）')
    pro.add_argument('--input', nargs='+', help='一个或多个本地源文件（财报）')
    pro.add_argument('--run', default='', help='已有的 analysis run_id（优先于 --input）')
    pro.add_argument('--output', type=Path)
    pro.add_argument('--company', default='')
    pro.add_argument('--period', default='')
    pro.add_argument('--scope', choices=['auto', 'consolidated', 'parent', 'unknown'], default='auto')
    pro.add_argument('--industry', default='')
    pro.add_argument('--valuation-date', default='')
    pro.add_argument('--price', type=float)
    pro.add_argument('--market-cap', type=float)
    pro.add_argument('--options', default='', help='额外 options JSON，如 {"wacc":0.09,"base_year":2025}')
    args = parser.parse_args(argv)
    if args.command == 'export':
        run = json.loads(args.run.read_text(encoding='utf-8'))
        path = svc.export_run(run, args.format, args.output)
        print(json.dumps({'ok': True, 'path': str(path.resolve())}, ensure_ascii=False))
        return 0

    if args.command == 'repro':
        from . import repro as repro_mod
        old = json.loads(args.run.read_text(encoding='utf-8'))
        if not old.get('repro'):
            print(json.dumps({
                'ok': False, 'command': 'repro',
                'error': '这份 run.json 里没有 repro 凭证（可能是早于本功能生成的），'
                         '请先用当前版本重跑一次分析。'}, ensure_ascii=False))
            return 1
        tid = 'cli-' + uuid.uuid4().hex
        folder = config.UPLOAD_DIR / tid
        folder.mkdir(parents=True)
        names = []
        for name in args.input:
            source = Path(name).expanduser().resolve()
            if not source.is_file():
                parser.error(f'文件不存在：{source}')
            dest = folder / source.name
            shutil.copy2(source, dest)
            names.append(dest.name)
        # 用与首次运行**相同**的参数重跑，否则比的不是同一件事。
        saved = old.get('parameters') or {}
        parameters = {'company': saved.get('company') or '',
                      'period': saved.get('period') or '',
                      'scope': saved.get('scope') or 'auto'}
        analysis = svc.analyze_financial_report(tid, {'filenames': names, **parameters})
        rerun = svc.load_run(tid, analysis['run_id'])
        comparison = repro_mod.compare(old.get('repro') or {}, rerun.get('repro') or {})
        result = {
            'ok': comparison['reproducible'],
            'command': 'repro',
            'original_run_id': old.get('run_id'),
            'rerun_run_id': rerun['run_id'],
            'comparison': comparison,
            'original_repro': old.get('repro'),
            'rerun_repro': rerun.get('repro'),
            'conclusion': ('两次运行输出哈希完全一致，同一输入可复现。'
                           if comparison['reproducible'] else
                           '两次运行输出哈希不一致；若代码或 Prompt 已改动，'
                           '需说明差异来源，否则属于不确定性缺陷。'),
        }
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            svc._write_json(args.output / 'repro-首次.json', old.get('repro'))
            svc._write_json(args.output / 'repro-重跑.json', rerun.get('repro'))
            svc._write_json(args.output / 'repro-对比.json', comparison)
            result['output_directory'] = str(args.output.resolve())
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0 if comparison['reproducible'] else 1

    if args.command == 'audit':
        tid = 'cli-' + uuid.uuid4().hex
        payload = {'report_path': str(args.report)}
        for key, value in (('run_id', args.run), ('forensic_run_id', args.forensic_run),
                           ('valuation_run_id', args.valuation_run)):
            if value:
                payload[key] = value
        produced = []
        result = {'ok': True, 'command': 'audit'}
        audit = svc.audit_report(tid, payload)
        produced.append((audit['run_id'], audit))

        # 报告里没写运行 ID、用户也没指定，但给了源文件：现跑一遍作为数据源。
        # 注意这是**兜底**——优先用报告自己声明的运行，否则拿新数据核对旧报告，
        # 会把"数据版本不同"误报成"计数抄错"。
        if not audit.get('runs_located') and not args.run and args.input:
            folder = config.UPLOAD_DIR / tid
            folder.mkdir(parents=True, exist_ok=True)
            names = []
            for name in args.input:
                source = Path(name).expanduser().resolve()
                if not source.is_file():
                    parser.error(f'文件不存在：{source}')
                dest = folder / source.name
                if dest.exists():
                    dest = folder / (uuid.uuid4().hex[:6] + '-' + source.name)
                shutil.copy2(source, dest)
                names.append(dest.name)
            analysis = svc.analyze_financial_report(tid, {
                'filenames': names, 'company': args.company, 'period': args.period,
                'scope': args.scope})
            run_id = analysis['run_id']
            forensic = svc.forensic_scan(tid, {'run_id': run_id})
            produced.append((forensic['run_id'], forensic))
            valuation_run_id = ''
            if args.with_valuation:
                overrides = {}
                if args.overrides:
                    try:
                        overrides = json.loads(args.overrides)
                    except json.JSONDecodeError as exc:
                        parser.error(f'--overrides 不是合法 JSON：{exc}')
                valuation = svc.scenario_valuation(tid, {
                    'run_id': run_id, 'forensic_run_id': forensic['run_id'],
                    'overrides': overrides, 'use_findings': True})
                valuation_run_id = valuation['run_id']
                produced.append((valuation_run_id, valuation))
            payload.update({'run_id': run_id, 'forensic_run_id': forensic['run_id'],
                            'valuation_run_id': valuation_run_id})
            audit = svc.audit_report(tid, payload)
            produced.append((audit['run_id'], audit))
            result['fallback_analysis_run'] = run_id

        result['audit'] = audit
        result['ok'] = bool(audit.get('ok'))
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            for run_id, _ in produced:
                run = svc.load_run(tid, run_id)
                target = args.output / run.get('kind', 'run') / run_id
                svc._write_json(target / 'run.json', run)
                for fmt in ('xlsx', 'json', 'md'):
                    svc.export_run(run, fmt, target)
            result['output_directory'] = str(args.output.resolve())
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    if args.command == 'ablate':
        from . import ablations, report_audit
        tid = 'cli-' + uuid.uuid4().hex
        folder = config.UPLOAD_DIR / tid
        folder.mkdir(parents=True)
        names = []
        for name in args.input:
            source = Path(name).expanduser().resolve()
            if not source.is_file():
                parser.error(f'文件不存在：{source}')
            dest = folder / source.name
            if dest.exists():
                dest = folder / (uuid.uuid4().hex[:6] + '-' + source.name)
            shutil.copy2(source, dest)
            names.append(dest.name)

        analysis = svc.analyze_financial_report(tid, {
            'filenames': names, 'company': args.company, 'period': args.period,
            'scope': args.scope})
        run = svc.load_run(tid, analysis['run_id'])
        forensic = svc.forensic_scan(tid, {'run_id': analysis['run_id']})
        forensic_run = svc.load_run(tid, forensic['run_id'])

        peer_runs = []
        for path in args.peer_run:
            candidate = Path(path).expanduser().resolve()
            if not candidate.is_file():
                parser.error(f'同行运行文件不存在：{candidate}')
            peer_runs.append(json.loads(candidate.read_text(encoding='utf-8')))

        report_text = None
        if args.report:
            candidate = Path(args.report).expanduser().resolve()
            if not candidate.is_file():
                parser.error(f'报告文件不存在：{candidate}')
            report_text = report_audit.read_report_text(str(candidate))

        result = ablations.run_all(run, forensic_run=forensic_run, peer_runs=peer_runs,
                                   synthetic_peers=args.synthetic_peers,
                                   report_text=report_text)
        markdown = ablations.render_markdown(result)
        result['analysis_run_id'] = analysis['run_id']
        result['forensic_run_id'] = forensic['run_id']
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            svc._write_json(args.output / '消融实验.json', result)
            (args.output / '消融实验报告.md').write_text(markdown, encoding='utf-8')
            result['output_directory'] = str(args.output.resolve())
        print(json.dumps({'ok': True, 'command': 'ablate',
                          'analysis_run_id': analysis['run_id'],
                          'forensic_run_id': forensic['run_id'],
                          'summary': result['summary'],
                          'output_directory': result.get('output_directory')},
                         ensure_ascii=False, default=str))
        return 0
    if args.command == 'model':
        run_id = args.run
        if not run_id:
            if not args.input:
                parser.error('--model 需要 --run 或 --input 至少其一')
            tid = 'cli-' + uuid.uuid4().hex
            folder = config.UPLOAD_DIR / tid
            folder.mkdir(parents=True)
            names = []
            for name in args.input:
                source = Path(name).expanduser().resolve()
                if not source.is_file():
                    parser.error(f'文件不存在：{source}')
                dest = folder / source.name
                if dest.exists():
                    dest = folder / (uuid.uuid4().hex[:6] + '-' + source.name)
                shutil.copy2(source, dest)
                names.append(dest.name)
            parameters = {'company': args.company, 'period': args.period, 'scope': args.scope}
            analysis = svc.analyze_financial_report(tid, {'filenames': names, **parameters})
            run_id = analysis['run_id']
        else:
            tid = run_id.split('-')[0] if '-' in run_id else 'cli-legacy'
        overrides = {}
        if args.overrides:
            try:
                overrides = json.loads(args.overrides)
            except json.JSONDecodeError as exc:
                parser.error(f'--overrides 不是合法 JSON：{exc}')
            if not isinstance(overrides, dict):
                parser.error('--overrides 必须是 JSON 对象')
        result = svc.build_valuation_model(tid, {'run_id': run_id, 'overrides': overrides})
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            target = args.output / 'model' / result['run_id']
            svc._write_json(target / 'run.json', result)
            for fmt in ('xlsx', 'json', 'md'):
                svc.export_run(result, fmt, target)
            result['output_directory'] = str(args.output.resolve())
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0

    # ---------------------------------------------------------------- 新增子命令（增强补丁）
    if args.command == 'announce':
        tid = 'cli-' + uuid.uuid4().hex
        folder = config.UPLOAD_DIR / tid
        folder.mkdir(parents=True)
        results, produced = [], []
        for name in args.input:
            source = Path(name).expanduser().resolve()
            if not source.is_file():
                parser.error(f'文件不存在：{source}')
            dest = folder / source.name
            shutil.copy2(source, dest)
            item = svc.extract_announcement_records(tid, {'filename': dest.name})
            results.append(item)
            produced.append((item['run_id'], item))
        result = {'ok': True, 'command': 'announce', 'documents': results,
                  'n_documents': len(results),
                  'n_records': sum(r.get('n_records') or 0 for r in results),
                  'n_checks_flagged': sum(r.get('n_checks_flagged') or 0 for r in results)}
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            for run_id, _ in produced:
                run = svc.load_run(tid, run_id)
                target = args.output / run.get('kind', 'run') / run_id
                svc._write_json(target / 'run.json', run)
                (target / 'records.json').write_text(
                    json.dumps(run.get('records', []), ensure_ascii=False, indent=1, default=str),
                    encoding='utf-8')
            result['output_directory'] = str(args.output.resolve())
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0

    if args.command == 'errorcheck':
        from . import report_audit
        tid = 'cli-' + uuid.uuid4().hex
        folder = config.UPLOAD_DIR / tid
        folder.mkdir(parents=True)
        report = Path(args.report).expanduser().resolve()
        if not report.is_file():
            parser.error(f'报告文件不存在：{report}')
        dest = folder / report.name
        shutil.copy2(report, dest)
        source_names = []
        for name in args.source:
            candidate = Path(name).expanduser().resolve()
            if not candidate.is_file():
                parser.error(f'原始财报不存在：{candidate}')
            target = folder / candidate.name
            if target.exists():
                target = folder / (uuid.uuid4().hex[:6] + '-' + candidate.name)
            shutil.copy2(candidate, target)
            source_names.append(target.name)
        payload = {'filename': dest.name, 'source_filenames': source_names}
        if dest.suffix.lower() in ('.md', '.txt'):
            # 文本类报告直接传正文，避免 CLI 与工具走两条不同的解析路径。
            payload.pop('filename')
            payload['text'] = report_audit.read_report_text(str(dest))
        result = svc.check_report(tid, payload)
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            run = svc.load_run(tid, result['run_id'])
            svc._write_json(args.output / 'findings.json', {
                'source_file': result.get('source_file'), 'summary': run.get('summary'),
                'findings': run.get('findings', []), 'suspects': run.get('suspects', [])})
            result['output_directory'] = str(args.output.resolve())
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0

    if args.command == 'quality':
        from . import report_audit
        from .report_quality_v2 import assess_quality
        items = []
        for name in args.report:
            path = Path(name).expanduser().resolve()
            if not path.is_file():
                parser.error(f'报告文件不存在：{path}')
            text = report_audit.read_report_text(str(path))
            assessment = assess_quality(text)
            items.append({'file': str(path), 'total': assessment['total'],
                          'coverage': assessment['coverage'],
                          'dimensions': {k: v['score'] for k, v in assessment['dimensions'].items()},
                          'n_review': assessment['n_review'], 'detail': assessment})
        ranked = sorted([i for i in items if i['total'] is not None],
                        key=lambda i: i['total'], reverse=True)
        result = {'ok': True, 'command': 'quality', 'reports': items,
                  'ranking': [{'file': i['file'], 'total': i['total'], 'coverage': i['coverage']}
                              for i in ranked],
                  'note': ('总分是项目内部量表，不是竞赛官方评分；排序只在覆盖率相近时可比，'
                           '请同时看 coverage 与 review_queue。')}
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            svc._write_json(args.output / '质量评估.json', result)
            result['output_directory'] = str(args.output.resolve())
        # 可交互 HTML：单文件、无外部依赖，评审环境断网也能打开。
        if args.html or args.html_dir:
            from .quality_report_html import write_quality_html
            target_dir = args.html_dir or args.output or None
            written = []
            for item in items:
                stem = Path(item['file']).stem
                folder = Path(target_dir) if target_dir else Path(item['file']).parent
                folder.mkdir(parents=True, exist_ok=True)
                out = folder / f'{stem}.quality.html'
                write_quality_html(out, item['detail'], report_name=stem)
                written.append(str(out.resolve()))
            result['html_reports'] = written
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0

    if args.command == 'pro-model':
        run_id = args.run
        if not run_id:
            if not args.input:
                parser.error('pro-model 需要 --run 或 --input 至少其一')
            tid = 'cli-' + uuid.uuid4().hex
            folder = config.UPLOAD_DIR / tid
            folder.mkdir(parents=True)
            names = []
            for name in args.input:
                source = Path(name).expanduser().resolve()
                if not source.is_file():
                    parser.error(f'文件不存在：{source}')
                dest = folder / source.name
                if dest.exists():
                    dest = folder / (uuid.uuid4().hex[:6] + '-' + source.name)
                shutil.copy2(source, dest)
                names.append(dest.name)
            analysis = svc.analyze_financial_report(tid, {
                'filenames': names, 'company': args.company, 'period': args.period,
                'scope': args.scope})
            run_id = analysis['run_id']
        else:
            tid = run_id.split('-')[0] if '-' in run_id else 'cli-legacy'
        options = {}
        if args.options:
            try:
                options = json.loads(args.options)
            except json.JSONDecodeError as exc:
                parser.error(f'--options 不是合法 JSON：{exc}')
            if not isinstance(options, dict):
                parser.error('--options 必须是 JSON 对象')
        for key, value in (('industry', args.industry), ('valuation_date', args.valuation_date),
                           ('price', args.price), ('market_cap', args.market_cap)):
            if value not in (None, ''):
                options[key] = value
        result = svc.build_pro_valuation_model(tid, {'run_id': run_id, 'options': options})
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            target = args.output / 'valuation_pro' / result['run_id']
            target.mkdir(parents=True, exist_ok=True)
            for export in result.get('exports') or []:
                src = Path(export['path'])
                if src.is_file():
                    shutil.copy2(src, target / src.name)
            result['output_directory'] = str(args.output.resolve())
        print(json.dumps({k: v for k, v in result.items() if k not in ('projection',)},
                         ensure_ascii=False, default=str))
        return 0 if result.get('ok') else 1

    tid = 'cli-' + uuid.uuid4().hex
    folder = config.UPLOAD_DIR / tid
    folder.mkdir(parents=True)
    names = []
    for name in args.input:
        source = Path(name).expanduser().resolve()
        if not source.is_file():
            parser.error(f'文件不存在：{source}')
        dest = folder / source.name
        if dest.exists():
            dest = folder / (uuid.uuid4().hex[:6] + '-' + source.name)
        shutil.copy2(source, dest)
        names.append(dest.name)
    parameters = {'company': args.company, 'period': args.period, 'scope': args.scope}

    if args.command in ('forensic', 'valuation'):
        try:
            peer = json.loads(args.peer) if args.peer else {}
        except json.JSONDecodeError as exc:
            parser.error(f'--peer 不是合法 JSON：{exc}')
        if not isinstance(peer, dict):
            parser.error('--peer 必须是 JSON 对象')
        overrides = {}
        if args.command == 'valuation' and args.overrides:
            try:
                overrides = json.loads(args.overrides)
            except json.JSONDecodeError as exc:
                parser.error(f'--overrides 不是合法 JSON：{exc}')
            if not isinstance(overrides, dict):
                parser.error('--overrides 必须是 JSON 对象')

        analysis = svc.analyze_financial_report(tid, {'filenames': names, **parameters})
        # analyze_financial_report 把一次调用里的所有文件合并成**一个** analysis 运行，
        # 返回的是该运行的紧凑摘要（顶层 run_id），没有 runs 数组。
        analysis_runs = ([{'run_id': analysis['run_id']}] if analysis.get('run_id') else [])
        produced = []
        result = {'ok': True, 'command': args.command,
                  'analysis_runs': [item['run_id'] for item in analysis_runs]}
        for item in analysis_runs:
            run_id = item['run_id']
            forensic_run_id = ''
            if not args.no_forensic:
                forensic = svc.forensic_scan(tid, {'run_id': run_id, 'peer': peer})
                forensic_run_id = forensic['run_id']
                produced.append((forensic_run_id, forensic))
                if args.command == 'forensic':
                    result.setdefault('forensics', []).append(forensic)
            if args.command == 'valuation':
                payload = {'run_id': run_id, 'overrides': overrides,
                           'use_findings': bool(forensic_run_id)}
                if forensic_run_id:
                    payload['forensic_run_id'] = forensic_run_id
                valuation = svc.scenario_valuation(tid, payload)
                produced.append((valuation['run_id'], valuation))
                result.setdefault('valuations', []).append(valuation)
        if args.command == 'forensic':
            result['ok'] = all(f.get('ok', True) for f in result.get('forensics', []))
        else:
            result['ok'] = bool(result.get('valuations')) and all(
                v.get('ok') for v in result['valuations'])
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            for run_id, _ in produced:
                run = svc.load_run(tid, run_id)
                target = args.output / run.get('kind', 'run') / run_id
                svc._write_json(target / 'run.json', run)
                for fmt in ('xlsx', 'json', 'md'):
                    svc.export_run(run, fmt, target)
            result['output_directory'] = str(args.output.resolve())
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0

    if args.command == 'inspect':
        result = {'ok': True, 'documents': [svc.inspect_document(tid, {
            'filename': name, 'page': args.page, 'query': args.query}) for name in names]}
    elif args.command == 'extract':
        result = {'ok': True, 'runs': [svc.extract_financial_data(tid, {'filename': name, **parameters}) for name in names]}
    else:
        result = svc.analyze_financial_report(tid, {'filenames': names, **parameters})
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        if args.command == 'inspect':
            svc._write_json(args.output / '文档检查.json', result)
        else:
            runs = result.get('runs', [result])
            for number, item in enumerate(runs):
                run = svc.load_run(tid, item['run_id'])
                output = args.output if len(runs) == 1 else args.output / str(number + 1)
                svc._write_json(output / 'run.json', run)
                for fmt in ('xlsx', 'json', 'md'):
                    svc.export_run(run, fmt, output)
            result['output_directory'] = str(args.output.resolve())
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
