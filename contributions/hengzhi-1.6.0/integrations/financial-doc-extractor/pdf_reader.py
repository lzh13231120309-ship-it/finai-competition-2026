"""Local PDF parsing with page-level diagnostics and optional OCR."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import pdfplumber
import pymupdf
from engine import parse_page_range, prepare_table

MAX_BYTES=100*1024*1024
MAX_PAGES=300


def ocr_page(page,language,backend):
    if backend in {'auto','tesseract'}:
        try:
            tp=page.get_textpage_ocr(language=language,dpi=200,full=True)
            words=page.get_text('words',textpage=tp,sort=True)
            blocks=[{'text':w[4],'box':[w[0]/page.rect.width,w[1]/page.rect.height,w[2]/page.rect.width,w[3]/page.rect.height],'confidence':None} for w in words]
            return page.get_text('text',textpage=tp,sort=True),'tesseract',blocks
        except Exception as exc:
            if backend=='tesseract':raise RuntimeError('Tesseract OCR 不可用，请配置对应语言 tessdata') from exc
    if backend in {'auto','vision'} and sys.platform=='darwin' and shutil.which('swift'):
        with tempfile.TemporaryDirectory(prefix='financial-ocr-') as d:
            image=Path(d)/'page.png'
            page.get_pixmap(matrix=pymupdf.Matrix(2,2),alpha=False).save(image)
            cache=Path(tempfile.gettempdir())/'financial-doc-swift-cache'
            cache.mkdir(mode=0o700,exist_ok=True)
            run=subprocess.run(['swift','-module-cache-path',str(cache),str(Path(__file__).with_name('ocr_vision.swift')),str(image)],capture_output=True,text=True,timeout=90)
            if run.returncode:raise RuntimeError('Apple Vision OCR 执行失败：'+run.stderr[-300:])
            blocks=json.loads(run.stdout)
            blocks.sort(key=lambda b:(round(b['box'][1],2),b['box'][0]))
            return '\n'.join(b['text'] for b in blocks),'apple_vision',blocks
    raise RuntimeError('OCR 不可用：安装 Tesseract 语言包，或在 macOS 上使用 Apple Vision/Swift')


def read_pdf(pdf_path,extract_tables=True,page_range=None,ocr_mode='auto',ocr_language='chi_sim+eng',ocr_backend='auto',table_strategy='auto',header_rows=1,password=None):
    if ocr_mode not in {'auto','off','force'}:raise ValueError('ocr_mode 必须是 auto/off/force')
    if ocr_backend not in {'auto','vision','tesseract'}:raise ValueError('ocr_backend 必须是 auto/vision/tesseract')
    if table_strategy not in {'auto','lines','text'}:raise ValueError('table_strategy 必须是 auto/lines/text')
    if isinstance(header_rows,bool) or not isinstance(header_rows,int) or not 1<=header_rows<=5:raise ValueError('header_rows 必须为 1–5 的整数')
    path=Path(pdf_path).expanduser().resolve()
    allowed=os.environ.get('FINANCIAL_DOC_ALLOWED_DIRS')
    if allowed:
        roots=[Path(x).resolve() for x in json.loads(allowed)]
        if not any(path.is_relative_to(root) for root in roots):
            raise ValueError('文件不在当前项目允许的上传或产物目录内')
    if not path.is_file():raise ValueError(f'文件不存在或不是普通文件: {path}')
    if path.stat().st_size>MAX_BYTES:raise ValueError('PDF 超过 100 MiB，请先拆分')
    content=path.read_bytes()
    result={'file':str(path),'sha256':hashlib.sha256(content).hexdigest(),'pages':0,'text':'','tables':[],'page_details':[],'warnings':[]}
    with pymupdf.open(stream=content,filetype='pdf') as doc:
        if doc.needs_pass and (not password or not doc.authenticate(password)):raise ValueError('PDF 已加密，需要正确密码')
        result['pages']=len(doc)
        selected=parse_page_range(page_range,len(doc))
        if len(selected)>MAX_PAGES:raise ValueError('一次最多处理 300 页，请使用 page_range 分批提取')
        result['selected_pages']=[i+1 for i in selected]
        # Decrypt in memory only for the independent table parser.
        table_bytes=doc.tobytes() if doc.is_encrypted or password else content
        with pdfplumber.open(io.BytesIO(table_bytes),password=password) as plumber:
            for i in selected:
                page=doc[i];text=page.get_text('text',sort=True);source='native';blocks=[];warnings=[]
                if ocr_mode=='force' or (ocr_mode=='auto' and len(''.join(text.split()))<20):
                    try:
                        recognized,backend,blocks=ocr_page(page,ocr_language,ocr_backend)
                        if recognized.strip():text=recognized;source=backend
                        else:warnings.append('OCR 未识别到文本')
                    except Exception as exc:warnings.append(str(exc))
                status='ok' if text.strip() else 'empty'
                if not text.strip():warnings.append('页面没有可用文本，不能视为提取成功')
                page_tables=[]
                if extract_tables and source=='native':
                    try:
                        pp=plumber.pages[i].dedupe_chars()
                        settings={} if table_strategy!='text' else {'vertical_strategy':'text','horizontal_strategy':'text'}
                        found=pp.find_tables(settings)
                        if not found and table_strategy=='auto':
                            found=pp.find_tables({'vertical_strategy':'text','horizontal_strategy':'text','min_words_vertical':3})
                            # Text layout can mistake prose for a table; only accept financial headers.
                            from engine import ALIASES,header_matches
                            found=[t for t in found if any(sum(header_matches(h,f) for f in ALIASES)>0 for h in (t.extract()[0] if t.extract() else []))]
                        for t in found:
                            rows=t.extract()
                            if len(rows)>header_rows and max(map(len,rows),default=0)>=2:
                                item=prepare_table(rows,i+1,len(page_tables)+1,list(t.bbox),header_rows)
                                item['source']='native';page_tables.append(item)
                    except Exception as exc:warnings.append('表格解析失败: '+str(exc))
                elif extract_tables and source!='native':
                    page_tables=ocr_tables(blocks,i+1)
                    warnings.append('扫描表格按 OCR 坐标生成候选，须结合原页图像复核' if page_tables else '扫描页未可靠重建表格；须结合原页图像复核')
                result['tables'].extend(page_tables)
                prose=text
                if page_tables and source!='native':
                    rects=[pymupdf.Rect(t['bbox']) for t in page_tables]
                    prose='\n'.join(b['text'] for b in blocks if not any(pymupdf.Rect(b['box']).intersects(r) for r in rects))
                elif page_tables:
                    rects=[pymupdf.Rect(t['bbox']) for t in page_tables]
                    prose='\n'.join(b[4] for b in page.get_text('blocks',sort=True) if len(b)>4 and isinstance(b[4],str) and not any(pymupdf.Rect(b[:4]).intersects(r) for r in rects))
                result['page_details'].append({'page':i+1,'text':text,'prose':prose,'source':source,'status':status,'ocr_blocks':blocks,'warnings':warnings})
                result['warnings'].extend(f'第 {i+1} 页: {w}' for w in warnings)
    result['text']='\n\n'.join(f'--- 第 {p["page"]} 页 ---\n{p["text"]}' for p in result['page_details'])
    result['status']='partial' if result['warnings'] else 'ok'
    return result


def ocr_tables(blocks,page_number):
    """Conservative single-line header reconstruction; always labelled a review candidate.

    Coordinates are normalized [0,1], top-left origin. Never forward-fill data.
    Complex merged headers are intentionally not inferred from OCR geometry.
    """
    from statistics import median
    from engine import ALIASES,header_matches
    blocks=[b for b in blocks if b.get('text','').strip() and len(b.get('box',[]))==4]
    if not blocks:return []
    height=median(b['box'][3]-b['box'][1] for b in blocks)
    tolerance=max(height*.55,.003)
    rows=[]
    for b in sorted(blocks,key=lambda b:((b['box'][1]+b['box'][3])/2,b['box'][0])):
        cy=(b['box'][1]+b['box'][3])/2
        if not rows or abs(cy-rows[-1][0])>tolerance:rows.append((cy,[b]))
        else:rows[-1][1].append(b)
    tables=[];cursor=0
    while cursor<len(rows):
        _,header=rows[cursor];header=sorted(header,key=lambda b:b['box'][0])
        hits=sum(any(header_matches(b['text'],f) for f in ALIASES) for b in header)
        if hits<2 or len(header)<2:
            cursor+=1;continue
        anchors=[(b['box'][0]+b['box'][2])/2 for b in header]
        bounds=[0]+[(a+b)/2 for a,b in zip(anchors,anchors[1:])]+[1]
        data=[];all_blocks=list(header);j=cursor+1;previous_y=rows[cursor][0]
        while j<len(rows):
            cy,items=rows[j]
            if cy-previous_y>height*4:break
            if sum(any(header_matches(b['text'],f) for f in ALIASES) for b in items)>=2:break
            cells=[[] for _ in header]
            for b in sorted(items,key=lambda b:b['box'][0]):
                mid=(b['box'][0]+b['box'][2])/2
                for k in range(len(header)):
                    if bounds[k]<=mid<=bounds[k+1]:cells[k].append(b);break
            if sum(bool(c) for c in cells)<2:break
            # A text observation crossing multiple columns cannot be split safely.
            if any(sum(b['box'][0]<a<b['box'][2] for a in anchors)>1 for b in items):break
            data.append([' '.join(b['text'] for b in c) for c in cells]);all_blocks.extend(items)
            previous_y=cy;j+=1
        if data:
            box=[min(b['box'][0] for b in all_blocks),min(b['box'][1] for b in all_blocks),max(b['box'][2] for b in all_blocks),max(b['box'][3] for b in all_blocks)]
            table=prepare_table([[b['text'] for b in header]]+data,page_number,len(tables)+1,box)
            table.update(source='ocr_geometry',needs_review=True,coordinate_system='normalized_top_left',ocr_blocks=all_blocks)
            tables.append(table)
        cursor=max(j,cursor+1)
    return tables
