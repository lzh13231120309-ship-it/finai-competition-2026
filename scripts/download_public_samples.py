"""Recreate public PDFs from the original source with SHA256 validation."""
from pathlib import Path
import urllib.request, hashlib, json
ROOT=Path(__file__).resolve().parents[1]/'金融测试集_100份'
def main():
    rows=json.loads((ROOT/'管理/文件清单.json').read_text(encoding='utf-8'));failures=[]
    for row in rows:
        if row['origin']!='public_original':continue
        target=(ROOT/Path(row['path'].replace('\\','/'))).resolve()
        if not target.is_relative_to(ROOT.resolve()):raise ValueError('样例路径越界')
        try:
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()==row['sha256']:continue
            with urllib.request.urlopen(urllib.request.Request(row['url'],headers={'User-Agent':'Mozilla/5.0'}),timeout=90) as r:data=r.read()
            if hashlib.sha256(data).hexdigest()!=row['sha256']:raise ValueError('来源文件已变化，指纹不一致')
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data);print(row['id'],'已下载')
        except Exception as e:failures.append({'id':row['id'],'error':str(e)});print(row['id'],'失败',e)
    if failures:raise SystemExit(f'有{len(failures)}份未恢复，请稍后重试。保留集只在冻结版本验收时使用。')
    print('公开材料指纹检查完成；结合仓库40份模拟材料组成原100份测试集。')
if __name__=='__main__':main()
