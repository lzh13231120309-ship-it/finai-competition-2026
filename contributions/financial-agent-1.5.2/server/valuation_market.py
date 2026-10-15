"""Optional observed price snapshots and reproducible local CSV beta calibration."""
from __future__ import annotations
import csv
import math
import re
import time
from datetime import date, timedelta
from pathlib import Path
from statistics import mean


def _iso_date(value, name='date'):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f'{name} requires YYYY-MM-DD')
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError(f'{name} requires YYYY-MM-DD')
    return result


def estimate_beta(stock: dict, benchmark: dict, min_observations=60):
    if not isinstance(min_observations, int) or isinstance(min_observations, bool) or min_observations < 3:
        raise ValueError('min_observations must be an integer >=3')
    common = sorted(set(stock) & set(benchmark))
    sr, mr, intervals = [], [], []
    for previous, current in zip(common, common[1:]):
        a, b, c, d = stock[previous], stock[current], benchmark[previous], benchmark[current]
        if min(a, b, c, d) <= 0 or not all(math.isfinite(v) for v in (a, b, c, d)):
            continue
        sr.append(math.log(b / a))
        mr.append(math.log(d / c))
        intervals.append((previous, current, (_iso_date(current) - _iso_date(previous)).days))
    n = len(sr)
    if n < min_observations:
        return {'ok': False, 'observations': n, 'reason': f'对齐收益率少于{min_observations}个，不能可靠估计beta'}
    x, y = mean(mr), mean(sr)
    variance = sum((v - x) ** 2 for v in mr)
    if variance <= 1e-16:
        return {'ok': False, 'observations': n, 'reason': '基准收益率方差为零或过小'}
    beta = sum((a - x) * (b - y) for a, b in zip(mr, sr)) / variance
    intercept = y - beta * x
    residual = sum((b - intercept - beta * a) ** 2 for a, b in zip(mr, sr))
    total = sum((b - y) ** 2 for b in sr)
    se = math.sqrt(max(0, residual / (n - 2) / variance))
    return {'ok': True, 'beta': beta, 'alpha_per_observation': intercept,
            'r_squared': 1 - residual / total if total > 0 else None,
            'standard_error': se, 'approximate_95pct_interval': [beta - 1.96 * se, beta + 1.96 * se],
            'observations': n, 'start_date': intervals[0][0], 'end_date': intervals[-1][1],
            'intervals_over_one_calendar_day': sum(days > 1 for _, _, days in intervals),
            'max_interval_calendar_days': max(days for _, _, days in intervals),
            'method': 'OLS log-return covariance / benchmark variance, adjusted closes on common date intervals',
            'note': '双方收益使用相同起止日期；周末、假日或缺失日期可形成多日区间，alpha按观测区间计。历史beta不保证未来风险；95%区间为未修正异方差和自相关的OLS渐近近似。'}


def read_price_csv(path: Path, valuation_date=None):
    if path.stat().st_size > 20_000_000:
        raise ValueError('行情CSV超过20MB')
    today = _iso_date(valuation_date, 'valuation_date') if valuation_date else date.today()
    if today > date.today():
        raise ValueError('valuation_date cannot be in the future')
    points, seen, bad = {}, set(), 0
    with path.open(encoding='utf-8-sig', newline='') as file:
        for row in csv.DictReader(file):
            try:
                day = str(row.get('date') or row.get('Date') or row.get('日期') or '').strip()
                if _iso_date(day) > today:
                    raise ValueError('未来行情日期')
                adjusted = float(row.get('adj_close') or row.get('Adj Close') or row.get('复权收盘') or row.get('close') or row.get('Close') or row.get('收盘'))
                raw = float(row.get('close') or row.get('Close') or row.get('收盘') or adjusted)
                if not math.isfinite(adjusted) or not math.isfinite(raw) or min(adjusted, raw) <= 0:
                    raise ValueError('无效价格')
                if day in seen:
                    # Neither first nor last conflicting row is privileged.
                    bad += 1 + int(day in points)
                    points.pop(day, None)
                    continue
                seen.add(day)
                points[day] = (adjusted, raw)
            except (ValueError, TypeError):
                bad += 1
    if not points:
        raise ValueError('没有有效日期与收盘价格（未来日期/重复日期已排除），请使用date,close,adj_close列')
    days = sorted(points)
    return {'points': {day: points[day][0] for day in days},
            'raw_points': {day: points[day][1] for day in days}, 'price': points[days[-1]][1],
            'as_of': days[-1], 'source': str(path), 'invalid_rows': bad, 'currency': None}


def _fetch_yahoo(symbol, period, as_of=None):
    import yfinance as yf
    ticker = yf.Ticker(symbol)
    kwargs = {'auto_adjust': False, 'timeout': 10}
    if as_of:
        end = _iso_date(as_of, 'as_of')
        years = {'1y': 1, '2y': 2, '5y': 5}[period]
        kwargs.update(start=(end - timedelta(days=366 * years)).isoformat(), end=(end + timedelta(days=1)).isoformat())
    else:
        kwargs['period'] = period
    history = ticker.history(**kwargs)
    if history is None or history.empty:
        raise ValueError(f'{symbol}未返回有效行情，可能代码错误或行情服务不可用')
    metadata, points = ticker.history_metadata or {}, {}
    for timestamp, row in history.iterrows():
        raw, adjusted = float(row['Close']), float(row.get('Adj Close', row['Close']))
        if math.isfinite(adjusted) and math.isfinite(raw) and min(raw, adjusted) > 0 and timestamp.date() <= date.today():
            points[timestamp.date().isoformat()] = (adjusted, raw)
    if not points:
        raise ValueError('行情包含无效价格')
    days = sorted(points)
    return {'points': {day: points[day][0] for day in days},
            'raw_points': {day: points[day][1] for day in days}, 'price': points[days[-1]][1],
            'currency': metadata.get('currency'), 'as_of': days[-1],
            'source': f'https://finance.yahoo.com/quote/{symbol}/history/', 'invalid_rows': 0}


