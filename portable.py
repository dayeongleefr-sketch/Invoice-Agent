"""Local web application launcher and portable backup; no credentials are packaged."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile

ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT/'frontend'),str(ROOT/'ia/src')]


@contextmanager
def exclusive(root=ROOT):
    windows = os.name == 'nt'
    if windows:
        import msvcrt
    else:
        import fcntl
    path=root/'portable.lock'
    with path.open('a+b') as stream:
        if stream.tell()==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        try:
            if windows:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:raise ValueError('앱 또는 백업이 실행 중입니다. 실행 창을 닫은 후 다시 시도하세요.') from None
        try:yield
        finally:
            stream.seek(0)
            if windows:msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def initialize(root=ROOT,recalculate=False):
    from demo_company_workflow import calculate,verify_artifacts,BASE,load
    current=root/BASE/'workflow_result.json'
    if current.exists() and not recalculate:
        value=load(current);verify_artifacts(root,value)
        return value
    return calculate(root)


def backup(root=ROOT):
    from agent_adapter import load,worker_alive
    for name in ('job.json','chat_job.json'):
        state=load(root/'frontend/.runtime'/name,{})
        if state.get('status')=='running' and worker_alive(state.get('worker_pid')):
            raise ValueError('검증 작업이 아직 실행 중입니다. 완료된 후 백업하세요.')
    manifest=load(root/'MANIFEST.json')
    if not manifest:raise ValueError('배포 명세가 없습니다.')
    paths={root/r for r in manifest['files'] if (root/r).is_file()}
    paths.add(root/'MANIFEST.json')
    for folder in ('data/demo_company','ia/config'):
        paths.update(p for p in (root/folder).rglob('*') if p.is_file() and p.suffix not in ('.lock','.tmp'))
    for name in ('conversations.json','last_verification_job.json'):
        p=root/'frontend/.runtime'/name
        if p.is_file():paths.add(p)
    files={}
    for path in sorted(paths):
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink() or any(p.is_symlink() or p.is_junction() for p in path.parents if p!=root.parent):
            raise ValueError('외부 연결 경로는 백업하지 않습니다.')
        relative=path.relative_to(root).as_posix()
        if any(part in {'.git','.codex','.venv','__pycache__'} for part in path.relative_to(root).parts):continue
        files[relative]=path.read_bytes()
    hashes={n:hashlib.sha256(b).hexdigest() for n,b in files.items()}
    files['BACKUP_MANIFEST.json']=(json.dumps({'files':hashes,'credentials_included':False,'runtime_reinstalled_by_setup':True},ensure_ascii=False,indent=2)+'\n').encode()
    folder=root/'backups';folder.mkdir(exist_ok=True)
    target=folder/('agent_backup_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.zip')
    with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for name,data in files.items():
            info=zipfile.ZipInfo('logistics_agent_full/'+name)
            info.create_system=3
            info.external_attr=(0o100755 if name.endswith(('.command','.sh')) else 0o100644)<<16
            info.compress_type=zipfile.ZIP_DEFLATED
            z.writestr(info,data)
    with zipfile.ZipFile(target) as z:
        for name,h in hashes.items():
            if hashlib.sha256(z.read('logistics_agent_full/'+name)).hexdigest()!=h:raise ValueError('백업 지문 검증 실패')
    return target


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['init','serve','calculate','backup','check-backup'])
    parser.add_argument('--port',type=int,default=8090)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    with exclusive():
        if args.action=='backup':print(backup());return
        if args.action=='check-backup':
            data=json.loads((ROOT/'BACKUP_MANIFEST.json').read_text('utf-8'))
            for name,h in data['files'].items():
                path=ROOT/name
                if not path.resolve().is_relative_to(ROOT) or hashlib.sha256(path.read_bytes()).hexdigest()!=h:raise ValueError('복구 파일 지문 불일치: '+name)
            print('백업 파일 지문 일치');return
        value=initialize(recalculate=args.action=='calculate')
        print('가상 자료 계산 상태:',value['processing_status'],'미확정 질문:',len(value['questions']))
        if args.action!='serve':return
        with socket.socket() as sock:
            try:sock.bind(('127.0.0.1',args.port))
            except OSError:raise ValueError('포트가 사용 중입니다. Windows: Start.ps1 -Port 8091 / Mac: bash 02_START.command --port 8091') from None
        os.environ['DEMO_WEB_PORT']=str(args.port)
        if not args.no_browser:
            def open_when_ready():
                for _ in range(100):
                    try:
                        urllib.request.urlopen(f'http://127.0.0.1:{args.port}',timeout=1).close()
                        webbrowser.open(f'http://127.0.0.1:{args.port}');return
                    except OSError:time.sleep(.25)
            threading.Thread(target=open_when_ready,daemon=True).start()
        runpy.run_path(str(ROOT/'frontend/app.py'),run_name='__main__')


if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as exc:
        print(str(exc),file=sys.stderr);raise SystemExit(1)
