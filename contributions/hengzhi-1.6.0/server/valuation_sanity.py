"""估值合理性闸门：把"常识"写成可复核的规则，每条都带阈值、依据、是否启发式。

设计原则
--------
- 闸门**不改结果**，只产出 pass/warn/fail 与行动建议；是否降级由 valuation_review 汇总决定。
- 每条阈值都标 heuristic：True 表示"经验阈值，不是数学定理"（评审问起时可直接答：依据是什么、
  为什么这个数、什么情况下可以突破）。heuristic=False 的只有数学上硬约束（g ≥ WACC）。
- 输入缺失 → 该闸门 status='skip'，不用默认值替代。
- 估值与市值偏离 > ±50% 时，闸门要求附上"市场隐含假设"（反向 DCF 的结果）；没有则 fail。

阈值依据（简述；详见 data/knowledge/估值常见错误清单）
------------------------------------------------------
- 终值占比 > 75%：Rosenbaum & Pearl《投资银行》及业内惯例，5 年 DCF 终值常占 60–80%，
  超过 75% 说明结论几乎只由永续假设决定。（启发式）
- 隐含 EV/EBITDA ∉ [3, 35]、P/E ∉ [5, 60]：A 股非金融企业长期分布的宽区间，越界多半是输入错误而非"便宜/昂贵"。（启发式）
- 终期 capex/D&A < 1.0：资产无法永续维持；稳态增长 g 对应 capex/D&A ≈ 1 + g×(平均资产年限/2)。（会计恒等式推论 + 启发式）
- 末 3 年增速 > 名义 GDP 上限(4%)：企业不能永远快于经济整体。（启发式，上限与 dcf_core 一致）
- WACC < 5% 或 > 15%：A 股 10 年国债 ~1.7% + β×ERP(5–7%) 的合理波动区间之外。（启发式）
- 市值偏离 ±50%：诊断阈值，非置信区间。（启发式）
"""
from __future__ import annotations

from decimal import Decimal

from .dcf_core import LONG_RUN_NOMINAL_GDP, dec, ser

D = Decimal


def _gate(gid, name, status, value, threshold, basis, message, action='', heuristic=True):
    return {'id': gid, 'name': name, 'status': status, 'value': ser(value) if isinstance(value, D) else value,
            'threshold': threshold, 'basis': basis, 'heuristic': heuristic, 'message': message, 'action': action}


