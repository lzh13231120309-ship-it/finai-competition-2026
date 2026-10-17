"""端到端流水线：路由 -> 抽表 -> 归一化 -> 落事实 -> 勾稽反查 -> 修正 -> 出账本。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from domain.models import CheckResult, FinancialFact, RawTable
from tools.normalize.mapper import load_subjects, match_subject
from tools.normalize.units import detect_declared_unit, magnitude_outliers, parse_cell_value
from tools.parse.tables import extract_tables
from tools.validate.tieout import diagnose, run_tieouts

DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
MIN_TABLE_CONFIDENCE = 0.50


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def detect_period_columns(
    table: RawTable, period_end: str, prev_period_end: str
) -> tuple[dict[int, str], int]:
    """从表头识别每一列代表哪个期间，同时返回表头所在行号（表头行本身不是数据）。"""
    for r, row in enumerate(table.cells[:3]):
        mapping: dict[int, str] = {}
        for j, cell in enumerate(row):
            if not cell:
                continue
            m = DATE_RE.search(cell)
            if m:
                mapping[j] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            elif re.search(r"期末|年末|本年", cell):
                mapping[j] = period_end
            elif re.search(r"期初|年初|上年", cell):
                mapping[j] = prev_period_end
        if len(mapping) >= 2:
            return mapping, r
    return {}, -1


def _row_is_label_only(row: list[str]) -> bool:
    body = [c for c in row[1:] if c]
    return not body


def build_facts(
    tables: list[RawTable],
    subjects,
    doc_id: str,
    period_end: str,
    prev_period_end: str,
) -> tuple[list[FinancialFact], dict]:
    facts: list[FinancialFact] = []
    unmapped: set[str] = set()
    blocked: list[dict] = []
    stats = {"match_levels": {}, "unit_declared": {}, "low_confidence_tables": []}

    for table in tables:
        if table.confidence < MIN_TABLE_CONFIDENCE:
            blocked.append({"table_id": table.table_id, "confidence": table.confidence, "reason": "低于置信度阈值"})
            continue
        declared = detect_declared_unit(table.cells)
        if declared is None and table.unit_header_raw:
            from tools.normalize.units import parse_unit_scale

            declared = parse_unit_scale(table.unit_header_raw)
        unit_exp, unit_raw, unit_note = 0, None, None
        if declared:
            unit_exp, unit_raw = declared
        else:
            unit_note = "整表未声明单位，暂按元存入，需人工确认"
            stats["low_confidence_tables"].append(table.table_id)

        period_map, header_row = detect_period_columns(table, period_end, prev_period_end)
        stats["unit_declared"][table.table_id] = {"scale": unit_exp, "unit": unit_raw, "note": unit_note}
        if header_row < 0:
            blocked.append({"table_id": table.table_id, "confidence": table.confidence,
                            "reason": "未识别到期间列，整表转人工复核"})
            continue

        for r, row in enumerate(table.cells):
            if r <= header_row:          # 表头行既不是数据，也不该进未映射清单
                continue
            if not row or not row[0] or _row_is_label_only(row):
                continue
            label = row[0]
            match = match_subject(label, subjects)
            if match.spec is None:
                unmapped.add(label)
                continue
            stats["match_levels"][match.level] = stats["match_levels"].get(match.level, 0) + 1
            for j, period in period_map.items():
                if j >= len(row) or not row[j]:
                    continue
                value, cell_scale, note = parse_cell_value(row[j])
                if value is None:
                    continue
                scale = cell_scale if cell_scale is not None else unit_exp
                value_std = value * (Decimal(10) ** scale)
                conf = table.confidence * match.confidence
                if unit_note:
                    conf *= 0.5
                facts.append(
                    FinancialFact(
                        fact_id=f"{doc_id}:{match.spec.code}:{period}",
                        doc_id=doc_id,
                        code=match.spec.code,
                        raw_label=label,
                        statement=match.spec.statement,
                        period=period,
                        value=value_std,
                        scale=scale,
                        unit_raw=unit_raw,
                        page=table.page,
                        table_id=table.table_id,
                        span_id=f"{table.table_id}:r{r}:c{j}",
                        confidence=round(conf, 3),
                        match_level=match.level,
                        note=note or unit_note,
                    )
                )

    stats["unmapped_labels"] = sorted(unmapped)
    stats["blocked_tables"] = blocked
    stats["facts"] = len(facts)
    if stats["match_levels"]:
        total = sum(stats["match_levels"].values())
        stats["map_coverage"] = round(sum(v for k, v in stats["match_levels"].items() if k in {"exact", "normalized", "pattern"}) / total, 3)
    return facts, stats


def apply_corrections(facts: list[FinancialFact], results: list[CheckResult]) -> list[dict]:
    """只接受「单变量、唯一解」的修正；有歧义的一律转人工复核。"""
    log: list[dict] = []
    for res in results:
        if res.passed:
            continue
        hyps = diagnose(facts, res)
        if not hyps:
            log.append({"rule_id": res.rule_id, "period": res.period, "status": "unresolved",
                        "detail": "单变量替换搜索无解，转人工复核"})
            continue
        targets = {h.fact_id for h in hyps}
        if len(targets) > 1:
            log.append({"rule_id": res.rule_id, "period": res.period, "status": "ambiguous",
                        "candidates": [h.fact_id for h in hyps]})
            continue
        h = hyps[0]
        for i, f in enumerate(facts):
            if f.fact_id == h.fact_id:
                facts[i] = f.model_copy(update={
                    "value": h.value_after,
                    "scale": h.to_scale,
                    "suspect": True,
                    "confidence": round(f.confidence * 0.6, 3),
                    "note": (f.note or "") + f" | 勾稽反查：{h.from_scale}->{h.to_scale} 单位错位已修正，待人工确认",
                })
        log.append({
            "rule_id": res.rule_id, "period": res.period, "status": "fixed",
            "fact_id": h.fact_id, "code": h.code,
            "from_scale": h.from_scale, "to_scale": h.to_scale,
            "value_before": str(h.value_before), "value_after": str(h.value_after),
            "residual_after": str(h.residual_after),
        })
    return log


def ingest(
    pdf_path: str | Path,
    subjects_path: str | Path,
    doc_id: str,
    period_end: str,
    prev_period_end: str,
    out_dir: str | Path,
) -> dict:
    pdf_path, out_dir = Path(pdf_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tables, plan = extract_tables(pdf_path, doc_id)
    subjects = load_subjects(subjects_path)
    facts, stats = build_facts(tables, subjects, doc_id, period_end, prev_period_end)

    periods = sorted({f.period for f in facts})

    # 量级异常检测先跑：它是独立于勾稽的第二条线索，两者互相印证才敢改数
    outliers = [
        {"fact_id": facts[i].fact_id, "code": facts[i].code, "value": str(facts[i].value)}
        for i in magnitude_outliers([f.value for f in facts])
    ]

    checks_before = run_tieouts(facts, periods)
    corrections = apply_corrections(facts, checks_before)
    checks_after = run_tieouts(facts, periods)

    (out_dir / "tables.json").write_text(
        json.dumps([t.model_dump(mode="json") for t in tables], ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "facts.json").write_text(
        json.dumps([f.model_dump(mode="json") for f in facts], ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "checks_before.json").write_text(
        json.dumps([c.model_dump(mode="json") for c in checks_before], ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "checks_after.json").write_text(
        json.dumps([c.model_dump(mode="json") for c in checks_after], ensure_ascii=False, indent=2), encoding="utf-8")

    repro = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "doc_id": doc_id,
        "source_pdf_sha256": _sha256(pdf_path),
        "subjects_dict_sha256": _sha256(Path(subjects_path)),
        "route_plan": plan.model_dump(mode="json"),
        "periods": periods,
        "artifacts": {
            name: _sha256(out_dir / name)
            for name in ("tables.json", "facts.json", "checks_before.json", "checks_after.json")
        },
        "stats": stats,
    }
    (out_dir / "repro.json").write_text(json.dumps(repro, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "plan": plan,
        "tables": tables,
        "facts": facts,
        "stats": stats,
        "checks_before": checks_before,
        "checks_after": checks_after,
        "corrections": corrections,
        "magnitude_outliers": outliers,
        "out_dir": out_dir,
    }
