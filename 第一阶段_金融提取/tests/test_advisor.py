from pathlib import Path
import pytest
from finance_extract.advisor import inspect_file, selected_pages, review_guidance, classify

ROOT=Path(__file__).resolve().parents[1]
def test_native_routes_without_ocr():
    p=inspect_file(ROOT/'samples/public/moutai_2024_summary.pdf','4')
    assert p['tier']=='flash' and p['ocr']=='txt'
    assert p['sampled_pages'][0]['page']==4
    assert any(x['type']=='financial' for x in p['profiles'])
def test_image_requires_ocr():
    p=inspect_file(ROOT/'samples/derived/moutai_table_scan.png')
    assert p['tier']=='basic' and p['ocr']=='ocr'
@pytest.mark.parametrize('pages',['0','3-2','99'])
def test_invalid_ranges_rejected(pages):
    with pytest.raises(ValueError):selected_pages(7,pages)
def test_sampling_bound_and_transparency():
    assert len(selected_pages(500))<=12
    assert selected_pages(500)[-1]==499
def test_classification_preserves_unknown():
    assert classify('与金融无关的材料')[0]['type']=='unknown'
def test_priorities_are_not_confidence_scores():
    r={'fields':[{'name':'revenue','review_reasons':['unit_unknown'],'evidence':{'page':2}}], 'events':[], 'blocks':[]}
    d=review_guidance(r,{'mode':'auto'})
    assert d['review_queue'][0]['priority']=='优先核对'
    assert '概率' in d['confidence_note'] and 'confidence' not in d
