"""单位识别与换算。

三条铁律：
1. 表头声明的单位（"单位：万元"）只作用于该表；跨表引用必须重新判定。
2. 单元格自带单位（"1,234.56万元"）优先于表头声明。
3. 换算前的原始数字与所用 scale 都要落库，否则结果不可复现、不可审计。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from statistics import median

# 顺序敏感：长的单位必须排在前面，否则 "百万元" 会被 "万元" 抢先匹配。
_UNITS: tuple[tuple[str, int], ...] = (
    ("亿元", 8),
    ("百万元", 6),
    ("千元", 3),
    ("万元", 4),
    ("元", 0),
)
_UNIT_RE = re.compile("|".join(u for u, _ in _UNITS))
# 必须先认「单位：X」这种声明式写法，否则 "编制单位：XX公司 金额单位：万元"
# 里的第一个「元」来自「单位」二字，会把万元表误判成元表。
_UNIT_DECL_RE = re.compile(r"单位\s*[:：]\s*(?:人民币)?\s*(" + "|".join(u for u, _ in _UNITS) + r")")

_MISSING_TOKENS = {"", "-", "—", "–", "－", "/", "不适用", "无", "未披露", "n/a", "na", "null"}
_ZERO_TOKENS = {"-", "—", "–", "－", "/"}
_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def _halfwidth(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def parse_unit_scale(text: str) -> tuple[int, str] | None:
    """从一段文字里找出单位，返回 (10的幂, 单位原文)。

    两级优先：先找 "单位：万元" 这种显式声明，再退回找独立单位词。
    """
    if not text:
        return None
    half = _halfwidth(text)
    m = _UNIT_DECL_RE.search(half) or _UNIT_RE.search(half)
    if not m:
        return None
    unit = m.group(1) if m.re is _UNIT_DECL_RE else m.group(0)
    for name, exp in _UNITS:
        if name == unit:
            return exp, unit
    return None


def detect_declared_unit(rows: list[list[str]]) -> tuple[int, str] | None:
    """在前几行里找整表单位声明，如 "单位：人民币万元"。"""
    for row in rows[:3]:
        for cell in row:
            if not cell:
                continue
            if re.search(r"单位\s*[:：]", _halfwidth(cell)):
                found = parse_unit_scale(cell)
                if found:
                    return found
    return None


def parse_cell_value(text: str) -> tuple[Decimal | None, int | None, str | None]:
    """解析单元格 -> (原始数字, 单元格自带 scale, 备注)。

    处理：千分位、括号负数、破折号表示 0、不适用表示缺失、单元格内单位。
    """
    if text is None:
        return None, None, "empty"
    raw = _halfwidth(str(text)).strip()
    compact = raw.replace(" ", "").replace(",", "").replace("，", "")
    if compact.lower() in _MISSING_TOKENS:
        if compact in _ZERO_TOKENS:
            return Decimal(0), None, "dash_as_zero"
        return None, None, "missing_token"

    neg = False
    if (compact.startswith("(") and compact.endswith(")")) or (
        compact.startswith("（") and compact.endswith("）")
    ):
        neg = True
        compact = compact[1:-1]

    unit = parse_unit_scale(compact)
    cell_scale = unit[0] if unit else None
    body = _UNIT_RE.sub("", compact)

    m = _NUM_RE.search(body)
    if not m:
        return None, None, "no_number"
    try:
        value = Decimal(m.group(0).replace(",", ""))
    except InvalidOperation:
        return None, None, "parse_error"
    if neg:
        value = -value
    return value, cell_scale, None


def magnitude_outliers(values: list[Decimal]) -> list[int]:
    """量级异常检测：与中位数量级相差 1000 倍以上的，疑似单位错位。"""
    clean = [v for v in values if v is not None and v != 0]
    if len(clean) < 4:
        return []
    exps = sorted(v.copy_abs().adjusted() for v in clean)
    mid = median(exps)
    out = []
    for i, v in enumerate(values):
        if v is None or v == 0:
            continue
        if abs(v.copy_abs().adjusted() - mid) >= 3:
            out.append(i)
    return out