def run_gates(ctx):
    """ctx 键（均可缺省）：
    tv_share, implied_ev_ebitda, implied_pe, terminal_capex_da, explicit_growth(list), terminal_growth, wacc,
    net_cash, market_cap, equity_base, market_implied(dict), minority_equity, minority_profit_share,
    minority_equity_share, enterprise_value, etr, ebit_margin_last, ebit_margin_mean, is_cyclical, normalized_ebit,
    bs_age_days, n_peers, three_statement_ok, equity_value, terminal_reinvestment, terminal_roic, spread,
    shares_inference_ok, profile.
    """
    g = []

    def has(k):
        return ctx.get(k) is not None

    if has('implied_exit_multiple') and has('peer_ev_ebitda') and dec(ctx['peer_ev_ebitda']):
        v = dec(ctx['implied_exit_multiple']) / dec(ctx['peer_ev_ebitda'])
        ok = D('0.67') <= v <= D('1.5')
        g.append(_gate('G18_EXIT_CROSS', 'Gordon 隐含退出倍数 vs 同行 EV/EBITDA', 'pass' if ok else 'warn', v, '比值 ∈ [0.67, 1.5]',
                       '终值互验：永续增长终值隐含的终年 EV/EBITDA 应与同行当前倍数同量级（启发式）',
                       f'Gordon 隐含终年 EV/EBITDA = {float(ctx["implied_exit_multiple"]):.1f}，同行中位 {float(ctx["peer_ev_ebitda"]):.1f}（比值 {float(v):.2f}）',
                       '' if ok else '检查终期 ROIC/g 是否过乐观，或同行是否真正可比；两种终值方法差异须在报告中解释'))
    if has('tv_share'):
        v = dec(ctx['tv_share'])
        g.append(_gate('G01_TV_SHARE', '终值占企业价值比例', 'warn' if v > D('0.75') else 'pass', v, '≤ 75%',
                       '惯例：5–10 年 DCF 终值占比常在 60–80%；> 75% 表示结论主要由永续假设决定',
                       f'终值占 EV {float(v):.1%}' + ('，高于 75%' if v > D('0.75') else ''),
                       '核对永续增长率/ROIC/退出倍数互验；必须展示 WACC×g 矩阵' if v > D('0.75') else ''))
    if has('implied_ev_ebitda'):
        v = dec(ctx['implied_ev_ebitda'])
        ok = D(3) <= v <= D(35)
        g.append(_gate('G02a_IMPLIED_EV_EBITDA', 'DCF 隐含 EV/EBITDA(LTM)', 'pass' if ok else 'warn', v, '[3, 35] 倍',
                       'A 股非金融企业长期分布的宽区间；越界多为输入错误（启发式）',
                       f'隐含 EV/EBITDA = {float(v):.1f} 倍', '' if ok else '先检查收入、利润率、净债务与单位，再解释公司特殊性'))
    if has('implied_pe'):
        v = dec(ctx['implied_pe'])
        ok = D(5) <= v <= D(60)
        g.append(_gate('G02b_IMPLIED_PE', 'DCF 隐含 P/E', 'pass' if ok else 'warn', v, '[5, 60] 倍',
                       'A 股主要行业 PE 常见区间（启发式）', f'隐含 P/E = {float(v):.1f} 倍',
                       '' if ok else '检查净利润口径与权益桥'))
    if has('terminal_capex_da'):
        v = dec(ctx['terminal_capex_da'])
        st = 'pass' if v >= D(1) else ('fail' if v < D('0.8') else 'warn')
        g.append(_gate('G03_CAPEX_DA', '终期资本开支 / 折旧摊销', st, v, '≥ 1.0',
                       '资产无法永续地"只折旧不更新"；稳态 capex/D&A ≈ 1 + g×资产年限/2（启发式）',
                       f'终期 capex/D&A = {float(v):.2f}', '' if st == 'pass' else '提高终期资本开支或降低折旧，避免终值虚高'))
    gr = ctx.get('explicit_growth')
    if gr:
        tail = [dec(x) for x in gr[-3:]]
        peak = max(tail)
        cap = dec(ctx.get('gdp_cap'), LONG_RUN_NOMINAL_GDP)
        ok = peak <= cap + D('0.02')
        g.append(_gate('G04_GROWTH_VS_GDP', '预测末 3 年收入增速 vs 长期名义 GDP', 'pass' if ok else 'warn', peak,
                       f'≤ 名义 GDP 上限 {cap:.0%} + 2pp 缓冲', '企业不能长期快于经济整体（启发式）',
                       f'末 3 年最高增速 {float(peak):.1%}', '' if ok else '延长预测期或加快增速衰减'))
    if has('wacc'):
        v = dec(ctx['wacc'])
        ok = D('0.05') <= v <= D('0.15')
        g.append(_gate('G05_WACC_RANGE', 'WACC 区间', 'pass' if ok else 'fail', v, '[5%, 15%]',
                       'rf(≈1.7%)+β×ERP(5–7%) 的合理波动之外（启发式）', f'WACC = {float(v):.2%}',
                       '' if ok else '复核 rf、β、ERP、债务权重'))
    if has('spread'):
        v = dec(ctx['spread'])
        g.append(_gate('G05b_SPREAD', 'WACC − g 利差', 'pass' if v >= D('0.02') else 'warn', v, '≥ 2pp',
                       '利差 <2pp 时 g 变动 1pp 可令终值变动 >30%（启发式）', f'利差 {float(v):.2%}',
                       '' if v >= D('0.02') else '降低 g 或复核 WACC'))
    if has('net_cash') and has('market_cap') and dec(ctx['market_cap']):
        nc, mc = dec(ctx['net_cash']), dec(ctx['market_cap'])
        if nc > 0:
            ratio = nc / mc
            g.append(_gate('G06_NET_CASH', '净现金占市值', 'warn' if ratio > D('0.3') else 'pass', ratio, '≤ 30% 视为常规',
                           '净现金越大，EV 与权益价值差越大；FCFF 不含利息收入，现金须在桥里单独加回（启发式阈值）',
                           f'净现金占市值 {float(ratio):.1%}',
                           '核对现金是否受限/在境外/与大股东资金池；高现金公司 EV 倍数比 PE 更可靠' if ratio > D('0.3') else ''))
    if has('minority_equity') and has('enterprise_value') and dec(ctx['enterprise_value']):
        mi, ev = dec(ctx['minority_equity']), dec(ctx['enterprise_value'])
        ratio = mi / ev if ev > 0 else None
        note = ''
        flag = False
        if ratio is not None and ratio > D('0.10'):
            flag = True
            note = f'少数股东权益占 EV {float(ratio):.1%}；'
        material = ratio is not None and ratio > D('0.02')   # 少数股东权益占 EV < 2% 时占比失配不具重要性
        if material and has('minority_profit_share') and has('minority_equity_share'):
            ps, es = dec(ctx['minority_profit_share']), dec(ctx['minority_equity_share'])
            if es and es > 0 and (ps / es > D(2) or ps / es < D('0.5')):
                flag = True
                note += f'少数股东损益占比 {float(ps):.1%} 与权益占比 {float(es):.1%} 相差 2 倍以上；'
        g.append(_gate('G07_MINORITY', '少数股东权益口径', 'warn' if flag else 'pass', ratio, '占 EV ≤ 10%，且损益占比/权益占比 ∈ [0.5, 2]',
                       '桥里按账面扣减；若少数股东盈利能力与母公司差异大，账面口径偏离市场价值（启发式）',
                       note or '少数股东权益按账面扣减，量级不大', '用少数股东损益×母公司 PE 作市场口径交叉验证' if flag else ''))
    if has('equity_base') and has('market_cap') and dec(ctx['market_cap']):
        dev = dec(ctx['equity_base']) / dec(ctx['market_cap']) - 1
        big = abs(dev) > D('0.5')
        mi = ctx.get('market_implied')
        if big and not mi:
            st, msg = 'fail', f'基准估值偏离市值 {float(dev):+.0%}，但未提供市场隐含假设'
        elif big:
            st, msg = 'warn', f'基准估值偏离市值 {float(dev):+.0%}；已附市场隐含假设（见 market_implied）'
        else:
            st, msg = 'pass', f'基准估值偏离市值 {float(dev):+.0%}'
        g.append(_gate('G08_MARKET_DEVIATION', '估值 vs 市值偏离', st, dev, '|偏离| ≤ 50%，否则须给市场隐含假设',
                       '偏离大不等于估值错，但必须说明"市场在押注什么"（启发式诊断阈值，非置信区间）', msg,
                       '' if st == 'pass' else '解读反向 DCF：市场隐含增速/利润率相对历史是否合理'))
    if has('etr'):
        v = dec(ctx['etr'])
        ok = D('0.10') <= v <= D('0.35')
        g.append(_gate('G09_TAX', '有效税率', 'pass' if ok else 'warn', v, '[10%, 35%]',
                       'A 股法定 25%，高新 15%；越界多为一次性项目/递延所得税（启发式）', f'归一化有效税率 {float(v):.1%}',
                       '' if ok else '使用多年平均或法定税率，终期税率不宜长期 <15%'))
    if has('normalized_ebit'):
        v = dec(ctx['normalized_ebit'])
        g.append(_gate('G10_POSITIVE_EBIT', '正常化 EBIT 为正', 'pass' if v > 0 else 'fail', v, '> 0',
                       '正常化经营利润 ≤ 0 时，Gordon 终值无经济意义', '正常化 EBIT ' + ('为正' if v > 0 else '≤ 0，DCF 拒绝'),
                       '' if v > 0 else '改用 PS/EV-Sales/PB，或由使用者显式给出扭亏路径'))
    if ctx.get('is_cyclical') and has('ebit_margin_last') and has('ebit_margin_mean') and dec(ctx['ebit_margin_mean']):
        last, mean = dec(ctx['ebit_margin_last']), dec(ctx['ebit_margin_mean'])
        peak = last > mean * D('1.3')
        g.append(_gate('G11_PEAK', '周期股峰值外推', 'warn' if peak else 'pass', last / mean, '当年 EBIT 率 ≤ 5 年均值 × 1.3',
                       '周期股当年盈利处于峰值时外推会高估（启发式）',
                       f'当年 EBIT 率 {float(last):.1%} vs 5 年均值 {float(mean):.1%}', '本模型已用均值回归；请核对正常化假设' if peak else ''))
    if has('bs_age_days'):
        v = int(ctx['bs_age_days'])
        g.append(_gate('G12_STALE', '报表日距估值日', 'warn' if v > 270 else 'pass', v, '≤ 270 天',
                       '三季报之后年报数据已显著过时（启发式）', f'报表日距估值日 {v} 天',
                       '用 TTM 或最新季报更新基期' if v > 270 else ''))
    if has('n_peers'):
        v = int(ctx['n_peers'])
        g.append(_gate('G13_PEERS', '可比公司数量', 'pass' if v >= 5 else ('warn' if v >= 3 else 'fail'), v, '≥ 5（最少 3）',
                       '统计分位数需要足够样本（启发式）', f'同行 {v} 家', '' if v >= 5 else '放宽行业口径或手动补充同行'))
    if has('three_statement_ok'):
        ok = bool(ctx['three_statement_ok'])
        g.append(_gate('G14_TIE_OUT', '三表勾稽', 'pass' if ok else 'fail', ok, '全部通过', '会计恒等式（硬约束）',
                       '三表勾稽' + ('通过' if ok else '未通过'), '' if ok else '定位残差来源后再用估值', heuristic=False))
    if has('equity_value'):
        v = dec(ctx['equity_value'])
        g.append(_gate('G15_POSITIVE_EQUITY', '权益价值为正', 'pass' if v > 0 else 'fail', v, '> 0',
                       '普通股有限责任：权益价值不为负（硬约束）', '权益价值' + ('为正' if v > 0 else '≤ 0'),
                       '' if v > 0 else '检查桥项（债务/少数股东）与现金流', heuristic=False))
    if has('terminal_reinvestment'):
        v = dec(ctx['terminal_reinvestment'])
        g.append(_gate('G16_REINVEST', '终值再投资率 g/ROIC', 'pass' if v < 1 else 'fail', v, '< 100%',
                       '再投资率 ≥ 100% 即每赚一元要投超过一元，不可持续（数学含义）', f'g/ROIC = {float(v):.1%}',
                       '', heuristic=False))
    if ctx.get('shares_inference_ok') is False:
        g.append(_gate('G17_SHARES', '股数推断', 'warn', None, '面值交叉验证通过', '股本÷面值须与 NP/EPS 一致', '股数无法交叉验证',
                       '提供公告总股本与来源'))
    order = {'fail': 0, 'warn': 1, 'pass': 2}
    summary = {'fail': sum(1 for x in g if x['status'] == 'fail'), 'warn': sum(1 for x in g if x['status'] == 'warn'),
               'pass': sum(1 for x in g if x['status'] == 'pass'), 'total': len(g)}
    return {'gates': sorted(g, key=lambda x: order[x['status']]), 'summary': summary,
            'status': 'fail' if summary['fail'] else ('warn' if summary['warn'] else 'pass')}
