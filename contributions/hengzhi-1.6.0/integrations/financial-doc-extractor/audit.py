"""Local, one-file-per-invocation audit records; never log passwords or API keys."""
import hashlib
import json
import os
import platform
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4


def environment():
    return {'python':platform.python_version(),**{name:version(name) for name in ('pymupdf','pdfplumber','mcp')}}


def write_audit(directory,tool,arguments,result,document=None):
    path=Path(directory).expanduser();path.mkdir(parents=True,exist_ok=True,mode=0o700)
    args={k:('[REDACTED]' if any(s in k.lower() for s in ('password','token','key','secret')) else v) for k,v in arguments.items()}
    canonical=json.dumps(result,ensure_ascii=False,sort_keys=True,allow_nan=False)
    record={'timestamp_utc':datetime.now(timezone.utc).isoformat(),'tool':tool,'arguments':args,'environment':environment(),'result_sha256':hashlib.sha256(canonical.encode()).hexdigest(),'result':result}
    if document is not None:record['document']=document
    target=path/(uuid4().hex+'.json')
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(record,f,ensure_ascii=False,indent=2,allow_nan=False)
    return target
