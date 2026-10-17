"""勾稽反查：用会计恒等式反推"哪个数字解析错了、错在单位还是错在取值"。

思路是先验错、再归因、再自动修正，而不是简单抛异常：
  1. 跑恒等式 -> 不成立
  2. 单变量替换搜索：只动一个 fact，把它按其他单位口径重新解释（元/千元/万元/百万元/亿元）
  3. 若恰好有一个 fact 能修好 -> 判为"单位错位"，自动修正 + 降置信度 + 留痕
  4. 若多个 fact 或多种改法都能修好 -> 判为歧义，转人工复核
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from domain.models import CheckResult, FinancialFact

ALT_SCALES = (0, 3, 4, 6, 8)  # 元 千元 万元 百万元 亿元
SCALE_NAME = {0: "元", 3: "千元", 4: "万元", 6: "百万元", 8: "亿元"}


@dataclass(frozen=True)
class Identity:
    rule_id: str
    name: str
    lhs_codes: tuple[str, ...]
    rhs_codes: tuple[str, ...]


@dataclass(frozen=True)
class Hypothesis:
    fact_id: str
    code: str
    from_scale: int
    to_scale: int
    value_before: Decimal
    value_after: Decimal
    residual_after: Decimal


IDENTITIES: tuple[Identity, ...] = (
    Identity("BS_BALANCE", "资产总计 = 负债合计 + 所有者权益合计", ("ASSETS_TOTAL",), ("LIAB_TOTAL", "EQUITY_TOTAL")),
)


def tolerance_for(facts: list[FinancialFact], decimals: int = 2) -> Decimal:
    """容差必须跟着「被披露的显示精度」走，否则四舍五入会造成假阳性。

    单位是元、显示两位小数 -> 半个最小单位 = 0.01 元，下限取 1 元。
    单位是万元、显示两位小数 -> 100 元。
    """
    exp = max((f.scale for f in facts), default=0)
    ulp = Decimal(10) ** (exp - decimals)
    return max(Decimal(1), ulp)


def _sum(facts: list[FinancialFact]) -> Decimal:
    return sum((f.value for f in facts), Decimal(0))


def _pick(facts: list[FinancialFact], code: str) -> list[FinancialFact]:
    return [f for f in facts if f.code == code]


def check_identity(identity: Identity, facts: list[FinancialFact], period: str) -> CheckResult | None:
    lhs = [f for c in identity.lhs_codes for f in _pick(facts, c)]
    rhs = [f for c in identity.rhs_codes for f in _pick(facts, c)]
    codes = set(identity.lhs_codes) | set(identity.rhs_codes)
    present = {f.code for f in facts if f.code in codes}
    if present != codes:
        return CheckResult(
            rule_id=identity.rule_id,
            name=identity.name,
            period=period,
            passed=False,
            lhs=Decimal(0),
            rhs=Decimal(0),
            diff=Decimal(0),
            tolerance=Decimal(0),
            fact_ids=[f.fact_id for f in lhs + rhs],
            detail=f"科目不齐，无法勾稽：缺 {sorted(codes - present)}",
        )
    lhs_v, rhs_v = _sum(lhs), _sum(rhs)
    tol = tolerance_for(lhs + rhs)
    diff = lhs_v - rhs_v
    return CheckResult(
        rule_id=identity.rule_id,
        name=identity.name,
        period=period,
        passed=abs(diff) <= tol,
        lhs=lhs_v,
        rhs=rhs_v,
        diff=diff,
        tolerance=tol,
        fact_ids=[f.fact_id for f in lhs + rhs],
    )


def run_tieouts(facts: list[FinancialFact], periods: list[str]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for period in periods:
        pf = [f for f in facts if f.period == period and f.value is not None]
        if not pf:
            continue
        for identity in IDENTITIES:
            res = check_identity(identity, pf, period)
            if res:
                results.append(res)
        results.append(check_duplicate_conflict(pf, period))
    return [r for r in results if r is not None]


def check_duplicate_conflict(facts: list[FinancialFact], period: str) -> CheckResult | None:
    """同科目同期间出现多个互相矛盾的取值（不同页/不同解析策略/附注 vs 主表）。"""
    seen: dict[str, list[FinancialFact]] = {}
    for f in facts:
        if f.code:
            seen.setdefault(f.code, []).append(f)
    for code, group in seen.items():
        if len(group) < 2:
            continue
        values = {f.value for f in group}
        if len(values) > 1:
            tol = tolerance_for(group)
            spread = max(values) - min(values)
            if spread > tol:
                return CheckResult(
                    rule_id="DUP_CONFLICT",
                    name=f"{code} 同名科目取值冲突",
                    period=period,
                    passed=False,
                    lhs=max(values),
                    rhs=min(values),
                    diff=spread,
                    tolerance=tol,
                    fact_ids=[f.fact_id for f in group],
                    detail="同一科目在同一期间出现多个不同取值，需确认口径或来源",
                )
    return None


def diagnose(facts: list[FinancialFact], result: CheckResult) -> list[Hypothesis]:
    """单变量替换搜索：只改一个 fact 的单位口径，看恒等式能否成立。"""
    if result.passed or result.rule_id == "DUP_CONFLICT":
        return []
    identity = next((i for i in IDENTITIES if i.rule_id == result.rule_id), None)
    if identity is None:
        return []

    scope = [f for f in facts if f.period == result.period and f.code in set(identity.lhs_codes) | set(identity.rhs_codes)]
    lhs_ids = {f.fact_id for c in identity.lhs_codes for f in _pick(scope, c)}
    rhs_ids = {f.fact_id for c in identity.rhs_codes for f in _pick(scope, c)}

    hypotheses: list[Hypothesis] = []
    for target in scope:
        if not target.value:
            continue
        for exp in ALT_SCALES:
            if exp == target.scale:
                continue
            new_value = target.value * (Decimal(10) ** (exp - target.scale))
            lhs_v = Decimal(0)
            rhs_v = Decimal(0)
            modified: list[FinancialFact] = []
            for f in scope:
                v = new_value if f.fact_id == target.fact_id else f.value
                modified.append(f.model_copy(update={"value": v, "scale": exp if f.fact_id == target.fact_id else f.scale}))
                if f.fact_id in lhs_ids:
                    lhs_v += v
                elif f.fact_id in rhs_ids:
                    rhs_v += v
            tol = tolerance_for(modified)
            residual = lhs_v - rhs_v
            if abs(residual) <= tol:
                hypotheses.append(
                    Hypothesis(
                        fact_id=target.fact_id,
                        code=target.code or "",
                        from_scale=target.scale,
                        to_scale=exp,
                        value_before=target.value,
                        value_after=new_value,
                        residual_after=residual,
                    )
                )
    hypotheses.sort(key=lambda h: (abs(h.to_scale - h.from_scale), abs(h.residual_after)))
    return hypotheses
