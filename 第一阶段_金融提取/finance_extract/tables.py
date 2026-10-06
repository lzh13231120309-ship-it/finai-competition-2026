"""Expand HTML table spans while retaining coordinates and source values."""
from html.parser import HTMLParser
import re

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.rows = []
        self.row = None
        self.cell = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table':
            self.depth += 1
            if self.depth == 1:
                self.rows = []
        if self.depth != 1:
            return
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th'):
            def span(key):
                try:
                    return max(1, min(100, int(attrs.get(key, '1'))))
                except ValueError:
                    return 1
            self.cell = {'text': '', 'rowspan': span('rowspan'), 'colspan': span('colspan'), 'header': tag == 'th'}
        elif tag == 'br' and self.cell is not None:
            self.cell['text'] += ' '

    def handle_data(self, text):
        if self.depth == 1 and self.cell is not None:
            self.cell['text'] += text

    def handle_endtag(self, tag):
        if self.depth == 1:
            if tag in ('td', 'th') and self.cell is not None:
                self.cell['text'] = re.sub(r'\s+', ' ', self.cell['text']).strip()
                if self.row is not None:
                    self.row.append(self.cell)
                self.cell = None
            elif tag == 'tr' and self.row is not None:
                self.rows.append(self.row)
                self.row = None
            elif tag == 'table':
                self.tables.append(expand(self.rows))
        if tag == 'table':
            self.depth = max(0, self.depth - 1)

def expand(rows):
    grid = {}
    for r, cells in enumerate(rows):
        c = 0
        for cell in cells:
            while (r, c) in grid:
                c += 1
            for dr in range(cell['rowspan']):
                for dc in range(cell['colspan']):
                    grid[r + dr, c + dc] = {'text': cell['text'], 'origin': [r, c], 'header': cell['header']}
            c += cell['colspan']
    if not grid:
        return []
    height = max(r for r, _ in grid) + 1
    width = max(c for _, c in grid) + 1
    if height > 10000 or width > 300:
        raise ValueError('Table too large; requires review')
    return [[grid.get((r, c), {'text': '', 'origin': [r, c], 'header': False}) for c in range(width)] for r in range(height)]

def read_tables(html):
    parser = TableParser()
    parser.feed(html)
    return parser.tables

def compact(text):
    return re.sub(r'\s+', '', text)
