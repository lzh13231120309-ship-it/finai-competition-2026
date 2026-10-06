"""Evidence-first financial fields. Unsupported structures stay visible as tables."""
from pathlib import Path
from hashlib import sha256
import re
from .numbers import quantity, infer_unit
from .tables import read_tables, compact

METRICS = {
    '营业总收入': 'revenue_total', '营业收入': 'revenue', '主营业务收入': 'revenue_main',
    '归属于上市公司股东的净利润': 'net_profit_parent', '归属于母公司股东的净利润': 'net_profit_parent',
    '归属于上市公司股东的扣除非经常性损益的净利润': 'net_profit_parent_adjusted',
    '归属于上市公司股东的扣除非经常性损益后净利润': 'net_profit_parent_adjusted',
    '归属于上市公司股东的扣除非经常性损益后的净利润': 'net_profit_parent_adjusted',
    '经营活动产生的现金流量净额': 'operating_cash_flow', '经营活动产生的现金流净额': 'operating_cash_flow',
    '总资产': 'assets_total', '资产总计': 'assets_total', '负债合计': 'liabilities_total',
    '归属于上市公司股东的净资产': 'equity_parent', '归属于母公司股东权益合计': 'equity_parent',
    '基本每股收益': 'eps_basic', '稀释每股收益': 'eps_diluted', '加权平均净资产收益率': 'roe_weighted',
    '净利润': 'net_profit', '货币资金': 'cash', '营业利润': 'operating_profit',
    '营业成本':'revenue_cost','营业总成本':'cost_total','流动资产合计':'current_assets','流动负债合计':'current_liabilities',
    '应收账款':'accounts_receivable','存货':'inventory','所有者权益合计':'equity_total','股东权益合计':'equity_total',
    '所有者权益（或股东权益）合计':'equity_total','所有者权益(或股东权益)合计':'equity_total',
    '购建固定资产、无形资产和其他长期资产支付的现金':'capex',
    '短期借款':'debt_short','长期借款':'debt_long','应付债券':'bonds_payable',
    '销售费用':'selling_expense','管理费用':'admin_expense','研发费用':'rd_expense',
    '财务费用':'finance_expense','所得税费用':'tax_expense','资产减值损失':'impairment_loss',
    '总股数':'shares_outstanding','股份总数':'shares_outstanding','有息负债合计':'interest_bearing_debt_total',
    '折旧与摊销':'depreciation_amortization',
}

def strings(node):
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [s for part in node for s in strings(part)]
    if isinstance(node, dict):
        # Only textual keys: avoid treating image base64, bbox, or IDs as document content.
        return [s for k, v in node.items() if k in ('text', 'content', 'html', 'table_body', 'body', 'spans', 'lines') for s in strings(v)]
    return []

def blocks_from_middle(middle):
    result = []
    pages = middle.get('pages', middle.get('pdf_info', []))
    for index, page in enumerate(pages):
        page_index = page.get('page_idx', index)
        for position, b in enumerate(page.get('blocks', page.get('para_blocks', []))):
            text = '\n'.join(strings(b.get('content', b.get('lines', b))))
            result.append({'page': page_index + 1, 'block': b.get('index', position),
                           'kind': b.get('type', 'text'), 'bbox': b.get('bbox'), 'text': text})
    return result

def metric(label):
    clean = compact(label)
    clean = re.sub(r'[（(](?:元/股|元|万元|亿元|%)[）)]', '', clean)
    clean = re.sub(r'^(?:[一二三四五六七八九十]+[、.．]|\d+[、.．]|减[:：]|加[:：])', '', clean)
    return METRICS.get(clean)

def period(header):
    if '变动前' in header:
        return 'before_change'
    if '变动后' in header:
        return 'after_change'
    years = re.findall(r'(?:19|20)\d{2}', header)
    if len(set(years)) == 1:
        year=years[0]
        if any(x in header for x in ('年末','12月31日')):return year+'-year-end'
        date=re.search(r'((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日',header)
        if date:return f'{date[1]}-{int(date[2]):02d}-{int(date[3]):02d}'
        if any(x in header for x in ('半年度','上半年','1-6月','1—6月','1至6月')):return year+'-H1'
        if any(x in header for x in ('1-9月','1—9月','1至9月','前三季度')):return year+'-9M'
        q=re.search(r'第([一二三四1234])季度',header)
        if q:return year+'-Q'+{'一':'1','二':'2','三':'3','四':'4'}.get(q.group(1),q.group(1))
        return year
    if any(x in header for x in ('本期', '本年', '报告期')):
        return 'current_unresolved'
    if any(x in header for x in ('上期', '上年', '去年')):
        return 'previous_unresolved'
    if '变动前' in header:
        return 'before_change'
    if '变动后' in header:
        return 'after_change'
    return None

