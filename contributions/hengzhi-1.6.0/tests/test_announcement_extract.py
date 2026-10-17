"""公告机械解析的回归测试。

用例直接取自竞赛选题①测试题 T1–T9 的陷阱设计（见
`桌面/人工智能金融大赛/数据结构化提取/结构化提取测试题/`），
所以这些断言同时也是"我们能否答对那套题"的可复现证据。
"""
import unittest
from decimal import Decimal

from server import announcement_extract as ax


class CnNumberTests(unittest.TestCase):
    def test_basic_units(self):
        self.assertEqual(ax.cn_number('壹亿贰仟伍佰万'), 125000000)
        self.assertEqual(ax.cn_number('一亿零五万'), 100050000)
        self.assertEqual(ax.cn_number('十二万三千四百五十六'), 123456)
        self.assertEqual(ax.cn_number('三千'), 3000)

    def test_invalid(self):
        self.assertIsNone(ax.cn_number(''))
        self.assertIsNone(ax.cn_number('abc'))


class MoneyTests(unittest.TestCase):
    def test_uppercase_chinese_amount(self):
        # T2：壹亿贰仟伍佰万元整
        self.assertEqual(ax.money_yuan('人民币壹亿贰仟伍佰万元整'), Decimal(125000000))

    def test_currency_symbol(self):
        self.assertEqual(ax.money_yuan('¥125,000,000.00'), Decimal('125000000.00'))

    def test_wan_conversion(self):
        # T6：万元必须换算成元
        self.assertEqual(ax.money_yuan('人民币 5,000 万元'), Decimal(50000000))
        self.assertEqual(ax.money_yuan('由 12,000 万元变更为 17,000 万元'), Decimal(120000000))
        self.assertEqual(ax.money_yuan('1,250.36 万元'), Decimal(12503600))

    def test_keeps_precision_below_one_wan(self):
        # 0.36 万 = 3,600 元，四舍五入成 12,500,000 是错的
        self.assertEqual(ax.money_yuan('1,250.36 万元'), Decimal('12503600.00'))

    def test_plain_yuan(self):
        self.assertEqual(ax.money_yuan('88,567,500.00 元（含税）'), Decimal('88567500.00'))

    def test_section_number_is_not_money(self):
        # 实测踩过：T7 的"一、""二、"被解析成 1 元、2 元
        self.assertIsNone(ax.money_yuan('一、本次司法冻结基本情况'))
        self.assertIsNone(ax.money_yuan('二、其他情况说明'))
        self.assertIsNone(ax.money_yuan('三、收到政府补助'))


class ShareTests(unittest.TestCase):
    def test_shares(self):
        self.assertEqual(ax.shares('18,750,000 股'), Decimal(18750000))
        self.assertEqual(ax.shares('45,000,000 股'), Decimal(45000000))

    def test_wan_shares(self):
        # T8：1,500 万股 = 15,000,000 股
        self.assertEqual(ax.shares('1,500 万股'), Decimal(15000000))


class DateTests(unittest.TestCase):
    def test_chinese_date(self):
        self.assertEqual(ax.iso_date('2026年9月18日'), '2026-09-18')
        self.assertEqual(ax.iso_date('2026年8月15日'), '2026-08-15')

    def test_spaced_date_from_ocr(self):
        # T9：扫描件 OCR 出"2026 年 10 月 20 日"
        self.assertEqual(ax.iso_date('2026 年 10 月 20 日'), '2026-10-20')

    def test_dotted_date(self):
        self.assertEqual(ax.iso_date('2026.9.5'), '2026-09-05')

    def test_invalid_date(self):
        self.assertIsNone(ax.iso_date('2026年13月40日'))
        self.assertIsNone(ax.iso_date('没有日期'))


class PercentTests(unittest.TestCase):
    def test_percent_sign(self):
        self.assertEqual(ax.percent('6.25%')['value'], Decimal('6.25'))
        self.assertEqual(ax.percent('6.25%')['form'], 'percent_sign')

    def test_decimal_form(self):
        # T8：表格里 0.05 表示 5%
        parsed = ax.percent('0.05')
        self.assertEqual(parsed['value'], Decimal('5.00'))
        self.assertEqual(parsed['form'], 'decimal')

    def test_explicit_decimal_form_overrides(self):
        parsed = ax.percent('20.00', decimal_form=False)
        self.assertEqual(parsed['value'], Decimal('20.00'))
        self.assertEqual(parsed['form'], 'percent_sign')


