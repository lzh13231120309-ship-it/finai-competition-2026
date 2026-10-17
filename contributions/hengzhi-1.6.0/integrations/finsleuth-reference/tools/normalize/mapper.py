"""科目别名词典匹配：级联降级，每级都留下匹配证据。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

try:  # 有 rapidfuzz 更好，没有也能跑
    from rapidfuzz import fuzz as _fuzz
except Exception:  # pragma: no cover
    _fuzz = None

import difflib

CONFIDENCE = {"exact": 1.0, "normalized": 0.95, "pattern": 0.90, "fuzzy": 0.55}

# 归一化时剥掉的噪音：附注号、"其中："、"减："、空白
_NOISE_RES = [
    re.compile(r"[（(]\s*(?:附?注|详见|见)?\s*[一二三四五六七八九十\d]{0,4}\s*[）)]"),
    re.compile(r"^\s*(?:其中|减|加|其中：|减：|加：)\s*[:：]?\s*"),
    re.compile(r"\s+"),
]
# 后缀修饰词：剥掉后可与标准名对齐
_SUFFIX_NOISE = ("净额", "账面价值", "账面余额", "合计", "总计", "小计")


@dataclass(frozen=True)
class SubjectSpec:
    code: str
    standard_name: str
    statement: str | None
    aliases: tuple[str, ...]
    patterns: tuple[str, ...]
    excludes: tuple[str, ...]


@dataclass(frozen=True)
class MatchResult:
    spec: SubjectSpec | None
    level: str
    confidence: float
    label: str


def load_subjects(path: str | Path) -> list[SubjectSpec]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [
        SubjectSpec(
            code=d["code"],
            standard_name=d["standard_name"],
            statement=d.get("statement"),
            aliases=tuple(d.get("aliases") or ()),
            patterns=tuple(d.get("patterns") or ()),
            excludes=tuple(d.get("excludes") or ()),
        )
        for d in data
    ]


def normalize_label(raw: str) -> str:
    text = str(raw or "")
    for rx in _NOISE_RES:
        text = rx.sub("", text)
    return text.strip()


def _strip_suffix(label: str) -> str:
    for suf in _SUFFIX_NOISE:
        if label.endswith(suf) and len(label) > len(suf):
            return label[: -len(suf)]
    return label


def match_subject(raw_label: str, specs: list[SubjectSpec]) -> MatchResult:
    label = normalize_label(raw_label)
    if not label:
        return MatchResult(None, "empty", 0.0, label)

    stripped = _strip_suffix(label)
    candidates: list[MatchResult] = []

    for spec in specs:
        # 1) 排除词优先：宁可漏配，也不要错配
        if any(ex and ex in label for ex in spec.excludes):
            continue
        # 2) 精确别名
        if label in spec.aliases or stripped in spec.aliases:
            level = "exact" if label in spec.aliases else "normalized"
            candidates.append(MatchResult(spec, level, CONFIDENCE[level], label))
            continue
        # 3) 前缀/正则
        hit = False
        for pat in spec.patterns:
            try:
                if re.search(pat, label):
                    candidates.append(MatchResult(spec, "pattern", CONFIDENCE["pattern"], label))
                    hit = True
                    break
            except re.error:
                continue
        if hit:
            continue
        # 4) 模糊兜底：只作为"待人工确认"的候选，置信度压低
        best = 0.0
        for alias in spec.aliases:
            if _fuzz is not None:
                score = _fuzz.ratio(label, alias)
            else:
                score = difflib.SequenceMatcher(None, label, alias).ratio() * 100
            best = max(best, score)
        if best >= 88:
            candidates.append(MatchResult(spec, "fuzzy", CONFIDENCE["fuzzy"], label))

    if not candidates:
        return MatchResult(None, "unmapped", 0.0, label)
    candidates.sort(key=lambda c: (-c.confidence, len(c.label)))
    return candidates[0]