def evidence(block, table_id=None, row=None, column=None, quote=None):
    return {'page': block['page'], 'block': block['block'], 'bbox': block['bbox'],
            'table_id': table_id, 'row': row, 'column': column,
            'quote': quote if quote is not None else block['text']}

def field(name, raw, block, unit=None, header='', **where):
    context = where.pop('context', '')
    q = quantity(raw, unit)
    reviews = []
    if q['status'] != 'parsed':
        reviews.append(q['status'])
    if q.get('value') is not None and q.get('unit') == '%':
        from decimal import Decimal
        if name in ('holder_ratio', 'capital_ratio', 'before_ratio', 'after_ratio') and not 0 <= Decimal(q['value']) <= 100:
            reviews.append('percentage_out_of_range')
    p = period(header)
    if name in METRICS.values() and not p:
        reviews.append('period_unknown')
    if p and p.endswith('_unresolved'):
        reviews.append('period_requires_document_context')
    return {'name': name, 'label': where.pop('label', name), **q, 'period': p,
            'column_header': header, 'currency': 'CNY' if q.get('unit') and '元' in q['unit'] and '人民币' in context else None,
            'review_reasons': reviews, 'evidence': evidence(block, **where)}

def headers_at(grid, stop):
    width = max(map(len, grid), default=0)
    headers = []
    for c in range(width):
        seen = []
        for row in grid[:stop]:
            if c < len(row) and row[c]['text'] and row[c]['text'] not in seen:
                seen.append(row[c]['text'])
        headers.append(' / '.join(seen))
    return headers

def repair_financial_rows(grid):
    """Join only adjacent label fragments forming an exact known metric.

    At most one row may contain numeric data; the untouched table is retained.
    """
    repaired, changes = [], []
    r = 0
    while r < len(grid):
        found = None
        for length in (1, 2, 3):
            rows = grid[r:r+length]
            if len(rows) != length or not all(rows):
                continue
            if length>1 and not all(row[0]['text'].strip() for row in rows):continue
            label = ''.join(row[0]['text'] for row in rows)
            if not metric(label):
                continue
            numeric_rows = [i for i, row in enumerate(rows) if any(quantity(cell['text'])['value'] is not None for cell in row[1:])]
            if len(numeric_rows) > 1:
                continue
            if length > 1 and not numeric_rows:
                continue
            base = rows[numeric_rows[0] if numeric_rows else 0]
            row = [dict(cell) for cell in base]
            row[0] = {**row[0], 'text': label}
            found = (length, row, r + (numeric_rows[0] if numeric_rows else 0))
            break
        if found:
            length, row, source_row = found
            repaired.append((source_row, row, list(range(r+1, r+length+1))))
            if length > 1:
                changes.append({'source_rows': list(range(r+1, r+length+1)), 'joined_label': row[0]['text']})
            r += length
        else:
            repaired.append((r, grid[r], [r+1]))
            r += 1
    return repaired, changes

