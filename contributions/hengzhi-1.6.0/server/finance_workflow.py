"""Explicitly scoped five-capability workflows with source fingerprints and checkpoints."""
from __future__ import annotations
import hashlib,json
from pathlib import Path
from . import finance_service as fs

STAGES=('extraction','financial_analysis','valuation','report_check','quality')
LABELS=dict(zip(STAGES,('结构化提取','财报分析','估值建模','研报纠错','质量评估')))


def _files(task_id,names):
    if not isinstance(names,list) or any(not isinstance(n,str) for n in names):
        raise ValueError('文件名必须是列表')
    return list(dict.fromkeys(str(fs.resolve_attachment(task_id,n).name) for n in names))


def _fingerprint(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda:stream.read(1024*1024),b''):h.update(part)
    return h.hexdigest()


def run_workflow(task_id,args,cancel_event=None):
    fs._id(task_id)
    goals=args.get('goals',list(STAGES))
    if not isinstance(goals,list) or not goals or any(g not in STAGES for g in goals):
        raise ValueError('请选择有效的五项能力；不能提交空流程')
    goals=[g for g in STAGES if g in goals]
    def active(key):return key in goals and not (cancel_event and cancel_event.is_set())
    financial=_files(task_id,args.get('financial_filenames') or [])
    announcements=_files(task_id,args.get('announcement_filenames') or [])
    report_name=str(args.get('report_filename') or '')
    report_path=fs.resolve_attachment(task_id,report_name) if report_name else None
    if report_name in financial or report_name in announcements:
        raise ValueError('待核查研报不能同时作为原始财报或公告依据')
    text=str(args.get('report_text') or '')
    if text and report_name:raise ValueError('报告正文与报告附件请选择一种，避免核查对象不明确')
    if len(text)>2_000_000:raise ValueError('报告超过200万字符，请分段处理')
    analysis_id=str(args.get('analysis_run_id') or '')
    analysis=fs.load_run(task_id,analysis_id) if analysis_id else None
    if analysis and analysis.get('task_id')!=task_id:raise ValueError('分析结果不属于当前任务')
    if analysis and analysis.get('kind')!='analysis':raise ValueError('analysis_run_id必须是当前任务财报分析结果')
    if analysis and financial and set(financial)!=set(analysis.get('filenames') or []):
        raise ValueError('指定分析结果与财报附件不一致，请只选一种依据或使用相同附件')
    if analysis:
        financial=_files(task_id,analysis.get('filenames') or [])
        if report_name in financial:raise ValueError('待核查研报不能同时作为复用分析的原始财报依据')
    fingerprints={name:_fingerprint(fs.resolve_attachment(task_id,name)) for name in dict.fromkeys(financial+announcements+([report_name] if report_name else []))}
    stale=[]
    if analysis:
        for doc in analysis.get('documents') or []:
            name=doc.get('filename')
            if name and doc.get('sha256') and fingerprints.get(name)!=doc['sha256']:stale.append(name)
    options=args.get('valuation_options') or {}
    if not isinstance(options,dict):raise ValueError('估值参数须为对象')
    results={};full={};analysis_error=None
    needed=any(g in goals for g in ('extraction','financial_analysis','valuation')) or bool(financial or analysis)
    if stale:analysis_error='原始附件已变化，必须重新分析：'+','.join(stale);analysis=None
    elif needed and analysis is None and financial and not (cancel_event and cancel_event.is_set()):
        try:
            preview=fs.analyze_financial_report(task_id,{'filenames':financial,**{k:args[k] for k in ('company','period','scope') if args.get(k)}})
            analysis_id=preview['run_id'];analysis=fs.load_run(task_id,analysis_id);full['analysis_preview']=preview
        except Exception as error:analysis_error=str(error)
    elif analysis:analysis_id=analysis['run_id']

    def stage(key,status,summary,**data):
        results[key]={'key':key,'name':LABELS[key],'status':status,'summary':summary,**data}

    if active('extraction'):
        records=[];announcement_runs=[];errors=[]
        if analysis:
            records=[{k:f.get(k) for k in ('code','raw_label','value','unit','currency','company','period','period_type','scope','status','evidence_ids','source')} for f in analysis.get('facts') or []]
        for name in announcements:
            if cancel_event and cancel_event.is_set():break
            try:
                parsed=fs.extract_announcement_records(task_id,{'filename':name});announcement_runs.append(parsed['run_id'])
                records.extend({'filename':name,'event_record':r} for r in parsed.get('records') or [])
                if parsed.get('n_checks_flagged') or parsed.get('review') or parsed.get('warnings'):errors.append(name+'有待复核项')
            except Exception as error:errors.append(name+'：'+str(error))
        if records:
            structured=fs.save_run(task_id,{'kind':'workflow_extraction','records':records,'parent_run_id':analysis_id if analysis else None,'announcement_run_ids':announcement_runs,'filenames':financial+announcements})
            fact_rows=[r for r in records if 'code' in r]
            traceable=sum(bool(r.get('evidence_ids')) for r in fact_rows)
            verified=sum(r.get('status')=='verified' for r in fact_rows)
            incomplete=sum(any(r.get(k) in (None,'','unknown','auto') for k in ('code','currency','period','scope')) or r.get('value') is None for r in fact_rows)
            pending=bool(errors or announcement_runs or incomplete or verified<len(fact_rows) or traceable<len(fact_rows))
            if announcement_runs:errors.append('公告记录已切分，仍需结合字段schema做原文支持验证')
            stage('extraction','needs_review' if pending else 'completed',f'输出{len(records)}条记录，财务事实{verified}/{len(fact_rows)}已核验；有证据锚点{traceable}条',run_id=structured['run_id'],record_count=len(records),warnings=errors)
        else:stage('extraction','needs_input','未提取出可用记录；请提供明确的财报或公告附件',warnings=errors+([analysis_error] if analysis_error else []))
    if active('financial_analysis'):
        if not analysis:stage('financial_analysis','needs_input',analysis_error or '请选择原始财报附件或已有分析结果')
        else:
            checks=analysis.get('checks') or [];failed=[c for c in checks if c.get('status') not in ('pass','ok','passed')]
            stage('financial_analysis','needs_review' if failed or analysis.get('review_queue') or not checks or not analysis.get('metrics') or not any(f.get('status')=='verified' for f in analysis.get('facts') or []) else 'completed',f'指标{len(analysis.get("metrics") or [])}项，勾稽未通过或待确认{len(failed)}项',run_id=analysis_id,check_count=len(checks),unresolved_check_count=len(failed))
    if active('valuation'):
        if not analysis:stage('valuation','needs_input',analysis_error or '估值需要同一批原始财报分析结果')
        else:
            try:
                valuation=fs.build_pro_valuation_model(task_id,{'run_id':analysis_id,'options':dict(options)})
                full['valuation']=valuation
                workbook_check=None
                if valuation.get('ok') and not (cancel_event and cancel_event.is_set()):
                    from .workbook_verification import verify_workbook
                    workbook_check=verify_workbook(task_id,valuation)
                stage('valuation','completed' if valuation.get('ok') and not valuation.get('refusals') else 'needs_input','估值模型已保存；默认项和假设仍须复核' if valuation.get('ok') else '缺少可靠输入，未用默认数值强行建模',run_id=valuation.get('run_id'),refusals=valuation.get('refusals') or [],exports=valuation.get('exports') or [],workbook_check=workbook_check)
                if workbook_check and workbook_check['status'] in ('mismatched','needs_review'):
                    results['valuation']['status']='needs_review'
                    results['valuation'].setdefault('warnings',[]).append('Excel独立复算未通过，请查看workbook_check')
            except Exception as error:stage('valuation','error',str(error))
    if any(active(g) for g in ('report_check','quality')) and report_path:
        try:
            from .report_audit import read_report_text
            text=read_report_text(str(report_path))
        except Exception as error:
            for key in ('report_check','quality'):
                if key in goals:stage(key,'error','报告读取失败：'+str(error))
    report_args={'text':text}
    if report_name:report_args['filename']=report_name
    if active('report_check') and 'report_check' not in results:
        if not text.strip():stage('report_check','needs_input','请选择待核查研报附件或粘贴正文')
        else:
            try:
                # General source checker expects PDFs; other formats use the already verified fact pool.
                pdfs=[n for n in financial if Path(n).suffix.lower()=='.pdf'] if not stale else []
                if pdfs:
                    checked=fs.check_report(task_id,{**report_args,'source_filenames':pdfs})
                else:
                    from .errorcheck_service import check_report
                    checked=check_report(text=text,source_paths=[])
                    stored=fs.save_run(task_id,{**checked,'kind':'errorcheck','filenames':[report_name] if report_name else [],'source_filenames':[]})
                    checked['run_id']=stored['run_id']
                audit=fs.audit_report(task_id,{**report_args,'run_id':analysis_id}) if analysis else None
                full['report_check']=checked;full['evidence_audit']=audit
                count=checked.get('n_findings')
                if count is None:count=(checked.get('summary') or {}).get('n_findings',0)
                hard=(audit or {}).get('hard_issue_count',0)
                unresolved=((audit or {}).get('claim_review') or {}).get('stats',{}).get('unverifiable',0)
                stage('report_check','needs_review' if count or hard or unresolved or checked.get('suspects') or not analysis else 'completed',f'可复算问题{count}项，证据核查硬问题{hard}项；'+('使用明确财报依据' if analysis else '仅内部检查，未完成原始事实核验'),run_id=checked['run_id'],evidence_audit_run_id=(audit or {}).get('run_id'),reference_provided=bool(analysis),unverifiable_claim_count=unresolved,finding_count=count)
            except Exception as error:stage('report_check','error',str(error))
    if active('quality') and 'quality' not in results:
        if not text.strip():stage('quality','needs_input','质量评估需要待评研报')
        else:
            try:
                quality=fs.assess_report_quality(task_id,report_args);full['quality']=quality
                # Observable-language scoring is never a certificate of factual accuracy.
                audit=full.get('evidence_audit')
                if audit is None and analysis:
                    audit=fs.audit_report(task_id,{**report_args,'run_id':analysis_id})
                    full['evidence_audit']=audit
                stage('quality','needs_review','已评可观察项；需人工审阅行业适用性、因果与利益冲突',run_id=quality['run_id'],observable_score=quality.get('total'),observable_coverage=quality.get('coverage'),overall_quality_score=None,fact_verification=(audit or {}).get('claim_review',{}).get('stats'),exports=quality.get('exports') or [],score_scope='项目内部可观察项评分，不能替代事实核验或完整人工评审')
            except Exception as error:stage('quality','error',str(error))
    for key in goals:
        if key not in results:stage(key,'cancelled','已停止后续步骤；已产生的运行结果保留')
    changed=[]
    for name,digest in fingerprints.items():
        try:
            if _fingerprint(fs.resolve_attachment(task_id,name))!=digest:changed.append(name)
        except (OSError,ValueError):changed.append(name)
    if changed:
        for s in results.values():s['status']='needs_review';s.setdefault('warnings',[]).append('执行期间附件发生变化，结果不能作为同一版本交付')
    payload={'kind':'finance_workflow','workflow_version':'five-capabilities-v1','filenames':financial+announcements+([report_name] if report_name else []),'requested_goals':goals,'analysis_run_id':analysis_id if analysis else None,'source_fingerprints':fingerprints,'report_sha256':hashlib.sha256(text.encode()).hexdigest() if text else None,'stages':[results[g] for g in goals],'completed':all(results[g]['status']=='completed' for g in goals),'source_changed_during_run':changed,'stale_analysis_files':stale,'limitations':['只复用明确选择的分析，不默认选择最近公司。','每一步运行ID对应其实际输出；质量可观察项分数与事实核验分开。','未提供报告、参数或依据的步骤列待补充，不声称五项全部通过。']}
    saved=fs.save_run(task_id,payload);directory=fs.run_dir(task_id,saved['run_id'])
    fs._write_json(directory/'报告.json',saved)
    def safe(value):return str(value).replace('|','／').replace('\n',' ')
    lines=['# 五项能力联动结果','','| 能力 | 状态 | 结果 |','|---|---|---|']+[f'| {s["name"]} | {s["status"]} | {safe(s["summary"])} |' for s in saved['stages']]
    lines+=['','整体状态：'+('已完成' if payload['completed'] else '部分完成，存在待补充或待复核步骤'),'']+['- '+note for note in payload['limitations']]
    (directory/'报告.md').write_text('\n'.join(lines),encoding='utf-8')
    for s in saved['stages']:
        if s.get('run_id'):s['complete_result']=f'/api/financial/artifacts/{task_id}/{s["run_id"]}/run.json'
    fs._write_json(directory/'run.json',saved)
    return {'ok':True,**saved,'complete_result':f'/api/financial/artifacts/{task_id}/{saved["run_id"]}/run.json','exports':[{'format':fmt,'url':f'/api/financial/artifacts/{task_id}/{saved["run_id"]}/'+name} for fmt,name in [('json','报告.json'),('md','报告.md')]]}
