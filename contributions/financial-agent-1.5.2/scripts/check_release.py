"""Fail closed if private state or credential-like content enters the release tree."""
from pathlib import Path
import json,re,sys
ROOT=Path(__file__).resolve().parents[1]
BAD_DIRS={'data','.venv','.venv-mcp','__pycache__','.git','node_modules','exports','backups'}
PATTERNS=[re.compile(r'[/]Users[/][^/\s]+/'),re.compile(r'[/]home[/][^/\s]+/'),re.compile(r'(?i)[A-Z]:\\Users\\[^\\\s]+\\'),re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),re.compile(r'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{30,})\b')]
errors=[];checked=0
for path in ROOT.rglob('*'):
    if not path.is_file():continue
    rel=path.relative_to(ROOT)
    # Runtime ignored paths are never release content. Check tracked/manifest files before publishing.
    if set(rel.parts)&BAD_DIRS:
        if '--strict' in sys.argv:errors.append((str(rel),'private/runtime directory must not be packaged'))
        continue
    if path.is_symlink():errors.append((str(rel),'symlink'));continue
    if path.suffix.lower() in {'.db','.sqlite','.pdf','.docx','.xlsx','.pyc','.pem','.key','.log','.gguf','.onnx'}:
        errors.append((str(rel),'unexpected private document, binary or state'));continue
    raw=path.read_bytes()
    try:text=raw.decode('utf-8')
    except UnicodeDecodeError:errors.append((str(rel),'unexpected binary'));continue
    checked+=1
    for pattern in PATTERNS:
        if pattern.search(text):errors.append((str(rel),'path or credential pattern'))
    if path.name.endswith('.json'):
        try:
            value=json.loads(text)
            if isinstance(value,dict) and value.get('api_keys') and any(value['api_keys'].values()):errors.append((str(rel),'nonempty credentials'))
        except ValueError:pass
print(json.dumps({'checked_files':checked,'passed':not errors,'findings':errors},ensure_ascii=False))
sys.exit(bool(errors))
