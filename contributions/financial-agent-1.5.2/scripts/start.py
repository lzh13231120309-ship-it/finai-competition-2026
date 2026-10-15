"""Create only this project's environment, then serve on loopback."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
import venv
import webbrowser

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=17899)
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--python',help='Use an already prepared Python executable')
    args=parser.parse_args()
    if sys.version_info<(3,11):raise SystemExit('Requires Python 3.11 or newer')
    python=Path(args.python).absolute() if args.python else ROOT/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not args.python and (not python.exists() or not (ROOT/'.venv/.ready').exists()):
        venv.EnvBuilder(with_pip=True).create(ROOT/'.venv')
        subprocess.run([str(python),'-m','pip','install','-r',str(ROOT/'requirements.txt')],check=True)
        (ROOT/'.venv/.ready').touch()
    ocr=ROOT/'server/native/finocr'
    if sys.platform=='darwin' and not ocr.exists() and shutil.which('xcrun'):
        sdk=os.environ.get('FINAGENT_MACOS_SDK')
        flags=['-sdk',sdk] if sdk else []
        result=subprocess.run(['xcrun','swiftc','-O',*flags,'-framework','Vision','-framework','AppKit',str(ROOT/'server/native/ocr.swift'),'-o',str(ocr)])
        if result.returncode:print('OCR compiler unavailable; scanned documents will require another backend.')
    env={**os.environ,'FINAGENT_DATA_DIR':str(ROOT/'data'),'PYTHONDONTWRITEBYTECODE':'1'}
    proc=subprocess.Popen([str(python),'-m','server.launcher','--port',str(args.port),'--no-parent-watch'],cwd=ROOT,env=env)
    try:
        url=f'http://127.0.0.1:{args.port}'
        for _ in range(600):
            if proc.poll() is not None:raise SystemExit('Service failed to start')
            try:
                with urllib.request.urlopen(url+'/api/health',timeout=.5) as response:
                    if response.status==200:break
            except OSError:time.sleep(.1)
        else:raise SystemExit('Service startup timed out')
        print('Agent is ready:',url,flush=True)
        if not args.no_browser:webbrowser.open(url)
        proc.wait()
    except KeyboardInterrupt:pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=8)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()

if __name__=='__main__':main()