def parse_financial(grid, block, table_id, context, document_year=None):
    fields = []
    first = next((r for r, row in enumerate(grid) if row and metric(row[0]['text'])), None)
    if first is None:
        return fields
    headers = headers_at(grid, first)
    table_unit = infer_unit(context)
    fixed_rows, repairs = repair_financial_rows(grid)
    column_roles=[];role=None
    for head in headers:
        if any(x in head for x in ('年初至','1-9月','1—9月','前三季度')):role='cumulative'
        elif '本报告期' in head and '期末' not in head:role='quarter'
        column_roles.append(role)
    restated=any('调整后' in h for h in headers)
    for r, row, source_rows in fixed_rows:
        if not row:
            continue
        label = row[0]['text']
        key = metric(label)
        if not key:
            continue
        for c in range(1, len(row)):
            head = headers[c] if c < len(headers) else ''
            if any(x in head for x in ('附注', '注释', '序号')):
                continue
            # Percent change columns are not level values for the metric.
            if any(x in head for x in ('增减', '增长', '变化')):
                continue
            u = infer_unit(label) or infer_unit(head) or table_unit
            if key.startswith('eps_'):
                u = '元/股'
            if key == 'roe_weighted':
                u = '%'
            raw = row[c]['text']
            q = quantity(raw, u)
            if q['status'] == 'invalid':
                continue
            f = field(key, raw, block, u, head, label=label, context=context,
                      table_id=table_id, row=r + 1, column=c + 1, quote=f'{label} | {head} | {raw}')
            f['evidence']['source_rows'] = source_rows
            if len(source_rows) > 1:
                f['review_reasons'].append('reconstructed_split_label')
            qmatch = re.search(r'第([一二三四1234])季度', head)
            if qmatch and document_year:
                quarter = {'一':'1','二':'2','三':'3','四':'4'}.get(qmatch.group(1), qmatch.group(1))
                f['period'] = f'{document_year}-Q{quarter}'
                f['period_basis'] = 'document_title_and_explicit_quarter_header'
                f['review_reasons'] = [x for x in f['review_reasons'] if x != 'period_unknown']
            f['scope'] = '母公司' if '母公司' in context[-500:] else '合并' if '合并' in context[-500:] else '未明确'
            f['period_role']=column_roles[c] if c<len(column_roles) else None
            f['basis']='调整后' if '调整后' in head else '调整前' if '调整前' in head else '调整后' if restated else '原报'
            if restated and not any(x in head for x in ('调整前','调整后')):f['basis_evidence']='本表提供调整后比较栏，当期与调整后上期比较；请复核会计调整说明'
            fields.append(f)
    return fields

PLEDGE_COLUMNS = {
    '股东名称': 'shareholder', '本次质押股数': 'pledged_shares', '本次质押数量': 'pledged_shares',
    '质押起始日': 'pledge_start', '质押到期日': 'pledge_end', '质权人': 'pledgee',
    '占其所持股份比例': 'holder_ratio', '占公司总股本比例': 'capital_ratio', '质押融资资金用途': 'purpose', '质押用途': 'purpose',
}

def parse_pledge(grid, block, table_id):
    header_end = next((r + 1 for r, row in enumerate(grid) if any('本次质押' in compact(cell['text']) and ('股数' in compact(cell['text']) or '数量' in compact(cell['text'])) for cell in row)), None)
    if not header_end:
        return []
    headers = headers_at(grid, header_end)
    columns = {}
    for c, head in enumerate(headers):
        for match, name in PLEDGE_COLUMNS.items():
            if match in compact(head):
                columns[c] = name
    if 'pledged_shares' not in columns.values():
        return []
    records = []
    for r, row in enumerate(grid[header_end:], header_end):
        if not row or compact(row[0]['text']) in ('合计', '总计', '小计'):
            continue
        record = {'type': 'pledge', 'fields': {}, 'evidence': evidence(block, table_id, r + 1)}
        for c, name in columns.items():
            if c >= len(row):
                continue
            raw = row[c]['text']
            if name in ('pledged_shares', 'holder_ratio', 'capital_ratio'):
                u = (infer_unit(headers[c]) or '股') if name == 'pledged_shares' else '%'
                record['fields'][name] = field(name, raw, block, u, headers[c], table_id=table_id,
                                               row=r + 1, column=c + 1, quote=f'{headers[c]} | {raw}')
            else:
                record['fields'][name] = {'raw': raw, 'value': raw or None, 'review_reasons': [],
                                         'evidence': evidence(block, table_id, r + 1, c + 1, raw)}
        records.append(record)
    return records

