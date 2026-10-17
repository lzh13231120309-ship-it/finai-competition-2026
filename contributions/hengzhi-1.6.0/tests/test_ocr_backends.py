"""OCR 后端选择与降级路径的测试。

要点：
- 后端选择只有一处真源（`OCR_BACKENDS` / `ocr_backends()`），可用性判断不重复实现。
- `_ocr()` 逐个后端尝试，全部不可用才抛错，且错误信息要能指导修复。
- `OcrBackendUnavailable` 必须仍是 RuntimeError 子类，否则 `_parse_pdf` 的整页降级会失效。
"""
from pathlib import Path

import pytest

from server import financial_documents as fd
from server import ocr_fallback as of


def _block(text, bbox, confidence=0.9, tokens=None):
    return {'text': text, 'bbox': list(bbox), 'confidence': confidence, 'tokens': tokens or []}


def test_backend_unavailable_is_runtime_error():
    # _parse_pdf 用 `except RuntimeError` 做整页降级；子类关系断了就会让整份年报失败
    assert issubclass(fd.OcrBackendUnavailable, RuntimeError)


def test_ocr_backends_reports_every_backend_with_reason():
    rows = fd.ocr_backends()
    assert [r['backend'] for r in rows] == list(fd.OCR_BACKENDS)
    for row in rows:
        assert set(row) == {'backend', 'available', 'reason'}
        assert isinstance(row['available'], bool)
        assert row['reason']
    # 可选后端缺失时必须给出可执行的修复建议，而不是只说"不可用"
    missing = [r for r in rows if not r['available'] and r['backend'] == 'rapidocr']
    for row in missing:
        assert 'rapidocr-onnxruntime' in row['reason']


def test_ocr_available_uses_single_source_of_truth(monkeypatch):
    monkeypatch.setattr(fd, 'ocr_backends',
                        lambda: [{'backend': 'finocr', 'available': False, 'reason': '没有 finocr'},
                                 {'backend': 'rapidocr', 'available': False, 'reason': '没装 rapidocr'}])
    ok, why = of.ocr_available()
    assert ok is False and 'finocr' in why and 'rapidocr' in why

    monkeypatch.setattr(fd, 'ocr_backends',
                        lambda: [{'backend': 'finocr', 'available': True, 'reason': '使用 /x/finocr'},
                                 {'backend': 'rapidocr', 'available': False, 'reason': '没装 rapidocr'}])
    ok, why = of.ocr_available()
    assert ok is True and 'finocr' in why and 'rapidocr' not in why


def test_ocr_uses_first_available_backend_and_rescales(monkeypatch):
    calls = []

    def fake_finocr(path):
        calls.append(('finocr', Path(path).name))
        return [_block('下', [10, 40, 20, 50]), _block('上', [10, 5, 20, 15])]

    monkeypatch.setattr(fd, '_ocr_finocr', fake_finocr)
    monkeypatch.setattr(fd, '_ocr_rapidocr', lambda path: pytest.fail('不应回退到 rapidocr'))

    items = fd._ocr(Path('page.png'), scale_x=2.0, scale_y=3.0)
    assert calls == [('finocr', 'page.png')]
    # 坐标按 scale 归一，且按 y 排序（同一 y 带内按 x）
    assert [i['text'] for i in items] == ['上', '下']
    assert items[0]['bbox'] == [20.0, 15.0, 40.0, 45.0]


def test_ocr_falls_back_when_primary_backend_unavailable(monkeypatch):
    def unavailable(path):
        raise fd.OcrBackendUnavailable('没有 finocr')

    monkeypatch.setattr(fd, '_ocr_finocr', unavailable)
    monkeypatch.setattr(fd, '_ocr_rapidocr', lambda path: [_block('备用', [1, 2, 3, 4])])

    items = fd._ocr(Path('page.png'))
    assert [i['text'] for i in items] == ['备用']


def test_ocr_raises_with_actionable_message_when_no_backend(monkeypatch):
    def unavailable(name):
        def _f(path):
            raise fd.OcrBackendUnavailable(f'{name} 不可用')
        return _f

    monkeypatch.setattr(fd, '_ocr_finocr', unavailable('finocr'))
    monkeypatch.setattr(fd, '_ocr_rapidocr', unavailable('rapidocr'))
    with pytest.raises(RuntimeError) as excinfo:
        fd._ocr(Path('page.png'))
    message = str(excinfo.value)
    assert 'finocr' in message and 'rapidocr' in message
    assert 'rapidocr-onnxruntime' in message          # 给修复命令，不是只说"失败"


def test_ocr_does_not_swallow_real_backend_failure(monkeypatch):
    # 后端"跑了但失败"必须原样抛出，不能被当成"后端不可用"而静默换后端
    def broken(path):
        raise RuntimeError('离线 OCR 失败：image too large')

    monkeypatch.setattr(fd, '_ocr_finocr', broken)
    monkeypatch.setattr(fd, '_ocr_rapidocr', lambda path: pytest.fail('不应吞掉真实失败后换后端'))
    with pytest.raises(RuntimeError, match='image too large'):
        fd._ocr(Path('page.png'))


def test_rapidocr_adapter_normalizes_quad_boxes(monkeypatch):
    class FakeEngine:
        def __call__(self, path):
            return ([
                [[[10, 20], [30, 20], [30, 25], [10, 25]], '营业收入', 0.88],
                [[[5, 5], [15, 5], [15, 9], [5, 9]], '项目', 0.91],
            ], 0.1)

    monkeypatch.setattr(fd, '_rapidocr_engine', lambda: FakeEngine())
    blocks = fd._ocr_rapidocr(Path('page.png'))
    assert len(blocks) == 2
    first = [b for b in blocks if b['text'] == '营业收入'][0]
    # 四点框折叠成 [x0,y0,x1,y1]
    assert first['bbox'] == [10.0, 20.0, 30.0, 25.0]
    assert first['confidence'] == 0.88
    # 每个 block 自带一个 token，供 _spatial_tables 使用
    assert first['tokens'][0]['text'] == '营业收入'
    assert first['tokens'][0]['bbox'] == first['bbox']


def test_rapidocr_adapter_reports_missing_optional_dependency(monkeypatch):
    def boom():
        raise ImportError('No module named rapidocr_onnxruntime')

    monkeypatch.setattr(fd, '_rapidocr_engine', boom)
    with pytest.raises(fd.OcrBackendUnavailable) as excinfo:
        fd._ocr_rapidocr(Path('page.png'))
    assert 'rapidocr-onnxruntime' in str(excinfo.value)
