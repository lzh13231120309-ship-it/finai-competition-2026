"""Auditable driver forecasts, sustainable terminal reinvestment and uncertainty tools.

All primary arithmetic uses Decimal. This forecasts operating drivers, not three
balanced financial statements. Distributions are user assumptions, not market odds.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import random
import math
from statistics import NormalDist
from datetime import date

from .valuation import DEFAULT_ASSUMPTIONS, SCENARIOS, calibrate_from_facts, risks_to_parameters

D = Decimal
ZERO, ONE = D(0), D(1)
ENGINE_VERSION = 'advanced-dcf-1.0'
DRIVERS = ('revenue_growth', 'gross_margin', 'opex_ratio', 'tax_rate', 'da_to_revenue',
           'capex_to_revenue', 'receivable_days', 'inventory_days', 'payable_days')
BOUNDS = {'revenue_growth': (D('-.99'), D(2)), 'gross_margin': (ZERO, ONE),
          'opex_ratio': (ZERO, ONE), 'tax_rate': (ZERO, ONE),
          'da_to_revenue': (ZERO, ONE), 'capex_to_revenue': (ZERO, ONE),
          'receivable_days': (ZERO, D(3650)), 'inventory_days': (ZERO, D(3650)),
          'payable_days': (ZERO, D(3650)), 'wacc': (D('.000001'), ONE),
          'terminal_growth': (ZERO, D('.2')), 'terminal_roic': (D('.000001'), ONE),
          'sustainable_profit_discount': (ZERO, ONE), 'near_term_fcff_haircut': (ZERO, ONE),
          'impairment_to_revenue': (ZERO, ONE)}


def number(value, name='value'):
    if value is None or isinstance(value, bool):
        raise ValueError(f'{name}: requires a finite number')
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'{name}: requires a finite number') from None
    if not result.is_finite():
        raise ValueError(f'{name}: requires a finite number')
    return result


def serial(value):
    if isinstance(value, D):
        return format(value, 'f')
    if isinstance(value, dict):
        return {k: serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    return value


def _d(value):
    """可选数值：取不到返回 None，不抛异常（与 number 的严格语义区分开）。"""
    if value is None or value == '' or isinstance(value, bool):
        return None
    if isinstance(value, D):
        return value
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def validate(params):
    for key, (low, high) in BOUNDS.items():
        if key in params and not low <= number(params[key], key) <= high:
            raise ValueError(f'{key}: outside [{low}, {high}]')
    if number(params.get('revenue_base'), 'revenue_base') <= 0:
        raise ValueError('revenue_base: must be positive')
    if params.get('shares') is not None and number(params['shares'], 'shares') <= 0:
        raise ValueError('shares: must be positive or missing')
    if params['wacc'] <= params['terminal_growth']:
        raise ValueError('wacc must exceed terminal_growth')
    if params['terminal_growth'] > params['terminal_roic']:
        raise ValueError('terminal_growth exceeds terminal_roic: reinvestment would exceed NOPAT')


def market_date_checks(market, report_period, valuation_date=None):
    """Date validation is diagnostic; it never extrapolates observed inputs."""
    if not market:
        return []
    def parse(raw, name):
        if not isinstance(raw, str) or len(raw) != 10:
            raise ValueError(f'{name} requires ISO date YYYY-MM-DD')
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            raise ValueError(f'{name} requires ISO date YYYY-MM-DD') from None
        if parsed.isoformat() != raw:
            raise ValueError(f'{name} requires ISO date YYYY-MM-DD')
        return parsed
    today = parse(valuation_date, 'valuation_date') if valuation_date else date.today()
    dates = []
    if market.get('as_of'):
        dates.append(('market.as_of', parse(market['as_of'], 'market.as_of')))
    for key, source in market.get('field_sources', {}).items():
        if isinstance(source, dict) and source.get('as_of'):
            dates.append((f'market.{key}.as_of', parse(source['as_of'], f'market.{key}.as_of')))
    warnings = []
    report_date = None
    if report_period:
        text = str(report_period)
        try:
            report_date = date.fromisoformat(text if len(text) == 10 else text + '-12-31')
        except ValueError:
            warnings.append('报告日期无法转换为完整日期，未推断市场数据与报告日期的先后。')
    for name, observed in dates:
        if observed > today:
            raise ValueError(f'{name} is in the future relative to valuation_date')
        if (today - observed).days > 365:
            warnings.append(f'{name} 距估值日期已超过365天；采用原始观测值，未自动外推为当前市场事实。')
        if report_date and observed < report_date:
            warnings.append(f'{name} 早于财报期末；市场与财报时点不一致，需核验。')
    return warnings


def equity_cost_from_market(market, currency):
    required = ('risk_free_rate', 'equity_risk_premium', 'beta', 'source', 'as_of', 'currency')
    if not market or any(market.get(k) in (None, '') for k in required):
        return None
    if currency and market['currency'] != currency:
        raise ValueError('market currency differs from financial inputs')
    rf, erp, beta = (number(market[k], k) for k in required[:3])
    country = number(market.get('country_risk_premium', 0), 'country_risk_premium')
    if not ZERO <= rf <= ONE or not ZERO <= erp <= ONE or not ZERO <= beta <= D(5) or not ZERO <= country <= ONE:
        raise ValueError('market equity CAPM inputs outside valid bounds')
    return rf + beta * erp + country


def derive_discount_rate(params, market, currency):
    if market and market.get('currency') and currency and market['currency'] != currency:
        raise ValueError('market currency differs from financial inputs')
    required = ('risk_free_rate', 'equity_risk_premium', 'beta', 'cost_of_debt', 'debt_weight',
                'source', 'as_of', 'currency')
    if not market or any(market.get(k) in (None, '') for k in required):
        return {'method': 'explicit_override' if params.get('_wacc_override') else 'default_assumption',
                'wacc': params['wacc'], 'source': '使用者假设' if params.get('_wacc_override') else 'DEFAULT_ASSUMPTIONS',
                'note': '缺少完整且有来源、日期和币种的市场输入，未声称市场校准 WACC。'}
    if currency and market['currency'] != currency:
        raise ValueError('market currency differs from financial inputs')
    rf, erp, beta, debt, weight = (number(market[k], k) for k in required[:5])
    country = number(market.get('country_risk_premium', 0), 'country_risk_premium')
    if not ZERO <= rf <= ONE or not ZERO <= erp <= ONE or not ZERO <= beta <= D(5) or not ZERO <= debt <= ONE or not ZERO <= weight <= ONE or not ZERO <= country <= ONE:
        raise ValueError('market CAPM inputs outside valid bounds')
    cost_equity = rf + beta * erp + country
    wacc = cost_equity * (ONE - weight) + debt * (ONE - params['tax_rate']) * weight
    if not params.get('_wacc_override'):
        params['wacc'] = wacc
    return {'method': 'explicit_override' if params.get('_wacc_override') else 'CAPM',
            'wacc': params['wacc'], 'derived_wacc': wacc, 'cost_of_equity': cost_equity,
            'after_tax_cost_of_debt': debt * (ONE - params['tax_rate']),
            'market_inputs': market, 'source': market['source'], 'as_of': market['as_of'],
            'currency': market['currency'],
            'input_status': 'supplied_market_assumptions_not_independently_verified',
            'debt_weight_basis': market.get('debt_weight_basis', 'unspecified_user_assumption'),
            'debt_weight_status': 'supplied_market_value_weight' if market.get('debt_weight_basis') == 'market_value' else 'assumed_capital_structure_weight'}


def driver_paths(params, forecast, reference=None):
    """Explicit paths keep risk deltas; automatic fade retains scenario endpoint shifts."""
    years = int(params['years'])
    reference = reference or params
    explicit = forecast.get('paths', {})
    unknown = set(explicit) - set(DRIVERS)
    if unknown:
        raise ValueError('unsupported forecast paths: ' + ', '.join(sorted(unknown)))
    paths = {}
    for key in DRIVERS:
        shift = params[key] - reference[key]
        if key in explicit:
            raw = explicit[key]
            if not isinstance(raw, list) or len(raw) != years:
                raise ValueError(f'{key} path must contain exactly {years} annual values')
            values = [number(v, key) + shift for v in raw]
        else:
            endpoint = number(forecast.get('stable', {}).get(key, reference[key]), key) + shift
            if key == 'revenue_growth' and key not in forecast.get('stable', {}):
                endpoint = params['terminal_growth']
            values = [params[key] + (endpoint - params[key]) * D(t) / D(years - 1)
                      for t in range(years)]
        low, high = BOUNDS[key]
        if any(v < low or v > high for v in values):
            raise ValueError(f'{key} forecast path outside valid bounds')
        paths[key] = values
    return paths


def evaluate(params, forecast, reference=None):
    validate(params)
    paths = driver_paths(params, forecast, reference)
    revenue = params['revenue_base']
    def nwc(rev, gm, ar, inv, ap):
        return rev * (ar + (inv - ap) * (ONE - gm)) / D(365)
    previous_nwc = nwc(revenue, params['gross_margin'], params['receivable_days'],
                       params['inventory_days'], params['payable_days'])
    rows, pv_explicit = [], ZERO
    for index in range(int(params['years'])):
        t = index + 1
        v = {key: paths[key][index] for key in DRIVERS}
        revenue *= ONE + v['revenue_growth']
        cogs, opex = revenue * (ONE - v['gross_margin']), revenue * v['opex_ratio']
        impairment = revenue * params['impairment_to_revenue'] if t == 1 else ZERO
        ebit = revenue - cogs - opex - impairment
        # Operating losses do not invent immediately usable tax refunds.
        tax = max(ebit, ZERO) * v['tax_rate']
        nopat = ebit - tax
        da, capex = revenue * v['da_to_revenue'], revenue * v['capex_to_revenue']
        working = nwc(revenue, v['gross_margin'], v['receivable_days'], v['inventory_days'], v['payable_days'])
        delta = working - previous_nwc
        raw = nopat + da - capex - delta
        haircut = params['near_term_fcff_haircut'] if t <= 2 else ZERO
        # A haircut on negative cashflow must increase its magnitude, not improve it.
        fcff = raw - abs(raw) * haircut
        fcff -= abs(fcff) * params['sustainable_profit_discount']
        discount = ONE / (ONE + params['wacc']) ** t
        pv_explicit += fcff * discount
        rows.append(dict(year=t, **v, revenue=revenue, cogs=cogs, operating_expenses=opex,
                         impairment=impairment, ebit=ebit, operating_tax=tax, nopat=nopat,
                         depreciation_amortization=da, capex=capex, nwc=working,
                         delta_nwc=delta, fcff_before_haircuts=raw, fcff=fcff,
                         discount_factor=discount, pv_fcff=fcff * discount))
        previous_nwc = working
    last = rows[-1]
    g, w = params['terminal_growth'], params['wacc']
    terminal_revenue = revenue * (ONE + g)
    terminal_ebit = terminal_revenue * (last['gross_margin'] - last['opex_ratio'])
    terminal_nopat = terminal_ebit - max(terminal_ebit, ZERO) * last['tax_rate']
    reinvestment_rate = g / params['terminal_roic']
    terminal_fcff_raw = terminal_nopat * (ONE - reinvestment_rate)
    terminal_fcff = terminal_fcff_raw - abs(terminal_fcff_raw) * params['sustainable_profit_discount']
    tv = terminal_fcff / (w - g)
    pv_tv = tv / (ONE + w) ** int(params['years'])
    ev = pv_explicit + pv_tv
    equity = ev - params['net_debt'] - params['minority_interest'] + params['non_operating_assets']
    val = dict(ok=True, enterprise_value=ev, equity_value=equity,
               value_per_share=equity / params['shares'] if params.get('shares') else None,
               pv_explicit_fcff=pv_explicit, pv_terminal_value=pv_tv,
               terminal_value=tv, terminal_fcff=terminal_fcff, terminal_nopat=terminal_nopat,
               terminal_reinvestment_rate=reinvestment_rate,
               terminal_reinvestment=terminal_nopat * reinvestment_rate,
               terminal_value_share=pv_tv / ev if ev else None, wacc=w, terminal_growth=g,
               net_debt=params['net_debt'], minority_interest=params['minority_interest'],
               non_operating_assets=params['non_operating_assets'])
    return rows, val


def residual_income(model, years, shares):
    required = ('book_equity', 'net_income', 'cost_of_equity', 'roe', 'payout_ratio', 'terminal_roe')
    missing = [k for k in required if model.get(k) is None]
    if missing:
        raise ValueError('financial institutions require residual-income inputs: ' + ', '.join(missing))
    m = {k: number(model[k], k) for k in required}
    if m['book_equity'] <= 0 or not ZERO < m['cost_of_equity'] <= ONE or not ZERO <= m['payout_ratio'] <= ONE or not ZERO <= m['roe'] <= ONE or not ZERO <= m['terminal_roe'] <= ONE:
        raise ValueError('invalid residual-income parameters')
    book, pv, rows = m['book_equity'], ZERO, []
    for t in range(1, years + 1):
        roe = m['roe'] + (m['terminal_roe'] - m['roe']) * D(t - 1) / D(years - 1)
        income = m['net_income'] if t == 1 else book * roe
        ri = income - book * m['cost_of_equity']
        dividend = income * m['payout_ratio']
        close = book + income - dividend
        if close <= 0:
            raise ValueError('residual-income book equity becomes nonpositive')
        discount = ONE / (ONE + m['cost_of_equity']) ** t
        pv += ri * discount
        rows.append(dict(year=t, opening_book_equity=book, roe=roe, net_income=income,
                         dividends=dividend, residual_income=ri, closing_book_equity=close,
                         discount_factor=discount, pv_residual_income=ri * discount))
        book = close
    g = m['terminal_roe'] * (ONE - m['payout_ratio'])
    if g >= m['cost_of_equity']:
        raise ValueError('residual-income terminal growth must be below cost of equity')
    terminal_ri = book * (m['terminal_roe'] - m['cost_of_equity'])
    tv = terminal_ri / (m['cost_of_equity'] - g)
    pv_tv = tv / (ONE + m['cost_of_equity']) ** years
    equity = m['book_equity'] + pv + pv_tv
    return rows, dict(ok=True, equity_value=equity, value_per_share=equity / shares if shares else None,
                      enterprise_value=None, pv_explicit_fcff=None, terminal_fcff=None,
                      pv_explicit_residual_income=pv, terminal_residual_income=terminal_ri,
                      pv_terminal_value=pv_tv, terminal_value=tv,
                      terminal_value_share=pv_tv / equity if equity else None,
                      wacc=None, cost_of_equity=m['cost_of_equity'], terminal_growth=g)


def sensitivity(params, forecast):
    matrix, tornado = [], []
    for gshift in (D('-.01'), ZERO, D('.01')):
        cells = []
        for wshift in (D('-.02'), ZERO, D('.02')):
            p = dict(params, wacc=params['wacc'] + wshift,
                     terminal_growth=params['terminal_growth'] + gshift)
            try:
                value = evaluate(p, forecast)[1]['equity_value']
                cells.append({'wacc': p['wacc'], 'equity_value': value, 'ok': True})
            except ValueError as error:
                cells.append({'wacc': p['wacc'], 'equity_value': None, 'ok': False, 'reason': str(error)})
        matrix.append({'terminal_growth': params['terminal_growth'] + gshift, 'cells': cells})
    for key, step in [('wacc', D('.01')), ('terminal_growth', D('.005')),
                      ('gross_margin', D('.02')), ('revenue_growth', D('.02')),
                      ('opex_ratio', D('.01')), ('capex_to_revenue', D('.01'))]:
        vals = []
        for direction in (-1, 1):
            p = dict(params)
            p[key] += step * direction
            try:
                vals.append(evaluate(p, forecast, params)[1]['equity_value'])
            except ValueError:
                vals.append(None)
        tornado.append({'parameter': key, 'step': step, 'low_value': vals[0], 'high_value': vals[1],
                        'impact': abs(vals[1] - vals[0]) if all(v is not None for v in vals) else None})
    tornado.sort(key=lambda x: x['impact'] if x['impact'] is not None else D(-1), reverse=True)
    return {'matrix': matrix, 'tornado': tornado, 'note': '逐项扰动并重算预测与终值；排序按权益价值变化绝对幅度。'}


def reverse_dcf(params, forecast, config):
    if not config:
        return {'enabled': False}
    try:
        key = config.get('parameter', 'revenue_growth')
        if key not in ('revenue_growth', 'gross_margin', 'wacc'):
            raise ValueError('unsupported reverse-DCF parameter')
        target = config.get('market_equity_value')
        if target is None:
            target = number(config.get('price'), 'price') * number(config.get('shares', params.get('shares')), 'shares')
        target = number(target, 'market_equity_value')
        if target <= 0:
            raise ValueError('market equity value must be positive')
        low, high = number(config.get('low', '.001')), number(config.get('high', '.5'))
        if low >= high:
            raise ValueError('reverse DCF requires low < high')
        def gap(value):
            p = dict(params)
            p[key] = value
            return evaluate(p, forecast, params)[1]['equity_value'] - target
        flo, fhi = gap(low), gap(high)
        if flo * fhi > 0:
            return {'enabled': True, 'ok': False, 'status': 'no_root', 'parameter': key,
                    'target_equity_value': target, 'low_equity_value': flo + target,
                    'high_equity_value': fhi + target, 'reason': '目标未被指定参数区间夹住；未返回边界假根。'}
        midpoint, fm = low, flo
        for count in range(100):
            if flo == 0:
                midpoint, fm = low, flo
                break
            midpoint = (low + high) / 2
            fm = gap(midpoint)
            if abs(fm) <= max(abs(target), ONE) * D('1e-12'):
                break
            if flo * fm <= 0:
                high = midpoint
            else:
                low, flo = midpoint, fm
        return {'enabled': True, 'ok': True, 'status': 'solved', 'parameter': key,
                'implied_value': midpoint, 'target_equity_value': target,
                'calculated_equity_value': target + fm, 'iterations': count + 1,
                'note': '市场价格反推的是使模型匹配价格的假设，不是预测。'}
    except (ValueError, TypeError) as error:
        return {'enabled': True, 'ok': False, 'status': 'invalid_input', 'reason': str(error)}


def monte_carlo(params, forecast, config):
    if not config:
        return {'enabled': False}
    try:
        raw_samples = number(config.get('samples', 500), 'samples')
        if raw_samples != raw_samples.to_integral_value() or not 1 <= raw_samples <= 2000:
            raise ValueError('samples must be an integer in [1, 2000]')
        samples = int(raw_samples)
        raw_seed = number(config.get('seed', 0), 'seed')
        if raw_seed != raw_seed.to_integral_value():
            raise ValueError('seed must be an integer')
        seed = int(raw_seed)
        variables = config.get('variables', {})
        if not variables:
            raise ValueError('provide explicit triangular variables; no hidden probability assumptions')
        distributions = {}
        for key, shape in variables.items():
            if key not in BOUNDS:
                raise ValueError('unsupported Monte Carlo parameter: ' + key)
            low, mode, high = (number(shape.get(k), key + '.' + k) for k in ('low', 'mode', 'high'))
            if not low <= mode <= high or not BOUNDS[key][0] <= low <= high <= BOUNDS[key][1]:
                raise ValueError('invalid triangular bounds for ' + key)
            distributions[key] = (low, mode, high)
        correlation = config.get('correlations')
        factor = None
        names = list(distributions)
        if correlation:
            size = len(names)
            if isinstance(correlation, dict):
                unknown = set(correlation) - set(names)
                if unknown:
                    raise ValueError('correlation contains unknown variables')
                matrix = [[number(correlation.get(a, {}).get(b, ONE if a == b else ZERO), 'correlation')
                           for b in names] for a in names]
            elif isinstance(correlation, list) and len(correlation) == size and all(isinstance(row, list) and len(row) == size for row in correlation):
                matrix = [[number(v, 'correlation') for v in row] for row in correlation]
            else:
                raise ValueError('correlations must be a symmetric variable map or square matrix in variables order')
            if any(abs(matrix[i][j]) > ONE or matrix[i][j] != matrix[j][i]
                   or (i == j and matrix[i][j] != ONE) for i in range(size) for j in range(size)):
                raise ValueError('correlation matrix must be symmetric, bounded [-1,1], with diagonal 1')
            # Gaussian copula dependence draws are floats; cashflows and triangular
            # inverse transforms still use Decimal. Require positive definiteness.
            factor = [[0.0] * size for _ in range(size)]
            for i in range(size):
                for j in range(i + 1):
                    remainder = float(matrix[i][j]) - sum(factor[i][k] * factor[j][k] for k in range(j))
                    if i == j:
                        if remainder <= 1e-12:
                            raise ValueError('correlation matrix must be positive definite')
                        factor[i][j] = math.sqrt(remainder)
                    else:
                        factor[i][j] = remainder / factor[j][j]
        rng, values, rejected = random.Random(seed), [], {}
        for _ in range(samples):
            p = dict(params)
            uniforms = None
            if factor:
                normal = [rng.gauss(0, 1) for _ in names]
                uniforms = [NormalDist().cdf(sum(factor[i][j] * normal[j] for j in range(i + 1)))
                            for i in range(len(names))]
            for index, (key, (low, mode, high)) in enumerate(distributions.items()):
                # Random draw is converted once; inverse triangular transform is Decimal.
                u = D(str(uniforms[index] if uniforms is not None else rng.random()))
                if low == high:
                    draw = low
                elif u <= (mode - low) / (high - low):
                    draw = low + (u * (high - low) * (mode - low)).sqrt()
                else:
                    draw = high - ((ONE - u) * (high - low) * (high - mode)).sqrt()
                p[key] = draw
            try:
                values.append(evaluate(p, forecast, params)[1]['equity_value'])
            except ValueError as error:
                reason = str(error)
                rejected[reason] = rejected.get(reason, 0) + 1
        values.sort()
        def quantile(q):
            at = D(len(values) - 1) * q
            i = int(at)
            return values[i] + (values[min(i + 1, len(values) - 1)] - values[i]) * (at - i)
        return {'enabled': True, 'ok': bool(values), 'samples': samples, 'seed': seed,
                'valid_samples': len(values), 'invalid_samples': samples - len(values),
                'invalid_reasons': rejected, 'variables': variables,
                'distribution': 'Gaussian-copula triangular user assumptions' if factor else 'independent triangular user assumptions',
                'correlations': correlation,
                'correlation_basis': 'latent_normal_dependence_not_Pearson_cashflow_correlation' if factor else None,
                'quantiles': {name: quantile(q) for name, q in [('p05', D('.05')), ('p50', D('.5')), ('p95', D('.95'))]} if values else {},
                'mean': sum(values, ZERO) / len(values) if values else None,
                'note': '用户输入的有界三角假设；相关矩阵为高斯copula潜变量相关性。仅有效样本的条件分布，不代表市场概率。无效样本公开计数，未重抽或截断。'}
    except (ValueError, TypeError, AttributeError) as error:
        return {'enabled': True, 'ok': False, 'reason': str(error)}


def build_advanced_model(analysis: dict, findings: list | None = None, options: dict | None = None) -> dict:
    """Build a JSON-safe auditable model; invalid inputs produce blocked diagnostics."""
    options = options or {}
    out = dict(engine_version=ENGINE_VERSION, ok=False, model_plan={}, discount_rate={},
               terminal_policy={}, inputs_used={}, assumption_sources={}, checks=[], warnings=[],
               scenarios={}, range={}, sensitivity={}, monte_carlo={'enabled': False}, reverse_dcf={'enabled': False})
    try:
        with localcontext() as ctx:
            ctx.prec = 28
            facts, metrics = analysis.get('facts', []), analysis.get('metrics', [])
            # Source amounts are already normalized by financial_analysis (unit/scale
            # describes the original document). Select a single reporting scope.
            verified = [f for f in facts if f.get('status') == 'verified']
            revenues = sorted((f for f in verified if f.get('code') == 'revenue'),
                              key=lambda f: (str(f.get('period', '')), f.get('scope') == 'consolidated'))
            annual_revenues = [f for f in revenues if f.get('period_type') in (None, '', 'annual')]
            if revenues and not annual_revenues and 'revenue_base' not in options.get('overrides', {}):
                raise ValueError('annual revenue required; quarter/YTD revenue is not annualized automatically')
            if annual_revenues:
                if revenues[-1] not in annual_revenues:
                    out['warnings'].append('最新收入为单季或累计口径；模型使用最近年度收入，未自动年化。')
                revenues = annual_revenues
            anchors = revenues or sorted((f for f in verified if f.get('code') == 'parent_net_profit' and f.get('period_type') in (None, '', 'annual')), key=lambda f: (str(f.get('period', '')), f.get('scope') == 'consolidated'))
            selected_period = str(anchors[-1].get('period')) if anchors else None
            selected_scope = anchors[-1].get('scope') if anchors else None
            if selected_scope:
                excluded = [f for f in facts if f.get('scope') not in (None, '', selected_scope)]
                facts = [f for f in facts if f.get('scope') in (None, '', selected_scope)]
                metrics = [m for m in metrics if m.get('scope') in (None, '', selected_scope)]
                if excluded:
                    out['warnings'].append('已排除其他报表口径事实，避免合并与母公司数据混用。')
            if selected_period:
                facts = [f for f in facts if ((f.get('code') == 'revenue' and f.get('period_type') in (None, '', 'annual'))
                                             or str(f.get('period')) == selected_period)
                         and f.get('period_type') not in ('quarter', 'ytd')]
                metrics = [m for m in metrics if str(m.get('period')) == selected_period
                           and m.get('period_type') not in ('quarter', 'ytd')]
            failed_checks = [c for c in analysis.get('checks', [])
                             if (c.get('status') in ('fail', 'failed') or c.get('ok') is False)
                             and c.get('scope') in (None, '', selected_scope)
                             and (not selected_period or str(c.get('period', selected_period)) == selected_period)]
            unresolved = [f for f in facts if f.get('status') == 'needs_review'
                          and f.get('code') in ('revenue', 'cost_of_revenue', 'total_assets', 'total_equity', 'net_profit')]
            if failed_checks or unresolved:
                if not options.get('allow_illustrative_model'):
                    raise ValueError('unresolved financial tie-outs/core facts require review before valuation')
                out['warnings'].append('存在未解决的勾稽或核心事实冲突；已显式允许示意建模，不能声称数据核验通过。')
                out['illustrative'] = True
            verified = [f for f in facts if f.get('status') == 'verified']
            share_codes = {'total_shares', 'issued_shares', 'ordinary_shares', 'shares_outstanding'}
            monetary = [f for f in verified if f.get('code') not in share_codes and f.get('code') not in ('basic_eps', 'diluted_eps')]
            currencies = {f.get('currency') for f in monetary if f.get('currency') not in (None, '', 'unknown')}
            if len(currencies) > 1:
                raise ValueError('incompatible financial currency: ' + ', '.join(sorted(currencies)))
            companies = {f.get('company') for f in monetary if f.get('company')}
            if len(companies) > 1:
                raise ValueError('incompatible financial company scope')
            for f in monetary:
                # 值单位必须表示"已归一化到基准货币的绝对额"。
                # 数据管道历史上会写 'CNY'（币种）而不是 '元'（单位）—— 两者语义相同，
                # 都表示"值是以元为单位的金额"，因此一并接受。实测踩过：只认 '元'
                # 会让所有真实运行被这条校验挡住，高级估值链路完全跑不通。
                # 但 'CNY/share'（每股口径）、'万元'（未归一化）必须拒绝：
                # 前者是每股指标，后者直接进模型会错 10000 倍。
                unit = f.get('value_unit')
                if unit and unit not in ('元', 'base_currency', 'currency_units', 'CNY', 'RMB'):
                    raise ValueError('financial values must be normalized to base currency units')
                if f.get('scale') is not None and number(f['scale'], 'scale') <= 0:
                    raise ValueError('financial unit scale must be positive')
            currency = next(iter(currencies), analysis.get('currency'))
            calibration = calibrate_from_facts(facts, metrics)
            params = dict(DEFAULT_ASSUMPTIONS, **calibration['params'])
            forecast = options.get('forecast', {})
            params['terminal_roic'] = number(forecast.get('terminal_roic', '.10'), 'terminal_roic')
            # Profit/EPS derives weighted-average shares, not the issued denominator.
            estimated_shares = params.get('shares')
            params['shares'] = None
            share_facts = sorted((f for f in verified if f.get('code') in share_codes
                                  and f.get('unit') in ('股', 'shares', 'share')
                                  and (not revenues or str(f.get('period')) == str(revenues[-1].get('period')))),
                                 key=lambda f: str(f.get('period', '')))
            if share_facts:
                params['shares'] = number(share_facts[-1]['value'], 'shares')
                calibration['provenance']['shares'] = {'source': 'fact', 'share_basis': 'issued_shares',
                                                       'period': share_facts[-1].get('period'),
                                                       'evidence_ids': share_facts[-1].get('evidence_ids', [])}
            elif options.get('allow_estimated_shares') and estimated_shares:
                params['shares'] = estimated_shares
                calibration['provenance']['shares'] = {'source': 'derived', 'share_basis': 'weighted_average_estimate',
                                                       'detail': '归母净利润/EPS；经使用者显式允许，非期末已发行股数。'}
                out['warnings'].append('每股价值使用净利润/EPS推算的加权平均股数，需核验期末已发行股数。')
            else:
                calibration['provenance'].pop('shares', None)
            market_shares = options.get('market', {})
            out['warnings'].extend(market_date_checks(market_shares, selected_period, options.get('valuation_date')))
            if params['shares'] is None and market_shares.get('shares') is not None:
                if not all(market_shares.get(k) for k in ('share_basis', 'source', 'as_of')):
                    raise ValueError('market shares require share_basis, source and as_of')
                if market_shares['share_basis'] not in ('total_ordinary', 'issued_ordinary', 'shares_outstanding'):
                    raise ValueError('market share_basis must be total_ordinary, issued_ordinary or shares_outstanding; float/weighted averages are invalid')
                params['shares'] = number(market_shares['shares'], 'shares')
                calibration['provenance']['shares'] = {k: market_shares[k] for k in ('share_basis', 'source', 'as_of')}
            overrides = options.get('overrides', {})
            for key, value in overrides.items():
                if key not in params and key not in ('revenue_base',):
                    raise ValueError('unsupported override: ' + key)
                params[key] = number(value, key) if value is not None else None
            for key, value in list(params.items()):
                if value is not None:
                    params[key] = number(value, key)
            years_raw = number(forecast.get('years', params['years']), 'years')
            if years_raw != years_raw.to_integral_value() or not 5 <= years_raw <= 15:
                raise ValueError('forecast years must be an integer in [5, 15]')
            params['years'] = int(years_raw)
            params['_wacc_override'] = 'wacc' in overrides
            out['inputs_used'] = calibration['provenance']
            out['assumption_sources'] = {k: ({'source': 'user_override'} if k in overrides else calibration['provenance'].get(k, {'source': 'default_assumption'})) for k in params if not k.startswith('_')}
            for key in forecast.get('paths', {}):
                out['assumption_sources'][key] = {'source': 'user_forecast_path'}
            out['assumption_sources']['terminal_roic'] = {'source': 'user_forecast' if 'terminal_roic' in forecast else 'default_assumption', 'note': '稳定期 ROIC 假设，非市场测量值。'}
            out['warnings'].extend(calibration['warnings'])
            out['checks'].extend(calibration['checks'])
            if params.get('shares') is not None and params['shares'] <= 0:
                raise ValueError('shares must be positive')
            if params.get('shares') is None:
                out['warnings'].append('未提供总股本；每股价值为空，不以零股本替代。')
            industry = str(options.get('industry', analysis.get('industry', ''))).lower()
            if not industry:
                industry = ' '.join(str(v) for v in companies).lower()
                out['warnings'].append('行业信息不足；模型路由仅依据公司名称线索，需使用者核验行业。')
            financial = any(word in industry for word in ('bank', 'insurance', 'financial', '银行', '保险', '金融', '证券', 'securit'))
            out['model_plan'] = {'method': 'residual_income' if financial else 'multi_stage_fcff',
                                 'years': params['years'], 'forecast_type': 'operating_driver_forecast',
                                 'balanced_three_statements': False, 'currency': currency,
                                 'scope': next(iter({f.get('scope') for f in verified if f.get('scope')}), None)}
            if any(word in industry for word in ('real estate', 'property', '房地产', '地产')):
                out['warnings'].append('房地产/持有物业业务需净资产或项目现金流交叉估值；通用驱动 FCFF 仅作示意。')
                out['illustrative'] = True
            if financial:
                supplied_model = options.get('equity_model', {})
                model = {k: v for k, v in supplied_model.items() if k != 'scenarios'}
                equity_sources = {k: {'source': 'user_equity_assumption'} for k in model}
                for key, codes in [('book_equity', ('parent_equity',)), ('net_income', ('parent_net_profit',))]:
                    if model.get(key) is None:
                        candidates = [f for f in verified if f.get('code') in codes
                                      and (not selected_period or str(f.get('period')) == selected_period)]
                        if candidates:
                            chosen = candidates[-1]
                            model[key] = number(chosen['value'], key)
                            equity_sources[key] = {'source': 'fact', 'period': chosen.get('period'),
                                                   'scope': chosen.get('scope'), 'evidence_ids': chosen.get('evidence_ids', [])}
                if model.get('cost_of_equity') is None:
                    cost = equity_cost_from_market(options.get('market'), currency)
                    if cost is not None:
                        model['cost_of_equity'] = cost
                        equity_sources['cost_of_equity'] = {'source': 'CAPM_supplied_inputs',
                                                            'as_of': market_shares['as_of'], 'detail': 'rf + beta*ERP + country risk'}
                if model.get('roe') is None:
                    actual_roes = [m for m in metrics if m.get('code') == 'roe' and m.get('value') is not None]
                    if actual_roes:
                        model['roe'] = number(actual_roes[-1]['value'], 'roe')
                        equity_sources['roe'] = {'source': 'historical_metric_extrapolation_assumption',
                                                  'period': actual_roes[-1].get('period'),
                                                  'note': '历史ROE作为首年预测假设，非已核验未来ROE。'}
                        out['warnings'].append('历史ROE已用作未来首年假设；未来盈利能力仍需使用者核验。')
                required = ('book_equity', 'net_income', 'cost_of_equity', 'roe', 'payout_ratio', 'terminal_roe')
                missing = [key for key in required if model.get(key) is None]
                out['data_needs'] = [{'parameter': key, 'reason': '金融机构剩余收益模型必需；未用默认值补齐。'} for key in missing]
                out['equity_assumption_sources'] = equity_sources
                out['model_plan']['future_assumption_parameters'] = ['cost_of_equity', 'roe', 'payout_ratio', 'terminal_roe']
                if missing:
                    raise ValueError('financial institutions require residual-income inputs: ' + ', '.join(missing))
                scenario_overrides = supplied_model.get('scenarios', {})
                if set(scenario_overrides) - {'optimistic', 'stress'}:
                    raise ValueError('equity_model.scenarios supports optimistic and stress only')
                out['discount_rate'] = {'method': equity_sources['cost_of_equity']['source'],
                                        'cost_of_equity': number(model['cost_of_equity']),
                                        'input_status': 'supplied_assumptions_not_future_facts'}
                out['warnings'].append('金融机构使用清洁盈余剩余收益模型；需补充资本充足率与信用损失情景，不套用工业企业 FCFF 风险参数。')
                for name in SCENARIOS:
                    changes = scenario_overrides.get(name, {})
                    if set(changes) - {'roe', 'payout_ratio', 'terminal_roe'}:
                        raise ValueError('equity scenario permits roe, payout_ratio and terminal_roe only')
                    scenario_model = dict(model, **changes)
                    rows, valuation = residual_income(scenario_model, params['years'], params.get('shares'))
                    adjustments = [{'parameter': k, 'from': number(model[k]), 'to': number(v),
                                    'source': 'explicit_user_equity_scenario_assumption',
                                    'path': 'ROE/分红/稳定期ROE假设传导至净收益、账面权益与剩余收益'}
                                   for k, v in changes.items() if number(v) != number(model[k])]
                    out['scenarios'][name] = {'label': SCENARIOS[name]['label'],
                                               'parameters': dict(scenario_model, shares=params.get('shares'), years=params['years']),
                                               'projection': rows, 'valuation': valuation,
                                               'risk_adjustments': adjustments,
                                               'scenario_status': 'explicit_equity_assumption' if adjustments else 'shared_equity_assumptions_no_risk_quantification'}
                base_val = out['scenarios']['base']['valuation']
                stress_val = out['scenarios']['stress']['valuation']
                gap = stress_val['equity_value'] - base_val['equity_value']
                distinct = bool(out['scenarios']['stress']['risk_adjustments']) and gap != 0
                out['stress_diagnostic'] = {'insufficient': not distinct, 'unquantified_credit_capital_risk': True,
                                            'equity_value_gap_vs_base': gap / base_val['equity_value'] if base_val['equity_value'] else None,
                                            'reason': '仅显式股权假设情景；尚未量化信用损失和资本充足率风险。' if distinct else '未提供不同压力情景假设；压力情景与基准相同，不构成信用或资本压力测试。'}
                out['terminal_policy'] = {'method': 'clean_surplus_residual_income', 'terminal_growth': base_val['terminal_growth']}
            else:
                out['discount_rate'] = derive_discount_rate(params, options.get('market'), currency)
                out['assumption_sources']['wacc'] = {'source': out['discount_rate']['method']}
                # ---- 折现率的财报侧校准 ----
                # market 不完整时 derive_discount_rate 会退化为硬编码默认值（0.10）。
                # 但债务成本、资本结构权重、存款收益率这三项**能从年报直接算出来**，
                # 且本模块用敏感度归因量化了每项参数对 WACC 的影响 ——
                # 净现金公司的债务权重可能只有 1%，此时债务成本无关紧要，
                # 而 β 与股权风险溢价才是决定性的。这个判断必须落在输出里。
                try:
                    from . import wacc as wacc_module
                    detail = wacc_module.derive(
                        facts, metrics, overrides=overrides, industry=industry,
                        market=options.get('market') or {},
                        period=selected_period, scope=selected_scope,
                        tax_rate=params.get('tax_rate'))
                    out['discount_rate_detail'] = detail
                    if (out['discount_rate']['method'] == 'default_assumption'
                            and detail.get('ok')
                            and detail.get('statement_calibrated')
                            and not params.get('_wacc_override')):
                        out['discount_rate']['method'] = 'financial_statement_calibration'
                        out['discount_rate']['wacc'] = detail['wacc']
                        out['discount_rate']['derived_wacc'] = detail['wacc']
                        out['discount_rate']['cost_of_equity'] = detail['cost_of_equity']
                        out['discount_rate']['note'] = detail['summary']
                        params['wacc'] = detail['wacc']
                        out['assumption_sources']['wacc'] = {
                            'source': 'financial_statement_calibration',
                            'detail': '债务成本/资本结构由财报实算，β 由行业去杠杆 β 经 Hamada 加杠杆，'
                                      '无风险利率与股权风险溢价为外部参数或默认值'}
                        out['warnings'].extend(detail.get('warnings') or [])
                    elif out['discount_rate']['method'] == 'default_assumption':
                        # 校准不成立（财报里一项都取不到）时必须说明，否则读者会以为
                        # 6.x% 的折现率是算出来的。detail 里的警告已经写明了原因。
                        out['warnings'].extend(detail.get('warnings') or [])
                except (ValueError, TypeError, AttributeError, InvalidOperation, KeyError) as error:
                    out['discount_rate_detail'] = {'ok': False, 'reason': str(error)}
                # ---- 把三表模型需要的融资利率注入参数 ----
                # 驱动法 FCFF 是无杠杆的，用不到借款成本与存款收益率；但三表联动
                # 必须计利息，否则 447 亿元现金产生的利息收入恒为 0 —— 这个失效
                # 是静默的：三表照样配平、勾稽全过，只是每年凭空少掉十几亿利润，
                # 而且报告上看不出任何异常。所以这两个利率必须显式注入并留下来源。
                financing_rates = {}
                _detail = out.get('discount_rate_detail') or {}
                _kd = _d(_detail.get('cost_of_debt'))
                if _kd is None:
                    _kd = _d((_detail.get('cost_of_debt_detail') or {}).get('midpoint'))
                if _kd is not None:
                    financing_rates['cost_of_debt'] = _kd
                    params['cost_of_debt'] = _kd
                # cash_yield 是 {ok, rate, ...} 结构，不是标量 —— 取 rate 而不是整个字典
                _cy_info = _detail.get('cash_yield') or {}
                _cy = _d(_cy_info.get('rate')) if isinstance(_cy_info, dict) else _d(_cy_info)
                if _cy is not None:
                    financing_rates['cash_yield'] = _cy
                    params['cash_yield'] = _cy
                out['financing_rates'] = {
                    'values': {k: serial(v) for k, v in financing_rates.items()},
                    'source': 'financial_statement_calibration',
                    'note': '借款成本与存款收益率由年报实算或按信用利差取量级值；'
                            '驱动法 FCFF 不使用它们，三表联动计息时使用。',
                }
                if not financing_rates:
                    out['warnings'].append(
                        '未能从财报推算出借款成本与存款收益率，三表联动的利息费用与利息收入'
                        '将按 0 处理，利润表会被系统性低估。')
                validate(params)
                out['terminal_policy'] = {'method': 'NOPAT*(1-g/ROIC)', 'terminal_roic': params['terminal_roic'],
                                          'reinvestment_rate': params['terminal_growth'] / params['terminal_roic'],
                                          'source': 'https://pages.stern.nyu.edu/~adamodar/New_Home_Page/valquestions/termvalueexreturns.htm'}
                payout = number(forecast.get('payout_ratio', options.get('payout_ratio', '.5')), 'payout_ratio')
                for name in SCENARIOS:
                    mapped = risks_to_parameters(params, findings or [], name)
                    p = dict(mapped['parameters'])
                    try:
                        rows, valuation = evaluate(p, forecast, params)
                    except ValueError as error:
                        rows, valuation = [], {'ok': False, 'reason': str(error), 'enterprise_value': None,
                                               'equity_value': None, 'value_per_share': None}
                    out['scenarios'][name] = {'label': SCENARIOS[name]['label'],
                                               'parameters': {k: v for k, v in p.items() if not k.startswith('_')},
                                               'projection': rows, 'valuation': valuation,
                                               'risk_adjustments': mapped['applied'], 'unmatched_risks': mapped['unmatched_risks'],
                                               'floor_applied': mapped.get('floor_applied'),
                                               'binding_floor_parameters': mapped.get('binding_floor_parameters'),
                                               'evidence_driven_adjustments': mapped.get('evidence_driven_adjustments'),
                                               'degraded_to_structural_only': mapped.get('degraded_to_structural_only')}
                    # ---- 平衡三表联动预测 ----
                    # 用**同一套驱动路径**跑三表，才能与驱动法 FCFF 逐项对账。
                    # 三表会自己做四项勾稽（资产=负债+权益、现金滚动、留存收益滚存、
                    # 现金非负），任何一条不通过都说明配平逻辑有实质错误。
                    try:
                        from . import model3
                        statement_paths = driver_paths(p, forecast, params)
                        driver_fcff = [row['fcff'] for row in rows] if rows else None
                        out['scenarios'][name]['three_statements'] = model3.build(
                            facts, metrics, p, paths=statement_paths,
                            funding={'payout_ratio': payout,
                                     'mode': options.get('funding_mode'),
                                     'minimum_cash': options.get('minimum_cash'),
                                     'interest_basis': options.get('interest_basis'),
                                     'other_noncurrent_policy': options.get('other_noncurrent_policy')},
                            driver_fcff=driver_fcff)
                    except (ValueError, TypeError, AttributeError, InvalidOperation, KeyError) as error:
                        out['scenarios'][name]['three_statements'] = {'ok': False, 'reason': str(error)}
                out['sensitivity'] = sensitivity(params, forecast)
                # ---- 情景可分辨性校验 ----
                # 三情景必须真的互不相同。压力情景此前完全由法证发现驱动，
                # 扫描没有产出发现时它与基准逐位相同 —— 报告里"压力情景"一栏
                # 和基准一模一样，却没有任何提示，风险章节等于摆设。
                # 结构性压力下限已经从源头堵住这个洞，这里再做一次出口校验：
                # 只要两个情景的估值完全相等，就必须显式报出来。
                base_p = (out['scenarios'].get('base') or {}).get('parameters') or {}
                base_ev = ((out['scenarios'].get('base') or {}).get('valuation') or {}).get('equity_value')
                scenario_distinctness = []
                for name, scenario in out['scenarios'].items():
                    if name == 'base':
                        continue
                    changed = {}
                    for key in sorted(set(base_p) | set(scenario['parameters'])):
                        if key not in DRIVERS:
                            continue
                        if base_p.get(key) != scenario['parameters'].get(key):
                            changed[key] = {'base': serial(base_p.get(key)),
                                            'scenario': serial(scenario['parameters'].get(key))}
                    ev = (scenario.get('valuation') or {}).get('equity_value')
                    same = bool(base_ev is not None and ev is not None and base_ev == ev)
                    scenario_distinctness.append({
                        'scenario': name, 'label': scenario.get('label'),
                        'drivers_changed': sorted(changed), 'driver_changes': changed,
                        'equity_value_differs_from_base': not same,
                        'evidence_driven_adjustments': scenario.get('evidence_driven_adjustments'),
                        'degraded_to_structural_only': scenario.get('degraded_to_structural_only'),
                    })
                    if same:
                        out['warnings'].append(
                            f"{scenario.get('label')} 的估值与基准情景完全相同：该情景没有改变"
                            '任何驱动变量。这通常意味着本轮扫描没有产出可传导的发现，'
                            '报告不得把该情景当作压力测试的结论使用。')
                out['scenario_distinctness'] = scenario_distinctness
                degraded = [d for d in scenario_distinctness if not d['equity_value_differs_from_base']]
                out['checks'].append({
                    'name': 'scenario_distinctness',
                    'ok': not degraded,
                    'detail': ('三个情景的驱动假设与估值结果互不相同。' if not degraded else
                               '以下情景与基准情景估值完全相同，不构成有效的情景对照：'
                               + '、'.join(d['label'] or d['scenario'] for d in degraded)),
                })
                structural_only = [d['scenario'] for d in scenario_distinctness
                                   if d.get('degraded_to_structural_only')]
                if structural_only:
                    out['warnings'].append(
                        '本轮扫描未产出可传导的法证发现，'
                        + '、'.join(structural_only)
                        + ' 情景完全由结构性情景假设构成（非证据驱动）；报告必须区分说明，'
                          '不能声称压力情景由证据链推出。')
                out['monte_carlo'] = monte_carlo(params, forecast, options.get('monte_carlo'))
                out['reverse_dcf'] = reverse_dcf(params, forecast, options.get('reverse_dcf'))
                # ---- 稳定期纪律校验 ----
                # 放在情景跑完之后：终值占比是估值结果的一部分，必须一并纳入校验。
                # 折现率被财报侧校准压低时（低利率环境常见），终值占比会显著上升，
                # 此时"结论几乎完全由永续假设决定"这一点必须显式报出来。
                try:
                    from . import valuation_policy
                    base_valuation = (out['scenarios'].get('base') or {}).get('valuation') or {}
                    check_params = dict(params)
                    check_params['terminal_value_share'] = base_valuation.get('terminal_value_share')
                    out['terminal_policy_checks'] = valuation_policy.terminal_growth_check(
                        check_params,
                        risk_free_rate=(params.get('risk_free_rate')
                                        or (options.get('market') or {}).get('risk_free_rate')
                                        or (out.get('discount_rate_detail') or {}).get('risk_free_rate')),
                        explicit_growth_path=driver_paths(params, forecast, params).get('revenue_growth'))
                    out['warnings'].extend(out['terminal_policy_checks'].get('notes') or [])
                except (ValueError, TypeError, AttributeError, InvalidOperation, KeyError) as error:
                    out['terminal_policy_checks'] = {'ok': False, 'reason': str(error)}
            vals = sorted(s['valuation']['equity_value'] for s in out['scenarios'].values() if s['valuation'].get('ok'))
            out['ok'] = out['scenarios'].get('base', {}).get('valuation', {}).get('ok', False)
            # model_plan 里的这两个字段必须反映**实际**跑了什么，不能写死。
            # 此前硬编码 balanced_three_statements=False，即使三表已经生成并通过
            # 全部勾稽，输出仍声称"没有平衡三表" —— 报告引用这个字段就会自相矛盾。
            base_statements = (out['scenarios'].get('base') or {}).get('three_statements') or {}
            if out.get('model_plan'):
                out['model_plan']['balanced_three_statements'] = bool(base_statements.get('ok'))
                out['model_plan']['forecast_type'] = (
                    'operating_driver_forecast_with_balanced_three_statements'
                    if base_statements.get('ok') else 'operating_driver_forecast')
                out['model_plan']['three_statement_checks'] = base_statements.get('checks')
            # ---- 估值方法三角 ----
            # 把 DCF、相对估值、资产基础法汇总成加权区间并诊断分歧。
            # 单看 DCF 一个数容易让读者误以为结论是确定的；三个方法摆在
            # 一起才能看出"结论对方法选择有多敏感"。离散度过大时明确
            # 提示"呈现区间而非单点"，而不是取中位数了事。
            try:
                from . import valuation_policy
                base_val = (out['scenarios'].get('base') or {}).get('valuation') or {}
                methods = {}
                if base_val.get('ok') and base_val.get('equity_value'):
                    methods['residual_income' if financial else 'dcf'] = {'ok': True, 'equity_value': base_val['equity_value'],
                                      'enterprise_value': base_val.get('enterprise_value')}
                book_equity = None
                for fact in verified:
                    if (fact.get('code') in ('parent_equity', 'equity')
                            and (not selected_period or str(fact.get('period')) == selected_period)):
                        book_equity = number(fact['value'], 'book_equity')
                if book_equity and book_equity > 0:
                    methods['asset_based'] = {
                        'ok': True, 'equity_value': book_equity, 'basis': 'book_value_reference',
                        'note': '归母净资产账面值仅作会计参照，不代表公允价值、清算价值或价值下限。'}
                # 相对估值：需要使用者提供可比倍数（外部数据，本引擎不臆造）
                multiples = {}
                multiple_ev = _d(options.get('comparable_ev_ebitda'))
                multiple_pe = _d(options.get('comparable_pe'))
                if not financial and multiple_ev and multiple_ev > 0 and params.get('ebitda_base'):
                    multiples['ev_ebitda'] = (multiple_ev * params['ebitda_base']
                                              - params['net_debt'] - params['minority_interest'] + params['non_operating_assets'])
                if multiple_pe and multiple_pe > 0 and params.get('parent_net_profit_base'):
                    multiples['pe'] = multiple_pe * params['parent_net_profit_base']
                if multiples:
                    values = sorted(v for v in multiples.values() if v and v > 0)
                    if values:
                        methods['comparables'] = {
                            'ok': True,
                            'equity_value': values[len(values) // 2],
                            'multiples': {k: serial(v) for k, v in multiples.items()},
                            'note': '按使用者提供的可比倍数反推；倍数的可比组选取依据须在报告中说明。'}
                out['method_triangle'] = valuation_policy.method_triangle(
                    methods, shares=params.get('shares'))
                if out['method_triangle'].get('dispersion_alert'):
                    out['warnings'].append(out['method_triangle']['dispersion_note'])
            except (ValueError, TypeError, AttributeError, InvalidOperation, KeyError) as error:
                out['method_triangle'] = {'ok': False, 'reason': str(error)}
            out['data_quality'] = {'from_company_data': sorted(calibration['provenance']),
                                   'from_default_assumptions': sorted(k for k in params if k not in calibration['provenance'] and k not in overrides and not k.startswith('_')),
                                   'overridden_by_user': sorted(overrides),
                                   'status': 'illustrative' if out.get('illustrative') else 'assumption_based',
                                   'reporting_period': selected_period, 'reporting_scope': selected_scope}
            if vals:
                out['range'] = {'equity_value_low': vals[0], 'equity_value_high': vals[-1],
                                'equity_value_mid': vals[len(vals) // 2],
                                'value_per_share_low': vals[0] / params['shares'] if params.get('shares') else None,
                                'value_per_share_high': vals[-1] / params['shares'] if params.get('shares') else None}
            for s in out['scenarios'].values():
                share = s['valuation'].get('terminal_value_share')
                if share is not None and share > D('.75'):
                    out['warnings'].append(s['label'] + '终值占比超过75%，请重点核验稳定期假设。')
            out['checks'].append({'name': 'model_validity', 'ok': out['ok'], 'detail': '已检查有限数、驱动边界、终值利差与增长再投资一致性。'})
    except (ValueError, TypeError, AttributeError, InvalidOperation, KeyError) as error:
        out['ok'] = False
        out['warnings'].append(str(error))
        out['checks'].append({'name': 'input_validation', 'ok': False, 'detail': str(error)})
        out['model_plan']['status'] = 'blocked'
        out['model_plan']['data_request'] = str(error)
    from .valuation_review import review_model
    out['valuation_review'] = review_model(out)
    return serial(out)
