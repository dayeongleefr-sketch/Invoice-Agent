"""Content-addressed local originals, calculator and Python for exact rule replay.

Only trusted rule confirmation creates an archive. An immutable ledger audit
pins the manifest; replay never writes current sources, results or rule state.
"""
from datetime import datetime, timezone
import hashlib
import os
from portable_replay_runtime import environment, choose, description
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

from business_memory import canonical, digest
from chat_commands import source_snapshot
from chat_intake import _safe_file

BASE=Path('data/demo_company/memory/replay')


def file_hash(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def ledger_manifest(store,rule_id):
    with store.memory._connect() as db:
        if not store._schema(db):return None
        row=db.execute("SELECT detail_json FROM memory_rule_audit WHERE event='replay_assets_saved' AND rule_id=?",(rule_id,)).fetchone()
        return json.loads(row[0])['manifest_hash'] if row else None


def capture(commands, receipt, sources):
    root=commands.root;rule_id=receipt['rule_id']
    if ledger_manifest(commands.store,rule_id):return True
    # Isolated tests/read-only installations may not include the portable runtime.
    runtime=_safe_file(root,Path('ia/.runtime/python'))
    runtime_available=os.name=='nt' and (runtime/'python.exe').is_file()
    captured_environment=environment()
    payload=receipt['payload']
    if digest(sources)!=receipt['source_fingerprint']:
        raise ValueError('기준 저장 당시 원본과 재현 자료의 버전이 다릅니다.')
    paths={relative:entry['sha256'] for relative,entry in sources['files'].items()}
    if (root/'requirements.txt').is_file():paths['requirements.txt']=file_hash(root/'requirements.txt')
    for folder in ([root/'ia/src',runtime] if runtime_available else [root/'ia/src']):
        for p in folder.rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.log'):
                if folder==runtime and p.suffix.lower() not in ('.exe','.dll','.pyd','.zip','.py','._pth','.txt'):continue
                paths[p.relative_to(root).as_posix()]=file_hash(_safe_file(root,p.relative_to(root)))
    archive=_safe_file(root,BASE);blobs=archive/'objects';blobs.mkdir(parents=True,exist_ok=True)
    for relative,expected in paths.items():
        src=_safe_file(root,Path(relative));dest=_safe_file(root,BASE/'objects'/expected)
        if not dest.exists():
            tmp=dest.with_name(expected+'.'+uuid.uuid4().hex+'.tmp')
            try:
                shutil.copyfile(src,tmp)
                if file_hash(tmp)!=expected:raise ValueError('재현 자료를 보관하는 동안 원본이 변경되었습니다.')
                tmp.replace(dest)
            finally:tmp.unlink(missing_ok=True)
        elif file_hash(dest)!=expected:raise ValueError('기존 재현 자료의 해시가 다릅니다.')
    if digest(source_snapshot(root))!=digest(sources):raise ValueError('자료가 변경되어 정확 재현 보관을 완료하지 않았습니다.')
    record=commands.memory.get_record(receipt['record_id'])
    record['payload'].update(targets=payload['targets'],source_snapshot=sources)
    manifest={'schema':'rule-replay-v2','environment':captured_environment,'rule_id':rule_id,'files':paths,'record':record,
              'spec':payload['spec'],'calculation':payload['calculation'],
              'source_fingerprint':receipt['source_fingerprint'],'code_fingerprint':receipt['code_fingerprint']}
    if payload['spec']['kind']=='formula' and payload['spec']['parameters'].get('result_references'):
        import copy
        revision=commands.memory.revision()
        events={e['record_id']:e for e in commands.store.list_rules()}
        captured={}
        def collect(rid,stack):
            if rid in stack or len(stack)>=20:raise ValueError('순환·과도한 참조는 정확 재현 보관 대상이 아닙니다.')
            if rid in captured:return
            event=copy.deepcopy(events.get(rid))
            if not event or event['stopped'] or event['invalidated']:raise ValueError('앞 계산의 유효한 기록이 없습니다.')
            upstream,_=commands._record(rid,sources)
            event['_replay_record']=upstream
            captured[rid]=event
            for child in event['payload']['spec']['parameters'].get('result_references',[]):collect(child,(*stack,rid))
        for rid in payload['spec']['parameters']['result_references']:collect(rid,(receipt['record_id'],))
        if revision!=commands.memory.revision():raise ValueError('계산 기준이 변경되어 재현 보관을 다시 해야 합니다.')
        manifest['dependencies']=list(captured.values())
    raw=canonical(manifest).encode('utf-8');stamp=hashlib.sha256(raw).hexdigest()
    dest=_safe_file(root,BASE/'manifests'/(stamp+'.json'));dest.parent.mkdir(parents=True,exist_ok=True)
    tmp=dest.with_suffix('.'+uuid.uuid4().hex+'.tmp');tmp.write_bytes(raw);tmp.replace(dest)
    with commands.memory._connect(write=True) as db:
        if not db.execute("SELECT 1 FROM memory_rule_audit WHERE event='replay_assets_saved' AND rule_id=?",(rule_id,)).fetchone():
            revision=db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]
            db.execute('UPDATE memory_head SET revision=? WHERE singleton=1 AND revision=?',(revision+1,revision))
            commands.store._audit(db,'replay_assets_saved',receipt['proposal_id'],revision+1,datetime.now(timezone.utc),rule_id,{'manifest_hash':stamp})
    return True


