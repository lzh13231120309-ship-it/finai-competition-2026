"""Offline-first valuation CLI: python -m server.valuation_cli --help."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

FORMATS = ('xlsx', 'docx', 'json', 'md')


def _formats(value: str) -> list[str]:
    formats = list(dict.fromkeys(part.strip().lower() for part in value.split(',') if part.strip()))
    if not formats or any(part not in FORMATS for part in formats):
        raise argparse.ArgumentTypeError('formats supports xlsx,docx,json,md')
    return formats


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f'{type(value).__name__} is not JSON serializable')


def _read_json(value: str, label: str):
    path = Path(value).expanduser().resolve()
    def invalid_constant(constant):
        raise ValueError(f'invalid JSON numeric constant: {constant}')
    try:
        if path.stat().st_size > 100_000_000:
            raise ValueError('JSON input exceeds 100MB resource limit')
        with path.open(encoding='utf-8-sig') as stream:
            return json.load(stream, parse_float=Decimal, parse_constant=invalid_constant)
    except (OSError, ValueError) as error:
        raise ValueError(f'{label} {path}: {error}') from error


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False, default=_json_default)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _emit(value, stream=sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, default=_json_default), file=stream)


def _build(args) -> int:
    from .valuation_advanced import build_advanced_model

    analysis = _read_json(args.run, 'analysis run')
    options = _read_json(args.options, 'options') if args.options else {}
    if not isinstance(analysis, dict) or not isinstance(options, dict):
        raise ValueError('analysis run and options must be JSON objects')
    findings = []; forensic_run_id = None
    if args.findings:
        forensic = _read_json(args.findings, 'findings')
        if isinstance(forensic, dict):
            findings = forensic.get('findings', [])
            forensic_run_id = forensic.get('run_id')
            if forensic.get('parent_run_id') and forensic['parent_run_id'] != analysis.get('run_id'):
                raise ValueError('forensic parent_run_id does not match analysis run_id')
        else:
            findings = forensic
        if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
            raise ValueError('findings must be a list of objects or an object containing findings')
    folder = Path(args.output).expanduser().resolve()
    if (folder/'run.json').exists():
        raise ValueError(f'output already contains run.json; choose a new directory: {folder}')
    if options.get('market'):
        from .valuation_service import normalize_market
        options['market'] = normalize_market(options['market'])
    run = build_advanced_model(analysis, findings=findings, options=options)
    company = analysis.get('company')
    if not company:
        companies = {item.get('company') for item in analysis.get('facts', []) if item.get('company')}
        company = next(iter(companies)) if len(companies) == 1 else None
    run.update(run_id='valuation_' + uuid4().hex, kind='valuation_model', company=company)
    if analysis.get('run_id'):
        run['parent_run_id'] = analysis['run_id']
    if forensic_run_id:
        run['forensic_run_id'] = forensic_run_id
    run['options'] = json.loads(json.dumps(options,default=_json_default,allow_nan=False))
    run['input_files'] = {'analysis': str(Path(args.run).expanduser().resolve())}
    if args.options:
        run['input_files']['options'] = str(Path(args.options).expanduser().resolve())
    if args.findings:
        run['input_files']['findings'] = str(Path(args.findings).expanduser().resolve())
    # The full run remains available even when the model is blocked or a report fails.
    run_path = folder / 'run.json'
    _write_json(run_path, run)
    exports = {}; errors = {}
    for fmt in args.formats:
        try:
            from .valuation_reports import export_model
            path = Path(export_model(run, fmt, folder)).resolve()
            if not path.is_file():
                raise ValueError('report exporter did not produce a file')
            exports[fmt] = str(path)
        except (OSError, ValueError, ImportError, TypeError, RuntimeError) as error:
            errors[fmt] = f'{type(error).__name__}: {error}'
    summary = dict(ok=bool(run.get('ok')) and not errors, run_id=run['run_id'], kind=run['kind'],
                   company=company, output_dir=str(folder), complete_result=str(run_path), exports=exports,
                   model_ok=bool(run.get('ok')), model_plan=run.get('model_plan', {}),
                   warnings=run.get('warnings', []))
    if run.get('parent_run_id'):
        summary['parent_run_id'] = run['parent_run_id']
    if errors:
        summary['export_errors'] = errors
    _emit(summary)
    return 1 if errors else 0 if run.get('ok') else 2


def _market(args) -> int:
    from .valuation_market import get_market_data

    options = {key: value for key, value in vars(args).items()
               if key not in ('command', 'output') and value is not None}
    def resolve_file(value):
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f'local CSV does not exist: {path}')
        return path
    result = get_market_data(options, resolve_file=resolve_file)
    if args.output:
        _write_json(Path(args.output).expanduser().resolve(), result)
    _emit(result)
    return 0 if result.get('ok') else 2


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description='Build auditable valuation models from saved financial analysis; local CSV market data supported.')
    commands = root.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build', help='Build a model from offline financial-analysis JSON')
    build.add_argument('--run', required=True, help='Saved financial analysis JSON')
    build.add_argument('--options', help='Valuation options JSON; all assumptions and market inputs remain explicit')
    build.add_argument('--output', required=True, help='Output directory for full run and reports')
    build.add_argument('--findings', help='Forensic findings list or saved forensic-run JSON')
    build.add_argument('--formats', type=_formats, default=list(FORMATS), help='Comma-separated xlsx,docx,json,md (default: all)')
    market = commands.add_parser('market', help='Price snapshot and historical OLS beta; offline CSV or optional public feed')
    market.add_argument('--symbol', help='Stock symbol (not needed for local CSV)')
    market.add_argument('--benchmark', help='Benchmark symbol')
    market.add_argument('--period', choices=('1y','2y','5y'), default='2y')
    market.add_argument('--as-of', help='Observation-date ceiling YYYY-MM-DD; not retrieval date')
    market.add_argument('--price-file', help='Stock CSV: date,close,adj_close')
    market.add_argument('--benchmark-file', help='Benchmark CSV: date,close,adj_close')
    market.add_argument('--currency', help='Currency of the stock snapshot')
    market.add_argument('--benchmark-currency', help='Currency of the benchmark snapshot')
    market.add_argument('--output', help='Optional destination JSON path')
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return _build(args) if args.command == 'build' else _market(args)
    except (OSError, ValueError, TypeError, ImportError, RuntimeError) as error:
        _emit({'ok': False, 'error': f'{type(error).__name__}: {error}'}, sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