def parse_equity(grid, block, table_id):
    first = next((r for r, row in enumerate(grid) if any('变动前' in compact(cell['text']) for cell in row) and any('变动后' in compact(cell['text']) for cell in row)), None)
    if first is None:
        return []
    end = first + 1
    if end < len(grid) and any(any(x in compact(cell['text']) for x in ('持股数', '股数', '持股比例', '股份性质')) for cell in grid[end]):
        end += 1
    heads = headers_at(grid, end)
    records = []
    for r in range(end, len(grid)):
        row = grid[r]
        if not row or compact(row[0]['text']) in ('合计', '总计', '小计'):
            continue
        record = {'type': 'equity_change', 'shareholder': row[0]['text'], 'fields': {}, 'evidence': evidence(block, table_id, r + 1)}
        for c in range(1, len(row)):
            h = compact(heads[c])
            prefix = 'before' if '变动前' in h else 'after' if '变动后' in h else None
            if not prefix:
                continue
            name = prefix + ('_ratio' if '比例' in h else '_shares' if any(x in h for x in ('股数', '数量', '持股数')) else '_unknown')
            if name.endswith('_unknown'):
                continue
            u = '%' if name.endswith('_ratio') else infer_unit(heads[c]) or '股'
            record['fields'][name] = field(name, row[c]['text'], block, u, heads[c],
                                          table_id=table_id, row=r + 1, column=c + 1,
                                          quote=f"{row[0]['text']} | {heads[c]} | {row[c]['text']}")
        if all(record['fields'].get(k, {}).get('value') is not None for k in ('before_shares', 'after_shares')):
            records.append(record)
    return records

def parse_award_table(grid,block,table_id,context):
    header_end=next((r+1 for r,row in enumerate(grid) if any(any(k in compact(c['text']) for k in ('中标金额','中标价','合同金额')) for c in row)),None)
    if not header_end:return []
    heads=headers_at(grid,header_end)
    mapping={'项目名称':'project_name','工程名称':'project_name','标段名称':'project_name','招标人':'tenderer','采购人':'tenderer',
        '中标单位':'awardee','中标金额':'award_amount','中标价':'award_amount','合同金额':'contract_amount','合同期限':'contract_term'}
    cols={}
    for c,h in enumerate(heads):
        for label,name in mapping.items():
            if label in compact(h):cols[c]=name;break
    if not any(n in cols.values() for n in ('award_amount','contract_amount')) or 'project_name' not in cols.values():return []
    events=[]
    for r,row in enumerate(grid[header_end:],header_end):
        if not row or compact(row[0]['text']) in ('合计','小计','总计'):continue
        event={'type':'contract' if 'contract_amount' in cols.values() else 'award','fields':{},'evidence':evidence(block,table_id,r+1)}
        for c,name in cols.items():
            if c>=len(row):continue
            raw=row[c]['text'];quote=f'{heads[c]} | {raw}'
            if name in ('award_amount','contract_amount'):
                f=field(name,raw,block,infer_unit(heads[c]) or infer_unit(context),heads[c],table_id=table_id,row=r+1,column=c+1,quote=quote)
                f['review_reasons'].append('confirm_tax_and_contract_scope')
            else:f={'raw':raw,'value':raw or None,'review_reasons':[],'evidence':evidence(block,table_id,r+1,c+1,quote)}
            event['fields'][name]=f
        events.append(event)
    return events

