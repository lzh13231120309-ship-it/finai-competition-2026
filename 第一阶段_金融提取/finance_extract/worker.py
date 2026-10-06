"""Single-document local MinerU process; preserve raw output and provenance."""
from pathlib import Path
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import argparse
import csv
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

def environment():
    import tempfile
    temporary = ROOT / 'local_data' / 'tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ['TEMP'] = os.environ['TMP'] = str(temporary)
    tempfile.tempdir = str(temporary)
    os.environ['MINERU_HOME'] = str(ROOT.parent / 'runtime/mineru')
    os.environ['MINERU_MODEL_BASE_DIR'] = str(ROOT.parent / 'runtime/models')
    os.environ['MINERU_CONFIG'] = str(ROOT / 'mineru-local.yaml')
    os.environ['MINERU_MODEL_SOURCE'] = 'local'
    os.environ['MINERU_MODEL_SMALL_BACKEND'] = 'onnx'
    os.environ['MINERU_MODEL_VLM_ENGINE'] = 'llama-cpp'
    # Portable deployment stores packages and model weights under ASCII runtime paths.
    # Do not inherit any configured remote endpoint from the host.
    for k in ('MINERU_API_URL', 'MINERU_API_KEY', 'MINERU_MODEL_VLM_SERVER_URL', 'MINERU_MODEL_VLM_API_KEY'):
        os.environ.pop(k, None)

def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def flatten(result):
    for f in result['fields']:
        yield f
    for e in result['events']:
        for name, f in e['fields'].items():
            yield {'name': name, 'event_type': e['type'], **f}

def run(input_file, out, tier='flash', pages='', ocr='auto', display_name=None):
    environment()
    from mineru.parser import parse
    from mineru.config import VlmConfig
    from .extract import extract_middle
    source = Path(input_file).resolve()
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    checksum = sha256(source.read_bytes()).hexdigest()
    from .advisor import inspect_file, review_guidance
    requested_tier = tier
    plan = {'mode':'manual' if tier != 'auto' else 'auto','tier':tier,'ocr':ocr,'reason':'按用户明确选择解析，未自动改变处理方式。','local_only':True}
    trace = {'started_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
             'source_name': display_name or source.name, 'source_sha256': checksum,
             'tier': tier, 'requested_tier':requested_tier, 'plan':plan, 'ocr_mode': ocr, 'page_range': pages or 'all', 'local_only': True}
    write_json(out / 'trace.json', trace)
    try:
        from .native import TEXT_SUFFIXES, parse_native
        is_native=source.suffix.lower() in TEXT_SUFFIXES
        if is_native:
            tier,ocr='native','txt'
            plan={'mode':'native','tier':'native','ocr':'txt','local_only':True,'reason':'按原始CSV单元格或文本行直接读取，未运行OCR'}
            trace.update(tier=tier,ocr_mode=ocr,plan=plan)
        elif requested_tier == 'auto':
            plan = inspect_file(source, pages)
            tier, ocr = plan['tier'], plan['ocr']
            trace.update(tier=tier,ocr_mode=ocr,plan=plan)
            write_json(out / 'trace.json', trace)
        # The standalone and embedded entries share one machine and model set.
        from filelock import FileLock
        if is_native:
            if pages:raise ValueError('CSV/文本不支持PDF页码范围，请保留全文件处理')
            result=parse_native(source,display_name or source.name,checksum)
            write_json(out/'native_source.json',{'format':source.suffix,'sha256':checksum,'blocks':result['blocks']})
            (out/'mineru_original.md').write_text('\n'.join(b['text'] for b in result['blocks']),encoding='utf-8')
        else:
            with FileLock(str(ROOT / 'local_data' / 'parsing.lock'), timeout=3500):
                parsed = parse(source, tier=tier, ocr_mode=ocr, page_range=pages, image_analysis=False,
                               vlm_config=VlmConfig(engine='llama-cpp', server_url='', api_key='', max_concurrency=1))
            middle = parsed.to_dict()
            write_json(out / 'mineru_middle.json', middle)
            (out / 'mineru_original.md').write_text(parsed.markdown(), encoding='utf-8')
            result = extract_middle(middle, display_name or source.name, checksum)
        result['run'] = {'tier': tier, 'ocr_mode': ocr, 'page_range': pages or 'all', 'elapsed_seconds': round(time.time() - started, 3)}
        # OCR/VLM outputs are candidates; rule checks are not statistical confidence.
        for f in flatten(result):
            if tier not in ('flash','native') or ocr != 'txt':
                f.setdefault('review_reasons', []).append('verify_recognized_value_against_original')
        result['review_required'] = bool(result['warnings']) or any(f.get('review_reasons') for f in flatten(result))
        result['assistant'] = review_guidance(result, plan)
        write_json(out / 'financial.json', result)
        with (out / 'fields.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            names = ['name', 'label', 'event_type', 'raw', 'value', 'unit', 'base_value', 'base_unit', 'period', 'page', 'table_id', 'row', 'column', 'review_reasons', 'quote']
            writer = csv.DictWriter(stream, fieldnames=names)
            writer.writeheader()
            for f in flatten(result):
                ev = f.get('evidence', {})
                record = {n: f.get(n, ev.get(n, '')) for n in names}
                record['review_reasons'] = '; '.join(f.get('review_reasons', []))
                # Neutralize spreadsheet formulas without altering JSON evidence.
                for k, v in record.items():
                    if isinstance(v, str) and v.startswith(('=', '+', '@')):
                        record[k] = "'" + v
                writer.writerow(record)
        trace.update(status='done', elapsed_seconds=round(time.time() - started, 3),
                     extracted_fields=len(result['fields']), events=len(result['events']), tables=len(result['tables']))
        write_json(out / 'trace.json', trace)
        return result
    except Exception as exc:
        trace.update(status='failed', elapsed_seconds=round(time.time() - started, 3), error=f'{type(exc).__name__}: {exc}')
        write_json(out / 'trace.json', trace)
        raise

def main():
    p = argparse.ArgumentParser()
    p.add_argument('input')
    p.add_argument('--output', required=True)
    p.add_argument('--tier', choices=['auto', 'flash', 'basic', 'standard', 'advanced'], default='flash')
    p.add_argument('--pages', default='')
    p.add_argument('--ocr', choices=['auto', 'txt', 'ocr'], default='auto')
    p.add_argument('--display-name')
    a = p.parse_args()
    result = run(a.input, a.output, a.tier, a.pages, a.ocr, a.display_name)
    print(json.dumps({'fields': len(result['fields']), 'events': len(result['events']), 'tables': len(result['tables'])}))

if __name__ == '__main__':
    main()
