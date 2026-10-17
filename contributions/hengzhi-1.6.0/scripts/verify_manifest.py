"""Check the shared source manifest without reading recipient state."""
from pathlib import Path
import hashlib,json,sys
root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'MANIFEST.json').read_text(encoding='utf-8'))
errors=[]
for item in manifest['files']:
    rel=Path(item['path'])
    if rel.is_absolute() or '..' in rel.parts:
        errors.append('unsafe manifest entry');continue
    path=root/rel
    if not path.is_file() or path.is_symlink():
        errors.append(str(rel));continue
    if hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
        errors.append(str(rel))
print(json.dumps({'checked':len(manifest['files']),'passed':not errors,'changed_or_missing':errors},ensure_ascii=False))
sys.exit(bool(errors))