def extract_middle(middle, input_name, input_hash=None):
    blocks = blocks_from_middle(middle)
    fields, events, tables, warnings = [], [], [], []
    context = ''
    title_text = '\n'.join(b['text'] for b in blocks[:20])
    year_match = re.search(r'((?:19|20)\d{2})\s*年?\s*(?:年度报告|半年度报告|第[一二三四1234]季度报告)', title_text)
    document_year = year_match.group(1) if year_match else None
    issuer_match=re.search(r'([\u4e00-\u9fffA-Za-z（）()·]{3,60}(?:股份有限公司|集团有限公司|有限责任公司))',title_text)
    issuer=issuer_match.group(1) if issuer_match else None
    report_type='H1' if '半年度报告' in title_text[:2000] else '9M' if '第三季度报告' in title_text[:2000] else 'Q1' if '第一季度报告' in title_text[:2000] else ''
    qtitle=re.search(r'第([一二三四1234])季度报告',title_text[:2000]); report_quarter={'一':'1','二':'2','三':'3','四':'4'}.get(qtitle[1],qtitle[1]) if qtitle else None
    active_scope=None
    for b in blocks:
        if '<table' in b['text'].lower():
            for grid in read_tables(b['text']):
                tid = f"p{b['page']}-b{b['block']}-t{len(tables)+1}"
                tables.append({'id': tid, 'page': b['page'], 'block': b['block'], 'bbox': b['bbox'],
                               'rows': [[cell['text'] for cell in row] for row in grid],
                               'cells': grid, 'original_html': b['text']})
                added=parse_financial(grid, b, tid, context, document_year)
                for f in added:
                    if active_scope:f['scope']=active_scope
                fields.extend(added)
                events.extend(parse_pledge(grid, b, tid))
                events.extend(parse_equity(grid, b, tid))
                events.extend(parse_award_table(grid,b,tid,context))
        else:
            context = (context + '\n' + b['text'])[-3000:]
            headline=compact(b['text'])
            if re.search(r'母公司(?:资产负债表|利润表|现金流量表|财务报表)',headline):active_scope='母公司'
            elif re.search(r'合并(?:资产负债表|利润表|现金流量表|财务报表)',headline):active_scope='合并'
            # Explicit bid amount only; receipt/date/title numbers are not amounts.
            text = b['text']
            for match in re.finditer(r'(中标金额|中标价|合同金额)\s*(?:[（(](含税|不含税)[）)])?\s*[:：为]?\s*(?:人民币)?\s*([\d,，.．]+\s*(?:亿元|万元|元))', text):
                raw = match.group(3)
                contract = match.group(1) == '合同金额'
                name = 'contract_amount' if contract else 'award_amount'
                item = field(name, raw, b, quote=match.group(0), context=text)
                item['review_reasons'].append('confirm_tax_and_contract_scope')
                if match.group(2):item['tax_basis']=match.group(2)
                events.append({'type': 'contract' if contract else 'award', 'fields': {name: item}, 'evidence': evidence(b, quote=match.group(0))})
    for f in fields:
        f['issuer']=issuer
        f.setdefault('basis','原报')
        p=f.get('period')
        if document_year and p in ('current_unresolved','previous_unresolved'):
            y=int(document_year)-(p=='previous_unresolved')
            suffix='Q'+report_quarter if report_quarter and f.get('period_role')=='quarter' else report_type
            if any(x in f.get('column_header','') for x in ('上年度末','上年末')):suffix='year-end'
            elif '期末' in f.get('column_header','') and '年初至' not in f.get('column_header','') and report_quarter:
                suffix=f'{int(report_quarter)*3:02d}-'+('31' if report_quarter in ('1','4') else '30')
            f['period']=str(y)+('-'+suffix if suffix else '')
            f['period_basis']='report_title_and_current_previous_header'
            f['review_reasons']=[x for x in f['review_reasons'] if x!='period_requires_document_context']
        elif report_type and p and re.fullmatch(r'(?:19|20)\d{2}',p):
            if not any(x in f.get('column_header','') for x in ('年末','年度','12月31日')):
                suffix='Q'+report_quarter if report_quarter and f.get('period_role')=='quarter' else report_type
                f['period']=p+'-'+suffix
                f['period_basis']='report_title_and_year_header'
    # Keep originals; repeated or conflicting values are not silently resolved.
    grouped = {}
    for f in fields:
        grouped.setdefault((f['name'], f['period'], f['column_header']), []).append(f)
    for group in grouped.values():
        if len({(f['value'], f['unit']) for f in group}) > 1:
            for f in group:
                f['review_reasons'].append('conflicting_values_or_scope')
    if not fields and not events:
        warnings.append('未识别到支持的金融字段。请查看原始内容和表格；不能据此认定文档没有相关信息。')
    if not blocks:
        warnings.append('解析结果没有可识别的页/块结构，需检查 MinerU 版本和输出格式。')
    return {'schema': 'finai.extraction', 'schema_version': '1.0',
            'source': {'filename': input_name, 'sha256': input_hash},
            'engine': middle.get('metadata', {}).get('producer'), 'engine_options': middle.get('extensions', {}).get('mineru'),
            'fields': fields, 'events': events, 'tables': tables, 'blocks': blocks, 'warnings': warnings,
            'review_required': any(f['review_reasons'] for f in fields) or any(f.get('review_reasons') for e in events for f in e['fields'].values())}