def replay(commands,selected):
    root=commands.root;stamp=ledger_manifest(commands.store,selected['rule_id'])
    if not stamp:return None
    path=_safe_file(root,BASE/'manifests'/(stamp+'.json'))
    if file_hash(path)!=stamp:raise ValueError('보관된 재현 명세가 변경되었습니다.')
    manifest=json.loads(path.read_text('utf-8'))
    if manifest['rule_id']!=selected['rule_id'] or manifest['calculation']!=selected['payload']['calculation'] or manifest['source_fingerprint']!=selected['source_fingerprint'] or manifest['code_fingerprint']!=selected['code_fingerprint']:
        raise ValueError('보관 자료와 승인된 기준 기록이 다릅니다.')
    parent=_safe_file(root,Path('ia/.runtime'));parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='rule_replay_',dir=parent) as td:
        stage=Path(td).resolve()
        for relative,h in manifest['files'].items():
            rel=Path(relative)
            if rel.is_absolute() or '..' in rel.parts or ':' in relative or not re.fullmatch(r'[a-f0-9]{64}',h):raise ValueError('잘못된 재현 파일 경로입니다.')
            target=stage/rel
            if not target.resolve().is_relative_to(stage):raise ValueError('재현 경로가 격리 범위를 벗어납니다.')
            blob=_safe_file(root,BASE/'objects'/h)
            if file_hash(blob)!=h:raise ValueError('보관된 원본 또는 실행 코드가 변경되었습니다.')
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(blob,target)
        (stage/'replay_input.json').write_text(canonical({'record':manifest['record'],'spec':manifest['spec'],'dependencies':manifest.get('dependencies',[])}),encoding='utf-8')
        # Selection IDs include file timestamps for UI change detection. Restored
        # files have new timestamps: rebind only after every semantic target field
        # agrees, then map IDs back for exact comparison with the approved result.
        script="""import json,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'ia/src'))
import rule_adapters
x=json.loads(Path('replay_input.json').read_text('utf-8'));ids={}
records=[x['record']]+[d['_replay_record'] for d in x.get('dependencies',[])]
for record in records:
 targets=record['payload'].get('targets',[])
 if not targets:continue
 from chat_intake import _current,_target
 current=_current(Path.cwd());rebound=[]
 for old in targets:
  index=int(old['issue_id']);new=_target(current['rows'][index],index,current['version'])
  fields=lambda t:{k:v for k,v in t.items() if k not in ('target_id','alias')}
  if fields(old)!=fields(new):raise ValueError('archived_target_mismatch')
  ids[new['target_id']]=old['target_id'];rebound.append(new)
 record['payload']['targets']=rebound
if x.get('dependencies'):
 import formula_rules
 r=formula_rules.calculate(Path.cwd(),x['record'],x['spec']['parameters'],_events=x['dependencies'])
else:
 r=rule_adapters.calculate(Path.cwd(),x['record'],x['spec']['kind'],x['spec']['parameters'])
for change in r.get('changes',[]):
 if change.get('target_id') in ids:change['target_id']=ids[change['target_id']]
Path('replay_output.json').write_text(json.dumps(r,ensure_ascii=False),encoding='utf-8')
"""
        executable,replay_mode=choose(manifest,stage)
        proc=subprocess.run([executable,'-I','-X','utf8','-c',script],cwd=stage,capture_output=True,timeout=600)
        if proc.returncode:raise ValueError('보관된 환경의 재계산에 실패했습니다. 현재 결과는 유지됩니다.')
        calculation=json.loads((stage/'replay_output.json').read_text('utf-8'))
        if calculation!=manifest['calculation']:raise ValueError('보관 당시 값과 재계산값이 다릅니다. 정확 재현으로 표시하지 않습니다.')
    p=selected['payload']
    return dict(state='comparison',lifecycle_action='exact_replay',record_id=selected['record_id'],selected_rule_id=selected['rule_id'],
                selected_rule_version=selected['rule_version'],month=p['month'],scope=p['scope'],application=p['application'],spec=p['spec'],
                changes=calculation['changes'],limitations=calculation['limitations'],transitions=[],target_count=len(p['targets']),
                calculation_executed=True,changes_applied=False,base_calculation_unchanged=True,future_rule_applied=False,
                replay_mode=replay_mode,archived_environment=manifest.get('environment'),current_environment=environment(),
                operating_system_cloned=False,message=description(replay_mode))