class NullAndTotalTests(unittest.TestCase):
    def test_null_symbols(self):
        for sym in ('—', '–', '-', '不适用', 'N/A'):
            self.assertTrue(ax.is_null_symbol(sym), sym)

    def test_zero_is_not_null(self):
        # T4：刘小燕累计质押为 0，必须输出 0，不是 null
        self.assertFalse(ax.is_null_symbol('0'))
        self.assertFalse(ax.is_null_symbol('0.00'))

    def test_total_row_plain(self):
        self.assertTrue(ax.is_total_row('合  计'))
        self.assertTrue(ax.is_total_row('总计'))
        self.assertTrue(ax.is_total_row('小计'))
        self.assertTrue(ax.is_total_row('Total'))

    def test_total_row_markdown(self):
        # T4 的合计行是 markdown 表格行
        self.assertTrue(ax.is_total_row('| 合计 | 53,000,000 | 7,400,000 | — |'))
        self.assertFalse(ax.is_total_row('| 陈志明 | 25,000,000 | 5,000,000 |'))
        self.assertFalse(ax.is_total_row('| 恒信贸易有限公司 | 18,000,000 | — |'))


class UndisclosedTests(unittest.TestCase):
    def test_court_phrase(self):
        # T7：执行法院公告未披露，必须填 null
        text = '执行法院及案件具体情况以相关司法机关出具的文书为准，公司将持续关注进展。'
        signals = ax.undisclosed_signals(text)
        self.assertTrue(signals)
        self.assertTrue(any('文书为准' in s['phrase'] for s in signals))

    def test_negation_signal(self):
        text = '本次冻结不存在被冻结股份数量超过其所持公司股份数量 50% 的情形。'
        self.assertTrue(ax.negation_signals(text, '超过'))

    def test_no_false_positive(self):
        self.assertEqual(ax.undisclosed_signals('公司总股本 300,000,000 股。'), [])


class FieldAliasTests(unittest.TestCase):
    def test_ocr_typo(self):
        # T5：OCR 备注已声明"质押权人"为笔误
        self.assertEqual(ax.normalize_field_name('质押权人'), '质权人')

    def test_unknown_passthrough(self):
        self.assertEqual(ax.normalize_field_name('出质人'), '出质人')


class TableTests(unittest.TestCase):
    TABLE = (
        '| 股东名称 | 持股总数（股） | 本次质押数量（股） | 本次质押占其所持股份比例 | 累计质押数量（股） | 累计质押占公司总股本比例 |\n'
        '|---|---|---|---|---|---|\n'
        '| 陈志明 | 25,000,000 | 5,000,000 | 20.00% | 12,000,000 | 4.00% |\n'
        '| 恒信贸易有限公司 | 18,000,000 | — | — | 9,500,000 | 3.17% |\n'
        '| 刘小燕 | 10,000,000 | 2,400,000 | 24.00% | 0 | 0.00% |\n'
        '| 合计 | 53,000,000 | 7,400,000 | — | 21,500,000 | — |\n'
    )

    def test_rows_and_total(self):
        rows = ax.parse_table_rows(self.TABLE)
        data = [r for r in rows if not r['is_total']]
        totals = [r for r in rows if r['is_total']]
        self.assertEqual(len(data), 4)      # 表头 + 3 名股东
        self.assertEqual(len(totals), 1)

    def test_null_cells(self):
        rows = ax.parse_table_rows(self.TABLE)
        hengxin = [r for r in rows if r['cells'][0] == '恒信贸易有限公司'][0]
        self.assertEqual(hengxin['null_cells'], [2, 3])
        liu = [r for r in rows if r['cells'][0] == '刘小燕'][0]
        self.assertEqual(liu['null_cells'], [])   # 0 不是空值

    def test_table_percents_by_header(self):
        rows = ax.parse_table_rows(self.TABLE)
        parsed = ax.table_percents(rows)
        values = {(p['row_label'], p['raw']): p['value'] for p in parsed}
        self.assertEqual(values[('陈志明', '20.00%')], Decimal('20.00'))
        self.assertEqual(values[('刘小燕', '0.00%')], Decimal('0.00'))
        # 合计行不产出
        self.assertFalse(any(p['row_label'] == '合计' for p in parsed))

    def test_decimal_percent_in_table(self):
        table = ('| 出质人 | 质押股数（股） | 占公司总股本比例 |\n'
                 '|---|---|---|\n'
                 '| 恒兴集团有限公司 | 15,000,000 | 0.05 |\n')
        parsed = ax.table_percents(ax.parse_table_rows(table))
        self.assertEqual(parsed[0]['value'], Decimal('5.00'))
        self.assertEqual(parsed[0]['form'], 'decimal')


class ScanTests(unittest.TestCase):
    def test_scan_shape(self):
        result = ax.scan('远大控股质押 18,750,000 股，占总股本 6.25%，期限 2026年8月15日 起。')
        self.assertTrue(result['amounts'] or result['shares'])
        self.assertTrue(result['dates'])
        self.assertTrue(result['percents'])
        for key in ('amounts', 'shares', 'dates', 'percents', 'table_rows',
                    'table_percents', 'undisclosed', 'total_rows', 'null_symbols', 'note'):
            self.assertIn(key, result)

    def test_scan_does_not_invent(self):
        result = ax.scan('本公告不包含任何数字。')
        self.assertEqual(result['amounts'], [])
        self.assertEqual(result['shares'], [])
        self.assertEqual(result['dates'], [])


if __name__ == '__main__':
    unittest.main()
