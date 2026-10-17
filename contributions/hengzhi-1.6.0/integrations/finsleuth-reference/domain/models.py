from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Statement = Literal["BS", "IS", "CF"]
Scope = Literal["consolidated", "parent"]


class SourceSpan(BaseModel):
    """最小可引用证据单元：报告里每个数字都要能回到这一行。"""
    span_id: str
    doc_id: str
    page: int
    bbox: tuple[float, float, float, float] | None = None
    table_id: str | None = None
    text: str = ""


class RawTable(BaseModel):
    """解析器产出的原始表格，尚未做任何语义理解。"""
    table_id: str
    doc_id: str
    page: int
    strategy: str                     # lines | text | ocr
    confidence: float
    cells: list[list[str]]
    unit_header_raw: str | None = None
    bbox: tuple[float, float, float, float] | None = None


class FinancialFact(BaseModel):
    """标准化财务事实：value 一律是「元」，scale 记录从哪里换算来的。"""
    fact_id: str
    doc_id: str
    code: str | None = None           # 标准科目代码；None = 未映射
    raw_label: str                    # 报表原文标签
    statement: Statement | None = None
    period: str                       # 2024-12-31
    value: Decimal                    # 单位：元
    scale: int = 0                    # value = 表格数字 * 10**scale
    unit_raw: str | None = None
    scope: Scope = "consolidated"
    page: int = 0
    table_id: str = ""
    span_id: str = ""
    confidence: float = 1.0
    match_level: str | None = None    # exact | normalized | pattern | fuzzy
    suspect: bool = False             # 被勾稽反查标记为可疑
    note: str | None = None


class CheckResult(BaseModel):
    """一条勾稽测试的结果。失败时不只报错，还要指出可疑 fact。"""
    rule_id: str
    name: str
    period: str
    passed: bool
    lhs: Decimal
    rhs: Decimal
    diff: Decimal
    tolerance: Decimal
    fact_ids: list[str] = Field(default_factory=list)
    suspects: list[str] = Field(default_factory=list)
    detail: str = ""


class RoutePlan(BaseModel):
    """解析器路由决策，必须记录下来（可解释 + 可复现）。"""
    doc_id: str
    pages: int
    text_pages: list[int] = Field(default_factory=list)
    scanned_pages: list[int] = Field(default_factory=list)
    decision: Literal["text", "ocr", "mixed"] = "text"
    reason: str = ""
