"""Parse financial quantities conservatively, preserving the literal value."""
from decimal import Decimal, InvalidOperation
import re
import unicodedata

MISSING = {'', '-', '--', '—', '–', '/', '不适用', '无', 'N/A', 'NULL'}
MULTIPLIERS = {'元': 1, '千元': 1000, '万元': 10000, '百万元': 1000000, '亿元': 100000000,
               '股': 1, '万股': 10000, '亿股': 100000000, '%': 1, '元/股': 1, '倍': 1}
UNITS = r'亿元|百万元|万元|千元|元/股|元|亿股|万股|股|%|倍'

def quantity(raw: str, unit: str | None = None) -> dict:
    original = str(raw)
    value = unicodedata.normalize('NFKC', original).strip()
    if value.upper() in MISSING:
        return {'raw': original, 'value': None, 'unit': unit, 'base_value': None, 'status': 'missing'}
    # Remove whitespace only around punctuation/digits, never guess O->0 / I->1.
    value = re.sub(r'\s+', '', value)
    explicit = re.search(rf'({UNITS})$', value)
    effective_unit = explicit.group(1) if explicit else unit
    if explicit:
        value = value[:explicit.start()]
    negative = value.startswith('(') and value.endswith(')')
    if negative:
        value = value[1:-1]
    if not re.fullmatch(r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?', value):
        return {'raw': original, 'value': None, 'unit': effective_unit, 'base_value': None, 'status': 'invalid'}
    try:
        n = Decimal(value.replace(',', '')) * (-1 if negative else 1)
    except InvalidOperation:
        return {'raw': original, 'value': None, 'unit': effective_unit, 'base_value': None, 'status': 'invalid'}
    base = n * Decimal(MULTIPLIERS[effective_unit]) if effective_unit in MULTIPLIERS else None
    return {'raw': original, 'value': format(n, 'f'), 'unit': effective_unit,
            'base_value': format(base, 'f') if base is not None else None,
            'base_unit': ('元' if effective_unit and '元' in effective_unit and '/' not in effective_unit
                          else '股' if effective_unit and '股' in effective_unit and '/' not in effective_unit
                          else effective_unit),
            'status': 'parsed' if effective_unit in MULTIPLIERS else 'unit_unknown'}

def infer_unit(text: str) -> str | None:
    # Explicit declaration or dimension in a header. Never infer currency from a company name.
    text = unicodedata.normalize('NFKC', text)
    declarations = list(re.finditer(rf'单位\s*[:：]\s*({UNITS})', text))
    if declarations:
        return declarations[-1].group(1)
    matches = list(re.finditer(rf'[（(]\s*({UNITS})\s*[）)]', text))
    return matches[-1].group(1) if matches else None
