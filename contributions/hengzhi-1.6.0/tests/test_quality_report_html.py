"""质量评估 HTML 报告渲染器测试。

关注三件事：
1. 渲染出的数字**必须等于**输入，不重算、不补全；
2. `None`（不适用）不能被渲染成 0 分；
3. 页面必须自包含（无外部资源），且对报告文本做 HTML 转义。
"""
import re

import pytest

from server import quality_report_html as qh


def _result(**over):
    base = {
        'ok': True,
        'rules_version': 'quality-v2-test',
        'total': 3.25,
        'coverage': 0.8,
        'n_review': 1,
        'scale_note': '项目内部量表（0–5），不是竞赛官方评分；未评部分不计入分母，须看覆盖率。',
        'coverage_note': '已评子指标权重占比 80%。',
        'text_stats': {'chars': 1000, 'lines': 40},
        'consistency': {'checks': 3, 'failures': 0, 'formula_checked': 2, 'formula_failed': 0},
        'dimensions': {
            'logic': {'name': '论证与逻辑', 'weight': 0.3, 'score': 4.0, 'applicable': 2, 'total_subs': 2},
            'evidence': {'name': '证据支撑', 'weight': 0.25, 'score': 2.0, 'applicable': 1, 'total_subs': 2},
        },
        'subs': [
            {'id': 'struct.completeness', 'dim': 'logic', 'name': '结构完整性', 'score': 5.0,
             'why': '章节齐全', 'hits': [{'rule': 'S.sec', 'text': '第一章', 'line': 1}],
             'counter': [], 'metrics': {'sections': 8}, 'applicable': True},
            {'id': 'arg.causal', 'dim': 'logic', 'name': '因果论证', 'score': 3.0,
             'why': '3/6 有因果连接词', 'hits': [], 'counter': [], 'metrics': {}, 'applicable': True},
            {'id': 'consist.formula', 'dim': 'evidence', 'name': '显式公式复算', 'score': None,
             'why': '报告无显式算式', 'hits': [], 'counter': [], 'metrics': {}, 'applicable': False},
            {'id': 'data.source_rate', 'dim': 'evidence', 'name': '来源标注率', 'score': 2.0,
             'why': '2/5 带来源', 'hits': [], 'counter': [], 'metrics': {}, 'applicable': True},
        ],
        'review_queue': [
            {'kind': 'low_sub', 'id': 'data.source_rate', 'dim': 'evidence', 'name': '来源标注率',
             'score': 2.0, 'why': '2/5 带来源', 'evidence': ['营业收入 100 亿元。']},
            {'kind': 'not_applicable', 'id': 'consist.formula', 'dim': 'evidence', 'name': '显式公式复算',
             'score': None, 'why': '报告无显式算式', 'evidence': []},
        ],
    }
    base.update(over)
    return base


def test_renders_self_contained_html():
    out = qh.render_quality_html(_result(), report_name='测试报告')
    assert out.startswith('<!DOCTYPE html>')
    assert out.rstrip().endswith('</html>')
    assert '测试报告' in out
    # 无任何外部资源引用（CDN / 外链脚本 / 外链样式 / 外链图片）
    assert not re.search(r'<script[^>]+src=', out)
    assert not re.search(r'<link[^>]+href=', out)
    assert not re.search(r'<img[^>]+src=', out)
    assert '@import' not in out


def test_numbers_come_from_input_not_recomputed():
    out = qh.render_quality_html(_result(), report_name='x')
    assert '3.25' in out          # total 原样
    assert '80.0%' in out         # coverage 原样
    assert 'quality-v2-test' in out   # 规则版本原样渲染
    assert '1000' in out and '40' in out   # text_stats 原样


def test_not_applicable_is_not_rendered_as_zero():
    out = qh.render_quality_html(_result(), report_name='x')
    # 不适用项必须显式写成"不适用"，且不能出现 0.00 分
    assert '不适用' in out
    assert '>0.00<' not in out
    assert 'data-applicable="false"' in out


def test_escapes_report_text():
    nasty = _result()
    nasty['subs'][0]['why'] = '<script>alert(1)</script>'
    nasty['subs'][0]['hits'][0]['text'] = '"><img src=x onerror=alert(2)>'
    out = qh.render_quality_html(nasty, report_name='<b>x</b>')
    assert '<script>alert(1)</script>' not in out
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in out
    assert '<img src=x onerror=alert(2)>' not in out


def test_rejects_failed_result():
    with pytest.raises(ValueError):
        qh.render_quality_html({'ok': False}, report_name='x')
    with pytest.raises(ValueError):
        qh.render_quality_html(None, report_name='x')


def test_discipline_statements_present():
    out = qh.render_quality_html(_result(), report_name='x')
    # 三条纪律必须出现在页面上，而不是只写在注释里
    assert '不是竞赛官方评分' in out
    assert '覆盖率' in out
    assert '长度基线' in out
    assert '未经自动评估' in out


def test_length_baseline_table_marks_known_weakness():
    out = qh.render_quality_html(_result(), report_name='x')
    # same_tier 上本模型 87.5% 低于长度基线 100%，必须被标成已知短板
    assert '已知短板' in out


def test_write_quality_html(tmp_path):
    path = qh.write_quality_html(tmp_path / 'sub' / 'a.html', _result(), report_name='x')
    assert path.is_file()
    assert path.read_text(encoding='utf-8').startswith('<!DOCTYPE html>')


def test_errorcheck_block_is_optional_and_labelled():
    with_ec = qh.render_quality_html(_result(), report_name='x',
                                     errorcheck={'summary': {'n_findings': 7, 'n_suspects': 2}})
    assert '研报纠错核查' in with_ec
    assert '待人工核验' in with_ec
    without = qh.render_quality_html(_result(), report_name='x')
    assert '研报纠错核查' not in without