def _crop(snapshot, as_of):
    if not as_of:
        return snapshot
    snapshot = dict(snapshot)
    snapshot['points'] = {day: value for day, value in snapshot['points'].items() if day <= as_of}
    if not snapshot['points']:
        raise ValueError('指定日期以前没有行情')
    effective = max(snapshot['points'])
    snapshot['price'] = snapshot.get('raw_points', {}).get(effective, snapshot.get('price') if effective == snapshot['as_of'] else None)
    snapshot['as_of'] = effective
    return snapshot


def get_market_data(args: dict, resolve_file=None):
    symbol, benchmark = str(args.get('symbol') or '').strip(), str(args.get('benchmark') or '').strip()
    period = str(args.get('period') or '2y')
    if period not in ('1y', '2y', '5y'):
        raise ValueError('period支持1y、2y、5y')
    if not benchmark:
        benchmark = '^HSI' if symbol.endswith('.HK') else '000300.SS' if symbol.endswith(('.SS', '.SZ')) else '^GSPC'
    for value in (symbol, benchmark):
        if value and not re.fullmatch(r'[A-Za-z0-9.^=\-]{1,32}', value):
            raise ValueError('股票或基准代码格式无效')
    as_of = args.get('as_of')
    if as_of and _iso_date(as_of, 'as_of') > date.today():
        raise ValueError('不能请求未来行情')
    warnings = []
    try:
        if args.get('price_file'):
            if not resolve_file:
                raise ValueError('当前环境不支持附件行情')
            stock = read_price_csv(resolve_file(args['price_file']))
            stock['currency'] = args.get('currency')
        else:
            if not symbol:
                raise ValueError('请提供股票代码或本地行情附件')
            stock = _fetch_yahoo(symbol, period, as_of)
        stock = _crop(stock, as_of)
    except Exception as error:
        return {'ok': False, 'symbol': symbol, 'benchmark': benchmark, 'error': f'{type(error).__name__}: {error}',
                'warnings': warnings, 'fallback': '可上传股票与基准的date,close,adj_close CSV，指定price_file和benchmark_file离线估计beta；缺失数据不填0。'}
    bench = None
    try:
        if args.get('benchmark_file'):
            if not resolve_file:
                raise ValueError('当前环境不支持附件行情')
            bench = read_price_csv(resolve_file(args['benchmark_file']))
            bench['currency'] = args.get('benchmark_currency') or args.get('currency')
        else:
            bench = _fetch_yahoo(benchmark, period, as_of)
        bench = _crop(bench, as_of)
        if not stock.get('currency') or not bench.get('currency'):
            beta = {'ok': False, 'reason': '股票或基准币种未确认，无法证明收益率币种一致'}
        elif stock['currency'] != bench['currency']:
            beta = {'ok': False, 'reason': '股票与基准币种不同，未做汇率一致性调整'}
        else:
            beta = estimate_beta(stock['points'], bench['points'])
    except Exception as error:
        beta = {'ok': False, 'reason': f'基准行情不可用：{type(error).__name__}: {error}'}
        warnings.append('基准行情不可用，保留股票价格快照；beta未校准，可补充基准CSV。')
    currency = stock.get('currency')
    if not currency:
        warnings.append('行情币种未确认，不能直接并入其他币种的财报估值。')
    age = (date.today() - _iso_date(stock['as_of'])).days
    if age > 7 and not as_of:
        warnings.append(f'行情已距当前日期{age}天，不能称为当前价格。')
    warnings.append('无风险利率、股权风险溢价、债务成本和市场资本权重仍需有来源的输入；行情beta不会自动证明WACC已校准。')
    beta_source = stock['source'] + (' ; ' + bench['source'] if bench else '')
    return {'ok': True, 'symbol': symbol, 'benchmark': benchmark, 'period': period,
            'price': stock['price'], 'currency': currency, 'as_of': stock['as_of'],
            'retrieved_at': time.time(), 'source': stock['source'], 'beta_estimate': beta,
            'price_observations': len(stock['points']), 'warnings': warnings,
            'invalid_rows': stock['invalid_rows'] + (bench['invalid_rows'] if bench else 0),
            'market_fields': {'beta': {'value': beta.get('beta'), 'source': beta_source,
                                     'as_of': beta.get('end_date'), 'currency': currency,
                                     'status': 'historical_estimate' if beta.get('ok') else 'unavailable', 'method': beta.get('method')},
                              'price': {'value': stock['price'], 'source': stock['source'],
                                        'as_of': stock['as_of'], 'currency': currency, 'status': 'observed_snapshot'}},
            'note': 'Yahoo公开行情通过yfinance获取，适用于研究/个人使用，受数据源条款约束；可用本地CSV替代。未获取或猜测公司股数。'}
